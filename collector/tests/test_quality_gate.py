"""Offline regression tests for the canonical repository quality gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
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
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}:{env['PATH']}",
                "GATE_ENV_CAPTURE": str(self.capture_dir / "environment"),
                "GATE_UV_CAPTURE": str(self.capture_dir / "uv-commands"),
                "GATE_UNIT_CAPTURE": str(self.capture_dir / "unit-paths"),
            }
        )
        if environment:
            env.update(environment)
        return subprocess.run(
            [str(self.root / "scripts" / "check"), *arguments],
            cwd=self.root,
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
            printf 'uv %s (fixture)\n' "${GATE_UV_VERSION:-0.11.32}"
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
            /usr/bin/python3 -m json.tool "${@: -1}"
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
            printf 'Docker Compose version %s\n' "${GATE_COMPOSE_VERSION:-2.40.3}"
            exit 0
        fi
        [[ "${GATE_FIXTURE_FAIL:-}" != "compose" ]] || exit 9
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
            printf 'systemd %s (fixture)\n' "${GATE_SYSTEMD_MAJOR:-255}"
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
        }
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "check: all checks passed" in result.stdout

    environment_lines = (gate_fixture.capture_dir / "environment").read_text(
        encoding="utf-8"
    ).splitlines()
    wethr_names = sorted(
        line.partition("=")[0] for line in environment_lines if line.startswith("WETHR_")
    )
    assert wethr_names == ["WETHR_DATA_DIR", "WETHR_DB_PATH", "WETHR_LIVE"]

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


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("sync", "locked Python environment synchronization failed"),
        ("python-runtime", "CPython 3.12.13 is required"),
        ("pytest", "pytest failed"),
        ("compile", "collector byte-compilation failed"),
        ("json", "malformed workflow JSON"),
        ("compose", "invalid Compose configuration"),
        ("systemd", "systemd unit validation failed"),
        ("systemd-diagnostics", "systemd unit validation reported diagnostics"),
        ("committed-whitespace", "whitespace errors found in committed branch changes"),
        ("staged-whitespace", "whitespace errors found in staged changes"),
        ("unstaged-whitespace", "whitespace errors found in unstaged changes"),
        ("untracked-whitespace", "whitespace errors found in untracked files"),
        ("missing-json", "no tracked n8n workflow JSON files found"),
        ("missing-compose", "no tracked Compose files found"),
        ("missing-units", "no tracked systemd service or timer units found"),
        ("missing-quality", "required quality workflow"),
        ("uv-version", "uv 0.11.32 is required, but found '0.11.320'"),
        ("compose-version", "Docker Compose 2.40.3 is required, but found '2.40.30'"),
        ("systemd-version", "systemd major 255 is required, but found '258'"),
        ("bad-base", "explicit diff base 'missing-ref' does not resolve to a commit"),
        ("bad-argument", "unknown argument '--unknown'"),
    ],
)
def test_gate_deliberate_failures(
    gate_fixture: GateFixture,
    scenario: str,
    expected_error: str,
) -> None:
    environment: dict[str, str] = {}
    arguments = ["--base", gate_fixture.base_sha]

    if scenario in {"sync", "python-runtime", "pytest", "compile", "compose", "systemd", "systemd-diagnostics"}:
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
    elif scenario == "bad-base":
        arguments = ["--base", "missing-ref"]
    elif scenario == "bad-argument":
        arguments = ["--unknown"]
    else:  # pragma: no cover - the parameter table defines every scenario
        raise AssertionError(f"unhandled scenario: {scenario}")

    result = gate_fixture.run(*arguments, environment=environment)
    assert result.returncode != 0, result.stdout
    assert expected_error in result.stderr


def test_quality_workflow_preserves_ci_contract() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "pull_request.head.sha" not in workflow
    assert "github.event.before" not in workflow
    assert 'python-version: "3.12.13"' in workflow
    assert "docker/setup-compose-action@4eb059ff7f16592f9c84d5ca339c53cb7c5064e2" in workflow
    assert "version: v2.40.3" in workflow
    assert workflow.count("run: ./scripts/check") == 1
