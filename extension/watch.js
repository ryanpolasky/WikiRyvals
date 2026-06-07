// Standalone read-only watch-party view. Opened as a full tab via
// chrome-extension://<id>/watch.html?match=<id> (e.g. cast on a projector).
// It fetches the match metadata once, then subscribes to the spectate WebSocket
// to render every player's live position. It never sends anything that can affect
// the match - it's a pure spectator.

const BACKEND = globalThis.WIKIRYVALS_BACKEND;  // from config.js (loaded first in watch.html)
const WS_BACKEND = BACKEND.replace(/^http/, "ws");

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const MATCH_ID = params.get("match");

let META = null;
let SOCK = null;
const PROGRESS = {};       // slot_id -> latest progress msg
const GHOST_KEY = "__ghost__";
let ghostSeq = 0;

function initials(name) {
  if (!name) return "?";
  const p = name.trim().split(/\s+/);
  return ((p[0]?.[0] || "") + (p[1]?.[0] || "")).toUpperCase() || name[0].toUpperCase();
}
function fmtTime(ms) {
  if (ms == null) return "-";
  const s = Math.floor(ms / 1000), m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}.${String(Math.floor((ms % 1000) / 100))}s`;
}
function setLive(on, text) {
  $("live").classList.toggle("on", !!on);
  $("live-text").textContent = text;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}

function playerCard(pl) {
  // Live state for this player comes from PROGRESS keyed by their redacted slot.
  const prog = pl.slot_id ? PROGRESS[pl.slot_id] : null;
  const div = document.createElement("div");
  div.className = "player";
  let badge = "";
  if (prog && prog.finished) { div.classList.add("done"); badge = `<span class="p-badge badge-done">FINISHED</span>`; }
  if ((prog && prog.flagged) || pl.flagged) { div.classList.add("flagged"); badge = `<span class="p-badge badge-flag">FLAGGED</span>`; }
  const cur = pl.is_bot
    ? `<span class="pending">👻 ghost</span>`
    : (prog && prog.current ? esc(prog.current) : `<span class="pending">waiting to start…</span>`);
  const clicks = prog ? prog.clicks : (pl.is_bot ? "-" : 0);
  const time = prog ? fmtTime(prog.elapsed_ms) : "-";
  div.innerHTML = `${badge}
    <div class="p-top">
      <div class="p-av">${pl.is_bot ? "👻" : esc(initials(pl.username))}</div>
      <div>
        <div class="p-name">${esc(pl.username)}</div>
        <div class="p-team">${[pl.rp != null ? pl.rp + " EP" : "", pl.region ? esc(pl.region) : ""].filter(Boolean).join(" · ")}</div>
      </div>
    </div>
    <div class="p-cur">${cur}</div>
    <div class="p-stats"><span>${clicks} clicks</span><span>${time}</span></div>`;
  return div;
}

function render() {
  if (!META) return;
  const c = $("content");
  c.className = "";
  const route = `<div class="route"><b>${esc(META.start)}</b><span class="arrow">→</span><b>${esc(META.target)}</b></div>` +
    `<div class="meta">${esc(META.difficulty)}${META.par ? " · par " + META.par : ""} · ${META.kind === "duo" ? "Ranked Duos" : "Ranked 1v1"}</div>`;
  c.innerHTML = route;

  if (META.resolved && META.results) {
    const banner = document.createElement("div");
    banner.className = "resolved";
    banner.innerHTML = `<div class="banner">Match complete</div>`;
    c.appendChild(banner);
  }

  if (META.kind === "duo") {
    const wrap = document.createElement("div");
    wrap.className = "teams";
    [["Team A", META.team_a], ["Team B", META.team_b]].forEach(([label, team]) => {
      const col = document.createElement("div");
      col.innerHTML = `<p class="team-h">${label}</p>`;
      const g = document.createElement("div");
      (team || []).forEach((pl) => g.appendChild(playerCard(pl)));
      col.appendChild(g);
      wrap.appendChild(col);
    });
    c.appendChild(wrap);
  } else {
    const grid = document.createElement("div");
    grid.className = "grid";
    (META.players || []).forEach((pl) => grid.appendChild(playerCard(pl)));
    c.appendChild(grid);
  }
}

async function loadMeta() {
  const res = await fetch(`${BACKEND}/api/ext/spectate/${encodeURIComponent(MATCH_ID)}`);
  if (!res.ok) throw new Error(`backend ${res.status}`);
  META = await res.json();
  render();
}

function connect() {
  SOCK = new WebSocket(`${WS_BACKEND}/api/ext/ws/spectate/${encodeURIComponent(MATCH_ID)}`);
  SOCK.onopen = () => setLive(true, "live");
  SOCK.onclose = () => { setLive(false, "disconnected"); };
  SOCK.onerror = () => setLive(false, "connection error");
  SOCK.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "progress" && msg.slot_id) {
      PROGRESS[msg.slot_id] = msg;
      render();
    } else if (msg.type === "resolved") {
      if (META) { META.resolved = true; META.results = msg.results; }
      render();
      setLive(true, "finished");
    }
  };
}

async function main() {
  if (!MATCH_ID) {
    $("content").innerHTML = `<h2>No match specified</h2><p>Open a watch-party link from a live race.</p>`;
    setLive(false, "no match");
    return;
  }
  try {
    await loadMeta();
    connect();
  } catch (e) {
    $("content").innerHTML = `<h2>Couldn't load the match</h2>` +
      `<p>It may have already ended, or the backend is unavailable.</p>`;
    setLive(false, "unavailable");
  }
}

main();
