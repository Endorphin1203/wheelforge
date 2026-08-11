from pathlib import Path
from threading import Event

import pytest

from wheelforge_worker.__main__ import create_consumer
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
    assert settings.maintenance_age_seconds == 86400
    assert settings.worker_id == "wheelforge-worker-1"
    assert settings.allow_portable_workspace is False


def test_settings_parse_explicit_portable_workspace_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_ALLOW_PORTABLE_WORKSPACE", "true")

    settings = Settings.from_env()

    assert settings.allow_portable_workspace is True


def test_settings_reject_invalid_portable_workspace_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_ALLOW_PORTABLE_WORKSPACE", "sometimes")

    with pytest.raises(ValueError, match="WF_ALLOW_PORTABLE_WORKSPACE must be true or false"):
        Settings.from_env()


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


def test_settings_reject_database_url_without_host(monkeypatch, tmp_path: Path) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_DATABASE_URL", "mysql+pymysql:///wheelforge")

    with pytest.raises(ValueError, match="host"):
        Settings.from_env()


def test_settings_reject_database_url_without_database_name(
    monkeypatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_DATABASE_URL", "mysql+pymysql://wf:wf@localhost:3306")

    with pytest.raises(ValueError, match="database name"):
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


def test_settings_reject_maintenance_age_under_one_hour(
    monkeypatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("WF_MAINTENANCE_AGE_SECONDS", "3599")

    with pytest.raises(ValueError, match="at least 3600 seconds"):
        Settings.from_env()


def test_settings_preserves_configured_root_symlink_for_adapter_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    target = tmp_path / "real-data"
    target.mkdir()
    configured = tmp_path / "data-link"
    configured.symlink_to(target, target_is_directory=True)
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("WF_DATA_ROOT", str(configured))

    settings = Settings.from_env()

    assert settings.data_root == configured
    assert settings.data_root.is_symlink()
    with pytest.raises(ValueError, match="data root must not traverse links"):
        create_consumer(settings, Event())


def test_create_consumer_rejects_root_beneath_linked_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, tmp_path)
    real_parent = tmp_path / "real-parent"
    data = real_parent / "data"
    data.mkdir(parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("WF_DATA_ROOT", str(linked_parent / "data"))

    settings = Settings.from_env()

    assert settings.data_root == linked_parent / "data"
    with pytest.raises(ValueError, match="data root must not traverse links"):
        create_consumer(settings, Event())
