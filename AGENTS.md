# Communi working agreement

- Push completed Communi changes directly to `origin/main`, the production Git
  branch. The user has authorized this delivery path; do not introduce a separate
  release approval step.
- Do not start local builds, Docker environments, or local test runs unless the
  user explicitly requests them. This is the user's workflow preference from
  2026-10-25.
- Review the diff before pushing. Keep secrets, customer files and local runtime
  data out of Git.
- Verify delivery with the remote commit and available live health/API checks.
  Report a Git push and an observed live deployment as separate states.
