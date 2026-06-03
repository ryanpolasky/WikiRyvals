"use strict";

// WikiRyvals lobby. Phase 1: real accounts (passwordless email + code),
// ranked matchmaking with a "match found" screen, ranked results, private
// code-based lobbies, and a live leaderboard/history/profile, all backed by
// the FastAPI server. Race starts still go through the background worker so the
// in-page HUD on Wikipedia drives the actual race; the lobby polls the server
// for the authoritative result.

const BACKEND = globalThis.WIKIRYVALS_BACKEND;  // from config.js (loaded first in lobby.html)
const TOKEN_KEY = "wr_token";

let TOKEN = null;
let ME = null;                 // current user dict
const poll = { ticket: null, result: null, lobby: null }; // interval handles

// Presence port so the in-page pull tab can toggle this panel shut (Chrome has
// no sidePanel.close(), so we close ourselves when the background asks).
try {
  const panelPort = chrome.runtime.connect({ name: "rwr-panel" });
  panelPort.onMessage.addListener((m) => { if (m && m.type === "close") window.close(); });
} catch (_) {}

// ---------------------------------------------------------------- net helpers

function bgOnce(type, extra, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    // If the service worker is wedged the response callback can never fire, which
    // would otherwise await forever. A timeout guarantees bg() always resolves.
    const timer = setTimeout(() => done({ ok: false, error: "timeout" }), timeoutMs || 3500);
    try {
      chrome.runtime.sendMessage(Object.assign({ type }, extra || {}), (resp) => {
        clearTimeout(timer);
        // Reading lastError clears the "Unchecked runtime.lastError" warning that
        // fires when the service worker was asleep and the channel closed early.
        const err = chrome.runtime.lastError;
        if (err || !resp) done({ ok: false, error: (err && err.message) || "empty" });
        else done(resp);
      });
    } catch (e) {
      clearTimeout(timer);
      done({ ok: false, error: String(e) });
    }
  });
}

// MV3 evicts the background service worker after ~30s idle, and the first message
// after eviction can come back empty (or hang) while it cold-starts. Retry a
// couple of times with backoff so a sleeping worker still gets woken.
async function bg(type, extra) {
  let resp = await bgOnce(type, extra);
  for (let i = 0; i < 2 && (!resp || resp.ok === false); i++) {
    await new Promise((r) => setTimeout(r, 300 * (i + 1)));
    resp = await bgOnce(type, extra);
  }
  return resp || { ok: false };
}

async function api(path, { method = "GET", body = null, auth = false } = {}) {
  const opts = { method, headers: {} };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(auth && TOKEN ? Object.assign({ token: TOKEN }, body) : body);
  }
  if (auth && TOKEN && method === "GET") {
    opts.headers["Authorization"] = "Bearer " + TOKEN;
  }
  const res = await fetch(BACKEND + path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-json */ }
  if (!res.ok) {
    const detail = (data && data.detail) || `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

function $(id) { return document.getElementById(id); }
function initials(name) {
  if (!name) return "··";
  const parts = name.replace(/[^A-Za-z0-9 ]/g, " ").trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}
function fmtTime(ms) {
  if (ms == null) return "-";
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}
// Cosmetic badges next to a username from account tags. `compact` shrinks the
// label for the tight me-bar. Usernames are validated [A-Za-z0-9_-] server-side,
// so the surrounding name is safe to interpolate.
function cbadgesHTML(tags, { compact = false } = {}) {
  if (!Array.isArray(tags) || !tags.length) return "";
  let out = "";
  if (tags.includes("creator")) {
    out += ` <span class="cbadge cbadge-creator" title="Creator">Creator</span>`;
  }
  if (tags.includes("beta_tester")) {
    out += ` <span class="cbadge cbadge-beta" title="Beta Tester">${compact ? "Beta" : "Beta Tester"}</span>`;
  }
  return out;
}

// VS-card meta row: compact tag badges followed by the player's region.
function vsMetaHTML(tags, region) {
  const badges = cbadgesHTML(tags, { compact: true });
  const reg = region ? `<span class="vs-region">${region}</span>` : "";
  return badges + reg;
}

// ---------------------------------------------------------------- rank visuals

const TIER_SLUGS = ["iron", "bronze", "silver", "gold", "plat", "diamond", "featured", "legend", "ryval"];
const TIER_LABELS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Featured", "Legend", "Ryval"];

// Base (divisioned) tiers have per-division crests that grow III→II→I
// (iron-3/2/1.svg, …). Apex tiers (division 0) and the ladder strip use the
// tier-level crest (iron.svg). Falls back to the tier crest if no division.
const DIVISIONED = new Set(["iron", "bronze", "silver", "gold", "plat", "diamond"]);
function iconFor(slug, division) {
  const s = slug || "iron";
  if (division && DIVISIONED.has(s)) return `icons/ranks/${s}-${division}.svg`;
  return `icons/ranks/${s}.svg`;
}

function rankBadgeHTML(rank) {
  if (!rank) return "";
  const slug = rank.slug && rank.slug !== "unranked" ? rank.slug : "iron";
  const name = rank.name || (rank.tier || "Unranked");
  return `<img class="rank-ic" src="${iconFor(slug, rank.division)}" alt="" /><span class="tier tier-${slug}">${name}</span>`;
}

// Client mirror of wikirace/ranks.py: map an EP (RP) total to its ladder rank so
// we can render a crest for any player from the match payload (which carries rp,
// not the resolved rank). Base tiers split into III/II/I (100 EP each); the apex
// tiers Featured/Legend/Ryval are one 300-EP block each above the base ladder.
const _RANK_LADDER = (() => {
  const DIV_RP = 100;
  const base = [["iron", "Iron"], ["bronze", "Bronze"], ["silver", "Silver"],
    ["gold", "Gold"], ["plat", "Platinum"], ["diamond", "Diamond"]];
  const apex = [["featured", "Featured"], ["legend", "Legend"], ["ryval", "Ryval"]];
  const roman = { 3: "III", 2: "II", 1: "I" };
  const out = [];
  let rp = 0;
  for (const [slug, tier] of base) {
    for (const div of [3, 2, 1]) {
      out.push({ slug, tier, division: div, name: `${tier} ${roman[div]}`, floor: rp });
      rp += DIV_RP;
    }
  }
  for (const [slug, tier] of apex) {
    out.push({ slug, tier, division: 0, name: tier, floor: rp });
    rp += DIV_RP * 3;
  }
  return out;
})();
function rankForRp(rp) {
  rp = Math.max(0, Math.floor(Number(rp) || 0));
  let r = _RANK_LADDER[0];
  for (const e of _RANK_LADDER) { if (rp >= e.floor) r = e; else break; }
  return r;
}

// Crest icon + "<rp> EP" for a VS player card's bottom row.
function vsRankHTML(rp) {
  const rk = rankForRp(rp);
  return `<img class="vs-crest" src="${iconFor(rk.slug, rk.division)}" alt="" title="${rk.name}" />`
    + `<span class="vs-ep">${rp} EP</span>`;
}

// ---------------------------------------------------------------- screens

const SCREENS = ["screen-auth", "screen-match", "screen-racing", "screen-result"];
function showOnly(id) {
  // Show one full-screen overlay (or the app, when id is null) and hide the rest.
  SCREENS.forEach((s) => { const el = $(s); if (el) el.hidden = s !== id; });
  $("app").hidden = id !== null;
}
function showApp() { showOnly(null); }

// ---------------------------------------------------------------- theme

const RWR_THEME_KEY = "rwr_theme";
let rwrTheme = "dark";
function systemTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyTheme(theme) {
  rwrTheme = theme === "light" ? "light" : "dark";
  document.body.classList.toggle("light", rwrTheme === "light");
}
function savedTheme(cb) {
  try {
    chrome.storage.local.get([RWR_THEME_KEY], (res) => cb((res && res[RWR_THEME_KEY]) || systemTheme()));
  } catch (_) { cb(systemTheme()); }
}
// The theme toggle now lives only in the in-page HUD; the panel just follows the
// saved choice and live-updates when it changes (here or in another tab).
(function initTheme() {
  savedTheme(applyTheme);
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === "local" && changes[RWR_THEME_KEY]) {
        applyTheme(changes[RWR_THEME_KEY].newValue || systemTheme());
      }
    });
  } catch (_) {}
})();

// ---------------------------------------------------------------- tabs

function showView(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${name}`));
  if (name === "play") { refreshDaily(); refreshWeekly(); loadRival(); }
  if (name === "friends") loadFriends();
  if (name === "leaderboard") loadLeaderboard();
  if (name === "history") loadHistory();
  if (name === "profile") loadProfile();
}
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.view)));

// Admins only (button stays hidden otherwise): open the dashboard in a tab -
// the side panel is too narrow for a search/results table.
$("open-admin").addEventListener("click", () => {
  const url = chrome.runtime.getURL("admin.html");
  try { chrome.tabs.create({ url }); } catch (_) { window.open(url, "_blank"); }
});

// ================================================================ AUTH

function authErr(msg) {
  const el = $("auth-err");
  el.textContent = msg || "";
  el.hidden = !msg;
}
function authStep(step) {
  ["auth-email-step", "auth-code-step", "auth-profile-step"].forEach((s) => { $(s).hidden = s !== step; });
  authErr("");
}

function guessRegion() {
  let tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (_) {}
  const r = tz.split("/")[0];
  const map = {
    America: "NA", US: "NA", Canada: "NA", Europe: "EU", Africa: "AF",
    Asia: "APAC", Australia: "OC", Pacific: "OC", Antarctica: "Other",
    Atlantic: "EU", Indian: "APAC",
  };
  // South America override (America/* that are clearly southern)
  const sa = ["Argentina", "Sao_Paulo", "Lima", "Bogota", "Santiago", "La_Paz", "Montevideo", "Asuncion", "Caracas"];
  if (sa.some((c) => tz.includes(c))) return "SA";
  const me = ["Riyadh", "Dubai", "Tehran", "Baghdad", "Jerusalem", "Qatar", "Kuwait", "Beirut"];
  if (me.some((c) => tz.includes(c))) return "ME";
  return map[r] || "Other";
}

$("auth-send").addEventListener("click", async () => {
  const email = $("auth-email").value.trim();
  if (!email || !email.includes("@")) { authErr("Enter a valid email."); return; }
  $("auth-send").disabled = true;
  try {
    const r = await api("/api/ext/auth/request-code", { method: "POST", body: { email } });
    $("auth-email-echo").textContent = email;
    authStep("auth-code-step");
    const dc = $("auth-dev-code");
    if (r.dev_code) {
      dc.hidden = false;
      dc.innerHTML = `Dev mode (no mail server): your code is <b>${r.dev_code}</b>`;
      $("auth-code").value = r.dev_code;
    } else { dc.hidden = true; }
  } catch (e) { authErr(e.message); }
  finally { $("auth-send").disabled = false; }
});

$("auth-back").addEventListener("click", () => authStep("auth-email-step"));

$("auth-verify").addEventListener("click", async () => {
  const email = $("auth-email").value.trim();
  const code = $("auth-code").value.trim();
  if (code.length !== 6) { authErr("The code is 6 digits."); return; }
  $("auth-verify").disabled = true;
  try {
    const r = await api("/api/ext/auth/verify", { method: "POST", body: { email, code } });
    TOKEN = r.token;
    await chrome.storage.local.set({ [TOKEN_KEY]: TOKEN });
    ME = r.user;
    if (ME.needs_username) {
      const guess = guessRegion();
      $("auth-region").value = guess;
      $("region-hint").textContent = `Auto-set from your timezone (${guess}). Change it if that's off.`;
      authStep("auth-profile-step");
    } else {
      enterApp();
    }
  } catch (e) { authErr(e.message); }
  finally { $("auth-verify").disabled = false; }
});

let unameTimer = null;
$("auth-username").addEventListener("input", () => {
  const name = $("auth-username").value.trim();
  const st = $("uname-status");
  st.textContent = ""; st.className = "uname-status";
  if (name.length < 3) return;
  clearTimeout(unameTimer);
  unameTimer = setTimeout(async () => {
    try {
      const r = await api(`/api/ext/auth/username-available?name=${encodeURIComponent(name)}`);
      st.textContent = r.available ? "✓ available" : "✗ taken";
      st.className = "uname-status " + (r.available ? "ok" : "bad");
    } catch (_) {}
  }, 350);
});

$("auth-save-profile").addEventListener("click", async () => {
  const username = $("auth-username").value.trim();
  const region = $("auth-region").value;
  if (username.length < 3) { authErr("Username must be at least 3 characters."); return; }
  $("auth-save-profile").disabled = true;
  try {
    const r = await api("/api/ext/auth/profile", { method: "POST", auth: true, body: { username, region } });
    ME = r.user;
    enterApp();
  } catch (e) { authErr(e.message); }
  finally { $("auth-save-profile").disabled = false; }
});

$("me-logout").addEventListener("click", async () => {
  try { await fetch(BACKEND + "/api/ext/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + TOKEN } }); } catch (_) {}
  TOKEN = null; ME = null;
  stopInvitePolling();
  await chrome.storage.local.remove(TOKEN_KEY);
  authStep("auth-email-step");
  showScreenAuth();
});

function showScreenAuth() { showOnly("screen-auth"); }

// ================================================================ ME / PROFILE

function renderMe() {
  if (!ME) return;
  $("me-avatar").textContent = initials(ME.username);
  $("me-name").innerHTML = (ME.username || "player") + cbadgesHTML(ME.tags, { compact: true });
  const rk = ME.rank || {};
  const placing = ME.in_placements;
  const promo = ME.promo || {};
  const inPromo = !placing && promo.in_promo;
  $("me-rank").innerHTML = (placing
    ? `<span class="tier tier-iron">Placements</span> · ${ME.placements_left} left`
    : `${rankBadgeHTML(rk)} · ${ME.rp} EP`)
    + (inPromo ? ` · <span class="promo-pill">⚑ promo</span>` : "");
  const pb = $("promo-banner");
  if (inPromo && promo.target_name) {
    $("promo-banner-rank").textContent = promo.target_name;
    pb.hidden = false;
  } else {
    pb.hidden = true;
  }
  renderStreak();
}

function renderStreak() {
  const d = (ME && ME.daily) || null;
  const chip = $("me-streak");
  const nudge = $("streak-nudge");
  if (!d || !d.streak) { chip.hidden = true; nudge.hidden = true; return; }
  chip.hidden = false;
  chip.textContent = `🔥 ${d.streak}`;
  chip.title = `${d.streak}-day daily streak (best ${d.best})`;
  // Comeback nudge: streak alive from yesterday, today's daily not done yet.
  if (d.at_risk && !d.played_today) {
    $("streak-nudge-n").textContent = d.streak;
    nudge.hidden = false;
  } else {
    nudge.hidden = true;
  }
}

function enterApp() {
  renderMe();
  refreshSeason();
  startInvitePolling();
  showApp();
  showView("play");
}

async function refreshSeason() {
  try {
    const s = (await api("/api/ext/season")).season;
    const chip = $("season-chip");
    chip.textContent = s.label;
    chip.title = `${s.label}, day ${s.day} of ${s.length_days}`;
    chip.hidden = false; // only reveal once we have the real season
  } catch (_) {}
}

async function refreshMe() {
  try { ME = (await api("/api/ext/me", { auth: true })).user; renderMe(); } catch (_) {}
}

// ================================================================ RANKED QUEUE

function clearPolls() {
  Object.keys(poll).forEach((k) => { if (poll[k]) { clearInterval(poll[k]); poll[k] = null; } });
}

$("rk-go").addEventListener("click", () => startQueue($("rk-diff").value));
$("duo-go").addEventListener("click", () => startQueue($("duo-diff").value, "duo"));

// Mode of the in-flight queue/match: "ranked" (1v1) or "duo" (2v2).
let MATCH_FORMAT = "ranked";

async function startQueue(difficulty, format = "ranked") {
  MATCH_FORMAT = format;
  const base = format === "duo" ? "/api/ext/mm/duo" : "/api/ext/mm";
  try {
    const r = await api(`${base}/enqueue`, { method: "POST", auth: true, body: { difficulty } });
    enterMatchSearching(format);
    pollQueue(r.ticket_id, format);
  } catch (e) { hint(e.message, "err"); }
}

// Drive an already-enqueued ticket to a found match. Shared by solo-queue and
// by a friend who was seated in the duos queue after accepting a party invite.
function pollQueue(ticket, format = "ranked", premade = false) {
  const base = format === "duo" ? "/api/ext/mm/duo" : "/api/ext/mm";
  poll.ticket = setInterval(async () => {
    try {
      const p = await api(`${base}/poll?ticket=${ticket}`);
      if (p.status === "found") {
        clearInterval(poll.ticket); poll.ticket = null;
        beginMatch(p.match, format === "duo" ? "duo" : "ranked");
      } else if (p.status === "searching") {
        const secs = Math.floor((p.waited_ms || 0) / 1000);
        // p.searching counts everyone in queue, including you.
        const others = Math.max(0, (p.searching || 1) - 1);
        const need = format === "duo"
          ? (premade ? " (you + partner; finding opponents)" : " (need 4 for a 2v2)")
          : "";
        const who = others === 0 ? "you're first in the queue"
                  : others === 1 ? "1 other player searching"
                  : `${others} other players searching`;
        $("match-note").textContent = `Searching near your rating… ${secs}s · ${who}${need}`;
      } else if (p.status === "expired" || p.status === "cancelled") {
        clearInterval(poll.ticket); poll.ticket = null;
        backToLobby();
      }
    } catch (e) { /* transient */ }
  }, 1000);
}

function enterMatchSearching(format = "ranked") {
  $("match-mode").textContent = format === "duo"
    ? "Searching for a 2v2…" : "Searching for a Ryval…";
  $("vs-you-name").textContent = ME.username;
  $("vs-you-av").textContent = initials(ME.username);
  $("vs-you-meta").innerHTML = vsMetaHTML(ME.tags, ME.region);
  $("vs-you-rank").innerHTML = vsRankHTML(ME.rp);
  $("vs-opp-name").textContent = "…";
  $("vs-opp-av").textContent = "?";
  $("vs-opp-meta").innerHTML = "";
  $("vs-opp-rank").textContent = "";
  $("match-countdown").textContent = "…";
  $("match-start").textContent = "-";
  $("match-target").textContent = "-";
  $("match-note").textContent = "Searching…";
  $("match-forfeit").hidden = true;
  showOnly("screen-match");
}

// ================================================================ FRIENDS + PARTY

function friendsHint(msg, kind) {
  const el = $("friends-hint");
  if (!el) return;
  el.textContent = msg || "";
  el.className = "hint" + (kind ? " " + kind : "");
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function friendStatusDot(card) {
  const cls = card.status === "in party" ? "in-party"
            : card.status === "searching" ? "searching"
            : card.online ? "online" : "offline";
  return `<span class="status-dot ${cls}" title="${esc(card.status || "offline")}"></span>`;
}

async function loadFriends() {
  try {
    const data = await api("/api/ext/friends", { auth: true });
    renderFriends(data);
  } catch (e) { friendsHint(e.message, "err"); }
}

function renderFriends(data) {
  const friends = data.friends || [];
  const incoming = data.incoming || [];
  const outgoing = data.outgoing || [];

  // Friends list.
  const list = $("friend-list");
  $("friends-empty").hidden = friends.length > 0;
  $("friends-count").textContent = friends.length ? `${friends.length}` : "";
  list.innerHTML = friends.map((f) => {
    const canInvite = f.status !== "in party";
    return `<li class="friend-li">
      <div class="friend-main">
        ${friendStatusDot(f)}
        <div class="friend-name">${esc(f.username)}</div>
        <div class="friend-sub">${f.in_placements ? "Placements" : esc(f.rank) + " · " + f.rp + " EP"}</div>
      </div>
      <div class="friend-actions">
        <button class="btn-primary btn-sm" data-invite="${esc(f.id)}" ${canInvite ? "" : "disabled"}>Invite</button>
        <button class="btn-ghost btn-sm" data-remove="${esc(f.id)}" title="Remove">✕</button>
      </div>
    </li>`;
  }).join("");

  // Requests (incoming need a response; outgoing are awaiting).
  const reqCard = $("friend-requests-card");
  reqCard.hidden = incoming.length === 0 && outgoing.length === 0;
  $("friend-incoming").innerHTML = incoming.map((f) => `
    <li class="friend-li">
      <div class="friend-main"><div class="friend-name">${esc(f.username)}</div>
        <div class="friend-sub">wants to be friends</div></div>
      <div class="friend-actions">
        <button class="btn-primary btn-sm" data-accept="${esc(f.id)}">Accept</button>
        <button class="btn-ghost btn-sm" data-decline="${esc(f.id)}">Decline</button>
      </div>
    </li>`).join("");
  $("friend-outgoing").innerHTML = outgoing.map((f) => `
    <li class="friend-li">
      <div class="friend-main"><div class="friend-name">${esc(f.username)}</div>
        <div class="friend-sub">request sent</div></div>
      <div class="friend-actions">
        <button class="btn-ghost btn-sm" data-remove="${esc(f.id)}" title="Cancel">✕</button>
      </div>
    </li>`).join("");

  // Tab badge = number of incoming requests awaiting a response.
  const badge = $("friends-badge");
  if (badge) { badge.hidden = incoming.length === 0; badge.textContent = String(incoming.length); }

  // Wire row actions.
  list.querySelectorAll("[data-invite]").forEach((b) =>
    b.addEventListener("click", () => inviteToDuo(b.dataset.invite)));
  document.querySelectorAll("#view-friends [data-remove]").forEach((b) =>
    b.addEventListener("click", () => friendAction("/api/ext/friends/remove", { friend_id: b.dataset.remove })));
  document.querySelectorAll("#view-friends [data-accept]").forEach((b) =>
    b.addEventListener("click", () => friendAction("/api/ext/friends/respond", { requester_id: b.dataset.accept, accept: true })));
  document.querySelectorAll("#view-friends [data-decline]").forEach((b) =>
    b.addEventListener("click", () => friendAction("/api/ext/friends/respond", { requester_id: b.dataset.decline, accept: false })));
}

async function friendAction(path, body) {
  try { await api(path, { method: "POST", auth: true, body }); loadFriends(); }
  catch (e) { friendsHint(e.message, "err"); }
}

async function addFriend() {
  const name = ($("friend-username").value || "").trim();
  if (!name) return;
  try {
    const r = await api("/api/ext/friends/request", { method: "POST", auth: true, body: { username: name } });
    $("friend-username").value = "";
    friendsHint(r.status === "accepted" ? `You're now friends with ${name}.` : `Request sent to ${name}.`, "ok");
    loadFriends();
  } catch (e) { friendsHint(e.message, "err"); }
}

// ---- party invite (inviter side) -----------------------------------------

async function inviteToDuo(friendId) {
  const difficulty = $("duo-diff") ? $("duo-diff").value : "any";
  try {
    const inv = await api("/api/ext/party/invite", { method: "POST", auth: true, body: { friend_id: friendId, difficulty } });
    enterMatchSearching("duo");
    $("match-mode").textContent = "Duos party";
    $("vs-opp-name").textContent = inv.to_name;
    $("match-note").textContent = `Invite sent to ${inv.to_name}. Waiting for them to accept…`;
    pollPartyInvite(inv.invite_id, inv.to_name);
  } catch (e) { friendsHint(e.message, "err"); }
}

function pollPartyInvite(inviteId, friendName) {
  poll.party = setInterval(async () => {
    try {
      const p = await api(`/api/ext/party/poll?invite=${inviteId}`, { auth: true });
      if (p.status === "accepted" && p.ticket_id) {
        clearInterval(poll.party); poll.party = null;
        $("match-note").textContent = `${friendName} joined. Finding an opposing pair…`;
        $("vs-you-name").textContent = `${ME.username} & ${friendName}`;
        pollQueue(p.ticket_id, "duo", true);
      } else if (["declined", "expired", "cancelled"].includes(p.status)) {
        clearInterval(poll.party); poll.party = null;
        backToLobby();
        showView("friends");
        friendsHint(p.status === "declined" ? `${friendName} declined the invite.`
                  : `Invite to ${friendName} ${p.status}.`, "err");
      }
    } catch (e) { /* transient */ }
  }, 1000);
}

// ---- party invite (recipient side) ---------------------------------------

let ACTIVE_INVITE = null;   // the invite currently shown in the banner
// Kept outside `poll` so backToLobby()/clearPolls() never stops it; this
// background poller must run for the whole session to catch incoming invites.
let invitePollTimer = null;

function startInvitePolling() {
  if (invitePollTimer) return;
  const tick = async () => {
    if (!TOKEN) return;
    try {
      const r = await api("/api/ext/party/incoming", { auth: true });
      const inv = (r.invites || [])[0] || null;
      // Don't pop the banner over an in-flight match/search.
      if (!$("app").hidden) showInviteBanner(inv);
    } catch (_) { /* offline/transient */ }
  };
  tick();
  invitePollTimer = setInterval(tick, 3000);
}

function stopInvitePolling() {
  if (invitePollTimer) { clearInterval(invitePollTimer); invitePollTimer = null; }
  showInviteBanner(null);
}

function showInviteBanner(inv) {
  const banner = $("party-banner");
  if (!banner) return;
  if (!inv) { ACTIVE_INVITE = null; banner.hidden = true; return; }
  ACTIVE_INVITE = inv;
  $("party-banner-from").textContent = inv.from_name;
  banner.hidden = false;
}

async function acceptInvite() {
  if (!ACTIVE_INVITE) return;
  const inv = ACTIVE_INVITE;
  $("party-banner").hidden = true;
  try {
    const r = await api("/api/ext/party/accept", { method: "POST", auth: true, body: { invite_id: inv.invite_id } });
    enterMatchSearching("duo");
    $("match-mode").textContent = "Duos party";
    $("vs-you-name").textContent = `${ME.username} & ${inv.from_name}`;
    $("match-note").textContent = `Teamed with ${inv.from_name}. Finding an opposing pair…`;
    pollQueue(r.ticket_id, "duo", true);
  } catch (e) {
    showView("friends");
    friendsHint(e.message, "err");
  }
}

async function declineInvite() {
  if (!ACTIVE_INVITE) return;
  const inv = ACTIVE_INVITE;
  ACTIVE_INVITE = null;
  $("party-banner").hidden = true;
  try { await api("/api/ext/party/decline", { method: "POST", auth: true, body: { invite_id: inv.invite_id } }); }
  catch (_) {}
}

$("friend-add").addEventListener("click", addFriend);
$("friend-username").addEventListener("keydown", (e) => { if (e.key === "Enter") addFriend(); });
$("party-accept").addEventListener("click", acceptInvite);
$("party-decline").addEventListener("click", declineInvite);

// ================================================================ MATCH FOUND

let CURRENT_MATCH = null;

function beginMatch(match, mode) {
  CURRENT_MATCH = match;
  const isDuo = mode === "duo" || match.team_kind === "duo";
  const inPromo = mode !== "private" && ME && ME.promo && ME.promo.in_promo;
  $("match-mode").textContent = (isDuo ? "Duos Match Found"
    : (mode === "private" ? "Private Match" : "Match Found"))
    + (inPromo ? " · ⚑ PROMO GAME" : "");
  if (isDuo) {
    const mate = match.teammate;
    const opps = match.opponents || [];
    const youTag = cbadgesHTML(match.you.tags, { compact: true });
    const mateTag = mate ? cbadgesHTML(mate.tags, { compact: true }) : "";
    $("vs-you-name").innerHTML = `${match.you.username}${youTag}${mate ? " & " + mate.username + mateTag : ""}`;
    $("vs-you-av").textContent = initials(match.you.username);
    $("vs-you-meta").innerHTML = "";
    $("vs-you-rank").innerHTML = mate ? `with ${mate.username}${mate.is_bot ? " 👻" : ""}` : "";
    $("vs-opp-name").innerHTML = opps.map((o) => o.username + cbadgesHTML(o.tags, { compact: true })).join(" & ") || "opponents";
    $("vs-opp-av").textContent = "2";
    $("vs-opp-meta").innerHTML = "";
    const avg = opps.length ? Math.round(opps.reduce((a, o) => a + o.rating, 0) / opps.length) : 0;
    $("vs-opp-rank").innerHTML = opps.some((o) => o.is_bot) ? `~${avg} · ghosts` : `~${avg} avg`;
  } else {
    const opp = match.opponent;
    $("vs-you-name").textContent = match.you.username;
    $("vs-you-av").textContent = initials(match.you.username);
    $("vs-you-meta").innerHTML = vsMetaHTML(match.you.tags, match.you.region);
    $("vs-you-rank").innerHTML = vsRankHTML(match.you.rp);
    $("vs-opp-name").textContent = opp.username;
    $("vs-opp-av").textContent = initials(opp.username);
    $("vs-opp-meta").innerHTML = vsMetaHTML(opp.tags, opp.region);
    $("vs-opp-rank").innerHTML = vsRankHTML(opp.rp);
  }
  $("match-start").textContent = match.start;
  $("match-target").textContent = match.target;
  showOnly("screen-match");

  let n = 5;
  $("match-countdown").textContent = n;
  $("match-note").textContent = "Get ready…";
  const iv = setInterval(() => {
    n -= 1;
    $("match-countdown").textContent = n > 0 ? n : "GO";
    if (n <= 0) { clearInterval(iv); launchRace(match, mode); }
  }, 1000);
}

// Fallback path: start the race straight from the lobby page (extension origin
// has storage + tabs + backend host permissions) when the background service
// worker is asleep/wedged and won't answer. Mirrors background.js newRace so the
// content script picks up the same race state from chrome.storage.
async function startRaceDirect(mode, start, target) {
  let url = `${BACKEND}/api/ext/new?difficulty=${encodeURIComponent(mode || "any")}`;
  if (start && target) url += `&start=${encodeURIComponent(start)}&target=${encodeURIComponent(target)}`;
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const data = await res.json();
  await chrome.storage.local.set({ race: data });
  await chrome.tabs.create({ url: data.start_url });
  return data;
}

async function launchRace(match, mode) {
  // Start the race via the background worker (opens a Wikipedia tab + drives HUD),
  // then bind that race to the match so the server scores the authoritative run.
  // Wrapped so a flaky worker wake can never strand the player on the countdown.
  try {
    let race = null;
    const resp = await bg("newRace", { newTab: true, difficulty: mode, start: match.start, target: match.target });
    if (resp && resp.ok && resp.race) {
      race = resp.race;
    } else {
      // Worker didn't answer (MV3 eviction). Launch directly from the page.
      race = await startRaceDirect(mode, match.start, match.target);
    }
    if (!race || !race.race_id) {
      hint("Couldn't open the race tab - back to lobby, try again.", "err");
      backToLobby();
      return;
    }
    try {
      await api("/api/ext/mm/bind", { method: "POST", auth: true, body: { match_id: match.match_id, race_id: race.race_id } });
    } catch (e) { /* non-fatal: result poll will report no race */ }
    enterRacing(match);
  } catch (e) {
    hint("Couldn't start the race. The backend may be unavailable.", "err");
    backToLobby();
  }
}

// ================================================================ RACING

// Duos live HUD state: user_id -> {name, role} and the latest progress line.
let DUO_ROSTER = null;

function buildDuoRoster(match) {
  const roster = {};
  let n = 0;
  const add = (side, role) => {
    if (!side) return;
    // Humans key by user_id so live progress can attribute to them; ghosts get
    // a synthetic key (they pre-recorded their run, so no live updates arrive).
    const key = side.user_id || `ghost-${role}-${n++}`;
    roster[key] = { name: side.username, role, ghost: !side.user_id };
  };
  add(match.teammate, "teammate");
  (match.opponents || []).forEach((o) => add(o, "opponent"));
  DUO_ROSTER = { players: roster, progress: {} };
}

function renderDuoHud() {
  if (!DUO_ROSTER) return;
  const rows = Object.entries(DUO_ROSTER.players).map(([uid, p]) => {
    const pr = DUO_ROSTER.progress[uid];
    const tag = p.role === "teammate" ? "🤝" : "⚔";
    const name = p.name + (p.ghost ? " 👻" : "");
    const state = pr
      ? (pr.finished ? `finished - ${pr.clicks} clicks · ${fmtTime(pr.elapsed_ms)}`
                     : `${pr.current || "…"} · ${pr.clicks} clicks`)
      : (p.ghost ? "racing…" : "ready…");
    return `<div class="duo-hud-row"><span>${tag} ${name}</span><span>${state}</span></div>`;
  });
  $("racing-opp").innerHTML = rows.join("");
}

function enterRacing(match) {
  const isDuo = match.team_kind === "duo";
  $("racing-start").textContent = match.start;
  $("racing-target").textContent = match.target;
  $("racing-live").textContent = "";
  const oppEl = $("racing-opp");
  oppEl.hidden = false;
  if (isDuo) {
    buildDuoRoster(match);
    renderDuoHud();
  } else {
    DUO_ROSTER = null;
    oppEl.textContent = `${match.opponent ? match.opponent.username : "Opponent"} is racing…`;
  }
  // Watch-party: anyone with this link can spectate the match read-only (great
  // for casting an ACM-night match on a projector).
  const watchBtn = $("racing-watch");
  if (match.match_id) {
    watchBtn.hidden = false;
    watchBtn.dataset.matchId = match.match_id;
    $("racing-watch-hint").hidden = true;
  } else {
    watchBtn.hidden = true;
  }
  showOnly("screen-racing");

  // Primary path: a WebSocket to the match relays the opponent's live position
  // and the final result the instant the match decides (no polling lag).
  openMatchSocket(match);

  // Fallback path (worker asleep / WS blocked): poll local race + server result
  // on a relaxed cadence. The WS handler clears this once it lands a result.
  poll.result = setInterval(async () => {
    try {
      const rs = await bg("getRace");
      if (rs.ok && rs.race) {
        const r = rs.race;
        $("racing-live").textContent = `${r.clicks || 0} clicks · ${fmtTime(r.elapsed_ms)}`;
      }
    } catch (_) {}
    try {
      const r = await api("/api/ext/mm/result", { method: "POST", auth: true, body: { match_id: match.match_id } });
      if (r.status === "resolved") { await finishWithResult(r); }
    } catch (e) { /* waiting / race not bound yet */ }
  }, 2500);
}

// ---- realtime match channel ------------------------------------------------

let MATCH_WS = null;

function closeMatchSocket() {
  if (MATCH_WS) {
    try { MATCH_WS.onclose = null; MATCH_WS.close(); } catch (_) {}
    MATCH_WS = null;
  }
}

function openMatchSocket(match) {
  closeMatchSocket();
  if (!TOKEN || typeof WebSocket === "undefined") return;
  const wsBase = BACKEND.replace(/^http/, "ws");
  const url = `${wsBase}/api/ext/ws/match/${match.match_id}?token=${encodeURIComponent(TOKEN)}`;
  let ws;
  try { ws = new WebSocket(url); } catch (_) { return; }
  MATCH_WS = ws;
  // Keepalive so the socket isn't reaped by idle proxies during a long race.
  let ka = null;
  ws.onopen = () => { ka = setInterval(() => { try { ws.send("ping"); } catch (_) {} }, 20000); };
  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "progress") {
      // Each side's progress is tagged with its user_id; never show your own.
      if (ME && msg.user_id && msg.user_id !== ME.id) {
        if (DUO_ROSTER && DUO_ROSTER.players[msg.user_id]) {
          DUO_ROSTER.progress[msg.user_id] = msg;
          $("racing-opp").hidden = false;
          renderDuoHud();
        } else {
          const who = match.opponent ? match.opponent.username : "Opponent";
          $("racing-opp").hidden = false;
          $("racing-opp").textContent = msg.finished
            ? `${who} finished - ${msg.clicks} clicks · ${fmtTime(msg.elapsed_ms)}`
            : `${who}: ${msg.current || "…"} · ${msg.clicks} clicks`;
        }
      }
    } else if (msg.type === "resolved") {
      const mine = msg.results && ME ? msg.results[ME.id] : null;
      if (mine) finishWithResult(mine);
    }
  };
  ws.onclose = () => { if (ka) clearInterval(ka); if (MATCH_WS === ws) MATCH_WS = null; };
  ws.onerror = () => { /* fallback poll keeps running */ };
}

async function finishWithResult(r) {
  if (poll.result) { clearInterval(poll.result); poll.result = null; }
  closeMatchSocket();
  await refreshMe();
  showResult(r);
}

$("racing-forfeit").addEventListener("click", async () => {
  if (!CURRENT_MATCH) return backToLobby();
  try {
    const r = await api("/api/ext/mm/result", { method: "POST", auth: true, body: { match_id: CURRENT_MATCH.match_id, forfeit: true } });
    if (r.status === "resolved") { await finishWithResult(r); return; }
  } catch (_) {}
  backToLobby();
});

$("racing-watch").addEventListener("click", async () => {
  const mid = $("racing-watch").dataset.matchId;
  if (!mid) return;
  const url = chrome.runtime.getURL(`watch.html?match=${encodeURIComponent(mid)}`);
  const hint = $("racing-watch-hint");
  hint.hidden = false;
  try {
    await navigator.clipboard.writeText(url);
    hint.textContent = "Watch link copied - open it in a tab to cast the match.";
  } catch (_) {
    hint.textContent = url;
  }
});

// ================================================================ RESULTS

function renderDuoCompare(r) {
  const you = r.you;
  const mate = r.teammate;
  const opps = r.opponents || [];
  const cols = [
    { label: "You", s: you },
    { label: mate ? mate.username + (mate.is_bot ? " 👻" : "") : "-", s: mate },
    ...opps.map((o) => ({ label: o.username + (o.is_bot ? " 👻" : ""), s: o })),
  ];
  const head = `<div class="cmp-row cmp-head cmp-duo"><span></span>${
    cols.map((c, i) => `<span class="${i <= 1 ? "cmp-team" : "cmp-opp"}">${c.label}</span>`).join("")
  }</div>`;
  const cell = (s, f) => (s ? f(s) : "-");
  const row = (label, f) => `<div class="cmp-row cmp-duo"><span>${label}</span>${
    cols.map((c) => `<span>${cell(c.s, f)}</span>`).join("")
  }</div>`;
  $("result-compare").innerHTML = head
    + row("Result", (s) => (s.finished ? "Finished" : "DNF"))
    + row("Clicks", (s) => (s.clicks ?? "-"))
    + row("Time", (s) => fmtTime(s.time_ms))
    + (you.flagged ? `<div class="cmp-flag">⚠ Your run was flagged by anti-cheat (can't win).</div>` : "");
}

let LAST_RESULT = null;
function showResult(r) {
  clearPolls();
  closeMatchSocket();
  LAST_RESULT = r;
  $("share-preview").hidden = true;
  const won = r.won, draw = r.draw;
  const isDuo = r.team_kind === "duo";
  const isRanked = r.ranked || r.mode === "ranked";
  $("result-banner").textContent = draw ? "Draw"
    : (won ? (isDuo ? "Team Victory" : "Victory") : (isDuo ? "Team Defeat" : "Defeat"));
  $("result-banner").className = "result-banner " + (draw ? "draw" : (won ? "win" : "loss"));
  $("result-route").innerHTML = `<b>${r.start}</b> → <b>${r.target}</b>`;

  const d = r.rp.delta;
  $("rp-delta").textContent = (d > 0 ? "+" : "") + d;
  $("rp-delta").className = "rp-delta " + (d > 0 ? "pos" : (d < 0 ? "neg" : ""));

  const rank = r.rank;
  if (rank && rank.hidden) {
    $("result-rank-badge").innerHTML = `<span class="tier tier-iron">Placements</span>`;
    $("result-rank-name").textContent = "Placement match";
    $("result-rank-next").textContent = `${r.rp.placements_left} to go`;
    $("result-rank-fill").style.width = "0%";
    $("result-rank-rp").textContent = "Rank revealed after placements.";
  } else if (rank && isRanked) {
    $("result-rank-badge").innerHTML = rankBadgeHTML({ slug: rank.slug_after, division: rank.division_after, name: rank.after });
    const promo = r.promo || {};
    const promoTag = promo.entered ? " ⚑ promo" : "";
    $("result-rank-name").textContent = rank.after + (rank.promoted ? " ▲ promoted!" : (rank.demoted ? " ▼" : promoTag));
    if (rank.promoted) celebrateRankUp(rank.after, rank.slug_after, promo.won, rank.division_after);
    $("result-rank-next").textContent = rank.next_name ? `→ ${rank.next_name}` : "Apex";
    const pct = rank.rp_span ? Math.max(0, Math.min(100, (rank.rp_into / rank.rp_span) * 100)) : 0;
    $("result-rank-fill").style.width = pct + "%";
    $("result-rank-rp").textContent = rank.next_name ? `${rank.rp_into} / ${rank.rp_span} EP · ${rank.rp_to_next} to ${rank.next_name}` : `${r.rp.after} EP`;
  } else {
    $("result-rank-badge").innerHTML = "";
    $("result-rank-name").textContent = "Unranked match";
    $("result-rank-next").textContent = "";
    $("result-rank-fill").style.width = "0%";
    $("result-rank-rp").textContent = "Private lobbies don't affect your rank.";
  }

  const rt = r.rating;
  $("result-rating").innerHTML = (isRanked && rt)
    ? `Rating ${rt.before} → <b>${rt.after}</b> <span class="${rt.delta >= 0 ? "pos" : "neg"}">(${rt.delta >= 0 ? "+" : ""}${rt.delta})</span>`
    : "";

  // CS2-style promotion series callout.
  const pr = r.promo || {};
  const promoEl = $("result-promo");
  if (pr.entered && pr.target_name) {
    promoEl.className = "promo-note armed";
    promoEl.innerHTML = `⚑ <b>Promotion series!</b> Held at 99% - win your next ranked match to reach <b>${pr.target_name}</b>. Lose it and you drop the normal amount.`;
    promoEl.hidden = false;
  } else if (pr.won) {
    promoEl.className = "promo-note won";
    promoEl.innerHTML = `▲ <b>Promo cleared!</b> You won the series and ranked up.`;
    promoEl.hidden = false;
  } else if (pr.lost) {
    promoEl.className = "promo-note lost";
    promoEl.innerHTML = `Promo lost - back to the climb. Get back to the top of the tier for another shot.`;
    promoEl.hidden = false;
  } else {
    promoEl.hidden = true;
  }

  if (isDuo) {
    renderDuoCompare(r);
  } else {
    const you = r.you, opp = r.opponent;
    $("result-compare").innerHTML = `
      <div class="cmp-row cmp-head"><span></span><span>You</span><span>${opp.username}${opp.is_bot ? " 👻" : ""}</span></div>
      <div class="cmp-row"><span>Result</span><span>${you.finished ? "Finished" : "DNF"}</span><span>${opp.finished ? "Finished" : "DNF"}</span></div>
      <div class="cmp-row"><span>Clicks</span><span>${you.clicks ?? "-"}</span><span>${opp.clicks ?? "-"}</span></div>
      <div class="cmp-row"><span>Time</span><span>${fmtTime(you.time_ms)}</span><span>${fmtTime(opp.time_ms)}</span></div>
      ${you.flagged ? `<div class="cmp-flag">⚠ Your run was flagged by anti-cheat (can't win).</div>` : ""}`;
  }

  showOnly("screen-result");
}

function celebrateRankUp(name, slug, promoWin, division) {
  $("rankup-badge").innerHTML = rankBadgeHTML({ slug, name, division });
  $("rankup-name").textContent = name;
  $("rankup-kicker").textContent = promoWin ? "Promotion won" : "Promoted";
  $("rankup-sub").innerHTML = promoWin
    ? `You cleared your promo series - keep climbing to <b>Ryval</b>.`
    : `You ranked up - keep climbing to <b>Ryval</b>.`;
  const ov = $("rankup-overlay");
  ov.hidden = false;
  // Re-trigger the entrance animation if it fires twice in a session.
  ov.classList.remove("show"); void ov.offsetWidth; ov.classList.add("show");
}
$("rankup-dismiss").addEventListener("click", () => { $("rankup-overlay").hidden = true; });
$("rankup-overlay").addEventListener("click", (e) => {
  if (e.target.id === "rankup-overlay") $("rankup-overlay").hidden = true;
});

$("result-again").addEventListener("click", () => {
  backToLobby();
  if (MATCH_FORMAT === "duo") startQueue($("duo-diff").value, "duo");
  else startQueue($("rk-diff").value);
});
$("result-home").addEventListener("click", backToLobby);

// ---------------------------------------------------------- share recap card
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function buildRecapCanvas(r) {
  const W = 600, H = 760, S = 2;            // 2x for crisp output
  const cv = document.createElement("canvas");
  cv.width = W * S; cv.height = H * S;
  const x = cv.getContext("2d");
  x.scale(S, S);
  const won = r.won, draw = r.draw, isDuo = r.team_kind === "duo";
  const orange = cssVar("--orange") || "#f0883e";
  const accent = cssVar("--accent") || "#6699ff";
  const ink = "#eaecf0", sub = "#9aa3af";
  const winC = "#37b24d", lossC = "#e8633a";
  const accentResult = draw ? sub : (won ? winC : lossC);

  // Background
  x.fillStyle = "#0f1419"; x.fillRect(0, 0, W, H);
  x.fillStyle = "#141a21"; roundRect(x, 24, 24, W - 48, H - 48, 22); x.fill();
  x.strokeStyle = accentResult; x.lineWidth = 2;
  roundRect(x, 24, 24, W - 48, H - 48, 22); x.stroke();

  // Wordmark
  x.textBaseline = "alphabetic";
  x.font = "800 30px system-ui, sans-serif";
  x.fillStyle = accent; x.fillText("Ry", 56, 84);
  const ryW = x.measureText("Ry").width;
  x.fillStyle = ink; x.fillText("vals", 56 + ryW, 84);
  x.font = "600 13px system-ui, sans-serif"; x.fillStyle = sub;
  x.fillText((isDuo ? "Ranked Duos" : (r.ranked ? "Ranked 1v1" : "Match")), 58, 104);

  // Result banner
  x.font = "900 64px system-ui, sans-serif"; x.fillStyle = accentResult;
  const banner = draw ? "DRAW" : (won ? (isDuo ? "TEAM WIN" : "VICTORY") : (isDuo ? "TEAM LOSS" : "DEFEAT"));
  x.fillText(banner, 56, 184);

  // Route
  x.font = "700 22px system-ui, sans-serif"; x.fillStyle = ink;
  wrapTwo(x, r.start, r.target, 56, 232, W - 112);

  // Stat compare box
  const boxY = 300, you = r.you || {}, opp = r.opponent || {};
  x.fillStyle = "#0f151c"; roundRect(x, 56, boxY, W - 112, 150, 14); x.fill();
  x.font = "700 14px system-ui, sans-serif"; x.fillStyle = sub;
  x.fillText("You", 240, boxY + 34);
  x.fillText(opp.username ? trunc(opp.username, 12) + (opp.is_bot ? " 👻" : "") : "Ryval", 410, boxY + 34);
  const rows = [
    ["Time", fmtTime(you.time_ms), fmtTime(opp.time_ms)],
    ["Clicks", String(you.clicks ?? "-"), String(opp.clicks ?? "-")],
    ["Result", you.finished ? "Finished" : "DNF", opp.finished ? "Finished" : "DNF"],
  ];
  rows.forEach((rw, i) => {
    const yy = boxY + 70 + i * 28;
    x.fillStyle = sub; x.font = "600 14px system-ui, sans-serif"; x.fillText(rw[0], 80, yy);
    x.fillStyle = ink; x.font = "700 15px system-ui, sans-serif";
    x.fillText(rw[1], 240, yy); x.fillText(rw[2], 410, yy);
  });

  // EP delta + rank
  const d = (r.rp && r.rp.delta) || 0;
  x.font = "900 40px system-ui, sans-serif";
  x.fillStyle = d > 0 ? winC : (d < 0 ? lossC : sub);
  x.fillText(`${d > 0 ? "+" : ""}${d} EP`, 56, boxY + 230);
  const rk = r.rank;
  if (rk && rk.after && !rk.hidden) {
    x.font = "700 18px system-ui, sans-serif"; x.fillStyle = orange;
    const label = rk.after + (rk.promoted ? "  ▲ promoted" : "");
    x.fillText(label, 56, boxY + 262);
  }

  // Flag callout
  if (you.flagged) {
    x.fillStyle = lossC; x.font = "600 13px system-ui, sans-serif";
    x.fillText("⚠ Flagged by anti-cheat - didn't count as a clean finish.", 56, boxY + 296);
  }

  // Footer
  x.fillStyle = sub; x.font = "600 13px system-ui, sans-serif";
  x.fillText(`${(r.difficulty || "any")} · par ${r.par ?? "-"}`, 56, H - 48);
  x.textAlign = "right"; x.fillText("wikiryvals - find your Ryval", W - 56, H - 48); x.textAlign = "left";
  return cv;
}

function roundRect(x, a, b, w, h, r) {
  x.beginPath();
  x.moveTo(a + r, b); x.arcTo(a + w, b, a + w, b + h, r);
  x.arcTo(a + w, b + h, a, b + h, r); x.arcTo(a, b + h, a, b, r);
  x.arcTo(a, b, a + w, b, r); x.closePath();
}
function trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function wrapTwo(x, start, target, px, py, maxW) {
  x.fillStyle = "#9aa3af"; x.font = "600 14px system-ui, sans-serif"; x.fillText("FROM", px, py - 22);
  x.fillStyle = "#eaecf0"; x.font = "700 22px system-ui, sans-serif"; x.fillText(trunc(start, 26), px, py);
  x.fillStyle = "#9aa3af"; x.font = "600 14px system-ui, sans-serif"; x.fillText("TO", px, py + 22);
  x.fillStyle = "#eaecf0"; x.font = "700 22px system-ui, sans-serif"; x.fillText(trunc(target, 26), px, py + 44);
}

$("result-share").addEventListener("click", () => {
  if (!LAST_RESULT) return;
  const cv = buildRecapCanvas(LAST_RESULT);
  $("share-img").src = cv.toDataURL("image/png");
  $("share-preview").dataset.canvas = "1";
  $("share-preview").hidden = false;
  $("share-hint").textContent = "";
  $("share-preview")._canvas = cv;
});
$("share-download").addEventListener("click", () => {
  const cv = $("share-preview")._canvas; if (!cv) return;
  const a = document.createElement("a");
  a.href = cv.toDataURL("image/png"); a.download = "wikiryvals-result.png"; a.click();
});
$("share-copy").addEventListener("click", async () => {
  const cv = $("share-preview")._canvas; if (!cv) return;
  try {
    const blob = await new Promise((res) => cv.toBlob(res, "image/png"));
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    $("share-hint").textContent = "Copied to clipboard ✓";
  } catch (_) { $("share-hint").textContent = "Couldn't copy - use Download instead."; }
});

function backToLobby() {
  clearPolls();
  closeMatchSocket();
  CURRENT_MATCH = null;
  bg("clearRace");
  showApp();
  showView("play");
  refreshMe();  // pick up RP / promo-state changes from the match just played
}

// ================================================================ SOLO / CASUAL

function hint(msg, kind) {
  const h = $("play-hint");
  h.textContent = msg || "";
  h.className = "hint" + (kind ? " " + kind : "");
}
async function startSolo(opts) {
  hint("Starting race…");
  try {
    let race = null;
    const resp = await bg("newRace", Object.assign({ newTab: true }, opts));
    if (resp && resp.ok && resp.race) race = resp.race;
    else race = await startRaceDirect(opts.difficulty, opts.start, opts.target);
    if (race && race.race_id) hint(`Race started: ${race.start} → ${race.target}. Opened in a new tab, go race!`, "ok");
    else hint("Couldn't start a race. The backend may be unavailable.", "err");
  } catch (e) {
    hint("Couldn't start a race. The backend may be unavailable.", "err");
  }
}
$("qm-go").addEventListener("click", () => startSolo({ difficulty: $("qm-diff").value }));
$("daily-go").addEventListener("click", startDaily);
$("streak-nudge-go").addEventListener("click", () => { showView("play"); startDaily(); });
$("daily-board-toggle").addEventListener("click", toggleDailyBoard);
$("weekly-go").addEventListener("click", startWeekly);
$("weekly-board-toggle").addEventListener("click", toggleWeeklyBoard);
$("rival-rematch").addEventListener("click", () => startQueue($("rk-diff").value));

// ---------------------------------------------------------------- ryval
function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

async function loadRival() {
  const card = $("rival-card");
  try {
    const r = (await api("/api/ext/rival", { auth: true })).rival;
    if (!r) { card.hidden = true; return; }
    $("rival-avatar").textContent = initials(r.username);
    $("rival-name").textContent = r.username;
    const rank = (r.rank && !r.in_placements) ? `${rankBadgeHTML(r.rank)} · ` : "";
    const ago = r.last_played ? ` · last met ${fmtAgo(r.last_played)}` : "";
    $("rival-rec").innerHTML = `${rank}<b>${r.wins}\u2013${r.losses}</b> vs you${ago}`;
    card.hidden = false;
  } catch (_) { card.hidden = true; }
}

// ---------------------------------------------------------------- daily
async function refreshDaily() {
  try {
    const d = await api("/api/ext/daily" + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""));
    $("daily-route").textContent = `${d.start} → ${d.target}`;
    $("daily-board-toggle").hidden = false;
    const res = d.your_result;
    if (res) {
      const done = res.finished
        ? `${fmtTime(res.time_ms)} · ${res.clicks} clicks${res.flagged ? " (flagged)" : ""}`
        : "did not finish";
      $("daily-go").textContent = "Play again (practice)";
      hint(`Today's daily is locked in: ${done}. Replays are practice only.`, "ok");
    } else {
      $("daily-go").textContent = "Play today's";
    }
  } catch (e) {
    $("daily-route").textContent = "Couldn't load today's daily.";
  }
}

async function startDaily() {
  if (!ME) { showScreenAuth(); return; }
  hint("Starting today's daily…");
  try {
    const resp = await bg("dailyRace", { token: TOKEN, newTab: true });
    if (resp && resp.ok && resp.race) {
      hint(`Daily: ${resp.race.start} → ${resp.race.target}. Opened in a new tab, go race!`, "ok");
    } else {
      hint("Couldn't start the daily. Is the backend running?", "err");
    }
  } catch (e) {
    hint("Couldn't start the daily. Is the backend running?", "err");
  }
}

async function toggleDailyBoard() {
  const el = $("daily-board");
  if (!el.hidden) { el.hidden = true; $("daily-board-toggle").textContent = "Today's board"; return; }
  try {
    const d = await api("/api/ext/daily/board");
    el.innerHTML = "";
    if (!d.board.length) {
      el.innerHTML = `<li class="daily-empty">No finishers yet today, be the first.</li>`;
    } else {
      for (const r of d.board) {
        const li = document.createElement("li");
        const stat = r.finished ? `${fmtTime(r.time_ms)} · ${r.clicks}` : "DNF";
        const me = ME && r.username === ME.username ? " is-me" : "";
        li.className = "daily-row" + me;
        li.innerHTML = `<span class="dr-pos">${r.position}</span>` +
                       `<span class="dr-name">${r.username || "-"}</span>` +
                       `<span class="dr-stat">${stat}${r.flagged ? " ⚑" : ""}</span>`;
        el.appendChild(li);
      }
    }
    el.hidden = false;
    $("daily-board-toggle").textContent = "Hide board";
  } catch (e) { hint("Couldn't load the daily board.", "err"); }
}
// ---------------------------------------------------------------- weekly puzzle
async function refreshWeekly() {
  try {
    const d = await api("/api/ext/weekly" + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""));
    const par = d.par ? ` · par ${d.par}` : "";
    $("weekly-route").textContent = `${d.start} → ${d.target}${par}`;
    $("weekly-board-toggle").hidden = false;
    const res = d.your_result;
    if (res) {
      const done = res.finished
        ? `${fmtTime(res.time_ms)} · ${res.clicks} clicks${res.flagged ? " (flagged)" : ""}`
        : "did not finish";
      $("weekly-go").textContent = "Play again (practice)";
      hint(`This week's puzzle is locked in: ${done}. Replays are practice only.`, "ok");
    } else {
      $("weekly-go").textContent = "Play this week's";
    }
  } catch (e) {
    $("weekly-route").textContent = "Couldn't load this week's puzzle.";
  }
}

async function startWeekly() {
  if (!ME) { showScreenAuth(); return; }
  hint("Starting this week's puzzle…");
  try {
    const resp = await bg("weeklyRace", { token: TOKEN, newTab: true });
    if (resp && resp.ok && resp.race) {
      hint(`Weekly: ${resp.race.start} → ${resp.race.target}. Opened in a new tab, go race!`, "ok");
    } else {
      hint("Couldn't start the weekly puzzle. Is the backend running?", "err");
    }
  } catch (e) {
    hint("Couldn't start the weekly puzzle. Is the backend running?", "err");
  }
}

async function toggleWeeklyBoard() {
  const el = $("weekly-board");
  if (!el.hidden) { el.hidden = true; $("weekly-board-toggle").textContent = "This week's board"; return; }
  try {
    const d = await api("/api/ext/weekly/board");
    el.innerHTML = "";
    if (!d.board.length) {
      el.innerHTML = `<li class="daily-empty">No finishers yet this week, be the first.</li>`;
    } else {
      for (const r of d.board) {
        const li = document.createElement("li");
        const stat = r.finished ? `${fmtTime(r.time_ms)} · ${r.clicks}` : "DNF";
        const me = ME && r.username === ME.username ? " is-me" : "";
        li.className = "daily-row" + me;
        li.innerHTML = `<span class="dr-pos">${r.position}</span>` +
                       `<span class="dr-name">${r.username || "-"}</span>` +
                       `<span class="dr-stat">${stat}${r.flagged ? " ⚑" : ""}</span>`;
        el.appendChild(li);
      }
    }
    el.hidden = false;
    $("weekly-board-toggle").textContent = "Hide board";
  } catch (e) { hint("Couldn't load the weekly board.", "err"); }
}

$("c-go").addEventListener("click", () => {
  const start = $("c-start").value.trim(), target = $("c-target").value.trim();
  if (!start || !target) { hint("Enter both a start and a target article.", "err"); return; }
  startSolo({ difficulty: "custom", start, target });
});

// ================================================================ PRIVATE LOBBIES

function roomsHint(msg, kind) {
  const h = $("rooms-hint"); h.textContent = msg || ""; h.className = "hint" + (kind ? " " + kind : "");
}

$("lobby-create").addEventListener("click", async () => {
  try {
    const r = await api("/api/ext/lobby/create", { method: "POST", auth: true, body: { difficulty: $("lobby-diff").value } });
    $("lobby-code-big").textContent = r.code;
    $("lobby-wait").hidden = false;
    roomsHint("");
    poll.lobby = setInterval(async () => {
      try {
        const p = await api(`/api/ext/lobby/poll?code=${r.code}`, { auth: true });
        if (p.status === "started" && p.match) {
          clearInterval(poll.lobby); poll.lobby = null;
          $("lobby-wait").hidden = true;
          beginMatch(p.match, "private");
        }
      } catch (_) {}
    }, 1500);
  } catch (e) { roomsHint(e.message, "err"); }
});

$("lobby-cancel").addEventListener("click", () => {
  if (poll.lobby) { clearInterval(poll.lobby); poll.lobby = null; }
  $("lobby-wait").hidden = true;
});

$("lobby-join").addEventListener("click", async () => {
  const code = $("lobby-code").value.trim().toUpperCase();
  if (code.length !== 6) { roomsHint("Codes are 6 characters.", "err"); return; }
  try {
    const r = await api("/api/ext/lobby/join", { method: "POST", auth: true, body: { code } });
    if (r.match) beginMatch(r.match, "private");
    else roomsHint("Joined. Waiting for host to start…");
  } catch (e) { roomsHint(e.message, "err"); }
});

// ================================================================ LEADERBOARD

async function loadLeaderboard() {
  const list = $("lb-list");
  try {
    const r = await api("/api/ext/leaderboard?limit=50");
    if (!r.entries.length) { list.className = "list is-empty"; list.innerHTML = `<p class="preview-tag">No ranked players yet. Be the first, queue up!</p>`; return; }
    list.className = "list";
    list.innerHTML = r.entries.map((e) => {
      const mine = ME && e.id === ME.id;
      const rk = e.rank || {};
      const placing = e.in_placements;
      const tier = placing ? `<span class="tier tier-iron">Placements</span>` : rankBadgeHTML(rk);
      return `<div class="li${mine ? " me-highlight" : ""}"><span class="rank">${e.position}</span>
        <div class="li-main"><b>${e.username}${mine ? ' <span class="you">you</span>' : ""}</b>
        <span class="li-sub">${tier} · ${e.rp} EP · ${Math.round(e.rating)}</span></div>
        <span class="li-end">${e.wins}W ${e.losses}L</span></div>`;
    }).join("");
  } catch (e) { list.className = "list is-empty"; list.innerHTML = `<p class="preview-tag">Couldn't load leaderboard.</p>`; }
}

// ================================================================ HISTORY

async function loadHistory() {
  const list = $("hist-list");
  try {
    const r = await api("/api/ext/history?limit=25", { auth: true });
    if (!r.matches.length) { list.className = "list is-empty"; list.innerHTML = `<p class="preview-tag">No matches yet. Play a game!</p>`; return; }
    list.className = "list";
    list.innerHTML = r.matches.map((m) => {
      const res = m.result === "win" ? `<span class="win">Win</span>` : (m.result === "draw" ? `<span class="flag">Draw</span>` : (m.result === "loss" && m.time_ms ? `<span class="loss">Loss</span>` : `<span class="loss">DNF</span>`));
      const rp = m.mode === "ranked" ? ` · <span class="${m.rp_delta >= 0 ? "win" : "loss"}">${m.rp_delta >= 0 ? "+" : ""}${m.rp_delta} EP</span>` : ` · <span class="li-sub">private</span>`;
      const vs = m.opponent ? ` vs ${m.opponent}${m.opponent_bot ? " 👻" : ""}` : "";
      return `<div class="li"><div class="li-main"><b>${m.start} → ${m.target}</b>
        <span class="li-sub">${res}${rp} · ${m.clicks ?? "-"} clicks · ${fmtTime(m.time_ms)}${m.par ? " · par " + m.par : ""}${vs}</span></div></div>`;
    }).join("");
  } catch (e) { list.className = "list is-empty"; list.innerHTML = `<p class="preview-tag">Couldn't load history.</p>`; }
}

// ================================================================ PROFILE

// Horizontal tier carousel: centers the current tier and pages via arrows.
function setupTierCarousel(track, curIdx) {
  const prev = $("pf-ladder-prev"), next = $("pf-ladder-next");
  const centerOn = (idx) => {
    const card = track.children[idx];
    if (!card) return;
    track.scrollLeft = card.offsetLeft - (track.clientWidth - card.clientWidth) / 2;
  };
  const step = (dir) => {
    const cw = track.children[0] ? track.children[0].offsetWidth + 8 : 86;
    track.scrollBy({ left: dir * cw, behavior: "smooth" });
  };
  const updateArrows = () => {
    const max = track.scrollWidth - track.clientWidth - 1;
    prev.disabled = track.scrollLeft <= 1;
    next.disabled = track.scrollLeft >= max;
  };
  prev.onclick = () => step(-1);
  next.onclick = () => step(1);
  track.onscroll = updateArrows;
  requestAnimationFrame(() => { centerOn(curIdx); updateArrows(); });
}

async function loadProfile() {
  if (!ME) return;
  await refreshMe();
  $("pf-avatar").textContent = initials(ME.username);
  $("pf-name").innerHTML = (ME.username || "player") + cbadgesHTML(ME.tags);
  $("admin-card").hidden = !ME.is_admin;
  const placing = ME.in_placements;
  $("pf-rank").innerHTML = placing
    ? `<span class="tier tier-iron">Placements</span> · ${ME.placements_left} left · ${ME.rp} EP`
    : `${rankBadgeHTML(ME.rank)} · <b>${ME.rp}</b> EP · ${Math.round(ME.rating)} <span class="rd">± ${Math.round(ME.rd)}</span>`;

  const winRate = ME.games ? Math.round((ME.wins / ME.games) * 100) : 0;
  $("pf-stats").innerHTML = `
    <div class="stat"><div class="stat-n">${ME.games}</div><div class="stat-l">Races</div></div>
    <div class="stat"><div class="stat-n">${winRate}%</div><div class="stat-l">Win rate</div></div>
    <div class="stat"><div class="stat-n">${ME.streak >= 0 ? "▲" : "▼"}${Math.abs(ME.streak)}</div><div class="stat-l">Streak</div></div>
    <div class="stat"><div class="stat-n">${fmtTime(ME.best_time_ms)}</div><div class="stat-l">Best</div></div>
    <div class="stat"><div class="stat-n">🔥${(ME.daily && ME.daily.streak) || 0}</div><div class="stat-l">Daily streak</div></div>
    <div class="stat"><div class="stat-n">${ME.wins}</div><div class="stat-l">Wins</div></div>`;

  const curSlug = (ME.rank && ME.rank.slug && ME.rank.slug !== "unranked") ? ME.rank.slug : "iron";
  const curIdx = TIER_SLUGS.indexOf(curSlug);
  const track = $("pf-ladder");
  track.innerHTML = TIER_SLUGS.map((s, i) =>
    `<div class="tc-card tier-${s}${i === curIdx ? " on" : ""}" title="${TIER_LABELS[i]}"><img class="tc-crest" src="${iconFor(s)}" alt="" /><span class="tc-name">${TIER_LABELS[i]}</span></div>`
  ).join("");
  setupTierCarousel(track, curIdx);

  const rk = ME.rank || {};
  const pct = rk.rp_span ? Math.max(0, Math.min(100, (rk.rp_into / rk.rp_span) * 100)) : 0;
  $("pf-progress").style.width = pct + "%";
  $("pf-progress-label").innerHTML = placing
    ? `Finish ${ME.placements_left} placement match${ME.placements_left === 1 ? "" : "es"} to get ranked.`
    : (rk.next_name ? `<b>${rk.rp_to_next}</b> EP to <b>${rk.next_name}</b>.` : `You're at the apex. Find your Ryval.`);
}

// ================================================================ BOOT

(async () => {
  const el = $("backend-dot"), label = $("backend-label");
  try {
    const res = await fetch(`${BACKEND}/api/ext/health`);
    if (res.ok) { el.classList.add("ok"); label.textContent = "backend online"; }
    else throw new Error(String(res.status));
  } catch (e) { el.classList.add("bad"); label.textContent = "backend offline"; }

  // The season endpoint is public, so populate the chip even on the signed-out
  // screen (it stays hidden if the fetch fails - no "Season" placeholder).
  refreshSeason();

  try {
    const got = await chrome.storage.local.get([TOKEN_KEY]);
    TOKEN = got && got[TOKEN_KEY];
  } catch (_) {}

  if (TOKEN) {
    try { ME = (await api("/api/ext/me", { auth: true })).user; enterApp(); return; }
    catch (_) { TOKEN = null; try { await chrome.storage.local.remove(TOKEN_KEY); } catch (_) {} }
  }
  authStep("auth-email-step");
  showScreenAuth();
})();
