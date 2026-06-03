#!/usr/bin/env python3
"""Generate per-division rank crests for the 6 divisioned base tiers.

Each tier keeps its color identity; the crest GROWS across its divisions:
chevron count encodes the division (III = 1 chevron, II = 2, I = 3), and a row
of small pips along the base encodes the tier's height on the ladder
(Iron = 1 pip … Diamond = 6). Apex tiers (Featured, Legend, Ryval) keep their
single existing crest. Output: extension/icons/ranks/<slug>-<division>.svg
"""
from __future__ import annotations
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "extension" / "icons" / "ranks"

# slug -> (gradient top, gradient bottom, dark border fill, ladder index)
TIERS = {
    "iron":    ("#888d92", "#52575c", "#3a3e42", 1),
    "bronze":  ("#cd7f4a", "#9c5a21", "#7a431a", 2),
    "silver":  ("#e2e8ee", "#9aa1a8", "#777d83", 3),
    "gold":    ("#ffd968", "#d9a21b", "#a9790b", 4),
    "plat":    ("#79e6d6", "#2f9d8f", "#1f7c70", 5),
    "diamond": ("#7cc0ff", "#2f7fd6", "#1f5ea8", 6),
}

# division value -> number of chevrons (III is the entry division, I the top)
CHEVRONS = {3: 1, 2: 2, 1: 3}
ROMAN = {3: "III", 2: "II", 1: "I"}
CHEV_OPACITY = [0.95, 0.89, 0.83]

SHIELD = "M32 4 L56 14 L56 34 Q56 52 32 60 Q8 52 8 34 L8 14 Z"


def chevrons(n: int) -> str:
    out = []
    for i in range(n):
        y = 42 - i * 7  # stack upward from the base
        out.append(
            f'  <path d="M22 {y} L32 {y - 7} L42 {y}" fill="none" stroke="#ffffff" '
            f'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="{CHEV_OPACITY[i]}"/>'
        )
    return "\n".join(out)


def pips(n: int) -> str:
    """A centered row of small gems at the base, one per ladder step. Spacing +
    radius are kept tight (and the row nudged up to y=47, where the shield is
    wider) so even Diamond's 6 pips sit inside the crest with margin."""
    gap = 4.8
    span = (n - 1) * gap
    x0 = 32 - span / 2
    out = []
    for i in range(n):
        cx = x0 + i * gap
        out.append(
            f'  <circle cx="{cx:.1f}" cy="47" r="1.4" fill="#ffffff" '
            f'opacity="0.92" stroke="#00000022" stroke-width="0.5"/>'
        )
    return "\n".join(out)


_HEAD = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="{label}">
  <defs>
    <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{top}"/>
      <stop offset="1" stop-color="{bot}"/>
    </linearGradient>
  </defs>
  <path d="{shield}" fill="{border}"/>
  <path d="{shield}" fill="url(#body)" transform="translate(32 32) scale(0.88) translate(-32 -32)"/>
  <path d="M32 7 L52 15.5 Q52 17 32 24 Q12 17 12 15.5 Z" fill="#ffffff" opacity="0.16"/>'''


def division_svg(slug: str, division: int) -> str:
    top, bot, border, idx = TIERS[slug]
    head = _HEAD.format(label=f"{slug} {ROMAN[division]} rank",
                        top=top, bot=bot, border=border, shield=SHIELD)
    return f"{head}\n{chevrons(CHEVRONS[division])}\n{pips(idx)}\n</svg>\n"


def tier_svg(slug: str) -> str:
    """The single tier-level crest (ladder strip / fallback): a clean shield with
    no chevrons and no pips, so tiers read by color alone."""
    top, bot, border, _idx = TIERS[slug]
    head = _HEAD.format(label=f"{slug} rank", top=top, bot=bot,
                        border=border, shield=SHIELD)
    return f"{head}\n</svg>\n"


def main() -> None:
    n = 0
    for slug in TIERS:
        (OUT / f"{slug}.svg").write_text(tier_svg(slug))
        n += 1
        for division in (3, 2, 1):
            (OUT / f"{slug}-{division}.svg").write_text(division_svg(slug, division))
            n += 1
    print(f"wrote {n} crests ({len(TIERS)} tier + {len(TIERS) * 3} division) to {OUT}")


if __name__ == "__main__":
    main()
