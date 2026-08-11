# WheelForge V1 Acceptance Matrix

The release owner records the commit, host, date, and command output for every
operator check. Automated evidence uses repository test node IDs or commands.

| Requirement | Evidence |
| --- | --- |
| `requirements.txt` UTF-8, BOM, GBK, LF, and CRLF | `worker/tests/parser/test_requirements.py` |
| Supported requirement operators and extras | `worker/tests/parser/test_requirements.py` |
| Reject VCS, editable, URL, include, and local path syntax | `integration-tests/test_security_boundaries.py::test_unsafe_requirement_never_reaches_build_queue` |
| Duplicate and conflicting direct requirements | `worker/tests/parser/test_requirements.py` |
| 512 KiB upload boundary | `integration-tests/test_security_boundaries.py::test_upload_larger_than_512_kib_is_rejected` |
| 2,000 logical-line boundary | `integration-tests/test_security_boundaries.py::test_more_than_2000_requirement_lines_fail_parsing` |
| Linux x86_64 and ARM64 profiles | `integration-tests/test_deployment_files.py::test_target_bootstrap_covers_the_v1_matrix_idempotently` |
| Windows x64 and ARM64 profiles | `integration-tests/test_deployment_files.py::test_target_bootstrap_covers_the_v1_matrix_idempotently` |
| Python 3.9 through 3.13 profile constraint | `worker/tests/target/test_policy.py::test_target_profile_accepts_supported_target_matrix` |
| manylinux2014 and Windows platform tag policy | `worker/tests/target/test_policy.py` |
| Tsinghua, Aliyun, then PyPI fallback | `integration-tests/test_compatibility_results.py::test_compatibility_table_explains_up_down_and_unchanged` |
| Built-in source URL allowlist | `integration-tests/test_security_boundaries.py::test_builtin_package_source_url_is_immutable` |
| Direct and transitive dependency resolution | `worker/tests/resolver/test_pip_report.py` |
| Strict and compatible solve modes | `worker/tests/resolver/test_compatible.py` |
| Compatible upgrade, downgrade, and unchanged results | `integration-tests/test_compatibility_results.py::test_compatibility_table_explains_up_down_and_unchanged` |
| Wheel-only acquisition and source fallback | `worker/tests/download/test_wheels.py` |
| Python, ABI, OS, and CPU Wheel tag validation | `worker/tests/target/test_policy.py` |
| Unsafe ZIP paths and archive resource limits | `worker/tests/validation/test_archive.py` |
| Wheel METADATA, WHEEL, and RECORD hash validation | `worker/tests/validation/test_archive.py` |
| Validation never imports or executes dependency code | `worker/tests/validation/test_archive.py::test_validation_never_extracts_imports_or_invokes_subprocess` |
| Static dependency-closure validation | `worker/tests/validation/test_static.py` |
| ZIP README, original/resolved requirements, scripts, manifest, checksums, and packages | `worker/tests/artifact/test_builder.py` |
| Generated ZIP entry path safety | `integration-tests/test_security_boundaries.py::test_generated_artifact_contains_only_safe_paths` |
| Partial-success Artifact is visibly non-installable | `integration-tests/test_failure_and_cancel.py::test_download_failure_produces_explainable_partial_artifact` |
| Failed build has no Artifact and supports retry | `integration-tests/test_failure_and_cancel.py::test_unresolvable_build_fails_without_artifact_and_can_retry` |
| Running build cancellation and cleanup | `integration-tests/test_failure_and_cancel.py::test_running_download_can_be_cancelled_without_artifact` |
| Created through terminal task state transitions and logs | `worker/tests/jobs/test_pipeline.py` |
| Expired lease takeover and stale-owner fencing | `worker/tests/jobs/test_repository.py` and `worker/tests/jobs/test_mysql_integration.py` |
| Database-driven package, bytes, archive, timeout, and retention limits | `integration-tests/test_resource_limits.py::test_worker_reads_one_bounded_resource_snapshot_per_job` |
| Cross-user file, task, log, Artifact, and download isolation | `integration-tests/test_security_boundaries.py::test_non_admin_and_cross_user_resources_are_hidden` |
| Administrator-only user, source, and configuration operations | `backend/src/test/java/com/wheelforge/api/admin/AdminControllerTest.java` |
| Secret scrubbing and bounded child-process logs | `integration-tests/test_harness_safety.py::test_process_output_is_bounded_and_sanitized` |
| Artifact listing, streaming download, count, and retention | `backend/src/test/java/com/wheelforge/api/artifact/ArtifactControllerTest.java` and `ArtifactRetentionJobTest.java` |
| Native Linux ARM64 happy-path package | `integration-tests/test_linux_arm64_build.py::test_linux_arm64_cp311_build` |
| Native MySQL migration and repository behavior | `make verify-mysql` on a disposable MySQL database |
| Same non-root service identity and hardened filesystem access | `integration-tests/test_deployment_files.py::test_services_share_a_hardened_non_root_identity_and_environment` |
| Environment file is root-readable and contains no live defaults | `integration-tests/test_deployment_files.py::test_deployment_environment_has_placeholders_not_live_secrets`; operator checks `stat /etc/wheelforge/wheelforge.env` is `root:root 0600` |
| API, MySQL, data root, and workspace root are healthy | `deploy/smoke.sh` on the release host |
| systemd unit syntax and sandbox compatibility | Operator runs `systemd-analyze verify` and starts both units on the Linux release host |
| MySQL plus local Artifact backup and restore | Operator performs the documented restore drill and verifies one downloaded Artifact checksum |
| Static-validation wording on every target | Operator checks UI, README, manifest, and delivery notes state that no target installation was executed |
| Docker, Redis, and MinIO are absent from V1 runtime | `integration-tests/test_deployment_files.py::test_operations_and_acceptance_docs_cover_release_duties`; operator inspects installed service dependencies |

## Release Commands

```sh
make verify
WF_TEST_JDBC_URL='jdbc:mysql://127.0.0.1:3306/wheelforge_test?connectionTimeZone=UTC' \
WF_TEST_DATABASE_USER='wheelforge_test' \
WF_TEST_DATABASE_PASSWORD='test-password' \
WF_TEST_DATABASE_URL='mysql+pymysql://wheelforge_test:test-password@127.0.0.1:3306/wheelforge_test' \
WF_TEST_DATABASE_DISPOSABLE=1 \
make verify-mysql
worker/.venv/bin/pytest integration-tests -q
systemd-analyze verify deploy/wheelforge-api.service deploy/wheelforge-worker.service
set -a; . /etc/wheelforge/wheelforge.env; set +a; deploy/smoke.sh
```
