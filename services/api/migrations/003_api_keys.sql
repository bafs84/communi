create table ckw_api_keys (
  id blob primary key not null,
  user_id blob not null references app_users(id) on delete cascade,
  name text not null,
  token_prefix text not null,
  token_hash text not null unique,
  scopes text not null,
  project_id blob references ckw_projects(id) on delete cascade,
  created_at text not null,
  expires_at text not null,
  revoked_at text,
  last_used_at text
);

create index ckw_api_keys_user_idx on ckw_api_keys (user_id, created_at desc);
