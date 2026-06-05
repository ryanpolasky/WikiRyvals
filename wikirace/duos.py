"""2v2 duos matchmaking, built as a parallel system to the 1v1 :class:`MatchMaker`.

Solo and 1v1 are the product's core, so rather than generalize the (a, b) match
model and risk regressing it, duos lives here as its own queue + match type. It
reuses the 1v1 building blocks (:class:`Side`, the rating window, the clean-
finisher scoring rule) so behaviour stays consistent.

Grouping: four near-rated players are split into two balanced teams (snake-seed by
rating). A duos search needs four real players - it keeps searching until then
(no bot/ghost fill). Team result = the team's *fastest clean finisher*; the
team with the lower best time wins (tiebreak fewer clicks), and a flagged finish
can't clinch it. Durable like 1v1: each live match serializes to the accounts DB
(tagged ``"kind": "duo"``) and rehydrates on restart.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .glicko2 import Rating
from .matchmaking import (
    MATCH_TTL,
    RATING_WINDOW_GROWTH,
    RATING_WINDOW_MAX,
    RATING_WINDOW_START,
    TICKET_TTL,
    Side,
    Ticket,
    _band_for_rating,
    _difficulty_ok,
    _score,
    uuid_hex,
)

TEAM_SIZE = 2
MATCH_SIZE = TEAM_SIZE * 2

PromptPicker = Callable[[str | None], "dict | None"]
ParFn = Callable[[str, str], "int | None"]


@dataclass
class DuoMatch:
    match_id: str
    difficulty: str
    start: str
    target: str
    par: int
    team_a: list[Side]
    team_b: list[Side]
    mode: str = "ranked_duo"
    countdown_start: float = field(default_factory=time.time)
    resolved: bool = False
    created_at: float = field(default_factory=time.monotonic)
    last_touch: float = field(default_factory=time.monotonic)

    def all_sides(self) -> list[Side]:
        return [*self.team_a, *self.team_b]

    def human_sides(self) -> list[Side]:
        return [s for s in self.all_sides() if s.user_id is not None]

    def side_for(self, user_id: str) -> Side | None:
        for s in self.all_sides():
            if s.user_id == user_id:
                return s
        return None

    def team_of(self, user_id: str) -> tuple[list[Side], list[Side]] | None:
        """Return (my_team, opp_team) for ``user_id``, or None if not a player."""
        if any(s.user_id == user_id for s in self.team_a):
            return self.team_a, self.team_b
        if any(s.user_id == user_id for s in self.team_b):
            return self.team_b, self.team_a
        return None

    def to_dict(self) -> dict:
        return {
            "kind": "duo",
            "match_id": self.match_id, "mode": self.mode,
            "difficulty": self.difficulty, "start": self.start,
            "target": self.target, "par": self.par,
            "team_a": [s.to_dict() for s in self.team_a],
            "team_b": [s.to_dict() for s in self.team_b],
            "countdown_start": self.countdown_start, "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DuoMatch":
        m = cls(
            match_id=d["match_id"], mode=d.get("mode", "ranked_duo"),
            difficulty=d.get("difficulty", "any"), start=d["start"],
            target=d["target"], par=int(d.get("par") or 0),
            team_a=[Side.from_dict(s) for s in d["team_a"]],
            team_b=[Side.from_dict(s) for s in d["team_b"]],
        )
        m.countdown_start = d.get("countdown_start", m.countdown_start)
        m.resolved = bool(d.get("resolved"))
        return m

    def public(self, me_user_id: str | None = None) -> dict:
        my_team, opp_team = self.team_a, self.team_b
        if me_user_id is not None and any(s.user_id == me_user_id for s in self.team_b):
            my_team, opp_team = self.team_b, self.team_a
        you = next((s for s in my_team if s.user_id == me_user_id), my_team[0])
        teammate = next((s for s in my_team if s is not you), None)

        def pub(side: Side) -> dict:
            # Include the (opaque) user_id so the live HUD can attribute each
            # progress event to the right teammate/opponent.
            d = side.public()
            d["user_id"] = side.user_id
            return d

        return {
            "match_id": self.match_id,
            "mode": self.mode,
            "team_kind": "duo",
            "difficulty": self.difficulty,
            "start": self.start,
            "target": self.target,
            "par": self.par,
            "countdown_start": self.countdown_start,
            "you": pub(you),
            "teammate": pub(teammate) if teammate else None,
            "opponents": [pub(s) for s in opp_team],
            # A representative single opponent so generic UI/log code still works.
            "opponent": pub(opp_team[0]),
            "resolved": self.resolved,
        }


def _team_best(team: list[Side]) -> Side | None:
    """The team's scoring representative: its fastest *clean* finisher, if any."""
    clean = [s for s in team if s.finished and not s.flagged and s.time_ms is not None]
    if not clean:
        return None
    return min(clean, key=lambda s: (s.time_ms, s.clicks if s.clicks is not None else 1 << 62))


def team_score(team_a: list[Side], team_b: list[Side]) -> float:
    """A's result vs B: 1.0 win / 0.5 draw / 0.0 loss. Each team is represented by
    its fastest clean finisher, then the same grace-window rule as 1v1 applies (a
    rep who finished within GRACE_MS of the other can still win on fewer clicks)."""
    a, b = _team_best(team_a), _team_best(team_b)
    if a is not None and b is not None:
        return _score(a, b)
    if a is not None:
        return 1.0
    if b is not None:
        return 0.0
    return 0.5  # neither team finished cleanly


class DuoMatchMaker:
    def __init__(
        self, prompt_picker: PromptPicker, par_fn: ParFn,
        on_persist: Callable[[str, dict], None] | None = None,
        on_forget: Callable[[str], None] | None = None,
    ) -> None:
        self._pick = prompt_picker
        self._par = par_fn
        self._persist = on_persist or (lambda _mid, _blob: None)
        self._forget = on_forget or (lambda _mid: None)
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}
        self._matches: dict[str, DuoMatch] = {}
        self._race_index: dict[str, str] = {}

    # ---- durability -------------------------------------------------------
    def _save(self, m: DuoMatch) -> None:
        try:
            self._persist(m.match_id, m.to_dict())
        except Exception:
            pass

    def restore(self, blobs: list[dict]) -> int:
        n = 0
        with self._lock:
            for blob in blobs:
                if blob.get("kind") != "duo":
                    continue
                try:
                    m = DuoMatch.from_dict(blob)
                except (KeyError, TypeError, ValueError):
                    continue
                if m.resolved:
                    continue
                self._matches[m.match_id] = m
                for side in m.all_sides():
                    if side.race_id:
                        self._race_index[side.race_id] = m.match_id
                n += 1
        return n

    def match_for_race(self, race_id: str) -> tuple[str, str] | None:
        with self._lock:
            mid = self._race_index.get(race_id)
            if not mid:
                return None
            m = self._matches.get(mid)
            if not m:
                return None
            for side in m.all_sides():
                if side.race_id == race_id and side.user_id:
                    return (mid, side.user_id)
            return None

    # ---- queue ------------------------------------------------------------
    def enqueue(self, user: dict, difficulty: str, party_id: str | None = None) -> Ticket:
        with self._lock:
            self._sweep()
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
                party_id=party_id,
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

    def _window(self, tickets: list[Ticket]) -> float:
        wait = max(self._waited(t) for t in tickets)
        return min(RATING_WINDOW_MAX, RATING_WINDOW_START + RATING_WINDOW_GROWTH * wait)

    def _atomic_units(self, tickets: list[Ticket]) -> list[list[Ticket]]:
        """Group searchers into atomic units: a premade party (co-present tickets
        sharing a party_id) is one unit that can never be split across teams; a
        solo is a unit of one. Preserves oldest-first order by first member."""
        by_party: dict[str, list[Ticket]] = {}
        for t in tickets:
            if t.party_id:
                by_party.setdefault(t.party_id, []).append(t)
        units: list[list[Ticket]] = []
        seen: set[str] = set()
        for t in tickets:
            if t.ticket_id in seen:
                continue
            if t.party_id:
                members = by_party.get(t.party_id, [t])
                for m in members:
                    seen.add(m.ticket_id)
                units.append(members)
            else:
                seen.add(t.ticket_id)
                units.append([t])
        return units

    def _try_match(self) -> None:
        """Group four compatible, near-rated searchers into one balanced 2v2.

        Matching is done over *atomic units* (a party stays together), so two
        friends queued as a premade pair are never seated on opposing teams."""
        searching = [t for t in self._tickets.values() if t.status == "searching"]
        searching.sort(key=lambda t: t.enqueued_at)  # oldest first (fairness)
        units = self._atomic_units(searching)
        used: set[int] = set()
        for i, anchor in enumerate(units):
            if i in used or len(anchor) > MATCH_SIZE:
                continue
            group = list(anchor)
            picked = [i]
            for j, cand in enumerate(units):
                if len(group) == MATCH_SIZE:
                    break
                if j in used or j == i or j in picked:
                    continue
                if len(group) + len(cand) > MATCH_SIZE:
                    continue
                if not all(_difficulty_ok(c.difficulty, g.difficulty)
                           for c in cand for g in group):
                    continue
                window = self._window(group + cand)
                if all(abs(c.rating.rating - g.rating.rating) <= window
                       for c in cand for g in group):
                    group += cand
                    picked.append(j)
            if len(group) == MATCH_SIZE:
                used.update(picked)
                self._form_match([Side(t.user_id, t.username, t.rating, t.rp, t.region, tags=t.tags)
                                  for t in group], group)

    def _resolve_difficulty(self, tickets: list[Ticket]) -> str:
        for t in tickets:
            if t.difficulty != "any":
                return t.difficulty
        avg = sum(t.rating.rating for t in tickets) / max(1, len(tickets))
        return _band_for_rating(avg)

    def _form_match(self, sides: list[Side], tickets: list[Ticket],
                    difficulty: str | None = None) -> None:
        difficulty = difficulty or self._resolve_difficulty(tickets)
        start, target, par = self._prompt(difficulty)
        # Pair each human side with its ticket's party_id so premade partners
        # are guaranteed to share a team.
        entries = [(s, tickets[i].party_id if i < len(tickets) else None)
                   for i, s in enumerate(sides)]
        team_a, team_b = _assign_teams(entries)
        match = DuoMatch(
            match_id=uuid_hex(), difficulty=difficulty,
            start=start, target=target, par=par,
            team_a=team_a, team_b=team_b,
        )
        self._matches[match.match_id] = match
        self._save(match)
        for t in tickets:
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

    # ---- match lifecycle --------------------------------------------------
    def get_match(self, match_id: str, user_id: str | None = None) -> dict | None:
        with self._lock:
            m = self._matches.get(match_id)
            return m.public(user_id) if m else None

    def spectate(self, match_id: str) -> dict | None:
        """Read-only, no-perspective view of a duos match for the watch-party."""
        with self._lock:
            m = self._matches.get(match_id)
            if m is None:
                return None

            def p(side: Side) -> dict:
                d = side.public()
                d["user_id"] = side.user_id
                return d

            return {
                "match_id": m.match_id, "kind": "duo", "mode": m.mode,
                "difficulty": m.difficulty, "start": m.start, "target": m.target,
                "par": m.par, "resolved": m.resolved,
                "team_a": [p(s) for s in m.team_a],
                "team_b": [p(s) for s in m.team_b],
                "players": [p(s) for s in m.all_sides()],
            }

    def has_match(self, match_id: str) -> bool:
        with self._lock:
            return match_id in self._matches

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
        """Record a player's result; resolve once every human has submitted."""
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
            pending = [s for s in m.human_sides() if not s.submitted]
            if pending:
                self._save(m)
                return None
            return self._resolve(m)

    def _resolve(self, m: DuoMatch) -> dict:
        m.resolved = True
        try:
            self._forget(m.match_id)
        except Exception:
            pass
        a_score = team_score(m.team_a, m.team_b)
        return {
            "match_id": m.match_id, "mode": m.mode,
            "start": m.start, "target": m.target, "par": m.par,
            "difficulty": m.difficulty,
            "team_a": m.team_a, "team_b": m.team_b,
            "a_score": a_score,
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
                for side in stale.all_sides():
                    if side.race_id:
                        self._race_index.pop(side.race_id, None)
                try:
                    self._forget(mid)
                except Exception:
                    pass

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "searching": sum(1 for t in self._tickets.values() if t.status == "searching"),
                "matches": len(self._matches),
            }


def _balance_teams(sides: list[Side]) -> tuple[list[Side], list[Side]]:
    """Snake-seed four players by rating into two balanced teams: the strongest
    pairs with the weakest, so average team rating is as close as possible."""
    ordered = sorted(sides, key=lambda s: s.rating.rating, reverse=True)
    # ordered = [s0>=s1>=s2>=s3]; team A = strongest + weakest, B = middle two.
    team_a = [ordered[0], ordered[3]]
    team_b = [ordered[1], ordered[2]]
    return team_a, team_b


def _assign_teams(entries: list[tuple[Side, str | None]]) -> tuple[list[Side], list[Side]]:
    """Split four sides into two teams of two, keeping any premade party (sides
    sharing a party_id) together. With no parties, fall back to the rating-
    balanced snake seed so all-solo matches stay fair."""
    groups: dict[str, list[Side]] = {}
    for side, pid in entries:
        # Solos get a unique key so they're never merged.
        key = pid if pid else f"solo-{id(side)}"
        groups.setdefault(key, []).append(side)
    pairs = [g for g in groups.values() if len(g) >= 2]
    if not pairs:
        return _balance_teams([s for s, _ in entries])
    singles = [g[0] for g in groups.values() if len(g) == 1]
    team_a: list[Side] = []
    team_b: list[Side] = []
    for g in pairs:
        (team_a if not team_a else team_b).extend(g[:TEAM_SIZE])
    for s in singles:
        (team_a if len(team_a) < TEAM_SIZE else team_b).append(s)
    return team_a, team_b
