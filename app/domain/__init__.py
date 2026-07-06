"""Domain knowledge layer for the NL→SQL backend.

This package is the single source of truth for the database semantic layer:
schema catalog, SQL generation rules, business glossary, and deterministic
formatting helpers. Ported from `conversational_bot_v15.py` (the Streamlit
prototype) and reorganized into typed, documented, pure modules.

Purity contract: nothing in `app.domain` performs I/O, imports `app.config`,
imports FastAPI, or talks to a database. It is data + functions only, so it
can be imported anywhere (prompt builders, SQL validators, API layer, tests)
without pulling in runtime dependencies or side effects.
"""

from __future__ import annotations
