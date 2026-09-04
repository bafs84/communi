#!/bin/sh
set -eu

umask 077
mkdir -p secrets

create_secret() {
  target=$1
  command=$2
  if [ ! -f "$target" ]; then
    sh -c "$command" > "$target"
    chmod 600 "$target"
    echo "created $target"
  fi
}

create_secret secrets/data_encryption_key "openssl rand -base64 32 | tr -d '\\n'"
create_secret secrets/ckw_internal_token "openssl rand -base64 48 | tr -d '\\n'"
create_secret secrets/garage_rpc_secret "openssl rand -hex 32 | tr -d '\\n'"

if [ ! -f secrets/cloudflared_token ]; then
  : > secrets/cloudflared_token
  chmod 600 secrets/cloudflared_token
  echo "created empty secrets/cloudflared_token; paste the Cloudflare Tunnel token before production start"
fi
