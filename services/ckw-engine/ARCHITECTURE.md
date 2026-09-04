# Architecture

## Product boundaries

### LEG potential

The React application in `frontend/` talks to `portal_api.py`. Project uploads and persisted analyses live in `portal_analysis.py`. The planning workspace exposes:

- customer and PV time-series ingestion;
- LEG potential and participant optimization;
- residual-solar analysis and an explicit LEG price model;
- editable Word and Excel project exports.

It is directly addressable at `#/leg` and only loads the LEG calculation, residual-solar, optimization, and LEG
time-series APIs in addition to shared project data.

### vZEV simulation and battery

The vZEV planning workspace reuses the same projects, customer uploads, PV uploads, and plant selection, but it
does not expose LEG pricing or LEG reports. It contains:

- optional CKW ZEV aggregate ingestion and measured energy-flow visualization;
- synthetic vZEV benefit simulation and CKW CSV export;
- load-shifting screening;
- battery-placement alternatives for PV self-consumption and peak shaving.

It is directly addressable at `#/vzev` and only loads the vZEV-related analyses in addition to shared project data.

### Monthly vZEV billing

Billing is intentionally independent of the persisted planning projects:

```text
monthly CKW CSV
    -> vzev_invoice.py              parse and calculate
    -> vzev_invoice_documents.py    render one editable invoice per consumer
    -> vzev_feed_in.py              reconcile quarterly export and HKN credits
    -> vzev_community_overview.py   visualize costs, allocations, and cash flow
    -> vzev_invoice_bundle.py       package DOCX files and control index as ZIP
    -> portal_api.py or `ckw invoices`
```

`vzev_operator_overview.py` remains available for analytical model comparisons. Those comparisons are deliberately not included on participant invoices. Generated material belongs in `outputs/`, not beside source code.

The billing workspace is directly addressable at `#/billing`.

### Shared calculations

- `ckw_csv.py` and `duckdb_ingest.py`: classic CKW CSV normalization and ingestion.
- `pricing_import_2025.py`: CKW tariff models used by planning and billing.
- `vzev_simulation.py`: synthetic vZEV data and financial comparison.
- `battery_simulation.py`: battery scenarios shared by the portal analysis.

## Data boundaries

- `data/portal/` contains planning/simulation example inputs.
- `data/vzev/` contains monthly operator exports for invoice generation.
- DuckDB files are local runtime state and should not be used as source fixtures for tests.
- `data/legacy-import/` retains duplicate historical imports until they can be explicitly removed.

## Entrypoints

- `uv run ckw serve`: FastAPI application.
- `npm --prefix frontend run dev`: Vite development UI.
- `uv run ckw invoices`: per-participant invoice generation.
- `uv run ckw overview`: operator overview generation.

All business logic remains callable from Python and is covered by tests; UI and CLI layers only validate inputs and package results.
