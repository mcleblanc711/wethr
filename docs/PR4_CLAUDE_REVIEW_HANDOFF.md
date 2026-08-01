# PR #4 Independent Re-Review Handoff

## Review objective

Independently re-review PR #4, `Address P0-03 quality gate review findings`,
after the follow-up remediation prompted by the prior report. Treat this file as
a navigation aid, not evidence that the findings are resolved. Re-derive the
result from the current PR diff and production commands.

Repository: `mcleblanc711/wethr`

Base: `main` (derive the current merge base with Git)

Head: `agent/p0-03-gate-review-followups`

Expected scope: quality-gate code, its offline regression tests and locked test
dependency, workflow-hardening documentation, and this handoff. No file under
`collector/src/` should change.

## Prior findings to re-check

### MAJOR-1 — CI workflow contract regression protection

The old test used independent substring assertions and passed all seven known-bad
workflow mutations. The replacement in
`collector/tests/test_quality_gate.py` loads the workflow with PyYAML's
`BaseLoader` and compares the complete trigger, permission, concurrency, job,
runner, step, action-pin, input, environment, and command structure.

The regression table must reject each mutation:

1. checkout `ref: ${{ github.event.pull_request.head.ref }}`;
2. `WETHR_DIFF_BASE: ${{ github.sha }}`;
3. an additional multiline shell step;
4. `permissions: contents: write`;
5. unpinned `actions/checkout@v4`;
6. uv version drift to `0.99.0`; and
7. runner drift to `ubuntu-latest`.

Check that the real workflow passes the same structural assertion and that each
mutated source fails it for the intended reason.

### MINOR-1 — untracked whitespace from a foreign working directory

`scripts/check` now invokes:

```text
git -C "$repo_root" diff --no-index --check -- /dev/null "$repo_root/$file"
```

The regression fixture runs the production gate from a second Git repository
whose `core.whitespace` disables trailing-space checks. An untracked file in the
Wethr fixture must still fail under Wethr's repository configuration.

### MINOR-2 — inherited Python and uv selectors

After consuming `WETHR_DIFF_BASE`, the gate removes shell-addressable inherited
`WETHR_*` variables plus Python/uv selectors that can alter imports, interpreter
behavior, synchronization, or environment placement. In particular, verify
that hostile `PYTHONPATH` does not reach gate children and an inherited
`UV_PROJECT_ENVIRONMENT` cannot relocate the environment.

The gate deliberately exports:

- `WETHR_LIVE=0`;
- temporary `WETHR_DATA_DIR` and `WETHR_DB_PATH`;
- `PYTHONHASHSEED=0`; and
- `UV_PROJECT_ENVIRONMENT=<repo>/collector/.venv`.

Confirm sanitization still occurs after diff-base resolution so an intentional
`WETHR_DIFF_BASE` remains usable.

### MINOR-3 — systemd contract

This remediation does not claim an immutable systemd package on GitHub-hosted
runners. The documented contract is now explicit:

- uv, CPython, and Compose are exact pins;
- systemd is a runner/host-supplied major-255 compatibility boundary;
- any `systemd-analyze verify` diagnostic fails closed; and
- `uv sync` must precede unit verification because rewritten `ExecStart` paths
  resolve `collector/.venv/bin/python`.

Assess whether this explicit fail-closed boundary is acceptable for PR #4 or
whether an exact systemd package/container pin remains required. Do not treat
the documentation as eliminating point-release variance.

### MINOR-4 — uncovered failure paths

New tests should exercise:

- unresolved environment-supplied `WETHR_DIFF_BASE`;
- zero-SHA environment fallback to local `main`;
- a missing required command;
- unparseable uv, Compose, and systemd version output through the integration
  fixture;
- untracked-file inspection failure; and
- the non-worktree guard.

Check that each assertion executes the production `scripts/check` function or
script rather than duplicating the behavior under test.

## Additional changes to inspect

- Compose output is captured and any exit-zero diagnostic now fails closed.
- The systemd version parser accepts both `systemd 255 (...)` and
  `systemd 255.4` while still enforcing major 255.
- The fixture uses `sys.executable` instead of `/usr/bin/python3`.
- The environment-sanitization loop no longer relies on an `&&` final command
  under `set -e`.
- Documentation says "shell-addressable" rather than claiming Bash can unset
  invalid-identifier environment names.
- Recorded test results omit nondeterministic elapsed times.
- The exact gate CPython pin is explicitly not presented as changing the
  pre-existing `/usr/bin/python3` runtime in deployed collector/export units.

## Verification already observed

Focused offline regression suite:

```text
cd collector
uv run --frozen --python 3.12.13 \
  python -m pytest -q tests/test_quality_gate.py -p no:cacheprovider
78 passed
```

Full production gate:

```text
./scripts/check --base origin/main
184 passed
3 tracked workflow JSON files parsed
1 tracked Compose file validated
7 tracked systemd units validated
committed, staged, unstaged, and untracked whitespace checks passed
check: all checks passed
```

Re-run commands as needed; do not rely only on these recorded results.

## Suggested review output

Provide:

1. blocker/major/minor/nit counts;
2. file-and-line evidence for every finding;
3. a finding-by-finding disposition against the prior report;
4. exact commands run and observed results;
5. any remaining fail-open or reproducibility risks; and
6. a clear merge recommendation.

Keep unrelated follow-ups (`web3` in live redemption and bare-Python examples in
`collector/README.md`) out of the merge decision unless this PR newly changes
their behavior or claims.
