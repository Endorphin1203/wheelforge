from __future__ import annotations

import os
from urllib.parse import urlsplit

import wheelforge_worker.download.wheels as wheels_module
import wheelforge_worker.jobs.pipeline as pipeline_module
import wheelforge_worker.resolver.pip_report as pip_report_module
from wheelforge_worker.sources import PackageSource


def _fixture_url() -> str:
    value = os.environ.pop("WF_TEST_FIXTURE_INDEX_URL", "")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/simple")
    ):
        raise ValueError("WF_TEST_FIXTURE_INDEX_URL must be a local /simple HTTP URL")
    return value.rstrip("/")


def main() -> int:
    fixture = _fixture_url()
    pip_report_module.resolver_source_url = lambda _source: fixture
    wheels_module.source_url = lambda _source: fixture
    metadata_root = fixture.removesuffix("/simple") + "/pypi"
    pipeline_module._INDEX_JSON_BASES = {
        source: metadata_root for source in PackageSource
    }

    from wheelforge_worker.__main__ import main as worker_main

    return worker_main()


if __name__ == "__main__":
    raise SystemExit(main())
