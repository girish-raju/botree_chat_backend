"""Question normalization and temporal-intent extraction.

`normalize_question` maps paraphrases of the same question onto the same
canonical string so exact-match cache lookups ("L0") hit across trivial
rewordings ("show me sales today" == "can you tell me the revenue today").
It lowercases, strips punctuation (keeping digits/letters/spaces/`%`),
canonicalizes business synonyms (`app.domain.glossary.SYNONYMS`), and strips
leading/trailing filler phrases.

`extract_temporal_intent` classifies the RAW (pre-normalization) question
into one canonical time-frame token. It exists purely as a false-positive
guard for the semantic cache (see `app.cache.semantic.QueryCache`): two
questions can be near-identical in embedding space ("sales today" vs "sales
yesterday") while referring to disjoint data, so a semantic hit is only
accepted when the temporal intents also match.
"""

from __future__ import annotations

import re

from app.domain.glossary import SYNONYMS

#: Filler phrases stripped from the leading/trailing edges of a normalized
#: question. Order does not matter for correctness (stripping is iterated to
#: a fixpoint) but longer phrases are listed first for readability.
_FILLER_PHRASES: tuple[str, ...] = (
    "please",
    "can you",
    "show me",
    "give me",
    "what is",
    "what are",
    "tell me",
)

#: Keep digits, letters, whitespace, and `%` (percentages are meaningful in
#: business questions, e.g. "growth % this month"); everything else becomes a
#: space so words don't get glued together (e.g. "Q1/Q2" -> "q1 q2").
_NON_KEEP_CHARS_RE = re.compile(r"[^a-z0-9\s%]")
_WHITESPACE_RE = re.compile(r"\s+")

#: Synonym keys sorted longest-phrase-first (by word count, then character
#: length) so multi-word phrases are canonicalized before their component
#: words could be matched in isolation (e.g. "purchase spend" before "spend").
_SYNONYM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(key)}\b"), value)
    for key, value in sorted(
        SYNONYMS.items(), key=lambda kv: (-len(kv[0].split()), -len(kv[0]))
    )
]


def _apply_synonyms(text: str) -> str:
    for pattern, replacement in _SYNONYM_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _strip_filler(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for filler in _FILLER_PHRASES:
            if text == filler:
                text = ""
                changed = True
                continue
            prefix = f"{filler} "
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
            suffix = f" {filler}"
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                changed = True
        text = text.strip()
    return text


def normalize_question(text: str) -> str:
    """Return a canonical form of `text` for exact-match cache lookups.

    Deterministic and pure: lowercase -> strip punctuation (keep
    alphanumerics/spaces/`%`) -> collapse whitespace -> synonym
    canonicalization -> strip leading/trailing filler (iteratively).
    """
    s = text.lower()
    s = _NON_KEEP_CHARS_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = _apply_synonyms(s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = _strip_filler(s)
    return s


# --- Temporal intent -------------------------------------------------------

#: Ordered (most-specific-first) keyword groups per canonical intent token.
#: Order matters: "last month" must be checked before "this month" would ever
#: be able to shadow it, etc. Since each group's keywords are disjoint from
#: the others', the actual iteration order below just needs "last_*" checked
#: before "this_*" isn't strictly required, but is kept for clarity.
_TEMPORAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("yesterday", ("yesterday",)),
    ("today", ("today", "todays")),
    ("last_week", ("last week", "past week", "previous week")),
    ("this_week", ("this week", "current week")),
    ("last_month", ("last month", "past month", "previous month")),
    (
        "this_month",
        (
            "this month",
            "current month",
            "mtd",
            "month to date",
            "month-to-date",
        ),
    ),
    ("last_quarter", ("last quarter", "past quarter", "previous quarter")),
    ("this_quarter", ("this quarter", "current quarter", "qtd")),
    ("last_year", ("last year", "past year", "previous year")),
    (
        "this_year",
        (
            "this year",
            "current year",
            "ytd",
            "year to date",
            "year-to-date",
        ),
    ),
)

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

#: Explicit calendar dates: ISO (2026-07-06), slash/dash numeric
#: (06/07/2026, 06-07-2026), or a month name with a day/year nearby.
_EXPLICIT_DATE_RE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b(?:" + "|".join(_MONTH_NAMES) + r")\b\s*\d{0,4}"
)


def extract_temporal_intent(text: str) -> str:
    """Classify `text`'s time frame into one canonical token.

    Applied to the RAW question (before synonym mangling / filler
    stripping), since normalization could coincidentally merge distinct time
    frames. Keyword/regex based, case-insensitive.
    """
    s = text.lower()

    for intent, keywords in _TEMPORAL_KEYWORDS:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", s):
                return intent

    if _EXPLICIT_DATE_RE.search(s):
        return "date_range"

    return "none"


__all__ = ["normalize_question", "extract_temporal_intent"]
