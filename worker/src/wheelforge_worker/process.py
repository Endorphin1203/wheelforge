from __future__ import annotations

import math
import re
import subprocess
import threading
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence


_ENVIRONMENT_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_PROCESS_OUTPUT_BYTES = 4096
PROCESS_OUTPUT_TRUNCATION_MARKER = "\n...[truncated]..."
_TRUNCATION_MARKER_BYTES = PROCESS_OUTPUT_TRUNCATION_MARKER.encode()


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
    ) -> ProcessResult:
        command = _validate_invocation(argv, cwd, timeout, env)
        started = _monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            raise ProcessExecutionError("process could not be started") from error

        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            _kill_and_wait(process)
            raise ProcessExecutionError("process output pipes were not created")

        stdout_capture = _PipeCapture()
        stderr_capture = _PipeCapture()
        threads = (
            threading.Thread(
                target=_drain_pipe,
                args=(stdout_pipe, stdout_capture),
                name="process-stdout-drain",
            ),
            threading.Thread(
                target=_drain_pipe,
                args=(stderr_pipe, stderr_capture),
                name="process-stderr-drain",
            ),
        )
        started_threads: list[threading.Thread] = []
        try:
            for thread in threads:
                thread.start()
                started_threads.append(thread)
        except RuntimeError as error:
            _kill_and_wait(process)
            stdout_pipe.close()
            stderr_pipe.close()
            for thread in started_threads:
                thread.join()
            raise ProcessExecutionError("process output drain could not start") from error

        timeout_error: subprocess.TimeoutExpired | None = None
        wait_error: OSError | None = None
        try:
            process.wait(timeout=timeout.total_seconds())
        except subprocess.TimeoutExpired as error:
            timeout_error = error
            try:
                _kill_and_wait(process)
            except OSError as lifecycle_error:
                wait_error = lifecycle_error
        except OSError as error:
            wait_error = error
            try:
                _kill_and_wait(process)
            except OSError:
                pass
        finally:
            for thread in threads:
                thread.join()

        stdout = _bounded_bytes(
            bytes(stdout_capture.prefix), truncated=stdout_capture.truncated
        )
        stderr = _bounded_bytes(
            bytes(stderr_capture.prefix), truncated=stderr_capture.truncated
        )

        if wait_error is not None:
            raise ProcessExecutionError("process could not be reaped") from wait_error
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


def _drain_pipe(stream: BinaryIO, capture: _PipeCapture) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            capture.retain(chunk)
    except Exception as error:
        capture.error = error
    finally:
        stream.close()


def _kill_and_wait(process: subprocess.Popen[bytes]) -> None:
    process.kill()
    process.wait()


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
