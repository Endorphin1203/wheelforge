from __future__ import annotations

import hashlib
import io
import json
import warnings
import zipfile

import pytest

from helpers.artifact import ArtifactError, ArtifactReader


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _valid_archive() -> bytes:
    entries = {
        "README.md": b"offline bundle\n",
        "install.sh": b"#!/bin/sh\n",
        "verify.sh": b"#!/bin/sh\n",
        "manifest.json": json.dumps(
            {
                "validationLevel": "STATIC",
                "installVerified": False,
                "target": {"pythonVersion": "3.11"},
            },
            separators=(",", ":"),
        ).encode(),
        "packages/demo-1.0.0-py3-none-any.whl": b"wheel",
    }
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(entries.items())
    ).encode()
    return _archive([*entries.items(), ("checksums.sha256", checksums)])


def test_reader_exposes_manifest_wheels_and_linux_scripts() -> None:
    artifact = ArtifactReader.open_bytes(_valid_archive())

    assert artifact.manifest["target"]["pythonVersion"] == "3.11"
    assert artifact.wheels == ("packages/demo-1.0.0-py3-none-any.whl",)
    assert artifact.scripts == ("install.sh", "verify.sh")
    assert artifact.verify_checksums() is True


def test_reader_rejects_duplicate_zip_members() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        content = _archive(
            [
                ("manifest.json", b"{}"),
                ("manifest.json", b"{}"),
                ("checksums.sha256", b""),
            ]
        )

    with pytest.raises(ArtifactError, match="duplicate"):
        ArtifactReader.open_bytes(content)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "packages/../../escape", "packages//wheel.whl"]
)
def test_reader_rejects_unsafe_member_paths(name: str) -> None:
    content = _archive(
        [(name, b"bad"), ("manifest.json", b"{}"), ("checksums.sha256", b"")]
    )

    with pytest.raises(ArtifactError, match="unsafe"):
        ArtifactReader.open_bytes(content)


def test_checksum_verification_rejects_tampering() -> None:
    entries = {
        "manifest.json": b"{}",
        "README.md": b"tampered",
    }
    checksums = (
        f"{'0' * 64}  README.md\n"
        f"{hashlib.sha256(entries['manifest.json']).hexdigest()}  manifest.json\n"
    ).encode()
    artifact = ArtifactReader.open_bytes(
        _archive([*entries.items(), ("checksums.sha256", checksums)])
    )

    assert artifact.verify_checksums() is False


def test_reader_rejects_checksum_manifest_that_omits_a_member() -> None:
    content = _archive(
        [
            ("manifest.json", b"{}"),
            ("README.md", b"present"),
            (
                "checksums.sha256",
                f"{hashlib.sha256(b'{}').hexdigest()}  manifest.json\n".encode(),
            ),
        ]
    )
    artifact = ArtifactReader.open_bytes(content)

    assert artifact.verify_checksums() is False
