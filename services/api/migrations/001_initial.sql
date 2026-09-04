create extension if not exists pgcrypto;

create table if not exists app_users (
  id uuid primary key default gen_random_uuid(),
  auth0_sub text not null unique,
  auth0_email text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists workspace_profiles (
  user_id uuid primary key references app_users(id) on delete cascade,
  municipality_key text,
  municipality_label text,
  municipality_name text,
  canton text,
  operator_name text,
  operator_website text,
  operator_address text,
  operator_postal_code text,
  has_battery boolean not null default false,
  has_ev_charging boolean not null default false,
  questions text,
  share_with_researchers boolean not null default false,
  interested_in_community boolean not null default false,
  visible_interest boolean not null default false,
  contact_email text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists workspace_profiles_municipality_idx
  on workspace_profiles (municipality_key);

create index if not exists workspace_profiles_interest_idx
  on workspace_profiles (municipality_key, interested_in_community, visible_interest);

create table if not exists workspace_uploads (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references app_users(id) on delete cascade,
  file_name text not null,
  mime_type text,
  byte_size integer not null default 0,
  sha256 text not null,
  object_bucket text not null,
  object_key text not null,
  row_count integer not null default 0,
  total_kwh double precision not null default 0,
  daylight_consumption_percent double precision not null default 0,
  solar_proxy_percent double precision not null default 0,
  detected_timestamp_column text,
  detected_consumption_column text,
  first_timestamp text,
  last_timestamp text,
  expires_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists workspace_uploads_user_idx
  on workspace_uploads (user_id, created_at desc);

create index if not exists workspace_uploads_expiry_idx
  on workspace_uploads (expires_at)
  where expires_at is not null;

create table if not exists workspace_questions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  municipality_key text,
  body text not null,
  created_at timestamptz not null default now()
);

create index if not exists workspace_questions_user_idx
  on workspace_questions (user_id, created_at desc);
