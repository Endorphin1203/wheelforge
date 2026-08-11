from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping


_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([^\r\n]+)\Z")
_SCRIPT_NAMES = frozenset({"install.sh", "verify.sh", "install.bat", "verify.bat"})


class ArtifactError(ValueError):
    """The downloaded artifact is malformed or unsafe to inspect."""


@dataclass(frozen=True)
class ArtifactReader:
    _members: Mapping[str, bytes]
    manifest: Mapping[str, Any]
    checksums: Mapping[str, str]

    @classmethod
    def open(cls, path: Path) -> "ArtifactReader":
        with Path(path).open("rb") as stream:
            return cls._read(stream)

    @classmethod
    def open_bytes(cls, content: bytes) -> "ArtifactReader":
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        return cls._read(io.BytesIO(content))

    @classmethod
    def _read(cls, stream: BinaryIO) -> "ArtifactReader":
        try:
            with zipfile.ZipFile(stream) as archive:
                names = [item.filename for item in archive.infolist()]
                if len(names) != len(set(names)):
                    raise ArtifactError("artifact contains duplicate ZIP members")
                for name in names:
                    _require_safe_name(name)
                members = {name: archive.read(name) for name in names}
        except ArtifactError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise ArtifactError("artifact is not a readable ZIP") from error

        try:
            manifest_content = members["manifest.json"]
            checksum_content = members["checksums.sha256"]
        except KeyError as error:
            raise ArtifactError(f"artifact is missing {error.args[0]}") from error

        manifest = _load_manifest(manifest_content)
        checksums = _load_checksums(checksum_content)
        return cls(
            MappingProxyType(members),
            MappingProxyType(manifest),
            MappingProxyType(checksums),
        )

    @property
    def filenames(self) -> tuple[str, ...]:
        return tuple(sorted(self._members))

    @property
    def wheels(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.filenames
            if name.startswith("packages/") and name.endswith(".whl")
        )

    @property
    def scripts(self) -> tuple[str, ...]:
        return tuple(name for name in self.filenames if name in _SCRIPT_NAMES)

    def read(self, name: str) -> bytes:
        try:
            return self._members[name]
        except KeyError as error:
            raise ArtifactError(f"artifact member does not exist: {name}") from error

    def verify_checksums(self) -> bool:
        expected_names = set(self._members) - {"checksums.sha256"}
        if set(self.checksums) != expected_names:
            return False
        return all(
            hashlib.sha256(self._members[name]).hexdigest() == digest
            for name, digest in self.checksums.items()
        )


def _require_safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ArtifactError(f"artifact contains an unsafe ZIP member: {name!r}")


def _load_manifest(content: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactError(f"manifest contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactError(f"manifest contains non-standard number: {value}")
            ),
        )
    except ArtifactError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactError("manifest.json is invalid") from error
    if not isinstance(parsed, dict):
        raise ArtifactError("manifest.json must contain an object")
    return parsed


def _load_checksums(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ArtifactError("checksums.sha256 must be ASCII") from error
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ArtifactError("checksums.sha256 contains an invalid line")
        digest, name = match.groups()
        _require_safe_name(name)
        if name == "checksums.sha256" or name in result:
            raise ArtifactError("checksums.sha256 contains a duplicate or self entry")
        result[name] = digest
    return result
