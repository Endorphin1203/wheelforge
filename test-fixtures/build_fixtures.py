from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path


ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


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
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compresslevel=9)
    return output.getvalue()


def _write_page(path: Path, links: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as page:
        page.write("<!doctype html>\n")
        for href, label in links:
            page.write(f'<a href="{html.escape(href)}">{html.escape(label)}</a>\n')


def build(output_root: Path) -> None:
    if output_root.exists():
        shutil.rmtree(output_root)
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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_fixtures.py OUTPUT_DIRECTORY")
    build(Path(sys.argv[1]).resolve())
