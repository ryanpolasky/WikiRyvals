"use strict";

// Single network/authority layer for the extension. All backend calls go through
// here (extension origin, granted via host_permissions) so the content script
// never makes cross-origin requests and there's no CORS/scraping surface.

// Backend origin comes from the shared config (single source of truth).
importScripts("config.js");
const BACKEND = globalThis.WIKIRYVALS_BACKEND;
const RACE_KEY = "race";

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

async function newRace(difficulty, sender, start, target, newTab) {
  let url = `${BACKEND}/api/ext/new?difficulty=${encodeURIComponent(difficulty || "any")}`;
  if (start && target) {
    url += `&start=${encodeURIComponent(start)}&target=${encodeURIComponent(target)}`;
  }
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`backend ${res.status}`);
  const data = await res.json();
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

async function reportVisit(title, links) {
  const race = await getRace();
  if (!race || race.finished) return race;
  const res = await fetch(`${BACKEND}/api/ext/visit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ race_id: race.race_id, title, links: links || [] }),
  });
  if (!res.ok) return race;
  const data = await res.json();
  // Preserve start_url across updates so the popup can still link to it.
  data.start_url = race.start_url;
  await setRace(data);
  return data;
}

// Track whether the side panel is open via a presence port the lobby connects on
// load, so the in-page pull tab can TOGGLE it shut (Chrome has no
// sidePanel.close(), so the panel closes itself when asked).
let panelPort = null;
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "rwr-panel") return;
  panelPort = port;
  port.onDisconnect.addListener(() => { if (panelPort === port) panelPort = null; });
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Open the side panel synchronously, still inside the content script's click
  // gesture - chrome.sidePanel.open() requires a user gesture, so we must not
  // await anything before calling it (that's why this is handled before the
  // async block below).
  if (msg && msg.type === "openSidePanel") {
    // Already open? Toggle it shut - it closes itself over the presence port.
    if (panelPort) {
      try { panelPort.postMessage({ type: "close" }); } catch (_) {}
      sendResponse({ ok: true });
      return;
    }
    try {
      if (!(chrome.sidePanel && chrome.sidePanel.open)) {
        throw new Error("sidePanel.open unavailable (needs Chrome 116+)");
      }
      const tab = sender && sender.tab;
      const opts = tab && tab.windowId != null
        ? { windowId: tab.windowId }
        : { tabId: tab && tab.id };
      chrome.sidePanel.open(opts);
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
        sendResponse({ ok: true, race: await newRace(msg.difficulty, sender, msg.start, msg.target, msg.newTab) });
      } else if (msg.type === "dailyRace") {
        sendResponse({ ok: true, race: await dailyRace(msg.token, msg.newTab) });
      } else if (msg.type === "weeklyRace") {
        sendResponse({ ok: true, race: await weeklyRace(msg.token, msg.newTab) });
      } else if (msg.type === "visit") {
        sendResponse({ ok: true, race: await reportVisit(msg.title, msg.links) });
      } else if (msg.type === "getRace") {
        sendResponse({ ok: true, race: await getRace() });
      } else if (msg.type === "clearRace") {
        await clearRace();
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // async response
});
