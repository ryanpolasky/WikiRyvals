"use strict";

// Single network/authority layer for the extension. All backend calls go through
// here (extension origin, granted via host_permissions) so the content script
// never makes cross-origin requests and there's no CORS/scraping surface.

// Backend origin comes from the shared config (single source of truth).
importScripts("config.js");
const BACKEND = globalThis.WIKIRYVALS_BACKEND;
const RACE_KEY = "race";

// ---- Match-safe auto-update -------------------------------------------------
// Chrome applies a downloaded extension update the next time this service worker
// shuts down, and that restart reloads the side panel and kills the live match
// WebSocket (it lives in the panel, not here). So while a multiplayer match is in
// progress we keep the worker awake, which defers the update, then apply it with
// chrome.runtime.reload() the instant the match ends - a clean swap at the lobby.
// Solo races run in a normal tab and don't need this, so the panel only holds
// during ranked/duo matches.
let UPDATE_PENDING = false;
let MATCH_HOLD = false;
let KEEPALIVE = null;
let HOLD_CAP = null;
const HOLD_CAP_MS = 15 * 60 * 1000;  // safety release if a match never signals its end

function startKeepAlive() {
  if (KEEPALIVE) return;
  // A periodic extension-API call resets the worker's ~30s idle timer, so a
  // pending update can't be applied out from under an active match.
  KEEPALIVE = setInterval(() => {
    try { chrome.runtime.getPlatformInfo(() => void chrome.runtime.lastError); }
    catch (_) {}
  }, 20000);
}
function stopKeepAlive() {
  if (KEEPALIVE) { clearInterval(KEEPALIVE); KEEPALIVE = null; }
}
function setMatchHold(active) {
  active = !!active;
  if (active === MATCH_HOLD) return;
  MATCH_HOLD = active;
  if (active) {
    startKeepAlive();
    if (HOLD_CAP) clearTimeout(HOLD_CAP);
    HOLD_CAP = setTimeout(() => setMatchHold(false), HOLD_CAP_MS);
  } else {
    if (HOLD_CAP) { clearTimeout(HOLD_CAP); HOLD_CAP = null; }
    stopKeepAlive();
    // Match's over: apply a deferred update now (a no-op when none is waiting).
    if (UPDATE_PENDING) { try { chrome.runtime.reload(); } catch (_) {} }
  }
}
chrome.runtime.onUpdateAvailable.addListener(() => {
  // Update is downloaded but not yet applied because the worker is running. If a
  // match is holding the worker awake, setMatchHold(false) applies it on match
  // end; otherwise the worker idles out shortly and Chrome applies it then.
  UPDATE_PENDING = true;
});

// Clicking the toolbar icon opens the WikiRyvals lobby as a docked side panel,
// so it sits beside live Wikipedia instead of replacing it. Guarded so a missing
// sidePanel API (older Chrome) can never abort the message listener below.
try {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch((e) => console.warn("sidePanel setup failed", e));
  }
} catch (e) {
  console.warn("sidePanel unavailable", e);
}

// Restrict the side panel to Wikipedia tabs: enable it there (so the toolbar icon
// and the in-page pull tab open the lobby), and disable it on every other site so
// the panel can't be opened off Wikipedia. Driven per-tab by the tab's URL.
function isWikipediaUrl(url) {
  try { return /(^|\.)wikipedia\.org$/i.test(new URL(url).hostname); }
  catch (_) { return false; }
}
async function syncPanelForTab(tabId, url) {
  if (!(chrome.sidePanel && chrome.sidePanel.setOptions)) return;
  try {
    if (isWikipediaUrl(url)) {
      await chrome.sidePanel.setOptions({ tabId, path: "lobby.html", enabled: true });
    } else {
      await chrome.sidePanel.setOptions({ tabId, enabled: false });
    }
  } catch (_) { /* tab probably closed */ }
}
try {
  chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
    if ((info.status === "complete" || info.url) && tab && tab.url) syncPanelForTab(tabId, tab.url);
  });
  chrome.tabs.onActivated.addListener(({ tabId }) => {
    chrome.tabs.get(tabId, (tab) => {
      if (!chrome.runtime.lastError && tab && tab.url) syncPanelForTab(tabId, tab.url);
    });
  });
  // Catch tabs already open when the service worker starts.
  chrome.tabs.query({}, (tabs) => (tabs || []).forEach((t) => {
    if (t.id != null && t.url) syncPanelForTab(t.id, t.url);
  }));
} catch (e) {
  console.warn("sidePanel per-tab gating unavailable", e);
}

async function getRace() {
  const obj = await chrome.storage.local.get(RACE_KEY);
  return obj[RACE_KEY] || null;
}
async function setRace(race) {
  await chrome.storage.local.set({ [RACE_KEY]: race });
}
async function clearRace() {
  await chrome.storage.local.remove(RACE_KEY);
}

async function activeTabId(sender) {
  if (sender && sender.tab && sender.tab.id != null) return sender.tab.id;
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab ? tab.id : null;
}

async function newRace(difficulty, sender, start, target, newTab, matchId) {
  let url = `${BACKEND}/api/ext/new?difficulty=${encodeURIComponent(difficulty || "any")}`;
  if (start && target) {
    url += `&start=${encodeURIComponent(start)}&target=${encodeURIComponent(target)}`;
  }
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const data = await res.json();
  // Tag ranked/duo races with their match_id so the in-page HUD can offer a real
  // Forfeit instead of a "New race" that would silently abandon the match.
  if (matchId) data.match_id = matchId;
  await setRace(data);
  // From the lobby side panel we open a fresh Wikipedia tab so the panel stays
  // put; from the in-page HUD we navigate the active tab.
  if (newTab) {
    chrome.tabs.create({ url: data.start_url });
  } else {
    const tabId = await activeTabId(sender);
    if (tabId != null) chrome.tabs.update(tabId, { url: data.start_url });
  }
  return data;
}

async function dailyRace(token, newTab) {
  // Today's shared daily route, bound server-side to this account so the finish
  // is recorded as their official attempt.
  const res = await fetch(`${BACKEND}/api/ext/daily/start?token=${encodeURIComponent(token || "")}`,
                          { method: "POST" });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const data = await res.json();
  await setRace(data);
  if (newTab) chrome.tabs.create({ url: data.start_url });
  return data;
}

async function weeklyRace(token, newTab) {
  // This week's puzzle, bound server-side to this account (one official attempt
  // per ISO week).
  const res = await fetch(`${BACKEND}/api/ext/weekly/start?token=${encodeURIComponent(token || "")}`,
                          { method: "POST" });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const data = await res.json();
  await setRace(data);
  if (newTab) chrome.tabs.create({ url: data.start_url });
  return data;
}

async function forfeitMatch(matchId) {
  // The side panel normally drives ranked results, but the in-page HUD can
  // forfeit too. Auth goes in the body as `token` (matching the panel's api()).
  const { wr_token } = await chrome.storage.local.get("wr_token");
  const res = await fetch(`${BACKEND}/api/ext/mm/result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: wr_token || "", match_id: matchId, forfeit: true }),
  });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  return await res.json();
}

async function sendHeartbeat(matchId) {
  // Presence ping from the live race tab so the server can detect a closed/crashed
  // tab and force-forfeit it. Best-effort; never throws. `alive` is false once the
  // match is gone/resolved, so the content script knows it can stop pinging.
  try {
    const { wr_token } = await chrome.storage.local.get("wr_token");
    const res = await fetch(`${BACKEND}/api/ext/mm/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: wr_token || "", match_id: matchId }),
    });
    return res.ok ? await res.json() : { ok: false };
  } catch (_) {
    return { ok: false };
  }
}

async function reportVisit(title, links, via, viaFrom, nav) {
  const race = await getRace();
  if (!race || race.finished) return race;
  const res = await fetch(`${BACKEND}/api/ext/visit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Forward via/via_from so the server validates the *click* (a real on-page link)
    // instead of the redirect it may resolve to, and nav so it can flag browser
    // back/forward. These were being dropped here, which silently broke the whole
    // redirect/anti-cheat path (the server always saw via=None).
    body: JSON.stringify({
      race_id: race.race_id, title, links: links || [],
      via: via || null, via_from: viaFrom || null, nav: nav || null,
    }),
  });
  if (!res.ok) return race;
  const data = await res.json();
  // Preserve fields the server's race state doesn't echo back: start_url (so the
  // popup can still link to it) and the match_id tag (so the in-page HUD keeps
  // knowing this is a ranked/duo match instead of reverting to a solo "End race").
  data.start_url = race.start_url;
  if (race.match_id) data.match_id = race.match_id;
  await setRace(data);
  return data;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Open the side panel synchronously, still inside the content script's click
  // gesture - chrome.sidePanel.open() requires a user gesture, so we must not
  // await anything before calling it (that's why this is handled before the
  // async block below).
  if (msg && msg.type === "openSidePanel") {
    // Stateless toggle (no open-flag for a worker eviction to lose): open() is a
    // no-op if the panel's already up, and the toggle broadcast is only received
    // by an *already-open* panel - one that's just opening isn't listening yet -
    // which then closes itself. open() must stay synchronous (user gesture).
    try {
      if (!(chrome.sidePanel && chrome.sidePanel.open)) {
        throw new Error("sidePanel.open unavailable (needs Chrome 116+)");
      }
      const tab = sender && sender.tab;
      const opts = tab && tab.windowId != null
        ? { windowId: tab.windowId }
        : { tabId: tab && tab.id };
      try { chrome.sidePanel.open(opts); } catch (_) {}
      chrome.runtime.sendMessage({ type: "rwr-panel-toggle" }, () => void chrome.runtime.lastError);
      sendResponse({ ok: true });
    } catch (e) {
      console.warn("openSidePanel failed", e);
      sendResponse({ ok: false, error: String(e) });
    }
    return; // handled synchronously; don't fall through to the async block
  }
  (async () => {
    try {
      if (msg.type === "newRace") {
        sendResponse({ ok: true, race: await newRace(msg.difficulty, sender, msg.start, msg.target, msg.newTab, msg.match_id) });
      } else if (msg.type === "dailyRace") {
        sendResponse({ ok: true, race: await dailyRace(msg.token, msg.newTab) });
      } else if (msg.type === "weeklyRace") {
        sendResponse({ ok: true, race: await weeklyRace(msg.token, msg.newTab) });
      } else if (msg.type === "visit") {
        sendResponse({ ok: true, race: await reportVisit(msg.title, msg.links, msg.via, msg.via_from, msg.nav) });
      } else if (msg.type === "getRace") {
        sendResponse({ ok: true, race: await getRace() });
      } else if (msg.type === "clearRace") {
        await clearRace();
        sendResponse({ ok: true });
      } else if (msg.type === "matchHold") {
        sendResponse({ ok: true });
        setMatchHold(!!msg.active);
      } else if (msg.type === "forfeitMatch") {
        sendResponse({ ok: true, result: await forfeitMatch(msg.match_id) });
      } else if (msg.type === "heartbeat") {
        sendResponse({ ok: true, result: await sendHeartbeat(msg.match_id) });
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // async response
});
