-- Wethr on Snowflake: plumbing for AI clients (Claude Dashboards/connectors, ChatGPT).
-- Run AFTER setup.sql and after dbt has built WETHR.ANALYTICS.
--
-- Verify against current docs before running: the Claude-side steps come from
-- Snowflake's docs plus third-party write-ups, not an official Anthropic guide.
--   https://docs.snowflake.com/en/sql-reference/sql/create-mcp-server
--   https://docs.snowflake.com/en/user-guide/oauth-custom
--
-- Security model: every AI client signs in as WETHR_AI, which holds ONLY
-- WETHR_READER (SELECT on ANALYTICS). Read-only is enforced by Snowflake RBAC,
-- not by trusting the client to only send SELECTs.

-- ---------------------------------------------------------------- AI login user
USE ROLE USERADMIN;
CREATE USER IF NOT EXISTS WETHR_AI
  TYPE = PERSON                       -- OAuth sign-in is an interactive browser flow
  PASSWORD = '<choose-a-strong-password>'
  MUST_CHANGE_PASSWORD = TRUE
  DEFAULT_ROLE = WETHR_READER
  DEFAULT_WAREHOUSE = WETHR_WH
  TIMEZONE = 'UTC'                    -- same dates as dbt sees, whatever the client's locale
  COMMENT = 'Read-only identity for Claude / ChatGPT connectors';

USE ROLE SECURITYADMIN;
GRANT ROLE WETHR_READER TO USER WETHR_AI;

-- ---------------------------------------------------------------- managed MCP server
-- Exposes one tool: run SQL. Reachable at
--   https://<account_url>/api/v2/databases/WETHR/schemas/ANALYTICS/mcp-servers/WETHR_MCP
USE ROLE SYSADMIN;
CREATE OR REPLACE MCP SERVER WETHR.ANALYTICS.WETHR_MCP
  FROM SPECIFICATION $$
    tools:
      - title: "Wethr SQL"
        name: "wethr_sql"
        type: "SYSTEM_EXECUTE_SQL"
        description: >
          Run read-only SQL against WETHR.ANALYTICS (dbt marts: fct_trades,
          daily_pnl_by_epoch, calibration_by_bracket, market_spread_daily).
  $$;

USE ROLE SECURITYADMIN;
GRANT USAGE ON MCP SERVER WETHR.ANALYTICS.WETHR_MCP TO ROLE WETHR_READER;

-- ---------------------------------------------------------------- OAuth for claude.ai
-- Claude's connector UI asks for a client ID + secret (no dynamic registration).
USE ROLE ACCOUNTADMIN;
CREATE SECURITY INTEGRATION IF NOT EXISTS CLAUDE_OAUTH
  TYPE = OAUTH
  OAUTH_CLIENT = CUSTOM
  OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
  OAUTH_REDIRECT_URI = 'https://claude.ai/api/mcp/auth_callback'
  OAUTH_ISSUE_REFRESH_TOKENS = TRUE
  OAUTH_REFRESH_TOKEN_VALIDITY = 7776000      -- 90 days, so you don't re-auth daily
  PRE_AUTHORIZED_ROLES_LIST = ('WETHR_READER')
  ENABLED = TRUE;
-- ACCOUNTADMIN / SECURITYADMIN / ORGADMIN are blocked from OAuth by default; keep it that way.

-- Copy OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET into Claude > Settings > Connectors > Snowflake.
SELECT SYSTEM$SHOW_OAUTH_CLIENT_SECRETS('CLAUDE_OAUTH');

-- ChatGPT: create a second integration the same way with ChatGPT's redirect URI
-- (shown in its connector setup screen), e.g. CHATGPT_OAUTH.
