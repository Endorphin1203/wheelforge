from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping, Sequence


_ENVIRONMENT_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MAX_DIAGNOSTIC_CHARS = 4096


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
        self.stdout = _bounded_output(stdout)
        self.stderr = _bounded_output(stderr)
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
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=dict(env),
                timeout=timeout.total_seconds(),
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ProcessTimeoutError(
                _as_text(error.stdout), _as_text(error.stderr)
            ) from error
        except OSError as error:
            raise ProcessExecutionError("process could not be started") from error

        return ProcessResult(
            command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
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


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _bounded_output(value: str) -> str:
    return value[:_MAX_DIAGNOSTIC_CHARS]


def _monotonic() -> float:
    from time import monotonic

    return monotonic()
