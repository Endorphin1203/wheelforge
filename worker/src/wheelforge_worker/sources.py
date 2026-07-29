from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class PackageSource(StrEnum):
    TSINGHUA = "TSINGHUA"
    ALIYUN = "ALIYUN"
    PYPI = "PYPI"


SOURCE_ORDER: tuple[PackageSource, ...] = (
    PackageSource.TSINGHUA,
    PackageSource.ALIYUN,
    PackageSource.PYPI,
)
SOURCE_URLS = MappingProxyType(
    {
        PackageSource.TSINGHUA: "https://pypi.tuna.tsinghua.edu.cn/simple",
        PackageSource.ALIYUN: "https://mirrors.aliyun.com/pypi/simple",
        PackageSource.PYPI: "https://pypi.org/simple",
    }
)
_URL_TO_SOURCE = MappingProxyType({url: source for source, url in SOURCE_URLS.items()})


def source_url(source: PackageSource) -> str:
    """Return the server-owned URL for an explicitly selected source code."""
    if not isinstance(source, PackageSource):
        raise ValueError("source must be a builtin package source code")
    return SOURCE_URLS[source]


def resolver_source_url(source: str | PackageSource) -> str:
    """Preserve the strict resolver's legacy acceptance of exact builtin URLs."""
    if isinstance(source, PackageSource):
        return source_url(source)
    if not isinstance(source, str):
        raise ValueError("source must be a builtin source code or URL")
    try:
        return source_url(PackageSource(source))
    except ValueError:
        pass
    if source in _URL_TO_SOURCE:
        return source
    raise ValueError("source is not a builtin package index")
