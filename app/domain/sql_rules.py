"""SQL generation rules and few-shot examples for the NL→SQL prompt.

Ported from the `generate_query_plan` prompt in `conversational_bot_v15.py`
(~lines 1265-1577): the 18 numbered "CRITICAL RULES" plus the block of
"Example correct SQL" pairs. Rule content is kept verbatim; only light
re-formatting (indentation, wrapping) was applied for readability as a
Python string constant.
"""

from __future__ import annotations

#: The 18 hard rules the SQL-generation prompt must include, verbatim from
#: the prototype (alias conventions, join keys, revenue-column mapping,
#: counting rules, date/MTD/YTD handling via CURDATE(), Pareto, state-name
#: mapping, fact-table isolation, coverage/purchase/route-plan nuances).
SQL_RULES: str = """### CRITICAL RULES (NEVER break these):
1. TABLE ALIASES — always use exactly:
   distributor_t            → d
   rpt_invoice_summary_t    → inv
   rpt_order_summary_t      → ord
   rpt_customer_master_t    → cust
   salesman_t               → sm
   sales_force_hier_level_t → sfl
   sales_force_hier_value_t → sfv
   rpt_purchase_summary_t   → pur
   rpt_coverage_productivity_t → cov
   rpt_route_coverage_plan_t   → rcp

2. JOIN KEYS:
   distributor_t.code       = rpt_invoice_summary_t.distributor_code
   distributor_t.code       = rpt_order_summary_t.distributor_code
   distributor_t.code       = rpt_customer_master_t.distr_code
   distributor_t.code       = salesman_t.distributor_code
   salesman_t.code          = rpt_invoice_summary_t.salesman_code
   salesman_t.code          = rpt_order_summary_t.salesman_code

3. GEOGRAPHY COLUMNS (same across all tables):
   geo_hier2_name = Zone    (SOUTH, EAST, NORTH-EAST)
   geo_hier3_name = Region  (REGION 1, REGION 6)
   geo_hier4_name = State   (TAMILNADU STATE, WB STATE, ANDHRA PRADESH STATE, JHARKHAND STATE)
   geo_hier6_name = District (TIRUCHIRAPPALLI District, CHENNAI District)
   geo_hier7_name = Town    (Trichy Town, Chennai Town)

4. SALES HIERARCHY COLUMNS (same across all tables):
   sales_hier1_name = VP / HEAD OF SALES
   sales_hier2_name = ZSM
   sales_hier3_name = RSM
   sales_hier4_name = BM
   sales_hier5_name = ASM
   sales_hier6_name = SO

5. REVENUE / SALES METRICS:
   Invoice revenue   → rpt_invoice_summary_t.measure_14 (Net Amount). measure_13 is TAX only — never use it for revenue
   Order revenue     → rpt_order_summary_t.gross_amt
   Invoice quantity  → rpt_invoice_summary_t.measure_1
   Order quantity    → rpt_order_summary_t.order_qty
   Purchase value    → rpt_purchase_summary_t.net_amount (tax-incl spend); gross_amount = before tax
   Purchase quantity → rpt_purchase_summary_t.received_qty (or invoice_qty for invoiced qty)
   Coverage metrics  → rpt_coverage_productivity_t uses COUNTS (no_of_planned_outlets,no_of_actual_outlets, active_outlets), NOT money. It has NO revenue.
   NEVER use measure_1 as revenue — it is QUANTITY only

6. COUNTING RULES:
   COUNT distributors → COUNT(DISTINCT d.code) FROM distributor_t d
   COUNT salesmen     → COUNT(DISTINCT sm.code) FROM salesman_t sm
   COUNT customers    → COUNT(DISTINCT cust.customer_code) FROM rpt_customer_master_t cust
   COUNT orders       → COUNT(DISTINCT ord.order_number) FROM rpt_order_summary_t ord
   COUNT invoices     → COUNT(DISTINCT inv.invoice_number) FROM rpt_invoice_summary_t inv
   NEVER COUNT(name) — always COUNT(DISTINCT <primary_key>)

7. SQL on a SINGLE LINE — no newlines inside SQL string.
8. No single quotes inside JSON values.
9. DATE FILTERING: invoice_date and order_date are YYYY-MM-DD format.
   Year filter: YEAR(inv.invoice_date) = 2025
   Month filter: MONTH(inv.invoice_date) = 8

10. PRODUCT HIERARCHY in invoice/order tables:
    product_hier1_name = Company
    product_hier2_name = Group (GROUP-A, GROUP-B)
    product_hier3_name = Category (A - SEMI SWEET, C - CRACKER)
    product_hier4_name = Brand (GOOGLY, MARIE, THINZ)
    product_hier8_name = Most specific product name

11. PARETO: always WHERE CumulativePct <= 80 (NOT >= 80)

12. STATE NAME MAPPING — exact values:
    "Tamil Nadu"    → geo_hier4_name = 'TAMILNADU STATE'
    "West Bengal"   → geo_hier4_name = 'WB STATE'
    "Andhra Pradesh"→ geo_hier4_name = 'ANDHRA PRADESH STATE'
    "Jharkhand"     → geo_hier4_name = 'JHARKHAND STATE'
    "South"         → geo_hier2_name = 'SOUTH'
    "East"          → geo_hier2_name = 'EAST'
    "North East"    → geo_hier2_name = 'NORTH-EAST'

13. SELF-CONTAINED COLUMNS — these columns exist DIRECTLY in
    rpt_invoice_summary_t and rpt_order_summary_t:
    sales_hier1_name, sales_hier2_name, sales_hier3_name,
    sales_hier4_name, sales_hier5_name, sales_hier6_name,
    sales_hier1_code, sales_hier2_code, sales_hier3_code,
    sales_hier4_code, sales_hier5_code, sales_hier6_code,
    geo_hier2_name, geo_hier4_name, geo_hier6_name, geo_hier7_name,
    distributor_name, salesman_name, product_name,
    retailer_name, route_name

    NEVER JOIN distributor_t or salesman_t JUST to filter
    by sales hierarchy or geography when querying invoice or order tables.

    WRONG: SELECT inv.sales_hier5_name FROM rpt_invoice_summary_t inv
           JOIN distributor_t d ON inv.distributor_code = d.code
           WHERE d.sales_hier3_name = 'RSM-SOUTH'

    CORRECT: SELECT inv.sales_hier5_name FROM rpt_invoice_summary_t inv
             WHERE inv.sales_hier3_name = 'RSM-SOUTH'

    Only JOIN distributor_t when you need columns that are EXCLUSIVELY
    in distributor_t like: d.code, d.lob_code, d.distributor_type,
    d.created_date, d.last_modified_date

14. RELATIVE / TO-DATE PERIODS (MTD, YTD, QTD, today, yesterday, last month):
    - ANCHOR every relative period to the REAL CALENDAR via CURDATE().
      NEVER hardcode a year/month, and NEVER use MAX(date) as the anchor.
    - Invoice/revenue questions → rpt_invoice_summary_t + invoice_date.
      Order questions → rpt_order_summary_t + order_date.
    - If no rows fall in the window, the system reports 0 and the latest data date.
      NEVER invent a number or a date to fill an empty period.

    MTD (month to date):
      invoice_date >= DATE_FORMAT(CURDATE(),'%Y-%m-01') AND invoice_date <= CURDATE()
    YTD (year to date):
      invoice_date >= DATE_FORMAT(CURDATE(),'%Y-01-01') AND invoice_date <= CURDATE()
    QTD (quarter to date):
      invoice_date >= MAKEDATE(YEAR(CURDATE()),1) + INTERVAL (QUARTER(CURDATE())-1)*3 MONTH AND invoice_date <= CURDATE()
    Today:        invoice_date = CURDATE()
    Yesterday:    invoice_date = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
    Last month (full prior month):
      invoice_date >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH,'%Y-%m-01') AND invoice_date < DATE_FORMAT(CURDATE(),'%Y-%m-01')
    Last N days:  invoice_date >= DATE_SUB(CURDATE(), INTERVAL N DAY) AND invoice_date <= CURDATE()

    - For order questions swap invoice_date→order_date.

15. NEVER JOIN TWO FACT TABLES TO EACH OTHER. The fact tables are:
    rpt_invoice_summary_t, rpt_order_summary_t, rpt_purchase_summary_t,
    rpt_coverage_productivity_t, rpt_route_coverage_plan_t.
    Joining any two of these multiplies rows and produces WRONG inflated sums.
    Each question must query exactly ONE fact table. You MAY still join a fact
    table to the dimension tables distributor_t / salesman_t when you need a
    column that lives ONLY there. If a question seems to need two fact tables
    (e.g. "purchase vs sales"), answer using ONE table and state what it covers —
    do NOT fabricate a combined number.

16. COVERAGE / PRODUCTIVITY AGGREGATES (rpt_coverage_productivity_t):
    - coverage_perc and productivity_perc_* are PER-ROW percentages.
      NEVER SUM them, and NEVER AVG them across rows — averaging ratios is wrong.
    - For an aggregate coverage %, recompute from counts:
        ROUND(SUM(no_of_actual_outlets) / NULLIF(SUM(no_of_planned_outlets),0) * 100, 2)
    - For raw activity, SUM the count columns (no_of_planned_outlets, no_of_actual_outlets).
    - This table is per salesman-route-DAY, so always consider a date filter.

17. PURCHASE (rpt_purchase_summary_t):
    - It has NO salesman_code — never group purchases by salesman.
    - Supplier has no master table — use supplier_name / supplier_code directly.
    - "Purchase value/spend" → SUM(pur.net_amount). "Before tax" → SUM(pur.gross_amount).
    - shortage_qty / damage_qty / excess_qty describe receiving discrepancies.

18. ROUTE PLAN (rpt_route_coverage_plan_t) is one row per planned VISIT (salesman-route-
    customer-day). Listing a column without aggregation floods duplicates.
    - "which routes / what routes for salesman X" → SELECT DISTINCT rcp.route_name ...
    - "how many outlets per route" → COUNT(DISTINCT rcp.customer_code) GROUP BY route_name
    - "which customers/outlets on route X" → SELECT DISTINCT rcp.customer_name ...
    Always use DISTINCT or COUNT(DISTINCT ...) — never list raw rows from this table.

19. USER SCOPE / "MY", "OUR", "MINE" — CRITICAL:
    The user's territory and data-access scope are applied AUTOMATICALLY by the
    system (RBAC) AFTER your SQL runs. Words like "my", "our", "mine", "my zone",
    "my region", "my area", "my team", "in my territory" do NOT go into the SQL.
    - Treat "sales in my zone" / "my MTD sales" EXACTLY like "total sales" — just
      the metric, with NO geo_hier* or sales_hier* filter and no GROUP BY unless a
      breakdown word ("by", "per", "wise", "each") is present.
    - NEVER add a geo_hier* or sales_hier* filter the user did not name explicitly.
    - NEVER invent literal values like 'My Zone', 'My Region', 'My Area', or
      'VP Sales'. No such value exists — e.g. `geo_hier2_name = 'My Zone'` matches
      ZERO rows and produces an empty (wrong) result.
    - Only add a geo/hierarchy filter when the user names a REAL place
      (e.g. "in Chennai", "Tamil Nadu", "South zone") — see rules 3 and 12.
    - NEVER switch to a conversational answer to ask who the user is or for a
      distributor/role code — the scope is already known to the system."""


#: Question → SQL few-shot pairs, ported verbatim from the "Example correct
#: SQL (single line)" block of the prototype prompt (~lines 1442-1509).
FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "question": "Distributor count by state",
        "sql": "SELECT d.geo_hier4_name AS State, COUNT(DISTINCT d.code) AS DistributorCount FROM distributor_t d GROUP BY d.geo_hier4_name ORDER BY DistributorCount DESC LIMIT 10",
    },
    {
        "question": "Top products by invoice revenue in a state",
        "sql": "SELECT inv.product_name, SUM(inv.measure_14) AS TotalRevenue FROM rpt_invoice_summary_t inv WHERE inv.geo_hier4_name = 'WB STATE' GROUP BY inv.product_name ORDER BY TotalRevenue DESC LIMIT 10",
    },
    {
        "question": "Total order value by zone",
        "sql": "SELECT ord.geo_hier2_name AS Zone, SUM(ord.gross_amt) AS TotalOrderValue FROM rpt_order_summary_t ord GROUP BY ord.geo_hier2_name ORDER BY TotalOrderValue DESC LIMIT 10",
    },
    {
        "question": "Active customer count per distributor",
        "sql": "SELECT cust.distributor_name, COUNT(DISTINCT cust.customer_code) AS CustomerCount FROM rpt_customer_master_t cust WHERE cust.is_active = 'Y' GROUP BY cust.distributor_name ORDER BY CustomerCount DESC LIMIT 10",
    },
    {
        "question": "RSM wise distributor count",
        "sql": "SELECT d.sales_hier3_name AS RSM, COUNT(DISTINCT d.code) AS DistributorCount FROM distributor_t d GROUP BY d.sales_hier3_name ORDER BY DistributorCount DESC LIMIT 10",
    },
    {
        "question": "Pareto 80% revenue",
        "sql": "WITH rev AS (SELECT inv.product_name, SUM(inv.measure_14) AS TotalRevenue FROM rpt_invoice_summary_t inv GROUP BY inv.product_name ORDER BY TotalRevenue DESC), tot AS (SELECT SUM(TotalRevenue) AS Grand FROM rev), cum AS (SELECT r.product_name, r.TotalRevenue, ROUND(SUM(r.TotalRevenue) OVER (ORDER BY r.TotalRevenue DESC) * 100.0 / t.Grand, 2) AS CumulativePct FROM rev r, tot t) SELECT product_name, TotalRevenue, CumulativePct FROM cum WHERE CumulativePct <= 80",
    },
    {
        "question": "ASM revenue under RSM (NO JOIN needed — hierarchy already in invoice)",
        "sql": "SELECT inv.sales_hier5_name AS ASM, SUM(inv.measure_14) AS TotalRevenue FROM rpt_invoice_summary_t inv WHERE inv.sales_hier3_name = 'RSM-SOUTH' GROUP BY inv.sales_hier5_name ORDER BY TotalRevenue DESC LIMIT 20",
    },
    {
        "question": "RSM revenue summary (NO JOIN needed)",
        "sql": "SELECT inv.sales_hier3_name AS RSM, SUM(inv.measure_14) AS TotalRevenue, COUNT(DISTINCT inv.distributor_code) AS DistributorCount FROM rpt_invoice_summary_t inv GROUP BY inv.sales_hier3_name ORDER BY TotalRevenue DESC LIMIT 20",
    },
    {
        "question": "MTD invoice revenue by state",
        "sql": "SELECT inv.geo_hier4_name AS State, SUM(inv.measure_14) AS MTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-%m-01') AND inv.invoice_date <= CURDATE() GROUP BY inv.geo_hier4_name ORDER BY MTDRevenue DESC LIMIT 10",
    },
    {
        "question": "MTD total invoice revenue (single number)",
        "sql": "SELECT SUM(inv.measure_14) AS MTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-%m-01') AND inv.invoice_date <= CURDATE()",
    },
    {
        "question": "MTD total order value (single number)",
        "sql": "SELECT SUM(ord.gross_amt) AS MTDOrderValue FROM rpt_order_summary_t ord WHERE ord.order_date >= DATE_FORMAT(CURDATE(),'%Y-%m-01') AND ord.order_date <= CURDATE()",
    },
    {
        "question": "YTD invoice revenue by ASM",
        "sql": "SELECT inv.sales_hier5_name AS ASM, SUM(inv.measure_14) AS YTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-01-01') AND inv.invoice_date <= CURDATE() GROUP BY inv.sales_hier5_name ORDER BY YTDRevenue DESC LIMIT 20",
    },
    {
        "question": "Total purchase value by supplier",
        "sql": "SELECT pur.supplier_name, SUM(pur.net_amount) AS TotalPurchaseValue FROM rpt_purchase_summary_t pur GROUP BY pur.supplier_name ORDER BY TotalPurchaseValue DESC LIMIT 10",
    },
    {
        "question": "Purchase value by state (before tax)",
        "sql": "SELECT pur.geo_hier4_name AS State, SUM(pur.gross_amount) AS PurchaseBeforeTax FROM rpt_purchase_summary_t pur GROUP BY pur.geo_hier4_name ORDER BY PurchaseBeforeTax DESC LIMIT 10",
    },
    {
        "question": "Products with highest damage quantity",
        "sql": "SELECT pur.product_name, SUM(pur.damage_qty) AS TotalDamage FROM rpt_purchase_summary_t pur GROUP BY pur.product_name ORDER BY TotalDamage DESC LIMIT 10",
    },
    {
        "question": "Coverage % by ASM (recomputed from counts, NOT averaged)",
        "sql": "SELECT cov.sales_hier5_name AS ASM, ROUND(SUM(cov.no_of_actual_outlets) / NULLIF(SUM(cov.no_of_planned_outlets),0) * 100, 2) AS CoveragePct FROM rpt_coverage_productivity_t cov GROUP BY cov.sales_hier5_name ORDER BY CoveragePct DESC LIMIT 20",
    },
    {
        "question": "Outlets planned vs visited by salesman",
        "sql": "SELECT cov.salesman_name, SUM(cov.no_of_planned_outlets) AS Planned, SUM(cov.no_of_actual_outlets) AS Visited FROM rpt_coverage_productivity_t cov GROUP BY cov.salesman_name ORDER BY Planned DESC LIMIT 20",
    },
    {
        "question": "Planned outlet count per route (route plan)",
        "sql": "SELECT rcp.route_name, COUNT(DISTINCT rcp.customer_code) AS PlannedOutlets FROM rpt_route_coverage_plan_t rcp GROUP BY rcp.route_name ORDER BY PlannedOutlets DESC LIMIT 20",
    },
    {
        "question": "Planned routes for a salesman (DISTINCT — no duplicate flood)",
        "sql": "SELECT DISTINCT rcp.route_name FROM rpt_route_coverage_plan_t rcp WHERE rcp.salesman_name = 'Bhuvan' LIMIT 50",
    },
    {
        "question": "YTD total invoice revenue (single number, NO grouping)",
        "sql": "SELECT SUM(inv.measure_14) AS YTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-01-01') AND inv.invoice_date <= CURDATE()",
    },
    {
        "question": "what is my ytd sales (single total, NO grouping, NO invented filter)",
        "sql": "SELECT SUM(inv.measure_14) AS YTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-01-01') AND inv.invoice_date <= CURDATE()",
    },
    {
        "question": "what is my mtd sales (single total, NO grouping, NO invented filter)",
        "sql": "SELECT SUM(inv.measure_14) AS MTDRevenue FROM rpt_invoice_summary_t inv WHERE inv.invoice_date >= DATE_FORMAT(CURDATE(),'%Y-%m-01') AND inv.invoice_date <= CURDATE()",
    },
]
