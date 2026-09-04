create table if not exists ckw_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  name_ciphertext bytea not null,
  municipality_ciphertext bytea not null,
  config_ciphertext bytea not null,
  config_schema_version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists ckw_projects_user_idx
  on ckw_projects (user_id, updated_at desc);

create table if not exists ckw_project_files (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references ckw_projects(id) on delete cascade,
  original_filename_ciphertext bytea not null,
  upload_kind text not null check (upload_kind in ('solar', 'consumption', 'zev', 'billing')),
  media_type text not null default 'text/csv',
  byte_size bigint not null check (byte_size >= 0),
  sha256 text not null,
  object_bucket text not null,
  object_key text not null unique,
  created_at timestamptz not null default now()
);

create index if not exists ckw_project_files_project_idx
  on ckw_project_files (project_id, created_at);

create table if not exists ckw_results (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references ckw_projects(id) on delete cascade,
  operation text not null,
  filename_ciphertext bytea not null,
  media_type text not null,
  byte_size bigint not null check (byte_size >= 0),
  sha256 text not null,
  object_bucket text not null,
  object_key text not null unique,
  created_at timestamptz not null default now()
);

create index if not exists ckw_results_project_idx
  on ckw_results (project_id, created_at desc);

create table if not exists storage_deletion_outbox (
  id bigserial primary key,
  object_bucket text not null,
  object_key text not null,
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (object_bucket, object_key)
);

create index if not exists storage_deletion_outbox_ready_idx
  on storage_deletion_outbox (next_attempt_at, id);
