# Communi CKW architecture

```text
Browser
  |-- communi.ch/raum/ckw/ -- Auth0 bearer token ----|
  |-- ckw.communi.ch ------- guest cookie + CSRF ----|
                                                     v
Cloudflare Tunnel -> nginx gateway -> Rust ownership API
                                      |-- PostgreSQL metadata/ciphertext
                                      |-- Garage encrypted files/results
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
