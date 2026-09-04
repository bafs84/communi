# CKW privacy and data lifecycle

## Guest access

Guest access is intentionally fail-closed and ephemeral:

- Project and file metadata exist only in the RAM of the single API process.
- Uploaded files and generated results exist only on the API tmpfs.
- The private calculation engine uses a separate request-local temporary
  directory and DuckDB database, then removes it when the request finishes.
- A guest session expires after 60 minutes without activity and after 24 hours
  regardless of activity. The five-minute cleanup loop is supplemented by an
  expiry check on each request.
- “Sitzung beenden” removes the in-memory session and tmpfs directory
  immediately and expires the opaque HTTP-only cookie.
- Responses use `Cache-Control: no-store`; guest identifiers are random and
  stored in cookies only as hashes on the server.
- A restart removes every guest session by design.

Limits are 10 MB per file and 50 MB per guest session. Guest data is never
written to PostgreSQL, Garage, logs or a container image.

The older anonymous solar-share endpoint now returns its calculation directly
without persisting the upload. At startup, any legacy anonymous rows are removed
and their object keys are queued for deletion.

## Registration and claim

Auth0 self-registration can be started from the guest banner. After the Auth0
callback, the API verifies both the bearer token and the guest CSRF token before
copying the projects into the account. The guest source remains available until
all copies have succeeded; only then is the RAM/tmpfs session purged.

If the registered 1 GB quota cannot hold the session, claim stops before a copy
starts. A failed copy does not erase the guest source, so it can be retried
before expiry.

## Registered access

Registered projects remain until the owning Auth0 subject deletes them:

- Ownership and quotas are enforced by the Rust API.
- Project names, municipalities, configurations, original filenames, uploaded
  contents, generated filenames and generated contents are encrypted using
  AES-256-GCM with a random nonce before persistence.
- PostgreSQL stores ownership, ciphertext, content hashes, sizes, media types,
  operations and Garage object keys.
- Garage is private and contains application-encrypted objects only.
- Every generated CSV, DOCX, XLSX or ZIP is stored automatically and remains
  downloadable from the project result history.
- Deleting a project commits the database deletion and a durable object deletion
  outbox in one transaction. Failed Garage deletions are retried with backoff.

The registered quota is 1 GB per user. Changing that value is an operational
policy change, not a calculation change.

## Logging and calculation isolation

Request tracing must not record request bodies, filenames, Auth0 tokens, CSRF
tokens, project configuration values or engine error details. The public API
returns generic dependency errors for internal failures. The engine accepts only
an internal bearer token and is unreachable from the edge network.

The CKW calculation package remains stateless from the portal's perspective.
Each call reconstructs a temporary project, runs the original CKW functions and
returns the result. This boundary prevents account/storage concerns from
modifying tariff, interval, reconciliation or document-generation semantics.
