insert into package_sources (
  id, code, display_name, base_url, priority_no, enabled,
  timeout_seconds, failure_count, version_no, updated_at
) values
  ('10000000-0000-0000-0000-000000000001', 'TSINGHUA', 'Tsinghua PyPI',
   'https://pypi.tuna.tsinghua.edu.cn/simple', 10, true, 30, 0, 0, '2026-07-22 00:00:00.000000'),
  ('10000000-0000-0000-0000-000000000002', 'ALIYUN', 'Aliyun PyPI',
   'https://mirrors.aliyun.com/pypi/simple', 20, true, 30, 0, 0, '2026-07-22 00:00:00.000000'),
  ('10000000-0000-0000-0000-000000000003', 'PYPI', 'PyPI',
   'https://pypi.org/simple', 30, true, 30, 0, 0, '2026-07-22 00:00:00.000000')
on duplicate key update
  display_name = values(display_name),
  base_url = values(base_url);

insert into system_config (
  config_key, config_value, description, updated_by, updated_at, version_no
) values
  ('maxUploadSizeBytes', '524288', 'Maximum uploaded Requirements file size in bytes', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxRequirementLines', '2000', 'Maximum number of Requirements lines', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxPackageCount', '500', 'Maximum resolved package count per build', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxPackageSizeBytes', '536870912', 'Maximum size of one downloaded Wheel in bytes', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxArtifactSizeBytes', '2147483648', 'Maximum total Artifact size in bytes', null,
   '2026-07-22 00:00:00.000000', 0),
  ('minFreeDiskBytes', '5368709120', 'Minimum free disk space before accepting work', null,
   '2026-07-22 00:00:00.000000', 0),
  ('taskTimeoutSeconds', '3600', 'Maximum build execution time in seconds', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxConcurrentBuilds', '2', 'Maximum concurrent builds for one Worker', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxRetryAttempts', '3', 'Maximum retry attempts for a queued job', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxCandidatesPerRequirement', '20', 'Maximum compatible candidates per direct requirement',
   null, '2026-07-22 00:00:00.000000', 0),
  ('maxResolutionAttempts', '100', 'Maximum complete dependency resolution attempts', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxArchiveEntries', '10000', 'Maximum entries inspected in one Wheel archive', null,
   '2026-07-22 00:00:00.000000', 0),
  ('maxArchiveExpansionRatio', '100', 'Maximum allowed archive expansion ratio', null,
   '2026-07-22 00:00:00.000000', 0),
  ('artifactRetentionDays', '30', 'Artifact retention period in days', null,
   '2026-07-22 00:00:00.000000', 0),
  ('retentionEnabled', 'true', 'Whether automatic Artifact retention cleanup is enabled', null,
   '2026-07-22 00:00:00.000000', 0)
on duplicate key update
  config_key = values(config_key);
