from __future__ import annotations

import re
from typing import Any


_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#@]+@")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:password|passwd|pwd|token|access_token|refresh_token|api[_-]?key|secret)=)[^&#\s]+"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|api[_-]?key|secret)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/-]+=*")


def sanitize_text(value: object, *, limit: int = 2000) -> str:
    text = str(value).replace("\x00", "?")
    text = _URL_USERINFO.sub(r"\1***@", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    text = _KEY_VALUE_SECRET.sub(r"\1=***", text)
    text = _BEARER_SECRET.sub(r"\1 ***", text)
    return text[:limit]


def sanitize_error(error: BaseException, *, limit: int = 2000) -> str:
    lines: list[str] = []
    stack: list[tuple[BaseException, int]] = [(error, 0)]
    while stack:
        current, depth = stack.pop()
        prefix = "  " * min(depth, 8)
        if isinstance(current, BaseExceptionGroup):
            lines.append(
                f"{prefix}{type(current).__name__}: {sanitize_text(current.message)}"
            )
            stack.extend((child, depth + 1) for child in reversed(current.exceptions))
        else:
            lines.append(
                f"{prefix}{type(current).__name__}: {sanitize_text(current)}"
            )
        if sum(len(line) + 1 for line in lines) >= limit:
            break
    return sanitize_text("\n".join(lines), limit=limit)


def sanitize_structure(value: Any, *, string_limit: int = 2000) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, limit=string_limit)
    if isinstance(value, dict):
        return {
            sanitize_text(key, limit=200) if isinstance(key, str) else str(key): sanitize_structure(
                item, string_limit=string_limit
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_structure(item, string_limit=string_limit) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_structure(item, string_limit=string_limit) for item in value)
    return value


def contains_exception(error: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(error, kind):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(contains_exception(child, kind) for child in error.exceptions)
    return False
