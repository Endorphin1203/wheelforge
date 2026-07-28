create index ix_artifact_retention on artifacts(cleaned_at, expires_at, id);
create index ix_artifact_task_created on artifacts(build_task_id, created_at, id);
create index ix_download_active_lease
  on download_records(artifact_id, completed, downloaded_at);
