# Communi.ch platform

This repository is the deployable Communi.ch monorepo. It combines the public
Astro site, the Auth0-protected portal, the CKW LEG/vZEV/billing workspace, the
Rust ownership and storage API, and the unchanged Python calculation package.

## Layout

- `apps/site` — public Astro site and `/raum/` portal entry.
- `apps/ckw-web` — React workspace for LEG, vZEV/battery and vZEV billing.
- `services/api` — public Rust API for authentication, storage and privacy.
- `services/ckw-engine` — private FastAPI calculation engine and CKW library.
- `infra` — database, Garage, gateway and Cloudflare Tunnel configuration.
- `docs` — project documentation and deployment/privacy runbooks.

The pre-monorepo folders remain on disk as ignored rollback references. They are
not Docker build contexts and are not part of the deployed repository.

## Local development

```bash
cp .env.example .env
./scripts/bootstrap-secrets.sh
docker compose up --build
```

The local gateway listens on `http://127.0.0.1:8080`. Use the `Host` header
`ckw.localhost` to exercise the guest workspace.

## Verification

```bash
./scripts/check.sh
docker compose config --quiet
docker compose build
```

No customer CSV, DuckDB database, generated invoice, Auth0 secret, storage key,
or tunnel credential belongs in this repository or any container image.

Operational details are in [the deployment runbook](docs/DEPLOYMENT.md), and
the enforceable retention model is documented in
[the privacy lifecycle](docs/PRIVACY_AND_DATA_LIFECYCLE.md).

## Portainer deployment

For a Git-backed Docker Standalone stack with Cloudflare Tunnel included, use
`compose.portainer.yaml` and follow [the Portainer guide](docs/PORTAINER.md).
All deployment values, including secrets, are entered in Portainer. No local
`.env`, secret files or host bind mounts are required by this stack.
