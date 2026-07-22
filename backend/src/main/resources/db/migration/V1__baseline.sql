create table users (
  id char(36) primary key,
  username varchar(100) not null unique,
  password_hash varchar(255) not null,
  role varchar(20) not null,
  status varchar(20) not null,
  created_at datetime(6) not null
) engine=InnoDB default charset=utf8mb4;

create table requirement_files (
  id char(36) primary key,
  user_id char(36) not null,
  original_name varchar(255) not null,
  detected_encoding varchar(20),
  size_bytes bigint not null,
  sha256 char(64) not null,
  original_object_key varchar(512) not null,
  normalized_object_key varchar(512),
  parse_status varchar(20) not null,
  parse_error varchar(2000),
  created_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_requirement_file_user foreign key (user_id) references users(id)
) engine=InnoDB default charset=utf8mb4;

create table target_profiles (
  id char(36) primary key,
  code varchar(100) not null unique,
  os varchar(20) not null,
  architecture varchar(20) not null,
  python_implementation varchar(20) not null,
  python_version varchar(10) not null,
  python_full_version varchar(20) not null,
  platform_tag varchar(100) not null,
  abi_tags json not null,
  validation_type varchar(20) not null,
  validation_policy_version varchar(50) not null,
  enabled boolean not null,
  version_no bigint not null default 0
) engine=InnoDB default charset=utf8mb4;

create table build_tasks (
  id char(36) primary key,
  user_id char(36) not null,
  requirement_file_id char(36) not null,
  target_profile_id char(36) not null,
  source_task_id char(36),
  execution_id char(36),
  status varchar(30) not null,
  progress int not null default 0,
  current_stage varchar(50),
  solve_mode varchar(20) not null,
  target_snapshot json not null,
  cancel_requested boolean not null default false,
  failure_code varchar(100),
  failure_message varchar(2000),
  created_at datetime(6) not null,
  started_at datetime(6),
  finished_at datetime(6),
  deleted_at datetime(6),
  version_no bigint not null default 0,
  constraint fk_build_user foreign key (user_id) references users(id),
  constraint fk_build_file foreign key (requirement_file_id) references requirement_files(id),
  constraint fk_build_profile foreign key (target_profile_id) references target_profiles(id)
) engine=InnoDB default charset=utf8mb4;

create table build_jobs (
  id char(36) primary key,
  job_type varchar(30) not null,
  payload_version int not null,
  subject_id char(36) not null,
  payload_json json not null,
  status varchar(20) not null,
  priority_no int not null default 100,
  available_at datetime(6) not null,
  attempts int not null default 0,
  max_attempts int not null default 3,
  lease_owner varchar(100),
  execution_id char(36),
  lease_expires_at datetime(6),
  heartbeat_at datetime(6),
  last_error varchar(2000),
  created_at datetime(6) not null,
  started_at datetime(6),
  finished_at datetime(6),
  version_no bigint not null default 0,
  index ix_job_ready (status, available_at, priority_no, created_at),
  index ix_job_lease (status, lease_expires_at)
) engine=InnoDB default charset=utf8mb4;

create table requirement_items (
  id char(36) primary key,
  requirement_file_id char(36) not null,
  line_no int not null,
  normalized_name varchar(255) not null,
  extras_json json not null,
  specifier varchar(500) not null,
  marker_text varchar(1000),
  original_text varchar(2000) not null,
  supported boolean not null,
  error_code varchar(100),
  error_message varchar(2000),
  constraint fk_item_file foreign key (requirement_file_id) references requirement_files(id),
  index ix_item_file (requirement_file_id)
) engine=InnoDB default charset=utf8mb4;

create table resolved_packages (
  id char(36) primary key,
  build_task_id char(36) not null,
  normalized_name varchar(255) not null,
  final_version varchar(100),
  dependency_type varchar(20) not null,
  original_constraint varchar(500),
  strict_version varchar(100),
  change_direction varchar(30) not null,
  change_reason varchar(1000),
  attempts_json json not null,
  wheel_filename varchar(500),
  wheel_tags json,
  package_source_code varchar(50),
  sha256 char(64),
  wheel_status varchar(30) not null,
  error_message varchar(2000),
  constraint fk_resolved_task foreign key (build_task_id) references build_tasks(id),
  unique key uk_resolved_task_name (build_task_id, normalized_name),
  index ix_resolved_task (build_task_id)
) engine=InnoDB default charset=utf8mb4;

create table build_logs (
  id char(36) primary key,
  build_task_id char(36) not null,
  sequence_no bigint not null,
  stage varchar(50) not null,
  level varchar(20) not null,
  message varchar(4000) not null,
  context_json json,
  created_at datetime(6) not null,
  constraint fk_log_task foreign key (build_task_id) references build_tasks(id),
  unique key uk_log_sequence (build_task_id, sequence_no),
  index ix_log_cursor (build_task_id, sequence_no)
) engine=InnoDB default charset=utf8mb4;

create table package_sources (
  id char(36) primary key,
  code varchar(50) not null unique,
  display_name varchar(100) not null,
  base_url varchar(500) not null,
  priority_no int not null,
  enabled boolean not null,
  timeout_seconds int not null,
  failure_count bigint not null default 0,
  version_no bigint not null default 0,
  updated_at datetime(6) not null
) engine=InnoDB default charset=utf8mb4;

create table artifacts (
  id char(36) primary key,
  build_task_id char(36) not null,
  artifact_type varchar(30) not null,
  filename varchar(255) not null,
  object_key varchar(512) not null unique,
  size_bytes bigint not null,
  sha256 char(64) not null,
  build_status varchar(30) not null,
  validation_type varchar(30) not null,
  expires_at datetime(6) not null,
  download_count bigint not null default 0,
  cleaned_at datetime(6),
  created_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_artifact_task foreign key (build_task_id) references build_tasks(id),
  index ix_artifact_task (build_task_id),
  index ix_artifact_expiry (expires_at, cleaned_at)
) engine=InnoDB default charset=utf8mb4;

create table download_records (
  id char(36) primary key,
  artifact_id char(36) not null,
  user_id char(36) not null,
  ip_address varchar(45) not null,
  user_agent varchar(1000),
  completed boolean not null,
  downloaded_at datetime(6) not null,
  constraint fk_download_artifact foreign key (artifact_id) references artifacts(id),
  constraint fk_download_user foreign key (user_id) references users(id),
  index ix_download_artifact (artifact_id),
  index ix_download_user_time (user_id, downloaded_at)
) engine=InnoDB default charset=utf8mb4;

create table system_config (
  config_key varchar(100) primary key,
  config_value json not null,
  description varchar(500) not null,
  updated_by char(36),
  updated_at datetime(6) not null,
  version_no bigint not null default 0,
  constraint fk_config_user foreign key (updated_by) references users(id)
) engine=InnoDB default charset=utf8mb4;

create index ix_requirement_file_user on requirement_files(user_id, created_at);
create index ix_build_user_created on build_tasks(user_id, created_at);
create index ix_build_file on build_tasks(requirement_file_id);
create index ix_build_profile on build_tasks(target_profile_id);
