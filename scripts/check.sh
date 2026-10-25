#!/bin/sh
set -eu

root_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m unittest discover -s "$root_dir/scripts/tests"

cd "$root_dir/apps/ckw-web"
npm test
npm run build

cd "$root_dir/apps/site"
npm run typecheck
npm run build
npm run lint:check

cd "$root_dir"
docker build --target test --tag communi-api-test services/api
docker build --target test --tag communi-ckw-engine-test services/ckw-engine
docker compose --env-file .env.example config --quiet
docker compose --env-file docs/portainer.env.example -f compose.portainer.yaml config --quiet
echo "Source and container test targets passed."
