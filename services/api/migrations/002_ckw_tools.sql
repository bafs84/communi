create table if not exists ckw_projects (
  id blob primary key not null,
  user_id blob not null references app_users(id) on delete cascade,
  name_ciphertext blob not null,
  municipality_ciphertext blob not null,
  config_ciphertext blob not null,
  config_schema_version integer not null default 1,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
  updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
);

create index if not exists ckw_projects_user_idx
  on ckw_projects (user_id, updated_at desc);

create table if not exists ckw_project_files (
  id blob primary key not null,
  project_id blob not null references ckw_projects(id) on delete cascade,
  original_filename_ciphertext blob not null,
  upload_kind text not null check (upload_kind in ('solar', 'consumption', 'zev', 'billing')),
  media_type text not null default 'text/csv',
  byte_size integer not null check (byte_size >= 0),
  sha256 text not null,
  object_bucket text not null,
  object_key text not null unique,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
);

create index if not exists ckw_project_files_project_idx
  on ckw_project_files (project_id, created_at);

create table if not exists ckw_results (
  id blob primary key not null,
  project_id blob not null references ckw_projects(id) on delete cascade,
  operation text not null,
  filename_ciphertext blob not null,
  media_type text not null,
  byte_size integer not null check (byte_size >= 0),
  sha256 text not null,
  object_bucket text not null,
  object_key text not null unique,
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now'))
);

create index if not exists ckw_results_project_idx
  on ckw_results (project_id, created_at desc);

create table if not exists storage_deletion_outbox (
  id integer primary key autoincrement,
  object_bucket text not null,
  object_key text not null,
  attempts integer not null default 0,
  next_attempt_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
  unique (object_bucket, object_key)
);

create index if not exists storage_deletion_outbox_ready_idx
  on storage_deletion_outbox (next_attempt_at, id);
