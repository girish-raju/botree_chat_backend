"""Database schema catalog — the semantic description of every whitelisted table.

Ported verbatim (data-wise) from `conversational_bot_v15.py`:
  - `schema_description` dict                    (~lines 753-1044)
  - `format_schema_description()`                (~lines 1047-1053)
  - `COLUMN_TABLE_MAP` / `ALIAS_TABLE_MAP` / `AMBIGUOUS_COLUMNS`
    used by `validate_and_fix_sql()`              (~lines 72-217)
  - the `include_tables=[...]` whitelist passed to `SQLDatabase(...)`
    (~lines 1114-1125)
  - the JOIN-key documentation embedded in the `generate_query_plan` prompt
    (~lines 1300-1306)

Only code style was changed (typed, organized, documented) — every table,
column, and description string is preserved.
"""

from __future__ import annotations

# ============================================================
# TABLE WHITELIST
# ============================================================

#: The 10 tables the backend is allowed to see/query. Mirrors the
#: `include_tables=[...]` list passed to `SQLDatabase(...)` in the prototype.
INCLUDE_TABLES: frozenset[str] = frozenset(
    {
        "distributor_t",
        "rpt_invoice_summary_t",
        "rpt_order_summary_t",
        "rpt_customer_master_t",
        "salesman_t",
        "sales_force_hier_level_t",
        "sales_force_hier_value_t",
        "rpt_purchase_summary_t",
        "rpt_coverage_productivity_t",
        "rpt_route_coverage_plan_t",
    }
)

#: The 5 "fact" (rpt_*) summary tables. Rule 15 of `SQL_RULES` forbids ever
#: joining two of these to each other (it multiplies rows and inflates sums).
FACT_TABLES: frozenset[str] = frozenset(
    {
        "rpt_invoice_summary_t",
        "rpt_order_summary_t",
        "rpt_purchase_summary_t",
        "rpt_coverage_productivity_t",
        "rpt_route_coverage_plan_t",
    }
)

# ============================================================
# SCHEMA DESCRIPTION
# ============================================================

#: Full port of the prototype's `schema_description` dict: table -> {column: description}.
#: Every table, column, and description string is kept verbatim.
SCHEMA_DESCRIPTION: dict[str, dict[str, str]] = {
    "distributor_t": {
        "code": "Unique distributor code (PK) — JOIN key via distributor_code in other tables",
        "name": "Distributor name",
        "lob_code": "Line of business code",
        "sales_hier1_name": "VP Sales / HEAD OF SALES",
        "sales_hier2_name": "ZSM — Zonal/Divisional Sales Manager (e.g. ZONAL / DIVISIONAL SALES MANAGER- SOUTH)",
        "sales_hier3_name": "RSM — Regional Sales Manager (e.g. RSM-SOUTH, RSM-EAST)",
        "sales_hier4_name": "BM — Business Manager (e.g. BM - CHENNAI, BM - DURGAPUR)",
        "sales_hier5_name": "ASM — Area Sales Manager (e.g. ASM - CHENNAI)",
        "sales_hier6_name": "SO — Sales Officer (e.g. SO - CHENNAI)",
        "sales_hier1_code": "VP Sales code",
        "sales_hier2_code": "ZSM code",
        "sales_hier3_code": "RSM code",
        "sales_hier4_code": "BM code",
        "sales_hier5_code": "ASM code",
        "sales_hier6_code": "SO code",
        "geo_hier1_name": "Country (India)",
        "geo_hier2_name": "Zone (e.g. SOUTH, EAST, NORTH-EAST)",
        "geo_hier3_name": "Region (e.g. REGION 1, REGION 6)",
        "geo_hier4_name": "State (e.g. TAMILNADU STATE, WB STATE, ANDHRA PRADESH STATE, JHARKHAND STATE)",
        "geo_hier5_name": "Zone/State (e.g. TAMILNADU Zone_State, WB - II Zone_State)",
        "geo_hier6_name": "District (e.g. TIRUCHIRAPPALLI District, CHENNAI District)",
        "geo_hier7_name": "Town (e.g. Trichy Town, Chennai Town)",
        "geo_hier1_code": "Country code",
        "geo_hier2_code": "Zone code",
        "geo_hier3_code": "Region code",
        "geo_hier4_code": "State code",
        "geo_hier6_code": "District code",
        "geo_hier7_code": "Town code",
        "distributor_type": "Type of distributor",
        "distributor_code": "Alternate distributor code reference",
        "last_modified_date": "Last modified datetime (DD-MM-YYYY HH:MM)",
        "created_date": "Created datetime (DD-MM-YYYY HH:MM)",
    },
    "rpt_invoice_summary_t": {
        "invoice_number": "Invoice number (PK)",
        "product_code": "Product code (PK)",
        "distributor_code": "Distributor code — JOIN with distributor_t.code",
        "product_batch_code": "Product batch code",
        "product_name": "Full product name (e.g. GOOGLY 180 GM., RICH MARIE 280 GM.)",
        "product_company_code": "Company code (e.g. SFPPL)",
        "distributor_name": "Distributor name",
        "salesman_code": "Salesman code — JOIN with salesman_t.code",
        "salesman_name": "Salesman name",
        "route_code": "Route code",
        "route_name": "Route name",
        "retailer_channel_name": "Channel name (e.g. URBAN/WHOLESALE, URBAN/GENERAL TRADE RETAIL)",
        "retailer_group_name": "Group name (e.g. WHOLESALERS EXCLUSIVE, GROCERIES (LARGE))",
        "retailer_class_name": "Class name (e.g. CLASS-A)",
        "retailer_code": "Retailer code",
        "retailer_name": "Retailer/customer name",
        "invoice_status": "Invoice status (e.g. Delivered, Pending)",
        "invoice_source": "Source (e.g. SFA Order Booking)",
        "invoice_date": "Invoice date (YYYY-MM-DD)",
        "invoice_type": "Invoice type",
        "invoice_mode": "Invoice mode (e.g. Credit)",
        "measure_1": "Invoice quantity (primary billed qty)",
        "measure_7": "MRP value per unit",
        "measure_9": "Gross amount before tax/discount",
        "measure_12": "Net amount after deductions",
        "measure_14": "Net amount (invoice value after tax/deductions) — USE THIS for revenue/sales analysis",
        "measure_13": "Tax amount only — NOT revenue, never use for sales",
        "taxable_gross_amt": "Taxable gross amount",
        "delivery_date": "Delivery date (YYYY-MM-DD)",
        "sales_hier1_name": "VP Sales",
        "sales_hier2_name": "ZSM name",
        "sales_hier3_name": "RSM name",
        "sales_hier4_name": "BM name",
        "sales_hier5_name": "ASM name",
        "sales_hier6_name": "SO name",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "geo_hier7_name": "Town",
        "product_hier1_name": "Company (SAJ FOOD PRODUCTS PVT. LTD.)",
        "product_hier2_name": "Product group (GROUP-A, GROUP-B)",
        "product_hier3_name": "Category (A - SEMI SWEET, C - CRACKER, H - RUSKIT)",
        "product_hier4_name": "Brand/Sub-category (GOOGLY, MARIE, CREAM CRACKER, THINZ)",
        "product_hier8_name": "Most specific product name",
    },
    "rpt_order_summary_t": {
        "distributor_code": "Distributor code — JOIN with distributor_t.code",
        "order_number": "Order number",
        "product_code": "Product code",
        "product_name": "Product name",
        "distributor_name": "Distributor name",
        "salesman_code": "Salesman code — JOIN with salesman_t.code",
        "salesman_name": "Salesman name",
        "route_code": "Route code",
        "route_name": "Route name",
        "retailer_code": "Retailer code",
        "retailer_name": "Retailer name",
        "retailer_channel_name": "Channel name",
        "retailer_group_name": "Group name",
        "retailer_class_name": "Class name",
        "order_date": "Order date (YYYY-MM-DD)",
        "order_qty": "Order quantity (INTEGER)",
        "order_value": "Order value (DECIMAL)",
        "purchase_price": "Purchase price per unit (DECIMAL)",
        "mrp": "Maximum retail price (DECIMAL)",
        "sell_price": "Sell price per unit (DECIMAL)",
        "order_status": "Order status (Cancelled, Ordered, Delivered, Order Sent)",
        "invoice_number": "Invoice number if invoiced",
        "invoice_qty": "Invoiced quantity",
        "invoice_value": "Invoice value (DECIMAL)",
        "gross_amt": "Gross amount — USE THIS for order revenue",
        "net_amt": "Net amount after deductions (DECIMAL)",
        "product_hier1_name": "Company",
        "product_hier2_name": "Product group",
        "product_hier3_name": "Category",
        "product_hier4_name": "Brand/Sub-category",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "geo_hier7_name": "Town",
        "sales_hier1_name": "VP Sales",
        "sales_hier2_name": "ZSM",
        "sales_hier3_name": "RSM",
        "sales_hier4_name": "BM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
        "invoice_date": "Invoice date",
        "source": "Order source (e.g. SFA)",
    },
    "rpt_customer_master_t": {
        "cmp_code": "Company code (PK)",
        "distr_code": "Distributor code (PK) — JOIN with distributor_t.code",
        "customer_code": "Customer/retailer code (PK)",
        "customer_name": "Customer/retailer name",
        "email_id": "Customer email",
        "phone": "Customer phone",
        "mobile_no": "Customer mobile",
        "pin_no": "PIN code",
        "credit_days": "Credit days",
        "credit_limit": "Credit limit",
        "is_active": "Active flag (Y/N)",
        "outstanding_amount": "Outstanding payment amount",
        "outstanding_billcount": "Number of outstanding bills",
        "geo_city": "Customer city",
        "geo_state": "Customer state",
        "channel_name": "Channel (e.g. URBAN)",
        "sub_channelname": "Sub-channel (e.g. GENERAL TRADE RETAIL)",
        "group_name": "Group (e.g. GROCERIES (LARGE))",
        "class_name": "Class (e.g. CLASS-A)",
        "gst_tinno": "GST number",
        "distributor_name": "Distributor name",
        "salesman_name": "Assigned salesman",
        "route_name": "Assigned route",
        "created_date": "Customer creation date",
        "sales_hier1_name": "VP Sales",
        "sales_hier2_name": "ZSM",
        "sales_hier3_name": "RSM",
        "sales_hier4_name": "BM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "geo_hier7_name": "Town",
    },
    "salesman_t": {
        "code": "Salesman code (PK) — JOIN with invoice/order via salesman_code",
        "name": "Salesman name",
        "distributor_code": "Associated distributor — JOIN with distributor_t.code",
        "sales_hier1_name": "VP Sales",
        "sales_hier2_name": "ZSM",
        "sales_hier3_name": "RSM",
        "sales_hier4_name": "BM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
        "sales_hier1_code": "VP Sales code",
        "sales_hier2_code": "ZSM code",
        "sales_hier3_code": "RSM code",
        "sales_hier4_code": "BM code",
        "sales_hier5_code": "ASM code",
        "sales_hier6_code": "SO code",
        "geo_hier2_name": "Zone",
        "geo_hier3_name": "Region",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "geo_hier7_name": "Town",
    },
    "sales_force_hier_level_t": {
        "sf_level_code": "Level code (100=VP, 200=ZSM, 300=RSM, 400=BM, 500=ASM, 600=SO)",
        "sf_level_name": "Level name (VP Sales, Zonal Sales Manager, Regional Sales Manager, etc.)",
        "db_column_name": "DB column name (sales_hier1_code, sales_hier2_code, etc.)",
    },
    "sales_force_hier_value_t": {
        "sf_code": "Sales force member code — matches sales_hierN_code in other tables",
        "sf_name": "Sales force member name (e.g. HEAD OF SALES, RSM-SOUTH, SO - CHENNAI)",
        "sf_level_code": "Level (100=VP, 200=ZSM, 300=RSM, 400=BM, 500=ASM, 600=SO)",
        "parent_code": "Parent member code — for hierarchy traversal",
    },
    "rpt_purchase_summary_t": {
        "distributor_code": "Distributor code — JOIN with distributor_t.code",
        "distributor_name": "Distributor name",
        "supplier_code": "Supplier code (NO supplier master table — use supplier_name directly)",
        "supplier_name": "Supplier name",
        "cmp_invoice_number": "Company invoice number",
        "cmp_invoice_date": "Company invoice date (YYYY-MM-DD)",
        "grn_reference_number": "GRN (goods receipt note) number",
        "goods_received_date": "Goods received date (YYYY-MM-DD)",
        "grn_status": "GRN status",
        "product_code": "Product code",
        "product_name": "Product name",
        "product_batch_code": "Product batch code",
        "batch_expiry_date": "Batch expiry date",
        "invoice_qty": "Purchased quantity per company invoice",
        "received_qty": "Quantity actually received",
        "offer_qty": "Free/offer quantity",
        "shortage_qty": "Shortage qty (invoiced but not received)",
        "damage_qty": "Damaged quantity",
        "excess_qty": "Excess quantity received",
        "purchase_price": "Purchase price per unit",
        "mrp": "Maximum retail price",
        "gross_amount": "Purchase gross amount BEFORE tax (= taxable_amount)",
        "taxable_amount": "Taxable purchase amount (before tax)",
        "total_tax_amount": "Total tax on the purchase",
        "discount_amount": "Discount amount on purchase",
        "net_amount": "Purchase NET amount INCLUDING tax — USE THIS for total purchase value/spend",
        "po_reference_number": "Purchase order reference number",
        "product_hier1_name": "Company",
        "product_hier2_name": "Brand",
        "product_hier3_name": "Category",
        "product_hier4_name": "Sub-category",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "geo_hier7_name": "Town",
        "sales_hier3_name": "RSM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
        "distributor_type": "Distributor type",
    },
    "rpt_coverage_productivity_t": {
        "coverage_date": "Date of coverage/visit activity (YYYY-MM-DD)",
        "distributor_code": "Distributor code — JOIN with distributor_t.code",
        "distributor_name": "Distributor name",
        "salesman_code": "Salesman code — JOIN with salesman_t.code",
        "salesman_name": "Salesman name",
        "route_code": "Route code",
        "route_name": "Route name",
        "planned_route_name": "Planned route name",
        "actual_route_name": "Actual route name",
        "no_of_planned_outlets": "Outlets planned to visit",
        "no_of_actual_outlets": "Outlets actually visited",
        "active_outlets": "Active outlets",
        "no_of_outlet_not_visited": "Planned outlets NOT visited",
        "no_of_ordered_outlets_with_TO": "Visited outlets that ordered (with turnover)",
        "no_of_ordered_outlets_without_TO": "Visited outlets with no order",
        "coverage_perc": "Per-row coverage %. For aggregates DO NOT average — compute SUM(no_of_actual_outlets)/SUM(no_of_planned_outlets)*100",
        "productivity_perc_with_TO": "Per-row productivity % (with turnover). Recompute from counts for aggregates",
        "productivity_perc_without_TO": "Per-row productivity % (without turnover)",
        "distance_covered": "Distance covered",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "sales_hier3_name": "RSM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
    },
    "rpt_route_coverage_plan_t": {
        "company_code": "Company code (PPL)",
        "distributor_code": "Distributor code — JOIN with distributor_t.code",
        "distributor_name": "Distributor name",
        "salesman_code": "Salesman code — JOIN with salesman_t.code",
        "salesman_name": "Salesman name",
        "route_code": "Route code",
        "route_name": "Route name",
        "route_type": "Route type",
        "coverage_date": "Planned coverage date (YYYY-MM-DD)",
        "coverage_day": "Planned coverage day (e.g. Monday)",
        "customer_code": "Planned customer/outlet code",
        "customer_name": "Planned customer/outlet name",
        "channel_name": "Customer channel",
        "sub_channel_name": "Customer sub-channel",
        "group_name": "Customer group",
        "class_name": "Customer class",
        "visit_type": "Visit type",
        "salesman_category": "Salesman category",
        "customer_city_name": "Customer city",
        "customer_state_name": "Customer state",
        "geo_hier2_name": "Zone",
        "geo_hier4_name": "State",
        "geo_hier6_name": "District",
        "sales_hier3_name": "RSM",
        "sales_hier5_name": "ASM",
        "sales_hier6_name": "SO",
        "distributor_type": "Distributor type",
    },
}


def format_schema_description(schema_dict: dict[str, dict[str, str]] | None = None) -> str:
    """Render a schema dict as `Table: ...` / `  - col: desc` lines for an LLM prompt.

    Direct port of the prototype's `format_schema_description`. Defaults to
    `SCHEMA_DESCRIPTION` when no dict is supplied.
    """
    schema_dict = SCHEMA_DESCRIPTION if schema_dict is None else schema_dict
    text: list[str] = []
    for table, cols in schema_dict.items():
        text.append(f"Table: {table}")
        for col, desc in cols.items():
            text.append(f"  - {col}: {desc}")
    return "\n".join(text)


# ============================================================
# COLUMN / ALIAS MAPS — used by validate_and_fix_sql (SQL fixer, ported later)
# ============================================================

#: Maps a column name that is UNIQUE to one table -> that table's name.
#: Ported verbatim from the prototype's `COLUMN_TABLE_MAP` (~lines 73-171).
#: Columns shared across multiple tables are intentionally absent here — see
#: `AMBIGUOUS_COLUMNS` below.
COLUMN_TABLE_MAP: dict[str, str] = {
    # distributor_t unique columns
    "lob_code": "distributor_t",
    "billing_weight_roundOff": "distributor_t",
    "EnableBillingOnWeights": "distributor_t",
    "enable_billing_on_weights": "distributor_t",
    # rpt_invoice_summary_t unique columns
    "invoice_number": "rpt_invoice_summary_t",
    "product_code": "rpt_invoice_summary_t",
    "product_batch_code": "rpt_invoice_summary_t",
    "product_name": "rpt_invoice_summary_t",
    "product_company_code": "rpt_invoice_summary_t",
    "invoice_status": "rpt_invoice_summary_t",
    "invoice_source": "rpt_invoice_summary_t",
    "invoice_date": "rpt_invoice_summary_t",
    "measure_1": "rpt_invoice_summary_t",
    "measure_2": "rpt_invoice_summary_t",
    "measure_7": "rpt_invoice_summary_t",
    "measure_9": "rpt_invoice_summary_t",
    "measure_12": "rpt_invoice_summary_t",
    "measure_14": "rpt_invoice_summary_t",
    "taxable_gross_amt": "rpt_invoice_summary_t",
    "delivery_date": "rpt_invoice_summary_t",
    "invoice_type": "rpt_invoice_summary_t",
    "invoice_mode": "rpt_invoice_summary_t",
    # rpt_order_summary_t unique columns
    "order_number": "rpt_order_summary_t",
    "order_date": "rpt_order_summary_t",
    "order_qty": "rpt_order_summary_t",
    "order_value": "rpt_order_summary_t",
    "order_status": "rpt_order_summary_t",
    "invoice_qty": "rpt_order_summary_t",
    "invoice_value": "rpt_order_summary_t",
    "gross_amt": "rpt_order_summary_t",
    "net_amt": "rpt_order_summary_t",
    "purchase_price": "rpt_order_summary_t",
    "mrp": "rpt_order_summary_t",
    "sell_price": "rpt_order_summary_t",
    # rpt_customer_master_t unique columns
    "customer_code": "rpt_customer_master_t",
    "customer_name": "rpt_customer_master_t",
    "email_id": "rpt_customer_master_t",
    "credit_days": "rpt_customer_master_t",
    "credit_limit": "rpt_customer_master_t",
    "phone": "rpt_customer_master_t",
    "mobile_no": "rpt_customer_master_t",
    "is_active": "rpt_customer_master_t",
    "outstanding_amount": "rpt_customer_master_t",
    "outstanding_billcount": "rpt_customer_master_t",
    "geo_city": "rpt_customer_master_t",
    "geo_state": "rpt_customer_master_t",
    "gst_tinno": "rpt_customer_master_t",
    # sales_force_hier_level_t
    "sf_level_code": "sales_force_hier_level_t",
    "sf_level_name": "sales_force_hier_level_t",
    "db_column_name": "sales_force_hier_level_t",
    # sales_force_hier_value_t
    "sf_code": "sales_force_hier_value_t",
    "sf_name": "sales_force_hier_value_t",
    "parent_code": "sales_force_hier_value_t",
    # rpt_purchase_summary_t unique columns
    "supplier_code": "rpt_purchase_summary_t",
    "supplier_name": "rpt_purchase_summary_t",
    "grn_reference_number": "rpt_purchase_summary_t",
    "goods_received_date": "rpt_purchase_summary_t",
    "grn_status": "rpt_purchase_summary_t",
    "gross_amount": "rpt_purchase_summary_t",
    "net_amount": "rpt_purchase_summary_t",
    "total_tax_amount": "rpt_purchase_summary_t",
    "taxable_amount": "rpt_purchase_summary_t",
    "discount_amount": "rpt_purchase_summary_t",
    "received_qty": "rpt_purchase_summary_t",
    "shortage_qty": "rpt_purchase_summary_t",
    "damage_qty": "rpt_purchase_summary_t",
    "excess_qty": "rpt_purchase_summary_t",
    "po_reference_number": "rpt_purchase_summary_t",
    "cmp_invoice_number": "rpt_purchase_summary_t",
    "batch_expiry_date": "rpt_purchase_summary_t",
    # rpt_coverage_productivity_t unique columns
    "no_of_planned_outlets": "rpt_coverage_productivity_t",
    "no_of_actual_outlets": "rpt_coverage_productivity_t",
    "active_outlets": "rpt_coverage_productivity_t",
    "no_of_outlet_not_visited": "rpt_coverage_productivity_t",
    "no_of_ordered_outlets_with_TO": "rpt_coverage_productivity_t",
    "no_of_ordered_outlets_without_TO": "rpt_coverage_productivity_t",
    "coverage_perc": "rpt_coverage_productivity_t",
    "productivity_perc_with_TO": "rpt_coverage_productivity_t",
    "productivity_perc_without_TO": "rpt_coverage_productivity_t",
    "planned_route_name": "rpt_coverage_productivity_t",
    "actual_route_name": "rpt_coverage_productivity_t",
    "distance_covered": "rpt_coverage_productivity_t",
    # rpt_route_coverage_plan_t unique columns
    "coverage_day": "rpt_route_coverage_plan_t",
    "route_type": "rpt_route_coverage_plan_t",
    "visit_type": "rpt_route_coverage_plan_t",
    "salesman_category": "rpt_route_coverage_plan_t",
    "salesforce_code": "rpt_route_coverage_plan_t",
    "distributor_branch_code": "rpt_route_coverage_plan_t",
}

#: Canonical short alias -> table name, ported verbatim from `ALIAS_TABLE_MAP`.
#: These are the ONLY aliases the SQL generator/validator should use.
ALIAS_TABLE_MAP: dict[str, str] = {
    "d": "distributor_t",
    "inv": "rpt_invoice_summary_t",
    "ord": "rpt_order_summary_t",
    "cust": "rpt_customer_master_t",
    "sm": "salesman_t",
    "sfl": "sales_force_hier_level_t",
    "sfv": "sales_force_hier_value_t",
    "pur": "rpt_purchase_summary_t",
    "cov": "rpt_coverage_productivity_t",
    "rcp": "rpt_route_coverage_plan_t",
}

#: Columns that legitimately exist in MULTIPLE tables — a SQL fixer must never
#: auto-rewrite these (doing so breaks valid SQL, e.g. rcp.customer_code ->
#: cust.customer_code). Ported verbatim from the prototype's `AMBIGUOUS_COLUMNS`.
AMBIGUOUS_COLUMNS: frozenset[str] = frozenset(
    {
        "customer_code",
        "customer_name",
        "distributor_code",
        "distributor_name",
        "salesman_code",
        "salesman_name",
        "route_code",
        "route_name",
        "product_code",
        "product_name",
        "invoice_number",
        "invoice_date",
        "order_date",
        "coverage_date",
        "channel_name",
        "group_name",
        "class_name",
        "product_batch_code",
    }
)

# ============================================================
# RELATIONSHIPS — join-key documentation
# ============================================================

#: Join-key documentation, ported verbatim from rule 2 ("JOIN KEYS") of the
#: `generate_query_plan` prompt (~lines 1300-1306).
RELATIONSHIPS: str = """JOIN KEYS:
distributor_t.code       = rpt_invoice_summary_t.distributor_code
distributor_t.code       = rpt_order_summary_t.distributor_code
distributor_t.code       = rpt_customer_master_t.distr_code
distributor_t.code       = salesman_t.distributor_code
salesman_t.code          = rpt_invoice_summary_t.salesman_code
salesman_t.code          = rpt_order_summary_t.salesman_code"""
