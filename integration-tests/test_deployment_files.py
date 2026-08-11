from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def test_native_deployment_artifacts_are_complete() -> None:
    expected = (
        DEPLOY / ".env.example",
        DEPLOY / "wheelforge-api.service",
        DEPLOY / "wheelforge-worker.service",
        DEPLOY / "smoke.sh",
        ROOT / "scripts" / "bootstrap-target-profiles.sh",
        ROOT / "docs" / "operations.md",
        ROOT / "docs" / "v1-acceptance.md",
    )

    assert all(path.is_file() for path in expected)


def test_services_share_a_hardened_non_root_identity_and_environment() -> None:
    units = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            DEPLOY / "wheelforge-api.service",
            DEPLOY / "wheelforge-worker.service",
        )
    }
    for name, unit in units.items():
        assert "User=wheelforge" in unit, name
        assert "Group=wheelforge" in unit, name
        assert "EnvironmentFile=/etc/wheelforge/wheelforge.env" in unit, name
        assert "After=" in unit and "mysql.service" in unit, name
        assert "Restart=on-failure" in unit, name
        assert "NoNewPrivileges=true" in unit, name
        assert "PrivateTmp=true" in unit, name
        assert "ProtectSystem=strict" in unit, name
        assert "ProtectHome=true" in unit, name
        assert "UMask=0077" in unit, name
        assert "CapabilityBoundingSet=" in unit, name
        assert "ReadWritePaths=/var/lib/wheelforge" in unit, name

    assert "java" in units["wheelforge-api.service"]
    assert "wheelforge-api.jar" in units["wheelforge-api.service"]
    assert "python" in units["wheelforge-worker.service"]
    assert "-m wheelforge_worker" in units["wheelforge-worker.service"]


def test_deployment_environment_has_placeholders_not_live_secrets() -> None:
    content = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    required = {
        "WF_JDBC_URL",
        "WF_DATABASE_URL",
        "WF_DATABASE_USER",
        "WF_DATABASE_PASSWORD",
        "WF_AUTH_TOKEN_SECRET",
        "WF_DATA_ROOT",
        "WF_WORKSPACE_ROOT",
    }
    keys = {
        line.split("=", 1)[0]
        for line in content.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert required <= keys
    assert "replace-with-" in content
    assert "wheelforge_local" not in content
    assert "/var/lib/wheelforge/data" in content
    assert "/var/lib/wheelforge/work" in content


def test_shell_entrypoints_are_syntax_checked_executable_and_do_not_leak_passwords() -> None:
    scripts = (
        DEPLOY / "smoke.sh",
        ROOT / "scripts" / "bootstrap-target-profiles.sh",
    )
    for script in scripts:
        assert os.access(script, os.X_OK), script
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        content = script.read_text(encoding="utf-8")
        assert "--password=" not in content


def test_target_bootstrap_covers_the_v1_matrix_idempotently() -> None:
    content = (ROOT / "scripts" / "bootstrap-target-profiles.sh").read_text(
        encoding="utf-8"
    )
    profile_codes = set(
        re.findall(
            r"'(linux-(?:x86_64|arm64)-cp(?:39|310|311|312|313)-manylinux2014|windows-(?:x64|arm64)-cp(?:39|310|311|312|313))'",
            content,
        )
    )

    assert len(profile_codes) == 20
    assert "ON DUPLICATE KEY UPDATE" in content
    assert "validation_type" in content and "STATIC" in content
    assert "wheel-tags-v1" in content


def test_operations_and_acceptance_docs_cover_release_duties() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    acceptance = (ROOT / "docs" / "v1-acceptance.md").read_text(encoding="utf-8")
    required_operations = (
        "MySQL 8.4",
        "Java 21",
        "Python 3.12",
        "backup",
        "restore",
        "credential rotation",
        "Artifact retention",
        "expired lease",
        "cancellation cleanup",
        "journalctl",
        "static validation",
        "Docker",
        "Redis",
        "MinIO",
    )

    for phrase in required_operations:
        assert phrase.lower() in operations.lower(), phrase
    assert "does not install or execute dependency code" in operations
    assert "does not prove installation on the target machine" in operations
    assert "| Requirement | Evidence |" in acceptance
    assert "TODO" not in acceptance
    assert acceptance.count("| `") >= 20
