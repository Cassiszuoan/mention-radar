"""Keyword → entity matching.

Critical CJK rule (from the design review): Python's \\b does NOT work between
consecutive CJK characters (they count as \\w), so r'\\b掌機\\b' silently misses
"這台掌機很棒". Any keyword containing CJK is therefore matched as a plain
substring, regardless of its declared match_type.

All comparisons run on NFKC-normalized, casefolded text (full/half-width and
case differences collapse).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_CJK_RANGES = (
    (0x3040, 0x30FF),    # Hiragana / Katakana
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified
    (0xF900, 0xFAFF),    # CJK compat
    (0xFF66, 0xFF9D),    # half-width Katakana
)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def has_cjk(s: str) -> bool:
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in s)


@dataclass
class Matcher:
    entity_id: int
    keyword_id: int
    test: object = field(repr=False)  # callable(normalized_text) -> bool


def build_matchers(keyword_rows: list[dict], stats=None) -> list[Matcher]:
    """Compile keyword rows defensively: a bad regex must never take down the
    whole cycle — skip it and record it in run stats."""
    matchers: list[Matcher] = []
    for kw in keyword_rows:
        if not kw.get("active", True):
            continue
        raw = kw["keyword"]
        norm = normalize(raw)
        mt = kw.get("match_type", "phrase")
        try:
            if mt == "regex":
                rx = re.compile(norm)  # may raise re.error
                test = rx.search
            elif has_cjk(norm) or mt == "phrase":
                # plain substring; capture norm by default-arg
                test = lambda t, _n=norm: _n in t  # noqa: E731
            elif mt == "word":
                rx = re.compile(r"\b" + re.escape(norm) + r"\b")
                test = rx.search
            else:
                raise ValueError(f"unknown match_type {mt}")
        except (re.error, ValueError):
            if stats is not None:
                stats.setdefault("skipped_bad_keywords", []).append(kw["id"])
            continue
        matchers.append(Matcher(entity_id=kw["entity_id"], keyword_id=kw["id"], test=test))
    return matchers


def match_entities(text: str, matchers: list[Matcher]) -> set[int]:
    """Return the set of entity_ids whose keywords hit the text."""
    t = normalize(text)
    hits: set[int] = set()
    for m in matchers:
        if m.entity_id in hits:
            continue
        if m.test(t):
            hits.add(m.entity_id)
    return hits
