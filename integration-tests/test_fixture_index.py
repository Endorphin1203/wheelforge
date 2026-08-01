from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from functools import partial
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import ProxyHandler, build_opener

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = ROOT / "test-fixtures" / "index"
BUILD_PROGRAM = ROOT / "test-fixtures" / "build_fixtures.py"
PROJECT_ROOT = ROOT / "test-fixtures" / "projects"
MARKER_NAME = ".wheelforge-fixture-index.json"
MARKER_CONTENT = b'{"owner":"wheelforge-fixture-builder","schema":1}\n'
HTTP_TIMEOUT_SECONDS = 5
BUILD_TIMEOUT_SECONDS = 30
NO_PROXY_OPENER = build_opener(ProxyHandler({}))

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


class _SentinelProxyHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self) -> None:
        type(self).requests += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"sentinel proxy")

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
    with NO_PROXY_OPENER.open(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.status, response.read().decode("utf-8")


def _get_bytes(url: str) -> bytes:
    with NO_PROXY_OPENER.open(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read()


def _run_builder(output_root: Path, python: str = sys.executable) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    return subprocess.run(
        [python, str(BUILD_PROGRAM), str(output_root)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
    )


def _build_index(output_root: Path, python: str = sys.executable) -> None:
    result = _run_builder(output_root, python)
    assert result.returncode == 0, result.stderr


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


def _second_supported_python() -> str | None:
    configured = os.environ.get("WF_FIXTURE_SECOND_PYTHON")
    candidates = (
        [configured]
        if configured
        else [
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.9",
            "python3",
            "python",
        ]
    )
    current = os.path.normcase(os.path.realpath(sys.executable))
    for candidate in candidates:
        if not candidate:
            continue
        executable = shutil.which(candidate)
        if executable is None or os.path.normcase(os.path.realpath(executable)) == current:
            continue
        version = subprocess.run(
            [executable, "-c", "import sys; print(int(sys.version_info >= (3, 9)))"],
            check=False,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        if version.returncode == 0 and version.stdout.strip() == "1":
            return executable
    return None


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
        server.server_close()
        thread.join(timeout=HTTP_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "fixture HTTP server did not stop"


def test_fixture_index_exposes_only_wheels(generated_index: Path) -> None:
    with _serve_index(generated_index) as base_url:
        status, page = _get_text(f"{base_url}/simple/demo-common/")

    assert status == 200
    assert ".whl" in page
    assert ".tar.gz" not in page


def test_builder_rejects_output_symlink_without_touching_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    output_link = tmp_path / "index"
    try:
        output_link.symlink_to(victim, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    result = _run_builder(output_link)

    assert result.returncode != 0
    assert output_link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_builder_rejects_non_immediate_symlink_ancestor_without_touching_target(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    output_root = victim / "nested" / "index"
    output_root.parent.mkdir(parents=True)
    _build_index(output_root)
    sentinel = output_root / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(victim, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    result = _run_builder(link / "nested" / "index")

    assert result.returncode != 0
    assert link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_builder_rejects_unowned_existing_directory(tmp_path: Path) -> None:
    output_root = tmp_path / "index"
    output_root.mkdir()
    sentinel = output_root / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    result = _run_builder(output_root)

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_builder_replaces_only_an_exact_owned_index(tmp_path: Path) -> None:
    output_root = tmp_path / "index"
    _build_index(output_root)
    assert (output_root / MARKER_NAME).read_bytes() == MARKER_CONTENT
    stale = output_root / "stale.txt"
    stale.write_text("remove me", encoding="utf-8")

    _build_index(output_root)

    assert not stale.exists()
    assert (output_root / MARKER_NAME).read_bytes() == MARKER_CONTENT
    assert list(tmp_path.glob(".index.build-*")) == []
    assert list(tmp_path.glob(".index.previous-*")) == []


def test_builder_rejects_modified_marker_without_touching_directory(tmp_path: Path) -> None:
    output_root = tmp_path / "index"
    _build_index(output_root)
    (output_root / MARKER_NAME).write_text(
        '{"owner":"wheelforge-fixture-builder","schema":2}\n', encoding="utf-8"
    )
    sentinel = output_root / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    result = _run_builder(output_root)

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("protected", [Path.home(), ROOT, Path(ROOT.anchor)])
def test_builder_rejects_protected_output_roots(protected: Path) -> None:
    result = _run_builder(protected)

    assert result.returncode != 0


def test_fixture_http_never_uses_ambient_proxy(
    generated_index: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _SentinelProxyHandler.requests = 0
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _SentinelProxyHandler)
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    proxy_host, proxy_port = proxy.server_address
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        with _serve_index(generated_index) as base_url:
            status, page = _get_text(f"{base_url}/simple/demo-common/")
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=HTTP_TIMEOUT_SECONDS)
        assert not proxy_thread.is_alive(), "sentinel proxy did not stop"

    assert status == 200
    assert ".whl" in page
    assert _SentinelProxyHandler.requests == 0


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
                wheel = _get_bytes(urljoin(project_url, link))
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


def test_all_wheel_members_use_reproducible_stored_encoding(generated_index: Path) -> None:
    for wheel_path in (generated_index / "packages").iterdir():
        with zipfile.ZipFile(wheel_path) as archive:
            assert {member.compress_type for member in archive.infolist()} == {zipfile.ZIP_STORED}


def test_real_pip_selects_cp311_arm64_and_transitive_wheels(
    generated_index: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "downloads"
    destination.mkdir()
    environment = dict(os.environ)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "PIP_PROXY",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
        }
    )

    with _serve_index(generated_index) as base_url:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "download",
                "--dest",
                str(destination),
                "--index-url",
                f"{base_url}/simple/",
                "--only-binary=:all:",
                "--platform=manylinux2014_aarch64",
                "--python-version=3.11",
                "--implementation=cp",
                "--abi=cp311",
                "demo-direct==1.0.0",
                "demo-native==1.0.0",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in destination.iterdir()} == {
        "demo_common-1.2.3-py3-none-any.whl",
        "demo_direct-1.0.0-py3-none-any.whl",
        "demo_native-1.0.0-cp311-cp311-manylinux2014_aarch64.whl",
    }


def test_specs_are_the_only_fixture_package_source() -> None:
    assert not (ROOT / "test-fixtures" / "packages").exists()


def test_fixture_build_is_byte_reproducible_with_same_python(tmp_path: Path) -> None:
    first_index = tmp_path / "first"
    second_index = tmp_path / "second"
    _build_index(first_index)
    _build_index(second_index)

    assert _tree_fingerprint(first_index) == _tree_fingerprint(second_index)


def test_fixture_build_matches_an_optional_second_python(tmp_path: Path) -> None:
    second_python = _second_supported_python()
    if second_python is None:
        pytest.skip("no second supported Python interpreter is available")
    primary_index = tmp_path / "primary-python"
    second_index = tmp_path / "second-python"
    _build_index(primary_index)
    _build_index(second_index, second_python)

    assert _tree_fingerprint(primary_index) == _tree_fingerprint(second_index)


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
