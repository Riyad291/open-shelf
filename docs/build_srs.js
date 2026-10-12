/*
 * Builds the Software Requirements Specification for the Smart Bookstore
 * project, following the RUP SRS template supplied with the assignment.
 *
 *   node docs/build_srs.js
 */

const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  PageBreak, ImageRun, TableOfContents, Header, Footer, PageNumber,
  LevelFormat, convertInchesToTwip, PageOrientation, Tab,
  TabStopType, LeaderType, PositionalTab, PositionalTabAlignment,
  PositionalTabLeader, VerticalAlign,
} = require("docx");

// ---------------------------------------------------------------- constants

const PAGE_W = 12240;              // US Letter, DXA
const PAGE_H = 15840;
const MARGIN = 1080;               // 0.75"
const CONTENT_W = PAGE_W - MARGIN * 2;   // 10080

const INK = "1C1C22";
const MUTED = "5D574D";
const ACCENT = "7A2E28";
const HEAD_BG = "EFEBE3";
const ALT_BG = "F8F6F2";
const BORDER = "C9C2B4";

const PROJECT = "Inkwell & Co. Smart Bookstore";
const SUBSYSTEM = "AI-Enabled Multimodal Bookstore";
const VERSION = "1.0";
const DOC_ID = "SRS-SBS-001";
const DOC_DATE = "10/Oct/26";
const COMPANY = "[Team name]";

// ---------------------------------------------------------------- helpers

const thin = { style: BorderStyle.SINGLE, size: 4, color: BORDER };
const cellBorders = { top: thin, bottom: thin, left: thin, right: thin };

function p(text, opts = {}) {
  const {
    bold = false, italics = false, size = 20, color = INK,
    align = AlignmentType.LEFT, spacingBefore = 0, spacingAfter = 100,
    font = "Calibri", indent = null,
  } = opts;
  return new Paragraph({
    alignment: align,
    spacing: { before: spacingBefore, after: spacingAfter, line: 264 },
    indent: indent,
    children: [new TextRun({ text, bold, italics, size, color, font })],
  });
}

function body(text, opts = {}) {
  return p(text, { size: 20, ...opts });
}

const HEADINGS = [];   // collected while building, used for the contents page

function h1(text) {
  HEADINGS.push({ level: 1, text });
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 160 },
    children: [new TextRun({ text, bold: true, size: 30, color: ACCENT,
                             font: "Calibri" })],
  });
}

function h2(text) {
  HEADINGS.push({ level: 2, text });
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 280, after: 120 },
    children: [new TextRun({ text, bold: true, size: 24, color: INK,
                             font: "Calibri" })],
  });
}

function h3(text) {
  HEADINGS.push({ level: 3, text });
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 220, after: 100 },
    children: [new TextRun({ text, bold: true, size: 21, color: INK,
                             font: "Calibri" })],
  });
}

function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 70, line: 264 },
    children: [new TextRun({ text, size: 20, color: INK, font: "Calibri" })],
  });
}

function step(n, text) {
  return new Paragraph({
    spacing: { after: 60, line: 264 },
    indent: { left: 340, hanging: 340 },
    children: [
      new TextRun({ text: `${n}.`, size: 20, color: MUTED, font: "Calibri" }),
      new TextRun({ text: `\t${text}`, size: 20, color: INK, font: "Calibri" }),
    ],
    tabStops: [{ type: "left", position: 340 }],
  });
}

function cellPara(text, opts = {}) {
  const { bold = false, size = 19, color = INK, align = AlignmentType.LEFT,
          italics = false } = opts;
  return new Paragraph({
    alignment: align,
    spacing: { before: 40, after: 40, line: 240 },
    children: [new TextRun({ text, bold, size, color, italics,
                             font: "Calibri" })],
  });
}

function cell(content, width, opts = {}) {
  const { shading = null, bold = false, align = AlignmentType.LEFT,
          size = 19, color = INK } = opts;
  const paras = Array.isArray(content)
    ? content
    : [cellPara(String(content), { bold, align, size, color })];
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    borders: cellBorders,
    verticalAlign: VerticalAlign.TOP,
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    shading: shading
      ? { type: ShadingType.CLEAR, color: "auto", fill: shading }
      : undefined,
    children: paras,
  });
}

/** Simple table: header row of strings + rows of strings. */
function table(headers, rows, widths, opts = {}) {
  const { zebra = true } = opts;
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) =>
      cell(h, widths[i], { shading: HEAD_BG, bold: true, size: 19 })),
  });
  const bodyRows = rows.map((r, ri) => new TableRow({
    children: r.map((c, i) => {
      const shading = zebra && ri % 2 === 1 ? ALT_BG : null;
      return Array.isArray(c)
        ? cell(c, widths[i], { shading })
        : cell(String(c), widths[i], { shading });
    }),
  }));
  return new Table({
    columnWidths: widths,
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    rows: [headerRow, ...bodyRows],
  });
}

/** Label/value table used for each use-case report. */
function ucTable(rows) {
  const LW = 1850, VW = CONTENT_W - LW;
  return new Table({
    columnWidths: [LW, VW],
    width: { size: CONTENT_W, type: WidthType.DXA },
    rows: rows.map(([label, value]) => new TableRow({
      children: [
        cell(label, LW, { shading: HEAD_BG, bold: true, size: 19 }),
        Array.isArray(value) ? cell(value, VW) : cell(String(value), VW),
      ],
    })),
  });
}

function spacer(h = 120) {
  return new Paragraph({ spacing: { after: h }, children: [] });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

// ---------------------------------------------------------------- content

const definitions = [
  ["Actor", "A person or external system that interacts with the bookstore to achieve a goal."],
  ["Eigenfaces", "Face recognition by projecting a face image onto the principal components of a gallery of enrolled faces, then classifying by nearest neighbour."],
  ["LLM", "Large Language Model. Here, a model served locally by Ollama."],
  ["MFCC", "Mel-Frequency Cepstral Coefficients. A compact spectral description of a short audio frame, used for speaker identification."],
  ["No-order list", "Titles the store will stop reordering because customers rate them poorly (FR-8)."],
  ["Ollama", "A free tool that serves open-weight language models over local HTTP. No data leaves the shop's computer."],
  ["PCA", "Principal Component Analysis. Used to build the eigenface subspace."],
  ["RAG", "Retrieval-Augmented Generation. Answering a question from retrieved local documents rather than from the model's own parameters."],
  ["RC", "Registered Customer. A customer with an account and a balance."],
  ["SU", "Super-user. The shop owner's account; the only role with administrative privileges."],
  ["TF-IDF", "Term Frequency-Inverse Document Frequency. A weighting that makes a rare word count for more than a common one when ranking documents."],
  ["To-order list", "Titles the store will keep permanently in stock because customers rate them highly (FR-8)."],
  ["VC", "VIP Customer. A registered customer promoted by purchase volume or spend; receives a standing discount."],
  ["VS", "Visitor. An unauthenticated user who may browse but not transact."],
  ["YOLO", "You Only Look Once. A family of single-pass object detectors, used here for face and book detection."],
  ["YuNet", "A compact DNN face detector distributed with OpenCV; the default detector when YOLO weights are absent."],
];

const references = [
  ["[1]", "An AI-enabled multimodal smart bookstore", "Course project specification, final version, 02/Oct/2026. The source of requirements FR-1 to FR-14 and CON-1 to CON-4."],
  ["[2]", "IEEE Std 830-1998", "IEEE Recommended Practice for Software Requirements Specifications. Institute of Electrical and Electronics Engineers, 1998."],
  ["[3]", "Rational Unified Process SRS template", "The document template this specification follows."],
  ["[4]", "ARCHITECTURE.md", "Smart Bookstore design record: layering, resolved ambiguities, and known limitations. Delivered with the source."],
  ["[5]", "Project Gutenberg", "https://www.gutenberg.org - source of the public-domain texts used to populate the catalogue (CON-4)."],
  ["[6]", "Ollama", "https://ollama.com - local LLM runtime used for the assistant's general-knowledge fallback (CON-2)."],
  ["[7]", "OpenCV", "https://opencv.org - computer vision library used for face detection and camera capture."],
  ["[8]", "Turk, M. and Pentland, A.", "\"Eigenfaces for Recognition\", Journal of Cognitive Neuroscience, 3(1), 1991. The face classification method used in this system."],
];

const actors = [
  ["Visitor (VS)", "Primary", "An unauthenticated member of the public. May browse, search and ask public questions. Cannot transact or review."],
  ["Registered Customer (RC)", "Primary", "Holds an account, a prepaid balance and a face enrolment. Generalises Visitor: can do everything a visitor can, plus purchase, review and receive personalised recommendations."],
  ["VIP Customer (VC)", "Primary", "A registered customer promoted automatically by purchase count or spend. Generalises Registered Customer, adding a standing discount and access to the order lists."],
  ["Super-user (SU)", "Primary", "The shop owner. Generalises VIP Customer for read purposes and adds every administrative capability: catalogue, accounts, moderation, finance and biometric enrolment."],
  ["Local LLM", "Secondary", "An Ollama service on the shop's own machine. Answers questions the local knowledge base does not cover, and rephrases retrieved passages."],
  ["Camera", "Secondary", "The machine's webcam. Supplies frames for face enrolment, face login and Shelf Scan."],
  ["Microphone", "Secondary", "The machine's audio input. Supplies recordings for voice enrolment and voice login."],
];

const useCaseList = [
  ["UC-01", "Browse Catalogue", "VS", "Browse books by category, author or publication decade."],
  ["UC-02", "Search by Keyword", "VS", "Rank books against a text query over title, author, description and book text."],
  ["UC-03", "Search by Picture", "VS", "Find a book from a photograph of its cover or an internal figure."],
  ["UC-04", "Ask the Assistant", "VS", "Ask a question; the answer is scoped by role and labelled with its source."],
  ["UC-05", "Register with Face", "VS", "Create an account, enrolling a face as part of signing up."],
  ["UC-06", "Log In by Face or Voice", "RC", "Authenticate with a biometric sample."],
  ["UC-07", "Purchase a Book", "RC", "Buy a title against the account balance, with fines and discounts applied."],
  ["UC-08", "Rate and Review", "RC", "Submit a 1-5 star rating and a comment, subject to moderation."],
  ["UC-09", "View Recommendations", "RC", "See the three titles the system considers most relevant."],
  ["UC-10", "Scan a Shelf", "RC", "Photograph a bookshelf and receive recognition and recommendations."],
  ["UC-11", "Manage Catalogue", "SU", "Add, edit, restock and remove titles, covers and figures."],
  ["UC-12", "Manage Accounts", "SU", "Deposit funds, reopen suspended accounts, suspend accounts, clear warnings."],
  ["UC-13", "Moderate Reviews", "SU", "Maintain the taboo word list and review moderation activity."],
  ["UC-14", "View Finance Report", "SU", "Read revenue, fines, best sellers and liabilities."],
  ["UC-15", "Enrol Biometrics", "SU", "Capture and train face and voice samples for any account."],
  ["UC-16", "Act on Order Lists", "SU", "Read the no-order and to-order lists and restock accordingly."],
];

// Use-case reports -------------------------------------------------------

function useCaseReport(id, name, data) {
  const out = [h3(`3.1.${data.n}  ${id}  ${name}`)];
  const rows = [
    ["Brief description", data.brief],
    ["Primary actor", data.actor],
    ["Secondary actors", data.secondary || "None"],
    ["Preconditions", Array.isArray(data.pre)
      ? data.pre.map((t) => cellPara("–  " + t)) : data.pre],
    ["Basic flow", data.flow.map((t, i) =>
      new Paragraph({
        spacing: { before: 30, after: 30, line: 240 },
        indent: { left: 300, hanging: 300 },
        tabStops: [{ type: "left", position: 300 }],
        children: [
          new TextRun({ text: `${i + 1}.`, size: 19, color: MUTED,
                        font: "Calibri" }),
          new TextRun({ text: `\t${t}`, size: 19, color: INK,
                        font: "Calibri" }),
        ],
      }))],
    ["Alternative flows", data.alt.map((t) => cellPara("–  " + t))],
    ["Postconditions", Array.isArray(data.post)
      ? data.post.map((t) => cellPara("–  " + t)) : data.post],
    ["Requirements", data.reqs],
  ];
  if (data.special) rows.push(["Special requirements", data.special]);
  out.push(ucTable(rows));
  out.push(spacer(200));
  return out;
}

const UC = {
  "UC-01": {
    n: 1, name: "Browse Catalogue",
    brief: "A user browses the shelves along one of three axes without searching.",
    actor: "Visitor (and, by generalisation, every other role)",
    pre: ["The catalogue contains at least one title."],
    flow: [
      "The user opens the Browse view.",
      "The system offers three browsing axes: category, author and publication decade.",
      "The user selects an axis. The system lists every value on that axis with a count of titles.",
      "The user selects a value. The system displays the matching titles as cards showing cover, title, author, category, year, price, rating and order-list status.",
      "The user selects a title. The system records the browse against the title and, if the user is signed in, against their history, then displays the detail page.",
    ],
    alt: [
      "Empty axis value: the system states that nothing is shelved there.",
      "Missing cover image: the system draws a placeholder bearing the title rather than failing.",
      "Catalogue empty: the system invites the super-user to add titles.",
    ],
    post: [
      "The title's browse counter is incremented.",
      "A signed-in user's browsing history gains an entry, which feeds UC-09.",
    ],
    reqs: "FR-5, FR-2",
  },
  "UC-02": {
    n: 2, name: "Search by Keyword",
    brief: "A user finds titles by typing words, matched across metadata and book text.",
    actor: "Visitor",
    pre: ["The catalogue contains at least one title."],
    flow: [
      "The user enters one or more words in the Search view and submits.",
      "The system tokenises the query: lowercases it, discards stopwords and reduces each remaining word to a stem, so that 'opening' and 'opens' match the same documents.",
      "The system scores every title by TF-IDF over its title, author, category, description and the opening of its text file, weighting the title twice.",
      "The system adds a bonus where the query appears verbatim in the title or author.",
      "The system presents the ranked titles, each annotated with the terms that matched and whether they matched metadata or body text.",
    ],
    alt: [
      "Empty query: the system asks for a search term and does nothing else.",
      "No match: the system reports none and suggests an author surname or a broader term.",
      "A newly added title is indexed on the next search without a restart.",
    ],
    post: ["No persistent state changes."],
    reqs: "FR-5",
  },
  "UC-03": {
    n: 3, name: "Search by Picture",
    brief: "A user identifies a title from a photograph of its cover or an internal figure.",
    actor: "Visitor",
    secondary: "None (the image is supplied as a file)",
    pre: [
      "At least one indexed cover or figure exists.",
      "The imaging libraries (numpy, Pillow) are installed.",
    ],
    flow: [
      "The user chooses an image file.",
      "The system reduces the image to a descriptor of three normalised blocks: a 3x3 grid of HSV colour histograms, a 16x16 difference hash, and per-cell edge density.",
      "The system compares that descriptor against every indexed cover and figure by weighted cosine similarity.",
      "The system presents the best matches, each with its similarity as a percentage and whether the match was on a cover or a figure.",
    ],
    alt: [
      "Imaging libraries missing: the system disables the control and states what to install.",
      "Unreadable file: the system reports that the image could not be read.",
      "No confident match: results are shown with their low scores rather than suppressed, so the user can judge.",
    ],
    post: ["The descriptor cache is updated for any image whose file changed."],
    reqs: "FR-5",
    special: "Matching must survive rescaling, moderate blur, small rotation and JPEG compression of the query image.",
  },
  "UC-04": {
    n: 4, name: "Ask the Assistant",
    brief: "A user asks a free-text question and receives an answer scoped to their role and labelled with its source.",
    actor: "Visitor",
    secondary: "Local LLM",
    pre: ["The knowledge base contains at least one document."],
    flow: [
      "The user types a question into the chat box.",
      "The system matches the question against its table of answerable topics, each of which declares a minimum role.",
      "If a topic matches and the user's role meets its minimum, the system answers from stored records and labels the answer [store data].",
      "Otherwise the system retrieves the best-matching passages from the local knowledge base and decides whether the question is local: the best passage must clear a similarity threshold AND share at least one term that is rare across the corpus.",
      "If the question is local, the system answers from those passages, labels the answer [via RAG] and cites the source file and heading.",
      "If it is not local, the system delegates to the local LLM, labels the answer [via LLM] and says so in the text.",
    ],
    alt: [
      "Role too low: the system refuses, names the topic and states which tier is required, so the refusal is informative rather than an empty answer.",
      "LLM unavailable: the system says the question is not covered by local files and reports exactly how to start Ollama.",
      "RAG hit with LLM available: the model rephrases the retrieved passages, but the answer is still labelled [via RAG] because the facts are local.",
    ],
    post: ["No persistent state changes."],
    reqs: "FR-3, FR-12, FR-13",
    special: "Every answer must declare its provenance. An off-topic question must not be answered from local files merely because it shares filler words with them.",
  },
  "UC-05": {
    n: 5, name: "Register with Face",
    brief: "A visitor creates an account; enrolling a face is part of signing up, not an optional extra.",
    actor: "Visitor",
    secondary: "Camera",
    pre: [
      "The chosen username is not already taken.",
      "A camera is available and permitted.",
    ],
    flow: [
      "The visitor enters a display name, username, password and opening deposit.",
      "The system validates every field before touching the camera.",
      "The system opens a live camera window showing the feed with any detected face boxed.",
      "The system captures five face images automatically, about one per second, showing progress.",
      "The system creates the account as a Registered Customer with the stated opening balance.",
      "The system stores the samples, retrains the face model and signs the new customer in.",
    ],
    alt: [
      "Validation fails: the system reports the problem and the camera never opens.",
      "Camera unavailable or permission denied: the system explains how to grant access and no account is created.",
      "The user closes the camera before enough images are captured: no account is created, because an account with neither a usable password route nor a face could never be opened.",
      "Face enrolment disabled by configuration: the account is created from the form alone.",
    ],
    post: [
      "An account exists with role RC and the stated balance.",
      "Five face samples are stored and the face model has been retrained.",
      "The new customer is signed in.",
    ],
    reqs: "FR-1, FR-4",
    special: "The account must be created after the photographs, never before.",
  },
  "UC-06": {
    n: 6, name: "Log In by Face or Voice",
    brief: "An enrolled customer authenticates with a biometric sample rather than a password.",
    actor: "Registered Customer",
    secondary: "Camera, Microphone",
    pre: [
      "The account has at least one enrolled sample and the model has been trained.",
      "The account is not suspended.",
    ],
    flow: [
      "The user selects face or voice sign-in.",
      "For a face, the system opens the live camera window and reports the best match confidence as it goes.",
      "The system computes a descriptor from the sample and compares it against every enrolled identity.",
      "If the best match exceeds the confidence threshold, the system identifies the account.",
      "The system checks that the account is not suspended, then signs the user in and states which modality was used.",
    ],
    alt: [
      "Nobody enrolled: the system says so plainly instead of blaming the camera.",
      "No face found in frame: the system asks the user to move closer and improve the lighting.",
      "Below threshold: the system refuses and reports the best match against the required confidence.",
      "Identified but suspended: the system confirms recognition and then states the suspension and its remedy.",
      "Camera or microphone unavailable: the system offers the saved-file route and, for the super-user, the password.",
    ],
    post: ["A session is established for the identified account."],
    reqs: "FR-4, FR-6",
    special: "An unenrolled stranger must be refused. False acceptance is a more serious failure than false rejection.",
  },
  "UC-07": {
    n: 7, name: "Purchase a Book",
    brief: "A customer buys a title against their prepaid balance, with the VIP discount and the insufficient-funds fine applied.",
    actor: "Registered Customer",
    pre: [
      "The customer is signed in and not suspended.",
      "The title is in stock.",
    ],
    flow: [
      "The customer opens a title and requests to buy it.",
      "The system computes the price, applying the VIP discount if the customer holds that role.",
      "The system confirms the balance covers the price.",
      "The system debits the balance, decrements stock, and records the purchase against both the customer and the title.",
      "The system writes a ledger entry and evaluates promotion to VIP.",
      "The system confirms the purchase and the new balance, and announces any promotion.",
    ],
    alt: [
      "Balance too low: the purchase is refused, a one-dollar fine is charged for the attempt, and the fine is written to the ledger. Repeated attempts can drive the balance below zero.",
      "Balance falls below zero: the account is suspended immediately and the session ends.",
      "Out of stock: the purchase control is disabled and the state is shown on the card.",
      "Suspended account: the purchase is refused with the reason.",
      "Overdraft permitted by configuration: the purchase completes, the balance may go negative and the fine still applies.",
    ],
    post: [
      "Balance, purchase count, lifetime spend and purchase history are updated.",
      "Stock and the title's purchase counter are updated.",
      "A ledger entry exists for the sale or the fine.",
      "The customer may have been promoted to VIP or suspended.",
    ],
    reqs: "FR-6, FR-11",
  },
  "UC-08": {
    n: 8, name: "Rate and Review",
    brief: "A signed-in customer rates a title and comments on it, subject to the taboo filter.",
    actor: "Registered Customer",
    pre: [
      "The customer is signed in and not suspended.",
      "The customer has not already reviewed this title.",
    ],
    flow: [
      "The customer selects a star rating from one to five and optionally writes a comment.",
      "The system screens the comment against the taboo list, masking each occurrence with asterisks.",
      "The system determines the review's weight: a verified purchaser counts double.",
      "The system stores the review and recomputes the title's weighted and plain averages.",
      "The system re-evaluates the title's order-list status.",
      "The system confirms the posting and notes any masking and any extra weight.",
    ],
    alt: [
      "More than three taboo words: the comment is blocked entirely, the author receives a warning, and the review is withheld from public display but retained for the super-user.",
      "Warnings exceed the limit: a registered customer is suspended and the session ends; a VIP is demoted to registered customer and marked ineligible for re-promotion.",
      "Not signed in: the system explains that reviews are tied to an account so warnings can be applied.",
      "Rating outside one to five: the system refuses the submission.",
      "Already reviewed: the system refuses a second review of the same title.",
    ],
    post: [
      "A review record exists, masked or blocked as appropriate.",
      "The title's averages and order-list status are current.",
      "The author's warning count, role or suspension state may have changed.",
    ],
    reqs: "FR-7, FR-8, FR-9, FR-10",
  },
  "UC-09": {
    n: 9, name: "View Recommendations",
    brief: "The home page shows three titles, chosen differently depending on who is looking.",
    actor: "Registered Customer (and every other role, with different behaviour)",
    pre: ["The catalogue contains at least one title."],
    flow: [
      "The user opens the home page.",
      "If the user is a registered or VIP customer with history, the system builds a taste profile over categories and authors, weighting purchases three times as heavily as browses.",
      "The system scores every unpurchased title against that profile, adds the title's weighted rating and store-wide popularity, and boosts anything on the to-order list.",
      "The system excludes titles on the no-order list.",
      "The system presents the top three with a short reason under each.",
    ],
    alt: [
      "Visitor or super-user: the system shows the three most purchased and browsed titles store-wide, with no personalisation.",
      "New account with no history: the system falls back to store-wide popularity and says it is still learning.",
      "Empty catalogue: the system invites the super-user to add titles.",
    ],
    post: ["No persistent state changes."],
    reqs: "FR-2, FR-8",
  },
  "UC-10": {
    n: 10, name: "Scan a Shelf",
    brief: "A customer photographs a bookshelf; the system recognises the titles on it and recommends more. This is the team's creative feature.",
    actor: "Registered Customer",
    secondary: "Camera",
    pre: ["At least one cover is indexed."],
    flow: [
      "The user supplies a photograph of a shelf.",
      "The system separates books from shelf background by per-column detail, splitting the profile with an Otsu-style threshold.",
      "The system subdivides each block of detail into individual books by estimating how many it holds from the expected cover aspect ratio, then cutting at that many of the strongest internal edges.",
      "The system describes each region and matches it against the cover index.",
      "The system reports which titles it recognised and with what confidence.",
      "The system builds a taste profile from the recognised titles and recommends three further titles, excluding anything on the no-order list.",
      "The system flags any recognised title that is on the to-order list as one the shop would buy back.",
    ],
    alt: [
      "YOLO weights present: segmentation uses the detector's 'book' class instead of the edge algorithm; the system reports which route it took.",
      "Nothing recognised: the system says so and advises a straight-on photograph of a single shelf in good light.",
      "Cover index empty: the system explains that there is nothing to match against.",
    ],
    post: ["No persistent state changes. An annotated copy of the photograph is written for display."],
    reqs: "FR-14",
    special: "Estimating the number of books before cutting is what prevents cover artwork from being mistaken for a book boundary.",
  },
  "UC-11": {
    n: 11, name: "Manage Catalogue",
    brief: "The super-user adds and maintains titles, including the images that make picture search work.",
    actor: "Super-user",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user opens the catalogue management view.",
      "To add a title, they enter its metadata and optionally attach a cover image, internal figures and a text file.",
      "The system validates the entry, copies the attachments into the data folder and stores the record.",
      "The system indexes the new images so picture search finds them immediately.",
      "To maintain a title, the super-user selects it and restocks, re-prices or deletes it.",
    ],
    alt: [
      "Invalid field: the system names the problem and stores nothing.",
      "Attachment cannot be copied: the system reports the file error and stores nothing.",
      "Deletion: the system confirms first, and past reviews and purchases of the title survive it.",
    ],
    post: [
      "The catalogue and the picture index reflect the change.",
    ],
    reqs: "FR-5",
  },
  "UC-12": {
    n: 12, name: "Manage Accounts",
    brief: "The super-user credits accounts, reopens suspended ones and applies moderation outcomes by hand.",
    actor: "Super-user",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user opens the accounts view, which lists every account with its balance, spend, fines, warnings and state.",
      "To credit an account, they select it and enter an amount; the system adds it and records a ledger entry.",
      "To reopen a suspended account, they enter a deposit; the system credits it, confirms the balance is now positive, clears the suspension and resets the warning count.",
      "To suspend or clear warnings on an account, they select the corresponding action.",
    ],
    alt: [
      "Deposit does not clear the debt: the system refuses to reopen and states the remaining shortfall.",
      "Account is not suspended: the system says so rather than acting.",
      "Attempt to suspend a super-user: refused.",
    ],
    post: [
      "The account's balance, suspension state, warnings and VIP eligibility reflect the action.",
      "A ledger entry exists for any deposit.",
    ],
    reqs: "FR-6, FR-10",
  },
  "UC-13": {
    n: 13, name: "Moderate Reviews",
    brief: "The super-user maintains the taboo word list and inspects what the filter has done.",
    actor: "Super-user",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user opens the moderation view, which shows the current taboo list and recent moderation activity.",
      "They add or remove a word; the change takes effect immediately for subsequent reviews.",
      "They may type a trial comment into a tester, which reports how many taboo words it contains, what the masked text would look like, and whether it would be blocked.",
      "They review the activity log, which shows the original and the displayed text of every affected comment and every warning, demotion and suspension that followed.",
    ],
    alt: [
      "Duplicate word: the system states that it is already listed.",
      "No word selected for removal: the system asks for a selection.",
    ],
    post: ["The taboo list is persisted to configuration."],
    reqs: "FR-9, FR-10",
  },
  "UC-14": {
    n: 14, name: "View Finance Report",
    brief: "The super-user reads the shop's financial position. No other role may see any part of it.",
    actor: "Super-user",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user opens the finance view, or asks the assistant a financial question.",
      "The system derives every figure from the transaction ledger: sales revenue, fine income, refunds, order count, average order value, deposits taken, outstanding customer balances and inventory at retail.",
      "The system also reports best sellers, revenue by category and the account population by role.",
      "The system presents the report.",
    ],
    alt: [
      "No transactions: all figures read zero rather than erroring.",
      "A lower role asks a financial question: the assistant refuses and names the required tier.",
    ],
    post: ["No persistent state changes."],
    reqs: "FR-3",
    special: "Figures are derived from the ledger rather than stored separately, so the report cannot drift out of step with the accounts.",
  },
  "UC-15": {
    n: 15, name: "Enrol Biometrics",
    brief: "The super-user enrols a face or voice for any account and retrains the models.",
    actor: "Super-user",
    secondary: "Camera, Microphone",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user selects an account.",
      "For a face, the system opens the live camera window and captures the configured number of samples.",
      "For a voice, the system records from the microphone or accepts saved WAV files.",
      "The system stores the samples under the account.",
      "The system retrains both models and reports how many samples and identities each was trained on.",
    ],
    alt: [
      "Camera or microphone unavailable: the system offers enrolment from saved files instead.",
      "Too few samples for an identity: training skips that identity and names it in the report.",
      "Samples deleted: the super-user may clear an account's samples and re-enrol.",
    ],
    post: [
      "The account's sample count reflects the enrolment.",
      "The face and voice models are retrained and persisted.",
    ],
    reqs: "FR-4",
  },
  "UC-16": {
    n: 16, name: "Act on Order Lists",
    brief: "The super-user reads the lists the rating rules produce and restocks accordingly.",
    actor: "Super-user",
    pre: ["The super-user is signed in."],
    flow: [
      "The super-user opens the order lists view.",
      "The system classifies every title: more than three ratings averaging below two stars places it on the no-order list; more than three averaging above four places it on the to-order list.",
      "The system shows both lists with each title's weighted average, plain average and rating count side by side.",
      "The super-user restocks a to-order title directly from the list.",
    ],
    alt: [
      "Either list empty: the system says so.",
      "Weighting disabled by configuration: the plain average is used for classification and both are still displayed.",
    ],
    post: ["Stock may have changed."],
    reqs: "FR-8",
  },
};

// Supplementary requirement tables ---------------------------------------

const businessRules = [
  ["FR-1", "Roles", "The system shall support four user types with distinct privileges: visitor, registered customer, VIP customer and super-user."],
  ["FR-2", "Recommendations", "The home page shall show three recommended titles: personalised from browsing and purchase history for registered and VIP customers, and the most purchased and browsed titles store-wide for visitors and the super-user."],
  ["FR-3", "Assistant scope", "The assistant shall return progressively more information as the asker's privilege rises, in the order visitor, registered, VIP, super-user. Only the super-user shall receive any part of the finance report; other roles shall receive no meaningful financial result."],
  ["FR-4", "Biometric login", "Registered, VIP and super-user accounts shall authenticate with a face and/or voice sample. The system shall provide face detection, a trainable face classifier and a short audio classifier."],
  ["FR-5", "Catalogue", "The super-user shall have a graphical means of entering new titles. Users shall browse by category, author and publication time, and search by keyword and by picture of a cover or internal figure."],
  ["FR-6", "Balance and fines", "Registered and VIP accounts shall hold a money balance. A purchase costing more than the balance shall incur a one-dollar fine. An account with a negative balance shall be suspended and unable to sign in until the super-user reopens it with a positive deposit."],
  ["FR-7", "Weighted reviews", "Any signed-in customer shall rate a title from one to five stars with a comment. Reviews by verified purchasers shall carry greater weight in the displayed average."],
  ["FR-8", "Order lists", "A title with more than three ratings averaging below two stars shall be placed on the no-order list. A title with more than three ratings averaging above four stars shall be placed on the to-order list."],
  ["FR-9", "Taboo filter", "The super-user shall maintain a taboo word list. Each occurrence of a listed word in a comment shall be replaced with asterisks. A comment containing more than three such words shall be blocked entirely and its author issued one warning."],
  ["FR-10", "Warning outcomes", "A registered customer with more than two warnings shall be suspended. A VIP customer with more than two warnings shall be demoted to registered customer."],
  ["FR-11", "VIP promotion", "A registered customer who makes more than three purchases, or who spends more than two hundred dollars in total, shall be promoted to VIP and receive ten per cent off every subsequent purchase."],
  ["FR-12", "Knowledge base", "The system shall hold local information about the store and team as the corpus for retrieval-augmented generation."],
  ["FR-13", "Answer routing", "For each question the system shall first determine whether it relates to the local files. If so it shall answer from them and state that retrieval-augmented generation was used. If not it shall delegate to a language model and state that in the answer."],
  ["FR-14", "Creative feature", "The system shall provide one additional capability of the team's choosing that increases its intelligence."],
];

const constraints = [
  ["CON-1", "A local graphical application is sufficient; no web deployment is required."],
  ["CON-2", "No paid language model shall be required. A free, locally served model is sufficient."],
  ["CON-3", "No database shall be required. Local text files shall be adequate for every feature."],
  ["CON-4", "The catalogue may be populated from freely available public-domain texts."],
];

// ---------------------------------------------------------------- document

const diagramPath = path.join(__dirname, "use_case_diagram.png");
const diagram = fs.readFileSync(diagramPath);

const headerPara = new Header({
  children: [
    new Table({
      columnWidths: [5040, 5040],
      width: { size: CONTENT_W, type: WidthType.DXA },
      borders: {
        top: { style: BorderStyle.NONE }, bottom: thin,
        left: { style: BorderStyle.NONE }, right: { style: BorderStyle.NONE },
        insideHorizontal: { style: BorderStyle.NONE },
        insideVertical: { style: BorderStyle.NONE },
      },
      rows: [new TableRow({
        children: [
          new TableCell({
            width: { size: 5040, type: WidthType.DXA },
            borders: { top: { style: BorderStyle.NONE },
                       bottom: { style: BorderStyle.NONE },
                       left: { style: BorderStyle.NONE },
                       right: { style: BorderStyle.NONE } },
            children: [
              cellPara(PROJECT, { bold: true, size: 17, color: MUTED }),
              cellPara("Software Requirements Specification",
                       { size: 17, color: MUTED }),
            ],
          }),
          new TableCell({
            width: { size: 5040, type: WidthType.DXA },
            borders: { top: { style: BorderStyle.NONE },
                       bottom: { style: BorderStyle.NONE },
                       left: { style: BorderStyle.NONE },
                       right: { style: BorderStyle.NONE } },
            children: [
              cellPara(`Version ${VERSION}`,
                       { size: 17, color: MUTED, align: AlignmentType.RIGHT }),
              cellPara(`${DOC_DATE}   ${DOC_ID}`,
                       { size: 17, color: MUTED, align: AlignmentType.RIGHT }),
            ],
          }),
        ],
      })],
    }),
  ],
});

const footerPara = new Footer({
  children: [
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 100 },
      children: [
        new TextRun({ text: `Confidential    © ${COMPANY}    Page `,
                      size: 16, color: MUTED, font: "Calibri" }),
        new TextRun({ children: [PageNumber.CURRENT], size: 16,
                      color: MUTED, font: "Calibri" }),
        new TextRun({ text: " of ", size: 16, color: MUTED, font: "Calibri" }),
        new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 16,
                      color: MUTED, font: "Calibri" }),
      ],
    }),
  ],
});

const partA = [];   // portrait: front matter .. 2.1.2
const partB = [];   // landscape: the use-case diagram
const partC = [];   // portrait: 2.2 onwards
let cur = partA;

// ---- title page
cur.push(new Paragraph({ spacing: { before: 2200 }, children: [] }));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 160 },
  children: [new TextRun({ text: COMPANY, size: 26, color: MUTED,
                           font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 100 },
  children: [new TextRun({ text: PROJECT, bold: true, size: 56,
                           color: ACCENT, font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 420 },
  children: [new TextRun({ text: "Software Requirements Specification",
                           size: 34, color: INK, font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 90 },
  children: [new TextRun({ text: `for ${SUBSYSTEM}`, italics: true,
                           size: 24, color: MUTED, font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 620, after: 60 },
  children: [new TextRun({ text: `Version ${VERSION}`, bold: true, size: 24,
                           color: INK, font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 60 },
  children: [new TextRun({ text: DOC_DATE, size: 22, color: MUTED,
                           font: "Calibri" })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: DOC_ID, size: 22, color: MUTED,
                           font: "Calibri" })],
}));
cur.push(pageBreak());

// ---- revision history
cur.push(h1("Revision History"));
cur.push(table(
  ["Date", "Version", "Description", "Author"],
  [
    [DOC_DATE, "1.0", "Initial specification. Captures requirements FR-1 to FR-14 and constraints CON-1 to CON-4 from the project specification [1], expressed as sixteen use cases and a set of supplementary requirements.", "[Team member names]"],
  ],
  [1500, 1100, 5680, 1800],
));
cur.push(spacer(300));
cur.push(body(
  "Note: fields shown in square brackets are to be completed by the team before submission.",
  { italics: true, color: MUTED, size: 18 }));
cur.push(pageBreak());

// ---- table of contents
cur.push(h1("Table of Contents"));
const tocAnchor = partA.length;      // contents entries are spliced in here
cur.push(pageBreak());

// ---- 1. Introduction
cur.push(h1("1.  Introduction"));
cur.push(body(
  "This Software Requirements Specification describes the complete externally " +
  "observable behaviour of the Inkwell & Co. Smart Bookstore: an AI-enabled, " +
  "multimodal bookstore application that runs entirely on a single local " +
  "machine. It records what the system must do, the constraints it must " +
  "respect, and the quality attributes it must meet, in enough detail for a " +
  "designer to build it and a tester to confirm it was built."));
cur.push(body(
  "The specification follows the Rational Unified Process SRS outline. " +
  "Requirements are captured as use cases in Section 3.1 and as supplementary " +
  "requirements in Section 3.2."));

cur.push(h2("1.1  Purpose"));
cur.push(body(
  "The purpose of this document is to state, completely and unambiguously, " +
  "what the Smart Bookstore system is required to do for each of its four " +
  "classes of user, and what properties it must have beyond its functions."));
cur.push(body("Specifically, this document:"));
cur.push(bullet("defines the external behaviour of the application, including its user interface, its use of the camera and microphone, and its use of a locally served language model;"));
cur.push(bullet("records non-functional requirements covering performance, usability, reliability, security and privacy;"));
cur.push(bullet("records the design constraints imposed by the project specification [1], notably the prohibitions on a web deployment, a paid language model and a database;"));
cur.push(bullet("resolves, and documents the resolution of, the four points at which the project specification admitted more than one reading (Appendix B);"));
cur.push(bullet("provides a traceability matrix (Appendix A) linking every requirement to the use case that exercises it, the component that implements it and the test that verifies it."));
cur.push(body(
  "The intended readership is the development team, the course instructor " +
  "assessing conformance to the project specification, and any tester " +
  "verifying the delivered system."));

cur.push(h2("1.2  Scope"));
cur.push(body(
  "This specification applies to the whole of the Smart Bookstore application: " +
  "a single-user desktop program with a graphical interface, run locally, " +
  "storing all of its data in plain text files in its own directory."));
cur.push(body("In scope:", { bold: true }));
cur.push(bullet("Catalogue browsing, keyword search and picture search."));
cur.push(bullet("Account registration, biometric enrolment and biometric authentication."));
cur.push(bullet("Purchasing against a prepaid balance, with fines, suspension, promotion and discounting."));
cur.push(bullet("Rating and reviewing, with weighted averages, taboo-word moderation, warnings and automatic ordering lists."));
cur.push(bullet("A role-tiered conversational assistant answering from store records, from a local knowledge base by retrieval, or from a locally served language model."));
cur.push(bullet("Super-user administration of the catalogue, accounts, moderation, finance and biometric enrolment."));
cur.push(bullet("One additional capability of the team's choosing (Shelf Scan, UC-10)."));
cur.push(body("Out of scope:", { bold: true }));
cur.push(bullet("Any web or mobile deployment, any server component, and any multi-machine or concurrent-user operation."));
cur.push(bullet("Real payment processing. All money in the system is a prepaid store balance; no card, bank or payment network is involved."));
cur.push(bullet("Physical fulfilment, delivery tracking and tax calculation."));
cur.push(bullet("Any transmission of customer data, biometric samples or questions to a third-party service."));
cur.push(bullet("Account recovery by email or any other out-of-band channel."));

cur.push(h2("1.3  Definitions, Acronyms, and Abbreviations"));
cur.push(body("The following terms are used throughout this specification with the meanings given."));
cur.push(table(["Term", "Definition"],
  definitions, [2100, 7980]));

cur.push(h2("1.4  References"));
cur.push(table(["Ref.", "Title", "Source"],
  references, [700, 3100, 6280]));

cur.push(h2("1.5  Overview"));
cur.push(body(
  "Section 2 gives the background a reader needs before the requirements " +
  "themselves: the actors, a survey of the use-case model with a diagram, and " +
  "the assumptions and dependencies on which the system's viability rests."));
cur.push(body(
  "Section 3 contains the requirements proper. Section 3.1 gives a use-case " +
  "report for each of the sixteen use cases, stating preconditions, the basic " +
  "flow, alternative flows and postconditions. Section 3.2 gives the " +
  "supplementary requirements: the business rules expressed as numbered " +
  "functional requirements, followed by usability, reliability, performance, " +
  "supportability, security and interface requirements."));
cur.push(body(
  "Section 4 holds supporting information: a traceability matrix, a record of " +
  "the ambiguities resolved during analysis, and a data dictionary."));
cur.push(pageBreak());

// ---- 2. Overall Description
cur.push(h1("2.  Overall Description"));
cur.push(body(
  "The Smart Bookstore replaces the paper processes of a small independent " +
  "bookshop with a single desktop application used by both customers and the " +
  "shop owner. A customer uses it to find and buy books, to review them, and " +
  "to ask questions; the owner uses it to stock the shop, manage accounts, " +
  "moderate what customers write, and read the finances."));
cur.push(body(
  "Three characteristics distinguish it from an ordinary point-of-sale " +
  "system, and each shapes the requirements that follow."));
cur.push(body("It is multimodal.", { bold: true }));
cur.push(body(
  "A user interacts with it by text, by photograph and by face or voice. " +
  "Authentication is biometric rather than typed; a book may be found by " +
  "pointing a camera at it rather than by naming it."));
cur.push(body("It is privilege-tiered throughout.", { bold: true }));
cur.push(body(
  "The same screens and the same assistant produce different results for the " +
  "four roles. Privilege is not a single administrative gate but a property " +
  "of every answer the system gives."));
cur.push(body("It runs entirely locally.", { bold: true }));
cur.push(body(
  "There is no server, no database and no external service. Customer records, " +
  "biometric samples and questions never leave the machine. The language " +
  "model is served from the same computer."));

cur.push(h2("2.1  Use-Case Model Survey"));
cur.push(body(
  "Seven actors interact with the system: four human roles that form a " +
  "generalisation hierarchy, and three secondary actors representing external " +
  "resources the system depends upon."));
cur.push(h3("2.1.1  Actors"));
cur.push(table(["Actor", "Type", "Description"], actors,
  [2150, 1150, 6780]));
cur.push(spacer(200));
cur.push(body(
  "The three customer-facing roles generalise one another: a registered " +
  "customer can do everything a visitor can, a VIP customer everything a " +
  "registered customer can, and the super-user has, in addition, every " +
  "administrative capability. Where a use case below names the visitor as its " +
  "actor, every other role may also perform it."));

cur.push(h3("2.1.2  Use Cases"));
cur.push(table(["ID", "Use case", "Actor", "Brief description"],
  useCaseList, [750, 2100, 700, 6530]));
cur.push(spacer(220));

cur = partB;
cur.push(h3("2.1.3  Use-Case Diagram"));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { before: 120, after: 120 },
  children: [new ImageRun({
    data: diagram, type: "png",
    transformation: { width: 912, height: 579 },
  })],
}));
cur.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  spacing: { after: 240 },
  children: [new TextRun({
    text: "Figure 1 — Use-case model. Customer roles generalise left to " +
          "right; secondary actors are shown with dashed associations.",
    italics: true, size: 18, color: MUTED, font: "Calibri" })],
}));

cur = partC;
cur.push(h2("2.2  Assumptions and Dependencies"));
cur.push(body(
  "The viability of the system as specified rests on the following " +
  "assumptions. Each is stated with the consequence of its not holding."));
cur.push(table(
  ["ID", "Assumption", "Consequence if it does not hold"],
  [
    ["A-1", "A Python 3 runtime with Tkinter is available on the target machine.", "The application cannot start. Tkinter is not installable with pip and must come from the operating system's own package."],
    ["A-2", "A webcam is present, functional and permitted to the application.", "Face enrolment and face login are unavailable. Registration cannot complete while face enrolment is mandatory; the configuration flag must be cleared to fall back to passwords."],
    ["A-3", "The numerical and imaging libraries (numpy, Pillow, OpenCV) can be installed.", "Picture search, Shelf Scan and all biometric features are disabled. The rest of the system continues to work and reports what is missing."],
    ["A-4", "An Ollama service with a pulled model is reachable on the local machine.", "Questions not covered by the local knowledge base cannot be answered. Retrieval from local files and all store-record answers are unaffected."],
    ["A-5", "The catalogue is populated before the system is demonstrated.", "Browsing, search and recommendations have nothing to show."],
    ["A-6", "Exactly one instance of the application accesses a given data directory at a time.", "Concurrent writes could interleave. The system is specified for single-user desktop operation only."],
    ["A-7", "The machine's clock is approximately correct.", "Timestamps on purchases, reviews and ledger entries would be misleading, though no rule depends on absolute time."],
    ["A-8", "The user operating the super-user account is trusted.", "The super-user can read all finances and all customer records by design; the system does not defend against a hostile owner."],
  ],
  [700, 4400, 4980]));
cur.push(pageBreak());

// ---- 3. Specific Requirements
cur.push(h1("3.  Specific Requirements"));
cur.push(body(
  "This section states every software requirement in sufficient detail for " +
  "design and for test. Functional requirements are expressed as use cases in " +
  "Section 3.1; requirements that do not belong to any single use case are " +
  "stated in Section 3.2."));

cur.push(h2("3.1  Use-Case Reports"));
cur.push(body(
  "Each report below states the use case's brief description, its actors, the " +
  "conditions that must hold before it begins, its basic flow, the " +
  "alternatives to that flow, the conditions that hold when it ends, and the " +
  "numbered requirements it satisfies."));

Object.entries(UC).forEach(([id, data]) => {
  useCaseReport(id, data.name, data).forEach((el) => cur.push(el));
});

cur.push(pageBreak());

// ---- 3.2 Supplementary requirements
cur.push(h2("3.2  Supplementary Requirements"));
cur.push(body(
  "The requirements in this section either apply across several use cases or " +
  "describe properties of the system rather than its behaviour in any one " +
  "interaction."));

cur.push(h3("3.2.1  Functionality — Business Rules"));
cur.push(body(
  "The following numbered requirements are taken from the project " +
  "specification [1]. They are stated here as system requirements and are " +
  "referenced by the use-case reports above."));
cur.push(table(["ID", "Rule", "Requirement"], businessRules,
  [700, 1760, 7620]));
cur.push(spacer(200));
cur.push(body(
  "Boundary conditions are to be read strictly as written. “More than " +
  "three ratings” means four or more. An average of exactly 2.0 does not " +
  "place a title on the no-order list, and exactly 4.0 does not place it on " +
  "the to-order list. “More than two warnings” means the third " +
  "warning triggers the consequence. “More than three purchases” " +
  "means the fourth purchase promotes. Taboo words are counted as " +
  "occurrences, not as distinct words."));

cur.push(h3("3.2.2  Usability"));
cur.push(table(["ID", "Requirement"], [
  ["SR-U1", "Every action that can fail shall report why it failed in terms the user can act on, naming the remedy where one exists. A failure message shall not attribute a failure to a cause the system has not established."],
  ["SR-U2", "Any operation that uses the camera or microphone shall show the user what is being captured while it is being captured, including the region the system has detected and the progress of the capture."],
  ["SR-U3", "The interface shall present only those tabs and controls the signed-in role may use, so that privilege is visible rather than discovered through refusal."],
  ["SR-U4", "Where the assistant declines a question for want of privilege, it shall name the tier required, so that the tiering is observable."],
  ["SR-U5", "A user shall be able to complete registration, enrolment and first sign-in without reading documentation."],
  ["SR-U6", "Every figure that drives an automatic decision — rating averages, warning counts, progress toward VIP — shall be visible to the user it concerns."],
], [900, 9180]));

cur.push(h3("3.2.3  Reliability"));
cur.push(table(["ID", "Requirement"], [
  ["SR-R1", "A write to any data file shall be atomic: a failure during the write shall leave the previous contents intact rather than a partial file."],
  ["SR-R2", "A data file that cannot be parsed shall be preserved for inspection and the system shall start from an empty collection rather than refusing to start or overwriting the file."],
  ["SR-R3", "The absence of any optional dependency — imaging libraries, camera, microphone, language model — shall disable only the features that need it, and the system shall start and remain usable."],
  ["SR-R4", "A data file written by an earlier version shall remain loadable; fields added later shall be given their default values on load."],
  ["SR-R5", "An operation running in the background shall not fail if the user closes the window before it completes."],
  ["SR-R6", "No single user action shall be able to leave an account in a state from which it cannot be accessed by any means."],
], [900, 9180]));

cur.push(h3("3.2.4  Performance"));
cur.push(table(["ID", "Requirement", "Target"], [
  ["SR-P1", "Application start, including loading all data files and building the knowledge-base index", "Under 3 seconds for a catalogue of 100 titles"],
  ["SR-P2", "Keyword search response", "Under 200 ms for 100 titles"],
  ["SR-P3", "Picture search response against a built index", "Under 1 second for 200 indexed images"],
  ["SR-P4", "Face identification from a captured frame", "Under 300 ms once the model is trained"],
  ["SR-P5", "Camera preview frame rate", "At least 15 frames per second"],
  ["SR-P6", "Retrieval from the local knowledge base", "Under 150 ms for 100 passages"],
  ["SR-P7", "Any operation expected to exceed 1 second shall run without freezing the interface and shall show that it is working", "Applies to all camera, model-training and language-model operations"],
], [900, 5600, 3580]));

cur.push(h3("3.2.5  Supportability"));
cur.push(table(["ID", "Requirement"], [
  ["SR-S1", "Every tunable business rule — thresholds, weights, limits, the taboo list — shall be held in a single configuration file editable without changing code."],
  ["SR-S2", "Each point at which the project specification admitted more than one reading shall be implemented with a documented default and a configuration switch for the alternative (Appendix B)."],
  ["SR-S3", "The business rules shall be implementable and testable without constructing a user interface or loading any model."],
  ["SR-S4", "The system shall provide a diagnostic view reporting the state of the knowledge base, the language model, the search indexes, the biometric models and every rule threshold in force."],
  ["SR-S5", "Every numbered requirement shall be covered by at least one automated test."],
], [900, 9180]));

cur.push(h3("3.2.6  Security and Privacy"));
cur.push(table(["ID", "Requirement"], [
  ["SR-SEC1", "Passwords shall be stored only as a salted cryptographic hash. The plaintext shall not be written to any file."],
  ["SR-SEC2", "A malformed or hand-edited stored credential shall cause authentication to fail, not the application to stop."],
  ["SR-SEC3", "Biometric samples and the models derived from them shall remain on the local machine and shall not be transmitted anywhere."],
  ["SR-SEC4", "A customer shall be able to have their biometric samples and their account deleted on request."],
  ["SR-SEC5", "Biometric identification shall reject an unenrolled person. The confidence threshold shall be a single configurable value so that false acceptance can be tuned against false rejection."],
  ["SR-SEC6", "Financial and administrative information shall be unavailable to any role below super-user through every route, including the assistant."],
  ["SR-SEC7", "A suspended account shall be unable to authenticate by any means, biometric or otherwise."],
  ["SR-SEC8", "The original text of a blocked comment shall be retained for the super-user and withheld from everyone else."],
], [1000, 9080]));

cur.push(h3("3.2.7  Design Constraints"));
cur.push(body(
  "The following constraints are imposed by the project specification [1] and " +
  "are not open to trade-off."));
cur.push(table(["ID", "Constraint"], constraints, [900, 9180]));
cur.push(spacer(160));
cur.push(body(
  "A further constraint follows from CON-3: because there is no database, " +
  "every collection is held as a text file and read into memory at start-up. " +
  "This bounds the catalogue size the system is specified for (Section 4, " +
  "Appendix C) and is the reason for requirements SR-R1 and SR-R2."));

cur.push(h3("3.2.8  Interface Requirements"));
cur.push(body("User interfaces", { bold: true }));
cur.push(bullet("A single resizable desktop window, organised as tabs whose membership depends on the signed-in role."));
cur.push(bullet("A modal camera window used for enrolment and for biometric sign-in, showing the live feed, the detected region and progress."));
cur.push(bullet("Standard file-selection dialogues for supplying images, recordings and text files."));
cur.push(body("Hardware interfaces", { bold: true }));
cur.push(bullet("A video capture device accessed through the operating system's camera interface, subject to the platform's permission model."));
cur.push(bullet("An audio input device, used only when the optional recording library is present. Enrolment and login from saved audio files shall be available without it."));
cur.push(body("Software interfaces", { bold: true }));
cur.push(bullet("A language-model service reachable over HTTP on the local machine, addressed by a configurable URL and model name. The system shall treat its absence as a normal condition."));
cur.push(bullet("The host file system, for all persistent storage, within the application's own directory."));
cur.push(body("Communications interfaces", { bold: true }));
cur.push(bullet("None beyond the local loopback interface used to reach the language model. The system shall make no outbound network connection in normal operation."));

cur.push(pageBreak());

// ---- 4. Supporting Information
cur.push(h1("4.  Supporting Information"));
cur.push(body(
  "This section contains material that makes the specification easier to use. " +
  "The appendices are part of the requirements."));

cur.push(h2("Appendix A  —  Requirements Traceability Matrix"));
cur.push(body(
  "Every requirement from the project specification is traced to the use case " +
  "that exercises it, the component that implements it and the automated " +
  "tests that verify it."));
cur.push(table(["Req.", "Use case(s)", "Implementing component", "Verified by"], [
  ["FR-1", "UC-05, all", "models.py (roles), ui/app.py (tab membership)", "TestRoles"],
  ["FR-2", "UC-09", "recommend.py", "TestRecommendations"],
  ["FR-3", "UC-04, UC-14", "chat.py (intent table), finance.py", "TestChatTiering, TestFinance"],
  ["FR-4", "UC-05, UC-06, UC-15", "biometrics.py, ui/camera.py, ui/login.py", "TestFaceEnrolmentAndMatching, TestPasswordGating, TestCameraDialog"],
  ["FR-5", "UC-01, UC-02, UC-03, UC-11", "catalog.py, imagesearch.py, ui/admin.py", "TestCatalogue"],
  ["FR-6", "UC-07, UC-12", "rules.py (purchase, reopen_account)", "TestPurchasesAndBalance"],
  ["FR-7", "UC-08", "rules.py (submit_review, rating_summary)", "TestReviews"],
  ["FR-8", "UC-08, UC-16", "rules.py (list_status, order_lists)", "TestOrderLists"],
  ["FR-9", "UC-08, UC-13", "moderation.py", "TestModeration"],
  ["FR-10", "UC-08, UC-12", "rules.py (_apply_warning)", "TestReviews"],
  ["FR-11", "UC-07", "rules.py (_check_promotion, price_for)", "TestVipPromotion"],
  ["FR-12", "UC-04", "data/kb/*.txt, rag.py", "TestRag"],
  ["FR-13", "UC-04", "chat.py (ask), rag.py (is_local_question)", "TestRag, TestChatTiering"],
  ["FR-14", "UC-10", "shelfscan.py", "Manual: 19 of 19 across five shelf layouts"],
  ["CON-1", "All", "Tkinter desktop application", "Inspection"],
  ["CON-2", "UC-04", "llm.py (local HTTP client, standard library only)", "Inspection"],
  ["CON-3", "All", "storage.py (JSON text files)", "TestStorage"],
  ["CON-4", "UC-01", "tools/seed.py (public-domain text download)", "Inspection"],
], [800, 1900, 4180, 3200]));

cur.push(h2("Appendix B  —  Resolved Ambiguities"));
cur.push(body(
  "The project specification admitted more than one reading at four points. " +
  "Each is resolved here with a stated default and a configuration switch, so " +
  "that a different ruling can be adopted without changing code."));
cur.push(table(["Point", "Readings", "Resolution and rationale"], [
  ["Unaffordable purchase (FR-6)",
   "The purchase is refused and fined; or the purchase completes, the balance goes negative, and it is fined.",
   "Default: refused and fined (allow_overdraft_purchase = false). A fine reads as a penalty for the attempt, and a shop does not release goods that cannot be paid for. The suspension path remains reachable because repeated fines drive the balance below zero."],
  ["Demotion and re-promotion (FR-10 against FR-11)",
   "A VIP demoted for warnings still satisfies the promotion rule on their existing purchase history, so FR-11 would immediately restore the status FR-10 removed.",
   "Default: demotion marks the account ineligible for promotion until the super-user clears it (demotion_blocks_repromotion = true). Without this the demotion has no effect, which cannot be the intent of FR-10."],
  ["Which average classifies a title (FR-7 against FR-8)",
   "FR-7 weights purchaser reviews; FR-8 sets thresholds on “the average” without saying which.",
   "Default: the weighted average classifies, the unweighted count satisfies “more than three ratings” (order_lists_use_weighted_average = true). Weighting the count would let two purchasers appear as four reviewers. Both averages are displayed wherever a list decision is shown."],
  ["Whether visitors may review (FR-7 against FR-9, FR-10)",
   "FR-7 says “any user”; FR-9 and FR-10 require warnings to attach to an author and escalate to suspension or demotion.",
   "Default: reviewing requires an account (visitors_may_review = false). A visitor has no account to carry a warning, so including them would make the moderation chain unenforceable."],
], [2100, 3400, 4580]));

cur.push(h2("Appendix C  —  Data Dictionary"));
cur.push(body(
  "All persistent data is held as text. The following collections exist; each " +
  "is one file."));
cur.push(table(["Collection", "Key fields", "Notes"], [
  ["Users", "identifier, username, display name, password salt and hash, role, balance, warnings, suspension state and reason, VIP eligibility, purchase count, lifetime spend, fines paid, browsing history, purchase history",
   "Visitors are not stored; the visitor role is the absence of a session."],
  ["Books", "identifier, title, author, category, publication year, price, stock, description, cover path, figure paths, text path, browse count, purchase count",
   "Image and text paths are relative to the application directory."],
  ["Reviews", "identifier, book, author, role at time of writing, stars, original text, displayed text, taboo word count, blocked flag, verified-purchase flag, weight",
   "Both the original and the masked text are retained; only the super-user may see the original."],
  ["Transactions", "identifier, kind (purchase, fine, deposit, refund), user, book, amount, note, timestamp",
   "Every figure in the finance report is derived from this ledger."],
  ["Events", "kind, user, detail, timestamp", "Audit trail of warnings, suspensions, promotions and demotions."],
  ["Knowledge base", "Plain text files describing the store, its policies, its team and its frequently asked questions", "The corpus for FR-12 and FR-13. Editable without restarting."],
  ["Biometric samples", "Normalised face images and audio recordings, stored per account", "Never transmitted. Deletable on request (SR-SEC4)."],
  ["Biometric models", "Face subspace mean, basis and gallery; voice centroids", "Derived from the samples and regenerated by retraining."],
], [1700, 4400, 3980]));

cur.push(h2("Appendix D  —  Index of Requirements"));
cur.push(body(
  "FR-1 to FR-14: Section 3.2.1. CON-1 to CON-4: Section 3.2.7. " +
  "SR-U1 to SR-U6 (usability): Section 3.2.2. SR-R1 to SR-R6 (reliability): " +
  "Section 3.2.3. SR-P1 to SR-P7 (performance): Section 3.2.4. " +
  "SR-S1 to SR-S5 (supportability): Section 3.2.5. SR-SEC1 to SR-SEC8 " +
  "(security and privacy): Section 3.2.6. UC-01 to UC-16: Section 3.1."));

// ------------------------------------------------- contents page (2nd pass)

// Page numbers come from a previous render of this document; on the first
// build the file does not exist and the numbers are simply blank, which keeps
// the layout identical between the two passes.
let tocPages = {};
try {
  tocPages = JSON.parse(
    fs.readFileSync(path.join(__dirname, "toc_pages.json"), "utf8"));
} catch (e) { /* first pass */ }

fs.writeFileSync(path.join(__dirname, "toc_index.json"),
                 JSON.stringify(HEADINGS, null, 1));

const tocParas = HEADINGS
  .filter((h) => h.text !== "Table of Contents" && h.text !== "Revision History")
  .map((h) => {
    const indent = { 1: 0, 2: 300, 3: 620 }[h.level];
    const size = h.level === 1 ? 21 : 19;
    const bold = h.level === 1;
    const page = tocPages[h.text] != null ? String(tocPages[h.text]) : "";
    return new Paragraph({
      spacing: { before: h.level === 1 ? 110 : 20, after: 20, line: 240 },
      indent: { left: indent },
      // A right tab stop with a dot leader is understood by both Word and
      // LibreOffice; PositionalTab is not rendered by the latter.
      tabStops: [{
        type: TabStopType.RIGHT,
        position: CONTENT_W - 40,
        leader: LeaderType.DOT,
      }],
      children: [
        new TextRun({ text: h.text, bold, size,
                      color: h.level === 1 ? INK : MUTED, font: "Calibri" }),
        new TextRun({ children: [new Tab()], size, color: MUTED,
                      font: "Calibri" }),
        new TextRun({ text: page, bold, size, color: MUTED, font: "Calibri" }),
      ],
    });
  });

partA.splice(tocAnchor, 0, ...tocParas);

// ---------------------------------------------------------------- assemble

const doc = new Document({
  creator: COMPANY,
  title: `${PROJECT} — Software Requirements Specification`,
  description: `Software Requirements Specification, version ${VERSION}`,
  numbering: {
    config: [{
      reference: "bullets",
      levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 460, hanging: 240 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "◦",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 880, hanging: 240 } } } },
      ],
    }],
  },
  styles: {
    default: {
      document: { run: { font: "Calibri", size: 20, color: INK } },
    },
  },
  sections: [
    {
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H },
          margin: { top: 1240, right: MARGIN, bottom: 1080, left: MARGIN,
                    header: 560, footer: 560 },
        },
      },
      headers: { default: headerPara },
      footers: { default: footerPara },
      children: partA,
    },
    {
      // The use-case diagram is wider than it is tall, so it gets a landscape
      // page of its own rather than being shrunk to portrait width.
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H,
                  orientation: PageOrientation.LANDSCAPE },
          margin: { top: 1240, right: MARGIN, bottom: 1080, left: MARGIN,
                    header: 560, footer: 560 },
        },
      },
      headers: { default: headerPara },
      footers: { default: footerPara },
      children: partB,
    },
    {
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H },
          margin: { top: 1240, right: MARGIN, bottom: 1080, left: MARGIN,
                    header: 560, footer: 560 },
        },
      },
      headers: { default: headerPara },
      footers: { default: footerPara },
      children: partC,
    },
  ],
});

const out = path.join(__dirname, "Smart_Bookstore_SRS.docx");
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(out, buf);
  console.log("wrote", out, (buf.length / 1024).toFixed(0) + " KB");
});
