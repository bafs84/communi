# Communi CKW architecture

```text
Browser
  |-- communi.ch/raum/ckw/ -- Auth0 bearer token ----|
  |-- ckw.communi.ch ------- guest cookie + CSRF ----|
                                                     v
Cloudflare Tunnel -> nginx gateway -> Rust ownership API
                                      |-- SQLite metadata/ciphertext (WAL)
                                      |-- Local encrypted files/results (/data/objects)
                                      |-- RAM + tmpfs guest sessions
                                      |
                                      `-- internal token -> FastAPI CKW engine
                                                            `-- temporary DuckDB
                                                                + original CKW logic
```

The React application is shared by both hostnames. The gateway and runtime
configuration select the access mode; calculation screens and query semantics
remain common. The private engine exposes no project database and keeps no
request state.

All three imported workflows remain present:

1. LEG potential, prices, plant selection, residual solar, optimization and
   editable Word/Excel exports.
2. vZEV and battery simulation, load shifting, fixed or participant-specific PV
   tariff analysis, tariff CSV and editable Word export.
3. Multi-period vZEV billing with editable participant DOCX invoices and
   reconciliation/control artifacts.

No CSV, DuckDB database, prior project or generated customer document from the
source checkout is included in this monorepo or migrated at deployment.

The lean deployment has four services: gateway, API, calculation engine and
Cloudflare Tunnel. The API embeds SQLite and applies versioned migrations at
startup. No external database, object-storage service or migration container is
required. Run one API replica on one host with local persistent volumes.

On first startup the engine creates a private random service token in the
`internal-secrets` volume. The API mounts that volume read-only and starts after
the authenticated engine health check passes. The API creates its AES key in
`communi-data` alongside the SQLite database and encrypted objects. Existing keys
are reused; invalid keys fail closed. Guest files remain on tmpfs.

The original PostgreSQL SQL is retained in `services/api/legacy-postgres` only
as a historical reference; it is not run by the lean deployment.
