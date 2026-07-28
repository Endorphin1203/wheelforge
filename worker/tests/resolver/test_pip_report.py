from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

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
import wheelforge_worker.resolver.pip_report as pip_report_module


_TRUNCATION_MARKER = "\n...[truncated]..."
_DRAIN_THREAD_PREFIX = "process-"


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
    requires_dist: tuple[str, ...] = ("dep>=1",),
    requires_python: str | None = ">=3.9",
) -> dict[str, object]:
    archive_info: dict[str, object] = {}
    if hashes is not None:
        archive_info["hashes"] = hashes
    return {
        "download_info": {"url": url, "archive_info": archive_info},
        "metadata": {
            "name": name,
            "version": version,
            "requires_dist": list(requires_dist),
            "requires_python": requires_python,
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
    real_popen = subprocess.Popen

    def observing_popen(
        args: tuple[str, ...], **kwargs: Any
    ) -> subprocess.Popen[bytes]:
        observed.update(kwargs)
        observed["args"] = args
        return cast(Any, real_popen(args, **kwargs))

    monkeypatch.setattr("wheelforge_worker.process.subprocess.Popen", observing_popen)

    result = ProcessRunner().run(
        [sys.executable, "-c", "print('ok')"],
        tmp_path,
        timedelta(seconds=1),
        {"PIP_NO_INPUT": "1"},
    )

    assert result.return_code == 0
    assert result.stdout == "ok\n"
    assert observed["shell"] is False
    assert observed["env"] == {"PIP_NO_INPUT": "1"}
    assert "text" not in observed
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE
    if os.name == "posix":
        assert observed["start_new_session"] is True
    else:
        assert observed["creationflags"] == getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP"
        )


@pytest.mark.parametrize(
    ("script", "return_code"),
    [("print('pipe output')", 0), ("import sys; sys.exit(7)", 7)],
)
def test_runner_does_not_create_unbounded_temporary_output_files_for_exits(
    script: str,
    return_code: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def reject_temporary_file(*args: object, **kwargs: object) -> Any:
        raise AssertionError("process output must not use temporary files")

    monkeypatch.setattr(tempfile, "TemporaryFile", reject_temporary_file)

    result = ProcessRunner().run(
        [sys.executable, "-c", script],
        tmp_path,
        timedelta(seconds=5),
        {},
    )

    assert result.return_code == return_code


def test_runner_does_not_create_temporary_output_files_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject_temporary_file(*args: object, **kwargs: object) -> Any:
        raise AssertionError("process output must not use temporary files")

    monkeypatch.setattr(tempfile, "TemporaryFile", reject_temporary_file)

    with pytest.raises(ProcessTimeoutError):
        ProcessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            tmp_path,
            timedelta(milliseconds=50),
            {},
        )


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


@pytest.mark.parametrize("return_code", [0, 7])
def test_runner_bounds_flooded_output_for_every_exit(
    tmp_path: Path, return_code: int
) -> None:
    script = (
        "import os,sys;"
        "os.write(1,b'o'*100000);"
        "os.write(2,b'e'*100000);"
        f"sys.exit({return_code})"
    )

    result = ProcessRunner().run(
        [sys.executable, "-c", script], tmp_path, timedelta(seconds=5), {}
    )

    assert result.return_code == return_code
    assert len(result.stdout.encode()) <= 4096
    assert len(result.stderr.encode()) <= 4096
    assert result.stdout.endswith(_TRUNCATION_MARKER)
    assert result.stderr.endswith(_TRUNCATION_MARKER)


def test_runner_keeps_invalid_utf8_replacement_output_within_byte_limit(
    tmp_path: Path,
) -> None:
    result = ProcessRunner().run(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff' * 5000)"],
        tmp_path,
        timedelta(seconds=5),
        {},
    )

    assert len(result.stdout.encode()) <= 4096
    assert result.stdout.endswith(_TRUNCATION_MARKER)


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


def test_runner_bounds_flooded_output_on_timeout(tmp_path: Path) -> None:
    script = (
        "import os,time;"
        "os.write(1,b'o'*100000);"
        "os.write(2,b'e'*100000);"
        "time.sleep(5)"
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timedelta(milliseconds=100),
            {},
        )

    assert len(raised.value.stdout.encode()) <= 4096
    assert len(raised.value.stderr.encode()) <= 4096
    assert raised.value.stdout.endswith(_TRUNCATION_MARKER)
    assert raised.value.stderr.endswith(_TRUNCATION_MARKER)


def _descendant_parent_script(
    pid_path: Path,
    *,
    lifetime_seconds: float,
    redirect_output: bool = False,
    escape_group: bool = False,
) -> str:
    descendant = f"import time; time.sleep({lifetime_seconds})"
    options: list[str] = []
    if redirect_output:
        options.extend(
            ["stdout=subprocess.DEVNULL", "stderr=subprocess.DEVNULL"]
        )
    if escape_group:
        options.append("start_new_session=True")
    option_text = ", " + ", ".join(options) if options else ""
    return (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{descendant!r}]{option_text});"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid));"
        "time.sleep(10)"
    )


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_process_exit(pid: int, timeout_seconds: float = 0.75) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return True
        time.sleep(0.01)
    return not _process_is_running(pid)


def _kill_process_if_running(pid: int) -> None:
    if _process_is_running(pid):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_runner_timeout_kills_inherited_pipe_descendant_near_deadline(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "inherited-descendant.pid"
    script = _descendant_parent_script(pid_path, lifetime_seconds=1.5)
    started = time.monotonic()

    with pytest.raises(ProcessTimeoutError):
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timedelta(milliseconds=100),
            {},
        )

    elapsed = time.monotonic() - started
    descendant_pid = int(pid_path.read_text())
    try:
        assert elapsed < 0.8
        assert _wait_for_process_exit(descendant_pid)
        assert not any(
            thread.name.startswith(_DRAIN_THREAD_PREFIX)
            for thread in threading.enumerate()
        )
    finally:
        _kill_process_if_running(descendant_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_runner_timeout_kills_redirected_pipe_descendant(tmp_path: Path) -> None:
    pid_path = tmp_path / "redirected-descendant.pid"
    script = _descendant_parent_script(
        pid_path, lifetime_seconds=10, redirect_output=True
    )

    with pytest.raises(ProcessTimeoutError):
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timedelta(milliseconds=100),
            {},
        )

    descendant_pid = int(pid_path.read_text())
    try:
        assert _wait_for_process_exit(descendant_pid)
    finally:
        _kill_process_if_running(descendant_pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_runner_uses_typed_bounded_fallback_for_escaped_pipe_holder(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "escaped-descendant.pid"
    script = _descendant_parent_script(
        pid_path, lifetime_seconds=2, escape_group=True
    )
    started = time.monotonic()

    with pytest.raises(ProcessExecutionError):
        ProcessRunner().run(
            [sys.executable, "-c", script],
            tmp_path,
            timedelta(milliseconds=100),
            {},
        )

    elapsed = time.monotonic() - started
    descendant_pid = int(pid_path.read_text())
    try:
        assert elapsed < 1.2
    finally:
        _kill_process_if_running(descendant_pid)


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


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parse_report_rejects_non_json_constants(constant: str) -> None:
    payload = f'{{"version":"1","install":[],"extra":{constant}}}'

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(payload)


def test_parse_report_rejects_duplicate_json_keys() -> None:
    payload = '{"version":"1","version":"1","install":[]}'

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(payload)


def test_parse_report_rejects_invalid_utf8_path(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_bytes(b'{"version":"1","install":[]}\xff')

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(report_path)


@pytest.mark.parametrize("form", ["path", "text", "bytes"])
def test_parse_report_enforces_byte_limit_for_serialized_inputs(
    form: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pip_report_module, "MAX_PIP_REPORT_BYTES", 128, raising=False)
    payload = json.dumps({"version": "1", "install": [], "padding": "x" * 200})
    report_input: Path | str | bytes
    if form == "path":
        report_input = tmp_path / "report.json"
        report_input.write_text(payload, encoding="utf-8")
    elif form == "bytes":
        report_input = payload.encode()
    else:
        report_input = payload

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(report_input)


@pytest.mark.parametrize(
    "extra",
    [
        "x" * 65537,
        {f"field-{index}": index for index in range(129)},
        float("nan"),
    ],
)
def test_parse_report_bounds_in_memory_json_structure(extra: object) -> None:
    payload = _report()
    payload["extra"] = extra

    with pytest.raises(PipReportSchemaError):
        parse_pip_report(payload)


@pytest.mark.parametrize(
    "extra",
    [
        ["x" * 400, "y" * 400, "z" * 400],
        {("k" * 400) + str(index): index for index in range(3)},
        "\u4e2d" * 400,
    ],
)
def test_parse_report_enforces_aggregate_utf8_budget_for_mappings(
    extra: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pip_report_module, "MAX_PIP_REPORT_BYTES", 1024)
    payload = _report()
    payload["extra"] = extra

    with pytest.raises(PipReportSchemaError):
        parse_pip_report(payload)


def test_parse_report_maps_oversized_json_integer_to_typed_error() -> None:
    payload = '{"version":"1","install":[],"extra":' + ("9" * 5000) + "}"

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(payload)


def test_parse_report_maps_deep_json_nesting_to_typed_error() -> None:
    payload = (
        '{"version":"1","install":[],"extra":'
        + ("[" * 20_000)
        + "0"
        + ("]" * 20_000)
        + "}"
    )

    with pytest.raises(InvalidPipReportError):
        parse_pip_report(payload)


def test_parse_report_bounds_dependency_count() -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(
            _report(_install(requires_dist=tuple("dep" for _ in range(1001))))
        )


def test_parse_report_bounds_archive_hash_count() -> None:
    hashes = {f"sha{index}": "a" for index in range(17)}

    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(hashes=hashes)))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/demo-1.0.tar.gz",
        "https://example.test/demo-1.0.whl#fragment",
        "https://user:password@example.test/demo-1.0.whl",
        "http://example.test/demo-1.0.whl",
        "not a url/demo-1.0.whl",
        "https://bad host.example/demo-1.0-py3-none-any.whl",
        "https://example.test:not-a-port/demo-1.0-py3-none-any.whl",
        "https://[2001:db8::1/demo-1.0-py3-none-any.whl",
    ],
)
def test_parse_report_rejects_unsafe_or_nonwheel_artifacts(url: str) -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(url=url)))


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x7f"])
def test_parse_report_rejects_raw_unicode_controls_in_artifact_url(
    control: str,
) -> None:
    url = (
        f"https://example.test/bad{control}path/"
        "demo_pkg-1.2.3-py3-none-any.whl"
    )

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


@pytest.mark.parametrize("requested_order", [(False, True), (True, False)])
def test_parse_report_merges_requested_for_identical_duplicates(
    requested_order: tuple[bool, bool],
) -> None:
    result = parse_pip_report(
        _report(*(_install(requested=requested) for requested in requested_order))
    )

    assert len(result.packages) == 1
    assert result.packages[0].requested is True


@pytest.mark.parametrize(
    "conflict",
    [
        _install(hashes={"sha256": "different"}),
        _install(requires_dist=("other>=2",)),
        _install(requires_python=">=3.12"),
    ],
)
def test_parse_report_rejects_duplicate_observation_conflicts(
    conflict: dict[str, object],
) -> None:
    with pytest.raises(PipReportSchemaError):
        parse_pip_report(_report(_install(), conflict))


class _WritingRunner:
    def __init__(self, payload: dict[str, object], return_code: int = 0) -> None:
        self.payload = payload
        self.return_code = return_code
        self.calls: list[tuple[list[str], Path, timedelta, dict[str, str]]] = []
        self.requirements_text = ""
        self.attempt_mode = 0
        self.requirements_mode = 0

    def run(
        self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
    ) -> ProcessResult:
        self.calls.append((argv, cwd, timeout, env))
        requirements_path = Path(argv[-1])
        self.requirements_text = requirements_path.read_text(encoding="utf-8")
        self.attempt_mode = stat.S_IMODE(cwd.stat().st_mode)
        self.requirements_mode = stat.S_IMODE(requirements_path.lstat().st_mode)
        (cwd / "report.json").write_text(json.dumps(self.payload), encoding="utf-8")
        return ProcessResult(tuple(argv), self.return_code, "", "no credentials", timedelta(0))


def test_strict_resolver_uses_injected_runner_without_network(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    runner = _WritingRunner(_report(_install()))
    resolver = StrictResolver(tmp_path, runner=runner, timeout=timedelta(seconds=3))

    result = resolver.resolve(parse_requirements(b"demo-pkg==1.2.3\n"), profile_cp311_arm64, "PYPI")

    assert result.packages[0].name == "demo-pkg"
    assert runner.requirements_text == "demo-pkg==1.2.3\n"
    assert runner.attempt_mode == 0o700
    assert runner.requirements_mode == 0o600
    attempt_directory = runner.calls[0][1]
    assert attempt_directory.parent == tmp_path.resolve()
    assert not attempt_directory.exists()
    assert runner.calls[0][0] == build_resolve_argv(
        attempt_directory / "requirements.txt",
        attempt_directory / "report.json",
        profile_cp311_arm64,
        "PYPI",
    )
    assert runner.calls[0][3] == {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }


def test_strict_resolver_ignores_fixed_path_symlinks_and_cleans_attempt(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    outside_requirements = tmp_path / "outside-requirements.txt"
    outside_report = tmp_path / "outside-report.json"
    outside_requirements.write_text("sentinel requirements", encoding="utf-8")
    outside_report.write_text("sentinel report", encoding="utf-8")
    (tmp_path / "requirements.txt").symlink_to(outside_requirements)
    (tmp_path / "report.json").symlink_to(outside_report)
    runner = _WritingRunner(_report(_install()))

    result = StrictResolver(tmp_path, runner=runner).resolve(
        parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI"
    )

    assert result.packages[0].name == "demo-pkg"
    assert outside_requirements.read_text(encoding="utf-8") == "sentinel requirements"
    assert outside_report.read_text(encoding="utf-8") == "sentinel report"
    assert not runner.calls[0][1].exists()


def test_strict_resolver_rejects_symlink_report_and_cleans_attempt(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    outside_report = tmp_path / "outside-report.json"
    outside_report.write_text(json.dumps(_report(_install())), encoding="utf-8")

    class _SymlinkReportRunner(_WritingRunner):
        def run(
            self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
        ) -> ProcessResult:
            self.calls.append((argv, cwd, timeout, env))
            (cwd / "report.json").symlink_to(outside_report)
            return ProcessResult(tuple(argv), 0, "", "", timedelta(0))

    runner = _SymlinkReportRunner(_report())

    with pytest.raises(InvalidPipReportError):
        StrictResolver(tmp_path, runner=runner).resolve(
            parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI"
        )

    assert not runner.calls[0][1].exists()


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

    stale_report = tmp_path / "report.json"
    stale_report.write_text(json.dumps(_report(_install())), encoding="utf-8")
    resolver = StrictResolver(tmp_path, runner=_NoReportRunner())

    with pytest.raises(ResolverMissingReportError):
        resolver.resolve(parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI")

    assert stale_report.read_text(encoding="utf-8") == json.dumps(_report(_install()))


def test_strict_resolver_maps_runner_failures_to_typed_error(
    profile_cp311_arm64: TargetProfile, tmp_path: Path
) -> None:
    class _TimeoutRunner:
        def __init__(self) -> None:
            self.cwd: Path | None = None

        def run(
            self, argv: list[str], cwd: Path, timeout: timedelta, env: dict[str, str]
        ) -> ProcessResult:
            self.cwd = cwd
            raise ProcessTimeoutError("partial output", "partial error")

    runner = _TimeoutRunner()
    resolver = StrictResolver(tmp_path, runner=runner)

    with pytest.raises(ResolverProcessError):
        resolver.resolve(parse_requirements(b"demo\n"), profile_cp311_arm64, "PYPI")

    assert runner.cwd is not None
    assert not runner.cwd.exists()


@pytest.mark.parametrize(
    "timeout",
    [timedelta(0), timedelta(minutes=10, microseconds=1), "one minute"],
)
def test_strict_resolver_rejects_invalid_or_unbounded_timeout(
    timeout: object, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        StrictResolver(tmp_path, timeout=timeout)  # type: ignore[arg-type]
