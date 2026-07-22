from pathlib import Path

import pytest

from wheelforge_worker.settings import Settings


def configure_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "WF_DATABASE_URL", "mysql+pymysql://wf:wf@localhost:3306/wheelforge"
    )
    monkeypatch.setenv("WF_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("WF_WORKSPACE_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("WF_QUEUE_POLL_SECONDS", "2")
    monkeypatch.setenv("WF_JOB_LEASE_SECONDS", "60")
    monkeypatch.setenv("WF_WORKER_ID", "test-worker")


def test_settings_require_absolute_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "WF_DATABASE_URL", "mysql+pymysql://wf:wf@localhost:3306/wheelforge"
    )
    monkeypatch.setenv("WF_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("WF_WORKSPACE_ROOT", str(tmp_path / "work"))

    settings = Settings.from_env()

    assert settings.data_root == tmp_path / "data"
    assert settings.workspace_root == tmp_path / "work"
    assert settings.database_url.startswith("mysql+pymysql://")
    assert settings.queue_poll_seconds == 2
    assert settings.job_lease_seconds == 60
    assert settings.worker_id == "wheelforge-worker-1"


def test_settings_reject_relative_roots(monkeypatch, tmp_path: Path) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_DATA_ROOT", "relative-data")

    with pytest.raises(ValueError, match="WF_DATA_ROOT must be an absolute path"):
        Settings.from_env()


def test_settings_reject_non_mysql_pymysql_database_url(
    monkeypatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_DATABASE_URL", "postgresql://wf:wf@localhost/wheelforge")

    with pytest.raises(ValueError, match="mysql\\+pymysql"):
        Settings.from_env()


def test_settings_reject_equal_roots(monkeypatch, tmp_path: Path) -> None:
    configure_environment(monkeypatch, tmp_path)
    root = str(tmp_path / "shared")
    monkeypatch.setenv("WF_DATA_ROOT", root)
    monkeypatch.setenv("WF_WORKSPACE_ROOT", root)

    with pytest.raises(ValueError, match="must be different"):
        Settings.from_env()


def test_settings_normalize_roots_without_creating_them(
    monkeypatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    uncreated_root = tmp_path / "uncreated"
    monkeypatch.setenv("WF_DATA_ROOT", str(uncreated_root / "data" / ".." / "data"))
    monkeypatch.setenv("WF_WORKSPACE_ROOT", str(uncreated_root / "work"))

    settings = Settings.from_env()

    assert settings.data_root == uncreated_root / "data"
    assert settings.workspace_root == uncreated_root / "work"
    assert not uncreated_root.exists()


def test_settings_reject_roots_resolving_to_same_location(
    monkeypatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    uncreated_root = tmp_path / "uncreated"
    monkeypatch.setenv("WF_DATA_ROOT", str(uncreated_root / "data" / ".." / "shared"))
    monkeypatch.setenv("WF_WORKSPACE_ROOT", str(uncreated_root / "shared"))

    with pytest.raises(ValueError, match="must be different"):
        Settings.from_env()

    assert not uncreated_root.exists()


def test_settings_reject_non_positive_polling(monkeypatch, tmp_path: Path) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_QUEUE_POLL_SECONDS", "0")

    with pytest.raises(ValueError, match="WF_QUEUE_POLL_SECONDS must be positive"):
        Settings.from_env()


def test_settings_reject_short_leases(monkeypatch, tmp_path: Path) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_JOB_LEASE_SECONDS", "29")

    with pytest.raises(
        ValueError, match="WF_JOB_LEASE_SECONDS must be at least 30 seconds"
    ):
        Settings.from_env()
