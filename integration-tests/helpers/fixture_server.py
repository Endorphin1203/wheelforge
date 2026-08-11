from __future__ import annotations

import argparse
import json
import mimetypes
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)


_SOURCES = frozenset({"tsinghua", "aliyun", "pypi"})


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], index_root: Path, delay_seconds: float
    ) -> None:
        super().__init__(address, FixtureHandler)
        self.index_root = _plain_directory(index_root)
        self.delay_seconds = delay_seconds
        self.releases = _release_metadata(self.index_root / "packages")
        self.package_requests: dict[str, int] = defaultdict(int)


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureServer

    def do_GET(self) -> None:
        self._serve(include_body=True)

    def do_HEAD(self) -> None:
        self._serve(include_body=False)

    def _serve(self, *, include_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self.send_error(400)
            return
        try:
            decoded = unquote(parsed.path, errors="strict")
        except UnicodeError:
            self.send_error(400)
            return
        parts = decoded.strip("/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            self.send_error(400)
            return
        if len(parts) < 2 or parts[0] not in _SOURCES:
            self.send_error(404)
            return
        source, route, *tail = parts
        if route == "pypi":
            self._metadata(source, tail, include_body)
            return
        if route == "simple":
            self._file(self.server.index_root / "simple", tail, include_body)
            return
        if route == "packages" and len(tail) == 1:
            self._package(source, tail[0], include_body)
            return
        self.send_error(404)

    def _metadata(self, source: str, tail: list[str], include_body: bool) -> None:
        if len(tail) != 2 or tail[1] != "json":
            self.send_error(404)
            return
        if source == "tsinghua":
            self.send_error(503)
            return
        package = canonicalize_name(tail[0])
        releases = self.server.releases.get(package)
        if releases is None:
            self.send_error(404)
            return
        self._bytes(
            json.dumps({"releases": releases}, separators=(",", ":")).encode(),
            "application/json",
            include_body,
        )

    def _package(self, source: str, filename: str, include_body: bool) -> None:
        del source
        if include_body:
            self.server.package_requests[filename] += 1
            if (
                filename.startswith("partial_demo-")
                and self.server.package_requests[filename] > 1
            ):
                self.send_error(503)
                return
            if (
                filename.startswith("slow_demo-")
                and self.server.package_requests[filename] > 1
            ):
                time.sleep(self.server.delay_seconds)
        self._file(self.server.index_root / "packages", [filename], include_body)

    def _file(self, root: Path, tail: list[str], include_body: bool) -> None:
        if any("\\" in part or not part for part in tail):
            self.send_error(400)
            return
        path = root.joinpath(*tail)
        if path.is_dir():
            path = path / "index.html"
        try:
            if path.parent != root and root not in path.parents:
                raise ValueError
            content = path.read_bytes()
        except (OSError, ValueError):
            self.send_error(404)
            return
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._bytes(content, media_type, include_body)

    def _bytes(self, content: bytes, media_type: str, include_body: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format: str, *args: object) -> None:
        pass


def create_server(
    index_root: Path, host: str, port: int, *, delay_seconds: float = 2.0
) -> FixtureServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("fixture server must bind to loopback")
    if not 0 <= port <= 65535:
        raise ValueError("fixture server port is invalid")
    if not 0 <= delay_seconds <= 30:
        raise ValueError("fixture delay is invalid")
    return FixtureServer((host, port), index_root, delay_seconds)


def _release_metadata(package_root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    releases: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for wheel in sorted(package_root.glob("*.whl")):
        try:
            name, version, _build, _tags = parse_wheel_filename(wheel.name)
        except InvalidWheelFilename:
            continue
        releases[canonicalize_name(name)][str(version)].append(
            {
                "filename": wheel.name,
                "packagetype": "bdist_wheel",
                "requires_python": ">=3.9",
                "yanked": False,
            }
        )
    return {name: dict(versions) for name, versions in releases.items()}


def _plain_directory(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.is_symlink() or not absolute.is_dir():
        raise ValueError("fixture index root must be a plain directory")
    return absolute


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("index_root", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    server = create_server(
        arguments.index_root,
        arguments.host,
        arguments.port,
        delay_seconds=arguments.delay_seconds,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
