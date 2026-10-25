# Deploy the lean CKW platform in Portainer

Repository: `https://github.com/bafs84/communi.git`

Use `compose.portainer.yaml` by itself on a **Docker Standalone** environment.
It builds the public site, CKW app, Rust API and Python engine and includes
Cloudflare Tunnel. SQLite and private file storage are embedded in the API;
no PostgreSQL, Garage, S3 credentials or separate migration service are needed.
The stack uses named volumes and has no bind mounts or host ports.

GlitchTip error tracking for the supplied Communi project is preconfigured;
no additional environment variable is required. See [error tracking and upload
recovery](OBSERVABILITY.md) for coverage, privacy and optional overrides.

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

1. In **Applications → Applications → Create Application**, create `Communi`
   and select **Single Page Web Applications**. Copy the Domain and Client ID
   into the two `PUBLIC_AUTH0_*` variables above. Keep the browser app's token
   endpoint authentication method set to `None`; no client secret is needed.
2. In the application's Settings, enter the following comma-separated URLs and
   save. Use `communi.ch` as the canonical hostname; if serving `www.communi.ch`,
   redirect it to `communi.ch` at Cloudflare before starting a login flow.

| Setting | Values |
| --- | --- |
| Allowed Callback URLs | `https://communi.ch/raum/`, `https://communi.ch/raum/ckw/`, `https://ckw.communi.ch/` |
| Allowed Logout URLs | `https://communi.ch/raum/`, `https://ckw.communi.ch/`, `https://ckw.communi.ch/raum/` |
| Allowed Web Origins | `https://communi.ch`, `https://ckw.communi.ch` |

3. Enable **Refresh Token Rotation** in the application's Settings. Under
   **Advanced Settings → Grant Types**, enable **Authorization Code** and
   **Refresh Token**. The CKW client already requests refresh tokens through
   the Auth0 SPA SDK.
4. Under **Authentication → Database**, enable a database connection such as
   `Username-Password-Authentication` for the Communi application. Leave
   **Disable Sign Ups** off if visitors should be able to register themselves.
5. In **Applications → APIs → Create API**, create `Communi API` with identifier
   **`https://api.communi.ch`** (no trailing slash) and signing algorithm **RS256**.
   This identifier is the token audience; it does not require a separate DNS
   record, host or API container.
6. In that API's Settings, enable **Allow Offline Access**. If your tenant shows
   **Application Access Policy**, choose **Per-app authorization** for
   **User-Delegated Access**, then authorize the Communi SPA for user-delegated
   access on the **Application Access** tab. No custom API permissions or roles
   are required by this platform; the API enforces project ownership itself.

Save the application and API settings. For local development add the equivalent
callback/logout URLs and origins on `http://localhost:8080` and
`http://ckw.localhost:8080`.

If registration returns `Client ... is not authorized to access resource server
"https://api.communi.ch"`, open **Applications → APIs → Communi API → Application
Access** and authorize the SPA matching `PUBLIC_AUTH0_CLIENT_ID` for
**User-Delegated Access**. This is separate from enabling the login/database
connection. Retain per-app authorization; no machine-to-machine grant or custom
API scopes are needed. The CKW page reports this configuration failure without
retaining the OAuth error or transaction state in the address bar.

After deployment, visit `https://communi.ch/raum/ckw/`, register a test account,
create and save a synthetic project, log out, log in again and reopen it. This
checks the real Auth0 integration; the repository's isolated tests use a test
identity provider and do not verify your tenant configuration.

## 3. Configure the Cloudflare Tunnel

Create a remotely managed tunnel and configure this public hostname routing:

| Public hostname | Service URL |
| --- | --- |
| `communi.ch` | `http://gateway:8080` |
| `www.communi.ch` | Edge redirect to `https://communi.ch` (see below) |
| `ckw.communi.ch` | `http://gateway:8080` |

Verify **DNS → Records** as well as the tunnel route. The root domain and `ckw`
must have proxied CNAME records pointing to `<TUNNEL_ID>.cfargotunnel.com`.
Existing A/AAAA records pointing to a previous web host must be replaced for
those exact hostnames; changing the tunnel's Service URL alone does not move
traffic away from an old DNS destination. Preserve mail records (MX, SPF,
DKIM, DMARC and mail configuration records) and unrelated hostnames.

For `www`, configure an edge redirect to `https://communi.ch` preserving the
path and query string. Its existing proxied DNS records can remain in place
when the redirect handles the request at Cloudflare. This keeps Auth0 login
on the canonical hostname and avoids changing any mail records on `www`.

Leave the HTTP Host Header override unset. Enable HTTPS at Cloudflare. Bypass
caching for `/api/*`, `/v1/*`, `/raum/*`, `/communi-config.js` and the CKW hostname
except fingerprinted `/assets/*` files. Do not use Cache Everything on the app.

The guest calculator is at `https://ckw.communi.ch/`; authenticated saved projects
are at `https://communi.ch/raum/ckw/`.

If the old site still appears, request `/health/ready` and `/communi-config.js`.
The new gateway returns HTTP 200 for both. A previous hosting provider's 404
page means the request is reaching the old origin. Check the exact hostname's
DNS record and any wildcard fallback before rebuilding images or purging caches.

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
- [Auth0 SPA registration](https://auth0.com/docs/get-started/auth0-overview/create-applications/single-page-web-apps)
- [Auth0 SPA refresh-token configuration](https://support.auth0.com/center/s/article/How-to-Use-Refresh-Tokens-in-a-SPA)
- [Auth0 API application access policies](https://auth0.com/blog/developers-guide-api-access-policies-auth0/)
- [Cloudflare tunnel tokens](https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/)
- [SQLite WAL and backup considerations](https://www.sqlite.org/wal.html)
