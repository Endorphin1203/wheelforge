import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_root: Path
    workspace_root: Path
    queue_poll_seconds: int
    job_lease_seconds: int
    maintenance_age_seconds: int
    worker_id: str
    allow_portable_workspace: bool

    @classmethod
    def from_env(cls) -> "Settings":
        data_root = cls._absolute_path("WF_DATA_ROOT")
        workspace_root = cls._absolute_path("WF_WORKSPACE_ROOT")

        if data_root == workspace_root:
            raise ValueError("WF_DATA_ROOT and WF_WORKSPACE_ROOT must be different")

        queue_poll_seconds = cls._positive_integer("WF_QUEUE_POLL_SECONDS", "2")
        job_lease_seconds = cls._positive_integer("WF_JOB_LEASE_SECONDS", "60")
        if job_lease_seconds < 30:
            raise ValueError("WF_JOB_LEASE_SECONDS must be at least 30 seconds")
        maintenance_age_seconds = cls._positive_integer(
            "WF_MAINTENANCE_AGE_SECONDS", "86400"
        )
        if maintenance_age_seconds < 3600:
            raise ValueError(
                "WF_MAINTENANCE_AGE_SECONDS must be at least 3600 seconds"
            )

        return cls(
            database_url=cls._database_url(),
            data_root=data_root,
            workspace_root=workspace_root,
            queue_poll_seconds=queue_poll_seconds,
            job_lease_seconds=job_lease_seconds,
            maintenance_age_seconds=maintenance_age_seconds,
            worker_id=cls._required_value("WF_WORKER_ID", "wheelforge-worker-1"),
            allow_portable_workspace=cls._boolean(
                "WF_ALLOW_PORTABLE_WORKSPACE", "false"
            ),
        )

    @staticmethod
    def _required_value(name: str, default: str | None = None) -> str:
        value = os.getenv(name, default)
        if not value:
            raise ValueError(f"{name} is required")
        return value

    @classmethod
    def _database_url(cls) -> str:
        database_url = cls._required_value("WF_DATABASE_URL")
        parsed_url = urlparse(database_url)
        if parsed_url.scheme != "mysql+pymysql":
            raise ValueError("WF_DATABASE_URL must use the mysql+pymysql scheme")
        if not parsed_url.hostname:
            raise ValueError("WF_DATABASE_URL must include a host")
        if not parsed_url.path.strip("/"):
            raise ValueError("WF_DATABASE_URL must include a database name")
        return database_url

    @classmethod
    def _absolute_path(cls, name: str) -> Path:
        path = Path(cls._required_value(name))
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path")
        return Path(os.path.abspath(os.fspath(path)))

    @classmethod
    def _positive_integer(cls, name: str, default: str) -> int:
        value = cls._required_value(name, default)
        try:
            number = int(value)
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error

        if number <= 0:
            raise ValueError(f"{name} must be positive")
        return number

    @classmethod
    def _boolean(cls, name: str, default: str) -> bool:
        value = cls._required_value(name, default).lower()
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        return value == "true"
