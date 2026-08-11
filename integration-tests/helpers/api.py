from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


JsonObject = dict[str, Any]
T = TypeVar("T")
_TERMINAL = frozenset({"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"})


class ApiError(RuntimeError):
    """The local integration API returned an invalid or unsuccessful response."""


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        poll_interval: float = 0.1,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("integration API URL must use local plain HTTP")
        if parsed.query or parsed.fragment or not parsed.netloc:
            raise ValueError("integration API URL is invalid")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeouts must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._opener = build_opener(ProxyHandler({}))

    def login(self, username: str, password: str) -> JsonObject:
        response = self._json(
            "POST", "/api/auth/login", {"username": username, "password": password}
        )
        token = response.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ApiError("login response does not contain an access token")
        self.token = token
        return response

    def upload_requirements(self, path: Path) -> str:
        content = Path(path).read_bytes()
        boundary = f"wheelforge-{secrets.token_hex(16)}"
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="requirements.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        response = self._json_bytes(
            "POST",
            "/api/requirement-files",
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        return _required_string(response, "id")

    def requirement_file(self, file_id: str) -> JsonObject:
        return self._json("GET", f"/api/requirement-files/{file_id}")

    def requirement_items(self, file_id: str) -> list[JsonObject]:
        return _object_list(
            self._json_value("GET", f"/api/requirement-files/{file_id}/items")
        )

    def wait_for_parse(self, file_id: str, expected: str = "PARSED") -> JsonObject:
        return self._wait(
            lambda: self.requirement_file(file_id),
            lambda result: result.get("parseStatus") == expected,
            lambda result: str(result.get("parseStatus")),
        )

    def target_profiles(self) -> list[JsonObject]:
        return _object_list(self._json_value("GET", "/api/target-profiles"))

    def target_profile(self, code: str) -> JsonObject:
        matches = [
            profile for profile in self.target_profiles() if profile.get("code") == code
        ]
        if len(matches) != 1:
            raise ApiError(f"expected one enabled target profile named {code!r}")
        return matches[0]

    def create_build(
        self,
        requirement_file_id: str,
        profile_code: str,
        solve_mode: str = "COMPATIBLE",
    ) -> str:
        profile_id = _required_string(self.target_profile(profile_code), "id")
        response = self._json(
            "POST",
            "/api/build-tasks",
            {
                "requirementFileId": requirement_file_id,
                "targetProfileId": profile_id,
                "solveMode": solve_mode,
            },
        )
        return _required_string(response, "id")

    def build_task(self, task_id: str) -> JsonObject:
        return self._json("GET", f"/api/build-tasks/{task_id}")

    def cancel_build(self, task_id: str) -> JsonObject:
        return self._json("POST", f"/api/build-tasks/{task_id}/cancel")

    def retry_build(self, task_id: str) -> JsonObject:
        return self._json("POST", f"/api/build-tasks/{task_id}/retry")

    def wait_for_terminal(self, task_id: str) -> JsonObject:
        return self._wait(
            lambda: self.build_task(task_id),
            lambda result: result.get("status") in _TERMINAL,
            lambda result: str(result.get("status")),
        )

    def wait_for_stage(self, task_id: str, stage: str) -> JsonObject:
        return self._wait(
            lambda: self.build_task(task_id),
            lambda result: result.get("status") == stage,
            lambda result: str(result.get("status")),
        )

    def logs(self, task_id: str, after_sequence: int = 0) -> list[JsonObject]:
        query = urlencode({"afterSequence": after_sequence})
        return _object_list(
            self._json_value("GET", f"/api/build-tasks/{task_id}/logs?{query}")
        )

    def resolved_packages(self, task_id: str) -> list[JsonObject]:
        return _object_list(
            self._json_value("GET", f"/api/build-tasks/{task_id}/resolved-packages")
        )

    def version_comparison(self, task_id: str) -> list[JsonObject]:
        return _object_list(
            self._json_value("GET", f"/api/build-tasks/{task_id}/version-comparison")
        )

    def artifacts(self) -> list[JsonObject]:
        return _object_list(self._json_value("GET", "/api/artifacts?limit=100"))

    def artifact_for_task(self, task_id: str) -> JsonObject:
        matches = [
            item for item in self.artifacts() if item.get("buildTaskId") == task_id
        ]
        if len(matches) != 1:
            raise ApiError(f"expected one artifact for build task {task_id}")
        return matches[0]

    def download_artifact(self, artifact_id: str) -> bytes:
        return self._request(
            "GET", f"/api/artifacts/{artifact_id}/download", None, None
        )

    def _wait(
        self,
        operation: Callable[[], T],
        complete: Callable[[T], bool],
        describe: Callable[[T], str],
    ) -> T:
        deadline = time.monotonic() + self.timeout
        last = operation()
        while not complete(last):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"API poll timed out; last status was {describe(last)}"
                )
            time.sleep(min(self.poll_interval, remaining))
            last = operation()
        return last

    def _json(
        self, method: str, path: str, body: JsonObject | None = None
    ) -> JsonObject:
        value = self._json_value(method, path, body)
        if not isinstance(value, dict):
            raise ApiError("API response must contain a JSON object")
        return value

    def _json_value(
        self, method: str, path: str, body: JsonObject | None = None
    ) -> Any:
        encoded = (
            None if body is None else json.dumps(body, separators=(",", ":")).encode()
        )
        raw = self._request(
            method, path, encoded, "application/json" if encoded else None
        )
        return _decode_json(raw)

    def _json_bytes(
        self, method: str, path: str, body: bytes, content_type: str
    ) -> JsonObject:
        value = _decode_json(self._request(method, path, body, content_type))
        if not isinstance(value, dict):
            raise ApiError("API response must contain a JSON object")
        return value

    def _request(
        self, method: str, path: str, body: bytes | None, content_type: str | None
    ) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be absolute and local")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self._opener.open(
                request, timeout=min(self.timeout, 30.0)
            ) as response:
                return response.read()
        except HTTPError as error:
            content = error.read(64 * 1024)
            code = f"HTTP_{error.code}"
            try:
                payload = _decode_json(content)
                if isinstance(payload, dict) and isinstance(payload.get("code"), str):
                    code = payload["code"]
            except ApiError:
                pass
            raise ApiError(f"API request failed: {code}") from error
        except (OSError, URLError) as error:
            raise ApiError("local API request failed") from error


def _decode_json(content: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise ApiError(f"API response contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ApiError(f"API response contains non-standard number: {value}")
            ),
        )
    except ApiError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApiError("API response is not valid JSON") from error


def _object_list(value: Any) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ApiError("API response must contain a list of objects")
    return value


def _required_string(value: JsonObject, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ApiError(f"API response is missing string field {key}")
    return item
