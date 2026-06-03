"""Accounts, sessions, ratings and match history, backed by SQLite.

Auth is **passwordless**: a player enters an email, we mail them a short-lived
6-digit code, they verify it, then pick a username (region is auto-set from their
browser timezone on first login). No passwords to leak, minimal friction for a
competitive crowd.

Everything persistent lives here in one SQLite DB (durable across restarts, same
approach as the play graph). Glicko-2 rating fields, RP, and aggregate stats live
on the ``users`` row; every ranked result is also appended to ``matches`` for
history, leaderboards and dispute replay.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .glicko2 import DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, Rating, season_soft_reset
from .ranks import (
    PLACEMENT_GAMES,
    next_tier_border,
    promo_zone_floor,
    rank_for_rp,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCOUNTS_PATH = DATA_DIR / "accounts.sqlite3"

CODE_TTL_SECONDS = 600.0       # login code valid for 10 minutes
CODE_MAX_ATTEMPTS = 5          # wrong-code guesses before the code is burned
CODE_RESEND_COOLDOWN_SECONDS = 30.0  # min gap between code requests for one email
CODE_MAX_PER_HOUR = 6          # cap codes issued to one email per rolling hour
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

VALID_REGIONS = {"NA", "SA", "EU", "MEA", "APAC", "OCE", "Other"}

# Chrome Web Store review login: a fixed, well-known credential so reviewers can
# sign in without a real inbox. This single email skips code delivery and always
# verifies with REVIEW_CODE; every other address uses the normal emailed-code
# flow. Rotate/override via env if ever needed.
REVIEW_EMAIL = os.environ.get("WIKIRYVALS_REVIEW_EMAIL", "test@googlechromestore.com").strip().lower()
REVIEW_CODE = os.environ.get("WIKIRYVALS_REVIEW_CODE", "451320").strip()


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.lower()}:{code}".encode("utf-8")).hexdigest()


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


# Account tags are lowercase slugs (e.g. 'beta_tester') used to gate perks and
# cosmetics. Kept deliberately strict so they're safe to render and easy to key
# off in code.
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _norm_tag(tag: str) -> str:
    tag = (tag or "").strip().lower()
    if not _TAG_RE.match(tag):
        raise AccountError(
            "Tags must be 1\u201332 chars: lowercase letters, numbers, _ or -.")
    return tag


class AccountError(Exception):
    """Raised for user-facing account problems (bad code, taken username, ...)."""


class RateLimitError(AccountError):
    """Raised when an email requests login codes too often (maps to HTTP 429)."""


class AccountStore:
    def __init__(self, path: Path | str = ACCOUNTS_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Reentrant: _user_dict() reads tags under the lock and is itself called
        # from inside other locked sections (e.g. list_friends).
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    email         TEXT UNIQUE NOT NULL,
                    username      TEXT UNIQUE,
                    region        TEXT,
                    created_at    REAL NOT NULL,
                    rating        REAL NOT NULL,
                    rd            REAL NOT NULL,
                    vol           REAL NOT NULL,
                    rp            INTEGER NOT NULL DEFAULT 0,
                    games         INTEGER NOT NULL DEFAULT 0,
                    wins          INTEGER NOT NULL DEFAULT 0,
                    losses        INTEGER NOT NULL DEFAULT 0,
                    streak        INTEGER NOT NULL DEFAULT 0,
                    best_time_ms  INTEGER,
                    placement_games INTEGER NOT NULL DEFAULT 0,
                    flags         INTEGER NOT NULL DEFAULT 0,
                    -- Daily-play habit streak (distinct from the win/loss `streak`):
                    -- consecutive UTC days the player completed the daily challenge.
                    daily_streak  INTEGER NOT NULL DEFAULT 0,
                    daily_best    INTEGER NOT NULL DEFAULT 0,
                    last_daily    TEXT,
                    -- CS2-style promotion series: when set, the player's next
                    -- ranked game is a promo game guarding the tier border at
                    -- `promo_target_rp` (they're pinned to 99% until they win it).
                    in_promo        INTEGER NOT NULL DEFAULT 0,
                    promo_target_rp INTEGER,
                    -- manual flag: grants access to the admin dashboard
                    is_admin        INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS login_codes (
                    email        TEXT PRIMARY KEY,
                    code_hash    TEXT NOT NULL,
                    expires_at   REAL NOT NULL,
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    -- request rate-limiting (anti email-bomb): timestamp of the
                    -- last issued code + a rolling 1h window counter.
                    last_issued  REAL NOT NULL DEFAULT 0,
                    window_start REAL NOT NULL DEFAULT 0,
                    window_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token      TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id            TEXT PRIMARY KEY,
                    created_at    REAL NOT NULL,
                    user_id       TEXT NOT NULL,
                    opponent      TEXT,
                    opponent_bot  INTEGER NOT NULL DEFAULT 0,
                    mode          TEXT NOT NULL DEFAULT 'ranked',
                    start         TEXT,
                    target        TEXT,
                    par           INTEGER,
                    difficulty    TEXT,
                    result        TEXT,
                    clicks        INTEGER,
                    time_ms       INTEGER,
                    opp_clicks    INTEGER,
                    opp_time_ms   INTEGER,
                    rp_delta      INTEGER,
                    rating_before REAL,
                    rating_after  REAL,
                    rp_before     INTEGER,
                    rp_after      INTEGER,
                    flagged       INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_matches_user ON matches(user_id, created_at DESC);
                -- Live (unresolved) matches, serialized so an in-progress head-to-head
                -- survives a server restart instead of stranding both players.
                CREATE TABLE IF NOT EXISTS active_matches (
                    id         TEXT PRIMARY KEY,
                    blob       TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                -- Per-hop event log for ranked matches (replay / dispute audit).
                CREATE TABLE IF NOT EXISTS match_events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id  TEXT NOT NULL,
                    user_id   TEXT NOT NULL,
                    seq       INTEGER NOT NULL,
                    title     TEXT NOT NULL,
                    clicks    INTEGER NOT NULL DEFAULT 0,
                    flagged   INTEGER NOT NULL DEFAULT 0,
                    finished  INTEGER NOT NULL DEFAULT 0,
                    ts        REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_match ON match_events(match_id, seq);
                -- Daily challenge: one shared route per UTC day; each player's
                -- first finished attempt is their official entry on that day's board.
                CREATE TABLE IF NOT EXISTS daily_results (
                    date       TEXT NOT NULL,
                    user_id    TEXT NOT NULL,
                    username   TEXT,
                    start      TEXT,
                    target     TEXT,
                    clicks     INTEGER,
                    time_ms    INTEGER,
                    flagged    INTEGER NOT NULL DEFAULT 0,
                    finished   INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (date, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_board
                    ON daily_results(date, finished DESC, flagged, time_ms, clicks);
                -- Weekly puzzle: one hand-picked (hard) route per ISO week with a
                -- global board; same one-official-attempt rule as the daily, but
                -- keyed by week (e.g. "2026-W23") and never touches the daily streak.
                CREATE TABLE IF NOT EXISTS weekly_results (
                    week       TEXT NOT NULL,
                    user_id    TEXT NOT NULL,
                    username   TEXT,
                    start      TEXT,
                    target     TEXT,
                    clicks     INTEGER,
                    time_ms    INTEGER,
                    flagged    INTEGER NOT NULL DEFAULT 0,
                    finished   INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (week, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_weekly_board
                    ON weekly_results(week, finished DESC, flagged, time_ms, clicks);
                -- Seasons: a ladder runs in 6-8 week seasons; at rollover every
                -- player's final standing is archived and ratings soft-reset so
                -- veterans keep an edge but everyone gets a fresh climb (spec §5).
                CREATE TABLE IF NOT EXISTS seasons (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    label      TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at   REAL,
                    status     TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS season_standings (
                    season_id  INTEGER NOT NULL,
                    position   INTEGER NOT NULL,
                    user_id    TEXT NOT NULL,
                    username   TEXT,
                    rp         INTEGER NOT NULL,
                    rating     REAL NOT NULL,
                    tier       TEXT,
                    reward     TEXT,
                    PRIMARY KEY (season_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_standings_season
                    ON season_standings(season_id, position);
                -- Per-season rolling stats: one row per (season, player), tallied
                -- live as games resolve and kept forever keyed by season_id (a
                -- rollover just starts writing to the new season). Lets us draw a
                -- season history / analytics later; lifetime totals = SUM over rows.
                CREATE TABLE IF NOT EXISTS user_season_stats (
                    season_id       INTEGER NOT NULL,
                    user_id         TEXT NOT NULL,
                    games           INTEGER NOT NULL DEFAULT 0,
                    wins            INTEGER NOT NULL DEFAULT 0,
                    losses          INTEGER NOT NULL DEFAULT 0,
                    draws           INTEGER NOT NULL DEFAULT 0,
                    ranked_games    INTEGER NOT NULL DEFAULT 0,
                    ranked_wins     INTEGER NOT NULL DEFAULT 0,
                    ranked_losses   INTEGER NOT NULL DEFAULT 0,
                    duo_games       INTEGER NOT NULL DEFAULT 0,
                    duo_wins        INTEGER NOT NULL DEFAULT 0,
                    duo_losses      INTEGER NOT NULL DEFAULT 0,
                    rp_gained       INTEGER NOT NULL DEFAULT 0,
                    rp_lost         INTEGER NOT NULL DEFAULT 0,
                    peak_rp         INTEGER NOT NULL DEFAULT 0,
                    peak_rating     REAL,
                    best_win_streak INTEGER NOT NULL DEFAULT 0,
                    promos_won      INTEGER NOT NULL DEFAULT 0,
                    promos_lost     INTEGER NOT NULL DEFAULT 0,
                    flags           INTEGER NOT NULL DEFAULT 0,
                    clean_wins      INTEGER NOT NULL DEFAULT 0,
                    fastest_win_ms  INTEGER,
                    total_clicks    INTEGER NOT NULL DEFAULT 0,
                    total_time_ms   INTEGER NOT NULL DEFAULT 0,
                    daily_finished  INTEGER NOT NULL DEFAULT 0,
                    weekly_finished INTEGER NOT NULL DEFAULT 0,
                    first_at        REAL,
                    last_at         REAL,
                    PRIMARY KEY (season_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_season_stats_user
                    ON user_season_stats(user_id, season_id);
                -- Friends: a directed request (requester -> addressee) that
                -- becomes a mutual friendship once accepted. Used to party up
                -- for duos; private 1v1 lobbies stay code-based (no list).
                CREATE TABLE IF NOT EXISTS friendships (
                    requester_id TEXT NOT NULL,
                    addressee_id TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted
                    created_at   REAL NOT NULL,
                    PRIMARY KEY (requester_id, addressee_id)
                );
                CREATE INDEX IF NOT EXISTS idx_friend_addressee
                    ON friendships(addressee_id, status);
                CREATE INDEX IF NOT EXISTS idx_friend_requester
                    ON friendships(requester_id, status);
                -- Manual account tags: arbitrary lowercase slugs an admin can
                -- attach to accounts (e.g. 'beta_tester') to gate perks and
                -- cosmetics. One row per (account, tag).
                CREATE TABLE IF NOT EXISTS account_tags (
                    user_id  TEXT NOT NULL,
                    tag      TEXT NOT NULL,
                    added_at REAL NOT NULL,
                    added_by TEXT,
                    PRIMARY KEY (user_id, tag)
                );
                CREATE INDEX IF NOT EXISTS idx_account_tags_tag
                    ON account_tags(tag);
                """
            )
            self._ensure_user_columns()
            self._ensure_login_code_columns()
            self._conn.commit()

    def _ensure_login_code_columns(self) -> None:
        """Lightweight migration: add rate-limit columns to an older login_codes
        table. Caller holds the lock."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(login_codes)").fetchall()}
        wanted = {
            "last_issued": "REAL NOT NULL DEFAULT 0",
            "window_start": "REAL NOT NULL DEFAULT 0",
            "window_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, decl in wanted.items():
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE login_codes ADD COLUMN {name} {decl}")

    def _ensure_user_columns(self) -> None:
        """Lightweight migration: add any user columns missing from an older DB.
        (CREATE TABLE IF NOT EXISTS won't alter a table that already exists.)
        Caller holds the lock."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(users)").fetchall()}
        wanted = {
            "daily_streak": "INTEGER NOT NULL DEFAULT 0",
            "daily_best": "INTEGER NOT NULL DEFAULT 0",
            "last_daily": "TEXT",
            "in_promo": "INTEGER NOT NULL DEFAULT 0",
            "promo_target_rp": "INTEGER",
            "is_admin": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, decl in wanted.items():
            if name not in cols:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")

    # ---- login codes ------------------------------------------------------
    def issue_login_code(self, email: str) -> str:
        """Create (or replace) a 6-digit login code for ``email`` and return it.
        Caller is responsible for delivery (email in prod, dev surfacing locally)."""
        email = _norm_email(email)
        # Store-review account: hand back the fixed code without storing or
        # rate-limiting anything (it has no real inbox to mail).
        if email == REVIEW_EMAIL:
            return REVIEW_CODE
        if not email or "@" not in email:
            raise AccountError("Enter a valid email address.")
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = time.time()
        with self._lock:
            prev = self._conn.execute(
                "SELECT last_issued, window_start, window_count "
                "FROM login_codes WHERE email=?",
                (email,),
            ).fetchone()
            # Anti email-bomb: a short per-email cooldown plus a rolling hourly cap.
            if prev is not None:
                if now - prev["last_issued"] < CODE_RESEND_COOLDOWN_SECONDS:
                    raise RateLimitError(
                        "Please wait a moment before requesting another code.")
                if now - prev["window_start"] < 3600.0:
                    window_start = prev["window_start"]
                    window_count = prev["window_count"]
                    if window_count >= CODE_MAX_PER_HOUR:
                        raise RateLimitError(
                            "Too many codes requested. Try again later.")
                    window_count += 1
                else:
                    window_start, window_count = now, 1
            else:
                window_start, window_count = now, 1
            self._conn.execute(
                "INSERT INTO login_codes "
                "(email, code_hash, expires_at, attempts, last_issued, "
                " window_start, window_count) "
                "VALUES (?, ?, ?, 0, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
                "code_hash=excluded.code_hash, expires_at=excluded.expires_at, "
                "attempts=0, last_issued=excluded.last_issued, "
                "window_start=excluded.window_start, window_count=excluded.window_count",
                (email, _hash_code(email, code), now + CODE_TTL_SECONDS,
                 now, window_start, window_count),
            )
            self._conn.commit()
        return code

    def _ensure_user_locked(self, email: str):
        """Return the existing user row for ``email`` or create a fresh one (with a
        NULL username, so the caller prompts for one). Caller holds the lock."""
        user = self._conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
        if user is None:
            uid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO users (id, email, username, region, created_at, "
                "rating, rd, vol) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
                (uid, email, time.time(), DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL),
            )
            user = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (uid,)
            ).fetchone()
        return user

    def verify_login_code(self, email: str, code: str) -> dict:
        """Verify a code and return the (created-or-existing) user as a dict.

        New emails get a user row with a NULL username; the caller should detect
        ``needs_username`` and prompt for one via ``set_profile``.
        """
        email = _norm_email(email)
        code = (code or "").strip()
        # Store-review account: accept the fixed code directly, bypassing the
        # emailed-code table. Scoped to this one address; all others fall through
        # to the normal verification below.
        if email == REVIEW_EMAIL:
            if code != REVIEW_CODE:
                raise AccountError("Incorrect code.")
            with self._lock:
                user = self._ensure_user_locked(email)
                self._conn.commit()
            return self._user_dict(user)
        with self._lock:
            row = self._conn.execute(
                "SELECT code_hash, expires_at, attempts FROM login_codes WHERE email=?",
                (email,),
            ).fetchone()
            if row is None:
                raise AccountError("Request a login code first.")
            if time.time() > row["expires_at"]:
                self._conn.execute("DELETE FROM login_codes WHERE email=?", (email,))
                self._conn.commit()
                raise AccountError("That code expired. Request a new one.")
            if row["attempts"] >= CODE_MAX_ATTEMPTS:
                self._conn.execute("DELETE FROM login_codes WHERE email=?", (email,))
                self._conn.commit()
                raise AccountError("Too many attempts. Request a new code.")
            if _hash_code(email, code) != row["code_hash"]:
                self._conn.execute(
                    "UPDATE login_codes SET attempts=attempts+1 WHERE email=?", (email,)
                )
                self._conn.commit()
                raise AccountError("Incorrect code.")
            # Success: burn the code, create the user if new.
            self._conn.execute("DELETE FROM login_codes WHERE email=?", (email,))
            user = self._ensure_user_locked(email)
            self._conn.commit()
        return self._user_dict(user)

    # ---- profile / sessions ----------------------------------------------
    def set_profile(self, user_id: str, username: str, region: str | None) -> dict:
        username = (username or "").strip()
        if not (3 <= len(username) <= 20) or not all(
            c.isalnum() or c in "_-" for c in username
        ):
            raise AccountError("Username must be 3–20 chars (letters, numbers, _ or -).")
        region = region if region in VALID_REGIONS else "Other"
        with self._lock:
            clash = self._conn.execute(
                "SELECT 1 FROM users WHERE username=? AND id<>?", (username, user_id)
            ).fetchone()
            if clash:
                raise AccountError("That username is taken.")
            self._conn.execute(
                "UPDATE users SET username=?, region=? WHERE id=?",
                (username, region, user_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._user_dict(row)

    def username_available(self, username: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE username=?", ((username or "").strip(),)
            ).fetchone()
        return row is None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, user_id, now, now + SESSION_TTL_SECONDS),
            )
            self._conn.commit()
        return token

    def user_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at > ?",
                (token, time.time()),
            ).fetchone()
        return self._user_dict(row) if row else None

    def logout(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self._conn.commit()

    def get_user(self, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._user_dict(row) if row else None

    # ---- ratings / results -----------------------------------------------
    def rating_of(self, user: dict) -> Rating:
        return Rating(rating=user["rating"], rd=user["rd"], vol=user["vol"])

    def apply_result(
        self,
        user_id: str,
        new_rating: Rating,
        rp_delta: int,
        won: bool,
        *,
        time_ms: int | None,
        flagged: bool,
        is_placement: bool,
    ) -> dict:
        """Persist a ranked result onto the user row (rating, RP, aggregate stats).
        RP is floored at 0 so a player can't drop below Iron III.

        Promotion series (CS2-style): once a player is out of placements, a win
        that carries them into the top slice of a tier doesn't auto-promote -
        it pins them to 99% and arms a one-game *promo*. While ``in_promo`` is
        set their next ranked game decides the crossing: a win carries them over
        the tier border, a loss drops the normal amount from the pin (promo never
        shields a loss). The returned dict carries a ``_promo`` summary for the UI."""
        with self._lock:
            u = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if u is None:
                raise AccountError("Unknown user.")
            old_rp = u["rp"]
            placed = u["placement_games"] >= PLACEMENT_GAMES
            was_in_promo = bool(u["in_promo"])
            promo = {"entered": False, "won": False, "lost": False,
                     "in_promo": was_in_promo, "target_rp": u["promo_target_rp"]}

            if not placed or rp_delta == 0:
                # Hidden placements (rank not shown yet) and draws never touch the
                # promo machinery - apply RP plainly, leave any flag untouched.
                rp = max(0, old_rp + int(rp_delta))
                new_in_promo = 1 if was_in_promo else 0
                new_target = u["promo_target_rp"]
            elif was_in_promo:
                # This ranked game *is* the promo game. From the 99% pin a win
                # crosses the border; a loss drops the normal amount. Either way
                # the series is consumed.
                rp = max(0, old_rp + int(rp_delta))
                new_in_promo = 0
                new_target = None
                promo["won"], promo["lost"] = won, not won
                promo["in_promo"] = False
            else:
                tentative = max(0, old_rp + int(rp_delta))
                border = next_tier_border(old_rp)
                if (won and border is not None and old_rp < border
                        and tentative >= promo_zone_floor(border)):
                    # Trip the series: hold at 99% of the tier, arm the promo game.
                    rp = border - 1
                    new_in_promo = 1
                    new_target = border
                    promo.update(entered=True, in_promo=True, target_rp=border)
                else:
                    rp = tentative
                    new_in_promo = 0
                    new_target = None

            streak = (u["streak"] + 1) if won and u["streak"] >= 0 else (
                u["streak"] - 1 if (not won and u["streak"] <= 0) else (1 if won else -1)
            )
            best = u["best_time_ms"]
            if won and time_ms and (best is None or time_ms < best):
                best = time_ms
            self._conn.execute(
                "UPDATE users SET rating=?, rd=?, vol=?, rp=?, games=games+1, "
                "wins=wins+?, losses=losses+?, streak=?, best_time_ms=?, "
                "placement_games=placement_games+?, flags=flags+?, "
                "in_promo=?, promo_target_rp=? WHERE id=?",
                (
                    new_rating.rating, new_rating.rd, new_rating.vol, rp,
                    1 if won else 0, 0 if won else 1, streak, best,
                    1 if is_placement else 0, 1 if flagged else 0,
                    new_in_promo, new_target, user_id,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        out = self._user_dict(row)
        out["_promo"] = promo
        return out

    def record_match(self, **kw) -> str:
        mid = uuid.uuid4().hex
        cols = (
            "id", "created_at", "user_id", "opponent", "opponent_bot", "mode",
            "start", "target", "par", "difficulty", "result", "clicks", "time_ms",
            "opp_clicks", "opp_time_ms", "rp_delta", "rating_before", "rating_after",
            "rp_before", "rp_after", "flagged",
        )
        vals = {
            "id": mid, "created_at": time.time(), "opponent_bot": 0, "mode": "ranked",
            "flagged": 0,
        }
        vals.update(kw)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO matches ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})",
                tuple(vals.get(c) for c in cols),
            )
            self._conn.commit()
        return mid

    # ---- live match persistence (restart recovery + replay) ---------------
    def save_active_match(self, match_id: str, blob: dict) -> None:
        """Upsert the serialized state of a live (unresolved) match."""
        payload = json.dumps(blob, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO active_matches (id, blob, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                (match_id, payload, time.time()),
            )
            self._conn.commit()

    def delete_active_match(self, match_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM active_matches WHERE id=?", (match_id,))
            self._conn.commit()

    def load_active_matches(self, max_age_seconds: float | None = None) -> list[dict]:
        """Return serialized blobs for all live matches (optionally only recent ones),
        so the matchmaker can rehydrate them on startup."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT blob, updated_at FROM active_matches ORDER BY updated_at"
            ).fetchall()
        out: list[dict] = []
        now = time.time()
        for r in rows:
            if max_age_seconds is not None and (now - r["updated_at"]) > max_age_seconds:
                continue
            try:
                out.append(json.loads(r["blob"]))
            except (ValueError, TypeError):
                continue
        return out

    def append_match_event(
        self, match_id: str, user_id: str, seq: int, title: str,
        clicks: int, flagged: bool, finished: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO match_events (match_id, user_id, seq, title, clicks, "
                "flagged, finished, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (match_id, user_id, int(seq), title, int(clicks),
                 1 if flagged else 0, 1 if finished else 0, time.time()),
            )
            self._conn.commit()

    def match_events(self, match_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT match_id, user_id, seq, title, clicks, flagged, finished, ts "
                "FROM match_events WHERE match_id=? ORDER BY seq, id",
                (match_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- daily challenge --------------------------------------------------
    def record_daily_result(self, date: str, user_id: str, username: str | None,
                            start: str, target: str, clicks: int, time_ms: int,
                            flagged: bool, finished: bool) -> dict:
        """Record a player's official daily attempt. The FIRST attempt of the day
        is the one that counts (ranked, one shot); later attempts are practice and
        don't overwrite it. Returns {"recorded": bool, "result": <stored row>}."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM daily_results WHERE date=? AND user_id=?",
                (date, user_id),
            ).fetchone()
            if existing is not None:
                return {"recorded": False, "result": dict(existing)}
            self._conn.execute(
                "INSERT INTO daily_results (date, user_id, username, start, target, "
                "clicks, time_ms, flagged, finished, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, user_id, username, start, target, int(clicks), int(time_ms),
                 1 if flagged else 0, 1 if finished else 0, time.time()),
            )
            # Completing the daily extends the player's daily-play habit streak.
            if finished:
                self._bump_daily_streak_locked(user_id, date)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM daily_results WHERE date=? AND user_id=?",
                (date, user_id),
            ).fetchone()
        return {"recorded": True, "result": dict(row)}

    def _bump_daily_streak_locked(self, user_id: str, date: str) -> None:
        """Extend (or reset) a player's daily-play streak. Counts +1 if they also
        completed yesterday's daily, otherwise restarts at 1. Caller holds the lock
        and commits."""
        row = self._conn.execute(
            "SELECT daily_streak, daily_best, last_daily FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if row is None or row["last_daily"] == date:
            return  # unknown user, or already counted today
        try:
            yesterday = (datetime.date.fromisoformat(date)
                         - datetime.timedelta(days=1)).isoformat()
        except ValueError:
            yesterday = None
        streak = (row["daily_streak"] + 1) if row["last_daily"] == yesterday else 1
        best = max(row["daily_best"] or 0, streak)
        self._conn.execute(
            "UPDATE users SET daily_streak=?, daily_best=?, last_daily=? WHERE id=?",
            (streak, best, date, user_id),
        )

    def daily_streak_state(self, user_id: str, today: str) -> dict:
        """Live streak view: a streak stays alive if the last completed daily was
        today or yesterday, otherwise it's broken (display 0). `at_risk` means the
        streak is alive from yesterday but today's daily isn't done yet."""
        with self._lock:
            row = self._conn.execute(
                "SELECT daily_streak, daily_best, last_daily FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {"streak": 0, "best": 0, "played_today": False, "at_risk": False}
        last = row["last_daily"]
        try:
            yesterday = (datetime.date.fromisoformat(today)
                         - datetime.timedelta(days=1)).isoformat()
        except ValueError:
            yesterday = None
        played_today = last == today
        alive = last == today or last == yesterday
        return {
            "streak": row["daily_streak"] if alive else 0,
            "best": row["daily_best"] or 0,
            "played_today": played_today,
            "at_risk": (last == yesterday),
        }

    def daily_result(self, date: str, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily_results WHERE date=? AND user_id=?",
                (date, user_id),
            ).fetchone()
        return dict(row) if row else None

    def daily_board(self, date: str, limit: int = 50) -> list[dict]:
        """Today's standings: finishers first, clean before flagged, then fastest
        time, then fewest clicks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, clicks, time_ms, flagged, finished FROM daily_results "
                "WHERE date=? ORDER BY finished DESC, flagged ASC, time_ms ASC, clicks ASC "
                "LIMIT ?",
                (date, limit),
            ).fetchall()
        out = []
        for i, r in enumerate(rows):
            d = dict(r)
            d["position"] = i + 1
            out.append(d)
        return out

    def daily_count(self, date: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM daily_results WHERE date=?", (date,),
            ).fetchone()[0]

    # ---- weekly puzzle ----------------------------------------------------
    def record_weekly_result(self, week: str, user_id: str, username: str | None,
                             start: str, target: str, clicks: int, time_ms: int,
                             flagged: bool, finished: bool) -> dict:
        """Record a player's official weekly-puzzle attempt. Like the daily, the
        FIRST attempt of the week is the one that counts; later runs are practice.
        Unlike the daily, this never touches the daily-play streak."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM weekly_results WHERE week=? AND user_id=?",
                (week, user_id),
            ).fetchone()
            if existing is not None:
                return {"recorded": False, "result": dict(existing)}
            self._conn.execute(
                "INSERT INTO weekly_results (week, user_id, username, start, target, "
                "clicks, time_ms, flagged, finished, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (week, user_id, username, start, target, int(clicks), int(time_ms),
                 1 if flagged else 0, 1 if finished else 0, time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM weekly_results WHERE week=? AND user_id=?",
                (week, user_id),
            ).fetchone()
        return {"recorded": True, "result": dict(row)}

    def weekly_result(self, week: str, user_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM weekly_results WHERE week=? AND user_id=?",
                (week, user_id),
            ).fetchone()
        return dict(row) if row else None

    def weekly_board(self, week: str, limit: int = 50) -> list[dict]:
        """This week's standings: finishers first, clean before flagged, then
        fastest time, then fewest clicks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, clicks, time_ms, flagged, finished FROM weekly_results "
                "WHERE week=? ORDER BY finished DESC, flagged ASC, time_ms ASC, clicks ASC "
                "LIMIT ?",
                (week, limit),
            ).fetchall()
        out = []
        for i, r in enumerate(rows):
            d = dict(r)
            d["position"] = i + 1
            out.append(d)
        return out

    def weekly_count(self, week: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM weekly_results WHERE week=?", (week,),
            ).fetchone()[0]

    # ---- seasons ----------------------------------------------------------
    def _season_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {"id": row["id"], "label": row["label"], "started_at": row["started_at"],
                "ended_at": row["ended_at"], "status": row["status"]}

    def _active_season_locked(self) -> sqlite3.Row:
        """Return the active season, creating Season 0 (beta) on first use. Caller holds the lock."""
        row = self._conn.execute(
            "SELECT * FROM seasons WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO seasons(label, started_at, status) VALUES (?,?, 'active')",
                ("Season 0", time.time()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM seasons WHERE status='active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row

    def current_season(self) -> dict:
        with self._lock:
            return self._season_dict(self._active_season_locked())

    def list_seasons(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM seasons ORDER BY id DESC").fetchall()
        return [self._season_dict(r) for r in rows]

    def season_standings(self, season_id: int, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM season_standings WHERE season_id=? ORDER BY position LIMIT ?",
                (season_id, limit),
            ).fetchall()
        return [{"position": r["position"], "username": r["username"], "rp": r["rp"],
                 "rating": r["rating"], "tier": r["tier"], "reward": r["reward"]}
                for r in rows]

    # Only these column names may be bumped through add_season_stats - guards the
    # f-string SQL below against ever interpolating an unexpected identifier.
    _SEASON_STAT_COLS = frozenset({
        "games", "wins", "losses", "draws",
        "ranked_games", "ranked_wins", "ranked_losses",
        "duo_games", "duo_wins", "duo_losses",
        "rp_gained", "rp_lost", "peak_rp", "peak_rating", "best_win_streak",
        "promos_won", "promos_lost", "flags", "clean_wins",
        "fastest_win_ms", "total_clicks", "total_time_ms",
        "daily_finished", "weekly_finished",
    })

    def add_season_stats(self, user_id: str, *, inc: dict | None = None,
                         peak: dict | None = None, low: dict | None = None,
                         season_id: int | None = None) -> None:
        """Roll deltas into a player's current-season stat row (created on first
        touch). ``inc`` adds, ``peak`` keeps the running max, ``low`` keeps the
        running min while ignoring NULL (for fastest-finish style fields). Call
        sites wrap this best-effort so a stats hiccup can never break a result."""
        inc = {k: v for k, v in (inc or {}).items() if v}
        peak = {k: v for k, v in (peak or {}).items() if v is not None}
        low = {k: v for k, v in (low or {}).items() if v is not None}
        for col in (*inc, *peak, *low):
            if col not in self._SEASON_STAT_COLS:
                raise AccountError(f"Unknown season stat column: {col}")
        if not (inc or peak or low):
            return
        now = time.time()
        with self._lock:
            sid = season_id if season_id is not None else self._active_season_locked()["id"]
            self._conn.execute(
                "INSERT OR IGNORE INTO user_season_stats(season_id, user_id, first_at, last_at) "
                "VALUES (?,?,?,?)",
                (sid, user_id, now, now),
            )
            sets, args = ["last_at=?"], [now]
            for col, amt in inc.items():
                sets.append(f"{col}={col}+?"); args.append(amt)
            for col, val in peak.items():
                sets.append(f"{col}=MAX(COALESCE({col},0),?)"); args.append(val)
            for col, val in low.items():
                sets.append(f"{col}=CASE WHEN {col} IS NULL THEN ? ELSE MIN({col},?) END")
                args.extend([val, val])
            args.extend([sid, user_id])
            self._conn.execute(
                f"UPDATE user_season_stats SET {','.join(sets)} "
                "WHERE season_id=? AND user_id=?",
                tuple(args),
            )
            self._conn.commit()

    def _season_stats_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        g, w = d.get("games", 0) or 0, d.get("wins", 0) or 0
        d["win_rate"] = round(w / g, 4) if g else None
        d["net_rp"] = (d.get("rp_gained", 0) or 0) - (d.get("rp_lost", 0) or 0)
        return d

    def season_stats(self, user_id: str, season_id: int | None = None) -> dict | None:
        """A player's stat row for one season (the active season by default)."""
        with self._lock:
            sid = season_id if season_id is not None else self._active_season_locked()["id"]
            row = self._conn.execute(
                "SELECT * FROM user_season_stats WHERE season_id=? AND user_id=?",
                (sid, user_id),
            ).fetchone()
        return self._season_stats_dict(row)

    def user_season_history(self, user_id: str) -> list[dict]:
        """Every season's stat row for a player, newest first (for analytics)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT st.*, s.label AS season_label, s.status AS season_status "
                "FROM user_season_stats st JOIN seasons s ON s.id=st.season_id "
                "WHERE st.user_id=? ORDER BY st.season_id DESC",
                (user_id,),
            ).fetchall()
        return [self._season_stats_dict(r) for r in rows]

    def rollover_season(self, new_label: str) -> dict:
        """End the active season and start a new one. Archives every player's final
        standing (with their end-of-season tier as a cosmetic reward), then soft-resets
        the competitive state: rating compressed toward the mean with RD re-inflated,
        RP halved, streak cleared, and placements reopened for a fast fresh climb."""
        now = time.time()
        with self._lock:
            cur = self._active_season_locked()
            users = self._conn.execute(
                "SELECT * FROM users ORDER BY rp DESC, rating DESC"
            ).fetchall()
            for pos, r in enumerate(users, start=1):
                tier = rank_for_rp(r["rp"]).tier
                self._conn.execute(
                    "INSERT OR REPLACE INTO season_standings"
                    "(season_id, position, user_id, username, rp, rating, tier, reward) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (cur["id"], pos, r["id"], r["username"], r["rp"], r["rating"],
                     tier, f"{tier} ({cur['label']})"),
                )
            self._conn.execute(
                "UPDATE seasons SET status='ended', ended_at=? WHERE id=?", (now, cur["id"]),
            )
            for r in users:
                nr = season_soft_reset(Rating(rating=r["rating"], rd=r["rd"], vol=r["vol"]))
                new_rp = int(round(0.5 * r["rp"]))
                self._conn.execute(
                    "UPDATE users SET rating=?, rd=?, vol=?, rp=?, streak=0, "
                    "placement_games=0, in_promo=0, promo_target_rp=NULL WHERE id=?",
                    (nr.rating, nr.rd, nr.vol, new_rp, r["id"]),
                )
            # user_season_stats need no touch here: keyed by season_id, the ended
            # season's rows stay as permanent history and post-rollover games just
            # start tallying under the new season's id.
            new = self._conn.execute(
                "INSERT INTO seasons(label, started_at, status) VALUES (?,?, 'active')",
                (new_label, now),
            )
            new_row = self._conn.execute(
                "SELECT * FROM seasons WHERE id=?", (new.lastrowid,)
            ).fetchone()
            self._conn.commit()
        return {"ended_season": self._season_dict(cur), "archived": len(users),
                "season": self._season_dict(new_row)}

    def history(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM matches WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def top_rival(self, user_id: str) -> dict | None:
        """The single Ryval we float for a player: the human opponent they've faced
        most often (ties broken by most recent meeting). Ghost/bot opponents are
        ignored. Returns head-to-head record + last-played, or None if no human
        opponents yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT opponent, result, created_at FROM matches "
                "WHERE user_id=? AND opponent_bot=0 AND opponent IS NOT NULL "
                "AND mode='ranked'",
                (user_id,),
            ).fetchall()
            if not rows:
                return None
            agg: dict[str, dict] = {}
            for r in rows:
                name = r["opponent"]
                a = agg.setdefault(name, {"username": name, "games": 0, "wins": 0,
                                          "losses": 0, "last_played": 0.0})
                a["games"] += 1
                if r["result"] == "win":
                    a["wins"] += 1
                elif r["result"] == "loss":
                    a["losses"] += 1
                a["last_played"] = max(a["last_played"], r["created_at"] or 0.0)
            # Most-faced opponent wins the "rival" slot; recency breaks ties.
            rival = max(agg.values(), key=lambda a: (a["games"], a["last_played"]))
            # Decorate with the rival's current rank if they're a known user.
            opp = self._conn.execute(
                "SELECT * FROM users WHERE username=?", (rival["username"],)
            ).fetchone()
        if opp is not None:
            od = self._user_dict(opp)
            rival["rank"] = od["rank"]
            rival["in_placements"] = od["in_placements"]
        return rival

    def leaderboard(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE username IS NOT NULL AND games > 0 "
                "ORDER BY rp DESC, rating DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for i, r in enumerate(rows):
            d = self._user_dict(r)
            d["position"] = i + 1
            out.append(d)
        return out

    # ---- friends ----------------------------------------------------------
    def user_by_username(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username=?", ((username or "").strip(),)
            ).fetchone()
        return self._user_dict(row) if row else None

    def _friendship(self, a: str, b: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM friendships WHERE (requester_id=? AND addressee_id=?) "
            "OR (requester_id=? AND addressee_id=?)",
            (a, b, b, a),
        ).fetchone()

    def send_friend_request(self, requester_id: str, username: str) -> dict:
        """Send a friend request by username. If the named user already has a
        pending request out to ``requester_id``, accept it instead (mutual)."""
        target = self.user_by_username(username)
        if target is None:
            raise AccountError("No player with that username.")
        if target["id"] == requester_id:
            raise AccountError("You can't add yourself.")
        with self._lock:
            existing = self._friendship(requester_id, target["id"])
            if existing is not None:
                if existing["status"] == "accepted":
                    raise AccountError("You're already friends.")
                # Pending already exists in some direction.
                if existing["addressee_id"] == requester_id:
                    # They invited me first -> accept it.
                    self._conn.execute(
                        "UPDATE friendships SET status='accepted' "
                        "WHERE requester_id=? AND addressee_id=?",
                        (target["id"], requester_id),
                    )
                    self._conn.commit()
                    return {"status": "accepted", "friend": self._friend_card(target)}
                raise AccountError("Request already sent.")
            self._conn.execute(
                "INSERT INTO friendships (requester_id, addressee_id, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (requester_id, target["id"], time.time()),
            )
            self._conn.commit()
        return {"status": "pending", "friend": self._friend_card(target)}

    def respond_friend_request(self, user_id: str, requester_id: str, accept: bool) -> dict:
        """Accept or decline a pending request addressed to ``user_id``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM friendships WHERE requester_id=? AND addressee_id=? "
                "AND status='pending'",
                (requester_id, user_id),
            ).fetchone()
            if row is None:
                raise AccountError("No such pending request.")
            if accept:
                self._conn.execute(
                    "UPDATE friendships SET status='accepted' "
                    "WHERE requester_id=? AND addressee_id=?",
                    (requester_id, user_id),
                )
            else:
                self._conn.execute(
                    "DELETE FROM friendships WHERE requester_id=? AND addressee_id=?",
                    (requester_id, user_id),
                )
            self._conn.commit()
        return {"ok": True, "accepted": accept}

    def remove_friend(self, user_id: str, other_id: str) -> dict:
        """Remove a friendship (or cancel a pending request) in either direction."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM friendships WHERE (requester_id=? AND addressee_id=?) "
                "OR (requester_id=? AND addressee_id=?)",
                (user_id, other_id, other_id, user_id),
            )
            self._conn.commit()
        return {"ok": True}

    def are_friends(self, a: str, b: str) -> bool:
        with self._lock:
            row = self._friendship(a, b)
        return bool(row and row["status"] == "accepted")

    def list_friends(self, user_id: str) -> dict:
        """Return accepted friends + pending requests (incoming/outgoing)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM friendships WHERE requester_id=? OR addressee_id=?",
                (user_id, user_id),
            ).fetchall()
            friends, incoming, outgoing = [], [], []
            for r in rows:
                other_id = r["addressee_id"] if r["requester_id"] == user_id else r["requester_id"]
                u = self._conn.execute(
                    "SELECT * FROM users WHERE id=?", (other_id,)
                ).fetchone()
                if u is None:
                    continue
                card = self._friend_card(self._user_dict(u))
                if r["status"] == "accepted":
                    friends.append(card)
                elif r["addressee_id"] == user_id:
                    incoming.append(card)
                else:
                    outgoing.append(card)
        friends.sort(key=lambda c: (c["username"] or "").lower())
        return {"friends": friends, "incoming": incoming, "outgoing": outgoing}

    def _friend_card(self, u: dict) -> dict:
        """Trimmed public view of a user for the friends list."""
        return {
            "id": u["id"],
            "username": u["username"],
            "rp": u["rp"],
            "rating": u["rating"],
            "rank": u["rank"]["name"],
            "rank_slug": u["rank"]["slug"],
            "in_placements": u["in_placements"],
        }

    # ---- tags & admin -----------------------------------------------------
    def find_user(self, ident: str) -> dict | None:
        """Resolve an account by exact email or username (case-insensitive).
        Used by the admin CLI and lookups."""
        ident = (ident or "").strip()
        if not ident:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE email=? OR lower(username)=lower(?)",
                (_norm_email(ident), ident),
            ).fetchone()
        return self._user_dict(row) if row else None

    def tags_for(self, user_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag FROM account_tags WHERE user_id=? ORDER BY tag",
                (user_id,),
            ).fetchall()
        return [r["tag"] for r in rows]

    def add_tag(self, user_id: str, tag: str, added_by: str | None = None) -> dict:
        tag = _norm_tag(tag)
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM users WHERE id=?", (user_id,)
            ).fetchone() is None:
                raise AccountError("No such account.")
            self._conn.execute(
                "INSERT OR IGNORE INTO account_tags (user_id, tag, added_at, added_by) "
                "VALUES (?, ?, ?, ?)",
                (user_id, tag, time.time(), added_by),
            )
            self._conn.commit()
        return {"user_id": user_id, "tags": self.tags_for(user_id)}

    def remove_tag(self, user_id: str, tag: str) -> dict:
        tag = _norm_tag(tag)
        with self._lock:
            self._conn.execute(
                "DELETE FROM account_tags WHERE user_id=? AND tag=?", (user_id, tag)
            )
            self._conn.commit()
        return {"user_id": user_id, "tags": self.tags_for(user_id)}

    def set_admin(self, user_id: str, value: bool) -> dict:
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM users WHERE id=?", (user_id,)
            ).fetchone() is None:
                raise AccountError("No such account.")
            self._conn.execute(
                "UPDATE users SET is_admin=? WHERE id=?",
                (1 if value else 0, user_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return self._user_dict(row)

    def list_admins(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE is_admin=1 ORDER BY lower(username)"
            ).fetchall()
        return [self._admin_card(r) for r in rows]

    def search_accounts(self, query: str = "", limit: int = 25) -> list[dict]:
        """Admin account search by username/email substring (case-insensitive).
        Empty query returns the most recently created accounts."""
        limit = max(1, min(int(limit or 25), 100))
        with self._lock:
            q = (query or "").strip().lower()
            if q:
                like = f"%{q}%"
                rows = self._conn.execute(
                    "SELECT * FROM users WHERE lower(username) LIKE ? "
                    "OR lower(email) LIKE ? "
                    "ORDER BY (username IS NULL), lower(username) LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._admin_card(r) for r in rows]

    def _admin_card(self, row: sqlite3.Row) -> dict:
        """Account view for the admin dashboard (includes email + tags)."""
        d = self._user_dict(row)
        return {
            "id": d["id"],
            "username": d["username"],
            "email": d["email"],
            "region": d["region"],
            "rp": d["rp"],
            "games": d["games"],
            "rank": d["rank"]["name"],
            "rank_slug": d["rank"]["slug"],
            "in_placements": d["in_placements"],
            "is_admin": d["is_admin"],
            "tags": d["tags"],
            "created_at": row["created_at"],
        }

    # ---- serialization ----------------------------------------------------
    def _user_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        placed = row["placement_games"] >= PLACEMENT_GAMES
        rank = rank_for_rp(row["rp"])
        return {
            "id": row["id"],
            "email": row["email"],
            "username": row["username"],
            "region": row["region"],
            "is_admin": bool(row["is_admin"]),
            "tags": self.tags_for(row["id"]),
            "needs_username": row["username"] is None,
            "rating": round(row["rating"], 1),
            "rd": round(row["rd"], 1),
            "vol": row["vol"],
            "rp": row["rp"],
            "games": row["games"],
            "wins": row["wins"],
            "losses": row["losses"],
            "streak": row["streak"],
            "daily_streak": row["daily_streak"],
            "daily_best": row["daily_best"],
            "last_daily": row["last_daily"],
            "best_time_ms": row["best_time_ms"],
            "flags": row["flags"],
            "placement_games": row["placement_games"],
            "in_placements": not placed,
            "placements_left": max(0, PLACEMENT_GAMES - row["placement_games"]),
            "promo": {
                "in_promo": placed and bool(row["in_promo"]),
                "target_rp": row["promo_target_rp"],
                "target_name": (rank_for_rp(row["promo_target_rp"]).name
                                if (placed and row["in_promo"]
                                    and row["promo_target_rp"] is not None) else None),
            },
            "rank": {
                "name": ("Unranked" if not placed else rank.name),
                "tier": rank.tier,
                "division": rank.division,
                "slug": ("unranked" if not placed else rank.slug),
                "rp_into": rank.rp_into,
                "rp_span": rank.rp_span,
                "rp_to_next": rank.rp_to_next,
                "next_name": rank.next_name,
            },
        }
