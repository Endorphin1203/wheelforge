from __future__ import annotations

import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
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
        timeout_error: subprocess.TimeoutExpired | None = None
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=dict(env),
                    timeout=timeout.total_seconds(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as error:
                timeout_error = error
            except OSError as error:
                raise ProcessExecutionError("process could not be started") from error

            stdout = _read_bounded(stdout_file)
            stderr = _read_bounded(stderr_file)

        if timeout_error is not None:
            raise ProcessTimeoutError(stdout, stderr) from timeout_error

        return ProcessResult(
            command,
            completed.returncode,
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


def _read_bounded(stream: BinaryIO) -> str:
    stream.flush()
    stream.seek(0)
    return _bounded_bytes(stream.read(MAX_PROCESS_OUTPUT_BYTES + 1))


def _bounded_text(value: str) -> str:
    return _bounded_bytes(value[: MAX_PROCESS_OUTPUT_BYTES + 1].encode())


def _bounded_bytes(value: bytes) -> str:
    decoded = value.decode(errors="replace")
    encoded = decoded.encode()
    if len(encoded) <= MAX_PROCESS_OUTPUT_BYTES:
        return decoded
    content_limit = MAX_PROCESS_OUTPUT_BYTES - len(_TRUNCATION_MARKER_BYTES)
    content = encoded[:content_limit].decode("utf-8", errors="ignore")
    return content + PROCESS_OUTPUT_TRUNCATION_MARKER


def _monotonic() -> float:
    from time import monotonic

    return monotonic()
