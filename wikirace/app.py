"""FastAPI single-player Wikirace prototype with server-authoritative validation.

The server owns each race's state (start, target, current page, click path, clock).
Every navigation is validated against the link set of the page the player is
*actually* on (server-tracked), never the client's claim - so illegal jumps are
impossible and the full path is auditable. This is the Phase 1 anti-cheat core,
exercised here in single-player.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncio

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .accounts import ACCOUNTS_PATH, AccountError, AccountStore, RateLimitError
from .graph import induced_adjacency, shortest_hops, shortest_hops_via
from .glicko2 import Rating, rate_1v1
from .duos import DuoMatchMaker, _team_best
from .matchmaking import MatchMaker, Side
from .play_graph import PLAY_GRAPH_PATH, PlayGraph
from .ranks import compute_rp, rank_for_rp
from .realtime import hub
from .snapshot_store import PROMPTS_PATH, SnapshotStore
from .wiki import _is_article_title, normalize_title

WIKI_BASE = "https://en.wikipedia.org/wiki/"

app = FastAPI(title="WikiRyvals - Phase 0")
# The extension routes calls through its background worker (extension origin), but
# allow-all CORS keeps direct content-script fetches working in dev too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
store = SnapshotStore()
# Link graph grown from real play (see play_graph.py). Powers the "missed win"
# hint and, over time, BFS without crawling Wikipedia's API. The path is
# overridable (WIKIRYVALS_PLAY_GRAPH) so a container can point it at a mounted
# volume and keep the graph across restarts.
play_graph = PlayGraph(Path(os.environ.get("WIKIRYVALS_PLAY_GRAPH", str(PLAY_GRAPH_PATH))))

# Accounts + ratings + match history (durable, SQLite). Path overridable so a
# container can mount it on a volume and keep accounts across restarts.
accounts = AccountStore(Path(os.environ.get("WIKIRYVALS_ACCOUNTS", str(ACCOUNTS_PATH))))

# Login codes are emailed in production; with no SMTP configured (the default for
# a personal/local deploy) we surface the code to the client so the full
# passwordless flow is usable end-to-end without a mail server.
DEV_AUTH = os.environ.get("WIKIRYVALS_SMTP_HOST") in (None, "")


def _clean_links(raw: list[str] | None) -> list[str]:
    """Normalize + filter a client-reported link set to real article titles,
    using the same rules the server validates moves against."""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        t = normalize_title(r)
        if t and _is_article_title(t) and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def wiki_url(title: str) -> str:
    return WIKI_BASE + urllib.parse.quote(title.replace(" ", "_"))


class PromptProvider:
    def __init__(self) -> None:
        self.prompts: list[dict] = []
        if PROMPTS_PATH.exists():
            data = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
            self.prompts = data.get("prompts", [])

    def pick(self, difficulty: str | None) -> dict | None:
        pool = self.prompts
        if difficulty and difficulty != "any":
            pool = [p for p in pool if p["difficulty"] == difficulty]
        if pool:
            return random.choice(pool)
        return self._fallback(difficulty)

    def _fallback(self, difficulty: str | None) -> dict | None:
        """Generate a prompt on the fly if no prompt file exists yet."""
        if not store.loaded:
            return None
        adjacency = induced_adjacency(store.adjacency)
        titles = [t for t in store.titles if adjacency.get(t)]
        if len(titles) < 2:
            return None
        for _ in range(500):
            start, target = random.sample(titles, 2)
            hops = shortest_hops(adjacency, start, target, max_depth=5)
            if hops and hops >= 2:
                return {"start": start, "target": target, "hops": hops,
                        "difficulty": difficulty or "any"}
        return None


prompts = PromptProvider()


@app.on_event("startup")
def _startup() -> None:
    if not store.load_graph():
        print("WARNING: no snapshot graph found. Run `python -m snapshot.build_snapshot` "
              "and `python -m snapshot.generate_prompts` first.")
    # Let the realtime hub publish onto the serving loop from sync handlers.
    try:
        hub.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    # Rehydrate any live matches that were mid-flight when we last stopped
    # (only recent ones; older than the match TTL aren't worth recovering).
    try:
        blobs = accounts.load_active_matches(max_age_seconds=MATCH_RESTORE_MAX_AGE)
        restored = matchmaker.restore(blobs)
        restored_duo = duo_matchmaker.restore(blobs)
        if restored or restored_duo:
            print(f"Restored {restored} live 1v1 + {restored_duo} live duo match(es) "
                  "from the last run.")
    except Exception as e:  # pragma: no cover - best-effort recovery
        print(f"Could not restore live matches: {e}")


# ---------------------------------------------------------------------------
# Chrome-extension flow (play on live Wikipedia).
#
# Unlike the standalone app, we can't stop the browser from loading a page, so
# validation is *post-hoc*: each time the content script reports the article the
# player landed on, we check whether it was a legal link from the page they were
# previously on. Illegal hops (search box, URL bar, back button) get flagged, and
# a flagged path can't count as a clean finish.
# ---------------------------------------------------------------------------

@dataclass
class ExtRace:
    race_id: str
    start: str
    target: str
    difficulty: str
    optimal_hops: int
    current: str
    path: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished: bool = False
    finished_at: float | None = None
    flagged: bool = False
    # title -> the on-page link set the content script saw when the player was on
    # that page (latest observation). Used to detect a "missed win".
    links_seen: dict[str, list[str]] = field(default_factory=dict)
    missed_win: dict | None = None
    # When set, this race is today's daily challenge for a specific player; the
    # first finished daily attempt is recorded as their official board entry.
    daily_date: str | None = None
    daily_user: str | None = None
    daily_username: str | None = None
    # When set, this race is the weekly puzzle for a specific player; first
    # finished attempt of the ISO week is their official board entry.
    weekly_week: str | None = None
    weekly_user: str | None = None
    weekly_username: str | None = None
    # Wall-clock of the last interaction; drives idle eviction so memory stays
    # flat on an always-on server.
    last_touch: float = field(default_factory=time.monotonic)

    @property
    def clicks(self) -> int:
        return max(0, len(self.path) - 1)

    @property
    def elapsed_ms(self) -> int:
        if self.started_at is None:
            return 0
        # Freeze the clock at the finishing moment so a finished race reports a
        # stable time (used as the authoritative result for ranked matches).
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return int((end - self.started_at) * 1000)


EXT_RACES: dict[str, ExtRace] = {}

# Idle races are swept so an always-on box doesn't grow memory without bound.
RACE_TTL_SECONDS = 3600.0
_SWEEP_INTERVAL = 60.0
_last_sweep = 0.0


def _sweep_races(force: bool = False) -> None:
    """Drop races that have been idle longer than the TTL (throttled)."""
    global _last_sweep
    now = time.monotonic()
    if not force and (now - _last_sweep) < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    for rid in [r for r, x in EXT_RACES.items() if now - x.last_touch > RACE_TTL_SECONDS]:
        EXT_RACES.pop(rid, None)


def _known_links(race: ExtRace, page: str) -> set[str] | None:
    """Best link set for `page` WITHOUT calling Wikipedia, in priority order:
    (1) what the content script reported when the player was on that page,
    (2) the play graph (built from prior real play),
    (3) the frozen snapshot adjacency. None if we genuinely don't know it yet.
    """
    seen = race.links_seen.get(page)
    if seen:
        return set(seen)
    pg = play_graph.links_of(page)
    if pg:
        return set(pg)
    snap = store.adjacency.get(page)
    if snap:
        return set(snap)
    return None


def _merged_neighbors(node: str) -> set[str]:
    """Out-links for `node` from the play-built graph unioned with the snapshot.
    The play graph grows from real play (no API crawl), so par self-improves
    over time and can find routes the frozen snapshot doesn't know about."""
    return set(play_graph.links_of(node)) | set(store.adjacency.get(node, ()))


def _par_hops(start: str, target: str) -> int | None:
    """BFS-optimal click count over the merged play+snapshot graph, or None if
    we can't reach the target within the search depth (then we hide par rather
    than show a number we can't stand behind)."""
    return shortest_hops_via(_merged_neighbors, start, target, max_depth=6)


def _ext_state(race: ExtRace) -> dict:
    return {
        "race_id": race.race_id,
        "start": race.start,
        "target": race.target,
        "current": race.current,
        "clicks": race.clicks,
        "elapsed_ms": race.elapsed_ms,
        "finished": race.finished,
        "flagged": race.flagged,
        "optimal_hops": race.optimal_hops,
        "target_url": wiki_url(race.target),
        "path": race.path,
        "missed_win": race.missed_win,
    }


def _compute_missed_win(race: ExtRace) -> dict | None:
    """Find the earliest page the player passed through that linked *straight* to
    the target before the page they actually won from - i.e. a door they walked
    past. Uses only link sets we already saw (no API calls).
    """
    path = race.path
    if len(path) < 2:
        return None
    actual_clicks = race.clicks
    # Candidates are pages strictly before the page they won from (path[-2]).
    for i in range(0, len(path) - 2):
        page = path[i]
        if race.target in set(race.links_seen.get(page, ())):
            could = i + 1  # clicks to win if they'd clicked the target from page i
            if could < actual_clicks:
                return {
                    "at": page,
                    "could_have_clicks": could,
                    "actual_clicks": actual_clicks,
                    "saved": actual_clicks - could,
                }
    return None


class ExtVisitRequest(BaseModel):
    race_id: str
    title: str
    links: list[str] | None = None


@app.post("/api/ext/new")
def ext_new_race(
    difficulty: str = "any",
    start: str | None = None,
    target: str | None = None,
) -> dict:
    _sweep_races()
    # Custom race: caller supplies both endpoints (also used for deterministic
    # testing). Otherwise pull a difficulty-bucketed prompt from the snapshot.
    if start and target:
        start, target = normalize_title(start), normalize_title(target)
        # Par from the merged play+snapshot graph; 0 means "unknown" (hidden).
        hops = _par_hops(start, target)
        race = ExtRace(
            race_id=uuid.uuid4().hex,
            start=start,
            target=target,
            difficulty="custom",
            optimal_hops=int(hops or 0),
            current=start,
        )
        EXT_RACES[race.race_id] = race
        body = _ext_state(race)
        body["start_url"] = wiki_url(race.start)
        return body

    prompt = prompts.pick(difficulty)
    if prompt is None:
        raise HTTPException(503, "No prompts available - build the snapshot first.")
    # Recompute par over the merged play+snapshot graph (can only be <= the
    # snapshot-only value the prompt was generated with); fall back to the
    # prompt's stored hops if the merged BFS somehow can't reach it.
    hops = _par_hops(prompt["start"], prompt["target"])
    if hops is None:
        hops = prompt.get("hops")
    race = ExtRace(
        race_id=uuid.uuid4().hex,
        start=prompt["start"],
        target=prompt["target"],
        difficulty=prompt.get("difficulty", "any"),
        optimal_hops=int(hops or 0),
        current=prompt["start"],
    )
    EXT_RACES[race.race_id] = race
    body = _ext_state(race)
    body["start_url"] = wiki_url(race.start)
    return body


@app.post("/api/ext/visit")
def ext_visit(req: ExtVisitRequest) -> dict:
    race = EXT_RACES.get(req.race_id)
    if race is None:
        raise HTTPException(404, "Unknown race.")
    race.last_touch = time.monotonic()
    title = normalize_title(req.title)

    # Record what links are actually on this page (reported by the content script
    # from the live DOM). Latest observation wins, so links edited out of an
    # article get pruned from the play graph. No Wikipedia API calls.
    links = _clean_links(req.links)
    if links:
        race.links_seen[title] = links
        play_graph.record(title, links)

    if race.finished:
        return _ext_state(race)

    # First landing starts the clock and anchors the path (handles redirects on
    # the start article - we accept wherever the player first arrives).
    if race.started_at is None:
        race.started_at = time.monotonic()
        race.current = title
        race.path = [title]
        _route_race_update(race, is_hop=False)
        return _ext_state(race)

    # Same page (reload / in-page anchor) - no-op.
    if title == race.current:
        return _ext_state(race)

    # Validate the hop against the previous page's known links WITHOUT fetching
    # Wikipedia (client report → play graph → snapshot). If we genuinely have no
    # record of that page yet, we can't prove the hop illegal, so we accept it
    # but mark it unverified rather than paying for a live fetch on the hot path.
    known = _known_links(race, race.current)
    verified = known is not None
    legal = (title in known) if verified else True
    race.path.append(title)
    race.current = title
    if not legal:
        race.flagged = True
    if title == race.target:
        race.finished = True
        race.finished_at = time.monotonic()
        race.missed_win = _compute_missed_win(race)
        _record_daily_if_due(race)
        _record_weekly_if_due(race)
    # Push live progress to the opponent and, on finish, resolve instantly.
    _route_race_update(race, is_hop=True)
    state = _ext_state(race)
    state["legal"] = legal
    state["verified"] = verified
    return state


@app.get("/api/ext/race/{race_id}")
def ext_race_state(race_id: str) -> dict:
    race = EXT_RACES.get(race_id)
    if race is None:
        raise HTTPException(404, "Unknown race.")
    return _ext_state(race)


@app.get("/api/ext/health")
def ext_health() -> dict:
    # Lightweight liveness check the extension lobby uses to show backend status.
    return {
        "ok": True,
        "articles": len(store.adjacency),
        "prompts": len(prompts.prompts),
        "play_graph": play_graph.stats,
        "matchmaking": matchmaker.stats,
    }


# ===========================================================================
# Phase 1: accounts, ranked matchmaking, private lobbies, leaderboard.
#
# Auth is passwordless (email -> 6-digit code -> session token). The token is
# passed in the request body (the extension's background worker speaks JSON) or
# an `Authorization: Bearer` header. Ranked results are server-authoritative:
# clicks/time/flagged come from the EXT race state the server already tracked,
# never the client's claim.
# ===========================================================================

# Matchmaker shares the prompt pool + merged-graph par with the rest of the app.
# The persist/forget hooks make a live head-to-head durable: it's serialized to
# the accounts DB on every change and dropped once resolved, so a server restart
# mid-match rehydrates the match instead of stranding both players.
matchmaker = MatchMaker(
    prompt_picker=prompts.pick, par_fn=_par_hops,
    on_persist=accounts.save_active_match, on_forget=accounts.delete_active_match,
)

# 2v2 duos run as a parallel matcher over the same durable store (blobs are
# tagged "kind":"duo", so each matcher restores only its own on startup).
duo_matchmaker = DuoMatchMaker(
    prompt_picker=prompts.pick, par_fn=_par_hops,
    on_persist=accounts.save_active_match, on_forget=accounts.delete_active_match,
)

# Don't bother recovering matches older than this on startup (past the live TTL).
MATCH_RESTORE_MAX_AGE = 1800.0

# match_id -> {user_id -> ranked-result payload}. Lets the slower of two human
# players fetch their result after the match resolves.
RANKED_RESULTS: dict[str, dict[str, dict]] = {}

# Premade duos parties live only while an invite is pending/being seated (the
# durable record is the accepted match itself). invite_id -> invite dict.
DUO_INVITES: dict[str, dict] = {}
DUO_INVITES_LOCK = threading.Lock()
INVITE_TTL = 90.0  # seconds a pending invite stays live before it lapses


def _bearer(token: str | None, authorization: str | None) -> str | None:
    if token:
        return token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _require_user(token: str | None, authorization: str | None = None) -> dict:
    user = accounts.user_by_token(_bearer(token, authorization))
    if user is None:
        raise HTTPException(401, "Sign in first.")
    return user


def _require_admin(token: str | None, authorization: str | None = None) -> dict:
    user = _require_user(token, authorization)
    if not user.get("is_admin"):
        raise HTTPException(403, "Admins only.")
    return user


def _email_html(heading: str, body_html: str) -> str:
    """Branded HTML wrapper for transactional emails. Inline styles only (mail
    clients ignore <style>/external CSS); the logo is pulled from the site."""
    return f"""\
<!doctype html><html><body style="margin:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#202122;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
  <tr><td align="center">
    <table role="presentation" width="460" cellpadding="0" cellspacing="0" style="width:460px;max-width:100%;background:#ffffff;border:1px solid #e3e6ea;border-radius:14px;overflow:hidden;">
      <tr><td style="padding:22px 28px 4px;">
        <img src="https://wikiryvals.com/wr-128.png" width="44" height="44" alt="" style="vertical-align:middle;border-radius:10px;">
        <span style="font-size:22px;font-weight:800;vertical-align:middle;margin-left:10px;">Wiki<span style="color:#3366cc;">Ry</span>vals</span>
      </td></tr>
      <tr><td style="padding:8px 28px 0;font-size:18px;font-weight:700;">{heading}</td></tr>
      <tr><td style="padding:10px 28px 24px;font-size:14px;line-height:1.6;color:#3a4046;">{body_html}</td></tr>
      <tr><td style="padding:16px 28px;background:#f8f9fa;border-top:1px solid #eaecf0;font-size:12px;color:#72777d;">
        You're receiving this because someone requested it for your WikiRyvals account. If that wasn't you, you can safely ignore this email.
        <br><a href="https://wikiryvals.com" style="color:#3366cc;text-decoration:none;">wikiryvals.com</a>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _send_login_code(email: str, code: str) -> bool:
    """Deliver a login code. Returns True if it was actually emailed.

    With no SMTP host configured we run in dev mode: log the code and let the
    caller surface it to the client so the flow works without a mail server.
    """
    host = os.environ.get("WIKIRYVALS_SMTP_HOST")
    if not host:
        print(f"[WikiRyvals dev-auth] login code for {email}: {code}")
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Your WikiRyvals login code"
    msg["From"] = os.environ.get("WIKIRYVALS_SMTP_FROM", "noreply@wikiryvals.com")
    msg["To"] = email
    msg.set_content(f"Your WikiRyvals login code is: {code}\n\nIt expires in 10 minutes.")
    msg.add_alternative(_email_html(
        "Your login code",
        '<p style="margin:0 0 14px;">Use this code to sign in. It expires in 10 minutes.</p>'
        f'<div style="font-size:30px;font-weight:800;letter-spacing:8px;text-align:center;'
        f'background:#f3f4f6;border:1px solid #e3e6ea;border-radius:10px;padding:16px 0;color:#202122;">{code}</div>',
    ), subtype="html")
    port = int(os.environ.get("WIKIRYVALS_SMTP_PORT", "587"))
    user = os.environ.get("WIKIRYVALS_SMTP_USER")
    pw = os.environ.get("WIKIRYVALS_SMTP_PASS")
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        if user and pw:
            s.login(user, pw)
        s.send_message(msg)
    return True


# ---- auth -----------------------------------------------------------------

class RequestCodeReq(BaseModel):
    email: str


class VerifyReq(BaseModel):
    email: str
    code: str


class ProfileReq(BaseModel):
    token: str
    username: str
    region: str | None = None


# Per-IP throttle on code requests (single worker -> in-process is fine). Backs up
# the per-email cooldown in accounts so one IP can't fan out across many emails.
_CODE_IP_HITS: dict[str, list[float]] = {}
_CODE_IP_WINDOW = 3600.0
_CODE_IP_MAX = 20


def _ip_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _CODE_IP_HITS.get(ip, []) if now - t < _CODE_IP_WINDOW]
    if len(hits) >= _CODE_IP_MAX:
        _CODE_IP_HITS[ip] = hits
        return False
    hits.append(now)
    _CODE_IP_HITS[ip] = hits
    return True


@app.post("/api/ext/auth/request-code")
def auth_request_code(req: RequestCodeReq, request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not _ip_rate_ok(client_ip):
        raise HTTPException(429, "Too many requests. Try again later.")
    try:
        code = accounts.issue_login_code(req.email)
    except RateLimitError as e:
        raise HTTPException(429, str(e))
    except AccountError as e:
        raise HTTPException(400, str(e))
    emailed = _send_login_code(req.email, code)
    out: dict = {"ok": True, "emailed": emailed}
    # Dev mode (no SMTP): hand the code back so the flow is usable end-to-end.
    if not emailed and DEV_AUTH:
        out["dev_code"] = code
    return out


@app.post("/api/ext/auth/verify")
def auth_verify(req: VerifyReq) -> dict:
    try:
        user = accounts.verify_login_code(req.email, req.code)
    except AccountError as e:
        raise HTTPException(400, str(e))
    token = accounts.create_session(user["id"])
    return {"ok": True, "token": token, "user": user}


@app.post("/api/ext/auth/profile")
def auth_profile(req: ProfileReq) -> dict:
    user = _require_user(req.token)
    try:
        updated = accounts.set_profile(user["id"], req.username, req.region)
    except AccountError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "user": updated}


@app.get("/api/ext/auth/username-available")
def auth_username_available(name: str) -> dict:
    return {"available": accounts.username_available(name)}


@app.post("/api/ext/auth/logout")
def auth_logout(req: RequestCodeReq | None = None,
                authorization: str | None = Header(default=None)) -> dict:
    # Accept token via header for logout (no body required).
    token = _bearer(None, authorization)
    if token:
        accounts.logout(token)
    return {"ok": True}


@app.get("/api/ext/me")
def ext_me(token: str | None = None,
           authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    user["daily"] = accounts.daily_streak_state(user["id"], _today())
    return {"user": user}


@app.get("/api/ext/rival")
def ext_rival(token: str | None = None,
              authorization: str | None = Header(default=None)) -> dict:
    """The single Ryval we float for the player (most-faced human opponent)."""
    user = _require_user(token, authorization)
    return {"rival": accounts.top_rival(user["id"])}


# ---- daily challenge ------------------------------------------------------

def _today() -> str:
    """UTC date key (YYYY-MM-DD) so everyone shares one route per calendar day."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _daily_prompt(date: str) -> dict | None:
    """Deterministic shared route for a date: same start/target for everyone,
    chosen from the snapshot prompt pool by hashing the date (no per-day state)."""
    pool = prompts.prompts
    if not pool:
        return None
    seed = int(hashlib.sha256(date.encode("utf-8")).hexdigest(), 16)
    p = pool[seed % len(pool)]
    par = _par_hops(p["start"], p["target"]) or int(p.get("hops") or 0)
    return {"start": p["start"], "target": p["target"], "par": int(par or 0)}


def _record_daily_if_due(race: "ExtRace") -> None:
    """On a finished daily race, store the player's first official attempt."""
    if not race.daily_date or not race.daily_user:
        return
    try:
        res = accounts.record_daily_result(
            race.daily_date, race.daily_user, race.daily_username,
            start=race.start, target=race.target, clicks=race.clicks,
            time_ms=race.elapsed_ms, flagged=race.flagged, finished=race.finished,
        )
        # Count the daily once, on the official (first) finish, for season stats.
        if res.get("recorded") and race.finished:
            accounts.add_season_stats(race.daily_user, inc={"daily_finished": 1})
    except Exception:
        pass


@app.get("/api/ext/daily")
def daily_today(token: str | None = None,
                authorization: str | None = Header(default=None)) -> dict:
    date = _today()
    prompt = _daily_prompt(date)
    if prompt is None:
        raise HTTPException(503, "No prompts available - build the snapshot first.")
    out: dict = {"date": date, "start": prompt["start"], "target": prompt["target"],
                 "players": accounts.daily_count(date)}
    if prompt["par"]:
        out["par"] = prompt["par"]
    # If logged in, surface whether they've already played and their result.
    user = accounts.user_by_token(_bearer(token, authorization))
    if user:
        out["your_result"] = accounts.daily_result(date, user["id"])
    return out


@app.post("/api/ext/daily/start")
def daily_start(token: str | None = None,
                authorization: str | None = Header(default=None)) -> dict:
    """Create today's daily race for the player and bind their identity so the
    finish is recorded server-side (one official attempt per UTC day)."""
    user = _require_user(token, authorization)
    date = _today()
    prompt = _daily_prompt(date)
    if prompt is None:
        raise HTTPException(503, "No prompts available - build the snapshot first.")
    _sweep_races()
    start, target = normalize_title(prompt["start"]), normalize_title(prompt["target"])
    race = ExtRace(
        race_id=uuid.uuid4().hex, start=start, target=target,
        difficulty="daily", optimal_hops=int(prompt["par"] or 0), current=start,
        daily_date=date, daily_user=user["id"], daily_username=user.get("username"),
    )
    EXT_RACES[race.race_id] = race
    body = _ext_state(race)
    body["start_url"] = wiki_url(race.start)
    body["already_played"] = accounts.daily_result(date, user["id"]) is not None
    return body


@app.get("/api/ext/daily/board")
def daily_board(date: str | None = None, limit: int = 50) -> dict:
    d = date or _today()
    return {"date": d, "board": accounts.daily_board(d, limit),
            "players": accounts.daily_count(d)}


# ---- weekly puzzle --------------------------------------------------------

def _this_week() -> str:
    """ISO week key (e.g. "2026-W23") so everyone shares one puzzle per week."""
    y, w, _ = datetime.datetime.now(datetime.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def _weekly_prompt(week: str) -> dict | None:
    """Deterministic hand-picked route for an ISO week. Biases toward HARD prompts
    (the weekly puzzle should be devilish); falls back to the full pool if the
    snapshot has no hard prompts. No per-week state - just a hash of the week."""
    pool = prompts.prompts
    if not pool:
        return None
    hard = [p for p in pool if (p.get("difficulty") == "hard")]
    chosen_pool = hard or pool
    seed = int(hashlib.sha256(("weekly:" + week).encode("utf-8")).hexdigest(), 16)
    p = chosen_pool[seed % len(chosen_pool)]
    par = _par_hops(p["start"], p["target"]) or int(p.get("hops") or 0)
    return {"start": p["start"], "target": p["target"], "par": int(par or 0)}


def _record_weekly_if_due(race: "ExtRace") -> None:
    """On a finished weekly-puzzle race, store the player's first official attempt."""
    if not race.weekly_week or not race.weekly_user:
        return
    try:
        res = accounts.record_weekly_result(
            race.weekly_week, race.weekly_user, race.weekly_username,
            start=race.start, target=race.target, clicks=race.clicks,
            time_ms=race.elapsed_ms, flagged=race.flagged, finished=race.finished,
        )
        # Count the weekly once, on the official (first) finish, for season stats.
        if res.get("recorded") and race.finished:
            accounts.add_season_stats(race.weekly_user, inc={"weekly_finished": 1})
    except Exception:
        pass


@app.get("/api/ext/weekly")
def weekly_now(token: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
    week = _this_week()
    prompt = _weekly_prompt(week)
    if prompt is None:
        raise HTTPException(503, "No prompts available - build the snapshot first.")
    out: dict = {"week": week, "start": prompt["start"], "target": prompt["target"],
                 "players": accounts.weekly_count(week)}
    if prompt["par"]:
        out["par"] = prompt["par"]
    user = accounts.user_by_token(_bearer(token, authorization))
    if user:
        out["your_result"] = accounts.weekly_result(week, user["id"])
    return out


@app.post("/api/ext/weekly/start")
def weekly_start(token: str | None = None,
                 authorization: str | None = Header(default=None)) -> dict:
    """Create this week's puzzle race for the player; the finish is recorded
    server-side (one official attempt per ISO week)."""
    user = _require_user(token, authorization)
    week = _this_week()
    prompt = _weekly_prompt(week)
    if prompt is None:
        raise HTTPException(503, "No prompts available - build the snapshot first.")
    _sweep_races()
    start, target = normalize_title(prompt["start"]), normalize_title(prompt["target"])
    race = ExtRace(
        race_id=uuid.uuid4().hex, start=start, target=target,
        difficulty="weekly", optimal_hops=int(prompt["par"] or 0), current=start,
        weekly_week=week, weekly_user=user["id"], weekly_username=user.get("username"),
    )
    EXT_RACES[race.race_id] = race
    body = _ext_state(race)
    body["start_url"] = wiki_url(race.start)
    body["already_played"] = accounts.weekly_result(week, user["id"]) is not None
    return body


@app.get("/api/ext/weekly/board")
def weekly_board(week: str | None = None, limit: int = 50) -> dict:
    w = week or _this_week()
    return {"week": w, "board": accounts.weekly_board(w, limit),
            "players": accounts.weekly_count(w)}


# ---- seasons --------------------------------------------------------------

SEASON_LENGTH_DAYS = 49  # 7-week seasons (spec: 6-8 wks)


def _season_payload(s: dict) -> dict:
    """Decorate a season row with how far through it is, for the UI chip."""
    started = s.get("started_at") or time.time()
    day = max(1, int((time.time() - started) // 86400) + 1)
    return {**s, "day": day, "length_days": SEASON_LENGTH_DAYS,
            "ends_at": started + SEASON_LENGTH_DAYS * 86400}


@app.get("/api/ext/season")
def season_current() -> dict:
    return {"season": _season_payload(accounts.current_season())}


@app.get("/api/ext/season/list")
def season_list() -> dict:
    return {"seasons": accounts.list_seasons()}


@app.get("/api/ext/season/standings")
def season_standings(id: int, limit: int = 100) -> dict:
    return {"season_id": id, "standings": accounts.season_standings(id, limit)}


@app.get("/api/ext/me/season-stats")
def my_season_stats(season_id: int | None = None, token: str | None = None,
                    authorization: str | None = Header(default=None)) -> dict:
    """The signed-in player's stats for one season (the active one by default)
    plus their full per-season history, newest first, for later analytics."""
    user = _require_user(token, authorization)
    return {
        "current": accounts.season_stats(user["id"], season_id),
        "history": accounts.user_season_history(user["id"]),
    }


class RolloverReq(BaseModel):
    token: str
    label: str | None = None


@app.post("/api/ext/admin/season/rollover")
def season_rollover(req: RolloverReq) -> dict:
    """Admin-only: end the current season and start a fresh one (archives final
    standings + soft-resets every player). Gated by WIKIRYVALS_ADMIN_TOKEN so it
    can't be triggered by a normal client."""
    admin = os.environ.get("WIKIRYVALS_ADMIN_TOKEN")
    if not admin or req.token != admin:
        raise HTTPException(403, "Admin token required.")
    cur = accounts.current_season()
    label = req.label or f"Season {cur['id'] + 1}"
    return accounts.rollover_season(label)


# ---- admin dashboard: account tagging -------------------------------------
# Gated by the caller's own session + the is_admin flag on their account (set
# via `python -m wikirace.admin grant-admin`). Non-admins get 403 and the
# dashboard entry never shows for them.

class AdminTagReq(BaseModel):
    token: str | None = None
    user_id: str
    tag: str


@app.get("/api/ext/admin/accounts")
def admin_accounts(q: str = "", token: str | None = None,
                   authorization: str | None = Header(default=None)) -> dict:
    _require_admin(token, authorization)
    return {"accounts": accounts.search_accounts(q)}


@app.post("/api/ext/admin/tag")
def admin_tag(req: AdminTagReq,
              authorization: str | None = Header(default=None)) -> dict:
    admin = _require_admin(req.token, authorization)
    try:
        res = accounts.add_tag(req.user_id, req.tag,
                               added_by=admin["username"] or admin["id"])
    except AccountError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **res}


@app.post("/api/ext/admin/untag")
def admin_untag(req: AdminTagReq,
                authorization: str | None = Header(default=None)) -> dict:
    admin = _require_admin(req.token, authorization)
    try:
        res = accounts.remove_tag(req.user_id, req.tag)
    except AccountError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **res}


# ---- ranked matchmaking ---------------------------------------------------

class EnqueueReq(BaseModel):
    token: str
    difficulty: str | None = "any"


class TicketReq(BaseModel):
    ticket_id: str


class BindReq(BaseModel):
    token: str
    match_id: str
    race_id: str


class ResultReq(BaseModel):
    token: str
    match_id: str
    forfeit: bool = False


@app.post("/api/ext/mm/enqueue")
def mm_enqueue(req: EnqueueReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username before queuing.")
    ticket = matchmaker.enqueue(user, req.difficulty or "any")
    return {"ok": True, "ticket_id": ticket.ticket_id}


@app.get("/api/ext/mm/poll")
def mm_poll(ticket: str) -> dict:
    return matchmaker.poll(ticket)


@app.post("/api/ext/mm/cancel")
def mm_cancel(req: TicketReq) -> dict:
    matchmaker.cancel(req.ticket_id)
    return {"ok": True}


@app.post("/api/ext/mm/duo/enqueue")
def mm_duo_enqueue(req: EnqueueReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username before queuing.")
    ticket = duo_matchmaker.enqueue(user, req.difficulty or "any")
    return {"ok": True, "ticket_id": ticket.ticket_id}


@app.get("/api/ext/mm/duo/poll")
def mm_duo_poll(ticket: str) -> dict:
    return duo_matchmaker.poll(ticket)


@app.post("/api/ext/mm/duo/cancel")
def mm_duo_cancel(req: TicketReq) -> dict:
    duo_matchmaker.cancel(req.ticket_id)
    return {"ok": True}


@app.post("/api/ext/mm/bind")
def mm_bind(req: BindReq) -> dict:
    user = _require_user(req.token)
    # Harmless to call both: each no-ops if it doesn't own the match.
    matchmaker.bind_race(req.match_id, user["id"], req.race_id)
    duo_matchmaker.bind_race(req.match_id, user["id"], req.race_id)
    return {"ok": True}


@app.get("/api/ext/mm/match/{match_id}")
def mm_match(match_id: str, token: str | None = None,
             authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    m = matchmaker.get_match(match_id, user["id"]) or \
        duo_matchmaker.get_match(match_id, user["id"])
    if m is None:
        raise HTTPException(404, "Unknown match.")
    return m


@app.post("/api/ext/mm/result")
def mm_result(req: ResultReq) -> dict:
    user = _require_user(req.token)
    existing = RANKED_RESULTS.get(req.match_id, {}).get(user["id"])
    if existing:
        return existing
    is_duo = duo_matchmaker.has_match(req.match_id)
    maker = duo_matchmaker if is_duo else matchmaker
    rid = maker.race_of(req.match_id, user["id"])
    race = EXT_RACES.get(rid) if rid else None
    if race is None:
        raise HTTPException(400, "No race is bound to this match yet.")
    # Only commit a result once the player has actually finished (or is forfeiting).
    # Submitting early would resolve a ghost match instantly as a DNF, since the
    # bot side is always "submitted". Until then, report live progress.
    if not race.finished and not req.forfeit:
        return {"status": "racing", "clicks": race.clicks, "time_ms": race.elapsed_ms}
    resolution = maker.submit(
        req.match_id, user["id"],
        finished=race.finished, clicks=race.clicks,
        time_ms=race.elapsed_ms, flagged=race.flagged,
    )
    if resolution is None:
        return {"status": "waiting"}
    (_finalize_duo if is_duo else _finalize)(resolution)
    return RANKED_RESULTS.get(req.match_id, {}).get(user["id"], {"status": "waiting"})


def _finalize(resolution: dict) -> None:
    """Apply ratings/RP + persist for every human side of a resolved match, and
    cache each player's result payload. Idempotent per match."""
    mid = resolution["match_id"]
    if mid in RANKED_RESULTS:
        return
    RANKED_RESULTS[mid] = {}
    for key in ("a", "b"):
        side = resolution[key]["side"]
        if side.user_id is None:  # ghost opponent - nothing to persist
            continue
        RANKED_RESULTS[mid][side.user_id] = _build_result(resolution, key)


def _build_result(resolution: dict, key: str, *,
                  rating_opp: Rating | None = None,
                  rp_delta_override: int | None = None) -> dict:
    me = resolution[key]["side"]
    score = resolution[key]["score"]
    other_key = "b" if key == "a" else "a"
    opp = resolution[other_key]["side"]
    mode = resolution["mode"]
    is_ranked = mode in ("ranked", "ranked_duo")
    par = resolution["par"]
    won = score == 1.0
    draw = score == 0.5
    result_word = "win" if won else ("draw" if draw else "loss")

    user = accounts.get_user(me.user_id) or {}
    in_placements = bool(user.get("in_placements", True))
    pre = Rating(me.rating.rating, me.rating.rd, me.rating.vol)
    # For duos, rate each player against the *opposing team's* average strength.
    rate_against = rating_opp or opp.rating
    pre_rp = me.rp
    pre_rank = rank_for_rp(pre_rp)

    if is_ranked:
        new_rating = rate_1v1(pre, rate_against, score)
        if rp_delta_override is not None:
            rp_delta = rp_delta_override
        elif draw:
            rp_delta = 0
        else:
            rp_delta = compute_rp(
                pre, rate_against, won,
                clicks=me.clicks, par=par,
                time_ms=me.time_ms, opp_time_ms=opp.time_ms,
                clean=not me.flagged, placement=in_placements,
            ).delta
        updated = accounts.apply_result(
            me.user_id, new_rating, rp_delta, won,
            time_ms=me.time_ms if won else None,
            flagged=me.flagged, is_placement=True,
        )
        post_rp = updated["rp"]
        # The promo gate can pin RP (hold at 99%) so the *effective* change can
        # differ from the computed reward; report what actually moved.
        rp_delta = post_rp - pre_rp
        rating_after = new_rating.rating
    else:  # private / unranked
        rp_delta = 0
        post_rp = pre_rp
        rating_after = pre.rating
        updated = user

    post_rank = rank_for_rp(post_rp)
    accounts.record_match(
        user_id=me.user_id, opponent=opp.username,
        opponent_bot=1 if opp.is_bot else 0, mode=mode,
        start=resolution["start"], target=resolution["target"], par=par,
        difficulty=resolution["difficulty"], result=result_word,
        clicks=me.clicks, time_ms=me.time_ms,
        opp_clicks=opp.clicks, opp_time_ms=opp.time_ms,
        rp_delta=rp_delta, rating_before=pre.rating, rating_after=rating_after,
        rp_before=pre_rp, rp_after=post_rp, flagged=1 if me.flagged else 0,
    )

    # Per-season analytics: tally this competitive result onto the player's
    # current-season stat row. Best-effort - never let stats break the result.
    if is_ranked:
        fmt = "duo" if mode == "ranked_duo" else "ranked"
        promo = updated.get("_promo") or {}
        inc = {
            "games": 1, "wins": int(won), "draws": int(draw),
            "losses": int(not won and not draw),
            f"{fmt}_games": 1, f"{fmt}_wins": int(won),
            f"{fmt}_losses": int(not won and not draw),
            "rp_gained": max(0, rp_delta), "rp_lost": max(0, -rp_delta),
            "flags": int(bool(me.flagged)),
            "clean_wins": int(won and not me.flagged),
            "total_clicks": me.clicks or 0, "total_time_ms": me.time_ms or 0,
            "promos_won": int(bool(promo.get("won"))),
            "promos_lost": int(bool(promo.get("lost"))),
        }
        peak = {"peak_rp": post_rp, "peak_rating": round(rating_after, 1)}
        streak_now = updated.get("streak") or 0
        if streak_now > 0:
            peak["best_win_streak"] = streak_now
        low = {"fastest_win_ms": me.time_ms} if (won and me.time_ms) else None
        try:
            accounts.add_season_stats(me.user_id, inc=inc, peak=peak, low=low)
        except Exception:
            pass

    placed = is_ranked and not updated.get("in_placements", in_placements)
    show_rank = is_ranked and placed
    return {
        "status": "resolved",
        "mode": mode,
        "ranked": is_ranked,
        "result": result_word,
        "won": won,
        "draw": draw,
        "start": resolution["start"],
        "target": resolution["target"],
        "par": par,
        "difficulty": resolution["difficulty"],
        "you": me.public(),
        "opponent": opp.public(),
        "rp": {"delta": rp_delta, "before": pre_rp, "after": post_rp,
               "in_placements": bool(updated.get("in_placements", in_placements)),
               "placements_left": int(updated.get("placements_left", 0))},
        "rating": {"before": round(pre.rating), "after": round(rating_after),
                   "delta": round(rating_after - pre.rating)},
        "rank": {
            "before": pre_rank.name if is_ranked else None,
            "after": post_rank.name if show_rank else None,
            "slug_before": pre_rank.slug,
            "slug_after": post_rank.slug,
            "division_before": pre_rank.division,
            "division_after": post_rank.division,
            "rp_into": post_rank.rp_into, "rp_span": post_rank.rp_span,
            "next_name": post_rank.next_name, "rp_to_next": post_rank.rp_to_next,
            "promoted": show_rank and post_rank.floor_rp > pre_rank.floor_rp,
            "demoted": show_rank and post_rank.floor_rp < pre_rank.floor_rp,
            "hidden": is_ranked and not show_rank,
        },
        "promo": _promo_payload(updated, show_rank),
    }


def _promo_payload(updated: dict, show_rank: bool) -> dict:
    """Surface the CS2-style promo series to the results card: whether this game
    started a promo (pinned to 99%), or was the promo game itself (won/lost),
    and which rank is on the line."""
    p = updated.get("_promo") or {}
    target_rp = p.get("target_rp")
    target_name = rank_for_rp(target_rp).name if target_rp is not None else None
    entered = bool(p.get("entered")) and show_rank
    return {
        "entered": entered,
        "won": bool(p.get("won")) and show_rank,
        "lost": bool(p.get("lost")) and show_rank,
        "in_promo": bool(p.get("in_promo")) and show_rank,
        "target_name": target_name if (entered or p.get("in_promo")) else None,
    }


def _avg_rating(sides: list[Side]) -> Rating:
    n = max(1, len(sides))
    return Rating(
        rating=sum(s.rating.rating for s in sides) / n,
        rd=sum(s.rating.rd for s in sides) / n,
        vol=sum(s.rating.vol for s in sides) / n,
    )


def _synth_team_opponent(opp_team: list[Side]) -> Side:
    """Collapse the opposing duo into one Side (avg rating, best clean finisher's
    performance) so the per-player result + rating call can treat it like a 1v1
    opponent."""
    best = _team_best(opp_team)
    avg = _avg_rating(opp_team)
    return Side(
        user_id=None,
        username=" & ".join(s.username for s in opp_team),
        rating=avg,
        rp=int(round(sum(s.rp for s in opp_team) / max(1, len(opp_team)))),
        is_bot=all(s.is_bot for s in opp_team),
        submitted=True,
        finished=best is not None,
        clicks=best.clicks if best else None,
        time_ms=best.time_ms if best else None,
        flagged=False,
    )


def _shared_team_rp(team: list[Side], opp_team: list[Side], score: float,
                    par: int) -> int:
    """One RP delta for the whole team (avg rating vs opp avg, team's best run),
    so partners win or lose together by the same amount."""
    if score == 0.5:
        return 0
    won = score == 1.0
    best = _team_best(team)
    opp_best = _team_best(opp_team)
    team_avg = _avg_rating([s for s in team if s.user_id is not None] or team)
    opp_avg = _avg_rating(opp_team)
    placement = any(
        bool((accounts.get_user(s.user_id) or {}).get("in_placements", True))
        for s in team if s.user_id is not None
    )
    return compute_rp(
        team_avg, opp_avg, won,
        clicks=best.clicks if best else None, par=par,
        time_ms=best.time_ms if best else None,
        opp_time_ms=opp_best.time_ms if opp_best else None,
        clean=best is not None, placement=placement,
    ).delta


def _finalize_duo(resolution: dict) -> None:
    """Apply ratings/RP + persist for every human in a resolved 2v2, caching each
    player's result card (with teammate + both opponents). Idempotent per match."""
    mid = resolution["match_id"]
    if mid in RANKED_RESULTS:
        return
    RANKED_RESULTS[mid] = {}
    team_a: list[Side] = resolution["team_a"]
    team_b: list[Side] = resolution["team_b"]
    a_score: float = resolution["a_score"]
    par = resolution["par"]
    for team, opp_team, score in ((team_a, team_b, a_score),
                                  (team_b, team_a, 1.0 - a_score)):
        synth_opp = _synth_team_opponent(opp_team)
        opp_avg = _avg_rating(opp_team)
        shared_rp = _shared_team_rp(team, opp_team, score, par)
        for side in team:
            if side.user_id is None:  # ghost teammate
                continue
            teammate = next((s for s in team if s is not side), None)
            sub = {
                "match_id": mid, "mode": "ranked_duo",
                "start": resolution["start"], "target": resolution["target"],
                "par": par, "difficulty": resolution["difficulty"],
                "a": {"side": side, "score": score},
                "b": {"side": synth_opp, "score": 1.0 - score},
            }
            payload = _build_result(sub, "a", rating_opp=opp_avg,
                                    rp_delta_override=shared_rp)
            payload["team_kind"] = "duo"
            payload["teammate"] = teammate.public() if teammate else None
            payload["opponents"] = [s.public() for s in opp_team]
            RANKED_RESULTS[mid][side.user_id] = payload


# ---- realtime routing (live opponent progress + instant resolution) -------

def _route_race_update(race: "ExtRace", *, is_hop: bool) -> None:
    """If this race is bound to a live match, log the hop for replay, push the
    player's live position to the opponent, and auto-resolve the match the
    instant this side finishes (server-authoritative, no client poll needed)."""
    link = matchmaker.match_for_race(race.race_id)
    is_duo = False
    if link is None:
        link = duo_matchmaker.match_for_race(race.race_id)
        is_duo = link is not None
    if link is None:
        return
    match_id, user_id = link
    if is_hop:
        try:
            accounts.append_match_event(
                match_id, user_id, seq=race.clicks, title=race.current,
                clicks=race.clicks, flagged=race.flagged, finished=race.finished,
            )
        except Exception:
            pass
    # Opponent sees current article + click count + clock, never the full path.
    hub.publish(match_id, {
        "type": "progress", "user_id": user_id,
        "current": race.current, "clicks": race.clicks,
        "elapsed_ms": race.elapsed_ms, "finished": race.finished,
        "flagged": race.flagged,
    })
    if race.finished:
        _auto_resolve(match_id, user_id, race, is_duo=is_duo)


def _auto_resolve(match_id: str, user_id: str, race: "ExtRace",
                  *, is_duo: bool = False) -> None:
    """Submit this side's authoritative result; if the match decides, finalize
    and push the per-side result cards to every player over the channel."""
    if match_id in RANKED_RESULTS:
        # Already resolved (e.g. someone finished first) - re-push for late joiners.
        hub.publish(match_id, {"type": "resolved", "results": RANKED_RESULTS[match_id]})
        return
    maker = duo_matchmaker if is_duo else matchmaker
    resolution = maker.submit(
        match_id, user_id, finished=race.finished, clicks=race.clicks,
        time_ms=race.elapsed_ms, flagged=race.flagged,
    )
    if resolution is None:
        return  # still waiting on other human player(s) to finish
    (_finalize_duo if is_duo else _finalize)(resolution)
    hub.publish(match_id, {"type": "resolved",
                           "results": RANKED_RESULTS.get(match_id, {})})


@app.websocket("/api/ext/ws/match/{match_id}")
async def ws_match(websocket: WebSocket, match_id: str, token: str | None = None) -> None:
    """Live channel for a single match. Auth via ?token=; we only attach the
    socket to its room and relay server-pushed events (progress / resolved).
    Inbound client messages are ignored (the race tab reports via /visit)."""
    user = accounts.user_by_token(token or "")
    uid = user["id"] if user else None
    m = matchmaker.get_match(match_id, uid) or duo_matchmaker.get_match(match_id, uid)
    if user is None or m is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.connect(match_id, websocket)
    try:
        # If the match already resolved before this socket connected, send it now.
        if match_id in RANKED_RESULTS:
            await websocket.send_json({"type": "resolved", "results": RANKED_RESULTS[match_id]})
        while True:
            await websocket.receive_text()  # keepalive / ignored
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(match_id, websocket)


# ---- spectator / watch-party ----------------------------------------------

@app.get("/api/ext/spectate/{match_id}")
def ext_spectate(match_id: str) -> dict:
    """Read-only match metadata for the watch-party page (no auth - anyone with
    the link can watch). Returns players + route; live positions arrive over the
    spectate WebSocket. Includes the final results once the match resolves."""
    meta = matchmaker.spectate(match_id) or duo_matchmaker.spectate(match_id)
    if meta is None:
        raise HTTPException(404, "No such match (it may have ended and been swept).")
    if match_id in RANKED_RESULTS:
        meta["results"] = RANKED_RESULTS[match_id]
    return meta


@app.websocket("/api/ext/ws/spectate/{match_id}")
async def ws_spectate(websocket: WebSocket, match_id: str) -> None:
    """Read-only live feed for spectators. Joins the match's broadcast room so it
    receives every player's ``progress`` and the final ``resolved`` event, but it
    is never counted as a participant and can't influence the match."""
    meta = matchmaker.spectate(match_id) or duo_matchmaker.spectate(match_id)
    if meta is None and match_id not in RANKED_RESULTS:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await hub.connect(match_id, websocket)
    try:
        if match_id in RANKED_RESULTS:
            await websocket.send_json({"type": "resolved", "results": RANKED_RESULTS[match_id]})
        while True:
            await websocket.receive_text()  # spectators send nothing meaningful
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(match_id, websocket)


@app.get("/api/ext/mm/match/{match_id}/events")
def mm_match_events(match_id: str, token: str | None = None,
                    authorization: str | None = Header(default=None)) -> dict:
    """Per-hop replay log for a match (only the requesting player's own hops)."""
    user = _require_user(token, authorization)
    events = [e for e in accounts.match_events(match_id) if e["user_id"] == user["id"]]
    return {"match_id": match_id, "events": events}


# ---- private lobbies ------------------------------------------------------

class LobbyCreateReq(BaseModel):
    token: str
    difficulty: str | None = "any"


class LobbyJoinReq(BaseModel):
    token: str
    code: str


@app.post("/api/ext/lobby/create")
def lobby_create(req: LobbyCreateReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username first.")
    lobby = matchmaker.create_lobby(user, req.difficulty or "any")
    return {"ok": True, "code": lobby.code, "difficulty": lobby.difficulty}


@app.post("/api/ext/lobby/join")
def lobby_join(req: LobbyJoinReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username first.")
    try:
        lobby = matchmaker.join_lobby(req.code, user)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    m = matchmaker.get_match(lobby.match_id, user["id"]) if lobby.match_id else None
    return {"ok": True, "match": m}


@app.get("/api/ext/lobby/poll")
def lobby_poll(code: str, token: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    return matchmaker.poll_lobby(code, user["id"])


# ---- friends --------------------------------------------------------------

class FriendAddReq(BaseModel):
    token: str
    username: str


class FriendRespondReq(BaseModel):
    token: str
    requester_id: str
    accept: bool = True


class FriendRemoveReq(BaseModel):
    token: str
    friend_id: str


def _friends_payload(user_id: str) -> dict:
    """Friends list annotated with who is currently searching/in a duos party."""
    data = accounts.list_friends(user_id)
    searching = _searching_user_ids()
    in_party = _partied_user_ids()
    for card in data["friends"]:
        fid = card["id"]
        card["online"] = fid in searching or fid in in_party
        card["status"] = ("in party" if fid in in_party
                          else "searching" if fid in searching else "offline")
    return data


def _searching_user_ids() -> set[str]:
    ids: set[str] = set()
    for maker in (matchmaker, duo_matchmaker):
        for t in maker._tickets.values():  # noqa: SLF001 (same package)
            if t.status == "searching":
                ids.add(t.user_id)
    return ids


def _partied_user_ids() -> set[str]:
    with DUO_INVITES_LOCK:
        ids: set[str] = set()
        for inv in DUO_INVITES.values():
            if inv["status"] in ("pending", "accepted"):
                ids.add(inv["from_id"])
                if inv["status"] == "accepted":
                    ids.add(inv["to_id"])
        return ids


@app.post("/api/ext/friends/request")
def friends_request(req: FriendAddReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username first.")
    try:
        out = accounts.send_friend_request(user["id"], req.username)
    except AccountError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **out}


@app.post("/api/ext/friends/respond")
def friends_respond(req: FriendRespondReq) -> dict:
    user = _require_user(req.token)
    try:
        out = accounts.respond_friend_request(user["id"], req.requester_id, req.accept)
    except AccountError as e:
        raise HTTPException(400, str(e))
    return out


@app.post("/api/ext/friends/remove")
def friends_remove(req: FriendRemoveReq) -> dict:
    user = _require_user(req.token)
    return accounts.remove_friend(user["id"], req.friend_id)


@app.get("/api/ext/friends")
def friends_list(token: str | None = None,
                 authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    return _friends_payload(user["id"])


# ---- duos party (queue with a friend) -------------------------------------

class PartyInviteReq(BaseModel):
    token: str
    friend_id: str
    difficulty: str | None = "any"


class PartyInviteIdReq(BaseModel):
    token: str
    invite_id: str


def _expire_invites() -> None:
    now = time.time()
    for iid in [i for i, inv in DUO_INVITES.items()
                if inv["status"] == "pending" and now - inv["created_at"] > INVITE_TTL]:
        DUO_INVITES[iid]["status"] = "expired"


def _invite_public(inv: dict) -> dict:
    return {
        "invite_id": inv["id"],
        "party_id": inv["party_id"],
        "from_id": inv["from_id"],
        "from_name": inv["from_name"],
        "to_id": inv["to_id"],
        "to_name": inv["to_name"],
        "difficulty": inv["difficulty"],
        "status": inv["status"],
        "age_ms": int((time.time() - inv["created_at"]) * 1000),
    }


@app.post("/api/ext/party/invite")
def party_invite(req: PartyInviteReq) -> dict:
    user = _require_user(req.token)
    if user.get("needs_username"):
        raise HTTPException(400, "Pick a username first.")
    friend = accounts.get_user(req.friend_id)
    if friend is None:
        raise HTTPException(404, "Unknown player.")
    if not accounts.are_friends(user["id"], req.friend_id):
        raise HTTPException(400, "You can only invite friends.")
    with DUO_INVITES_LOCK:
        _expire_invites()
        # One live invite per inviter: replace any earlier pending one.
        for iid in [i for i, inv in DUO_INVITES.items()
                    if inv["from_id"] == user["id"] and inv["status"] == "pending"]:
            DUO_INVITES[iid]["status"] = "cancelled"
        invite = {
            "id": uuid.uuid4().hex,
            "party_id": uuid.uuid4().hex,
            "from_id": user["id"], "from_name": user["username"],
            "to_id": friend["id"], "to_name": friend["username"],
            "difficulty": req.difficulty or "any",
            "status": "pending", "created_at": time.time(),
            "tickets": {},
        }
        DUO_INVITES[invite["id"]] = invite
        pub = _invite_public(invite)
    return {"ok": True, **pub}


@app.get("/api/ext/party/incoming")
def party_incoming(token: str | None = None,
                   authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    with DUO_INVITES_LOCK:
        _expire_invites()
        out = [_invite_public(inv) for inv in DUO_INVITES.values()
               if inv["to_id"] == user["id"] and inv["status"] == "pending"]
    return {"invites": out}


@app.get("/api/ext/party/poll")
def party_poll(invite: str, token: str | None = None,
               authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    with DUO_INVITES_LOCK:
        _expire_invites()
        inv = DUO_INVITES.get(invite)
        if inv is None:
            return {"status": "expired"}
        pub = _invite_public(inv)
        pub["ticket_id"] = inv["tickets"].get(user["id"])
    return pub


@app.post("/api/ext/party/accept")
def party_accept(req: PartyInviteIdReq) -> dict:
    user = _require_user(req.token)
    with DUO_INVITES_LOCK:
        _expire_invites()
        inv = DUO_INVITES.get(req.invite_id)
        if inv is None or inv["status"] != "pending":
            raise HTTPException(400, "That invite is no longer available.")
        if inv["to_id"] != user["id"]:
            raise HTTPException(403, "This invite isn't for you.")
        inviter = accounts.get_user(inv["from_id"])
        if inviter is None:
            inv["status"] = "expired"
            raise HTTPException(400, "The inviter is no longer available.")
        # Seat both friends as one premade party in the duos queue.
        t_from = duo_matchmaker.enqueue(inviter, inv["difficulty"], party_id=inv["party_id"])
        t_to = duo_matchmaker.enqueue(user, inv["difficulty"], party_id=inv["party_id"])
        inv["tickets"] = {inv["from_id"]: t_from.ticket_id, inv["to_id"]: t_to.ticket_id}
        inv["status"] = "accepted"
    return {"ok": True, "ticket_id": t_to.ticket_id, "party_id": inv["party_id"],
            "difficulty": inv["difficulty"]}


@app.post("/api/ext/party/decline")
def party_decline(req: PartyInviteIdReq) -> dict:
    user = _require_user(req.token)
    with DUO_INVITES_LOCK:
        inv = DUO_INVITES.get(req.invite_id)
        if inv and inv["to_id"] == user["id"] and inv["status"] == "pending":
            inv["status"] = "declined"
    return {"ok": True}


@app.post("/api/ext/party/cancel")
def party_cancel(req: PartyInviteIdReq) -> dict:
    user = _require_user(req.token)
    tickets: list[str] = []
    with DUO_INVITES_LOCK:
        inv = DUO_INVITES.get(req.invite_id)
        if inv and inv["from_id"] == user["id"] and inv["status"] in ("pending", "accepted"):
            inv["status"] = "cancelled"
            tickets = list(inv["tickets"].values())
    # If the pair had already been seated in the queue, drop both tickets.
    for tid in tickets:
        duo_matchmaker.cancel(tid)
    return {"ok": True}


# ---- leaderboard + history ------------------------------------------------

@app.get("/api/ext/leaderboard")
def ext_leaderboard(limit: int = 50) -> dict:
    # Public board: never leak emails.
    entries = [{k: v for k, v in e.items() if k != "email"}
               for e in accounts.leaderboard(min(200, max(1, limit)))]
    return {"entries": entries}


@app.get("/api/ext/history")
def ext_history(token: str | None = None, limit: int = 20,
                authorization: str | None = Header(default=None)) -> dict:
    user = _require_user(token, authorization)
    return {"matches": accounts.history(user["id"], min(100, max(1, limit)))}

