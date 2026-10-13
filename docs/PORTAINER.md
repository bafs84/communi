# Deploy Communi and the CKW calculator with Portainer

Repository: `https://github.com/bafs84/communi.git`

Use `compose.portainer.yaml` as a **standalone** Compose file on a Portainer
**Docker Standalone** environment. It builds the public site, CKW workspace,
Rust API, Python calculation engine, database migration runner and Garage storage
from the repository, and starts Cloudflare Tunnel automatically. Docker Swarm
does not build these images from source and is not supported by this file.

No host ports are published. The tunnel connects to the gateway over Docker's
network; PostgreSQL, Garage and the engine have no published ports. The engine
can make outbound requests for public Swiss PV-plant data, and the API can
retrieve Auth0 signing keys.

Configuration and migrations are packaged into images. Persistent data uses
named Docker volumes. There are no host bind mounts, secret files or `env_file`
dependencies, so Portainer Business Edition's relative-volume feature is not
required. Secrets are supplied through Portainer's environment editor and are
visible to administrators with permission to inspect containers.

## 1. Configure the Cloudflare Tunnel

Create a remotely managed Cloudflare Tunnel and copy its tunnel token (the
`eyJ...` value, not the installation command or a Cloudflare API token).
Enter it as `CLOUDFLARED_TUNNEL_TOKEN` in Portainer. The stack passes it to
cloudflared as `TUNNEL_TOKEN`.

Create these published application routes on that tunnel:

| Public hostname | Service URL |
| --- | --- |
| `communi.ch` | `http://gateway:8080` |
| `www.communi.ch` | `http://gateway:8080` |
| `ckw.communi.ch` | `http://gateway:8080` |

Leave the HTTP Host Header override unset so the gateway receives the original
hostname and can select the correct app. Enable HTTPS at Cloudflare. Configure
cache bypass for `/api/*`, `/v1/*`, `/raum/*` and `/communi-config.js`, and for
the CKW hostname except its fingerprinted `/assets/*` files. Do not apply a
Cache Everything rule to the application or API.

The calculator is at `https://ckw.communi.ch/` for guests. Authenticated saved
projects are at `https://communi.ch/raum/ckw/`.

## 2. Configure Auth0

Use an Auth0 single-page application and an API audience. Set the SPA's public
client ID as `PUBLIC_AUTH0_CLIENT_ID`. No Auth0 client secret is needed.

| Auth0 application setting | Values |
| --- | --- |
| Allowed Callback URLs | `https://communi.ch/raum/`, `https://communi.ch/raum/ckw/`, `https://ckw.communi.ch/` |
| Allowed Logout URLs | `https://communi.ch/raum/`, `https://ckw.communi.ch/` |
| Allowed Web Origins | `https://communi.ch`, `https://ckw.communi.ch` |

Use the same audience for `AUTH0_AUDIENCE` and `PUBLIC_AUTH0_AUDIENCE`. Match
`PUBLIC_AUTH0_DOMAIN` and `AUTH0_ISSUER_BASE_URL` to your tenant (the issuer URL
includes `https://` and a trailing `/`). Enable self-signup in Auth0 if required.

## 3. Create the stack and enter variables

In Portainer, select your Docker Standalone environment, then **Stacks → Add
stack → Repository**:

- Name: `communi` (keep this name for later redeployments to reuse its volumes).
- Repository URL: `https://github.com/bafs84/communi.git`.
- Repository reference: `refs/heads/main`.
- Compose path: `compose.portainer.yaml`.
- If GitHub requires authentication, supply a read-only repository credential
  through Portainer's Git authentication fields.

In **Environment variables**, load [portainer.env.example](portainer.env.example)
and replace every `replace-*` value, or enter those variables individually.
Generate the following values independently, once, and save them in your
password/secrets manager:

| Variable | Generate or obtain |
| --- | --- |
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` (hex avoids URL escaping in the database connection string) |
| `S3_ACCESS_KEY_ID` | `printf 'GK'; openssl rand -hex 12` |
| `S3_SECRET_ACCESS_KEY` | `openssl rand -hex 32` |
| `GARAGE_RPC_SECRET` | `openssl rand -hex 32` |
| `DATA_ENCRYPTION_KEY` | `openssl rand -base64 32` (must decode to exactly 32 bytes) |
| `CKW_INTERNAL_TOKEN` | `openssl rand -hex 48` |
| `CLOUDFLARED_TUNNEL_TOKEN` | Cloudflare's remotely managed tunnel token |

Keep `PUBLIC_API_BASE_URL` empty to use the same origin. The remaining public
configuration in the example matches `communi.ch` and `ckw.communi.ch`.
Optional quotas, retention, log level and image overrides are listed in the
Compose file. Keep `CKW_COOKIE_SECURE=true` for the HTTPS deployment.

Click **Deploy the stack**. The first deployment compiles Rust and both web
applications and downloads Python dependencies; allow time and disk space for
the image builds. The migration service should finish with exit code 0; the
long-running services should remain running, with health checks passing where
configured. The stack does not require a manual database initialization step.

Portainer must be able to build images on this Docker environment. If your
installation cannot build Git stacks, clone this repository on the Docker host
and build the images with `docker compose --env-file <private-env-file> -f
compose.portainer.yaml build` first; then deploy the same stack on that engine.
Supply the same public build configuration and use the stack name `communi`.

## 4. Verify and update

- Check `migrate` logs for successful migrations and exit code 0.
- Check `garage`, `api` and `gateway` health in Portainer and check `cloudflared`
  logs for registered tunnel connections.
- Visit `https://communi.ch/health/ready`; it should return HTTP 200.
- Open `https://ckw.communi.ch/`, create a guest vZEV project, upload a
  non-customer test CSV and verify simulation and an export.
- Log in at `https://communi.ch/raum/ckw/` and verify saving and reopening a
  test project.

For source updates, pull the new Git revision, rebuild the application images
and redeploy. In Portainer, use **Pull and redeploy** and the image-build option
provided by your version. If it reuses existing local images, build explicitly
on the Docker host using the command above, then redeploy. Changes to the public
Auth0 build arguments also require rebuilding the gateway image because the
Astro site embeds that configuration at build time.

For repeatable releases, deploy a reviewed Git commit/tag and retain its built
images for rollback. Do not delete the persistent volumes during updates.

## Backups and existing deployments

Back up PostgreSQL, the `garage-meta` and `garage-data` volumes, and all private
environment values as one recovery set. The bundled Garage service has one
storage node; keep independent backups before storing real project data.
Losing `DATA_ENCRYPTION_KEY` makes saved encrypted data unrecoverable.

When moving an existing installation to this stack, retain its stack/volume
names and copy its existing encryption key, internal token, Garage RPC secret,
S3 credentials and database password into Portainer. Do not generate replacement
keys for existing data. Changing a Postgres environment password does not change
the password inside an initialized database, and changing Garage environment
credentials does not rotate an already imported key.

## References

- [Portainer: create a Git stack and set environment variables](https://docs.portainer.io/user/docker/stacks/add)
- [Portainer: relative-volume support](https://docs.portainer.io/advanced/relative-paths)
- [Cloudflare: tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)
- [Cloudflare: tunnel run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)
