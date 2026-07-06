"""Deterministic formatting and detection helpers.

Ported from `conversational_bot_v15.py`:
  - `is_greeting`                                   (~lines 282-302)
  - the canned greeting reply used in
    `process_user_query`                            (~lines 2045-2051)
  - `_is_money_col` / `_rupee_fmt`                   (~lines 1633-1661)
  - `compute_total_facts`                            (~lines 1663-1697)
  - `is_yoy_mtd_comparison`                           (~lines 1771-1781)
  - the two static SQL shapes built inside
    `handle_yoy_mtd_comparison`                       (~lines 1811-1819)

The deterministic YoY MTD comparison *handler* (`handle_yoy_mtd_comparison`)
needs a live DB connection and RBAC context, so it is intentionally NOT
ported here — only its detector (`is_yoy_mtd_comparison`) and the static SQL
template it fills in. The handler itself belongs in a future service-layer
module (e.g. `app/services/yoy.py`) that has access to the DB session and
`inject_rbac_filters`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# ============================================================
# GREETING DETECTION
# ============================================================

#: Keywords that mark a prompt as small talk rather than a data question.
#: Ported verbatim from `GREETING_KEYWORDS`.
GREETING_KEYWORDS: tuple[str, ...] = (
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "bye",
    "good morning",
    "good afternoon",
    "good evening",
    "what can you do",
    "help",
    "who are you",
    "ok",
    "okay",
    "cool",
    "how are you",
)

#: Canned reply returned when `is_greeting` matches, ported verbatim from
#: `process_user_query`'s greeting interception branch.
GREETING_REPLY: str = "Hello! I am Botree Insights Assistant. Ask me anything about your database!"


def is_greeting(prompt: str) -> bool:
    """Detect small talk / greetings that should bypass the SQL pipeline.

    Direct port of the prototype's `is_greeting`: strips trailing punctuation,
    rejects anything longer than 4 words, then looks for a whole-word keyword
    match.
    """
    prompt_clean = prompt.lower().strip().rstrip("!.,?")
    word_count = len(prompt_clean.split())

    if word_count > 4:
        return False

    for kw in GREETING_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, prompt_clean):
            return True
    return False


# ============================================================
# RUPEE / MONEY FORMATTING
# ============================================================

#: Column-name signals for MONETARY values (get a ₹ prefix). Ported verbatim
#: from `_MONEY_HINTS`.
MONEY_HINTS: tuple[str, ...] = (
    "revenue",
    "sales",
    "value",
    "amount",
    "amt",
    "gross",
    "net",
    "mrp",
    "price",
    "spend",
    "purchase",
    "outstanding",
    "turnover",
)

#: Column-name signals that override `MONEY_HINTS` — counts/qty/percent do
#: NOT get a rupee prefix even if they also match a money hint. Ported
#: verbatim from `_NON_MONEY_HINTS`.
NON_MONEY_HINTS: tuple[str, ...] = (
    "qty",
    "quantity",
    "count",
    "outlets",
    "perc",
    "percent",
    "pct",
    "days",
    "number",
    "distance",
)


def is_money_column(name: str) -> bool:
    """Decide whether a column should be rendered with a rupee prefix.

    Direct port of the prototype's `_is_money_col`: a non-money hint always
    wins, otherwise any money hint qualifies.
    """
    c = str(name).lower()
    if any(h in c for h in NON_MONEY_HINTS):
        return False
    return any(h in c for h in MONEY_HINTS)


def format_rupees(value: Any) -> str:
    """Format a number as Indian-style rupees: ₹250, ₹25,000, ₹2,50,000.

    Direct port of the prototype's `_rupee_fmt`: groups the last 3 digits,
    then the remainder in pairs of 2 (Indian digit grouping), and appends
    2 decimal places only when the value is not a whole number.
    """
    try:
        xf = float(value)
    except (TypeError, ValueError):
        return str(value)
    neg = xf < 0
    xf = abs(xf)
    whole = int(xf)
    s = str(whole)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        rest = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", rest)
        s = rest + "," + last3
    out = f"₹{s}" if xf == whole else f"₹{s}.{int(round((xf - whole) * 100)):02d}"
    return f"-{out}" if neg else out


# ============================================================
# DETERMINISTIC TOTALS (anti-hallucination guard)
# ============================================================

#: Column-name signals for columns worth summing when the caller asks for a
#: total. Ported verbatim from the `additive` tuple inside `compute_total_facts`.
ADDITIVE_HINTS: tuple[str, ...] = (
    "revenue",
    "value",
    "amount",
    "qty",
    "quantity",
    "count",
    "sales",
    "net",
    "gross",
    "sum",
    "total",
)

#: Row count at/above which a sum could be a PARTIAL total (matches the
#: prototype's 50-row LIMIT cap) — ported from the truncation guard in
#: `compute_total_facts`.
DEFAULT_ROW_CAP: int = 50


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compute_total_facts(
    columns: list[str],
    rows: list[Sequence[Any]],
    row_cap: int = DEFAULT_ROW_CAP,
) -> dict[str, Any]:
    """Deterministically sum numeric columns to prevent LLM arithmetic hallucination.

    Reimplementation (stdlib only, no pandas) of the prototype's
    `compute_total_facts`. Given the columns and rows of an already-executed
    query result:
      - If the row count may be truncated by the row cap, refuse to compute
        a sum (it would be a misleading partial total) and set `truncated`.
      - Otherwise sum columns that are numeric across all rows, preferring
        columns whose name matches an `ADDITIVE_HINTS` keyword; if none
        match, sum every numeric column.

    Note: the prototype also gated *whether* to call this at all on keywords
    like "total"/"sum"/"ytd" appearing in the user's question — that
    decision is an orchestration concern for the caller, not part of this
    pure computation, so it is not reproduced here.

    Returns a dict: `{"truncated": bool, "row_count": int,
    "target_columns": list[str], "totals": dict[str, float]}`.
    """
    row_count = len(rows)

    if row_count == 0 or not columns:
        return {"truncated": False, "row_count": row_count, "target_columns": [], "totals": {}}

    if row_count >= row_cap:
        return {
            "truncated": True,
            "row_count": row_count,
            "target_columns": [],
            "totals": {},
        }

    numeric_cols: list[str] = []
    for idx, col in enumerate(columns):
        values = [row[idx] for row in rows]
        if any(_is_numeric(v) for v in values):
            numeric_cols.append(col)

    if not numeric_cols:
        return {"truncated": False, "row_count": row_count, "target_columns": [], "totals": {}}

    target_cols = [c for c in numeric_cols if any(h in c.lower() for h in ADDITIVE_HINTS)]
    if not target_cols:
        target_cols = numeric_cols

    totals: dict[str, float] = {}
    for col in target_cols:
        idx = columns.index(col)
        totals[col] = sum(row[idx] for row in rows if _is_numeric(row[idx]))

    return {
        "truncated": False,
        "row_count": row_count,
        "target_columns": target_cols,
        "totals": totals,
    }


# ============================================================
# YOY MTD COMPARISON DETECTION
# ============================================================


def is_yoy_mtd_comparison(user_prompt: str) -> bool:
    """True only when the user asks to compare MTD against the SAME period last year.

    Direct port of the prototype's `is_yoy_mtd_comparison`: requires BOTH an
    MTD reference AND a last-year reference, to stay tight.
    """
    p = (user_prompt or "").lower()
    has_mtd = any(k in p for k in ("mtd", "month to date", "month-to-date"))
    has_ly = any(
        k in p
        for k in (
            "last year",
            "previous year",
            "last yr",
            "year on year",
            "year-on-year",
            "year over year",
            "yoy",
            "same month last year",
            "vs last year",
            "compared to last year",
            "compare to last year",
        )
    )
    return has_mtd and has_ly


#: Static SQL shape used by the (not-yet-ported) deterministic YoY MTD
#: handler, ported verbatim from `handle_yoy_mtd_comparison`. Fill in with
#: `.format(table=..., alias=..., datecol=..., measure=...)`; the handler
#: picks `table="rpt_order_summary_t", alias="ord", datecol="order_date",
#: measure="gross_amt"` when the question mentions "order" (and not
#: "invoice"), otherwise `table="rpt_invoice_summary_t", alias="inv",
#: datecol="invoice_date", measure="measure_14"`.
YOY_MTD_SQL_TEMPLATE: dict[str, str] = {
    "current": (
        "SELECT SUM({alias}.{measure}) AS CurrentMTD FROM {table} {alias} "
        "WHERE YEAR({alias}.{datecol}) = YEAR(CURDATE()) "
        "AND MONTH({alias}.{datecol}) = MONTH(CURDATE()) "
        "AND DAY({alias}.{datecol}) <= DAY(CURDATE())"
    ),
    "last_year": (
        "SELECT SUM({alias}.{measure}) AS LastYearMTD FROM {table} {alias} "
        "WHERE YEAR({alias}.{datecol}) = YEAR(CURDATE()) - 1 "
        "AND MONTH({alias}.{datecol}) = MONTH(CURDATE()) "
        "AND DAY({alias}.{datecol}) <= DAY(CURDATE())"
    ),
}
