from __future__ import annotations

from sqlalchemy import create_engine, insert

from wheelforge_worker.jobs.repository import (
    BuildResourceLimits,
    JobRepository,
    metadata,
    system_config,
)


def test_worker_reads_one_bounded_resource_snapshot_per_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    configured = {
        "maxPackageCount": 7,
        "maxPackageSizeBytes": 1024,
        "maxArtifactSizeBytes": 4096,
        "taskTimeoutSeconds": 90,
        "maxCandidatesPerRequirement": 3,
        "maxResolutionAttempts": 5,
        "maxArchiveEntries": 20,
        "maxArchiveExpansionRatio": 4,
        "artifactRetentionDays": 2,
    }
    with engine.begin() as connection:
        connection.execute(
            insert(system_config),
            [
                {
                    "config_key": key,
                    "config_value": value,
                    "description": key,
                    "version_no": 0,
                }
                for key, value in configured.items()
            ],
        )

    limits = JobRepository(engine, lease_seconds=30).resource_limits()

    assert limits == BuildResourceLimits(
        max_packages=7,
        max_wheel_bytes=1024,
        max_total_bytes=4096,
        task_timeout_seconds=90,
        max_candidates_per_requirement=3,
        max_resolution_attempts=5,
        max_archive_entries=20,
        max_archive_expansion_ratio=4,
        artifact_retention_days=2,
    )


def test_worker_resource_defaults_match_seeded_v1_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)

    assert (
        JobRepository(engine, lease_seconds=30).resource_limits()
        == BuildResourceLimits()
    )
