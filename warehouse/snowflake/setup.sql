-- Wethr on Snowflake: one-time account setup.
-- Run as ACCOUNTADMIN in a Snowsight worksheet (Projects > Worksheets > + SQL worksheet),
-- top to bottom. Safe to re-run: everything is CREATE ... IF NOT EXISTS or OR REPLACE.
--
-- What it builds:
--   WETHR_WH        XSMALL warehouse, suspends after 60s idle
--   WETHR_MONITOR   monthly credit cap; suspends WETHR_WH at 100%
--   WETHR.RAW       loaded Parquet, one table per SQLite table (written by WETHR_LOADER)
--   WETHR.ANALYTICS dbt output (written by WETHR_TRANSFORMER)
--   WETHR_READER    read-only role for Claude Dashboards / ChatGPT / MCP
--
-- Role hierarchy (each role is granted to the next so SYSADMIN can see everything):
--   WETHR_READER -> WETHR_TRANSFORMER -> SYSADMIN
--   WETHR_LOADER                      -> SYSADMIN

USE ROLE ACCOUNTADMIN;

-- ---------------------------------------------------------------- cost guard
-- 5 credits/month ~= $10 on Standard edition. Raise it if you hit it on purpose.
CREATE OR REPLACE RESOURCE MONITOR WETHR_MONITOR WITH
  CREDIT_QUOTA = 5
  FREQUENCY = MONTHLY
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS
    ON 75 PERCENT DO NOTIFY
    ON 100 PERCENT DO SUSPEND
    ON 110 PERCENT DO SUSPEND_IMMEDIATE;

-- ---------------------------------------------------------------- compute
CREATE WAREHOUSE IF NOT EXISTS WETHR_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Wethr learning warehouse';
ALTER WAREHOUSE WETHR_WH SET RESOURCE_MONITOR = WETHR_MONITOR;

-- Kill runaway queries (default is 2 days).
ALTER WAREHOUSE WETHR_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 600;

-- ---------------------------------------------------------------- storage
USE ROLE SYSADMIN;
CREATE DATABASE IF NOT EXISTS WETHR;
CREATE SCHEMA IF NOT EXISTS WETHR.RAW;
CREATE SCHEMA IF NOT EXISTS WETHR.ANALYTICS;

CREATE FILE FORMAT IF NOT EXISTS WETHR.RAW.PARQUET_FMT
  TYPE = PARQUET
  USE_LOGICAL_TYPE = TRUE        -- keep TIMESTAMPTZ / DATE as real types
  USE_VECTORIZED_SCANNER = TRUE;

-- Internal stage: files uploaded with PUT from this machine.
CREATE STAGE IF NOT EXISTS WETHR.RAW.WETHR_STAGE
  FILE_FORMAT = WETHR.RAW.PARQUET_FMT
  COMMENT = 'Parquet exports from scripts/export_parquet.py';

-- ---------------------------------------------------------------- roles
USE ROLE SECURITYADMIN;
CREATE ROLE IF NOT EXISTS WETHR_LOADER      COMMENT = 'Loads Parquet into WETHR.RAW';
CREATE ROLE IF NOT EXISTS WETHR_TRANSFORMER COMMENT = 'dbt: reads RAW, builds ANALYTICS';
CREATE ROLE IF NOT EXISTS WETHR_READER      COMMENT = 'Read-only, for AI/BI connectors';

GRANT ROLE WETHR_READER      TO ROLE WETHR_TRANSFORMER;
GRANT ROLE WETHR_TRANSFORMER TO ROLE SYSADMIN;
GRANT ROLE WETHR_LOADER      TO ROLE SYSADMIN;

-- Everyone needs the warehouse and the database.
GRANT USAGE ON WAREHOUSE WETHR_WH TO ROLE WETHR_READER;
GRANT USAGE ON WAREHOUSE WETHR_WH TO ROLE WETHR_LOADER;
GRANT USAGE ON DATABASE WETHR     TO ROLE WETHR_READER;
GRANT USAGE ON DATABASE WETHR     TO ROLE WETHR_LOADER;

-- Loader: owns what it creates in RAW.
GRANT USAGE, CREATE TABLE ON SCHEMA WETHR.RAW TO ROLE WETHR_LOADER;
GRANT READ, WRITE ON STAGE WETHR.RAW.WETHR_STAGE TO ROLE WETHR_LOADER;
GRANT USAGE ON FILE FORMAT WETHR.RAW.PARQUET_FMT TO ROLE WETHR_LOADER;

-- Transformer: reads RAW, builds ANALYTICS.
GRANT USAGE ON SCHEMA WETHR.RAW TO ROLE WETHR_TRANSFORMER;
GRANT SELECT ON ALL TABLES IN SCHEMA WETHR.RAW    TO ROLE WETHR_TRANSFORMER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA WETHR.RAW TO ROLE WETHR_TRANSFORMER;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA WETHR.ANALYTICS TO ROLE WETHR_TRANSFORMER;

-- Reader: ANALYTICS only (the clean dbt models). Grant RAW too if you want to
-- compare AI answers on raw vs modelled tables (see docs/DASHBOARDS_GUIDE.md).
GRANT USAGE ON SCHEMA WETHR.ANALYTICS TO ROLE WETHR_READER;
GRANT SELECT ON ALL TABLES IN SCHEMA WETHR.ANALYTICS    TO ROLE WETHR_READER;
GRANT SELECT ON ALL VIEWS IN SCHEMA WETHR.ANALYTICS     TO ROLE WETHR_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA WETHR.ANALYTICS TO ROLE WETHR_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA WETHR.ANALYTICS  TO ROLE WETHR_READER;
-- GRANT USAGE ON SCHEMA WETHR.RAW TO ROLE WETHR_READER;
-- GRANT SELECT ON ALL TABLES IN SCHEMA WETHR.RAW TO ROLE WETHR_READER;
-- GRANT SELECT ON FUTURE TABLES IN SCHEMA WETHR.RAW TO ROLE WETHR_READER;

-- ---------------------------------------------------------------- users
-- Service user for scripted loads and dbt: key-pair auth, no password.
-- Generate the key locally first (see warehouse/README.md), then paste the
-- public key body (no header/footer lines) below.
USE ROLE USERADMIN;
CREATE USER IF NOT EXISTS WETHR_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = WETHR_LOADER
  DEFAULT_WAREHOUSE = WETHR_WH
  TIMEZONE = 'UTC'                  -- session default is America/Los_Angeles; pin it so
                                    -- convert_timezone()/DATE() in dbt never shift by 7-8 h
  COMMENT = 'Scripted loads + dbt';
-- ALTER USER WETHR_SVC SET RSA_PUBLIC_KEY = 'MIIBIjANBgkq...';

USE ROLE SECURITYADMIN;
GRANT ROLE WETHR_LOADER      TO USER WETHR_SVC;
GRANT ROLE WETHR_TRANSFORMER TO USER WETHR_SVC;
-- Let your own login use the roles too:
-- GRANT ROLE WETHR_LOADER, WETHR_TRANSFORMER, WETHR_READER TO USER <your_user>;

-- ---------------------------------------------------------------- check
USE ROLE ACCOUNTADMIN;
SHOW RESOURCE MONITORS LIKE 'WETHR%';
SHOW WAREHOUSES LIKE 'WETHR%';
SHOW GRANTS TO ROLE WETHR_READER;
