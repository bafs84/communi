# Deploy the lean CKW platform in Portainer

Repository: `https://github.com/bafs84/communi.git`

Use `compose.portainer.yaml` by itself on a **Docker Standalone** environment.
It builds the public site, CKW app, Rust API and Python engine and includes
Cloudflare Tunnel. SQLite and private file storage are embedded in the API;
no PostgreSQL, Garage, S3 credentials or separate migration service are needed.
The stack uses named volumes and has no bind mounts or host ports.

## 1. Enter the external account values

In Portainer's stack environment editor, enter:

```dotenv
PUBLIC_AUTH0_DOMAIN=communi.eu.auth0.com
PUBLIC_AUTH0_CLIENT_ID=your-auth0-spa-client-id
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
```

Use your actual Auth0 tenant and SPA client ID. Paste just Cloudflare's remotely
managed tunnel token (`eyJ...`), not an API token or installation command.
You can import [portainer.env.example](portainer.env.example) and replace its
two placeholders. `PUBLIC_AUTH0_AUDIENCE` defaults to `https://api.communi.ch`;
create that API identifier in Auth0 or set this one optional variable to match
your chosen identifier. The backend derives its issuer and audience from the
same public settings, so no duplicate Auth0 variables are needed.

**All internal random secrets are generated automatically on first startup.**
The engine publishes a random internal token in `internal-secrets`; the API
creates its encryption key in `communi-data`. Neither needs a Portainer variable.
Existing files are reused across restarts and redeployments. Invalid keys cause
startup to fail instead of being replaced. Do not enter the old PostgreSQL,
Garage/S3, `DATA_ENCRYPTION_KEY` or `CKW_INTERNAL_TOKEN` variables.

## 2. Configure Auth0

Use an Auth0 single-page application and configure its allowed URLs:

| Setting | Values |
| --- | --- |
| Allowed Callback URLs | `https://communi.ch/raum/`, `https://communi.ch/raum/ckw/`, `https://ckw.communi.ch/` |
| Allowed Logout URLs | `https://communi.ch/raum/`, `https://ckw.communi.ch/` |
| Allowed Web Origins | `https://communi.ch`, `https://ckw.communi.ch` |

No client secret is needed. Enable self-signup in Auth0 if required.
For local development add the equivalent URLs on `http://localhost:8080` and
`http://ckw.localhost:8080`.

## 3. Configure the Cloudflare Tunnel

Create a remotely managed tunnel and these published application routes:

| Public hostname | Service URL |
| --- | --- |
| `communi.ch` | `http://gateway:8080` |
| `www.communi.ch` | `http://gateway:8080` |
| `ckw.communi.ch` | `http://gateway:8080` |

Leave the HTTP Host Header override unset. Enable HTTPS at Cloudflare. Bypass
caching for `/api/*`, `/v1/*`, `/raum/*`, `/communi-config.js` and the CKW hostname
except fingerprinted `/assets/*` files. Do not use Cache Everything on the app.

The guest calculator is at `https://ckw.communi.ch/`; authenticated saved projects
are at `https://communi.ch/raum/ckw/`.

## 4. Deploy and verify

In Portainer select **Stacks → Add stack → Repository**:

| Field | Value |
| --- | --- |
| Name | `communi` |
| Repository URL | `https://github.com/bafs84/communi.git` |
| Repository reference | `refs/heads/main` (or a reviewed release tag) |
| Compose path | `compose.portainer.yaml` |

Supply read-only Git authentication if the repository requires it. Enter the
variables above and deploy. The first build compiles Rust and the web apps;
allow time and disk space. Portainer Business Edition relative-volume support
is not required. Docker Swarm is not supported by this source-build stack.

The engine initializes its token and passes its authenticated health check;
the API then applies SQLite migrations, initializes its encryption key, and
starts. Check engine, API and gateway health and cloudflared connection logs.
Visit `https://communi.ch/health/ready`, create a synthetic guest project and
verify simulation/export, then verify Auth0 login and saving/reopening a project.

Portainer must support building Git stacks on this Docker environment. If it
cannot, clone the repository on the Docker host and build there first using
`docker compose --env-file <private-env-file> -p communi -f compose.portainer.yaml
build`; then deploy this stack on the same engine.

For updates pull the reviewed Git revision, rebuild and redeploy while retaining
the stack name and volumes. If your Portainer version reuses old local images,
build explicitly with the command above. Public Auth0 changes require a gateway
rebuild because the Astro site embeds that configuration.

## Backups and earlier installations

The single-host deployment uses two named volumes:

- `communi-data`: SQLite database/WAL, registered files/results and encryption key.
- `internal-secrets`: generated API-to-engine token.

Stop the stack for a consistent backup of both volumes, then restart. Retain both
on redeployment and restore them together. Keep the encryption key with the data;
losing it makes encrypted saved projects unrecoverable. Use local Docker volumes
and one API replica; SQLite WAL is not a distributed database.

This is a fresh SQLite deployment, not an automatic conversion of the previous
PostgreSQL/Garage storage. Keep any legacy volumes and backups intact until an
explicit data migration has been performed. The old SQL is retained under
`services/api/legacy-postgres` for reference.

## References

- [Portainer Git stacks and environment variables](https://docs.portainer.io/user/docker/stacks/add)
- [Cloudflare tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)
- [SQLite WAL and backup considerations](https://www.sqlite.org/wal.html)
