#!/bin/sh
set -eu

test -f /tmp/garage-ready
/garage bucket info "${GARAGE_DEFAULT_BUCKET:?}" >/dev/null 2>&1
