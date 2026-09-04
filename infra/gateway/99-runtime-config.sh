#!/bin/sh
set -eu

config_file=/var/run/communi-config.js
cat > "$config_file" <<EOF
window.__COMMUNI_CONFIG__ = Object.freeze({
  apiBaseUrl: "${PUBLIC_API_BASE_URL:-}",
  auth0Domain: "${PUBLIC_AUTH0_DOMAIN:-}",
  auth0ClientId: "${PUBLIC_AUTH0_CLIENT_ID:-}",
  auth0Audience: "${PUBLIC_AUTH0_AUDIENCE:-}",
  memberCkwUrl: "${PUBLIC_CKW_MEMBER_URL:-https://communi.ch/raum/ckw/}"
});
EOF
