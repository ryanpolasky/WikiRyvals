"use strict";

// Runs on every live Wikipedia article. Reports the page the player landed on to
// the backend (which validates the hop), renders the race HUD, and applies the
// in-page anti-cheat (kill Ctrl+F + the search box during a race).

function currentTitle() {
  const m = location.pathname.match(/^\/wiki\/(.+)$/);
  if (!m) return null;
  let t = decodeURIComponent(m[1].split("#")[0]);
  t = t.replace(/_/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : null;
}

// How this page was reached (Performance Navigation Timing): "navigate" | "reload"
// | "back_forward". Lets the backend flag the browser back/forward button - a
// classic Wikirace cheat - since those aren't clicks on an on-page link.
function navType() {
  try {
    const nav = performance.getEntriesByType("navigation")[0];
    if (nav && nav.type) return nav.type;
    if (performance.navigation) {
      const t = performance.navigation.type;
      return t === 2 ? "back_forward" : t === 1 ? "reload" : "navigate";
    }
  } catch (_) {}
  return "navigate";
}

// Namespaces that aren't real article pages (mirrors wiki.py on the backend).
const RWR_NON_ARTICLE = new Set([
  "file", "image", "category", "help", "wikipedia", "template", "template_talk",
  "special", "portal", "draft", "module", "mediawiki", "book", "talk", "user",
  "user_talk", "wikt", "s", "q", "commons",
]);

// Anchors that are NOT real article navigation: citations/footnotes, "[edit]"
// pencils, and message-box boilerplate. We intentionally KEEP navboxes and
// hatnotes - on live Wikipedia those are real, clickable article links a racer
// can legally use, so excluding them made the anti-cheat flag perfectly legal
// hops ("that link wasn't on the page"). This set is the basis the backend
// validates moves against, so it must match what the player can actually click.
const RWR_SKIP_SEL = [
  ".metadata", ".mw-references", ".reflist", ".reference", ".noprint",
  ".mw-editsection", ".ambox", ".sistersitebox", ".side-box",
  ".mw-empty-elt", "sup",
].join(",");

function hrefToTitle(href) {
  if (!href || href.startsWith("#")) return null;
  let path = href;
  if (/^https?:\/\//i.test(href) || href.startsWith("//")) {
    let u;
    try { u = new URL(href, location.href); } catch (e) { return null; }
    if (!/(^|\.)wikipedia\.org$/i.test(u.hostname)) return null;
    path = u.pathname;
  }
  const m = path.match(/^\/wiki\/(.+)$/);
  if (!m) return null;
  let t = decodeURIComponent(m[1].split("#")[0].split("?")[0]);
  t = t.replace(/_/g, " ").trim();
  if (!t) return null;
  if (t.includes(":")) {
    const prefix = t.split(":", 1)[0].trim().toLowerCase();
    if (RWR_NON_ARTICLE.has(prefix)) return null;
  }
  if (t.toLowerCase() === "main page") return null;
  return t[0].toUpperCase() + t.slice(1);
}

// Read the article's real link set straight from the live DOM, so the backend
// can build its graph + spot "missed win" pages without ever calling Wikipedia.
function collectLinks() {
  const root = document.querySelector(".mw-parser-output");
  if (!root) return [];
  const out = [];
  const seen = new Set();
  root.querySelectorAll("a[href]").forEach((a) => {
    if (a.closest(RWR_SKIP_SEL)) return;
    const t = hrefToTitle(a.getAttribute("href") || "");
    if (t && !seen.has(t)) { seen.add(t); out.push(t); }
  });
  return out;
}

// One-shot read of the link the player last clicked (set by the capture-phase
// listener in init). Consumed immediately and freshness-gated so a stale click
// can never be replayed to launder a later hop.
function readVia() {
  try {
    const raw = sessionStorage.getItem("rwr_via");
    sessionStorage.removeItem("rwr_via");
    if (raw) {
      const v = JSON.parse(raw);
      if (v && typeof v.to === "string" && Date.now() - (v.at || 0) < 15000) {
        return { to: v.to, from: v.from || null };
      }
    }
  } catch (_) {}
  return { to: null, from: null };
}

function send(type, extra) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(Object.assign({ type }, extra || {}), (resp) => {
      resolve(resp || { ok: false });
    });
  });
}

// ---- Theme (light / dark) -------------------------------------------------
// Persisted in chrome.storage.local under "rwr_theme" ("light" | "dark"); unset
// means follow the OS preference. Shared with the lobby + popup so the choice is
// consistent everywhere.
const RWR_THEME_KEY = "rwr_theme";
let rwrTheme = null; // resolved "light" | "dark"

function systemTheme() {
  return window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyTheme(theme) {
  rwrTheme = theme === "light" ? "light" : "dark";
  document.documentElement.classList.toggle("rwr-light", rwrTheme === "light");
  applyWikipediaTheme(rwrTheme);
  syncThemeButton();
}
// Mirror our light/dark choice onto Wikipedia's own Vector "Appearance" colour
// mode so the article matches the panel. Vector stores the preference as a
// `skin-theme-clientpref-{day,night,os}` class on <html>, and the matching
// colour CSS ships in the already-loaded Vector stylesheet - so swapping the
// class recolours the page even from our isolated content world (where the
// page's mw.user.clientPrefs API isn't reachable). We re-apply on every article
// load via loadTheme(), so the choice persists across navigation without having
// to write Wikipedia's own cookie.
function applyWikipediaTheme(theme) {
  const de = document.documentElement;
  if (!/skin-theme-clientpref-/.test(de.className)) return; // not a themed Vector page
  const want = theme === "light" ? "day" : "night";
  ["day", "night", "os"].forEach((v) =>
    de.classList.toggle("skin-theme-clientpref-" + v, v === want)
  );
}
function syncThemeButton() {
  const b = document.getElementById("rwr-theme");
  if (b) b.checked = rwrTheme === "dark"; // checkbox: checked = dark/night
}
function loadTheme() {
  try {
    chrome.storage.local.get([RWR_THEME_KEY], (res) => {
      applyTheme((res && res[RWR_THEME_KEY]) || systemTheme());
    });
  } catch (_) {
    applyTheme(systemTheme());
  }
}
function toggleTheme() {
  const b = document.getElementById("rwr-theme");
  const next = b && b.checked ? "dark" : "light"; // checked = dark/night
  applyTheme(next);
  try { chrome.storage.local.set({ [RWR_THEME_KEY]: next }); } catch (_) {}
}
// React to changes made in the lobby/popup (or another tab) live.
try {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[RWR_THEME_KEY]) {
      applyTheme(changes[RWR_THEME_KEY].newValue || systemTheme());
    }
  });
} catch (_) {}

let tick = null;
let timeBase = { elapsed: 0, at: 0, running: false };
// Synchronous mirror of "is a race live", kept current by updateHud(). The Ctrl+F
// handler needs this because it must preventDefault() *synchronously* - awaiting
// the background first lets the browser open the find bar before we can block it.
let raceActive = false;

// --- "Stuck?" hint --------------------------------------------------------
// If a race runs long (HINT_AFTER_MS), surface the target article's one-line
// Wikipedia short description as a gentle nudge. currentRace mirrors the live
// race for the timer tick; the content script reloads on every hop, so "already
// shown / dismissed" is persisted in sessionStorage (keyed by race) rather than
// these per-page-load guards.
const HINT_AFTER_MS = 180000; // 3 minutes
let currentRace = null;
let hintBusy = false;  // a summary fetch is in flight
let hintDone = false;  // shown or decided for this page load

// Auto-forfeit when the player leaves the race tab during a match (see the
// visibilitychange handler in init). The grace absorbs an accidental tab flick.
const LEAVE_GRACE_MS = 1200;
let leaveForfeitTimer = null;
let raceForfeited = false;

// Presence heartbeat: while a MATCH race is live, ping the server every
// HEARTBEAT_MS so it can detect a closed/crashed tab and force-forfeit it (the
// server-side backstop for tab-close, where visibilitychange can't fire in time).
const HEARTBEAT_MS = 5000;
let heartbeatTimer = null;
let heartbeatMatchId = null;

function fmt(ms) {
  return (ms / 1000).toFixed(1) + "s";
}

// Inject the namespaced SVG filter defs the theme switch references (filter:
// url(#rwr-sketchy*)). They must live in the page DOM; injecting once is enough.
function ensureSketchyDefs() {
  if (document.getElementById("rwr-sketchy-defs")) return;
  const wrap = document.createElement("div");
  wrap.innerHTML =
    '<svg id="rwr-sketchy-defs" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden">' +
    '<defs>' +
    '<filter id="rwr-sketchy" x="-10%" y="-10%" width="120%" height="120%">' +
    '<feTurbulence type="turbulence" baseFrequency="0.035 0.042" numOctaves="4" result="noise" seed="42"></feTurbulence>' +
    '<feDisplacementMap in="SourceGraphic" in2="noise" scale="4.5" xChannelSelector="R" yChannelSelector="G"></feDisplacementMap>' +
    '</filter>' +
    '<filter id="rwr-sketchy-sm" x="-18%" y="-18%" width="136%" height="136%">' +
    '<feTurbulence type="turbulence" baseFrequency="0.06" numOctaves="3" result="noise" seed="7"></feTurbulence>' +
    '<feDisplacementMap in="SourceGraphic" in2="noise" scale="2.5" xChannelSelector="R" yChannelSelector="G"></feDisplacementMap>' +
    '</filter>' +
    '</defs></svg>';
  const svg = wrap.firstElementChild;
  if (svg) document.documentElement.appendChild(svg);
}

function ensureHud() {
  let hud = document.getElementById("rwr-hud");
  if (hud) return hud;
  hud = document.createElement("div");
  hud.id = "rwr-hud";
  // Start collapsed; updateHud() expands the bar once a race is active so the
  // goal/stats never flash before the first getRace resolves.
  hud.classList.add("rwr-idle");
  hud.innerHTML = `
    <div class="rwr-brand">Wiki<span class="rwr-ry">Ry</span>vals</div>
    <div class="rwr-goal"><span class="rwr-chip rwr-start" id="rwr-start">-</span>
      <span class="rwr-arrow">→</span>
      <span class="rwr-chip rwr-target" id="rwr-target">-</span></div>
    <div class="rwr-stats">
      <span><b id="rwr-clicks">0</b> clicks</span>
      <span><b id="rwr-timer">0.0s</b></span>
      <span id="rwr-par-wrap">par <b id="rwr-par">-</b></span>
    </div>
    <div class="rwr-actions">
      <label class="theme-switch" id="rwr-theme-switch" title="Toggle light / dark">
        <input class="theme-switch__checkbox" id="rwr-theme" type="checkbox" aria-label="Toggle theme" />
        <div class="theme-switch__container">
          <div class="theme-switch__clouds"></div>
          <div class="theme-switch__stars-container">
            <svg fill="none" viewBox="0 0 144 55" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M135.831 3.00688C135.055 3.85027 134.111 4.29946 133 4.35447C134.111 4.40947 135.055 4.85867 135.831 5.71123C136.607 6.55462 136.996 7.56303 136.996 8.72727C136.996 7.95722 137.172 7.25134 137.525 6.59129C137.886 5.93124 138.372 5.39954 138.98 5.00535C139.598 4.60199 140.268 4.39114 141 4.35447C139.88 4.2903 138.936 3.85027 138.16 3.00688C137.384 2.16348 136.996 1.16425 136.996 0C136.996 1.16425 136.607 2.16348 135.831 3.00688ZM31 23.3545C32.1114 23.2995 33.0551 22.8503 33.8313 22.0069C34.6075 21.1635 34.9956 20.1642 34.9956 19C34.9956 20.1642 35.3837 21.1635 36.1599 22.0069C36.9361 22.8503 37.8798 23.2903 39 23.3545C38.2679 23.3911 37.5976 23.602 36.9802 24.0053C36.3716 24.3995 35.8864 24.9312 35.5248 25.5913C35.172 26.2513 34.9956 26.9572 34.9956 27.7273C34.9956 26.563 34.6075 25.5546 33.8313 24.7112C33.0551 23.8587 32.1114 23.4095 31 23.3545ZM0 36.3545C1.11136 36.2995 2.05513 35.8503 2.83131 35.0069C3.6075 34.1635 3.99559 33.1642 3.99559 32C3.99559 33.1642 4.38368 34.1635 5.15987 35.0069C5.93605 35.8503 6.87982 36.2903 8 36.3545C7.26792 36.3911 6.59757 36.602 5.98015 37.0053C5.37155 37.3995 4.88644 37.9312 4.52481 38.5913C4.172 39.2513 3.99559 39.9572 3.99559 40.7273C3.99559 39.563 3.6075 38.5546 2.83131 37.7112C2.05513 36.8587 1.11136 36.4095 0 36.3545ZM56.8313 24.0069C56.0551 24.8503 55.1114 25.2995 54 25.3545C55.1114 25.4095 56.0551 25.8587 56.8313 26.7112C57.6075 27.5546 57.9956 28.563 57.9956 29.7273C57.9956 28.9572 58.172 28.2513 58.5248 27.5913C58.8864 26.9312 59.3716 26.3995 59.9802 26.0053C60.5976 25.602 61.2679 25.3911 62 25.3545C60.8798 25.2903 59.9361 24.8503 59.1599 24.0069C58.3837 23.1635 57.9956 22.1642 57.9956 21C57.9956 22.1642 57.6075 23.1635 56.8313 24.0069ZM81 25.3545C82.1114 25.2995 83.0551 24.8503 83.8313 24.0069C84.6075 23.1635 84.9956 22.1642 84.9956 21C84.9956 22.1642 85.3837 23.1635 86.1599 24.0069C86.9361 24.8503 87.8798 25.2903 89 25.3545C88.2679 25.3911 87.5976 25.602 86.9802 26.0053C86.3716 26.3995 85.8864 26.9312 85.5248 27.5913C85.172 28.2513 84.9956 28.9572 84.9956 29.7273C84.9956 28.563 84.6075 27.5546 83.8313 26.7112C83.0551 25.8587 82.1114 25.4095 81 25.3545ZM136 36.3545C137.111 36.2995 138.055 35.8503 138.831 35.0069C139.607 34.1635 139.996 33.1642 139.996 32C139.996 33.1642 140.384 34.1635 141.16 35.0069C141.936 35.8503 142.88 36.2903 144 36.3545C143.268 36.3911 142.598 36.602 141.98 37.0053C141.372 37.3995 140.886 37.9312 140.525 38.5913C140.172 39.2513 139.996 39.9572 139.996 40.7273C139.996 39.563 139.607 38.5546 138.831 37.7112C138.055 36.8587 137.111 36.4095 136 36.3545ZM101.831 49.0069C101.055 49.8503 100.111 50.2995 99 50.3545C100.111 50.4095 101.055 50.8587 101.831 51.7112C102.607 52.5546 102.996 53.563 102.996 54.7273C102.996 53.9572 103.172 53.2513 103.525 52.5913C103.886 51.9312 104.372 51.3995 104.98 51.0053C105.598 50.602 106.268 50.3911 107 50.3545C105.88 50.2903 104.936 49.8503 104.16 49.0069C103.384 48.1635 102.996 47.1642 102.996 46C102.996 47.1642 102.607 48.1635 101.831 49.0069Z" clip-rule="evenodd" fill-rule="evenodd"></path></svg>
          </div>
          <div class="theme-switch__circle-container">
            <div class="theme-switch__sun-moon-container">
              <div class="theme-switch__moon">
                <div class="theme-switch__spot"></div>
                <div class="theme-switch__spot"></div>
                <div class="theme-switch__spot"></div>
              </div>
            </div>
          </div>
          <div class="theme-switch__shooting-star"></div>
          <div class="theme-switch__shooting-star-2"></div>
          <div class="theme-switch__meteor"></div>
          <div class="theme-switch__stars-cluster">
            <div class="star"></div>
            <div class="star"></div>
            <div class="star"></div>
            <div class="star"></div>
            <div class="star"></div>
          </div>
          <div class="theme-switch__aurora"></div>
          <div class="theme-switch__comets">
            <div class="comet"></div>
            <div class="comet"></div>
          </div>
        </div>
      </label>
      <button id="rwr-results" class="rwr-hidden" title="Show your last result">Results</button>
      <select id="rwr-diff" title="Difficulty">
        <option value="any">Any</option><option value="easy">Easy</option>
        <option value="medium">Medium</option><option value="hard">Hard</option>
      </select>
      <button id="rwr-new">New race</button>
    </div>
    <div id="rwr-flash" class="rwr-flash rwr-hidden"></div>`;
  ensureSketchyDefs();
  document.documentElement.appendChild(hud);
  hud.querySelector("#rwr-new").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const action = btn.dataset.action || "new";
    if (action === "forfeit") {
      // Ranked/duo: forfeiting counts as a loss, so confirm first.
      const matchId = btn.dataset.matchId;
      if (!matchId) return;
      if (!confirm("Forfeit this match? It counts as a loss.")) return;
      btn.disabled = true;
      const r = await send("forfeitMatch", { match_id: matchId });
      btn.disabled = false;
      if (r && r.ok) {
        await send("clearRace");
        updateHud(null);
        flash("You forfeited the match.");
      } else {
        flash("Couldn't forfeit - use the Forfeit button in the side panel.");
      }
    } else if (action === "end") {
      // Solo / daily / weekly: no opponent, so just abandon the current run.
      await send("clearRace");
      updateHud(null);
      flash("Race ended.");
    } else {
      const difficulty = hud.querySelector("#rwr-diff").value;
      await send("newRace", { difficulty });
    }
  });
  hud.querySelector("#rwr-theme").addEventListener("change", toggleTheme);
  // Re-open the results modal after it's been dismissed: the finished race stays
  // in storage until the next one starts, so re-fetch it and re-render the card.
  hud.querySelector("#rwr-results").addEventListener("click", async () => {
    const r = await send("getRace");
    if (r.ok && r.race && r.race.finished) renderWin(r.race);
  });
  syncThemeButton();
  return hud;
}

// A small "pull tab" pinned to the right edge of every Wikipedia article that
// opens the WikiRyvals side panel - so you don't have to hunt for the toolbar
// icon. A content script can't open the panel itself (no sidePanel API, and the
// open() call needs a user gesture), so the click just messages the background,
// which opens the panel synchronously while this click's gesture is still live.
function ensurePullTab() {
  if (document.getElementById("rwr-pulltab")) return;
  const tab = document.createElement("button");
  tab.id = "rwr-pulltab";
  tab.type = "button";
  tab.title = "Open WikiRyvals";
  tab.setAttribute("aria-label", "Open the WikiRyvals panel");
  tab.innerHTML =
    `<span class="rwr-pt-ico" aria-hidden="true">W<span class="rwr-ry">R</span></span>` +
    `<span class="rwr-pt-label">Lobby</span>`;
  tab.addEventListener("click", async () => {
    const r = await send("openSidePanel");
    if (!r || !r.ok) flash("Couldn't open the panel - click the WikiRyvals toolbar icon (or update Chrome).");
  });
  document.documentElement.appendChild(tab);
}

function flash(msg) {
  const f = document.getElementById("rwr-flash");
  if (!f) return;
  f.textContent = msg;
  f.classList.remove("rwr-hidden");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => f.classList.add("rwr-hidden"), 2400);
}

function raceHintKey(race) {
  return "rwr_hint_dismissed_" + (race.race_id || `${race.start}|${race.target}`);
}

function removeTargetHint() {
  const h = document.getElementById("rwr-hint");
  if (h) h.remove();
}

function showTargetHint(race, desc) {
  let h = document.getElementById("rwr-hint");
  if (!h) {
    h = document.createElement("div");
    h.id = "rwr-hint";
    document.documentElement.appendChild(h);
  }
  h.innerHTML =
    '<span class="rwr-hint-ico" aria-hidden="true">\u{1F4A1}</span>' +
    `<span class="rwr-hint-text">Stuck? <b>${esc(race.target)}</b> \u2014 ${esc(desc)}</span>` +
    '<button class="rwr-hint-x" type="button" aria-label="Dismiss hint">\u00D7</button>';
  const x = h.querySelector(".rwr-hint-x");
  if (x) x.addEventListener("click", () => {
    try { sessionStorage.setItem(raceHintKey(race), "1"); } catch (_) {}
    h.remove();
  });
}

// Called every timer tick. Once the race passes HINT_AFTER_MS, fetch the target's
// short description (Wikipedia REST summary - same origin, no extra permissions)
// and show a dismissible nudge. Guards keep it to one fetch + one show per race,
// persisted across same-tab hops so it doesn't re-pop on every page.
async function maybeShowTargetHint(ms) {
  if (hintDone || hintBusy) return;
  const race = currentRace;
  if (!race || race.finished || !race.target || ms < HINT_AFTER_MS) return;
  try { if (sessionStorage.getItem(raceHintKey(race)) === "1") { hintDone = true; return; } } catch (_) {}
  hintBusy = true;
  try {
    const cacheKey = "rwr_hint_desc_" + race.target;
    let desc = "";
    try { desc = sessionStorage.getItem(cacheKey) || ""; } catch (_) {}
    if (!desc) {
      const url = `${location.origin}/api/rest_v1/page/summary/` +
        encodeURIComponent(race.target.replace(/ /g, "_"));
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return; // transient (5xx / offline) - let a later tick retry
      const data = await res.json();
      desc = ((data && data.description) || "").trim();
      // Fall back to the lead sentence only when there's no short description, so
      // the nudge is never empty for the articles that lack one.
      if (!desc && data && data.extract) {
        desc = String(data.extract).split(". ")[0].trim();
        if (desc.length > 160) desc = desc.slice(0, 157) + "\u2026";
      }
      try { if (desc) sessionStorage.setItem(cacheKey, desc); } catch (_) {}
    }
    hintDone = true; // got a definitive answer; don't refetch this page load
    if (desc) showTargetHint(race, desc);
  } catch (_) {
    /* network hiccup - leave unshown so a later tick retries */
  } finally {
    hintBusy = false;
  }
}

// Fires after the grace once the race tab has gone hidden during a match. If it's
// still hidden (a real tab switch / minimize, not a same-tab navigation - those
// unload the page and kill this timer), forfeit the match.
async function onRaceTabLeft() {
  leaveForfeitTimer = null;
  if (!document.hidden) return;
  const race = currentRace;
  if (!race || !race.match_id || raceForfeited) return;
  raceForfeited = true;
  try { await send("forfeitMatch", { match_id: race.match_id }); } catch (_) {}
  try { await send("clearRace"); } catch (_) {}
  updateHud(null);
  flash("You left the race tab - match forfeited.");
}

function startHeartbeat(matchId) {
  if (heartbeatTimer && heartbeatMatchId === matchId) return;  // already pinging
  stopHeartbeat();
  heartbeatMatchId = matchId;
  heartbeatTimer = setInterval(async () => {
    const resp = await send("heartbeat", { match_id: matchId });
    // Stop once the match is gone/resolved so we don't ping a dead match forever.
    if (resp && resp.ok && resp.result && resp.result.alive === false) stopHeartbeat();
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  heartbeatMatchId = null;
}

function startTimer() {
  stopTimer();
  tick = setInterval(() => {
    const ms = timeBase.elapsed + (timeBase.running ? Date.now() - timeBase.at : 0);
    const el = document.getElementById("rwr-timer");
    if (el) el.textContent = fmt(ms);
    maybeShowTargetHint(ms);
  }, 100);
}
function stopTimer() {
  if (tick) clearInterval(tick);
  tick = null;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Compare clicks against the BFS-optimal par and return a branded verdict.
function verdict(clicks, par) {
  if (!par) return { label: "Finished", cls: "ok" };
  const over = clicks - par;
  if (over <= 0) return { label: "Optimal route", cls: "great" };
  if (over === 1) return { label: "1 click over par", cls: "good" };
  return { label: `${over} clicks over par`, cls: "meh" };
}

function pathChips(path, target) {
  return (path || [])
    .map((t, i) => {
      const role = i === 0 ? "rwr-pc-start"
        : t === target ? "rwr-pc-target" : "rwr-pc-mid";
      const arrow = i > 0 ? '<span class="rwr-pc-arrow">→</span>' : "";
      return `${arrow}<span class="rwr-pc ${role}">${esc(t)}</span>`;
    })
    .join("");
}

// Lightweight, dependency-free confetti burst. Honors reduced-motion; the canvas
// is fixed, click-through, and self-removes once the particles settle.
function fireConfetti(opts) {
  opts = opts || {};
  try {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  } catch (_) {}
  const root = opts.root || document.body || document.documentElement;
  if (!root) return;
  const colors = opts.colors || ["#f0883e", "#6699ff", "#ffd166", "#3fb950", "#e8eaed"];
  const cv = document.createElement("canvas");
  cv.style.cssText =
    "position:fixed;inset:0;width:100%;height:100%;margin:0;padding:0;border:0;" +
    "background:transparent;pointer-events:none;z-index:" + (opts.z || 2147483647) + ";";
  root.appendChild(cv);
  const ctx = cv.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W = (cv.width = Math.floor(window.innerWidth * dpr));
  let H = (cv.height = Math.floor(window.innerHeight * dpr));
  const onResize = () => {
    W = cv.width = Math.floor(window.innerWidth * dpr);
    H = cv.height = Math.floor(window.innerHeight * dpr);
  };
  window.addEventListener("resize", onResize);
  const cx = (opts.originX != null ? opts.originX : 0.5) * W;
  const cy = (opts.originY != null ? opts.originY : 0.34) * H;
  const parts = [];
  const N = opts.count || 150;
  for (let i = 0; i < N; i++) {
    const ang = -Math.PI / 2 + (Math.random() - 0.5) * 1.7;
    const spd = (5 + Math.random() * 11) * dpr;
    parts.push({
      x: cx + (Math.random() - 0.5) * 80 * dpr, y: cy,
      vx: Math.cos(ang) * spd, vy: Math.sin(ang) * spd,
      g: (0.15 + Math.random() * 0.12) * dpr,
      w: (5 + Math.random() * 6) * dpr, h: (8 + Math.random() * 7) * dpr,
      rot: Math.random() * Math.PI * 2, vr: (Math.random() - 0.5) * 0.35,
      color: colors[(Math.random() * colors.length) | 0],
      life: 0, ttl: 95 + Math.random() * 45,
    });
  }
  let raf = 0, frame = 0;
  const step = () => {
    frame++;
    ctx.clearRect(0, 0, W, H);
    let alive = 0;
    for (const p of parts) {
      if (p.life > p.ttl) continue;
      p.life++; alive++;
      p.vy += p.g; p.vx *= 0.99;
      p.x += p.vx; p.y += p.vy; p.rot += p.vr;
      ctx.save();
      ctx.globalAlpha = Math.max(0, 1 - p.life / p.ttl);
      ctx.translate(p.x, p.y); ctx.rotate(p.rot);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    if (alive && frame < 260) { raf = requestAnimationFrame(step); }
    else { window.removeEventListener("resize", onResize); cv.remove(); }
  };
  raf = requestAnimationFrame(step);
  setTimeout(() => {
    try { cancelAnimationFrame(raf); window.removeEventListener("resize", onResize); cv.remove(); } catch (_) {}
  }, 6000);
}

function renderWin(race) {
  let ov = document.getElementById("rwr-win");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "rwr-win";
    document.documentElement.appendChild(ov);
  }
  const clean = !race.flagged;
  const v = verdict(race.clicks, race.optimal_hops);
  ov.innerHTML = `
    <div class="rwr-win-card" role="dialog" aria-modal="true">
      <div class="rwr-win-top">
        <div class="rwr-win-brand">Wiki<span class="rwr-ry">Ry</span>vals</div>
        <button class="rwr-win-x" id="rwr-close" aria-label="Close">×</button>
      </div>
      <div class="rwr-win-hero ${clean ? "rwr-clean" : "rwr-flagged"}">
        <div class="rwr-win-emoji">${clean ? "🏁" : "⚠️"}</div>
        <div class="rwr-win-title">${clean ? "Target reached" : "Finished - flagged"}</div>
        <div class="rwr-win-route">
          <span class="rwr-pc rwr-pc-start">${esc(race.start)}</span>
          <span class="rwr-pc-arrow">→</span>
          <span class="rwr-pc rwr-pc-target">${esc(race.target)}</span>
        </div>
        ${clean && race.optimal_hops ? `<div class="rwr-win-badge rwr-${v.cls}">${v.label}</div>` : ""}
      </div>
      <div class="rwr-win-stats">
        <div class="rwr-stat"><div class="rwr-stat-num">${race.clicks}</div><div class="rwr-stat-lbl">clicks</div></div>
        <div class="rwr-stat"><div class="rwr-stat-num">${fmt(race.elapsed_ms)}</div><div class="rwr-stat-lbl">time</div></div>
        ${race.optimal_hops ? `<div class="rwr-stat"><div class="rwr-stat-num">${race.optimal_hops}</div><div class="rwr-stat-lbl">par</div></div>` : ""}
      </div>
      ${clean ? "" : '<div class="rwr-win-warn">An illegal hop (search box / URL bar / back button) was detected, so this run doesn\u2019t count as a clean finish.</div>'}
      ${race.missed_win ? `<div class="rwr-win-missed"><span class="rwr-missed-ico">\u{1F6AA}</span><div>You walked past the door - <b>${esc(race.missed_win.at)}</b> linked straight to <b>${esc(race.target)}</b>. You could've won in <b>${race.missed_win.could_have_clicks}</b> ${race.missed_win.could_have_clicks === 1 ? "click" : "clicks"} instead of ${race.missed_win.actual_clicks} (−${race.missed_win.saved}).</div></div>` : ""}
      <div class="rwr-win-pathwrap">
        <div class="rwr-win-pathlabel">Your route</div>
        <div class="rwr-win-path">${pathChips(race.path, race.target)}</div>
      </div>
      <button class="rwr-win-cta" id="rwr-mp">Make a free account to play multiplayer &rarr;</button>
      <div class="rwr-win-actions">
        <button class="rwr-btn-primary" id="rwr-again">New race</button>
        <button class="rwr-btn-ghost" id="rwr-dismiss">Keep reading</button>
      </div>
    </div>`;
  const newRace = async () => {
    ov.remove();
    await send("newRace", { difficulty: "any" });
  };
  ov.querySelector("#rwr-again").addEventListener("click", newRace);
  ov.querySelector("#rwr-dismiss").addEventListener("click", () => ov.remove());
  ov.querySelector("#rwr-close").addEventListener("click", () => ov.remove());
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  // Upsell: solo is account-free, but multiplayer needs an account. Opens the
  // side panel (sign-up / lobby). Soften the copy if they're already signed in.
  const mp = ov.querySelector("#rwr-mp");
  if (mp) {
    mp.addEventListener("click", () => { send("openSidePanel"); });
    try {
      chrome.storage.local.get(["wr_token"], (r) => {
        if (r && r.wr_token) mp.textContent = "Open the lobby to play 1v1 & duos \u2192";
      });
    } catch (_) {}
  }
  // Celebrate a clean, par-or-better solo run - the only "special" solo finish.
  if (clean && v.cls === "great") fireConfetti({ root: document.documentElement });
}

function applyRaceMode(active) {
  // Neutralize the on-page search box so it can't be used to teleport.
  document.querySelectorAll(
    "#searchInput, #searchform, .vector-search-box, #p-search input, .cdx-search-input__input"
  ).forEach((el) => {
    if (active) {
      el.setAttribute("disabled", "true");
      el.style.opacity = "0.4";
      el.style.pointerEvents = "none";
    } else {
      el.removeAttribute("disabled");
      el.style.opacity = "";
      el.style.pointerEvents = "";
    }
  });
}

function setHudButton(hud, race) {
  // The single HUD action button is contextual: "New race" when idle, "End race" to
  // abandon a *solo* run mid-race, and hidden entirely during a ranked/duo match
  // (which the race carries a match_id for) - that match's only Forfeit lives in the
  // side panel, so the top bar can't be fat-fingered into abandoning it. The
  // difficulty picker only matters when idle.
  const btn = hud.querySelector("#rwr-new");
  const diff = hud.querySelector("#rwr-diff");
  if (!btn) return;
  const active = !!(race && !race.finished);
  if (active && race.match_id) {
    // Ranked/duo match: the side panel owns the (confirmed) Forfeit. Keep the
    // in-page top bar free of any race-ending button so it can't be fat-fingered
    // into abandoning a live match.
    btn.style.display = "none";
    btn.dataset.action = "";
    delete btn.dataset.matchId;
    btn.classList.remove("rwr-danger");
  } else if (active) {
    // Solo / daily / weekly: no opponent, so ending the run from the top bar is fine.
    btn.style.display = "";
    btn.textContent = "End race";
    btn.dataset.action = "end";
    delete btn.dataset.matchId;
    btn.classList.remove("rwr-danger");
  } else {
    btn.style.display = "";
    btn.textContent = "New race";
    btn.dataset.action = "new";
    delete btn.dataset.matchId;
    btn.classList.remove("rwr-danger");
  }
  if (diff) diff.style.display = active ? "none" : "";
}

function updateHud(race, reveal) {
  const hud = ensureHud();
  raceActive = !!(race && !race.finished);
  currentRace = raceActive ? race : null;
  // Presence: ping the server while a MATCH is live so a closed/crashed tab gets
  // force-forfeited; stop otherwise (solo races, and once finished/idle).
  if (currentRace && currentRace.match_id) startHeartbeat(currentRace.match_id);
  else stopHeartbeat();
  const resultsBtn = hud.querySelector("#rwr-results");
  setHudButton(hud, race);
  if (!race) {
    // No active race: collapse the bar to just the brand + controls, hiding the
    // goal chips and the clicks/timer/par stats (see .rwr-idle in content.css).
    hud.classList.add("rwr-idle");
    hud.querySelector("#rwr-par-wrap").style.display = "none";
    if (resultsBtn) resultsBtn.classList.add("rwr-hidden");
    timeBase = { elapsed: 0, at: 0, running: false };
    stopTimer();
    removeTargetHint();
    hintDone = false;
    applyRaceMode(false);
    return;
  }
  hud.classList.remove("rwr-idle");
  hud.querySelector("#rwr-start").textContent = race.start;
  hud.querySelector("#rwr-target").textContent = race.target;
  hud.querySelector("#rwr-clicks").textContent = race.clicks;
  // Par comes from the merged play+snapshot graph; hide it entirely when we
  // couldn't compute one rather than showing a meaningless dash.
  const parWrap = hud.querySelector("#rwr-par-wrap");
  if (race.optimal_hops) {
    hud.querySelector("#rwr-par").textContent = race.optimal_hops;
    parWrap.style.display = "";
  } else {
    parWrap.style.display = "none";
  }
  applyRaceMode(!race.finished);
  // Once finished, surface a "Results" button so the modal can be reopened after
  // it's dismissed (otherwise the navbar just holds the frozen stats).
  if (resultsBtn) resultsBtn.classList.toggle("rwr-hidden", !race.finished);

  timeBase = { elapsed: race.elapsed_ms, at: Date.now(), running: !race.finished };
  document.getElementById("rwr-timer").textContent = fmt(race.elapsed_ms);
  if (race.finished) {
    stopTimer();
    removeTargetHint();
    // Only auto-open the modal on the *transition* to finished (this visit).
    // Later navigations come through init()'s else branch with reveal omitted, so
    // it won't re-pop on every page; the HUD's "Results" button reopens it.
    if (reveal) renderWin(race);
  } else {
    startTimer();
  }
}

async function init() {
  // Mark the page so our CSS can hide Wikipedia's fundraising/donation banners
  // (works even for the CentralNotice banners injected asynchronously).
  document.documentElement.classList.add("rwr-on");
  loadTheme();
  ensureHud();
  ensurePullTab();
  // Disable find-in-page (a classic Wikirace cheat) whenever a race is live.
  document.addEventListener("keydown", (ev) => {
    // Synchronous on purpose (see raceActive): we must preventDefault() before
    // yielding, or Chrome opens the find bar regardless.
    if (!raceActive) return;
    const k = ev.key ? ev.key.toLowerCase() : "";
    if ((ev.ctrlKey || ev.metaKey) && (k === "f" || k === "g")) {
      ev.preventDefault();
      ev.stopPropagation();
      flash("Find (Ctrl+F) is disabled during a race.");
    }
  }, true);

  // Remember the article link the player actually clicks so the backend can
  // validate the *click* (always a real on-page link) instead of the page they
  // land on - the two differ on Wikipedia redirects, which would otherwise be
  // flagged as illegal hops. sessionStorage survives the same-tab navigation to
  // the next article, where the visit report below reads and consumes it.
  document.addEventListener("click", (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest("a[href]");
    if (!a || !a.closest(".mw-parser-output") || a.closest(RWR_SKIP_SEL)) return;
    const to = hrefToTitle(a.getAttribute("href") || "");
    if (!to) return;
    try {
      sessionStorage.setItem(
        "rwr_via", JSON.stringify({ from: currentTitle(), to, at: Date.now() }));
    } catch (_) {}
  }, true);

  // Back/forward cache restore: the page returns WITHOUT re-running this script, so
  // report it explicitly as a back/forward hop (an illegal move the backend flags).
  window.addEventListener("pageshow", (ev) => {
    if (!ev.persisted) return;
    const t = currentTitle();
    if (!t) return;
    send("visit", { title: t, links: collectLinks(), nav: "back_forward" })
      .then((resp) => {
        if (resp && resp.ok && resp.race) {
          updateHud(resp.race, false);
          if (resp.race.flagged && resp.race.legal === false) {
            flash("Back/forward isn't a valid move - flagged.");
          }
        }
      });
  });

  // Leaving the race tab during a MATCH (tab switch / minimize) is an auto-forfeit.
  // The deferred check distinguishes a real tab switch (the page stays alive, so the
  // timer fires while still hidden) from a same-tab navigation to the next article
  // (the page unloads, killing the timer) - so legal hops never trip it.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (!currentRace || !currentRace.match_id) return;
      clearTimeout(leaveForfeitTimer);
      leaveForfeitTimer = setTimeout(onRaceTabLeft, LEAVE_GRACE_MS);
    } else if (leaveForfeitTimer) {
      clearTimeout(leaveForfeitTimer);
      leaveForfeitTimer = null;
      if (currentRace && currentRace.match_id && !raceForfeited) {
        flash("Heads up - leaving the race tab counts as a forfeit.");
      }
    }
  });

  const got = await send("getRace");
  const race = got.ok ? got.race : null;
  if (!race) {
    updateHud(null);
    return;
  }

  const title = currentTitle();
  if (title && !race.finished) {
    const via = readVia();
    const nav = navType();
    const resp = await send("visit", { title, links: collectLinks(), via: via.to, via_from: via.from, nav });
    updateHud(resp.ok ? resp.race : race, true);
    if (resp.ok && resp.race && resp.race.path && resp.race.path.length >= 2) {
      const last = resp.race.path[resp.race.path.length - 1];
      if (resp.race.flagged && last === title && resp.race.legal === false) {
        flash(nav === "back_forward"
          ? "Back/forward isn't a valid move - flagged."
          : "That wasn't a link on the previous page - flagged.");
      }
    }
  } else {
    updateHud(race);
  }
}

init();
