# Deployment runbook

For deployment through Portainer using environment variables and an included
Cloudflare Tunnel, use [the Portainer guide](PORTAINER.md) and
`compose.portainer.yaml` instead of the file-based secret setup below.

The repository deploys as one Docker Compose application. Only the gateway is
published on the host. PostgreSQL, Garage, the ownership API and the CKW
calculation engine live on an internal Docker network; Cloudflare Tunnel makes
outbound connections from the `edge` network.

## 1. Prepare configuration

```bash
cp .env.example .env
./scripts/bootstrap-secrets.sh
```

Replace every placeholder in `.env`. In particular, use independent random
values for `POSTGRES_PASSWORD`, `S3_ACCESS_KEY_ID` and
`S3_SECRET_ACCESS_KEY`. A Garage access key is `GK` followed by 24 hexadecimal
characters; the matching secret is 64 hexadecimal characters. Do not reuse
Auth0 or Cloudflare credentials.

The encryption key in `secrets/data_encryption_key` is required to read every
registered CKW project, file and result. Back it up separately from the data
volumes. Losing it makes the encrypted data unrecoverable.

## 2. Configure Auth0

Use a regular single-page application and API audience. Enable self-signup on
the database connection and add these application URLs:

- Allowed callback URLs: `https://communi.ch/raum/`,
  `https://communi.ch/raum/ckw/`, `https://ckw.communi.ch/`
- Allowed logout URLs: `https://communi.ch/raum/`,
  `https://ckw.communi.ch/`
- Allowed web origins: `https://communi.ch`, `https://ckw.communi.ch`

For local Auth0 testing, add the matching `http://localhost:8080` and
`http://ckw.localhost:8080` URLs. Production must use HTTPS.

Open registration is an Auth0 tenant setting; it is deliberately not hidden in
application code. Auth0 Universal Login handles email verification, password
policy and abuse controls.

## 3. Start and validate the private stack

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8080/health/live
curl --fail -H 'Host: communi.ch' http://127.0.0.1:8080/health/ready
curl --fail -H 'Host: ckw.localhost' http://127.0.0.1:8080/
```

Garage 2.2 starts in single-node mode and creates the configured private bucket
and key. The `migrate` service applies both idempotent PostgreSQL migrations
before the API starts. API readiness checks PostgreSQL, the Garage bucket and
the private calculation engine.

For a multi-node production storage cluster, replace the single-node Garage
service with a separately operated replicated Garage cluster while retaining
the same S3 contract. Full-disk encryption remains recommended even though CKW
payloads are encrypted by the application before upload.

## 4. Configure Cloudflare Tunnel

The bundled tunnel image is pinned to `cloudflare/cloudflared:2026.8.2` so a
routine redeploy cannot silently change the edge component. Review and test a
new release before updating the pin.

Create one remotely managed tunnel. Put its token, and nothing else, in
`secrets/cloudflared_token`. Configure these public hostnames in the Cloudflare
dashboard:

| Public hostname | Tunnel service |
| --- | --- |
| `communi.ch` | `http://gateway:8080` |
| `www.communi.ch` | `http://gateway:8080` |
| `ckw.communi.ch` | `http://gateway:8080` |

Then start the tunnel profile:

```bash
docker compose --profile tunnel up -d cloudflared
docker compose logs --tail=100 cloudflared
```

Do not publish the API, engine, PostgreSQL, Garage S3 or Garage admin ports.
Cloudflared needs only outbound network access. The gateway selects the public
site or guest CKW app using the original hostname.

## 5. Release and rollback

Before each release:

```bash
./scripts/check.sh
docker compose build
docker compose up -d
docker compose ps
```

Use immutable application image tags in CI/CD even though local Compose builds
from source. Roll back by redeploying the prior images; do not roll back
PostgreSQL or Garage volumes unless the database migration has been reviewed.
Both migrations in this release are additive and idempotent.

## Backups

Back up all of the following as one recovery set:

- PostgreSQL logical dump
- `garage-meta` and `garage-data` volumes
- `secrets/data_encryption_key`
- production `.env` values and the Garage RPC secret in a secrets manager

Cloudflare and Auth0 tokens can be reissued. The application encryption key
cannot be reconstructed.
