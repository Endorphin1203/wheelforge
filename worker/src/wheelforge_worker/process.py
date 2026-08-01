from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, Mapping, Sequence


_ENVIRONMENT_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_PROCESS_OUTPUT_BYTES = 4096
PROCESS_OUTPUT_TRUNCATION_MARKER = "\n...[truncated]..."
_TRUNCATION_MARKER_BYTES = PROCESS_OUTPUT_TRUNCATION_MARKER.encode()
_DRAIN_JOIN_TIMEOUT_SECONDS = 0.5
_DRAIN_CLOSE_JOIN_TIMEOUT_SECONDS = 0.1
_WINDOWS_TREE_KILL_TIMEOUT_SECONDS = 5.0
_PROCESS_REAP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    elapsed: timedelta


class ProcessError(RuntimeError):
    """Base class for failures at the restricted subprocess boundary."""


class ProcessValidationError(ProcessError):
    pass


class ProcessTimeoutError(ProcessError):
    def __init__(self, stdout: str, stderr: str) -> None:
        self.stdout = _bounded_text(stdout)
        self.stderr = _bounded_text(stderr)
        super().__init__("process timed out")


class ProcessExecutionError(ProcessError):
    pass


@dataclass(slots=True)
class _PipeCapture:
    prefix: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    error: Exception | None = None

    def retain(self, chunk: bytes) -> None:
        remaining = MAX_PROCESS_OUTPUT_BYTES - len(self.prefix)
        if remaining > 0:
            self.prefix.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True


class ProcessRunner:
    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout: timedelta,
        env: Mapping[str, str],
        *,
        inherited_fds: tuple[int, ...] = (),
    ) -> ProcessResult:
        command = _validate_invocation(argv, cwd, timeout, env)
        inherited_fds = _validate_inherited_fds(inherited_fds)
        started = _monotonic()
        try:
            process = _spawn_process_group(command, cwd, env, inherited_fds)
        except OSError as error:
            raise ProcessExecutionError("process could not be started") from error

        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            _terminate_process_tree(process)
            raise ProcessExecutionError("process output pipes were not created")

        stdout_capture = _PipeCapture()
        stderr_capture = _PipeCapture()
        threads = (
            threading.Thread(
                target=_drain_pipe,
                args=(stdout_pipe, stdout_capture),
                name="process-stdout-drain",
                daemon=True,
            ),
            threading.Thread(
                target=_drain_pipe,
                args=(stderr_pipe, stderr_capture),
                name="process-stderr-drain",
                daemon=True,
            ),
        )
        started_threads: list[threading.Thread] = []
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
        except RuntimeError as error:
            try:
                _terminate_process_tree(process)
            finally:
                _finish_drains(started_threads, (stdout_pipe, stderr_pipe))
            raise ProcessExecutionError("process output drain could not start") from error

        timeout_error: subprocess.TimeoutExpired | None = None
        lifecycle_error: ProcessExecutionError | None = None
        try:
            process.wait(timeout=timeout.total_seconds())
        except subprocess.TimeoutExpired as error:
            timeout_error = error
            try:
                _terminate_process_tree(process)
            except ProcessExecutionError as error:
                lifecycle_error = error
        except OSError as error:
            try:
                _terminate_process_tree(process)
            except ProcessExecutionError:
                pass
            lifecycle_error = ProcessExecutionError("process could not be reaped")
            lifecycle_error.__cause__ = error

        drains_finished = _finish_drains(threads, (stdout_pipe, stderr_pipe))

        stdout = _bounded_bytes(
            bytes(stdout_capture.prefix), truncated=stdout_capture.truncated
        )
        stderr = _bounded_bytes(
            bytes(stderr_capture.prefix), truncated=stderr_capture.truncated
        )

        if lifecycle_error is not None:
            raise lifecycle_error
        if not drains_finished:
            if timeout_error is None:
                try:
                    _terminate_process_tree(process)
                except ProcessExecutionError:
                    pass
            raise ProcessExecutionError("process output drain did not stop")
        if stdout_capture.error is not None or stderr_capture.error is not None:
            raise ProcessExecutionError("process output could not be drained")

        if timeout_error is not None:
            raise ProcessTimeoutError(stdout, stderr) from timeout_error

        return ProcessResult(
            command,
            process.returncode,
            stdout,
            stderr,
            timedelta(seconds=_monotonic() - started),
        )


def _validate_invocation(
    argv: Sequence[str], cwd: Path, timeout: timedelta, env: Mapping[str, str]
) -> tuple[str, ...]:
    if not isinstance(cwd, Path) or not cwd.is_dir():
        raise ProcessValidationError("cwd must be an existing directory")
    if not isinstance(timeout, timedelta) or timeout.total_seconds() <= 0:
        raise ProcessValidationError("timeout must be positive")
    if not math.isfinite(timeout.total_seconds()):
        raise ProcessValidationError("timeout must be finite")

    command = tuple(argv)
    if not command:
        raise ProcessValidationError("argv must not be empty")
    if any(not isinstance(token, str) or "\x00" in token for token in command):
        raise ProcessValidationError("argv must contain NUL-free strings")

    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not _ENVIRONMENT_KEY.fullmatch(key)
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ProcessValidationError("environment contains an unsafe key or value")
    return command


def _drain_pipe(stream: IO[Any], capture: _PipeCapture) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            capture.retain(chunk)
    except Exception as error:
        capture.error = error
    finally:
        stream.close()


def _spawn_process_group(
    command: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    inherited_fds: tuple[int, ...],
) -> subprocess.Popen[bytes]:
    if os.name == "nt":
        if inherited_fds:
            raise ProcessValidationError(
                "explicit descriptor inheritance is unavailable on Windows"
            )
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"),
        )
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
        start_new_session=True,
        pass_fds=inherited_fds,
    )


def _validate_inherited_fds(values: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(values, tuple) or any(
        type(value) is not int or value < 0 for value in values
    ):
        raise ProcessValidationError("inherited_fds must be nonnegative descriptors")
    if len(set(values)) != len(values):
        raise ProcessValidationError("inherited_fds must be unique")
    for descriptor in values:
        try:
            os.fstat(descriptor)
        except OSError as error:
            raise ProcessValidationError("inherited descriptor is not open") from error
    return values


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        _terminate_windows_process_tree(process)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        _finalize_parent_process(process)
        raise ProcessExecutionError("process group could not be terminated") from error
    try:
        process.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except Exception as error:
        raise ProcessExecutionError("process could not be reaped") from error


def _terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    taskkill_error: Exception | None = None
    taskkill_return_code: int | None = None
    try:
        completed = subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_WINDOWS_TREE_KILL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        taskkill_return_code = completed.returncode
    except Exception as error:
        taskkill_error = error

    parent_was_live = _finalize_parent_process(process)

    if taskkill_error is not None:
        raise ProcessExecutionError(
            "Windows process tree could not be terminated"
        ) from taskkill_error
    if taskkill_return_code != 0 and parent_was_live:
        raise ProcessExecutionError("Windows process tree could not be terminated")


def _finalize_parent_process(process: subprocess.Popen[bytes]) -> bool:
    poll_error: Exception | None = None
    kill_error: Exception | None = None
    reap_error: Exception | None = None
    try:
        parent_was_live = process.poll() is None
    except Exception as error:
        poll_error = error
        parent_was_live = True

    if parent_was_live:
        try:
            process.kill()
        except Exception as error:
            kill_error = error

    try:
        process.wait(timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except Exception as error:
        reap_error = error

    if reap_error is not None:
        raise ProcessExecutionError("process could not be reaped") from reap_error
    if kill_error is not None:
        raise ProcessExecutionError("process could not be terminated") from kill_error
    if poll_error is not None:
        raise ProcessExecutionError("process state could not be checked") from poll_error
    return parent_was_live


def _finish_drains(
    threads: Sequence[threading.Thread], pipes: tuple[IO[Any], IO[Any]]
) -> bool:
    deadline = _monotonic() + _DRAIN_JOIN_TIMEOUT_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - _monotonic()))
    if not any(thread.is_alive() for thread in threads):
        return True
    for pipe in pipes:
        try:
            pipe.close()
        except OSError:
            pass
    close_deadline = _monotonic() + _DRAIN_CLOSE_JOIN_TIMEOUT_SECONDS
    for thread in threads:
        thread.join(max(0.0, close_deadline - _monotonic()))
    return False


def _bounded_text(value: str) -> str:
    candidate = value[: MAX_PROCESS_OUTPUT_BYTES + 1]
    return _bounded_bytes(
        candidate.encode(), truncated=len(value) > MAX_PROCESS_OUTPUT_BYTES
    )


def _bounded_bytes(value: bytes, *, truncated: bool = False) -> str:
    decoded = value.decode(errors="replace")
    encoded = decoded.encode()
    if not truncated and len(encoded) <= MAX_PROCESS_OUTPUT_BYTES:
        return decoded
    content_limit = MAX_PROCESS_OUTPUT_BYTES - len(_TRUNCATION_MARKER_BYTES)
    content = encoded[:content_limit].decode("utf-8", errors="ignore")
    return content + PROCESS_OUTPUT_TRUNCATION_MARKER


def _monotonic() -> float:
    from time import monotonic

    return monotonic()
