# Wethr Workflow Hardening

Status: Phase 0 audit retained; the P0-03 reproducible quality scaffold and its
independent-review follow-up are implemented.

Last verified: 2026-07-30
P0-03 merge baseline: `539bd5b`

## Governing principles

- A gate that skips unrecognized input is not evidence.
- A script must not claim a check passed unless it ran that check successfully.
- Classifiers belong in pure functions with table-driven tests.
- Documentation and registries coordinate work; checks and protected settings
  enforce outcomes.
- Local agent permissions reduce prompts and blast radius but are not a sandbox.
- Every new check must be demonstrated against deliberately failing input.

## Phase 0 findings and P0-03 disposition

### Language and package/test tooling

- Application language: Python 3.12 (`>=3.12,<3.13`).
- `collector/pyproject.toml` and `collector/uv.lock` are the only dependency
  source; `collector/requirements.txt` was removed.
- The gate requires uv `0.11.32` and CPython `3.12.13` exactly;
  `collector/.python-version` selects that interpreter while the application
  compatibility range remains `>=3.12,<3.13`.
- The uv project is non-packaged and includes pytest plus pytest-socket in its
  development dependency group.
- No Ruff, Black, isort, mypy, pyright, tox, pre-commit, or formatter gate was
  added in this packet.

### Canonical gate (implemented by P0-03)

The public local and CI entrypoint is:

```text
./scripts/check [--base <git-ref>]
```

The command requires uv `0.11.32`, CPython `3.12.13`, Docker Compose `2.40.3`,
Git, and systemd major `255`. uv, Compose, and systemd are asserted before
expensive checks, and the synchronized Python runtime is asserted before tests.
Compose packaging suffixes are allowed only after the exact `2.40.3` core
version. The gate synchronizes the locked runtime and development environment;
runs the full pytest suite through `python -m pytest` with network sockets
disabled (Unix-domain sockets are allowed for the asyncio event loop); compiles
`collector/src` and `collector/scripts`; and validates every tracked workflow
JSON, Compose file, and systemd service/timer discovered through Git. All
`uv run` calls use `--frozen`.

Tracked paths are classified by pure, table-tested functions over a NUL-delimited
`git ls-files` stream. Compose files are recognized by one of the four standard
basenames at any depth; n8n JSON and systemd units must remain beneath their
documented roots. The quality workflow itself must remain tracked. An expected
empty file class is an error. Temporary systemd copies preserve their repository
relative paths before the documented `%h/projects/wethr` checkout prefix is
rewritten, so duplicate basenames cannot collide.

After the diff base is resolved, every inherited `WETHR_*` environment variable
is removed. The Python checks receive only `WETHR_LIVE=0`, `WETHR_DATA_DIR`, and
`WETHR_DB_PATH`, with both paths pointing to a temporary directory. Dependency
and interpreter downloads are allowed during `uv sync --locked`; Wethr runtime
APIs and production databases are not.

The diff base precedence is explicit `--base`, `WETHR_DIFF_BASE`, `origin/main`,
then local `main`. An explicit or environment-supplied nonzero ref must resolve.
The command resolves the merge base and checks committed branch changes, then
checks staged, unstaged, and ignored-excluded untracked files separately for
whitespace errors.

`collector/setup.sh` is a compatibility wrapper that requires uv and delegates
to this command. It no longer installs through pip, initializes a database,
performs partial imports, or invokes the legacy standalone test runner.

### CI and protected-branch state

GitHub repository: `mcleblanc711/wethr`
Default branch: `main`

P0-03 adds `.github/workflows/quality.yml` with one stable `quality` job for pull
requests, pushes to `main`, and manual dispatch. Pull requests use GitHub's merge
ref so the candidate is checked with the current base; pushes use the pushed
commit. Checkout retains full history without persisted Git credentials. CI
installs the pinned uv, Python, and Compose versions, supplies
`WETHR_DIFF_BASE` only from the pull-request base SHA, and invokes only
`./scripts/check` as its repository command.

At the last GitHub-settings audit:

- no classic branch protection existed;
- no repository rulesets existed;
- no status check was required;
- `main` reported `protected: false`;
- required contexts and checks were empty;
- `allow_update_branch` was false.

No GitHub setting is changed by P0-03. A workflow file is not branch protection.

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

There is still no versioned migration-number registry. P0-03 adds the dependency
lockfile; ordered migrations remain P0-04.

Tests use temporary fixture databases and did not reveal a globally claimed
fixture-ID registry.

### Generated artifacts

No tracked file was found to be generated from another tracked file.

The tracked n8n workflow JSON files are exports from external n8n runtime state.
They are the repository's version-controlled workflow representation, so they
should remain tracked. JSON validity belongs in the gate; runtime/export parity is
a later n8n lifecycle concern.

### Factual claims corrected in P0-03

- Test documentation now names the full canonical gate without embedding a test
  count that will drift as the suite changes.
- The documented scan default and source comment now agree on 600 seconds
  (10 minutes); runtime behavior is unchanged.
- Ensemble documentation now derives probability resolution from the members
  actually returned instead of promising one fixed pooled count.
- `collector/setup.sh` now states and performs only its compatibility role; it
  does not initialize an operational database or claim partial checks are complete.

Two audit caveats remain outside P0-03: daily/monthly direct-script systemd
entrypoints were observed failing with `ModuleNotFoundError: src`, and
`systemd-analyze verify` checks unit syntax rather than proving that an invoked
service can start safely.

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

Decision: implemented by P0-03.

The canonical gate, safe `setup.sh` delegation, locked dependencies, and corrected
test/count/timing statements are now present.

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

## Enforcement scaffold

P0-03 implements the reproducible quality scaffold. Coordination documents still
describe intent; the files below perform the checks.

### Coordination surfaces

- `docs/REMEDIATION_BLUEPRINT.md`
- `docs/WORKFLOW_HARDENING.md`

A packet/issue template and short `AGENTS.md`/`CLAUDE.md` pointers remain later
coordination work. They are not enforcement surfaces.

### P0-03 enforcement surfaces

- `collector/.python-version`, `collector/pyproject.toml`, and
  `collector/uv.lock`
- executable repository command `./scripts/check`
- compatibility wrapper `collector/setup.sh`
- `.github/workflows/quality.yml` with the stable job name `quality`

A migration classifier and strict protected-branch settings remain later work.

### Quality job

The local command and CI job:

1. assert uv `0.11.32`, CPython `3.12.13`, Compose `2.40.3`, and systemd `255`;
2. install the locked Python runtime and development environment;
3. run the full suite through `python -m pytest` with network sockets disabled;
4. compile `collector/src` and `collector/scripts`;
5. parse every tracked n8n workflow JSON file;
6. validate every tracked Compose file;
7. validate every tracked systemd service and timer;
8. check committed branch, staged, unstaged, and untracked whitespace; and
9. isolate Python data paths from the production databases without starting any
   application, service, timer, or container.

### Deliberate failure demonstrations

The offline fixture suite runs the production `scripts/check` against temporary
Git repositories and deterministic tool shims. The verified command was:

```text
cd collector
uv run --frozen --python 3.12.13 python -m pytest -q tests/test_quality_gate.py -p no:cacheprovider
60 passed in 3.40s
```

Each row below is a real parameterized fixture case. It asserts a nonzero exit
and the recorded terminal error marker.

| Gate class | Deliberately failing input | Terminal error marker |
|---|---|---|
| Tool versions | uv `0.11.320`; Compose `2.40.30`; systemd `258` | required version/major not found |
| Locked sync | shimmed `uv sync` failure | `locked Python environment synchronization failed` |
| Python runtime | shimmed CPython `3.12.12` | `CPython 3.12.13 is required` |
| Pytest | shimmed test failure | `pytest failed` |
| Compilation | shimmed compile failure | `collector byte-compilation failed` |
| Workflow JSON | malformed tracked JSON | `malformed workflow JSON` |
| Compose | rejected tracked configuration | `invalid Compose configuration` |
| systemd | nonzero verify and exit-zero diagnostic | `systemd unit validation failed/reported diagnostics` |
| Required file classes | remove JSON, Compose, all units, or quality workflow | class-specific `not found`/`not tracked` error |
| Diff selection | bad argument or unresolved explicit base | actionable argument/base error |
| Whitespace | committed, staged, unstaged, or untracked trailing space | scope-specific whitespace error |

The complete gate was then run with hostile inherited sizing, Telegram, diff-base,
and unknown future `WETHR_*` values. The fixture separately proved that child
Python processes received only the three gate-owned variables. Real output:

```text
166 passed in 65.04s (0:01:05)
check: parsing 3 tracked n8n workflow JSON file(s)
check: validating 1 tracked Compose file(s)
check: validating 7 tracked systemd unit(s)
check: checking committed whitespace from merge base 539bd5b25ead (origin/main)
check: checking staged whitespace
check: checking unstaged whitespace
check: checking untracked whitespace
check: all checks passed
```

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
