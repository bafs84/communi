# Deployment runbook

Use [the Portainer guide](PORTAINER.md) with `compose.portainer.yaml` for production.
The lean stack contains the gateway, Rust API with embedded SQLite, private Python
calculation engine and Cloudflare Tunnel. No separate database or storage service
is required.

## Local development

```bash
cp .env.example .env
# Set your Auth0 domain and SPA client ID in .env.
docker compose up -d --build
curl --fail http://127.0.0.1:8080/health/ready
curl --fail -H 'Host: ckw.localhost' http://127.0.0.1:8080/
```

Set `CKW_COOKIE_SECURE=false` only for local HTTP guest testing. Add matching
local callback/logout/web-origin URLs to Auth0 as described in the Portainer
guide. The local stack publishes the gateway on loopback port 8080; its tunnel
is optional:

```bash
docker compose --profile tunnel up -d cloudflared
```

Set `CLOUDFLARED_TUNNEL_TOKEN` first. The Portainer stack starts the tunnel
by default and publishes no host ports.

## Persistence and initialization

`communi-data` contains `/data/communi.sqlite`, SQLite WAL files, registered
uploads/exports in `/data/objects`, and the encryption key under `/data/secrets`.
`internal-secrets` contains the automatically generated service token. Keys
are generated on first startup and reused on all subsequent starts. SQLite
migrations are embedded in the API and applied before it accepts requests.

Guest metadata stays in RAM and guest files stay on tmpfs; API restarts end
guest sessions. Run one API replica on a single Docker host using local volumes.

## Verification and release

```bash
./scripts/check.sh
docker compose build
docker compose up -d
docker compose ps
```

Build the reviewed source revision; retain its images for rollback. Rebuild the
gateway when changing public Auth0 configuration. Do not delete volumes during
updates or roll back schema changes without reviewing compatibility.

## Backups and restore

For a simple consistent backup, stop the stack, back up **both named volumes**,
then start it again. Keep the database, objects and encryption key as one recovery
set. Restore both volumes under the original stack name before starting the API.
Losing the encryption key makes saved encrypted project data unrecoverable.
See [the privacy lifecycle](PRIVACY_AND_DATA_LIFECYCLE.md) for retention and
online-backup constraints.

This release initializes a new SQLite deployment. It does not automatically
import data from the earlier PostgreSQL/Garage design. Existing legacy volumes
must be backed up and retained; their data requires an explicit migration before
switching a populated installation. Historical SQL remains in
`services/api/legacy-postgres` as a reference.
