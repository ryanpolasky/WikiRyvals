"""In-memory matchmaking: ranked queue, async ghost fallback, and private lobbies.

The matchmaker is intentionally stateless-on-disk (live state only) - durable
things (ratings, RP, match history) are written by the caller via the account
store once a match resolves. This module just owns the *live* head-to-head:

  * **Ranked queue** - players enqueue with their rating/region/difficulty; we
    pair the closest-rated compatible opponent, widening the rating window the
    longer someone waits so a small pool never deadlocks.
  * **Ghost fallback** - if no human appears within a few seconds, we
    synthesize an *async ghost*: a recorded-style run by a similarly-rated
    phantom so a solo player still gets a real "match found" and a result. Ghost
    games are clearly flagged ``is_bot`` so the UI can mark them.
  * **Private lobbies** - code-based (create -> 6-char code -> opponent joins by
    code), no friends list. Private matches are unranked.

Resolution rule (both modes): a clean finisher beats a DNF; if both finish, the
faster wall-clock wins (tiebreak: fewer clicks); a flagged finish cannot win.
"""

from __future__ import annotations

import random
import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .glicko2 import Rating

# Rating proximity window (RP-of-skill points) for pairing; widens with wait.
RATING_WINDOW_START = 120.0
RATING_WINDOW_GROWTH = 45.0       # per second waited
RATING_WINDOW_MAX = 1400.0
# How long a solo player waits before we give them a ghost.
GHOST_AFTER_SECONDS = 8.0
# Live-state TTLs so memory stays flat on an always-on box.
TICKET_TTL = 120.0
MATCH_TTL = 1800.0
LOBBY_TTL = 1800.0

PromptPicker = Callable[[str | None], dict | None]
ParFn = Callable[[str, str], "int | None"]


def _code(n: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
    return "".join(secrets.choice(alphabet) for _ in range(n))


@dataclass
class Side:
    user_id: str | None
    username: str
    rating: Rating
    rp: int
    region: str = "Other"
    is_bot: bool = False
    race_id: str | None = None
    submitted: bool = False
    finished: bool = False
    clicks: int | None = None
    time_ms: int | None = None
    flagged: bool = False
    tags: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "username": self.username,
            "rating": round(self.rating.rating),
            "rp": self.rp,
            "is_bot": self.is_bot,
            "region": self.region,
            "finished": self.finished,
            "clicks": self.clicks,
            "time_ms": self.time_ms,
            "flagged": self.flagged,
            "tags": list(self.tags),
        }

    def to_dict(self) -> dict:
        """Full serialization (incl. private fields) for restart recovery."""
        return {
            "user_id": self.user_id, "username": self.username,
            "rating": [self.rating.rating, self.rating.rd, self.rating.vol],
            "rp": self.rp, "region": self.region, "is_bot": self.is_bot,
            "race_id": self.race_id, "submitted": self.submitted,
            "finished": self.finished, "clicks": self.clicks,
            "time_ms": self.time_ms, "flagged": self.flagged,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Side":
        r = d.get("rating") or [1500.0, 350.0, 0.06]
        return cls(
            user_id=d.get("user_id"), username=d.get("username", "player"),
            rating=Rating(r[0], r[1], r[2]), rp=int(d.get("rp", 0)),
            region=d.get("region", "Other"), is_bot=bool(d.get("is_bot")),
            race_id=d.get("race_id"), submitted=bool(d.get("submitted")),
            finished=bool(d.get("finished")), clicks=d.get("clicks"),
            time_ms=d.get("time_ms"), flagged=bool(d.get("flagged")),
            tags=list(d.get("tags") or []),
        )


@dataclass
class Match:
    match_id: str
    mode: str                 # "ranked" | "private"
    difficulty: str
    start: str
    target: str
    par: int
    a: Side                   # the enqueuing player (or host)
    b: Side                   # opponent (human or ghost)
    created_at: float = field(default_factory=time.monotonic)
    countdown_start: float = field(default_factory=time.time)
    resolved: bool = False
    last_touch: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "match_id": self.match_id, "mode": self.mode,
            "difficulty": self.difficulty, "start": self.start,
            "target": self.target, "par": self.par,
            "a": self.a.to_dict(), "b": self.b.to_dict(),
            "countdown_start": self.countdown_start, "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Match":
        m = cls(
            match_id=d["match_id"], mode=d.get("mode", "ranked"),
            difficulty=d.get("difficulty", "any"), start=d["start"],
            target=d["target"], par=int(d.get("par") or 0),
            a=Side.from_dict(d["a"]), b=Side.from_dict(d["b"]),
        )
        m.countdown_start = d.get("countdown_start", m.countdown_start)
        m.resolved = bool(d.get("resolved"))
        return m

    def side_for(self, user_id: str) -> Side | None:
        if self.a.user_id == user_id:
            return self.a
        if self.b.user_id == user_id:
            return self.b
        return None

    def other(self, side: Side) -> Side:
        return self.b if side is self.a else self.a

    def public(self, me_user_id: str | None = None) -> dict:
        me, opp = self.a, self.b
        if me_user_id is not None and self.b.user_id == me_user_id:
            me, opp = self.b, self.a
        return {
            "match_id": self.match_id,
            "mode": self.mode,
            "difficulty": self.difficulty,
            "start": self.start,
            "target": self.target,
            "par": self.par,
            "countdown_start": self.countdown_start,
            "you": me.public(),
            "opponent": opp.public(),
            "resolved": self.resolved,
        }


@dataclass
class Ticket:
    ticket_id: str
    user_id: str
    username: str
    rating: Rating
    rp: int
    region: str
    difficulty: str
    enqueued_at: float = field(default_factory=time.monotonic)
    status: str = "searching"      # searching | found | cancelled
    match_id: str | None = None
    # When two friends queue as a premade pair, both tickets share a party_id so
    # duos matchmaking always seats them on the same team. None = solo queue.
    party_id: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Lobby:
    code: str
    difficulty: str
    host: Side
    guest: Side | None = None
    match_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_touch: float = field(default_factory=time.monotonic)


class MatchMaker:
    def __init__(
        self, prompt_picker: PromptPicker, par_fn: ParFn,
        on_persist: Callable[[str, dict], None] | None = None,
        on_forget: Callable[[str], None] | None = None,
    ) -> None:
        self._pick = prompt_picker
        self._par = par_fn
        # Optional durability hooks: persist a live match's serialized state on
        # every change, forget it once resolved. Defaults to no-op (pure memory).
        self._persist = on_persist or (lambda _mid, _blob: None)
        self._forget = on_forget or (lambda _mid: None)
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}
        self._matches: dict[str, Match] = {}
        self._lobbies: dict[str, Lobby] = {}
        # race_id -> match_id, so a race's progress can be routed to its match.
        self._race_index: dict[str, str] = {}

    def _save(self, m: Match) -> None:
        try:
            self._persist(m.match_id, m.to_dict())
        except Exception:
            pass  # durability is best-effort; never break the live match

    def restore(self, blobs: list[dict]) -> int:
        """Rehydrate live (unresolved) matches from serialized blobs on startup.
        Returns the number restored."""
        n = 0
        with self._lock:
            for blob in blobs:
                try:
                    m = Match.from_dict(blob)
                except (KeyError, TypeError, ValueError):
                    continue
                if m.resolved:
                    continue
                self._matches[m.match_id] = m
                for side in (m.a, m.b):
                    if side.race_id:
                        self._race_index[side.race_id] = m.match_id
                n += 1
        return n

    def match_for_race(self, race_id: str) -> tuple[str, str] | None:
        """Return (match_id, user_id) for the side that owns ``race_id``, if any."""
        with self._lock:
            mid = self._race_index.get(race_id)
            if not mid:
                return None
            m = self._matches.get(mid)
            if not m:
                return None
            for side in (m.a, m.b):
                if side.race_id == race_id and side.user_id:
                    return (mid, side.user_id)
            return None

    # ---- ranked queue -----------------------------------------------------
    def enqueue(self, user: dict, difficulty: str) -> Ticket:
        with self._lock:
            self._sweep()
            # Replace any stale ticket for this user.
            for t in [t for t in self._tickets.values() if t.user_id == user["id"]]:
                self._tickets.pop(t.ticket_id, None)
            ticket = Ticket(
                ticket_id=uuid_hex(),
                user_id=user["id"],
                username=user["username"] or "player",
                rating=Rating(user["rating"], user["rd"], user["vol"]),
                rp=user["rp"],
                region=user.get("region") or "Other",
                difficulty=difficulty or "any",
                tags=list(user.get("tags") or []),
            )
            self._tickets[ticket.ticket_id] = ticket
            self._try_match()
            return ticket

    def poll(self, ticket_id: str) -> dict:
        with self._lock:
            self._sweep()
            t = self._tickets.get(ticket_id)
            if t is None:
                return {"status": "expired"}
            if t.status == "searching":
                self._try_match()
                if t.status == "searching" and self._waited(t) >= GHOST_AFTER_SECONDS:
                    self._make_ghost_match(t)
            out: dict = {"status": t.status, "waited_ms": int(self._waited(t) * 1000),
                         "searching": sum(1 for x in self._tickets.values()
                                          if x.status == "searching")}
            if t.status == "found" and t.match_id:
                m = self._matches.get(t.match_id)
                if m:
                    out["match"] = m.public(t.user_id)
            return out

    def cancel(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)

    def _waited(self, t: Ticket) -> float:
        return time.monotonic() - t.enqueued_at

    def _try_match(self) -> None:
        searching = [t for t in self._tickets.values() if t.status == "searching"]
        searching.sort(key=lambda t: t.enqueued_at)  # oldest first (fairness)
        used: set[str] = set()
        for i, a in enumerate(searching):
            if a.ticket_id in used:
                continue
            best: Ticket | None = None
            best_gap = None
            for b in searching[i + 1:]:
                if b.ticket_id in used or b.user_id == a.user_id:
                    continue
                if not _difficulty_ok(a.difficulty, b.difficulty):
                    continue
                window = self._window(a, b)
                gap = abs(a.rating.rating - b.rating.rating)
                if gap <= window and (best_gap is None or gap < best_gap):
                    best, best_gap = b, gap
            if best is not None:
                used.add(a.ticket_id)
                used.add(best.ticket_id)
                self._pair(a, best)

    def _window(self, a: Ticket, b: Ticket) -> float:
        wait = max(self._waited(a), self._waited(b))
        return min(RATING_WINDOW_MAX, RATING_WINDOW_START + RATING_WINDOW_GROWTH * wait)

    def _resolve_difficulty(self, a: Ticket, b: Ticket | None = None) -> str:
        if b is None:
            return a.difficulty if a.difficulty != "any" else _band_for_rating(a.rating.rating)
        if a.difficulty != "any":
            return a.difficulty
        if b.difficulty != "any":
            return b.difficulty
        avg = (a.rating.rating + b.rating.rating) / 2
        return _band_for_rating(avg)

    def _pair(self, a: Ticket, b: Ticket) -> None:
        difficulty = self._resolve_difficulty(a, b)
        start, target, par = self._prompt(difficulty)
        match = Match(
            match_id=uuid_hex(),
            mode="ranked",
            difficulty=difficulty,
            start=start, target=target, par=par,
            a=Side(a.user_id, a.username, a.rating, a.rp, a.region, tags=a.tags),
            b=Side(b.user_id, b.username, b.rating, b.rp, b.region, tags=b.tags),
        )
        self._matches[match.match_id] = match
        self._save(match)
        for t in (a, b):
            t.status = "found"
            t.match_id = match.match_id

    def _make_ghost_match(self, t: Ticket) -> None:
        difficulty = self._resolve_difficulty(t)
        start, target, par = self._prompt(difficulty)
        ghost = _make_ghost(t.rating, t.rp, par)
        match = Match(
            match_id=uuid_hex(),
            mode="ranked",
            difficulty=difficulty,
            start=start, target=target, par=par,
            a=Side(t.user_id, t.username, t.rating, t.rp, t.region, tags=t.tags),
            b=ghost,
        )
        self._matches[match.match_id] = match
        self._save(match)
        t.status = "found"
        t.match_id = match.match_id

    def _prompt(self, difficulty: str) -> tuple[str, str, int]:
        p = self._pick(difficulty) or self._pick("any") or {
            "start": "Albert Einstein", "target": "Philosophy", "hops": 0,
        }
        start, target = p["start"], p["target"]
        par = self._par(start, target)
        if par is None:
            par = int(p.get("hops") or 0)
        return start, target, int(par or 0)

    # ---- private lobbies --------------------------------------------------
    def create_lobby(self, user: dict, difficulty: str) -> Lobby:
        with self._lock:
            self._sweep()
            code = _code()
            while code in self._lobbies:
                code = _code()
            lobby = Lobby(
                code=code,
                difficulty=difficulty or "any",
                host=Side(user["id"], user["username"] or "host",
                          Rating(user["rating"], user["rd"], user["vol"]),
                          user["rp"], user.get("region") or "Other",
                          tags=list(user.get("tags") or [])),
            )
            self._lobbies[code] = lobby
            return lobby

    def join_lobby(self, code: str, user: dict) -> Lobby:
        with self._lock:
            self._sweep()
            lobby = self._lobbies.get((code or "").strip().upper())
            if lobby is None:
                raise KeyError("No lobby with that code.")
            if lobby.match_id is not None:
                raise ValueError("That lobby already started.")
            if lobby.host.user_id == user["id"]:
                raise ValueError("You're the host of that lobby.")
            lobby.guest = Side(
                user["id"], user["username"] or "guest",
                Rating(user["rating"], user["rd"], user["vol"]),
                user["rp"], user.get("region") or "Other",
                tags=list(user.get("tags") or []),
            )
            lobby.last_touch = time.monotonic()
            self._start_lobby(lobby)
            return lobby

    def _start_lobby(self, lobby: Lobby) -> None:
        difficulty = lobby.difficulty if lobby.difficulty != "any" else "medium"
        start, target, par = self._prompt(difficulty)
        match = Match(
            match_id=uuid_hex(),
            mode="private",
            difficulty=lobby.difficulty,
            start=start, target=target, par=par,
            a=lobby.host, b=lobby.guest,  # type: ignore[arg-type]
        )
        self._matches[match.match_id] = match
        self._save(match)
        lobby.match_id = match.match_id

    def poll_lobby(self, code: str, user_id: str) -> dict:
        with self._lock:
            self._sweep()
            lobby = self._lobbies.get((code or "").strip().upper())
            if lobby is None:
                return {"status": "expired"}
            lobby.last_touch = time.monotonic()
            if lobby.match_id:
                m = self._matches.get(lobby.match_id)
                return {
                    "status": "started",
                    "match": m.public(user_id) if m else None,
                }
            return {
                "status": "waiting",
                "code": lobby.code,
                "host": lobby.host.username,
                "difficulty": lobby.difficulty,
            }

    # ---- shared match lifecycle ------------------------------------------
    def get_match(self, match_id: str, user_id: str | None = None) -> dict | None:
        with self._lock:
            m = self._matches.get(match_id)
            return m.public(user_id) if m else None

    def spectate(self, match_id: str) -> dict | None:
        """Read-only, no-perspective view of a match for spectators/watch-party.
        Includes each side's (opaque) user_id so a viewer can attribute live
        progress events to the right player."""
        with self._lock:
            m = self._matches.get(match_id)
            if m is None:
                return None

            def p(side: Side) -> dict:
                d = side.public()
                d["user_id"] = side.user_id
                return d

            return {
                "match_id": m.match_id, "kind": "1v1", "mode": m.mode,
                "difficulty": m.difficulty, "start": m.start, "target": m.target,
                "par": m.par, "resolved": m.resolved,
                "players": [p(m.a), p(m.b)],
            }

    def race_of(self, match_id: str, user_id: str) -> str | None:
        with self._lock:
            m = self._matches.get(match_id)
            if not m:
                return None
            side = m.side_for(user_id)
            return side.race_id if side else None

    def bind_race(self, match_id: str, user_id: str, race_id: str) -> None:
        with self._lock:
            m = self._matches.get(match_id)
            if not m:
                return
            side = m.side_for(user_id)
            if side:
                side.race_id = race_id
                self._race_index[race_id] = match_id
                m.last_touch = time.monotonic()
                self._save(m)

    def submit(
        self, match_id: str, user_id: str, *,
        finished: bool, clicks: int | None, time_ms: int | None, flagged: bool,
    ) -> dict | None:
        """Record a player's result; return a resolution dict once the match can
        be decided (both submitted, or the opponent is a ghost). None while still
        waiting on a human opponent."""
        with self._lock:
            m = self._matches.get(match_id)
            if not m:
                return None
            side = m.side_for(user_id)
            if side is None:
                return None
            m.last_touch = time.monotonic()
            side.submitted = True
            side.finished = bool(finished)
            side.clicks = clicks
            side.time_ms = time_ms
            side.flagged = bool(flagged)
            opp = m.other(side)
            if not opp.submitted and not opp.is_bot:
                self._save(m)  # persist the half-submitted state
                return None  # wait for the human opponent
            return self._resolve(m)

    def _resolve(self, m: Match) -> dict:
        m.resolved = True
        try:
            self._forget(m.match_id)  # it now lives in durable match history
        except Exception:
            pass
        a, b = m.a, m.b
        a_score = _score(a, b)   # 1 win / .5 draw / 0 loss, from a's perspective
        return {
            "match_id": m.match_id,
            "mode": m.mode,
            "start": m.start, "target": m.target, "par": m.par,
            "difficulty": m.difficulty,
            "a": {"side": a, "score": a_score},
            "b": {"side": b, "score": 1.0 - a_score},
        }

    # ---- housekeeping -----------------------------------------------------
    def _sweep(self) -> None:
        now = time.monotonic()
        for tid in [t for t, x in self._tickets.items()
                    if now - x.enqueued_at > TICKET_TTL and x.status != "found"]:
            self._tickets.pop(tid, None)
        for mid in [m for m, x in self._matches.items() if now - x.last_touch > MATCH_TTL]:
            stale = self._matches.pop(mid, None)
            if stale is not None:
                for side in (stale.a, stale.b):
                    if side.race_id:
                        self._race_index.pop(side.race_id, None)
                try:
                    self._forget(mid)
                except Exception:
                    pass
        for code in [c for c, x in self._lobbies.items() if now - x.last_touch > LOBBY_TTL]:
            self._lobbies.pop(code, None)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "searching": sum(1 for t in self._tickets.values() if t.status == "searching"),
                "matches": len(self._matches),
                "lobbies": len(self._lobbies),
            }


def uuid_hex() -> str:
    return secrets.token_hex(12)


def _difficulty_ok(a: str, b: str) -> bool:
    return a == "any" or b == "any" or a == b


def _band_for_rating(rating: float) -> str:
    if rating < 1450:
        return "easy"
    if rating < 1750:
        return "medium"
    return "hard"


def _score(me: Side, opp: Side) -> float:
    """1.0 if `me` beats `opp`, 0.5 draw, 0.0 loss. A flagged finish can't win."""
    me_ok = me.finished and not me.flagged
    opp_ok = opp.finished and not opp.flagged
    if me_ok and opp_ok:
        # Both finished cleanly: faster time wins, tiebreak fewer clicks.
        if (me.time_ms or 1 << 62) < (opp.time_ms or 1 << 62):
            return 1.0
        if (me.time_ms or 0) > (opp.time_ms or 0):
            return 0.0
        if (me.clicks or 1 << 62) < (opp.clicks or 1 << 62):
            return 1.0
        if (me.clicks or 0) > (opp.clicks or 0):
            return 0.0
        return 0.5
    if me_ok and not opp_ok:
        return 1.0
    if opp_ok and not me_ok:
        return 0.0
    return 0.5  # neither finished cleanly -> draw


def _make_ghost(player_rating: Rating, player_rp: int, par: int) -> Side:
    """Synthesize a similarly-rated async ghost opponent and its recorded result.

    The ghost's rating is jittered around the player's; its run quality (clicks
    over par, per-hop reading speed) scales with that rating, with a small chance
    of a slip-up. This gives a believable, beatable opponent for a solo player.
    """
    jitter = random.uniform(-90, 90)
    g_rating = max(800.0, player_rating.rating + jitter)
    # Skill in [0,1]: higher rating -> closer to par and faster reading.
    skill = max(0.05, min(0.95, (g_rating - 1000.0) / 1400.0))

    base_par = max(1, par or random.randint(2, 4))
    # Extra clicks over par: skilled ghosts add fewer.
    extra = 0
    r = random.random()
    if r > 0.55 + 0.35 * skill:
        extra += 1
    if r > 0.85 + 0.13 * skill:
        extra += 1
    g_clicks = base_par + extra

    # Per-hop reading time: ~5.5s for weak, ~2.2s for strong, plus noise.
    per_hop = random.uniform(2.2, 5.5) - 2.6 * skill
    per_hop = max(1.2, per_hop)
    g_time_ms = int(g_clicks * per_hop * 1000 * random.uniform(0.9, 1.15))

    # Rare ghost DNF so wins feel earned, not guaranteed.
    finished = random.random() > 0.04
    name = random.choice(_GHOST_NAMES)
    return Side(
        user_id=None, username=name, rating=Rating(g_rating, 90.0, 0.06),
        rp=max(0, player_rp + int(jitter)), is_bot=True,
        finished=finished, submitted=True,
        clicks=g_clicks if finished else None,
        time_ms=g_time_ms if finished else None,
    )


_GHOST_NAMES = [
    "quasar", "helix", "nebula", "vortex", "cipher", "echo", "drift", "onyx",
    "zephyr", "lumen", "raven", "atlas", "comet", "flux", "ember", "sable",
]
