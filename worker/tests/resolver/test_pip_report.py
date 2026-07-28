from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from wheelforge_worker.parser import parse_requirements
from wheelforge_worker.process import (
    ProcessExecutionError,
    ProcessResult,
    ProcessRunner,
    ProcessTimeoutError,
    ProcessValidationError,
)
from wheelforge_worker.resolver import (
    InvalidPipReportError,
    PipReportSchemaError,
    PipReportVersionError,
    ResolverCommandError,
    ResolverMissingReportError,
    ResolverProcessError,
    StrictResolver,
    build_resolve_argv,
    parse_pip_report,
)
from wheelforge_worker.target import TargetProfile


@pytest.fixture
def profile_cp311_arm64() -> TargetProfile:
    return TargetProfile.model_validate(
        {
            "profileId": "d9428888-122b-11e1-b85c-61cd3cbb3210",
            "profileCode": "linux-arm64-cp311",
            "os": "LINUX",
            "architecture": "AARCH64",
            "pythonImplementation": "CPYTHON",
            "pythonVersion": "3.11",
            "pythonFullVersion": "3.11.9",
            "platformTag": "manylinux2014_aarch64",
            "abiTags": ["cp311", "abi3", "none"],
            "validationType": "STATIC",
            "validationPolicyVersion": "wheel-tags-v1",
            "profileVersion": 1,
        }
    )


def _report(*install: dict[str, object], version: object = "1") -> dict[str, object]:
    return {"version": version, "install": list(install)}


def _install(
    name: str = "Demo_Pkg",
    version: str = "1.2.3",
    url: str = "https://example.test/packages/demo_pkg-1.2.3-py3-none-any.whl",
    *,
    requested: bool = True,
    hashes: dict[str, str] | None = None,
) -> dict[str, object]:
    archive_info: dict[str, object] = {}
    if hashes is not None:
        archive_info["hashes"] = hashes
    return {
        "download_info": {"url": url, "archive_info": archive_info},
        "metadata": {
            "name": name,
            "version": version,
            "requires_dist": ["dep>=1"],
            "requires_python": ">=3.9",
        },
        "requested": requested,
    }


def test_pip_command_is_targeted_and_binary_only(
    profile_cp311_arm64: TargetProfile,
) -> None:
    argv = build_resolve_argv(
        Path("requirements.txt"),
        Path("report.json"),
        profile_cp311_arm64,
        "https://pypi.org/simple",
    )

    assert argv == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--report",
        "report.json",
        "--only-binary=:all:",
        "--platform",
        "manylinux2014_aarch64",
        "--python-version",
        "3.11",
        "--implementation",
        "cp",
        "--abi",
        "cp311",
        "--abi",
        "abi3",
        "--abi",
        "none",
        "--index-url",
        "https://pypi.org/simple",
        "-r",
        "requirements.txt",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "TSINGHUA",
        "ALIYUN",
        "PYPI",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://pypi.org/simple",
    ],
)
def test_pip_command_accepts_only_builtin_sources(
    profile_cp311_arm64: TargetProfile, source: str
) -> None:
    argv = build_resolve_argv(Path("requirements.txt"), Path("report.json"), profile_cp311_arm64, source)

    assert argv[argv.index("--index-url") + 1] in {
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://pypi.org/simple",
    }


@pytest.mark.parametrize(
    "source",
    [
        "unknown",
        "https://pypi.org/simple?inject=1",
        "https://user:password@pypi.org/simple",
        "https://pypi.org/simple#fragment",
        "https://pypi.org/simple\n--extra-index-url=https://bad.test",
        " https://pypi.org/simple",
    ],
)
def test_pip_command_rejects_source_injection_attempts(
    profile_cp311_arm64: TargetProfile, source: str
) -> None:
    with pytest.raises(ValueError):
        build_resolve_argv(Path("requirements.txt"), Path("report.json"), profile_cp311_arm64, source)


def test_runner_uses_tokenized_argv_and_complete_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        args: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        observed["args"] = args
        return subprocess.CompletedProcess(args, 0, "out", "err")

    monkeypatch.setattr("wheelforge_worker.process.subprocess.run", fake_run)

    result = ProcessRunner().run(
        [sys.executable, "-c", "print('ok')"],
        tmp_path,
        timedelta(seconds=1),
        {"PIP_NO_INPUT": "1"},
    )

    assert result.return_code == 0
    assert result.stdout == "out"
    assert observed["shell"] is False
    assert observed["env"] == {"PIP_NO_INPUT": "1"}
    assert observed["text"] is True
    assert observed["capture_output"] is True


@pytest.mark.parametrize(
    ("argv", "timeout", "env"),
    [
        ([], timedelta(seconds=1), {"PATH": "x"}),
        (["ok", 1], timedelta(seconds=1), {"PATH": "x"}),  # type: ignore[list-item]
        (["bad\x00"], timedelta(seconds=1), {"PATH": "x"}),
        (["ok"], timedelta(0), {"PATH": "x"}),
        (["ok"], timedelta(seconds=1), {"bad-key": "x"}),
        (["ok"], timedelta(seconds=1), {"PATH": 1}),  # type: ignore[dict-item]
    ],
)
def test_runner_rejects_invalid_launch_inputs(
    argv: list[str], timeout: timedelta, env: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(ProcessValidationError):
        ProcessRunner().run(argv, tmp_path, timeout, env)


def test_runner_returns_nonzero_exit_without_raising(tmp_path: Path) -> None:
    result = ProcessRunner().run(
        [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"],
        tmp_path,
        timedelta(seconds=5),
        {},
    )

    assert result.return_code == 7
    assert result.stderr == "bad\n"


def test_runner_maps_timeout_to_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ProcessTimeoutError) as raised:
        ProcessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            tmp_path,
            timedelta(milliseconds=10),
            {},
        )

    assert len(raised.value.stdout) <= 4096
    assert len(raised.value.stderr) <= 4096


def test_runner_maps_spawn_failure_to_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ProcessExecutionError):
        ProcessRunner().run(["definitely-not-a-command"], tmp_path, timedelta(seconds=1), {})


def test_parse_report_maps_direct_and_transitive_wheels() -> None:
    result = parse_pip_report(
        _report(
            _install(hashes={"sha256": "abc", "sha512": "def"}),
            _install(
                "Dependency",
                "2.0",
                "file:///fixtures/dependency-2.0-py3-none-any.whl",
                requested=False,
            ),
        )
    )

    assert result.packages[0].name == "demo-pkg"
    assert str(result.packages[0].version) == "1.2.3"
    assert result.packages[0].requested is True
    assert result.packages[0].wheel_filename == "demo_pkg-1.2.3-py3-none-any.whl"
    assert result.packages[0].requires_dist == ("dep>=1",)
    assert result.packages[0].requires_python == ">=3.9"
    assert {(item.algorithm, item.value) for item in result.packages[0].archive_hashes} == {
        ("sha256", "abc"),
        ("sha512", "def"),
    }
    assert result.packages[1].name == "dependency"
    assert result.packages[1].requested is False


@pytest.mark.parametrize("version", ["2", 2, 1.0, True, None])
def test_parse_report_rejects_unknown_version(version: object) -> None:
    with pytest.raises(PipReportVersionError):
        parse_pip_report(_report(version=version))


def test_parse_report_normalizes_integer_version_one() -> None:
    assert parse_pip_report(_report(version=1)).report_version == "1"


@pytest.mark.parametrize("payload", ["not json", b"[]", [], {"version": "1"}])
def test_parse_report_rejects_malformed_json_or_schema(
    payload: str | bytes | list[object] | dict[str, object]
) -> None:
    with pytest.raises((InvalidPipReportError, PipReportSchemaError)):
        parse_pip_report(payload)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/demo-1.0.tar.gz",
        "https://example.test/demo-1.0.whl#fragment",
        "https://user:password@example.test/demo-1.0.whl",
        "http://example.test/demo-1.0.whl",
        "not a url/demo-1.0.whl",
    ],
)
def test_parse_report_rejects_unsafe_or_nonwheel_artifacts(url: str) -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(url=url)))


def test_parse_report_rejects_invalid_wheel_filename() -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(url="https://example.test/not-a-wheel.whl")))


def test_parse_report_rejects_invalid_distribution_name() -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(name="not a package name")))


def test_parse_report_rejects_conflicting_duplicate_package() -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(
            _report(
                _install("demo", "1.0", "https://example.test/demo-1.0-py3-none-any.whl"),
                _install("demo", "2.0", "https://example.test/demo-2.0-py3-none-any.whl"),
            )
        )


class _WritingRunner:
    def __init__(self, payload: dict[str, object], return_code: int = 0) -> None:
        self.payload = payload
        self.return_code = return_code
        self.calls: list[tuple[list[str], Path, timedelta, dict[str, str]]] = []

    def run(
        self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
    ) -> ProcessResult:
        self.calls.append((argv, cwd, timeout, env))
        (cwd / "report.json").write_text(json.dumps(self.payload), encoding="utf-8")
        return ProcessResult(tuple(argv), self.return_code, "", "no credentials", timedelta(0))


def test_strict_resolver_uses_injected_runner_without_network(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = _WritingRunner(_report(_install()))
    resolver = StrictResolver(tmp_path, runner=runner, timeout=timedelta(seconds=3))

    result = resolver.resolve(parse_requirements(b"demo-pkg==1.2.3\n"), profile_cp311_arm64, "PYPI")

    assert result.packages[0].name == "demo-pkg"
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8") == "demo-pkg==1.2.3\n"
    assert runner.calls[0][0] == build_resolve_argv(
        tmp_path / "requirements.txt", tmp_path / "report.json", profile_cp311_arm64, "PYPI"
    )
    assert runner.calls[0][3] == {"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}


def test_strict_resolver_rejects_nonzero_pip_exit(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    resolver = StrictResolver(tmp_path, runner=_WritingRunner(_report(), return_code=1))

    with pytest.raises(ResolverCommandError):
        resolver.resolve(parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI")


def test_strict_resolver_rejects_missing_report(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    class _NoReportRunner:
        def run(
            self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
        ) -> ProcessResult:
            return ProcessResult(tuple(argv), 0, "", "", timedelta(0))

    resolver = StrictResolver(tmp_path, runner=_NoReportRunner())

    with pytest.raises(ResolverMissingReportError):
        resolver.resolve(parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI")


def test_strict_resolver_maps_runner_failures_to_typed_error(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    class _TimeoutRunner:
        def run(
            self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
        ) -> ProcessResult:
            raise ProcessTimeoutError("partial output", "partial error")

    resolver = StrictResolver(tmp_path, runner=_TimeoutRunner())

    with pytest.raises(ResolverProcessError):
        resolver.resolve(parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI")
