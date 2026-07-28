import re
from dataclasses import replace

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .models import (
    DuplicateRequirementConflictError,
    InputTooLargeError,
    InvalidRequirementError,
    ParsedRequirements,
    RequirementItem,
    RequirementsDecodeError,
    TooManyLinesError,
    UnsupportedRequirementSyntax,
)


MAX_INPUT_BYTES = 512 * 1024
MAX_LOGICAL_LINES = 2000
_PIP_ARGUMENT = re.compile(r"(?:^|[ \t])--?[a-z]", re.IGNORECASE)
_WINDOWS_DRIVE_PATH = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tar.bz2", ".tgz", ".zip")
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")
_SUPPORTED_OPERATORS = {"==", ">=", "<=", "~="}
_OPERATOR_ORDER = {"==": 0, "~=": 1, ">=": 2, "<=": 3}


def parse_requirements(raw: bytes) -> ParsedRequirements:
    if len(raw) > MAX_INPUT_BYTES:
        raise InputTooLargeError(len(raw), MAX_INPUT_BYTES)

    text, encoding = _decode(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = _logical_lines(text)
    if len(lines) > MAX_LOGICAL_LINES:
        raise TooManyLinesError(len(lines), MAX_LOGICAL_LINES)

    items: list[RequirementItem] = []
    indexes: dict[tuple[str, str | None], int] = {}
    constraints_by_key: dict[tuple[str, str | None], tuple[Specifier, ...]] = {}

    for line_no, original_text in enumerate(lines, start=1):
        requirement_text = _requirement_text(original_text, line_no)
        if requirement_text is None:
            continue

        parsed, constraints = _parse_line(requirement_text, line_no, original_text)
        marker = str(parsed.marker) if parsed.marker is not None else None
        name = canonicalize_name(parsed.name)
        extras = tuple(sorted(canonicalize_name(extra) for extra in parsed.extras))
        key = (name, marker)
        item = RequirementItem(
            line_no=line_no,
            name=name,
            extras=extras,
            specifier=_format_specifiers(constraints),
            marker=marker,
            original_text=original_text,
        )

        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(items)
            constraints_by_key[key] = constraints
            items.append(item)
            continue

        existing = items[existing_index]
        merged_constraints = _deduplicate_specifiers(
            (*constraints_by_key[key], *constraints)
        )
        if _has_obvious_conflict(merged_constraints):
            raise DuplicateRequirementConflictError(
                name, line_no, original_text, existing.line_no
            )
        constraints_by_key[key] = merged_constraints
        items[existing_index] = replace(
            existing,
            extras=tuple(sorted(set(existing.extras) | set(extras))),
            specifier=_format_specifiers(merged_constraints),
        )

    result_items = tuple(items)
    normalized_text = "\n".join(_format_item(item) for item in result_items) + "\n"
    return ParsedRequirements(normalized_text, result_items, encoding)


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    else:
        try:
            return raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("gbk"), "gbk"
    except UnicodeDecodeError as error:
        raise RequirementsDecodeError() from error


def _logical_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _requirement_text(original_text: str, line_no: int) -> str | None:
    if "\x00" in original_text:
        raise UnsupportedRequirementSyntax(
            "NUL bytes are not supported", line_no, original_text
        )

    stripped = original_text.strip()
    if not stripped or stripped.startswith("#"):
        return None

    comment_index = _find_unquoted(stripped, "#", whitespace_before=True)
    if comment_index is not None:
        stripped = stripped[:comment_index].rstrip()
    if stripped.endswith("\\"):
        raise UnsupportedRequirementSyntax(
            "line continuation is not supported", line_no, original_text
        )
    if not stripped:
        return None
    _reject_unsafe_text(stripped, line_no, original_text)
    return stripped


def _reject_unsafe_text(text: str, line_no: int, original_text: str) -> None:
    marker_index = _find_unquoted(text, ";")
    requirement_part = text if marker_index is None else text[:marker_index]
    lowered = requirement_part.casefold()
    if _PIP_ARGUMENT.search(requirement_part):
        raise UnsupportedRequirementSyntax(
            "pip options and file includes are not supported", line_no, original_text
        )
    if "@" in requirement_part or "://" in lowered or lowered.startswith("file:"):
        raise UnsupportedRequirementSyntax(
            "direct references and URLs are not supported", line_no, original_text
        )
    if any(prefix in lowered for prefix in _VCS_PREFIXES):
        raise UnsupportedRequirementSyntax(
            "version control references are not supported", line_no, original_text
        )
    if (
        requirement_part.startswith(("/", "./", "../", "~/", "\\"))
        or _WINDOWS_DRIVE_PATH.match(requirement_part)
        or "/" in requirement_part
        or "\\" in requirement_part
        or lowered.endswith(_ARCHIVE_SUFFIXES)
    ):
        raise UnsupportedRequirementSyntax(
            "local paths and package archives are not supported", line_no, original_text
        )


def _find_unquoted(
    text: str, target: str, *, whitespace_before: bool = False
) -> int | None:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue

        if character in {'"', "'"}:
            quote = character
        elif character == target and (
            not whitespace_before
            or (index > 0 and text[index - 1] in {" ", "\t"})
        ):
            return index
    return None


def _parse_line(
    text: str, line_no: int, original_text: str
) -> tuple[Requirement, tuple[Specifier, ...]]:
    try:
        parsed = Requirement(text)
    except InvalidRequirement as error:
        raise InvalidRequirementError(
            "invalid PEP 508 requirement", line_no, original_text
        ) from error

    if parsed.url is not None:
        raise UnsupportedRequirementSyntax(
            "direct references and URLs are not supported", line_no, original_text
        )

    constraints = tuple(parsed.specifier)
    for specifier in constraints:
        if specifier.operator not in _SUPPORTED_OPERATORS or "*" in specifier.version:
            raise UnsupportedRequirementSyntax(
                f"specifier {specifier!s} is not supported", line_no, original_text
            )
        try:
            Version(specifier.version)
        except InvalidVersion as error:
            raise InvalidRequirementError(
                f"invalid version in {specifier!s}", line_no, original_text
            ) from error
    return parsed, _deduplicate_specifiers(constraints)


def _deduplicate_specifiers(
    constraints: tuple[Specifier, ...]
) -> tuple[Specifier, ...]:
    unique = {(specifier.operator, specifier.version): specifier for specifier in constraints}
    return tuple(
        sorted(
            unique.values(),
            key=lambda value: (
                _OPERATOR_ORDER[value.operator],
                Version(value.version),
                value.version,
            ),
        )
    )


def _has_obvious_conflict(constraints: tuple[Specifier, ...]) -> bool:
    exact_versions = {
        Version(specifier.version)
        for specifier in constraints
        if specifier.operator == "=="
    }
    if len(exact_versions) > 1:
        return True
    if exact_versions:
        exact = next(iter(exact_versions))
        return any(
            not SpecifierSet(str(specifier)).contains(exact, prereleases=True)
            for specifier in constraints
        )

    lower: Version | None = None
    upper: Version | None = None
    upper_inclusive = True
    for specifier in constraints:
        version = Version(specifier.version)
        if specifier.operator in {">=", "~="}:
            lower = version if lower is None else max(lower, version)
        if specifier.operator == "<=":
            if upper is None or version < upper:
                upper = version
                upper_inclusive = True
        elif specifier.operator == "~=":
            compatible_upper = _compatible_upper_bound(version)
            if upper is None or compatible_upper <= upper:
                upper = compatible_upper
                upper_inclusive = False

    if lower is None or upper is None:
        return False
    return lower > upper or (lower == upper and not upper_inclusive)


def _compatible_upper_bound(version: Version) -> Version:
    prefix = version.release[:-1]
    upper_release = (*prefix[:-1], prefix[-1] + 1)
    rendered = ".".join(str(part) for part in upper_release)
    if version.epoch:
        rendered = f"{version.epoch}!{rendered}"
    return Version(rendered)


def _format_specifiers(constraints: tuple[Specifier, ...]) -> str:
    return ",".join(str(specifier) for specifier in constraints)


def _format_item(item: RequirementItem) -> str:
    extras = f"[{','.join(item.extras)}]" if item.extras else ""
    marker = f"; {item.marker}" if item.marker is not None else ""
    return f"{item.name}{extras}{item.specifier}{marker}"
