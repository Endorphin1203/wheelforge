from __future__ import annotations

from wheelforge_worker.target.models import TargetProfile


_MARKER_MATRIX = {
    ("LINUX", "X86_64"): {
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_system": "Linux",
        "sys_platform": "linux",
    },
    ("LINUX", "AARCH64"): {
        "os_name": "posix",
        "platform_machine": "aarch64",
        "platform_system": "Linux",
        "sys_platform": "linux",
    },
    ("WINDOWS", "AMD64"): {
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_system": "Windows",
        "sys_platform": "win32",
    },
    ("WINDOWS", "ARM64"): {
        "os_name": "nt",
        "platform_machine": "ARM64",
        "platform_system": "Windows",
        "sys_platform": "win32",
    },
}


def marker_environment(profile: TargetProfile) -> dict[str, str]:
    target_values = _MARKER_MATRIX[(profile.os, profile.architecture)]
    return {
        "implementation_name": "cpython",
        "implementation_version": profile.python_full_version,
        "os_name": target_values["os_name"],
        "platform_machine": target_values["platform_machine"],
        "platform_python_implementation": "CPython",
        "platform_release": "0",
        "platform_system": target_values["platform_system"],
        "platform_version": "0",
        "python_full_version": profile.python_full_version,
        "python_version": profile.python_version,
        "sys_platform": target_values["sys_platform"],
    }
