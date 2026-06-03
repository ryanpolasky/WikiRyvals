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

// Namespaces that aren't real article pages (mirrors wiki.py on the backend).
const RWR_NON_ARTICLE = new Set([
  "file", "image", "category", "help", "wikipedia", "template", "template_talk",
  "special", "portal", "draft", "module", "mediawiki", "book", "talk", "user",
  "user_talk", "wikt", "s", "q", "commons",
]);

// Page chrome whose links don't count as playable hops (mirrors wiki.py strip).
const RWR_SKIP_SEL = [
  ".navbox", ".vertical-navbox", ".metadata", ".mw-references", ".reflist",
  ".reference", ".noprint", ".mw-editsection", ".hatnote", ".ambox",
  ".sistersitebox", ".side-box", ".mw-empty-elt", "sup",
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
  if (b) b.textContent = rwrTheme === "light" ? "☀️" : "🌙";
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
  const next = rwrTheme === "light" ? "dark" : "light";
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

function fmt(ms) {
  return (ms / 1000).toFixed(1) + "s";
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
      <span id="rwr-par-wrap">par <b id="rwr-par">–</b></span>
    </div>
    <div class="rwr-actions">
      <button id="rwr-theme" title="Toggle light / dark" aria-label="Toggle theme">🌙</button>
      <select id="rwr-diff" title="Difficulty">
        <option value="any">Any</option><option value="easy">Easy</option>
        <option value="medium">Medium</option><option value="hard">Hard</option>
      </select>
      <button id="rwr-new">New race</button>
    </div>
    <div id="rwr-flash" class="rwr-flash rwr-hidden"></div>`;
  document.documentElement.appendChild(hud);
  hud.querySelector("#rwr-new").addEventListener("click", async () => {
    const difficulty = hud.querySelector("#rwr-diff").value;
    await send("newRace", { difficulty });
  });
  hud.querySelector("#rwr-theme").addEventListener("click", toggleTheme);
  syncThemeButton();
  return hud;
}

function flash(msg) {
  const f = document.getElementById("rwr-flash");
  if (!f) return;
  f.textContent = msg;
  f.classList.remove("rwr-hidden");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => f.classList.add("rwr-hidden"), 2400);
}

function startTimer() {
  stopTimer();
  tick = setInterval(() => {
    const el = document.getElementById("rwr-timer");
    if (!el) return;
    const ms = timeBase.elapsed + (timeBase.running ? Date.now() - timeBase.at : 0);
    el.textContent = fmt(ms);
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

function updateHud(race) {
  const hud = ensureHud();
  if (!race) {
    // No active race: collapse the bar to just the brand + controls, hiding the
    // goal chips and the clicks/timer/par stats (see .rwr-idle in content.css).
    hud.classList.add("rwr-idle");
    hud.querySelector("#rwr-par-wrap").style.display = "none";
    timeBase = { elapsed: 0, at: 0, running: false };
    stopTimer();
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

  timeBase = { elapsed: race.elapsed_ms, at: Date.now(), running: !race.finished };
  document.getElementById("rwr-timer").textContent = fmt(race.elapsed_ms);
  if (race.finished) {
    stopTimer();
    renderWin(race);
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
  // Disable find-in-page (a classic Wikirace cheat) whenever a race is live.
  document.addEventListener("keydown", async (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ["f", "g"].includes(ev.key.toLowerCase())) {
      const r = await send("getRace");
      if (r.ok && r.race && !r.race.finished) {
        ev.preventDefault();
        ev.stopPropagation();
        flash("Find (Ctrl+F) is disabled during a race.");
      }
    }
  }, true);

  const got = await send("getRace");
  const race = got.ok ? got.race : null;
  if (!race) {
    updateHud(null);
    return;
  }

  const title = currentTitle();
  if (title && !race.finished) {
    const resp = await send("visit", { title, links: collectLinks() });
    updateHud(resp.ok ? resp.race : race);
    if (resp.ok && resp.race && resp.race.path && resp.race.path.length >= 2) {
      const last = resp.race.path[resp.race.path.length - 1];
      if (resp.race.flagged && last === title && resp.race.legal === false) {
        flash("That wasn't a link on the previous page - flagged.");
      }
    }
  } else {
    updateHud(race);
  }
}

init();
