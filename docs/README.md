# Wethr Documentation

- [Learning SQL with Wethr](SQL_GUIDE.md) — a practical guide from Excel-style
  analysis through joins, CTEs, window functions, JSON, validation, and
  performance using the live Wethr SQLite schema.
- [Warehouse & AI dashboards](DASHBOARDS_GUIDE.md) — why Snowflake + dbt, the
  alternatives considered, the AI/BI tools to compare, and hands-on exercises.
  Setup steps are in [warehouse/README.md](../warehouse/README.md).
- [Remediation blueprint](REMEDIATION_BLUEPRINT.md) — the prioritized,
  one-packet-at-a-time path toward research-grade paper evidence.
- [Workflow hardening](WORKFLOW_HARDENING.md) — the verified Phase 0 audit,
  implemented P0-03 quality gate, deliberate skips, and later enforcement
  boundaries.

The remediation and workflow-hardening files are coordination documents.
`./scripts/check` and the `quality` workflow are the P0-03 enforcement surfaces.
Later mechanisms are not implemented until their code, tests, CI job, schema
constraint, or protected setting is present and verified.
