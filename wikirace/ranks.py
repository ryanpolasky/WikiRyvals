"""Rank ladder + variable Rank Points (RP), layered over the Glicko-2 rating.

Two numbers per player, deliberately separated:

  * **rating** (Glicko-2 MMR) drives *matchmaking* and the *expected score* used
    to scale RP. It's the honest skill estimate and is mostly hidden.
  * **RP** is the visible *ladder* number players climb. It only moves on ranked
    results and the amount is **variable**: beating a favourite or winning
    cleanly (few clicks vs par, big time margin) is worth more than scraping past
    an underdog. Losing to a much weaker opponent stings more. This is what makes
    the climb feel earned rather than a flat ±25.

Ladder (locked with Ryan): Iron, Bronze, Silver, Gold, Platinum, Diamond each
split into three divisions (III, II, I), then the unique apex tiers Featured,
Legend and Ryval. Hitting Ryval = "find your Ryval".
"""

from __future__ import annotations

from dataclasses import dataclass

from .glicko2 import Rating, expected_score

# RP per division; a divisioned tier spans 3 of these.
DIV_RP = 100
# Number of hidden placement matches before a rank is revealed.
PLACEMENT_GAMES = 5

# Divisioned base tiers, low -> high. Each gets divisions III/II/I.
_BASE_TIERS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond"]
# Single-division apex tiers above the base ladder.
_APEX_TIERS = ["Featured", "Legend", "Ryval"]

# Tier -> slug used for icon filenames + CSS classes.
TIER_SLUG = {
    "Iron": "iron", "Bronze": "bronze", "Silver": "silver", "Gold": "gold",
    "Platinum": "plat", "Diamond": "diamond", "Featured": "featured",
    "Legend": "legend", "Ryval": "ryval",
}


@dataclass(frozen=True)
class Rank:
    tier: str            # e.g. "Gold"
    division: int        # 3,2,1 for base tiers; 0 for apex tiers
    name: str            # e.g. "Gold II" or "Ryval"
    slug: str            # e.g. "gold"
    floor_rp: int        # RP at the start of this rank
    next_name: str | None
    rp_to_next: int | None   # RP needed to reach the next rank (None at Ryval)
    rp_into: int             # RP earned into the current rank
    rp_span: int | None      # total RP width of the current rank (None at Ryval)


def _ladder() -> list[tuple[str, int, int]]:
    """Build the ordered ladder as (name, division, floor_rp) entries."""
    out: list[tuple[str, int, int]] = []
    rp = 0
    for tier in _BASE_TIERS:
        for div in (3, 2, 1):  # III is the entry division
            out.append((f"{tier} {_roman(div)}", div, rp))
            rp += DIV_RP
    # Apex tiers are wider so they feel meaningful to reach/hold.
    for tier in _APEX_TIERS:
        out.append((tier, 0, rp))
        rp += DIV_RP * 3
    return out


def _roman(div: int) -> str:
    return {3: "III", 2: "II", 1: "I"}[div]


_LADDER = _ladder()
# Min RP at which each apex tier begins (handy for callers/marketing).
FEATURED_RP = next(f for n, d, f in _LADDER if n == "Featured")
LEGEND_RP = next(f for n, d, f in _LADDER if n == "Legend")
RYVAL_RP = next(f for n, d, f in _LADDER if n == "Ryval")

# --- promotion series (CS2-style) ------------------------------------------
# Every tier is the same width: three divisions (base) or one apex block.
TIER_RP = DIV_RP * 3  # 300
# A win that carries you into the top slice of a tier (>= this fraction of the
# way to the tier border) doesn't auto-promote - it pins you to 99% and gates
# the crossing behind a single promo game. Win it to enter the next tier; lose
# it and the normal loss applies from the pin (promo never shields a loss).
PROMO_ENTER = 0.90


def next_tier_border(rp: int) -> int | None:
    """RP at the next *tier* boundary above ``rp`` (Iron->Bronze ... Legend->Ryval),
    or ``None`` once a player is at/above the Ryval floor (no tier above)."""
    rp = max(0, int(rp))
    if rp >= RYVAL_RP:
        return None
    return ((rp // TIER_RP) + 1) * TIER_RP


def promo_zone_floor(border: int) -> int:
    """Lowest RP that, when reached by a win, trips the promo series for ``border``
    (i.e. the top ``1 - PROMO_ENTER`` slice of the tier just below the border)."""
    return border - TIER_RP + int(round(PROMO_ENTER * TIER_RP))


def rank_for_rp(rp: int) -> Rank:
    """Map a (clamped, non-negative) RP total to its ladder rank."""
    rp = max(0, int(rp))
    idx = 0
    for i, (_name, _div, floor) in enumerate(_LADDER):
        if rp >= floor:
            idx = i
        else:
            break
    name, div, floor = _LADDER[idx]
    tier = name.split(" ")[0]
    slug = TIER_SLUG[tier]
    if idx + 1 < len(_LADDER):
        next_name, _nd, next_floor = _LADDER[idx + 1]
        return Rank(
            tier=tier, division=div, name=name, slug=slug, floor_rp=floor,
            next_name=next_name, rp_to_next=next_floor - rp,
            rp_into=rp - floor, rp_span=next_floor - floor,
        )
    # Ryval: apex, no ceiling.
    return Rank(
        tier=tier, division=div, name=name, slug=slug, floor_rp=floor,
        next_name=None, rp_to_next=None, rp_into=rp - floor, rp_span=None,
    )


def all_tier_slugs() -> list[str]:
    return [TIER_SLUG[t] for t in (_BASE_TIERS + _APEX_TIERS)]


# --- variable RP -----------------------------------------------------------

WIN_BASE = 25
LOSS_BASE = 22
# Hard caps so a single match can't swing the ladder wildly.
WIN_MIN, WIN_MAX = 10, 45
LOSS_MIN, LOSS_MAX = 10, 45


@dataclass(frozen=True)
class RpOutcome:
    delta: int                 # signed RP change
    base: int                  # signed RP from the result+opponent only
    perf_bonus: int            # signed RP from clean-play performance
    expected: float            # pre-match win probability (for UI/debug)
    placement: bool            # was this a (amplified, hidden) placement game


def compute_rp(
    player: Rating,
    opponent: Rating,
    won: bool,
    *,
    clicks: int | None = None,
    par: int | None = None,
    time_ms: int | None = None,
    opp_time_ms: int | None = None,
    clean: bool = True,
    placement: bool = False,
) -> RpOutcome:
    """Variable RP for one ranked result.

    Magnitude scales with how *expected* the result was (Glicko expected score)
    and a small performance bonus for route efficiency (clicks vs par) and time
    margin over the opponent. Performance bonus is gated on a clean (unflagged)
    run so it can never reward a suspicious finish.
    """
    e = expected_score(player, opponent)

    if won:
        # Beating a favourite (low e) is worth more; stomping an underdog less.
        base = WIN_BASE * (1.30 - 0.6 * e)
    else:
        # Losing to a weaker opponent (high e for you) hurts more.
        base = -LOSS_BASE * (0.70 + 0.6 * e)
    base_i = int(round(base))

    perf = 0.0
    if clean and won:
        if par and par > 0 and clicks is not None:
            # Optimal route (clicks == par) is a clear bonus; over-par tapers it.
            over = max(0, clicks - par)
            perf += max(0.0, 6.0 - 2.0 * over)
        if time_ms is not None and opp_time_ms is not None and opp_time_ms > 0:
            margin = (opp_time_ms - time_ms) / opp_time_ms  # >0 = you were faster
            perf += max(-2.0, min(4.0, margin * 8.0))
    perf_i = int(round(perf))

    delta = base_i + perf_i
    if won:
        delta = max(WIN_MIN, min(WIN_MAX, delta))
    else:
        delta = -max(LOSS_MIN, min(LOSS_MAX, -delta))

    if placement:
        # Placements move the ladder faster so a new player converges quickly.
        delta = int(round(delta * 1.8))

    return RpOutcome(
        delta=delta, base=base_i, perf_bonus=perf_i, expected=e, placement=placement,
    )
