from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import os
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
MARKER_NAME = ".wheelforge-fixture-index.json"
MARKER_CONTENT = b'{"owner":"wheelforge-fixture-builder","schema":1}\n'
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WheelSpec:
    name: str
    version: str
    tag: str
    requires: tuple[str, ...] = ()

    @property
    def distribution(self) -> str:
        return self.name.replace("-", "_")

    @property
    def filename(self) -> str:
        return f"{self.distribution}-{self.version}-{self.tag}.whl"


SPECS = (
    WheelSpec("conflict-a", "1.0.0", "py3-none-any", ("demo-common (<1.2.3)",)),
    WheelSpec("conflict-b", "1.0.0", "py3-none-any", ("demo-common (>=1.2.4)",)),
    WheelSpec("demo-common", "1.2.2", "py3-none-any"),
    WheelSpec("demo-common", "1.2.3", "py3-none-any"),
    WheelSpec("demo-common", "1.2.4", "py3-none-any"),
    WheelSpec("demo-direct", "1.0.0", "py3-none-any", ("demo-common (==1.2.3)",)),
    WheelSpec("demo-native", "1.0.0", "cp310-cp310-manylinux2014_aarch64"),
    WheelSpec("demo-native", "1.0.0", "cp311-abi3-manylinux2014_aarch64"),
    WheelSpec("demo-native", "1.0.0", "cp311-cp311-manylinux2014_aarch64"),
    WheelSpec("demo-native", "1.0.0", "py3-none-any"),
    WheelSpec("downgrade-demo", "1.2.2", "cp311-cp311-manylinux2014_aarch64"),
    WheelSpec("downgrade-demo", "1.2.3", "cp311-cp311-manylinux2014_x86_64"),
    WheelSpec("missing-demo", "1.2.3", "cp311-cp311-manylinux2014_x86_64"),
    WheelSpec("partial-demo", "1.0.0", "py3-none-any"),
    WheelSpec("slow-demo", "1.0.0", "py3-none-any"),
    WheelSpec("unchanged-demo", "1.2.3", "cp311-cp311-manylinux2014_aarch64"),
    WheelSpec("upgrade-demo", "1.2.3", "cp311-cp311-manylinux2014_x86_64"),
    WheelSpec("upgrade-demo", "1.2.4", "cp311-cp311-manylinux2014_aarch64"),
    WheelSpec("wrong-tag-demo", "1.0.0", "cp310-cp310-manylinux2014_aarch64"),
    WheelSpec("wrong-tag-demo", "1.0.0", "cp311-cp311-win_amd64"),
)


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _wheel_bytes(spec: WheelSpec) -> bytes:
    dist_info = f"{spec.distribution}-{spec.version}.dist-info"
    metadata_lines = [
        "Metadata-Version: 2.1",
        f"Name: {spec.name}",
        f"Version: {spec.version}",
        "Summary: Deterministic WheelForge integration fixture",
        "Requires-Python: >=3.9",
    ]
    metadata_lines.extend(f"Requires-Dist: {requirement}" for requirement in spec.requires)
    metadata = ("\n".join(metadata_lines) + "\n").encode("utf-8")
    purelib = "true" if spec.tag.endswith("-none-any") else "false"
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: WheelForge fixture builder\n"
        f"Root-Is-Purelib: {purelib}\n"
        f"Tag: {spec.tag}\n"
    ).encode("utf-8")
    files = {
        f"{spec.distribution}/__init__.py": (
            f'__version__ = "{spec.version}"\n'.encode("utf-8")
        ),
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": wheel_metadata,
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path, content in sorted(files.items()):
        writer.writerow((path, _record_hash(content), len(content)))
    record_path = f"{dist_info}/RECORD"
    writer.writerow((record_path, "", ""))
    files[record_path] = record.getvalue().encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _write_page(path: Path, links: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as page:
        page.write("<!doctype html>\n")
        for href, label in links:
            page.write(f'<a href="{html.escape(href)}">{html.escape(label)}</a>\n')


def _populate(output_root: Path) -> None:
    package_root = output_root / "packages"
    simple_root = output_root / "simple"
    package_root.mkdir(parents=True)

    projects: dict[str, list[tuple[str, str]]] = {}
    for spec in sorted(SPECS, key=lambda item: item.filename):
        content = _wheel_bytes(spec)
        (package_root / spec.filename).write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        href = f"../../packages/{spec.filename}#sha256={digest}"
        projects.setdefault(spec.name, []).append((href, spec.filename))

    for project, links in sorted(projects.items()):
        _write_page(simple_root / project / "index.html", links)
    _write_page(
        simple_root / "index.html",
        [(f"{project}/", project) for project in sorted(projects)],
    )
    (output_root / MARKER_NAME).write_bytes(MARKER_CONTENT)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link_or_reparse_point(path: Path, status: os.stat_result) -> bool:
    if stat.S_ISLNK(status.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _validate_ancestry(output_root: Path) -> None:
    path_and_parents = (output_root, *output_root.parents)
    for candidate in reversed(path_and_parents):
        try:
            status = candidate.lstat()
        except FileNotFoundError:
            continue
        if _is_link_or_reparse_point(candidate, status):
            raise ValueError(f"output path traverses a link or reparse point: {candidate}")


def _validate_output(output_root: Path) -> bool:
    _validate_ancestry(output_root)
    if output_root == Path(output_root.anchor):
        raise ValueError(f"refusing filesystem root as output: {output_root}")
    if output_root in {REPOSITORY_ROOT, _absolute_path(Path.home())}:
        raise ValueError(f"refusing protected directory as output: {output_root}")

    parent = output_root.parent
    if not parent.is_dir():
        raise ValueError(f"output parent must be an existing plain directory: {parent}")
    if not output_root.exists():
        return False
    if not output_root.is_dir():
        raise ValueError(f"output must be a directory: {output_root}")

    marker = output_root / MARKER_NAME
    if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != MARKER_CONTENT:
        raise ValueError(f"existing output is not owned by this fixture builder: {output_root}")
    return True


def _publish(staging_root: Path, output_root: Path, replacing: bool) -> None:
    if not replacing:
        os.replace(staging_root, output_root)
        return

    backup = output_root.parent / f".{output_root.name}.previous-{uuid.uuid4().hex}"
    os.replace(output_root, backup)
    try:
        os.replace(staging_root, output_root)
    except BaseException:
        os.replace(backup, output_root)
        raise
    shutil.rmtree(backup)


def build(output_root: Path) -> None:
    output_root = _absolute_path(output_root)
    replacing = _validate_output(output_root)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.build-", dir=output_root.parent)
    )
    try:
        _populate(staging_root)
        _publish(staging_root, output_root, replacing)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_fixtures.py OUTPUT_DIRECTORY")
    try:
        build(Path(sys.argv[1]))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
