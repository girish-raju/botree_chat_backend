-- ============================================================================
-- SYNTHETIC local stand-in for the real "Bisk Farm" MySQL analytics database.
--
-- This is NOT the production schema. It is a hand-built test double that
-- covers exactly the 10 tables in app.domain.schema_catalog.INCLUDE_TABLES,
-- every column named in SCHEMA_DESCRIPTION for each of those tables, PLUS the
-- `sales_hierN_code` columns that app/rbac/injector.py assumes exist on any
-- table whenever the matching `sales_hierN_name` column is present in the
-- catalog (see the "Fidelity / assumption note" in injector.py). Without
-- those extra `_code` columns, every RBAC-scoped query from a restricted user
-- (zsm/rsm/bm/asm) would fail at execution with an "unknown column" error.
--
-- Kept deliberately forgiving (nullable columns, surrogate integer PKs on the
-- fact tables) since this only needs to be good enough to exercise the NL->SQL
-- pipeline end-to-end locally, not to be production-grade DDL.
-- ============================================================================

CREATE DATABASE IF NOT EXISTS biskfarm_report_pp3
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE biskfarm_report_pp3;

-- ----------------------------------------------------------------------------
-- distributor_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS distributor_t;
CREATE TABLE distributor_t (
    code                VARCHAR(32) NOT NULL PRIMARY KEY,
    name                VARCHAR(255),
    lob_code            VARCHAR(32),
    sales_hier1_name    VARCHAR(255),
    sales_hier2_name    VARCHAR(255),
    sales_hier3_name    VARCHAR(255),
    sales_hier4_name    VARCHAR(255),
    sales_hier5_name    VARCHAR(255),
    sales_hier6_name    VARCHAR(255),
    sales_hier1_code    VARCHAR(32),
    sales_hier2_code    VARCHAR(32),
    sales_hier3_code    VARCHAR(32),
    sales_hier4_code    VARCHAR(32),
    sales_hier5_code    VARCHAR(32),
    sales_hier6_code    VARCHAR(32),
    geo_hier1_name      VARCHAR(255),
    geo_hier2_name      VARCHAR(255),
    geo_hier3_name      VARCHAR(255),
    geo_hier4_name      VARCHAR(255),
    geo_hier5_name      VARCHAR(255),
    geo_hier6_name      VARCHAR(255),
    geo_hier7_name      VARCHAR(255),
    geo_hier1_code      VARCHAR(32),
    geo_hier2_code      VARCHAR(32),
    geo_hier3_code      VARCHAR(32),
    geo_hier4_code      VARCHAR(32),
    geo_hier6_code      VARCHAR(32),
    geo_hier7_code      VARCHAR(32),
    distributor_type    VARCHAR(64),
    distributor_code    VARCHAR(32),
    last_modified_date  DATETIME,
    created_date        DATETIME,
    INDEX idx_dist_geo7 (geo_hier7_name),
    INDEX idx_dist_geo6 (geo_hier6_name),
    INDEX idx_dist_geo4 (geo_hier4_name),
    INDEX idx_dist_geo2 (geo_hier2_name)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- salesman_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS salesman_t;
CREATE TABLE salesman_t (
    code                VARCHAR(32) NOT NULL PRIMARY KEY,
    name                VARCHAR(255),
    distributor_code    VARCHAR(32),
    sales_hier1_name    VARCHAR(255),
    sales_hier2_name    VARCHAR(255),
    sales_hier3_name    VARCHAR(255),
    sales_hier4_name    VARCHAR(255),
    sales_hier5_name    VARCHAR(255),
    sales_hier6_name    VARCHAR(255),
    sales_hier1_code    VARCHAR(32),
    sales_hier2_code    VARCHAR(32),
    sales_hier3_code    VARCHAR(32),
    sales_hier4_code    VARCHAR(32),
    sales_hier5_code    VARCHAR(32),
    sales_hier6_code    VARCHAR(32),
    geo_hier2_name      VARCHAR(255),
    geo_hier3_name      VARCHAR(255),
    geo_hier4_name      VARCHAR(255),
    geo_hier6_name      VARCHAR(255),
    geo_hier7_name      VARCHAR(255),
    INDEX idx_sm_dist (distributor_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- sales_force_hier_level_t  (static, global, 6 rows: VP..SO)
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS sales_force_hier_level_t;
CREATE TABLE sales_force_hier_level_t (
    sf_level_code   INT NOT NULL PRIMARY KEY,
    sf_level_name   VARCHAR(128),
    db_column_name  VARCHAR(64)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- sales_force_hier_value_t  (VP -> ZSM -> RSM -> BM -> ASM -> SO tree)
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS sales_force_hier_value_t;
CREATE TABLE sales_force_hier_value_t (
    sf_code         VARCHAR(32) NOT NULL PRIMARY KEY,
    sf_name         VARCHAR(255),
    sf_level_code   INT,
    parent_code     VARCHAR(32),
    INDEX idx_sfv_parent (parent_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_customer_master_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_customer_master_t;
CREATE TABLE rpt_customer_master_t (
    cmp_code            VARCHAR(32) NOT NULL,
    distr_code           VARCHAR(32) NOT NULL,
    customer_code        VARCHAR(32) NOT NULL,
    customer_name        VARCHAR(255),
    email_id             VARCHAR(255),
    phone                VARCHAR(32),
    mobile_no            VARCHAR(32),
    pin_no               VARCHAR(16),
    credit_days          INT,
    credit_limit         DECIMAL(18,2),
    is_active            VARCHAR(1),
    outstanding_amount   DECIMAL(18,2),
    outstanding_billcount INT,
    geo_city             VARCHAR(128),
    geo_state            VARCHAR(128),
    channel_name         VARCHAR(128),
    sub_channelname      VARCHAR(128),
    group_name           VARCHAR(128),
    class_name           VARCHAR(64),
    gst_tinno            VARCHAR(32),
    distributor_name     VARCHAR(255),
    salesman_name        VARCHAR(255),
    route_name           VARCHAR(255),
    created_date         DATETIME,
    sales_hier1_name     VARCHAR(255),
    sales_hier2_name     VARCHAR(255),
    sales_hier3_name     VARCHAR(255),
    sales_hier4_name     VARCHAR(255),
    sales_hier5_name     VARCHAR(255),
    sales_hier6_name     VARCHAR(255),
    sales_hier1_code     VARCHAR(32),
    sales_hier2_code     VARCHAR(32),
    sales_hier3_code     VARCHAR(32),
    sales_hier4_code     VARCHAR(32),
    sales_hier5_code     VARCHAR(32),
    sales_hier6_code     VARCHAR(32),
    geo_hier2_name       VARCHAR(255),
    geo_hier4_name       VARCHAR(255),
    geo_hier6_name       VARCHAR(255),
    geo_hier7_name       VARCHAR(255),
    PRIMARY KEY (cmp_code, distr_code, customer_code),
    INDEX idx_cust_geo7 (geo_hier7_name),
    INDEX idx_cust_geo6 (geo_hier6_name)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_invoice_summary_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_invoice_summary_t;
CREATE TABLE rpt_invoice_summary_t (
    id                      INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    invoice_number          VARCHAR(64),
    product_code            VARCHAR(32),
    distributor_code        VARCHAR(32),
    product_batch_code      VARCHAR(64),
    product_name            VARCHAR(255),
    product_company_code    VARCHAR(32),
    distributor_name        VARCHAR(255),
    salesman_code           VARCHAR(32),
    salesman_name            VARCHAR(255),
    route_code               VARCHAR(32),
    route_name               VARCHAR(255),
    retailer_channel_name    VARCHAR(128),
    retailer_group_name      VARCHAR(128),
    retailer_class_name      VARCHAR(64),
    retailer_code            VARCHAR(32),
    retailer_name            VARCHAR(255),
    invoice_status           VARCHAR(32),
    invoice_source           VARCHAR(64),
    invoice_date             DATE,
    invoice_type             VARCHAR(32),
    invoice_mode             VARCHAR(32),
    measure_1                DECIMAL(18,2),
    measure_7                DECIMAL(18,2),
    measure_9                DECIMAL(18,2),
    measure_12               DECIMAL(18,2),
    measure_14               DECIMAL(18,2),
    measure_13               DECIMAL(18,2),
    taxable_gross_amt        DECIMAL(18,2),
    delivery_date            DATE,
    sales_hier1_name         VARCHAR(255),
    sales_hier2_name         VARCHAR(255),
    sales_hier3_name         VARCHAR(255),
    sales_hier4_name         VARCHAR(255),
    sales_hier5_name         VARCHAR(255),
    sales_hier6_name         VARCHAR(255),
    sales_hier1_code         VARCHAR(32),
    sales_hier2_code         VARCHAR(32),
    sales_hier3_code         VARCHAR(32),
    sales_hier4_code         VARCHAR(32),
    sales_hier5_code         VARCHAR(32),
    sales_hier6_code         VARCHAR(32),
    geo_hier2_name           VARCHAR(255),
    geo_hier4_name           VARCHAR(255),
    geo_hier6_name           VARCHAR(255),
    geo_hier7_name           VARCHAR(255),
    product_hier1_name       VARCHAR(255),
    product_hier2_name       VARCHAR(128),
    product_hier3_name       VARCHAR(128),
    product_hier4_name       VARCHAR(128),
    product_hier8_name       VARCHAR(255),
    INDEX idx_inv_date (invoice_date),
    INDEX idx_inv_dist (distributor_code),
    INDEX idx_inv_geo7 (geo_hier7_name),
    INDEX idx_inv_geo6 (geo_hier6_name),
    INDEX idx_inv_geo4 (geo_hier4_name),
    INDEX idx_inv_geo2 (geo_hier2_name),
    INDEX idx_inv_h1 (sales_hier1_code),
    INDEX idx_inv_h2 (sales_hier2_code),
    INDEX idx_inv_h3 (sales_hier3_code),
    INDEX idx_inv_h4 (sales_hier4_code),
    INDEX idx_inv_h5 (sales_hier5_code),
    INDEX idx_inv_h6 (sales_hier6_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_order_summary_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_order_summary_t;
CREATE TABLE rpt_order_summary_t (
    id                      INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    distributor_code        VARCHAR(32),
    order_number             VARCHAR(64),
    product_code             VARCHAR(32),
    product_name              VARCHAR(255),
    distributor_name          VARCHAR(255),
    salesman_code             VARCHAR(32),
    salesman_name              VARCHAR(255),
    route_code                 VARCHAR(32),
    route_name                 VARCHAR(255),
    retailer_code               VARCHAR(32),
    retailer_name               VARCHAR(255),
    retailer_channel_name       VARCHAR(128),
    retailer_group_name         VARCHAR(128),
    retailer_class_name         VARCHAR(64),
    order_date                  DATE,
    order_qty                   INT,
    order_value                 DECIMAL(18,2),
    purchase_price               DECIMAL(18,2),
    mrp                          DECIMAL(18,2),
    sell_price                   DECIMAL(18,2),
    order_status                 VARCHAR(32),
    invoice_number                VARCHAR(64),
    invoice_qty                   INT,
    invoice_value                 DECIMAL(18,2),
    gross_amt                     DECIMAL(18,2),
    net_amt                       DECIMAL(18,2),
    product_hier1_name            VARCHAR(255),
    product_hier2_name             VARCHAR(128),
    product_hier3_name             VARCHAR(128),
    product_hier4_name             VARCHAR(128),
    geo_hier2_name                  VARCHAR(255),
    geo_hier4_name                  VARCHAR(255),
    geo_hier6_name                  VARCHAR(255),
    geo_hier7_name                  VARCHAR(255),
    sales_hier1_name                VARCHAR(255),
    sales_hier2_name                VARCHAR(255),
    sales_hier3_name                VARCHAR(255),
    sales_hier4_name                VARCHAR(255),
    sales_hier5_name                VARCHAR(255),
    sales_hier6_name                VARCHAR(255),
    sales_hier1_code                VARCHAR(32),
    sales_hier2_code                VARCHAR(32),
    sales_hier3_code                VARCHAR(32),
    sales_hier4_code                VARCHAR(32),
    sales_hier5_code                VARCHAR(32),
    sales_hier6_code                VARCHAR(32),
    invoice_date                    DATE,
    source                          VARCHAR(64),
    INDEX idx_ord_date (order_date),
    INDEX idx_ord_dist (distributor_code),
    INDEX idx_ord_geo7 (geo_hier7_name),
    INDEX idx_ord_geo6 (geo_hier6_name),
    INDEX idx_ord_geo4 (geo_hier4_name),
    INDEX idx_ord_geo2 (geo_hier2_name),
    INDEX idx_ord_h1 (sales_hier1_code),
    INDEX idx_ord_h2 (sales_hier2_code),
    INDEX idx_ord_h3 (sales_hier3_code),
    INDEX idx_ord_h4 (sales_hier4_code),
    INDEX idx_ord_h5 (sales_hier5_code),
    INDEX idx_ord_h6 (sales_hier6_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_purchase_summary_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_purchase_summary_t;
CREATE TABLE rpt_purchase_summary_t (
    id                        INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    distributor_code          VARCHAR(32),
    distributor_name          VARCHAR(255),
    supplier_code             VARCHAR(32),
    supplier_name             VARCHAR(255),
    cmp_invoice_number        VARCHAR(64),
    cmp_invoice_date          DATE,
    grn_reference_number      VARCHAR(64),
    goods_received_date       DATE,
    grn_status                VARCHAR(32),
    product_code              VARCHAR(32),
    product_name              VARCHAR(255),
    product_batch_code        VARCHAR(64),
    batch_expiry_date         DATE,
    invoice_qty               INT,
    received_qty              INT,
    offer_qty                 INT,
    shortage_qty              INT,
    damage_qty                INT,
    excess_qty                INT,
    purchase_price            DECIMAL(18,2),
    mrp                       DECIMAL(18,2),
    gross_amount              DECIMAL(18,2),
    taxable_amount            DECIMAL(18,2),
    total_tax_amount          DECIMAL(18,2),
    discount_amount           DECIMAL(18,2),
    net_amount                DECIMAL(18,2),
    po_reference_number       VARCHAR(64),
    product_hier1_name        VARCHAR(255),
    product_hier2_name        VARCHAR(128),
    product_hier3_name        VARCHAR(128),
    product_hier4_name        VARCHAR(128),
    geo_hier2_name            VARCHAR(255),
    geo_hier4_name            VARCHAR(255),
    geo_hier6_name            VARCHAR(255),
    geo_hier7_name            VARCHAR(255),
    sales_hier3_name          VARCHAR(255),
    sales_hier5_name          VARCHAR(255),
    sales_hier6_name          VARCHAR(255),
    sales_hier3_code          VARCHAR(32),
    sales_hier5_code          VARCHAR(32),
    sales_hier6_code          VARCHAR(32),
    distributor_type          VARCHAR(64),
    INDEX idx_pur_date (goods_received_date),
    INDEX idx_pur_dist (distributor_code),
    INDEX idx_pur_geo7 (geo_hier7_name),
    INDEX idx_pur_geo6 (geo_hier6_name),
    INDEX idx_pur_geo4 (geo_hier4_name),
    INDEX idx_pur_geo2 (geo_hier2_name),
    INDEX idx_pur_h3 (sales_hier3_code),
    INDEX idx_pur_h5 (sales_hier5_code),
    INDEX idx_pur_h6 (sales_hier6_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_coverage_productivity_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_coverage_productivity_t;
CREATE TABLE rpt_coverage_productivity_t (
    id                                  INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    coverage_date                       DATE,
    distributor_code                    VARCHAR(32),
    distributor_name                    VARCHAR(255),
    salesman_code                       VARCHAR(32),
    salesman_name                       VARCHAR(255),
    route_code                          VARCHAR(32),
    route_name                          VARCHAR(255),
    planned_route_name                  VARCHAR(255),
    actual_route_name                   VARCHAR(255),
    no_of_planned_outlets               INT,
    no_of_actual_outlets                INT,
    active_outlets                      INT,
    no_of_outlet_not_visited            INT,
    no_of_ordered_outlets_with_TO        INT,
    no_of_ordered_outlets_without_TO     INT,
    coverage_perc                       DECIMAL(9,2),
    productivity_perc_with_TO            DECIMAL(9,2),
    productivity_perc_without_TO         DECIMAL(9,2),
    distance_covered                    DECIMAL(9,2),
    geo_hier2_name                      VARCHAR(255),
    geo_hier4_name                      VARCHAR(255),
    geo_hier6_name                      VARCHAR(255),
    sales_hier3_name                    VARCHAR(255),
    sales_hier5_name                    VARCHAR(255),
    sales_hier6_name                    VARCHAR(255),
    sales_hier3_code                    VARCHAR(32),
    sales_hier5_code                    VARCHAR(32),
    sales_hier6_code                    VARCHAR(32),
    INDEX idx_cov_date (coverage_date),
    INDEX idx_cov_dist (distributor_code),
    INDEX idx_cov_geo6 (geo_hier6_name),
    INDEX idx_cov_geo4 (geo_hier4_name),
    INDEX idx_cov_geo2 (geo_hier2_name),
    INDEX idx_cov_h3 (sales_hier3_code),
    INDEX idx_cov_h5 (sales_hier5_code),
    INDEX idx_cov_h6 (sales_hier6_code)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- rpt_route_coverage_plan_t
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS rpt_route_coverage_plan_t;
CREATE TABLE rpt_route_coverage_plan_t (
    id                          INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    company_code                VARCHAR(32),
    distributor_code            VARCHAR(32),
    distributor_name            VARCHAR(255),
    salesman_code                VARCHAR(32),
    salesman_name                 VARCHAR(255),
    route_code                    VARCHAR(32),
    route_name                    VARCHAR(255),
    route_type                    VARCHAR(32),
    coverage_date                 DATE,
    coverage_day                  VARCHAR(16),
    customer_code                 VARCHAR(32),
    customer_name                 VARCHAR(255),
    channel_name                  VARCHAR(128),
    sub_channel_name              VARCHAR(128),
    group_name                    VARCHAR(128),
    class_name                    VARCHAR(64),
    visit_type                    VARCHAR(32),
    salesman_category             VARCHAR(64),
    customer_city_name            VARCHAR(128),
    customer_state_name           VARCHAR(128),
    geo_hier2_name                 VARCHAR(255),
    geo_hier4_name                 VARCHAR(255),
    geo_hier6_name                 VARCHAR(255),
    sales_hier3_name               VARCHAR(255),
    sales_hier5_name               VARCHAR(255),
    sales_hier6_name               VARCHAR(255),
    sales_hier3_code               VARCHAR(32),
    sales_hier5_code               VARCHAR(32),
    sales_hier6_code               VARCHAR(32),
    distributor_type               VARCHAR(64),
    INDEX idx_rcp_date (coverage_date),
    INDEX idx_rcp_dist (distributor_code),
    INDEX idx_rcp_geo6 (geo_hier6_name),
    INDEX idx_rcp_geo4 (geo_hier4_name),
    INDEX idx_rcp_geo2 (geo_hier2_name),
    INDEX idx_rcp_h3 (sales_hier3_code),
    INDEX idx_rcp_h5 (sales_hier5_code),
    INDEX idx_rcp_h6 (sales_hier6_code)
) ENGINE=InnoDB;
