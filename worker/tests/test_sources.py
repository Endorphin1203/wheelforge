from __future__ import annotations

import subprocess
import sys


def test_central_source_registry_imports_first_in_a_fresh_interpreter() -> None:
    script = """
import wheelforge_worker.sources as sources
from wheelforge_worker.resolver import PackageSource

assert sources.PackageSource is PackageSource
assert tuple(sources.SOURCE_ORDER) == (
    PackageSource.TSINGHUA,
    PackageSource.ALIYUN,
    PackageSource.PYPI,
)
assert tuple(sources.SOURCE_URLS) == sources.SOURCE_ORDER
try:
    sources.SOURCE_URLS[PackageSource.PYPI] = "https://bad.test/simple"
except TypeError:
    pass
else:
    raise AssertionError("source registry must be immutable")
print("ok")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
