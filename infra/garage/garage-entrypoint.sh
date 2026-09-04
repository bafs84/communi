#!/bin/sh
set -eu

: "${GARAGE_DEFAULT_ACCESS_KEY:?GARAGE_DEFAULT_ACCESS_KEY is required}"
: "${GARAGE_DEFAULT_SECRET_KEY:?GARAGE_DEFAULT_SECRET_KEY is required}"
: "${GARAGE_DEFAULT_BUCKET:?GARAGE_DEFAULT_BUCKET is required}"

/garage server &
server_pid=$!

stop_server() {
  kill -TERM "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap stop_server INT TERM

status_file=/tmp/garage-status
until /garage status >"$status_file" 2>/dev/null; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
    exit $?
  fi
  sleep 1
done

if grep -q "NO ROLE ASSIGNED" "$status_file"; then
  node_id=$(awk 'NF >= 3 && $1 != "ID" && $1 !~ /^=/ { print $1; exit }' "$status_file")
  /garage layout assign --zone communi --capacity "${GARAGE_NODE_CAPACITY:-100GB}" "$node_id"
  current_version=$(/garage layout show 2>/dev/null | awk '/Current cluster layout version:/ { print $5; exit }')
  /garage layout apply --version "$((current_version + 1))"
fi

if ! /garage key info "$GARAGE_DEFAULT_ACCESS_KEY" >/dev/null 2>&1; then
  /garage key import --yes -n communi-api \
    "$GARAGE_DEFAULT_ACCESS_KEY" "$GARAGE_DEFAULT_SECRET_KEY"
fi

if ! /garage bucket info "$GARAGE_DEFAULT_BUCKET" >/dev/null 2>&1; then
  /garage bucket create "$GARAGE_DEFAULT_BUCKET"
fi

/garage bucket allow --read --write --owner \
  "$GARAGE_DEFAULT_BUCKET" --key "$GARAGE_DEFAULT_ACCESS_KEY" >/dev/null
touch /tmp/garage-ready

wait "$server_pid"
