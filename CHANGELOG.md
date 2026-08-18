# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

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

[Unreleased]: https://github.com/Fidonis/qdrant-ingest/compare/v0.2.0...main
[0.2.0]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Fidonis/qdrant-ingest/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Fidonis/qdrant-ingest/releases/tag/v0.1.0
