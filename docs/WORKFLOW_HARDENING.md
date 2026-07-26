# Wethr Workflow Hardening

Status: Phase 0 audit and implementation blueprint. The checks described as
proposed below are **not implemented yet**.

Last verified: 2026-07-26
Repository baseline: `72dc12e`

## Governing principles

- A gate that skips unrecognized input is not evidence.
- A script must not claim a check passed unless it ran that check successfully.
- Classifiers belong in pure functions with table-driven tests.
- Documentation and registries coordinate work; checks and protected settings
  enforce outcomes.
- Local agent permissions reduce prompts and blast radius but are not a sandbox.
- Every new check must be demonstrated against deliberately failing input.

## Phase 0 findings

### Language and package/test tooling

- Application language: Python.
- Verified local interpreter: Python 3.12.3.
- Package manager: `pip` with `collector/requirements.txt`.
- Dependency lock: none.
- Installed test runner: pytest 9.0.3.
- pytest is not declared in the tracked requirements.
- `uv 0.11.28` is installed on the host but is not configured for this project.
- No tracked Ruff, Black, isort, mypy, pyright, tox, pre-commit, or formatter
  configuration exists.

### There is no canonical gate

The README documents a partial standalone runner:

```text
$ .venv/bin/python tests/test_core.py
==================================================
  63 passed, 0 failed, 63 total
==================================================
```

The README calls this “56 tests,” and it does not include
`test_calibration_ledger.py`.

The installed pytest executable does not work as an interchangeable entrypoint:

```text
$ .venv/bin/pytest -q
ERROR collecting tests/test_calibration_ledger.py
ModuleNotFoundError: No module named 'src'
```

The module form used by the merged calibration PR succeeds:

```text
$ .venv/bin/python -m pytest -q
106 passed in 71.87s (0:01:11)
```

Additional ad hoc checks succeeded:

```text
$ .venv/bin/python -m compileall -q src scripts
(exit 0, no output)

$ <parse n8n-wethr/workflows/*.json>
3 workflow JSON files parsed

$ systemd-analyze verify <all seven tracked units>
(exit 0, no output)

$ docker compose -f n8n-wethr/docker-compose.yml config -q
(exit 0, no output)

$ git diff --check
(exit 0, no output)
```

These commands are not assembled into one tracked command or CI job.

`collector/setup.sh` is not a safe substitute:

- it installs unconstrained dependency versions;
- it initializes the canonical default database before testing;
- it runs only `tests/test_core.py`;
- it then claims setup is complete.

### CI and protected-branch state

GitHub repository: `mcleblanc711/wethr`
Default branch: `main`

Verified GitHub state:

- zero Actions workflows;
- no classic branch protection;
- no repository rulesets;
- no required status checks;
- `main` reports `protected: false`;
- required contexts and checks are empty;
- `allow_update_branch` is false.

No GitHub setting was changed during this audit.

### Observed naming

Only two task branch names were recoverable:

- `agent/calibration-grade-data-roadmap`
- `agent/p0-01-recoverable-backups`

This suggests `agent/<packet-slug>` but is too small a sample to call an enforced
convention.

The eight available commit subjects use mostly imperative English without
Conventional Commit prefixes.

### Globally shared resources

These resources can be affected invisibly by a branch or agent command:

- `data/wethr.db`
- `n8n-wethr/wethr-output/wethr_audit.db`
- inline SQLite DDL and the shared `SCHEMA_VERSION` constant
- Docker container name `n8n`
- host port `5678`
- Docker Compose volume `n8n_data`
- fixed systemd unit names and paths
- n8n workflow IDs imported into one runtime
- safety-critical environment flags such as `WETHR_LIVE`

There is no versioned migration-number registry or dependency lockfile.

Tests use temporary fixture databases and did not reveal a globally claimed
fixture-ID registry.

### Generated artifacts

No tracked file was found to be generated from another tracked file.

The tracked n8n workflow JSON files are exports from external n8n runtime state.
They are the repository's version-controlled workflow representation, so they
should remain tracked. JSON validity belongs in the gate; runtime/export parity is
a later n8n lifecycle concern.

### Factual claims that have drifted

- `collector/README.md` says 56 tests; the standalone file has 63 and the full
  suite has 106.
- `collector/README.md` describes a 300-second scan default; code defaults to 600.
- `collector/src/config.py` labels 600 seconds as “5 min.”
- README material describes 109 ensemble members while source/tests also assert
  143; the currently configured non-GFS models total 109.
- `setup.sh` implies complete test/module verification but runs only the legacy
  standalone suite and imports a subset of modules.
- Operations documentation describes daily/monthly behavior whose direct-script
  systemd entrypoints were observed failing with `ModuleNotFoundError: src`.
- `systemd-analyze verify` checks unit syntax; it does not prove the invoked Python
  job can start safely.

## Applicability of the hardening brief

### Item 1 — Fail-closed status/checklist gate

Decision: skip for now.

There is no issue-state flip, branch checklist, changelog rule, version bump, or
migration note currently required per branch. Adding a classifier now would invent
a process rather than enforce an existing one.

If such a status mechanism is adopted later, its classifier must have three
explicit outcomes: recognized-pass, recognized-skip, and unrecognized-fail.

### Item 2 — Require up-to-date branches

Decision: apply when the first default-branch-comparing check is introduced.

The migration immutability gate planned for P0-04 will compare against `main`.
At that point `main` must require strict/up-to-date status checks or use a merge
queue. Otherwise two stale-green migration branches can still collide.

Use PATCH requests against individual protection sub-resources, never a blanket
replacement of the protection object, and re-read all settings after mutation.

### Item 3 — Gate globally claimed resources

Decision: apply with P0-04 versioned migrations.

The defect is present: schema evolution currently lives as inline, idempotent DDL
with one shared schema version. A meaningful uniqueness/contiguity/immutability
gate requires ordered migration files first.

The future check must enforce:

- unique migration IDs;
- contiguous numbering if “next free” remains the policy;
- immutability of migrations already present on `origin/main`;
- failure on unrecognized migration filenames;
- an actionable message identifying the file and correction.

### Item 4 — Tooling verifies its claims

Decision: apply in the first workflow implementation packet.

Create one canonical gate and make `setup.sh` invoke it or remove the setup
script's verification claim. Update stale test/count/timing statements.

There is no PR-body or report generator today, so the zero-byte-on-failure and
`--no-verify` behavior has no current generator to attach to. Apply that contract
when a generator is actually introduced.

### Item 5 — Narrow agent permissions

Decision: apply as a separate, scoped workflow concern.

The ignored Claude settings currently include broad patterns such as arbitrary
Python and virtual-environment Python execution. A Python interpreter can spawn
subprocesses, write files, make network calls, and invoke Git.

Required outcome:

- remove interpreter-level wildcard allows from local settings;
- pre-approve only the canonical gate and other reviewed exact invocations;
- prefer domain-restricted web tools over broad `curl`;
- label the allowlist as prompt reduction and blast-radius reduction, not a
  sandbox;
- identify every allowed command that can still make an outward action;
- use Claude Code sandboxing or OS controls when an actual boundary is needed.

Project `.claude/settings.json` arrays merge with local settings, so committing a
narrow shared file does not neutralize an existing broad
`.claude/settings.local.json`. The local file must also be cleaned deliberately.

Reference:
[Claude Code permissions](https://code.claude.com/docs/en/permissions).

### Item 6 — Fuzz pure functions before review

Decision: apply before or with high-risk schema/statistical packets.

This maps to demonstrated defects:

- distinct forecast sightings collapsed under one identity;
- timestamps were compared lexicographically instead of as datetimes;
- persisted representations lost distinctions that in-memory assertions did not
  expose.

Initial property targets:

- `iso_utc`/`parse_utc` persisted-form round trips and ordering;
- canonical JSON/hash stability across dictionary insertion order and JSON
  reload;
- forecast sighting identity across run time, capture cutoff, lead basis, and
  lead bucket;
- precision rounding and interval-boundary invariants;
- migration replay from persisted prior schemas.

Use finite, bounded strategies and engine-generated permutations. If CI uses a
derandomized profile, no property may draw ordering from external randomness.

### Item 7 — Untrack generated artifacts

Decision: skip.

No tracked-from-tracked build output exists. The n8n exports should remain tracked
and validated.

### Item 8 — Parallel-track lifecycle

Decision: skip while the one-issue/one-PR sequential policy remains in force.

Parallel operational work would collide through the fixed container, port,
volume, services, and databases. If concurrency is adopted later, worktree
lifecycle tooling must refuse unsafe retirement and read claims from the fetched
remote default branch.

## Proposed enforcement scaffold

This section describes a target structure, not files that already exist.

### Coordination surfaces

- `docs/REMEDIATION_BLUEPRINT.md`
- `docs/WORKFLOW_HARDENING.md`
- a packet/issue template containing defect, evidence, scope, non-goals, safety,
  failure cases, gate, and model routing
- short `AGENTS.md`/`CLAUDE.md` instructions that point to the canonical command

Every coordination file should label itself as non-enforcement.

### Enforcement surfaces

- `collector/pyproject.toml`
- `collector/uv.lock`
- one repository-level gate command, tentatively `scripts/check`
- `.github/workflows/quality.yml`
- later, a pure migration classifier/check with table-driven tests
- later, strict protected-branch settings

### Smallest useful quality job

The first quality job should:

1. install the locked Python 3.12 environment;
2. run the full suite through `python -m pytest`, never the ambiguous executable
   form;
3. compile `collector/src` and `collector/scripts`;
4. parse every tracked n8n workflow JSON file;
5. validate Docker Compose configuration;
6. validate all tracked systemd units;
7. run an effective whitespace/diff check over the PR range;
8. avoid the production databases and all network APIs.

Lint, format, and type enforcement are deliberately excluded from this MVP. No
baseline exists, and adopting all three would force unrelated source churn.
Introduce each only in a later packet with a reviewed baseline and deliberate
failure demonstration.

### Protected-branch activation

Do not configure required checks before the workflow exists on `main`.

After the reviewed quality workflow merges:

- require pull requests for `main`;
- require the exact quality-job context;
- set required status checks to strict/up-to-date;
- enable the update-branch path if useful;
- preserve or deliberately set admin enforcement;
- prohibit force pushes and deletion;
- re-fetch the protection and context list to prove the intended settings
  survived.

Operational GitHub-setting changes remain separate from the source PR and require
explicit approval.

## Model routing for workflow packets

| Packet | Builder | Reviewer |
|---|---|---|
| Canonical gate, dependency lock, and CI | Claude Sonnet 5, `high` | GPT-5.6 Sol, `high` |
| Agent permission narrowing | Claude Sonnet 5, `high` | GPT-5.6 Sol, `high` |
| Migration integrity and immutability | Claude Fable 5, `xhigh` | GPT-5.6 Sol, `max` |
| Persisted-form property invariants | Claude Fable 5, `xhigh` | GPT-5.6 Sol, `max` |

## Definition of done for a workflow packet

- One concern per commit.
- No production/source bug is silently fixed inside a workflow-only packet.
- Every new check has a deliberately failing demonstration.
- Classifiers are pure and table-tested.
- Unknown input fails with an actionable message.
- Local and CI entrypoints invoke the same canonical gate.
- The full gate is green, with real output recorded.
- GitHub settings are read back after any approved change.
- Documentation states what shipped, what was deliberately skipped, and why.
- The independent reviewer reports no unresolved Blocker or Major findings.
