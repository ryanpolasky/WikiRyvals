// WikiRyvals admin dashboard. Standalone extension page opened from the side
// panel's profile screen (admins only). Reuses the lobby's stored session token
// and talks to the gated /api/ext/admin/* endpoints - every call is verified
// server-side against the caller's is_admin flag, so this page is just a UI.

const BACKEND = globalThis.WIKIRYVALS_BACKEND; // from config.js
const TOKEN_KEY = "wr_token";
const THEME_KEY = "rwr_theme";
const QUICK_TAGS = ["beta_tester"];

let TOKEN = null;

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "admin-status" + (kind ? " " + kind : "");
}

async function api(path, { method = "GET", body = null } = {}) {
  const opts = { method, headers: {} };
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(Object.assign({ token: TOKEN }, body));
  }
  const res = await fetch(BACKEND + path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-json */ }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function chipHTML(acct, tag) {
  return `<span class="chip">${esc(tag)}<button title="Remove" data-act="untag" data-id="${esc(acct.id)}" data-tag="${esc(tag)}">×</button></span>`;
}

function acctHTML(acct) {
  const tags = acct.tags && acct.tags.length
    ? acct.tags.map((t) => chipHTML(acct, t)).join("")
    : `<span class="chip-empty">no tags</span>`;
  const quick = QUICK_TAGS.map((t) => {
    const has = (acct.tags || []).includes(t);
    return `<button class="btn-secondary quick" data-act="${has ? "untag" : "tag"}" data-id="${esc(acct.id)}" data-tag="${esc(t)}">${has ? "− " : "+ "}${esc(t)}</button>`;
  }).join("");
  return `
    <div class="acct" data-id="${esc(acct.id)}">
      <div class="acct-top">
        <span class="acct-name">${esc(acct.username || "(no username)")}</span>
        ${acct.is_admin ? `<span class="pill-admin">admin</span>` : ""}
        <span class="acct-email">${esc(acct.email)}</span>
      </div>
      <div class="acct-meta">${esc(acct.rank)} · ${acct.rp} EP · ${acct.games} races · ${esc(acct.region || "-")}</div>
      <div class="chips">${tags}</div>
      <div class="tag-row">
        ${quick}
        <input type="text" placeholder="custom tag…" data-role="tag-input" data-id="${esc(acct.id)}" />
        <button class="btn-primary" data-act="add-custom" data-id="${esc(acct.id)}">Add</button>
      </div>
    </div>`;
}

let LAST_QUERY = "";

async function search(q) {
  LAST_QUERY = q;
  setStatus("Searching…");
  try {
    const data = await api(`/api/ext/admin/accounts?q=${encodeURIComponent(q)}`);
    const accts = data.accounts || [];
    $("results").innerHTML = accts.map(acctHTML).join("");
    setStatus(accts.length ? `${accts.length} account${accts.length === 1 ? "" : "s"}` : "No accounts found.");
  } catch (e) {
    setStatus(e.message || "Search failed.", "err");
  }
}

async function mutate(act, id, tag) {
  if (!tag) { setStatus("Enter a tag first.", "err"); return; }
  const path = act === "untag" ? "/api/ext/admin/untag" : "/api/ext/admin/tag";
  try {
    await api(path, { method: "POST", body: { user_id: id, tag } });
    setStatus(`${act === "untag" ? "Removed" : "Added"} “${tag}”.`, "ok");
    await search(LAST_QUERY); // refresh so chips + badges reflect the change
  } catch (e) {
    setStatus(e.message || "Update failed.", "err");
  }
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;
  const id = btn.dataset.id;
  if (act === "add-custom") {
    const input = document.querySelector(`input[data-role="tag-input"][data-id="${CSS.escape(id)}"]`);
    mutate("tag", id, (input && input.value || "").trim());
  } else {
    mutate(act, id, btn.dataset.tag);
  }
});

$("search-btn").addEventListener("click", () => search($("q").value.trim()));
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search($("q").value.trim()); });

(async () => {
  // Match the lobby's theme.
  try {
    const t = await chrome.storage.local.get([THEME_KEY]);
    if (t && t[THEME_KEY] === "light") document.body.classList.add("light");
  } catch (_) {}

  try {
    const got = await chrome.storage.local.get([TOKEN_KEY]);
    TOKEN = got && got[TOKEN_KEY];
  } catch (_) {}

  if (!TOKEN) { setStatus("Not signed in. Open the WikiRyvals side panel and sign in first.", "err"); return; }

  try {
    const me = (await api("/api/ext/me")).user;
    if (!me.is_admin) { setStatus("Your account is not an admin.", "err"); return; }
    await search(""); // initial: newest accounts
  } catch (e) {
    setStatus(e.message || "Could not verify admin access.", "err");
  }
})();
