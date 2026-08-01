from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = ROOT / "test-fixtures" / "index"
BUILD_SCRIPT = ROOT / "test-fixtures" / "build-fixtures.sh"
PROJECT_ROOT = ROOT / "test-fixtures" / "projects"

EXPECTED_WHEELS = {
    "conflict-a": {"conflict_a-1.0.0-py3-none-any.whl"},
    "conflict-b": {"conflict_b-1.0.0-py3-none-any.whl"},
    "demo-common": {
        "demo_common-1.2.2-py3-none-any.whl",
        "demo_common-1.2.3-py3-none-any.whl",
        "demo_common-1.2.4-py3-none-any.whl",
    },
    "demo-direct": {"demo_direct-1.0.0-py3-none-any.whl"},
    "demo-native": {
        "demo_native-1.0.0-cp310-cp310-manylinux2014_aarch64.whl",
        "demo_native-1.0.0-cp311-abi3-manylinux2014_aarch64.whl",
        "demo_native-1.0.0-cp311-cp311-manylinux2014_aarch64.whl",
        "demo_native-1.0.0-py3-none-any.whl",
    },
    "downgrade-demo": {
        "downgrade_demo-1.2.2-cp311-cp311-manylinux2014_aarch64.whl",
        "downgrade_demo-1.2.3-cp311-cp311-manylinux2014_x86_64.whl",
    },
    "missing-demo": {
        "missing_demo-1.2.3-cp311-cp311-manylinux2014_x86_64.whl"
    },
    "unchanged-demo": {
        "unchanged_demo-1.2.3-cp311-cp311-manylinux2014_aarch64.whl"
    },
    "upgrade-demo": {
        "upgrade_demo-1.2.3-cp311-cp311-manylinux2014_x86_64.whl",
        "upgrade_demo-1.2.4-cp311-cp311-manylinux2014_aarch64.whl",
    },
    "wrong-tag-demo": {
        "wrong_tag_demo-1.0.0-cp310-cp310-manylinux2014_aarch64.whl",
        "wrong_tag_demo-1.0.0-cp311-cp311-win_amd64.whl",
    },
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.links.append(href)


def _links(page: str) -> list[str]:
    parser = _LinkParser()
    parser.feed(page)
    return parser.links


def _get_text(url: str) -> tuple[int, str]:
    with urlopen(url) as response:
        return response.status, response.read().decode("utf-8")


def _build_index(output_root: Path, python: str = sys.executable) -> None:
    environment = {
        **os.environ,
        "PYTHON": python,
        "PIP_NO_INDEX": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    subprocess.run(
        [str(BUILD_SCRIPT), str(output_root)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def generated_index(tmp_path: Path) -> Path:
    output_root = tmp_path / "index"
    _build_index(output_root)
    return output_root


def _tree_fingerprint(root: Path) -> list[tuple[str, str]]:
    return [
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _read_wheel_metadata(content: bytes, suffix: str) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        member = next(name for name in archive.namelist() if name.endswith(suffix))
        return archive.read(member).decode("utf-8")


def _assert_valid_record(content: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        record_name = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
        rows = csv.reader(io.StringIO(archive.read(record_name).decode("utf-8")))
        recorded_names: set[str] = set()
        for name, encoded_hash, size in rows:
            recorded_names.add(name)
            if name == record_name:
                assert encoded_hash == ""
                assert size == ""
                continue
            member = archive.read(name)
            expected_hash = base64.urlsafe_b64encode(hashlib.sha256(member).digest()).rstrip(b"=")
            assert encoded_hash == f"sha256={expected_hash.decode('ascii')}"
            assert size == str(len(member))
        assert recorded_names == set(archive.namelist())


@contextmanager
def _serve_index(index_root: Path) -> Iterator[str]:
    handler = partial(_QuietHandler, directory=str(index_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_fixture_index_exposes_only_wheels(generated_index: Path) -> None:
    with _serve_index(generated_index) as base_url:
        status, page = _get_text(f"{base_url}/simple/demo-common/")

    assert status == 200
    assert ".whl" in page
    assert ".tar.gz" not in page


def test_fixture_index_exposes_complete_compatibility_matrix(generated_index: Path) -> None:
    with _serve_index(generated_index) as base_url:
        status, root_page = _get_text(f"{base_url}/simple/")
        assert status == 200
        project_links = _links(root_page)
        project_names = {link.strip("/") for link in project_links}
        assert project_names == set(EXPECTED_WHEELS)

        for project_link in project_links:
            project_url = urljoin(f"{base_url}/simple/", project_link)
            page_status, project_page = _get_text(project_url)
            filenames = {
                Path(urlparse(link).path).name for link in _links(project_page)
            }
            assert page_status == 200
            assert filenames == EXPECTED_WHEELS[project_link.strip("/")]


def test_all_simple_links_are_hashed_wheels_with_valid_records(generated_index: Path) -> None:
    with _serve_index(generated_index) as base_url:
        _, root_page = _get_text(f"{base_url}/simple/")
        for project_link in _links(root_page):
            project_url = urljoin(f"{base_url}/simple/", project_link)
            _, project_page = _get_text(project_url)
            for link in _links(project_page):
                parsed = urlparse(link)
                assert parsed.path.endswith(".whl")
                fragment = parse_qs(parsed.fragment)
                assert set(fragment) == {"sha256"}
                assert len(fragment["sha256"]) == 1
                assert len(fragment["sha256"][0]) == 64
                with urlopen(urljoin(project_url, link)) as response:
                    wheel = response.read()
                assert hashlib.sha256(wheel).hexdigest() == fragment["sha256"][0]
                _assert_valid_record(wheel)

    assert {path.suffix for path in (generated_index / "packages").iterdir()} == {".whl"}


def test_wheel_metadata_covers_dependency_and_tag_scenarios(generated_index: Path) -> None:
    wheels = {
        path.name: path.read_bytes() for path in (generated_index / "packages").iterdir()
    }
    direct_metadata = _read_wheel_metadata(
        wheels["demo_direct-1.0.0-py3-none-any.whl"], ".dist-info/METADATA"
    )
    low_metadata = _read_wheel_metadata(
        wheels["conflict_a-1.0.0-py3-none-any.whl"], ".dist-info/METADATA"
    )
    high_metadata = _read_wheel_metadata(
        wheels["conflict_b-1.0.0-py3-none-any.whl"], ".dist-info/METADATA"
    )
    assert "Requires-Dist: demo-common (==1.2.3)" in direct_metadata
    assert "Requires-Dist: demo-common (<1.2.3)" in low_metadata
    assert "Requires-Dist: demo-common (>=1.2.4)" in high_metadata

    native_tags = {
        line.removeprefix("Tag: ")
        for filename, content in wheels.items()
        if filename.startswith("demo_native-")
        for line in _read_wheel_metadata(content, ".dist-info/WHEEL").splitlines()
        if line.startswith("Tag: ")
    }
    assert native_tags == {
        "cp310-cp310-manylinux2014_aarch64",
        "cp311-abi3-manylinux2014_aarch64",
        "cp311-cp311-manylinux2014_aarch64",
        "py3-none-any",
    }


def test_fixture_build_is_byte_reproducible_across_local_pythons(tmp_path: Path) -> None:
    system_index = tmp_path / "system-python"
    test_index = tmp_path / "test-python"
    _build_index(system_index, "/usr/bin/python3")
    _build_index(test_index, sys.executable)

    assert _tree_fingerprint(system_index) == _tree_fingerprint(test_index)


def test_checked_in_index_matches_a_fresh_offline_build(generated_index: Path) -> None:
    assert _tree_fingerprint(INDEX_ROOT) == _tree_fingerprint(generated_index)


def test_requirement_projects_cover_all_fixture_outcomes() -> None:
    project_files = {path.name for path in PROJECT_ROOT.glob("*.txt")}
    assert project_files == {
        "compatibility.txt",
        "conflict.txt",
        "downgrade.txt",
        "malicious.txt",
        "missing.txt",
        "success.txt",
        "unchanged.txt",
        "upgrade.txt",
        "wrong-tag.txt",
    }
    assert "demo-direct==1.0.0" in (PROJECT_ROOT / "success.txt").read_text()
    assert "upgrade-demo==1.2.3" in (PROJECT_ROOT / "upgrade.txt").read_text()
    assert "downgrade-demo==1.2.3" in (PROJECT_ROOT / "downgrade.txt").read_text()
