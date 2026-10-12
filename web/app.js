/* Inkwell & Co. — web client.
 *
 * Plain ES modules-free JavaScript, no build step: open the page and it runs.
 * Every call goes to the REST API, which in turn calls the same business
 * rules the desktop client uses, so there is no logic here beyond presentation
 * and no duplicated rule to drift.
 */

"use strict";

const API = "/api";
const CATEGORY_COLOUR = {
  literature: "#e8622c", science: "#1f7a6b", history: "#8c2f39",
  philosophy: "#4a3a82", poetry: "#b5476b", children: "#e0a32e",
  technology: "#2d6e9e", biography: "#5c6b3f",
};
const colour = (c) => CATEGORY_COLOUR[(c || "").toLowerCase()] || "#4d5a74";

const state = {
  token: localStorage.getItem("inkwell.token") || null,
  session: null,
  axis: "categories",
  facet: null,
  facets: null,
};

/* ------------------------------------------------------------------ util */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function money(n) { return "$" + Number(n || 0).toFixed(2); }

function stars(n) {
  const k = Math.round(Number(n) || 0);
  return "★".repeat(Math.max(0, Math.min(5, k))) + "☆".repeat(Math.max(0, 5 - k));
}

let toastTimer;
function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 5200);
}

async function api(path, { method = "GET", body, raw } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (body && !raw) headers["Content-Type"] = "application/json";

  const response = await fetch(API + path, {
    method,
    headers,
    body: raw ? body : (body ? JSON.stringify(body) : undefined),
  });

  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}

/* --------------------------------------------------------------- session */

async function refreshSession() {
  try {
    state.session = await api("/auth/session");
  } catch {
    state.token = null;
    localStorage.removeItem("inkwell.token");
    state.session = { signed_in: false, role: "VS", role_name: "Visitor" };
  }
  paintSession();
}

function paintSession() {
  const s = state.session || {};
  const signedIn = !!s.signed_in;

  $("#who-text").textContent = signedIn
    ? `${s.display_name || s.username} · ${s.role_name}` +
      (s.balance != null ? ` · ${money(s.balance)}` : "")
    : "Browsing as a visitor";

  $("#auth-btn").textContent = signedIn ? "Sign out" : "Sign in";
  $("#nav-account").hidden = !signedIn || s.role === "SU";
  $("#nav-admin").hidden = s.role !== "SU";

  const scopes = {
    VS: "You can ask about our books, categories, prices and shop policies. Your own account and the shop's internals need an account.",
    RC: "You can also ask about your balance, your purchases and what to read next.",
    VC: "You can also ask about the to-order and no-order lists, and your discount.",
    SU: "You can also ask about finances, every account, moderation and stock.",
  };
  $("#chat-scope").textContent = scopes[s.role] || scopes.VS;
  paintSuggestions();
}

/* ----------------------------------------------------------------- shelf */

function spine(book, rank) {
  const el = document.createElement("button");
  el.className = "spine";
  el.setAttribute("role", "listitem");
  el.style.setProperty("--spine", colour(book.category));
  el.style.height = (168 + ((book.title.length * 7) % 92)) + "px";
  if (book.title.length > 34) el.classList.add("wide");
  el.dataset.status = book.list_status;
  el.dataset.oos = book.stock === 0 ? "1" : "0";
  el.setAttribute("aria-label",
    `${book.title} by ${book.author}, ${money(book.your_price)}`);
  el.innerHTML =
    (rank ? `<span class="spine-rank">${rank}</span>` : "<span></span>") +
    `<span class="spine-text">${esc(book.title)}` +
      `<span class="spine-author">  ${esc(book.author)}</span></span>` +
    `<span class="spine-foot">${money(book.your_price)}</span>`;
  el.addEventListener("click", () => openBook(book.book_id));
  return el;
}

function fillShelf(container, books, ranked) {
  container.replaceChildren();
  books.forEach((b, i) => container.appendChild(spine(b, ranked ? i + 1 : 0)));
}

async function loadPicks() {
  const recs = await api("/recommendations");
  const s = state.session || {};
  const personal = s.signed_in && (s.role === "RC" || s.role === "VC");

  $("#shelf-title").textContent = personal
    ? `Picked for ${esc(s.display_name || s.username).split(" ")[0]}`
    : "On the shelf";
  $("#shelf-sub").textContent = personal
    ? "Chosen from the books you have browsed and bought. Anything we have stopped reordering is left out."
    : "The three most bought and most viewed books in the shop. Sign in and these become yours instead.";

  fillShelf($("#picks"), recs.map((r) => r.book), true);
  [...$("#picks").children].forEach((el, i) => {
    el.title = recs[i].reason;
  });
}

async function loadFacets() {
  state.facets = await api("/books/facets");
  paintFacets();
}

function paintFacets() {
  const list = (state.facets && state.facets[state.axis]) || [];
  const box = $("#facets");
  box.replaceChildren();
  list.forEach((f) => {
    const b = document.createElement("button");
    b.className = "chip" + (state.facet === f.value ? " on" : "");
    if (state.axis === "categories") b.style.setProperty("--chip", colour(f.value));
    b.innerHTML = `${esc(f.value)}<em>${f.count}</em>`;
    b.addEventListener("click", () => {
      state.facet = state.facet === f.value ? null : f.value;
      paintFacets();
      loadShelf();
    });
    box.appendChild(b);
  });
}

async function loadShelf() {
  const params = new URLSearchParams();
  if (state.facet) {
    const key = { categories: "category", authors: "author", decades: "decade" }[state.axis];
    params.set(key, state.facet);
  }
  const books = await api("/books" + (params.toString() ? "?" + params : ""));
  fillShelf($("#shelf"), books, false);
  $("#shelf-empty").hidden = books.length > 0;
}

/* ------------------------------------------------------------ book sheet */

function card(book, why) {
  const el = document.createElement("button");
  el.className = "card";
  el.style.setProperty("--spine", colour(book.category));
  el.innerHTML =
    (book.cover_url ? `<img src="${book.cover_url}" alt="">` : "") +
    `<span><span class="card-t">${esc(book.title)}</span>` +
    `<span class="card-a">${esc(book.author)}</span>` +
    `<span class="card-m">${money(book.your_price)} · ` +
      (book.rating ? `${stars(book.rating)} ${book.rating.toFixed(1)}` : "no ratings") +
    `</span>` +
    (why ? `<span class="card-why">${esc(why)}</span>` : "") +
    `</span>`;
  el.addEventListener("click", () => openBook(book.book_id));
  return el;
}

async function openBook(bookId) {
  const book = await api("/books/" + bookId);
  const s = state.session || {};
  const discounted = book.your_price < book.price;

  const statusNote = {
    "to-order": `<div class="note good">More than three ratings averaging above 4, so this is on our to-order list — we keep it in stock.</div>`,
    "no-order": `<div class="note bad">More than three ratings averaging below 2, so this is on our no-order list. We will not be reordering it.</div>`,
  }[book.list_status] || "";

  $("#sheet-body").innerHTML = `
    <div class="detail-top">
      ${book.cover_url ? `<img class="detail-cover" src="${book.cover_url}" alt="Cover of ${esc(book.title)}">` : ""}
      <div class="detail-main">
        <h2 id="sheet-title">${esc(book.title)}</h2>
        <p class="detail-author">${esc(book.author)}</p>
        <div class="facts">
          ${[["Category", book.category], ["Published", book.year || "unknown"],
             ["In stock", book.stock], ["Viewed", book.browse_count],
             ["Sold", book.purchase_count]]
            .map(([k, v]) => `<div><div class="fact-k">${k}</div><div class="fact-v">${esc(v)}</div></div>`)
            .join("")}
        </div>
        <p class="rating-line">${
          book.rating_count
            ? `<span class="stars">${stars(book.rating)}</span> ${book.rating.toFixed(2)} weighted
               · ${book.plain_rating.toFixed(2)} plain · ${book.rating_count} rating${book.rating_count === 1 ? "" : "s"}
               · ${book.verified_buyers} verified buyer${book.verified_buyers === 1 ? "" : "s"}`
            : "No ratings yet."
        }</p>
        ${statusNote}
        ${book.description ? `<p class="serif">${esc(book.description)}</p>` : ""}
        <div class="price-row">
          <span class="price">${money(book.your_price)}</span>
          ${discounted ? `<span class="price-was">${money(book.price)}</span>
                          <span class="price-off">VIP discount applied</span>` : ""}
        </div>
        <div id="buy-area"></div>
      </div>
    </div>
    ${book.figures.length ? `<h3>Figures from inside</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap">${
        book.figures.map((f) => `<img src="${f}" alt="" style="width:170px;border-radius:2px">`).join("")
      }</div>` : ""}
    <h3>Ratings and comments</h3>
    <p style="font-size:.86rem;color:var(--graphite-2)">
      Comments from customers who bought the book count double in the average.
      Taboo words are masked, and a comment with more than three is blocked.
    </p>
    <div id="review-form"></div>
    <div id="review-list">${
      book.reviews.length
        ? book.reviews.map(reviewHtml).join("")
        : `<p style="color:var(--graphite-2)">No ratings yet. Be the first.</p>`
    }</div>
  `;

  paintBuyArea(book);
  paintReviewForm(book);
  $("#sheet").hidden = false;
  $("#sheet-card, .sheet-card")?.scrollTo?.(0, 0);
}

function reviewHtml(r) {
  return `<div class="review">
    <div class="review-h">
      <span class="stars">${stars(r.stars)}</span>
      <span class="review-who">${esc(r.author)}</span>
      ${r.verified_purchase ? `<span class="badge">verified buyer</span>`
                            : `<span class="badge grey">counts once</span>`}
      <span style="margin-left:auto;color:var(--graphite-2);font-size:.8rem">${esc(r.created_at.slice(0, 10))}</span>
    </div>
    ${r.comment ? `<p class="review-body">${esc(r.comment)}</p>` : ""}
    ${r.taboo_hits ? `<p class="review-flag">${r.taboo_hits} taboo word${r.taboo_hits === 1 ? "" : "s"} masked by the filter.</p>` : ""}
  </div>`;
}

function paintBuyArea(book) {
  const area = $("#buy-area");
  const s = state.session || {};

  if (!s.signed_in) {
    area.innerHTML = `<p style="color:var(--graphite-2);font-size:.9rem">
      Sign in to buy. You can still rate and comment as a visitor.</p>`;
    return;
  }
  if (s.role === "SU") {
    area.innerHTML = `<p style="color:var(--graphite-2);font-size:.9rem">
      Super-user accounts manage the shop rather than buying from it.</p>`;
    return;
  }

  const short = s.balance < book.your_price;
  area.innerHTML = `
    <p style="font-size:.9rem;color:${short ? "var(--warn)" : "var(--graphite-2)"}">
      Your balance: ${money(s.balance)}</p>
    ${short ? `<div class="note">This costs more than your balance. Buying anyway is
       refused and charges a $1.00 fine.</div>` : ""}
    <button class="btn btn-paper" id="buy" ${book.stock ? "" : "disabled"}>
      ${book.stock ? "Buy this book" : "Out of stock"}</button>`;

  $("#buy")?.addEventListener("click", async () => {
    $("#buy").disabled = true;
    try {
      const r = await api(`/books/${book.book_id}/purchase`, { method: "POST" });
      toast(r.message, r.ok ? "good" : (r.suspended ? "bad" : "warn"));
      await refreshSession();
      if (r.suspended) { await signOut(); $("#sheet").hidden = true; return; }
      await Promise.all([loadPicks(), loadShelf()]);
      openBook(book.book_id);
    } catch (e) {
      toast(e.message, "bad");
      $("#buy").disabled = false;
    }
  });
}

function paintReviewForm(book) {
  let picked = 0;
  const s = state.session || {};
  $("#review-form").innerHTML = `
    <div class="stack" style="max-width:560px;margin-bottom:8px">
      <div>
        <div style="font-weight:600;font-size:.9rem">Leave a rating</div>
        ${!s.signed_in ? `<div style="font-size:.84rem;color:var(--graphite-2)">
          Posting as a visitor. Sign in and your rating is kept with your account,
          and counts double on books you bought.</div>` : ""}
        <div class="starpick" id="starpick">${
          [1, 2, 3, 4, 5].map((n) => `<button type="button" data-n="${n}" aria-label="${n} stars">☆</button>`).join("")
        }</div>
      </div>
      <textarea id="review-text" placeholder="What did you think?"></textarea>
      <div><button class="btn btn-paper" id="post-review">Post rating</button></div>
      <p class="msg" id="review-msg"></p>
    </div>`;

  $$("#starpick button").forEach((b) => {
    b.addEventListener("click", () => {
      picked = Number(b.dataset.n);
      $$("#starpick button").forEach((x, i) => {
        const on = i < picked;
        x.textContent = on ? "★" : "☆";
        x.classList.toggle("on", on);
      });
    });
  });

  $("#post-review").addEventListener("click", async () => {
    if (!picked) {
      $("#review-msg").textContent = "Choose a star rating first.";
      $("#review-msg").className = "msg bad";
      return;
    }
    try {
      const r = await api(`/books/${book.book_id}/reviews`, {
        method: "POST",
        body: { stars: picked, comment: $("#review-text").value.trim() },
      });
      toast(r.message, r.ok ? "good" : "warn");
      await refreshSession();
      if (r.suspended) { await signOut(); $("#sheet").hidden = true; return; }
      await loadShelf();
      openBook(book.book_id);
    } catch (e) {
      $("#review-msg").textContent = e.message;
      $("#review-msg").className = "msg bad";
    }
  });
}

/* ---------------------------------------------------------------- search */

async function runSearch(q) {
  const hits = await api("/search?q=" + encodeURIComponent(q));
  $("#search-count").hidden = false;
  $("#search-count").textContent =
    `${hits.length} result${hits.length === 1 ? "" : "s"} for “${q}”`;
  const grid = $("#search-results");
  grid.replaceChildren();
  hits.forEach((h) => grid.appendChild(
    card(h.book, `matched ${h.matched.slice(0, 4).join(", ")} in ${h.where}`)));
}

async function runImageSearch(file) {
  $("#drop-note").textContent = "Matching " + file.name + "…";
  const form = new FormData();
  form.append("file", file);
  try {
    const hits = await api("/search/image", { method: "POST", body: form, raw: true });
    $("#drop-note").textContent = `Matched against our cover index using ${file.name}`;
    $("#search-count").hidden = false;
    $("#search-count").textContent =
      `${hits.length} visual match${hits.length === 1 ? "" : "es"}`;
    const grid = $("#search-results");
    grid.replaceChildren();
    hits.forEach((h) => grid.appendChild(
      card(h.book, `${Math.round(h.similarity * 100)}% match on the ${h.kind}`)));
  } catch (e) {
    $("#drop-note").textContent = e.message;
  }
}

/* ------------------------------------------------------------------ chat */

const TAG = {
  "store data": "tag-store", "via RAG": "tag-rag", "via LLM": "tag-llm",
  "access denied": "tag-deny", "unavailable": "tag-err",
};

function paintSuggestions() {
  const role = (state.session || {}).role || "VS";
  const base = ["What are your opening hours?", "Do you ship internationally?",
                "What categories do you carry?"];
  const extra = {
    VS: ["What is the finance report?"],
    RC: ["What is my balance?", "What should I read next?"],
    VC: ["What is on the to-order list?", "What is my discount?"],
    SU: ["Show the finance report", "How many users are there?", "What is low on stock?"],
  }[role] || [];

  const box = $("#suggests");
  box.replaceChildren();
  [...base, ...extra].slice(0, 5).forEach((text) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.addEventListener("click", () => { $("#question").value = text; ask(); });
    box.appendChild(b);
  });
}

function looksTabular(text) {
  const lines = text.split("\n");
  if (lines.length < 3) return false;
  return lines.filter((l) => l.trim().includes("  ")).length >= lines.length / 2;
}

async function ask() {
  const q = $("#question").value.trim();
  if (!q) return;
  $("#question").value = "";
  $("#ask-btn").disabled = true;

  const thread = $("#thread");
  const turn = document.createElement("div");
  turn.className = "turn";
  turn.innerHTML = `<div class="turn-q">${esc(q)}</div><div class="turn-a">…</div>`;
  thread.appendChild(turn);
  thread.scrollTop = thread.scrollHeight;

  try {
    const a = await api("/chat", { method: "POST", body: { question: q } });
    turn.innerHTML =
      `<div class="turn-q">${esc(q)}</div>` +
      `<div><span class="tag ${TAG[a.source] || "tag-store"}">${esc(a.source)}</span></div>` +
      `<div class="turn-a${looksTabular(a.text) ? " mono" : ""}">${esc(a.text)}</div>` +
      (a.citations.length
        ? `<div class="turn-cite">Sources: ${esc(a.citations.join(", "))}</div>` : "");
  } catch (e) {
    turn.innerHTML = `<div class="turn-q">${esc(q)}</div>
      <div class="turn-a">${esc(e.message)}</div>`;
  }
  $("#ask-btn").disabled = false;
  thread.scrollTop = thread.scrollHeight;
}

/* --------------------------------------------------------------- account */

async function paintPurchases() {
  const box = $("#purchases");
  const rows = await api("/account/purchases");
  if (!rows.length) {
    box.innerHTML = `<p class="empty">Nothing bought yet. The shelf is that way.</p>`;
    return;
  }
  box.innerHTML = rows.map((r) => `
    <button class="bought" data-book="${esc(r.book_id)}">
      ${r.cover_url
        ? `<img src="${esc(r.cover_url)}" alt="">`
        : `<span class="bought-blank" aria-hidden="true"></span>`}
      <span class="bought-t">
        <b>${esc(r.title)}</b>
        <i>${esc(r.author)}</i>
      </span>
      <span class="bought-p">${money(r.price_paid)}</span>
      <span class="bought-d">${esc((r.at || "").slice(0, 10))}</span>
    </button>`).join("");
  $$("#purchases .bought").forEach((b) =>
    b.addEventListener("click", () => openBook(b.dataset.book)));
}

function paintAccount() {
  const s = state.session || {};
  if (!s.signed_in) return;
  $("#account-name").textContent = s.display_name || s.username;
  paintPurchases().catch((e) => {
    $("#purchases").innerHTML =
      `<p class="empty">${esc("Could not load your purchases: " + e.message)}</p>`;
  });

  const tiles = [
    ["Balance", money(s.balance), s.balance < 0 ? "bad" : (s.balance < 1 ? "warn" : "good")],
    ["Lifetime spend", money(s.total_spent), ""],
    ["Purchases", s.purchase_count, ""],
    ["Warnings", `${s.warnings} of 2`, s.warnings ? "warn" : ""],
    ["Tier", s.role_name, ""],
  ];
  $("#account-stats").innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="stat"><div class="stat-k">${k}</div>
     <div class="stat-v ${cls}">${esc(v)}</div></div>`).join("");

  if (s.role === "VC") {
    $("#vip-note").textContent =
      "You are a VIP: 10% off every purchase. More than two moderation warnings would return you to a registered customer.";
    $("#vip-meters").innerHTML = "";
    return;
  }
  $("#vip-note").textContent =
    "More than 3 purchases or more than $200.00 spent, whichever comes first.";
  const rows = [
    ["Purchases", s.purchase_count, 4],
    ["Spent", s.total_spent, 200.01],
  ];
  $("#vip-meters").innerHTML = rows.map(([k, v, target]) => `
    <div>
      <div class="meter-k"><span>${k}</span>
        <span>${k === "Spent" ? money(v) + " of " + money(target) : v + " of " + target}</span></div>
      <div class="meter-t"><div class="meter-f" style="width:${Math.min(100, (v / target) * 100)}%"></div></div>
    </div>`).join("");
}

/* ----------------------------------------------------------------- admin */

/* Renders whatever columns a report happens to have, so adding a query in
   analytics.py needs no change here. The column *name* is the only hint
   available about what a number means, so formatting keys off that. */
/* `average(?!$)` so average_order is money but plain_average and
   weighted_average stay star ratings. */
const MONEY_COLS  = /revenue|sales|fines|refunds|spent|spend|price|balance|value|total|average(?!$)/i;
const PCT_COLS    = /^pct_|_pct$|percent/i;
const COUNT_WORDS = { vip: "VIP", sql: "SQL", pct: "%", id: "ID" };

function sqlHeading(column) {
  return column.split("_")
    .map((w) => COUNT_WORDS[w.toLowerCase()] || w)
    .join(" ")
    .replace(/^./, (m) => m.toUpperCase());
}

function sqlCell(column, value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return esc(value);
  if (PCT_COLS.test(column)) return value.toFixed(1) + "%";
  if (MONEY_COLS.test(column)) return money(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function sqlTable(rows, empty) {
  if (!rows || !rows.length) return `<p class="empty">${esc(empty)}</p>`;
  const cols = Object.keys(rows[0]);
  const numeric = (c) => typeof rows[0][c] === "number";
  /* showAdmin wraps every table in a sideways scroller after injection, so
     nothing here needs to think about narrow screens. */
  return `<table class="table"><thead><tr>${
    cols.map((c) => `<th class="${numeric(c) ? "num" : ""}">${esc(sqlHeading(c))}</th>`).join("")
  }</tr></thead><tbody>${
    rows.map((r) => `<tr>${cols.map((c) =>
      `<td class="${numeric(c) ? "num" : ""}">${sqlCell(c, r[c])}</td>`).join("")}</tr>`).join("")
  }</tbody></table>`;
}

const adminPanels = {
  async reports() {
    const r = await api("/admin/analytics");
    const sections = [
      ["Revenue by month", "revenue_by_month",
       "A window function over a monthly CTE — sales, fines and a running total."],
      ["Category performance", "category_performance",
       "A LEFT JOIN, so a category that has sold nothing still shows up."],
      ["Best sellers", "best_sellers",
       "RANK() partitioned by category, so each shelf has its own leader."],
      ["Order lists", "order_lists",
       "The weighted average from spec item 7, as a view the reports share."],
      ["Customers by lifetime spend", "customer_value",
       "How far each customer is from the next tier."],
      ["Moderation", "moderation", "Taboo-word activity per customer."],
      ["Browsed but never bought", "dead_stock", "The shelf's quiet failures."],
    ];
    return `<p class="lede">
        Computed in SQL over a throwaway SQLite projection of the text files,
        rebuilt on every request. The files in <code>data/</code> stay the
        system of record — nothing here is ever written back.
      </p>` +
      sections.map(([title, key, note]) => `
        <h2>${esc(title)}</h2>
        <p class="query-note">${esc(note)}</p>
        ${sqlTable(r[key], "Nothing to report yet.")}`).join("");
  },

  async finance() {
    const f = await api("/admin/finance");
    const tiles = [
      ["Total revenue", money(f.total_revenue), "good"],
      ["Sales", money(f.revenue_sales), ""],
      ["Fines", money(f.revenue_fines), f.revenue_fines ? "warn" : ""],
      ["Orders", f.purchase_count, ""],
      ["Average order", money(f.average_order), ""],
      ["Customer balances", money(f.customer_liability), ""],
    ];
    return `<div class="stats">${tiles.map(([k, v, c]) =>
      `<div class="stat"><div class="stat-k">${k}</div><div class="stat-v ${c}">${esc(v)}</div></div>`).join("")}</div>
      <h2>Best sellers</h2>
      <table class="table"><thead><tr><th>Title</th><th class="num">Units</th><th class="num">Revenue</th></tr></thead>
      <tbody>${f.top_books.map((b) =>
        `<tr><td>${esc(b.title)}</td><td class="num">${b.units}</td><td class="num">${money(b.revenue)}</td></tr>`).join("")
        || `<tr><td colspan="3">Nothing sold yet.</td></tr>`}</tbody></table>
      <h2>Revenue by category</h2>
      <table class="table"><thead><tr><th>Category</th><th class="num">Revenue</th></tr></thead>
      <tbody>${f.revenue_by_category.map((c) =>
        `<tr><td>${esc(c.category)}</td><td class="num">${money(c.revenue)}</td></tr>`).join("")
        || `<tr><td colspan="2">Nothing sold yet.</td></tr>`}</tbody></table>`;
  },

  async orders() {
    const l = await api("/admin/order-lists");
    const section = (title, rows, blurb) => `
      <h2>${title} <span style="font-family:var(--sans);font-size:.85rem;color:var(--on-ink-dim)">${rows.length}</span></h2>
      <p class="lede">${blurb}</p>
      ${rows.map((r) => `<div class="listrow" style="--spine:${colour(r.book.category)}">
         <b>${esc(r.book.title)}</b>
         <span style="color:var(--on-ink-dim)">${esc(r.book.author)}</span>
         <span class="num">${r.average.toFixed(2)} weighted · ${r.plain_average.toFixed(2)} plain
           · ${r.count} ratings · stock ${r.book.stock}</span>
       </div>`).join("") || `<p class="empty">Nothing on this list.</p>`}`;
    return section("To order", l.to_order,
             "Keep these in stock — customers rate them highly.") +
           section("No order", l.no_order,
             "Stop reordering these. They are hidden from personalised recommendations automatically.");
  },

  async accounts() {
    const rows = await api("/admin/accounts");
    return `<table class="table">
      <thead><tr><th>ID</th><th>Username</th><th>Role</th>
      <th class="num">Balance</th><th class="num">Spent</th><th class="num">Warnings</th>
      <th>State</th><th></th></tr></thead>
      <tbody>${rows.map((u) => `<tr>
        <td>${esc(u.user_id)}</td><td>${esc(u.username)}</td><td>${esc(u.role_name)}</td>
        <td class="num">${money(u.balance)}</td>
        <td class="num">${money(u.total_spent)}</td>
        <td class="num">${u.warnings}</td>
        <td>${u.suspended ? `<span style="color:#ff9d94">suspended: ${esc(u.suspend_reason)}</span>` : "active"}</td>
        <td><button class="btn btn-ghost" data-deposit="${esc(u.user_id)}">
          ${u.suspended ? "Reopen" : "Deposit"}</button></td>
      </tr>`).join("")}</tbody></table>`;
  },

  async taboo() {
    const t = await api("/admin/taboo");
    return `<p class="lede">Any listed word is masked wherever it appears.
      A comment with more than three is blocked outright and its author warned.</p>
      <form id="taboo-form" style="display:flex;gap:8px;max-width:420px;margin-bottom:16px">
        <input id="taboo-word" placeholder="Add a word"
               style="flex:1;font:inherit;padding:9px 12px;background:var(--ink-deep);
                      color:var(--on-ink);border:1px solid var(--rule);border-radius:3px">
        <button class="btn btn-solid">Add</button>
      </form>
      <div class="chips">${t.words.map((w) =>
        `<button class="chip" data-taboo="${esc(w)}">${esc(w)} ✕</button>`).join("")
        || `<span class="empty">The list is empty.</span>`}</div>`;
  },

  async newbook() {
    return `<form id="book-form" class="stack" style="max-width:520px">
      <label>Title <input id="nb-title" required></label>
      <label>Author <input id="nb-author" required></label>
      <label>Category
        <select id="nb-cat">${Object.keys(CATEGORY_COLOUR).map((c) =>
          `<option>${c}</option>`).join("")}</select></label>
      <label>Year <input id="nb-year" type="number" value="2000"></label>
      <label>Price <input id="nb-price" type="number" step="0.01" value="12.00" required></label>
      <label>Stock <input id="nb-stock" type="number" value="10"></label>
      <label>Description <textarea id="nb-desc"></textarea></label>
      <div><button class="btn btn-solid">Add to the catalogue</button></div>
      <p class="msg" id="nb-msg"></p>
    </form>`;
  },
};

async function showAdmin(panel) {
  const body = $("#admin-body");
  body.innerHTML = `<p class="empty">Loading…</p>`;
  try {
    body.innerHTML = await adminPanels[panel]();
  } catch (e) {
    body.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }

  /* Every panel's tables get the sideways scroll, so a panel author never has
     to remember the wrapper and no table can push the page out on a phone. */
  $$("table.table", body).forEach((table) => {
    if (table.parentElement.classList.contains("table-scroll")) return;
    const scroller = document.createElement("div");
    scroller.className = "table-scroll";
    scroller.tabIndex = 0;
    scroller.setAttribute("role", "region");
    scroller.setAttribute("aria-label", "Table, scrolls sideways");
    table.replaceWith(scroller);
    scroller.appendChild(table);
  });

  $$("[data-deposit]", body).forEach((b) => b.addEventListener("click", async () => {
    const amount = Number(prompt("Amount to credit:", "20"));
    if (!amount || amount <= 0) return;
    const id = b.dataset.deposit;
    const suspended = b.textContent.trim() === "Reopen";
    try {
      await api(`/admin/accounts/${id}/${suspended ? "reopen" : "deposit"}`,
                { method: "POST", body: { amount } });
      toast(suspended ? "Account reopened." : "Deposit credited.", "good");
      showAdmin("accounts");
    } catch (e) { toast(e.message, "bad"); }
  }));

  $("#taboo-form", body)?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const word = $("#taboo-word").value.trim();
    if (!word) return;
    await api("/admin/taboo", { method: "POST", body: { word } });
    showAdmin("taboo");
  });
  $$("[data-taboo]", body).forEach((b) => b.addEventListener("click", async () => {
    await api("/admin/taboo/" + encodeURIComponent(b.dataset.taboo), { method: "DELETE" });
    showAdmin("taboo");
  }));

  $("#book-form", body)?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      const book = await api("/admin/books", { method: "POST", body: {
        title: $("#nb-title").value, author: $("#nb-author").value,
        category: $("#nb-cat").value, year: Number($("#nb-year").value || 0),
        price: Number($("#nb-price").value), stock: Number($("#nb-stock").value || 0),
        description: $("#nb-desc").value,
      }});
      toast(`Added “${book.title}”.`, "good");
      ev.target.reset();
      await Promise.all([loadFacets(), loadShelf()]);
    } catch (e) {
      $("#nb-msg").textContent = e.message;
      $("#nb-msg").className = "msg bad";
    }
  });
}

/* ------------------------------------------------------------------ auth */

async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch {}
  state.token = null;
  localStorage.removeItem("inkwell.token");
  await refreshSession();
  await Promise.all([loadPicks(), loadShelf()]);
  show("shelf");
  toast("Signed out. You are browsing as a visitor.");
}

async function signIn(username, password) {
  const r = await api("/auth/login", { method: "POST", body: { username, password } });
  state.token = r.token;
  localStorage.setItem("inkwell.token", r.token);
  await refreshSession();
  await Promise.all([loadPicks(), loadShelf()]);
  $("#auth").hidden = true;
  toast(`Signed in as ${r.session.display_name || r.session.username}.`, "good");
}

/* ----------------------------------------------------------------- views */

function show(view) {
  $$(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === view));
  $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  if (view === "account") paintAccount();
  if (view === "admin") showAdmin("finance");
  window.scrollTo(0, 0);
}

/* ------------------------------------------------------------------ wire */

function wire() {
  $$(".nav button").forEach((b) =>
    b.addEventListener("click", () => show(b.dataset.view)));
  $("#home-btn").addEventListener("click", () => show("shelf"));

  $$(".segmented [data-axis]").forEach((b) => b.addEventListener("click", () => {
    $$(".segmented [data-axis]").forEach((x) =>
      x.setAttribute("aria-selected", String(x === b)));
    state.axis = b.dataset.axis;
    state.facet = null;
    paintFacets();
    loadShelf();
  }));

  $$("[data-panel]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-panel]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    showAdmin(b.dataset.panel);
  }));

  $$("[data-close]").forEach((b) =>
    b.addEventListener("click", () => { $("#sheet").hidden = true; }));
  $$("[data-close-auth]").forEach((b) =>
    b.addEventListener("click", () => { $("#auth").hidden = true; }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("#sheet").hidden = true; $("#auth").hidden = true; }
  });

  $("#auth-btn").addEventListener("click", () => {
    if (state.session && state.session.signed_in) signOut();
    else { $("#auth").hidden = false; $("#li-user").focus(); }
  });

  async function attemptSignIn(username, password) {
    try {
      await signIn(username, password);
      $("#login-msg").textContent = "";
      $("#login-msg").className = "msg";
    } catch (err) {
      $("#login-msg").textContent = err.message;
      $("#login-msg").className = "msg bad";
    }
  }

  $("#login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    attemptSignIn($("#li-user").value.trim(), $("#li-pass").value);
  });

  /* A demo row fills the form *and* goes, so someone trying the shop out can
     switch tiers in one click instead of two. */
  $$("#demo-list button").forEach((b) => b.addEventListener("click", () => {
    $("#li-user").value = b.dataset.u;
    $("#li-pass").value = b.dataset.p;
    attemptSignIn(b.dataset.u, b.dataset.p);
  }));

  $("#reg-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/auth/register", { method: "POST", body: {
        username: $("#rg-user").value.trim(),
        password: $("#rg-pass").value,
        display_name: $("#rg-name").value.trim(),
        opening_deposit: Number($("#rg-dep").value || 0),
      }});
      state.token = r.token;
      localStorage.setItem("inkwell.token", r.token);
      await refreshSession();
      await Promise.all([loadPicks(), loadShelf()]);
      $("#auth").hidden = true;
      toast("Account created. Welcome.", "good");
    } catch (err) {
      $("#reg-msg").textContent = err.message;
      $("#reg-msg").className = "msg bad";
    }
  });

  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (q) runSearch(q);
  });

  const drop = $("#drop");
  $("#image-input").addEventListener("change", (e) => {
    if (e.target.files[0]) runImageSearch(e.target.files[0]);
  });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("over");
  }));
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) runImageSearch(file);
  });

  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
}

/* ------------------------------------------------------------------ boot */

(async function start() {
  /* Anything that slips past a local catch still reaches the person, rather
     than failing silently in the console. */
  window.addEventListener("unhandledrejection", (e) => {
    toast((e.reason && e.reason.message) || "Something went wrong.", "bad");
  });

  wire();
  await refreshSession();
  try {
    await Promise.all([loadFacets(), loadPicks(), loadShelf()]);
  } catch (e) {
    toast("Could not reach the server: " + e.message, "bad");
  }
})();
