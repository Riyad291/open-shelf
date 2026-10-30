/* Open Shelf — browser client.
 *
 * No framework and no build step: this is the whole front end. It talks to the
 * FastAPI service with fetch(), holds a JWT access token in memory and a
 * refresh token in localStorage, and renders the catalogue as book spines on a
 * shelf because the signature feature here recognises books from a shelf
 * photograph.
 */

"use strict";

const API = "/api";

// These mirror config/settings.json (warning_limit, trusted_review_threshold).
// They only label the account page; the server enforces the real rules.
const WARNING_LIMIT = 2;
const TRUSTED_AFTER = 3;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  access: null,
  refresh: localStorage.getItem("openshelf.refresh") || null,
  session: { signed_in: false, role: "VS", role_name: "Visitor" },
  page: { offset: 0, limit: 24, total: 0 },
  filter: { axis: "categories", value: null },
  library: { q: "", topic: null, sort: "title", offset: 0, limit: 30, total: 0 },
  camera: { stream: null, mode: null, shots: [] },
};

/* ------------------------------------------------------------------ util */

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (v) => "$" + Number(v ?? 0).toFixed(2);

const stars = (n) => n == null ? "" :
  "★".repeat(Math.round(n)) + "☆".repeat(Math.max(0, 5 - Math.round(n)));

let toastTimer = null;
function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

/* Every call goes through here, so the refresh dance lives in one place:
   a 401 on an authenticated call is retried once with a fresh access token
   before the person is told anything. */
async function api(path, { method = "GET", body, raw = false, retry = true } = {}) {
  const headers = {};
  if (!raw && body !== undefined) headers["Content-Type"] = "application/json";
  if (state.access) headers["Authorization"] = "Bearer " + state.access;

  const response = await fetch(API + path, {
    method,
    headers,
    body: raw ? body : (body !== undefined ? JSON.stringify(body) : undefined),
  });

  if (response.status === 401 && retry && state.refresh) {
    if (await refreshSession()) {
      return api(path, { method, body, raw, retry: false });
    }
  }

  if (response.status === 204) return null;

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}

/* --------------------------------------------------------------- session */

function storeTokens(data) {
  state.access = data.access_token;
  state.refresh = data.refresh_token;
  localStorage.setItem("openshelf.refresh", data.refresh_token);
  state.session = data.session;
}

function clearTokens() {
  state.access = null;
  state.refresh = null;
  localStorage.removeItem("openshelf.refresh");
  state.session = { signed_in: false, role: "VS", role_name: "Visitor" };
}

async function refreshSession() {
  if (!state.refresh) return false;
  try {
    const response = await fetch(API + "/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.refresh }),
    });
    if (!response.ok) { clearTokens(); return false; }
    storeTokens(await response.json());
    return true;
  } catch {
    clearTokens();
    return false;
  }
}

async function loadSession() {
  if (state.refresh && !state.access) await refreshSession();
  try {
    state.session = await api("/auth/session");
  } catch {
    state.session = { signed_in: false, role: "VS", role_name: "Visitor" };
  }
  paintSession();
}

function paintSession() {
  const s = state.session || {};
  const signedIn = !!s.signed_in;

  $("#who-text").textContent = signedIn
    ? `${s.display_name || s.username} · ${s.role_name}`
    : "Browsing as a visitor";

  $("#auth-btn").textContent = signedIn ? "Sign out" : "Sign in";
  $("#nav-account").hidden = !signedIn || s.role === "SU";
  $("#nav-admin").hidden = s.role !== "SU";

  const scopes = {
    VS: "You can ask about the books, the categories and how this site works. Your own account and the site's internals need an account.",
    RC: "You can also ask about your reviews, your standing and what to read next.",
    VC: "You can also ask about the recommended and not-recommended lists.",
    SU: "You can also ask for the catalogue statistics, every account and moderation.",
  };
  $("#chat-scope").textContent = scopes[s.role] || scopes.VS;
  paintSuggestions();
}

/* ----------------------------------------------------------------- shelf */

const SPINE_COLOURS = {
  literature: "#e8622c", science: "#1f7a6b", history: "#8c2f39",
  philosophy: "#4a3a82", poetry: "#b5476b", children: "#e0a32e",
  technology: "#2d6e9e", biography: "#5c6b3f",
};

function spine(book, rank) {
  const colour = SPINE_COLOURS[book.category] || "#6b7280";
  const height = 150 + ((book.title.length * 7) % 90);
  const badge = book.list_status === "no-order" ? "no-order"
    : book.list_status === "to-order" ? "to-order" : "";
  return `
    <button class="spine" role="listitem" data-book="${book.id}"
            style="--spine:${colour};--h:${height}px"
            title="${esc(book.title)} — ${esc(book.author)}">
      ${rank ? `<span class="spine-rank">${rank}</span>` : ""}
      <span class="spine-text">
        <b>${esc(book.title)}</b><i>${esc(book.author)}</i>
      </span>
      ${badge ? `<span class="spine-dot ${badge}" aria-hidden="true"></span>` : ""}
    </button>`;
}

function card(book, note) {
  return `
    <button class="card" data-book="${book.id}">
      ${book.cover_url ? `<img src="${esc(book.cover_url)}" alt="" loading="lazy">` : ""}
      <span class="card-t">
        <b>${esc(book.title)}</b>
        <i>${esc(book.author)}</i>
        ${book.rating != null ? `<span class="stars">${stars(book.rating)}</span>` : ""}
        ${note ? `<span class="card-n">${esc(note)}</span>` : ""}
      </span>
    </button>`;
}

async function loadPicks() {
  const picks = await api("/recommendations");
  $("#picks").innerHTML = picks
    .map((p, i) => spine(p.book, i + 1)).join("");
  $("#shelf-title").textContent = state.session.signed_in
    ? `Picked for ${esc(state.session.display_name || state.session.username)}`
    : "On the shelf";
  // The reason belongs to the first pick, not to the page: shown here it
  // read as if it described all three. Each pick explains itself on its card.
  $("#shelf-sub").textContent = !picks.length
    ? "Nothing to recommend yet."
    : state.session.signed_in
      ? "Chosen from what you have read and rated."
      : "Three picks from what readers here rate most.";
}

async function loadFacets() {
  const facets = await api("/books/facets");
  state.facets = facets;
  paintFacets();
}

function paintFacets() {
  const items = (state.facets || {})[state.filter.axis] || [];
  $("#facets").innerHTML = items.map((f) => `
    <button class="chip ${state.filter.value === f.value ? "on" : ""}"
            data-facet="${esc(f.value)}">
      ${esc(f.value)} <span>${f.count}</span>
    </button>`).join("");
}

async function loadShelf() {
  const params = new URLSearchParams({
    limit: state.page.limit, offset: state.page.offset,
  });
  if (state.filter.value) {
    const key = { categories: "category", authors: "author", decades: "decade" };
    params.set(key[state.filter.axis], state.filter.value);
  }

  const page = await api("/books?" + params.toString());
  state.page.total = page.total;
  $("#shelf").innerHTML = page.items.map((b) => spine(b)).join("");
  $("#shelf-empty").hidden = page.items.length > 0;

  const pages = Math.ceil(page.total / state.page.limit) || 1;
  const current = Math.floor(state.page.offset / state.page.limit) + 1;
  $("#shelf-pager").hidden = pages <= 1;
  $("#page-label").textContent = `Page ${current} of ${pages} · ${page.total} books`;
  $("#page-prev").disabled = state.page.offset === 0;
  $("#page-next").disabled = state.page.offset + state.page.limit >= page.total;
}

/* --------------------------------------------------------------- library */

/* The free technology catalogue. Each card links to /api/resources/{id}/open,
   which counts the click and redirects to wherever the book is published, so
   "Most opened" is real usage rather than a guess. */

const TOPIC_COLOURS = ["#e8622c", "#1f7a6b", "#8c2f39", "#4a3a82", "#b5476b",
                       "#e0a32e", "#2d6e9e", "#5c6b3f"];

function topicColour(name) {
  let h = 0;
  for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TOPIC_COLOURS[h % TOPIC_COLOURS.length];
}

function resourceCard(r) {
  const facts = [r.media, r.licence].filter(Boolean).map(esc).join(" · ");
  return `
    <a class="card res" href="${API}/resources/${r.id}/open" target="_blank"
       rel="noopener" style="--spine:${topicColour(r.topics[0])}">
      <span>
        <span class="card-t">${esc(r.title)}</span>
        ${r.in_progress ? `<span class="badge grey">in progress</span>` : ""}
        ${r.authors ? `<span class="card-a">${esc(r.authors)}</span>` : ""}
        ${r.description ? `<span class="card-d">${esc(r.description)}</span>` : ""}
        <span class="card-m">${facts}${facts && r.topics.length ? " · " : ""}${
          r.topics.map(esc).join(", ")}</span>
      </span>
    </a>`;
}

async function loadTopics() {
  const topics = await api("/resources/topics");
  const lib = state.library;
  $("#lib-topics").innerHTML = topics.slice(0, 28).map((t) => `
    <button class="chip ${lib.topic === t.slug ? "on" : ""}" data-topic="${esc(t.slug)}">
      ${esc(t.name)} <span>${t.count}</span>
    </button>`).join("");
}

async function loadLibrary() {
  const lib = state.library;
  const params = new URLSearchParams({
    limit: lib.limit, offset: lib.offset, sort: lib.sort,
  });
  if (lib.q) params.set("q", lib.q);
  if (lib.topic) params.set("topic", lib.topic);

  const page = await api("/resources?" + params.toString());
  lib.total = page.total;
  $("#lib-list").innerHTML = page.items.map(resourceCard).join("")
    || `<p class="empty">Nothing matches. Try a broader word, or clear the topic.</p>`;
  $("#lib-count").textContent = page.total.toLocaleString() +
    (page.total === 1 ? " resource" : " resources") +
    (lib.q ? ` matching “${lib.q}”` : "");

  const pages = Math.ceil(page.total / lib.limit) || 1;
  const current = Math.floor(lib.offset / lib.limit) + 1;
  $("#lib-pager").hidden = pages <= 1;
  $("#lib-page").textContent = `Page ${current} of ${pages}`;
  $("#lib-prev").disabled = lib.offset === 0;
  $("#lib-next").disabled = lib.offset + lib.limit >= page.total;
}

function wireLibrary() {
  const lib = state.library;
  $("#lib-form").addEventListener("submit", (e) => {
    e.preventDefault();
    lib.q = $("#lib-q").value.trim();
    lib.offset = 0;
    loadLibrary().catch((x) => toast(x.message, "bad"));
  });
  $("#lib-sort").addEventListener("change", () => {
    lib.sort = $("#lib-sort").value;
    lib.offset = 0;
    loadLibrary().catch((x) => toast(x.message, "bad"));
  });
  $("#lib-topics").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-topic]");
    if (!chip) return;
    lib.topic = lib.topic === chip.dataset.topic ? null : chip.dataset.topic;
    lib.offset = 0;
    $$("#lib-topics .chip").forEach((c) =>
      c.classList.toggle("on", c.dataset.topic === lib.topic));
    loadLibrary().catch((x) => toast(x.message, "bad"));
  });
  $("#lib-prev").addEventListener("click", () => {
    lib.offset = Math.max(0, lib.offset - lib.limit);
    loadLibrary().then(() => window.scrollTo(0, 0));
  });
  $("#lib-next").addEventListener("click", () => {
    lib.offset += lib.limit;
    loadLibrary().then(() => window.scrollTo(0, 0));
  });
}

/* ------------------------------------------------------------ book sheet */

async function openBook(id) {
  const book = await api("/books/" + id);
  $("#sheet-body").innerHTML = bookSheet(book);
  $("#sheet").hidden = false;
  wireBookSheet(book);
}

function bookSheet(book) {
  return `
    <div class="detail">
      ${book.cover_url ? `<img class="detail-cover" src="${esc(book.cover_url)}" alt="Cover of ${esc(book.title)}">` : ""}
      <div class="detail-main">
        <h2 id="sheet-title">${esc(book.title)}</h2>
        <p class="detail-author">${esc(book.author)}</p>
        <dl class="detail-facts">
          <div><dt>Category</dt><dd>${esc(book.category)}</dd></div>
          <div><dt>Published</dt><dd>${esc(book.year ?? "—")}</dd></div>
          <div><dt>Views</dt><dd>${book.browse_count}</dd></div>
        </dl>
        <p class="rating-line">${book.rating_count
          ? `<span class="stars">${stars(book.rating)}</span> ${book.rating} from ${book.rating_count} rating${book.rating_count === 1 ? "" : "s"}`
          : "No ratings yet."}</p>
        ${book.list_status !== "normal"
          ? `<p class="note ${book.list_status === "no-order" ? "bad" : "good"}">
               This title is on the <b>${book.list_status === "no-order"
                 ? "not-recommended" : "recommended"}</b> list.</p>` : ""}
        <p class="serif">${esc(book.description)}</p>

        <div id="buy-area"></div>
      </div>
    </div>

    ${book.figures.length ? `
      <h3>Figures from inside</h3>
      <div class="figures">${book.figures.map((f) =>
        `<img src="${esc(f.url)}" alt="Figure from ${esc(book.title)}" loading="lazy">`).join("")}</div>` : ""}

    <h3>Ratings and comments</h3>
    <p class="muted">Ratings from trusted contributors count double in the
      average. Taboo words are masked, and a comment with more than three is
      blocked.</p>
    <div id="review-list">${book.reviews.map(reviewHtml).join("")
      || `<p class="muted">Nobody has reviewed this yet.</p>`}</div>
    <div id="review-form"></div>`;
}

function reviewHtml(r) {
  return `
    <div class="review">
      <div class="review-head">
        <span class="stars">${stars(r.stars)}</span>
        <b>${esc(r.author)}</b>
        ${r.trusted_reviewer ? `<span class="badge">trusted contributor — counts double</span>` : ""}
      </div>
      <p>${esc(r.comment)}</p>
    </div>`;
}

function wireBookSheet(book) {
  const buy = $("#buy-area");
  if (!state.session.signed_in) {
    buy.innerHTML = `<p class="muted">You can rate and comment as a visitor.
      Sign in and your ratings are kept with your account.</p>`;
  } else if (state.session.role === "SU") {
    buy.innerHTML = `<p class="muted">You are signed in as an administrator.</p>`;
  } else {
    buy.innerHTML = `<p class="muted">Everything here is free. Rate it below
      and your reviews build your standing.</p>`;
  }

  const form = $("#review-form");
  form.innerHTML = `
    <h4>Leave a rating</h4>
    <p class="muted">${state.session.signed_in
      ? (state.session.trusted
          ? "Your rating counts double as a trusted contributor."
          : "Your rating is kept with your account. Reviews that pass moderation earn trusted-contributor status.")
      : "Posting as a visitor. Sign in and your rating is kept with your account."}</p>
    <div class="star-pick" id="star-pick">${[1, 2, 3, 4, 5].map((n) =>
      `<button data-stars="${n}" aria-label="${n} stars">☆</button>`).join("")}</div>
    <textarea id="review-text" placeholder="What did you think?"></textarea>
    <button class="btn btn-solid" id="post-review">Post rating</button>`;

  let chosen = 0;
  $$("#star-pick button").forEach((b) => b.addEventListener("click", () => {
    chosen = Number(b.dataset.stars);
    $$("#star-pick button").forEach((x, i) => {
      x.textContent = i < chosen ? "★" : "☆";
    });
  }));

  $("#post-review").addEventListener("click", async () => {
    if (!chosen) { toast("Pick a star rating first.", "warn"); return; }
    try {
      const result = await api(`/books/${book.id}/reviews`, {
        method: "POST",
        body: { stars: chosen, comment: $("#review-text").value },
      });
      toast(result.message, result.ok ? "good" : "bad");
      if (result.ok) openBook(book.id);
      loadSession();
    } catch (e) { toast(e.message, "bad"); }
  });
}

/* --------------------------------------------------------------- account */

async function paintAccount() {
  const s = state.session;
  if (!s.signed_in) return;
  $("#account-name").textContent = s.display_name || s.username;

  const tiles = [
    ["Standing", s.suspended ? "Suspended" : "Good", s.suspended ? "bad" : "good"],
    ["Warnings", `${s.warnings} of ${WARNING_LIMIT}`, s.warnings ? "warn" : ""],
    ["Tier", s.role_name, ""],
  ];
  $("#account-stats").innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="stat"><div class="stat-k">${k}</div>
     <div class="stat-v ${cls}">${esc(v)}</div></div>`).join("");

  if (s.trusted) {
    $("#vip-note").textContent =
      `You are a trusted contributor: your ratings count double. More than ${WARNING_LIMIT} moderation warnings would return you to member.`;
    $("#vip-meters").innerHTML = "";
  } else {
    $("#vip-note").textContent =
      `${TRUSTED_AFTER} reviews that pass moderation make you a trusted contributor.`;
    const v = s.review_count || 0;
    $("#vip-meters").innerHTML = `
      <div>
        <div class="meter-k"><span>Reviews accepted</span>
          <span>${v} of ${TRUSTED_AFTER}</span></div>
        <div class="meter-t"><div class="meter-f"
             style="width:${Math.min(100, (v / TRUSTED_AFTER) * 100)}%"></div></div>
      </div>`;
  }

  paintFacePanel().catch(() => {});
}

async function paintFacePanel() {
  const panel = $("#face-panel");
  const status = await api("/biometrics/status");

  if (!status.available) {
    panel.innerHTML = `<p class="note">${esc(status.message)}</p>`;
    return;
  }

  panel.innerHTML = `
    <div class="stats">
      <div class="stat"><div class="stat-k">Photographs enrolled</div>
        <div class="stat-v ${status.face_samples ? "good" : ""}">${status.face_samples}</div></div>
      <div class="stat"><div class="stat-k">Recommended</div>
        <div class="stat-v">${status.required_samples}</div></div>
      <div class="stat"><div class="stat-k">Detector</div>
        <div class="stat-v small">${esc(status.detector)}</div></div>
    </div>
    <div class="buy-row">
      <button class="btn btn-solid" id="enrol-face">
        ${status.face_samples ? "Add more photographs" : "Enrol my face"}
      </button>
      ${status.face_samples
        ? `<button class="btn btn-ghost" id="forget-face">Delete my face data</button>` : ""}
    </div>`;

  $("#enrol-face").addEventListener("click", () => openCamera("enrol"));
  const forget = $("#forget-face");
  if (forget) forget.addEventListener("click", async () => {
    if (!confirm("Delete every face photograph for this account?")) return;
    try {
      await api("/biometrics/face", { method: "DELETE" });
      toast("Face data deleted.", "good");
      paintFacePanel();
    } catch (e) { toast(e.message, "bad"); }
  });
}

/* ---------------------------------------------------------------- camera */

async function openCamera(mode) {
  state.camera.mode = mode;
  state.camera.shots = [];
  $("#camera-msg").textContent = "";
  $("#camera-title").textContent =
    mode === "enrol" ? "Enrol your face" : "Sign in with your face";
  $("#camera-hint").textContent = mode === "enrol"
    ? "Five photographs, with small changes of angle between them. Face the camera straight on, light in front of you."
    : "Look at the camera and press Capture.";
  $("#cam-go").textContent = mode === "enrol" ? "Capture (0 of 5)" : "Capture";
  $("#camera").hidden = false;

  try {
    state.camera.stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" },
      audio: false,
    });
  } catch (e) {
    $("#camera-msg").textContent =
      "The browser would not give us the camera. Allow camera access for this " +
      "site, make sure no other app is using it, and try again. (" + e.name + ")";
    $("#camera-msg").className = "msg bad";
    return;
  }

  const video = $("#cam-video");
  video.srcObject = state.camera.stream;
  await video.play().catch(() => {});
}

function closeCamera() {
  if (state.camera.stream) {
    // Releasing every track is what turns the camera light off. Leaving it on
    // after the dialog closes is alarming, and on some machines it blocks any
    // other app from opening the camera at all.
    state.camera.stream.getTracks().forEach((t) => t.stop());
    state.camera.stream = null;
  }
  $("#cam-video").srcObject = null;
  $("#camera").hidden = true;
}

function grabFrame() {
  const video = $("#cam-video");
  const canvas = $("#cam-canvas");
  canvas.width = video.videoWidth || 640;
  canvas.height = video.videoHeight || 480;
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

async function captureShot() {
  const button = $("#cam-go");
  const message = $("#camera-msg");

  if (!state.camera.stream) { message.textContent = "No camera."; return; }
  button.disabled = true;

  try {
    const blob = await grabFrame();

    if (state.camera.mode === "identify") {
      /* A burst, not a snapshot. A single frame makes signing in a coin
         toss: a blink, a slight turn or a moment of motion blur is enough
         for one frame to miss while the next one matches comfortably. The
         server scores all of them and keeps the best. */
      const message = $("#camera-msg");
      message.className = "msg";
      const burst = [blob];
      for (let i = 1; i < 5; i++) {
        message.textContent = `Hold still… ${i + 1} of 5`;
        await new Promise((r) => setTimeout(r, 260));
        burst.push(await grabFrame());
      }
      message.textContent = "Checking…";

      const form = new FormData();
      burst.forEach((b, i) => form.append("frames", b, `f${i}.png`));
      const data = await api("/biometrics/face/login", {
        method: "POST", body: form, raw: true, retry: false,
      });
      storeTokens(data);
      closeCamera();
      $("#auth").hidden = true;
      paintSession();
      await Promise.all([loadPicks(), loadShelf()]);
      toast(`Signed in as ${data.session.display_name || data.session.username}.`, "good");
      return;
    }

    state.camera.shots.push(blob);
    button.textContent = `Capture (${state.camera.shots.length} of 5)`;
    message.className = "msg";
    message.textContent = `Captured ${state.camera.shots.length}. ` +
      (state.camera.shots.length < 5 ? "Turn your head slightly and capture again."
                                     : "Sending…");

    if (state.camera.shots.length >= 5) {
      const form = new FormData();
      state.camera.shots.forEach((b, i) => form.append("frames", b, `f${i}.png`));
      const result = await api("/biometrics/face/enroll", {
        method: "POST", body: form, raw: true,
      });
      message.className = "msg " + (result.ok ? "good" : "bad");
      message.textContent = result.message;
      state.camera.shots = [];
      button.textContent = "Capture (0 of 5)";
      if (result.ok) { closeCamera(); paintFacePanel(); loadSession(); }
    }
  } catch (e) {
    message.className = "msg bad";
    message.textContent = e.message;
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------ shelf scan */

async function runShelfScan(file) {
  const note = $("#scan-note");
  const out = $("#scan-result");
  note.textContent = "Scanning " + file.name + "…";
  out.innerHTML = `<p class="empty">Segmenting the photo and matching covers…</p>`;

  const form = new FormData();
  form.append("photo", file, file.name);

  try {
    const result = await api("/shelf-scan", { method: "POST", body: form, raw: true });
    note.textContent = "";

    if (!result.ok) {
      out.innerHTML = `<p class="note bad">${esc(result.message)}</p>`;
      return;
    }

    out.innerHTML = `
      <h2>What the scanner saw</h2>
      <p class="muted">${esc(result.message)}
        Segmentation: ${esc(result.segmentation || "built-in two-stage")}.</p>
      ${result.annotated_url
        ? `<img class="scan-shot" src="${esc(result.annotated_url)}"
                alt="The shelf photo with detected books boxed">` : ""}

      <h3>Recognised</h3>
      <div class="grid">${result.matches.filter((m) => m.recognised).map((m) =>
        card(m.book, `${Math.round(m.confidence * 100)}% confident`)).join("")
        || `<p class="muted">Nothing matched our catalogue.</p>`}</div>

      ${result.taste_profile.length ? `
        <h3>Your taste, from this shelf</h3>
        <p class="lede">${result.taste_profile.map(esc).join(" · ")}</p>` : ""}

      ${result.recommendations.length ? `
        <h3>You might like</h3>
        <div class="grid">${result.recommendations.map((b) => card(b)).join("")}</div>` : ""}`;
  } catch (e) {
    note.textContent = "";
    out.innerHTML = `<p class="note bad">${esc(e.message)}</p>`;
  }
}

/* ------------------------------------------------------------------ chat */

const TAG = {
  "store data": "tag-store", "via RAG": "tag-rag", "via LLM": "tag-llm",
  "access denied": "tag-deny", "unavailable": "tag-deny",
};

function paintSuggestions() {
  const common = ["What are your opening hours?", "Do you ship internationally?",
                  "What categories do you carry?"];
  const extra = {
    RC: ["What is my status?", "What should I read next?"],
    VC: ["What do trusted contributors get?", "What is on the recommended list?"],
    SU: ["Show me the catalogue stats.", "Which accounts are suspended?"],
  }[state.session.role] || [];

  $("#suggests").innerHTML = common.concat(extra)
    .map((q) => `<button class="chip" data-ask="${esc(q)}">${esc(q)}</button>`).join("");
  $$("#suggests [data-ask]").forEach((b) => b.addEventListener("click", () => {
    $("#question").value = b.dataset.ask;
    ask();
  }));
}

async function ask() {
  const input = $("#question");
  const question = input.value.trim();
  if (!question) return;

  const thread = $("#thread");
  const block = document.createElement("div");
  block.className = "turn";
  block.innerHTML = `<p class="you"><span>You</span> <b>${esc(question)}</b></p>
                     <p class="thinking">Thinking…</p>`;
  thread.appendChild(block);
  input.value = "";
  thread.scrollTop = thread.scrollHeight;

  try {
    const answer = await api("/chat", { method: "POST", body: { question } });
    block.innerHTML = `
      <p class="you"><span>You</span> <b>${esc(question)}</b></p>
      <p><span class="tag ${TAG[answer.source] || "tag-store"}">${esc(answer.source)}</span></p>
      <div class="answer">${esc(answer.text).replace(/\n/g, "<br>")}</div>
      ${answer.citations.length
        ? `<p class="cites">Sources: ${answer.citations.map(esc).join(", ")}</p>` : ""}`;
  } catch (e) {
    block.innerHTML = `<p class="you"><span>You</span> <b>${esc(question)}</b></p>
                       <p class="note bad">${esc(e.message)}</p>`;
  }
  thread.scrollTop = thread.scrollHeight;
}

/* ----------------------------------------------------------------- admin */

function sqlHeading(column) {
  const words = { vip: "VIP", sql: "SQL", pct: "%", id: "ID" };
  return column.split("_").map((w) => words[w.toLowerCase()] || w).join(" ")
    .replace(/^./, (m) => m.toUpperCase());
}

// Nothing here is money any more. Kept as a pattern that matches nothing so
// that sqlCell's formatting rules stay in one place if a column ever is.
const MONEY_COLS = /^$/;
const PCT_COLS = /^pct_|_pct$|percent/i;

function sqlCell(column, value) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") {
    const asNumber = Number(value);
    if (value !== "" && !Number.isNaN(asNumber) && MONEY_COLS.test(column)) {
      return money(asNumber);
    }
    return esc(value);
  }
  if (PCT_COLS.test(column)) return value.toFixed(1) + "%";
  if (MONEY_COLS.test(column)) return money(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function sqlTable(rows, empty) {
  if (!rows || !rows.length) return `<p class="empty">${esc(empty)}</p>`;
  const cols = Object.keys(rows[0]);
  const numeric = (c) => MONEY_COLS.test(c) || PCT_COLS.test(c)
    || typeof rows[0][c] === "number";
  return `<table class="table"><thead><tr>${
    cols.map((c) => `<th class="${numeric(c) ? "num" : ""}">${esc(sqlHeading(c))}</th>`).join("")
  }</tr></thead><tbody>${
    rows.map((r) => `<tr>${cols.map((c) =>
      `<td class="${numeric(c) ? "num" : ""}">${sqlCell(c, r[c])}</td>`).join("")}</tr>`).join("")
  }</tbody></table>`;
}

const adminPanels = {
  async summary() {
    const s = await api("/admin/summary");
    const tiles = [
      ["Live resources", s.resources, "good"],
      ["Dead links", s.dead_links, s.dead_links ? "warn" : ""],
      ["Times opened", s.total_opens, ""],
      ["Topics", s.topics, ""],
      ["Accounts", s.accounts, ""],
      ["Trusted contributors", s.trusted_contributors, ""],
      ["Reviews", s.reviews, ""],
      ["Suspended", s.suspended_accounts, s.suspended_accounts ? "warn" : ""],
    ];
    const last = s.last_import;
    return `<div class="stats">${tiles.map(([k, v, c]) =>
      `<div class="stat"><div class="stat-k">${k}</div><div class="stat-v ${c}">${esc(v)}</div></div>`).join("")}</div>
      <h2>Last import</h2>
      ${last ? sqlTable([last], "") : `<p class="empty">No import has run yet.</p>`}`;
  },

  async reports() {
    const r = await api("/admin/analytics");
    const sections = [
      ["Catalogue growth", "catalogue_growth",
       "A window function over a monthly CTE: resources added, with a running total."],
      ["Topic coverage", "topic_coverage",
       "A LEFT JOIN, so a topic with nothing in it still shows up, with a zero."],
      ["Most opened in each topic", "most_opened",
       "RANK() partitioned by topic, so each topic has its own leader."],
      ["Not opened yet", "undiscovered", "Live resources nobody has clicked: candidates to feature."],
      ["Contributors", "contributors", "Who writes the reviews, and how many were blocked."],
      ["Moderation", "moderation", "Taboo-word activity per account."],
      ["Import history", "import_history", "One row per run, including failures."],
    ];
    return `<p class="lede">Computed in SQL against the live schema — CTEs,
      window functions and deliberate outer joins.</p>` +
      sections.map(([title, key, note]) => `
        <h2>${esc(title)}</h2>
        <p class="query-note">${esc(note)}</p>
        ${sqlTable(r[key], "Nothing to report yet.")}`).join("");
  },

  async orders() {
    const lists = await api("/admin/order-lists");
    return `
      <h2>Recommended</h2>
      <p class="query-note">More than 3 ratings, weighted average above 4.</p>
      ${sqlTable(lists.to_order, "Nothing qualifies yet.")}
      <h2>Not recommended</h2>
      <p class="query-note">More than 3 ratings, weighted average below 2.</p>
      ${sqlTable(lists.no_order, "Nothing qualifies yet.")}`;
  },

  async accounts() {
    const rows = await api("/admin/accounts");
    return `<table class="table"><thead><tr>
        <th>Username</th><th>Name</th><th>Role</th>
        <th class="num">Warnings</th><th>State</th><th></th>
      </tr></thead><tbody>${rows.map((a) => `
        <tr>
          <td>${esc(a.username)}</td><td>${esc(a.display_name)}</td>
          <td>${esc(a.role_name)}</td>
          <td class="num">${a.warnings}</td>
          <td>${a.suspended ? `<span class="badge bad">suspended</span>` : "active"}</td>
          <td class="num">
            ${a.suspended ? `<button class="btn btn-ghost btn-small" data-reopen="${a.id}">Reopen</button>` : ""}
          </td>
        </tr>`).join("")}</tbody></table>`;
  },

  async taboo() {
    const words = await api("/admin/taboo");
    return `
      <p class="lede">A comment containing more than three of these is blocked
        and earns its author a warning.</p>
      <div class="chips">${words.map((w) =>
        `<button class="chip" data-untaboo="${esc(w)}">${esc(w)} ×</button>`).join("")}</div>
      <form class="search" id="taboo-form">
        <input id="taboo-word" placeholder="Add a word" aria-label="New taboo word">
        <button class="btn btn-solid">Add</button>
      </form>`;
  },

  async newbook() {
    return `
      <form class="stack narrow" id="newbook-form">
        <label>Title <input id="nb-title" required></label>
        <label>Author <input id="nb-author" required></label>
        <label>Category <input id="nb-category" required placeholder="literature"></label>
        <label>Year <input id="nb-year" type="number" min="0" max="2200"></label>
        <label>Description <textarea id="nb-desc"></textarea></label>
        <button class="btn btn-solid">Add to the catalogue</button>
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

  $$("[data-reopen]", body).forEach((b) => b.addEventListener("click", async () => {
    // OK clears the warnings too; Cancel reopens and keeps them, so one more
    // blocked comment suspends the account again.
    const clear = confirm("Reopen the account. Also clear its warnings?");
    try {
      await api(`/admin/accounts/${b.dataset.reopen}/reopen`,
                { method: "POST", body: { clear_warnings: clear } });
      toast("Account reopened.", "good");
      showAdmin("accounts");
    } catch (e) { toast(e.message, "bad"); }
  }));

  $$("[data-untaboo]", body).forEach((b) => b.addEventListener("click", async () => {
    try {
      await api("/admin/taboo/" + encodeURIComponent(b.dataset.untaboo),
                { method: "DELETE" });
      showAdmin("taboo");
    } catch (e) { toast(e.message, "bad"); }
  }));

  const tabooForm = $("#taboo-form", body);
  if (tabooForm) tabooForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const word = $("#taboo-word").value.trim();
    if (!word) return;
    try {
      await api("/admin/taboo", { method: "POST", body: { word } });
      showAdmin("taboo");
    } catch (err) { toast(err.message, "bad"); }
  });

  const bookForm = $("#newbook-form", body);
  if (bookForm) bookForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/admin/books", {
        method: "POST",
        body: {
          title: $("#nb-title").value, author: $("#nb-author").value,
          category: $("#nb-category").value.toLowerCase(),
          year: Number($("#nb-year").value) || null,
          description: $("#nb-desc").value,
        },
      });
      toast("Added to the catalogue.", "good");
      bookForm.reset();
      loadShelf(); loadFacets();
    } catch (err) { toast(err.message, "bad"); }
  });
}

/* ----------------------------------------------------------------- views */

function show(view) {
  $$(".view").forEach((v) => v.classList.toggle("on", v.dataset.view === view));
  $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  if (view === "account") paintAccount();
  if (view === "admin") showAdmin("summary");
  window.scrollTo(0, 0);
}

/* ------------------------------------------------------------------ auth */

async function signIn(username, password) {
  const data = await api("/auth/login", {
    method: "POST", body: { username, password }, retry: false,
  });
  storeTokens(data);
  paintSession();
  await Promise.all([loadPicks(), loadShelf()]);
  $("#auth").hidden = true;
  toast(`Signed in as ${data.session.display_name || data.session.username}.`, "good");
}

async function signOut() {
  try {
    if (state.refresh) {
      await api("/auth/logout", { method: "POST",
                                  body: { refresh_token: state.refresh } });
    }
  } catch { /* signing out must never fail visibly */ }
  clearTokens();
  paintSession();
  await Promise.all([loadPicks(), loadShelf()]);
  show("library");
  toast("Signed out. You are browsing as a visitor.");
}

/* ------------------------------------------------------------------ wire */

function wire() {
  $$(".nav button").forEach((b) =>
    b.addEventListener("click", () => show(b.dataset.view)));
  $("#home-btn").addEventListener("click", () => show("library"));

  document.addEventListener("click", (e) => {
    const target = e.target.closest("[data-book]");
    if (target) openBook(target.dataset.book).catch((x) => toast(x.message, "bad"));
  });

  $$("[data-close]").forEach((b) =>
    b.addEventListener("click", () => { $("#sheet").hidden = true; }));
  $$("[data-close-auth]").forEach((b) =>
    b.addEventListener("click", () => { $("#auth").hidden = true; }));
  $$("[data-close-camera]").forEach((b) =>
    b.addEventListener("click", closeCamera));

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    $("#sheet").hidden = true;
    $("#auth").hidden = true;
    if (!$("#camera").hidden) closeCamera();
  });

  $("#auth-btn").addEventListener("click", () => {
    if (state.session.signed_in) signOut();
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

  /* A demo row fills the form and goes, so someone trying this out can
     switch tiers in one click instead of two. */
  $$("#demo-list button").forEach((b) => b.addEventListener("click", () => {
    $("#li-user").value = b.dataset.u;
    $("#li-pass").value = b.dataset.p;
    attemptSignIn(b.dataset.u, b.dataset.p);
  }));

  $("#face-login-btn").addEventListener("click", () => openCamera("identify"));
  $("#cam-go").addEventListener("click", captureShot);

  $("#reg-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const data = await api("/auth/register", {
        method: "POST", retry: false,
        body: {
          username: $("#rg-user").value.trim(),
          password: $("#rg-pass").value,
          display_name: $("#rg-name").value.trim(),
        },
      });
      storeTokens(data);
      paintSession();
      await Promise.all([loadPicks(), loadShelf()]);
      $("#auth").hidden = true;
      toast("Account created. Enrol your face from your account page.", "good");
      show("account");
    } catch (err) {
      $("#reg-msg").textContent = err.message;
      $("#reg-msg").className = "msg bad";
    }
  });

  $$("[data-axis]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-axis]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    state.filter = { axis: b.dataset.axis, value: null };
    state.page.offset = 0;
    paintFacets();
    loadShelf();
  }));

  $("#facets").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-facet]");
    if (!chip) return;
    state.filter.value = state.filter.value === chip.dataset.facet
      ? null : chip.dataset.facet;
    state.page.offset = 0;
    paintFacets();
    loadShelf();
  });

  $("#page-prev").addEventListener("click", () => {
    state.page.offset = Math.max(0, state.page.offset - state.page.limit);
    loadShelf();
  });
  $("#page-next").addEventListener("click", () => {
    state.page.offset += state.page.limit;
    loadShelf();
  });

  $("#search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (!q) return;
    try {
      const hits = await api("/search?q=" + encodeURIComponent(q));
      $("#search-count").hidden = false;
      $("#search-count").textContent =
        `${hits.length} result${hits.length === 1 ? "" : "s"} for “${q}”`;
      $("#search-results").innerHTML = hits.map((h) =>
        card(h.book, `matched ${h.matched.slice(0, 4).join(", ")} in ${h.where}`)).join("")
        || `<p class="empty">Nothing matched. Try an author or a title.</p>`;
    } catch (err) { toast(err.message, "bad"); }
  });

  function wireDrop(dropId, inputId, handler) {
    const drop = $(dropId);
    $(inputId).addEventListener("change", (e) => {
      if (e.target.files[0]) handler(e.target.files[0]);
    });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.remove("over");
    }));
    drop.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files[0];
      if (file) handler(file);
    });
  }

  wireDrop("#drop", "#image-input", async (file) => {
    $("#drop-note").textContent = "Matching " + file.name + "…";
    const form = new FormData();
    form.append("file", file);
    try {
      const hits = await api("/search/image", { method: "POST", body: form, raw: true });
      $("#drop-note").textContent = `Matched against our cover index using ${file.name}`;
      $("#search-count").hidden = false;
      $("#search-count").textContent =
        `${hits.length} visual match${hits.length === 1 ? "" : "es"}`;
      $("#search-results").innerHTML = hits.map((h) =>
        card(h.book, `${Math.round(h.score * 100)}% similar (${h.matched_on})`)).join("")
        || `<p class="empty">No cover in our index looks like that.</p>`;
    } catch (e) {
      $("#drop-note").textContent = "";
      toast(e.message, "bad");
    }
  });

  wireDrop("#scan-drop", "#scan-input", runShelfScan);

  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask(); });

  $$("[data-panel]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-panel]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    showAdmin(b.dataset.panel);
  }));
}

/* ------------------------------------------------------------------ boot */

(async function start() {
  /* Anything that slips past a local catch still reaches the person, rather
     than failing silently in the console. */
  window.addEventListener("unhandledrejection", (e) => {
    toast((e.reason && e.reason.message) || "Something went wrong.", "bad");
  });

  wire();
  wireLibrary();
  await loadSession();
  try {
    await Promise.all([loadTopics(), loadLibrary(),
                       loadFacets(), loadPicks(), loadShelf()]);
  } catch (e) {
    toast("Could not reach the server: " + e.message, "bad");
  }
})();
