from __future__ import annotations

import os
from urllib.parse import urlsplit

import wheelforge_worker.download.wheels as wheels_module
import wheelforge_worker.jobs.pipeline as pipeline_module
import wheelforge_worker.resolver.pip_report as pip_report_module
from wheelforge_worker.sources import PackageSource


_SLUGS = {
    PackageSource.TSINGHUA: "tsinghua",
    PackageSource.ALIYUN: "aliyun",
    PackageSource.PYPI: "pypi",
}


def _fixture_base_url() -> str:
    value = os.environ.pop("WF_TEST_FIXTURE_BASE_URL", "")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/")
    ):
        raise ValueError("WF_TEST_FIXTURE_BASE_URL must be a local root HTTP URL")
    return value.rstrip("/")


def main() -> int:
    fixture = _fixture_base_url()

    def simple_url(source: str | PackageSource) -> str:
        identity = PackageSource(source)
        return f"{fixture}/{_SLUGS[identity]}/simple"

    pip_report_module.resolver_source_url = simple_url
    wheels_module.source_url = simple_url
    pipeline_module._INDEX_JSON_BASES = {
        source: f"{fixture}/{_SLUGS[source]}/pypi" for source in PackageSource
    }

    from wheelforge_worker.__main__ import main as worker_main

    return worker_main()


if __name__ == "__main__":
    raise SystemExit(main())
