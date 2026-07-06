"""Business glossary, state-name mapping, and term synonyms.

Ported from `conversational_bot_v15.py`:
  - the "Business Glossary" block in `generate_query_plan`      (~lines 1556-1564)
  - the same glossary repeated (fuller form) in
    `generate_natural_response`                                 (~lines 1908-1915)
  - the "STATE NAME MAPPING" rule 12 in `generate_query_plan`    (~lines 1356-1363)

`SYNONYMS` has no direct dict equivalent in the prototype — it is derived
from rule 5 ("REVENUE / SALES METRICS") and the natural-language money-hint
list, which repeatedly treat these terms as interchangeable when talking
about the same underlying measure. Documented here as derived, not ported.
"""

from __future__ import annotations

#: Abbreviation -> (full name, schema column it maps to). Union of the two
#: glossary blocks in the prototype (the SQL-planning prompt's short form and
#: the natural-language prompt's "always use full names" form).
BUSINESS_GLOSSARY: dict[str, str] = {
    "ASM": "Area Sales Manager (sales_hier5_name)",
    "RSM": "Regional Sales Manager (sales_hier3_name)",
    "ZSM": "Zonal Sales Manager (sales_hier2_name)",
    "BM": "Business Manager (sales_hier4_name)",
    "SO": "Sales Officer (sales_hier6_name)",
    "VP": "Vice President Sales (sales_hier1_name)",
    "MRP": "Maximum Retail Price (mrp column)",
    "SKU": "product_name or product_code",
}

#: Free-text state/zone name (as a user might type it) -> exact
#: `geo_hier4_name` / `geo_hier2_name` value stored in the database. Ported
#: verbatim from rule 12 ("STATE NAME MAPPING") of `generate_query_plan`.
STATE_NAME_MAP: dict[str, str] = {
    "Tamil Nadu": "TAMILNADU STATE",
    "West Bengal": "WB STATE",
    "Andhra Pradesh": "ANDHRA PRADESH STATE",
    "Jharkhand": "JHARKHAND STATE",
    "South": "SOUTH",
    "East": "EAST",
    "North East": "NORTH-EAST",
}

#: Column that each `STATE_NAME_MAP` key resolves against — states map to
#: `geo_hier4_name`, the last three (zones) map to `geo_hier2_name`. Kept
#: alongside `STATE_NAME_MAP` since the prototype's rule 12 bakes this
#: column choice into the mapping itself.
STATE_NAME_MAP_COLUMN: dict[str, str] = {
    "Tamil Nadu": "geo_hier4_name",
    "West Bengal": "geo_hier4_name",
    "Andhra Pradesh": "geo_hier4_name",
    "Jharkhand": "geo_hier4_name",
    "South": "geo_hier2_name",
    "East": "geo_hier2_name",
    "North East": "geo_hier2_name",
}

#: DERIVED (not a literal dict in the prototype): term -> canonical measure
#: name, inferred from rule 5 ("REVENUE / SALES METRICS") which repeatedly
#: treats these words as referring to the same underlying revenue concept
#: (e.g. "sales" and "revenue" both mean `measure_14`/`gross_amt`), and from
#: the `_MONEY_HINTS` word list used to decide which columns get a rupee
#: prefix. Provided for future normalization use; document provenance if
#: extended.
SYNONYMS: dict[str, str] = {
    "sales": "revenue",
    "turnover": "revenue",
    "spend": "purchase_value",
    "purchase spend": "purchase_value",
    "invoice value": "revenue",
    "order value": "order_revenue",
}
