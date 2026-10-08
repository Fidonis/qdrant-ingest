# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

## [1.0.0] - 2026-10-08

### Added
- `delete_vanished` on `POST /v1/jobs/{id}/run`. With `false`, an `upsert` run
  adds new documents and replaces changed ones but removes nothing that is
  missing from the scan, and neither deletion guard applies because there is no
  deletion to guard. It defaults to `true`, any other mode is refused with 422,
  and the MCP tools do not offer it. It is meant for callers that hand documents
  over in batches and remove the files afterwards. (#41)
- Run progress. A run row reports `phase` (`syncing`, `scanning`, `embedding`,
  `cleaning up`), `files_seen`, `files_done` and the file in hand (`current`),
  written at every phase change and at most every two seconds in between. A
  dry run is marked `dry_run`. (#43)
- `GET /v1/jobs/{id}/documents` lists the documents a job tracks and what
  happened to each, filterable by `status`, `q` and `run_id`, with paging. A job
  summary carries `documents: {total, chunks}`. (#43)
- `POST /v1/config/validate` runs catalog text through the real loader (known
  connections, resolvable secrets, cross-job rules) and returns the errors
  without writing or applying anything. (#43)
- An encrypted secret store, `secrets.yaml` next to `jobs.yaml`
  (`QI_SECRETS_FILE`, default `/config/catalog/secrets.yaml`), encrypted like the
  connection api-keys with `QI_CONNECTIONS_SECRET`. A `${env:QI_SECRET_<NAME>}`
  reference is answered from the process environment first and from the store
  second, so the syntax of `jobs.yaml` does not change. The catalog poll watches
  the file, so a new credential makes the jobs that wait for it valid without a
  restart. A store that cannot be decrypted is reported as that and marks
  `/health` as degraded. (#43)
- `features` in the `/health` body (`run_progress`, `documents`, `validate`,
  `delete_runs`, `secret_store`), so a client can adapt to an older release
  without comparing version numbers. (#43, #47)
- `DELETE /v1/jobs/{id}/runs` deletes the runs of a job with their event logs,
  either all of them or those that started within `[since, until)`. It needs
  `confirm=true` (or `dry_run=true`, which only counts), works for a job that is
  no longer in the catalog, and never deletes a run that is still working. Points
  in Qdrant and the tracked documents are left alone. It is not an MCP tool. (#47)

### Changed
- A catalog reload no longer restarts every interval timer. Only jobs whose
  schedule changed are scheduled again, so editing the catalog does not push an
  `every:` job back by a whole interval, and a paused job stays paused.
- The standalone `docker/docker-compose.yml` mounts the config bundle read-only;
  nothing in the service writes there any more.
- A job catalog still served from the legacy `/config/jobs.yaml` logs a hint to
  move it to `/config/catalog/jobs.yaml` instead of offering a migration.

### Removed
- **Breaking.** The operator web interface (`QI_UI_PATH`, default `/ui`) is gone,
  together with its `QI_UI_*` settings, the `qdrant-ingest-ui` OIDC client it
  logged in with, the Node and Tailwind build stages of the image, and the
  `jinja2` and `itsdangerous` dependencies. `/ui` answers `404`; REST, MCP,
  `/health` and `/metrics` are unchanged. Jobs, runs, connections and source
  credentials are managed from
  [`papaia-manager`](https://github.com/Fidonis/papaia-manager) or by editing the
  catalog files. `QI_UI_*` values left in an existing environment are ignored, so
  a deployment that still sets them starts as before; the OIDC client can be
  deleted from the identity provider.

### Upgrade notes
- The state database migrates forward only. Once this release has opened it, an
  older ingester refuses to open it (`SchemaVersionError`), so take a copy of the
  state volume before upgrading if a rollback has to stay possible.
- Without the web interface there is no form that stores a connection api-key.
  `docs/connections.md` shows how to produce the `enc:1:` token on the command
  line.

## [0.3.0] - 2026-09-04

### Added
- Named Qdrant connections. Every Qdrant a job writes to is declared once in a
  new `connections.yaml` next to `jobs.yaml` (`QI_CONNECTIONS_FILE`, default
  `/config/catalog/connections.yaml`), by a unique name; a job selects one
  through a required `target.connection`. The file is managed entirely from the
  operator web interface -- list, create, edit, delete, and a **Test
  connection** button that reaches the instance with a short timeout. Each
  api-key is encrypted at rest (Fernet, keyed from a new
  `QI_CONNECTIONS_SECRET`); a literal key in the file is refused. Reloads and
  the mtime poll now watch both catalog files, and a `connections.yaml` that
  fails to validate leaves the previous set serving.

### Changed
- The embedding model is now purely a job-catalog concern:
  `defaults.embedding.model` or a per-job value, with an enabled job that
  resolves to no model treated as a load error. Only the embeddings endpoint
  (`QI_EMBEDDING_API_URL` / `QI_EMBEDDING_API_KEY`) stays global.
- The operator web interface moves to `papaia-manager`'s `fidonis-brand: 2`
  design system: retuned light/dark token sets, a global focus-visible ring,
  raised `base-100` panels on a `base-200` page, one filled primary action per
  page with the rest behind an overflow menu, destructive verbs confirming in a
  native dialog, and a fixed-width health status chip that no longer reflows the
  header on every poll. No REST or MCP behaviour change.

### Removed
- **Breaking.** `QI_QDRANT_URL`, `QI_QDRANT_API_KEY` and `QI_EMBEDDING_MODEL`
  are gone. A catalog without `target.connection` no longer loads: an operator
  must create `connections.yaml` through the interface, add `target.connection`
  to every job, and set `QI_CONNECTIONS_SECRET` in the environment. There is no
  compatibility shim.

## [0.2.0] - 2026-08-18

### Added
- An optional operator web interface at `QI_UI_PATH` (default `/ui`), served
  by the same process as REST and MCP: dashboard, job catalog editor, run
  history, collections, and orphans. OIDC login via a confidential
  `qdrant-ingest-ui` client, authorized by the existing
  `QI_OIDC_OPERATOR_ROLE` -- no second realm role needed. The job catalog
  moves to `/config/catalog/jobs.yaml`, the one part of the bundle directory
  the container may now write; an installation still carrying it at the old
  `/config/jobs.yaml` keeps being served from there, read-only, until
  migrated.

## [0.1.2] - 2026-08-18

### Fixed
- The MCP endpoint now answers `QI_MCP_PATH` itself. It used to be reachable
  only with a trailing slash: the transport was mounted under the configured
  path, so the path itself fell through to the router's redirect handling and
  returned `307`. MCP clients that guard against SSRF refuse to follow a
  redirect whose target resolves to a private address and abort before sending
  their bearer token, which surfaced as a transport error rather than as an
  authentication failure. The transport now answers `QI_MCP_PATH` and only that
  path; the trailing-slash form, which was the one that happened to work
  before, is redirected to it. No shipped artifact ever referenced that form,
  but a deployment that worked around the bug by adding the slash has to drop
  it again
- The build context no longer carries the development state. `.dockerignore`
  listed `.venv`, `__pycache__`, the tool caches and `.env` by their bare
  names, which Docker matches against the context root only -- unlike git,
  where the same spelling matches at any depth. Every one of those lives under
  `src/` here, so `COPY src/ /app/` copied them into the image: a locally built
  image was 849 MB against 490 MB from CI, 273 MB of it a host virtualenv and
  stale caches under `/app`. The patterns are now depth-independent, which also
  closes the path by which a developer's `src/.env` could have been built in

## [0.1.1] - 2026-08-14

### Fixed
- `/health` no longer probes Qdrant, the embeddings endpoint, and Tika on the
  request path. With an unreachable dependency the endpoint took several
  seconds to answer, well past the container healthcheck's own timeout, so a
  service that was running correctly was marked `unhealthy` indefinitely. The
  probes now run on a background thread and `/health` returns the cached
  snapshot in milliseconds
- REST handlers are no longer declared `async` while calling the synchronous
  engine. Those calls ran on the event loop and stalled unrelated requests,
  including the mounted MCP endpoint; they now run in a worker thread

### Added
- `deps_checked_at` in the `/health` body: the timestamp of the last
  dependency probe, `null` until the first one completes. Until then
  dependencies count as unknown rather than down, so a freshly started
  container does not report itself degraded on that basis

## [0.1.0] - 2026-08-14

First release.

### Added
- Job catalog (`jobs.yaml`) with a strict schema, `defaults` merging, and
  cross-job validation (unique labels per collection, one embedding model per
  collection, no system collections as targets)
- Env-only secret references: secret fields accept `${env:QI_SECRET_<NAME>}`
  and reject literals, which makes the catalog commit-safe by construction
- SQLite state store with WAL, per-thread connections, run history, retention,
  and reconciliation of runs interrupted by a hard restart
- Source synchronisation through an embedded `rclone` binary (S3, WebDAV,
  SFTP, SMB, FTP, Google Drive, Azure Blob, HTTP) plus in-place local scanning,
  with `--max-delete` derived from `max_delete_ratio`
- Text extraction via Apache Tika `/rmeta/text` with streaming uploads,
  bounded retries, and terminal handling of unsupported or encrypted files;
  Markdown is read directly so its heading structure survives
- Four format-aware chunkers: heading-aware markdown, paragraph window,
  spreadsheet row groups with a repeated header, and per-slide presentation
- Qdrant writer with job-scoped, generation-tagged points, the
  `_collection_meta` contract record, and the four keyword payload indexes
- Ingestion modes `full` (generation sweep), `append` (add-only with a
  state-loss probe), and `upsert` (update plus vanished-source deletion)
- Four-stage change detection: stat, content hash, extracted-text hash, and a
  parameter hash that forces a re-embed when chunking or model settings change
- Deletion guards: `empty_source_guard` and `max_delete_ratio`, overridable
  with `force`
- APScheduler-based run engine with cron and interval triggers, three overlap
  layers (coalescing, a per-job lock, a per-collection reader/writer lock),
  a global embedding semaphore and optional rate limit, `run_on_startup`
  catch-up, and cooperative shutdown
- Token-authenticated REST control plane with job, run, collection, config,
  orphan, preview, and dry-run endpoints, Prometheus metrics, and a `/health`
  endpoint that reports `degraded` instead of failing
- OIDC-secured MCP server exposing eight non-destructive tools; a triggered
  reindex can never pick a mode more destructive than the configured one
- Signed multi-arch images published to GHCR on release, and MCP registry
  publication under `de.fidonis/qdrant-ingest`

[Unreleased]: https://github.com/Fidonis/qdrant-ingest/compare/v1.0.0...main
[1.0.0]: https://github.com/Fidonis/qdrant-ingest/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/Fidonis/qdrant-ingest/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Fidonis/qdrant-ingest/releases/tag/v0.1.0
