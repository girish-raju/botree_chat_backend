"""Prompt assembly for the NL->SQL and NL-answer LLM calls.

The static/dynamic split matters for Anthropic prompt caching: everything
that never changes between requests (schema, rules, glossary, few-shots)
lives in `build_static_system_block`, which is deterministic and cached at
module level so repeated calls return the identical string (a prerequisite
for the `cache_control` breakpoint to actually hit). Anything that varies
per-request (today's date, the caller's role) goes in
`build_dynamic_system_block` instead, and must NEVER leak into the static
block.
"""

from __future__ import annotations

import functools
from datetime import date

from app.domain.glossary import BUSINESS_GLOSSARY, STATE_NAME_MAP
from app.domain.schema_catalog import RELATIONSHIPS, SCHEMA_DESCRIPTION, format_schema_description
from app.domain.sql_rules import FEW_SHOT_EXAMPLES, SQL_RULES


@functools.cache
def build_static_system_block() -> str:
    """Assemble the large, deterministic system prompt block.

    Deterministic (no dates, no request-specific values) so that repeated
    calls are byte-identical and Anthropic's prompt cache can reuse it.
    """
    sections: list[str] = []

    sections.append(
        "You are an expert MySQL analyst for an FMCG distribution database. "
        "Given a business question, you either generate a single, correct, "
        "read-only MySQL SELECT statement that answers it, or — if the "
        "question is not about the database at all — respond conversationally. "
        "You are precise, never fabricate data or columns, and always follow "
        "the rules below exactly."
    )

    sections.append("### SCHEMA")
    sections.append(format_schema_description())

    sections.append(RELATIONSHIPS)

    sections.append(SQL_RULES)

    sections.append("### BUSINESS GLOSSARY")
    for term, meaning in BUSINESS_GLOSSARY.items():
        sections.append(f"  - {term}: {meaning}")

    sections.append("### STATE / ZONE NAME MAPPING")
    for name, value in STATE_NAME_MAP.items():
        sections.append(f'  - "{name}" -> {value}')

    sections.append("### EXAMPLE QUESTIONS AND CORRECT SQL")
    for example in FEW_SHOT_EXAMPLES:
        sections.append(f"Q: {example['question']}")
        sections.append(f"SQL: {example['sql']}")

    return "\n\n".join(sections)


def build_dynamic_system_block(today: date, role_hint: str) -> str:
    """Small, per-request system block: dates and caller-role hints only.

    Never put dates or other volatile values in `build_static_system_block` —
    it would break prompt-cache reuse.
    """
    return (
        f"Today's date is {today.isoformat()}. The user is a {role_hint}. "
        "Generate a MySQL SELECT for the question via the query_database tool."
    )


REWRITE_PROMPT = """Given the conversation history and the latest user question, rewrite the \
latest question into a standalone analytical question by resolving pronouns, \
ellipsis, and implicit references (e.g. "what about last month", "and for TN") \
against the history. If the question is already standalone, return it unchanged.

Conversation history:
{history}

Latest question: {question}

Output ONLY the rewritten question, with no preamble, quotes, or explanation."""


ANSWER_PROMPT = """You are answering a business question using ONLY the facts and sample rows \
provided below. NEVER invent or estimate numbers that are not given. Rupee/currency \
amounts must be reported exactly as given (do not rescale or reformat magnitudes). \
Write a concise business summary of 1-3 sentences leading with the key total, \
top item, or insight. The complete data is appended to your answer as a table \
automatically, so do NOT list individual rows, do NOT write a table yourself, \
and do NOT describe the data as a sample, subset, or "available data". \
Do not mention SQL or columns.

Question: {question}

Facts:
{facts}

Sample rows (up to 5, columns: {columns}):
{sample_rows}

Answer:"""


TITLE_PROMPT = """Generate a short title (6 words or fewer) summarizing the following text. \
Output ONLY the title, no punctuation at the end, no quotes.

Text: {text}

Title:"""


def _build_cloudflare_sql_prompt() -> str:
    """Assemble the trimmed system prompt used by the Cloudflare provider.

    Trimmed relative to the Anthropic static block: table/column names only
    (no long descriptions), a condensed rule set, and 5 few-shots. The
    question itself is NOT included here — it is sent as a separate user
    message, matching the Anthropic provider's message structure.
    """
    schema_lines: list[str] = []
    for table, cols in SCHEMA_DESCRIPTION.items():
        schema_lines.append(f"{table}: {', '.join(cols.keys())}")

    few_shots = "\n".join(f"Q: {ex['question']}\nSQL: {ex['sql']}" for ex in FEW_SHOT_EXAMPLES[:5])

    return f"""You are an expert MySQL analyst for an FMCG distribution database. \
Given a business question, produce a single read-only MySQL SELECT statement, or a direct \
conversational answer if the question is not about the database.

### SCHEMA (tables and columns only)
{chr(10).join(schema_lines)}

{RELATIONSHIPS}

### CONDENSED RULES
1. Use table aliases exactly: distributor_t->d, rpt_invoice_summary_t->inv, \
rpt_order_summary_t->ord, rpt_customer_master_t->cust, salesman_t->sm, \
rpt_purchase_summary_t->pur, rpt_coverage_productivity_t->cov, \
rpt_route_coverage_plan_t->rcp.
2. Invoice revenue = inv.measure_14 (NEVER measure_13, NEVER measure_1). \
Order revenue = ord.gross_amt. Purchase value = pur.net_amount.
3. COUNT(DISTINCT <primary_key>), never COUNT(name).
4. Relative dates (MTD/YTD/today/etc.) MUST use CURDATE() — never hardcode a year.
5. Never join two fact tables (rpt_invoice_summary_t, rpt_order_summary_t, \
rpt_purchase_summary_t, rpt_coverage_productivity_t, rpt_route_coverage_plan_t) \
to each other.
6. USER SCOPE — the user's territory/access is applied AUTOMATICALLY by the system \
AFTER your SQL runs. Words like "my", "our", "mine", "my zone", "my region", \
"my area", "my team", "in my territory" are NOT filters. Treat "sales in my zone" \
EXACTLY like "total sales" — add NO geo_hier* or sales_hier* filter. NEVER invent \
literal values like 'My Zone', 'My Region', 'My Area' or 'VP Sales' — no such value \
exists and it matches ZERO rows. Only add a geo/sales filter when the user names a \
REAL place (e.g. "Chennai", "Tamil Nadu", "South zone").

### EXAMPLES
{few_shots}
Q: Which product sold most in my zone
SQL: SELECT inv.product_name, SUM(inv.measure_14) AS TotalSales FROM rpt_invoice_summary_t AS inv GROUP BY inv.product_name ORDER BY TotalSales DESC LIMIT 1
Q: Top distributors by sales value in my region
SQL: SELECT d.name, SUM(ord.gross_amt) AS SalesValue FROM distributor_t AS d JOIN rpt_order_summary_t AS ord ON d.code = ord.distributor_code GROUP BY d.name ORDER BY SalesValue DESC LIMIT 10
Q: what are my total sales this year
SQL: SELECT SUM(inv.measure_14) AS TotalSales FROM rpt_invoice_summary_t AS inv WHERE YEAR(inv.invoice_date) = YEAR(CURDATE())

(Note in the three examples above: "my zone", "my region", "my" add NO geo/sales \
filter — scope is applied automatically. Always mode='db' for questions about \
sales, revenue, orders, purchases, coverage, distributors, salesmen, products, \
customers or outstanding.)

Respond with STRICT JSON only, no markdown fences, no commentary, in exactly this shape:
{{"mode": "db" or "general", "sql": "<SELECT ... or empty string>", "answer": "<direct answer or empty string>"}}"""


CLOUDFLARE_SQL_PROMPT: str = _build_cloudflare_sql_prompt()


__all__ = [
    "build_static_system_block",
    "build_dynamic_system_block",
    "REWRITE_PROMPT",
    "ANSWER_PROMPT",
    "TITLE_PROMPT",
    "CLOUDFLARE_SQL_PROMPT",
]
