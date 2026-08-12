drop index ix_job_ready on build_jobs;

create index ix_job_ready on build_jobs
  (status, priority_no, created_at, id, available_at);
