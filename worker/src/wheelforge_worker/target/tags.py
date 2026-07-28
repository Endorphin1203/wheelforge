from __future__ import annotations

from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from wheelforge_worker.target.models import TargetProfile

_LINUX_ARCHITECTURES = {
    "X86_64": "x86_64",
    "AARCH64": "aarch64",
}


def wheel_is_compatible(filename: str, profile: TargetProfile) -> bool:
    try:
        _, _, _, wheel_tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return False
    return any(tag in compatible_tags(profile) for tag in wheel_tags)


def compatible_tags(profile: TargetProfile) -> frozenset[Tag]:
    tags: set[Tag] = set()
    platform_tags = compatible_platform_tags(profile)
    abi3_interpreters = compatible_abi3_interpreters(profile)

    for platform_tag in platform_tags:
        tags.add(Tag(profile.cpython_tag, profile.cpython_tag, platform_tag))
        tags.add(Tag(profile.cpython_tag, "abi3", platform_tag))
        tags.add(Tag(profile.cpython_tag, "none", platform_tag))
        tags.add(Tag(profile.python_tag, "none", platform_tag))
        tags.add(Tag("py3", "none", platform_tag))
        for interpreter in abi3_interpreters:
            tags.add(Tag(interpreter, "abi3", platform_tag))

    for interpreter in (profile.python_tag, "py3"):
        tags.add(Tag(interpreter, "none", "any"))

    return frozenset(tags)


def compatible_platform_tags(profile: TargetProfile) -> tuple[str, ...]:
    if profile.os != "LINUX":
        return (profile.platform_tag,)

    architecture = _LINUX_ARCHITECTURES[profile.architecture]
    platform_tags = [profile.platform_tag, f"manylinux_2_17_{architecture}"]
    for minor in range(16, -1, -1):
        platform_tags.append(f"manylinux_2_{minor}_{architecture}")
    if architecture == "x86_64":
        platform_tags.extend(["manylinux2010_x86_64", "manylinux1_x86_64"])
    return tuple(dict.fromkeys(platform_tags))


def compatible_abi3_interpreters(profile: TargetProfile) -> tuple[str, ...]:
    target_minor = int(profile.python_version.split(".")[1])
    return tuple(f"cp3{minor}" for minor in range(9, target_minor + 1))
