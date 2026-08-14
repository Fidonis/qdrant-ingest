# qdrant-ingest

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Scheduled multi-source document ingestion into an existing Qdrant.
Maintained by **Fidonis**.

`qdrant-ingest` synchronises documents from remote or local sources (S3,
WebDAV, SFTP, SMB, FTP, Google Drive, Azure Blob, HTTP, local directories),
extracts their text through an Apache Tika sidecar, chunks them
format-aware, embeds them against any OpenAI-compatible endpoint, and writes
the vectors into a Qdrant collection. Jobs are declared in a single
`jobs.yaml`, run on cron schedules or manual triggers, and support three
modes: `full`, `append`, and `upsert`.

It ships the ingestion pipeline, not the database: Qdrant, the embeddings
endpoint, and the Tika server are external and reached over their URLs.

```text
                    ┌──────────────────────────────────────┐
   jobs.yaml ──────▶│            qdrant-ingest             │
                    │                                      │
   S3 / WebDAV ────▶│  rclone ▶ Tika ▶ chunk ▶ embed ▶ ──┐ │
   SFTP / local     │  scheduler · SQLite state · guards │ │
                    │                                    │ │
                    │  REST :8300/v1   MCP :8300/mcp     │ │
                    └────────────────────────────────────┼─┘
                            ▲              ▲             │
                       bearer token     OIDC token       ▼
                        (operators)    (assistants)   Qdrant
```

## Quick start (Docker)

```bash
cp docker/.env.example docker/.env       # fill in the CHANGE_ME values
mkdir -p docker/config
cp docs/jobs.example.yaml docker/config/jobs.yaml
docker compose -f docker/docker-compose.yml up -d
curl -H "Authorization: Bearer $QI_API_TOKEN" http://localhost:8300/v1/jobs
```

The container starts cleanly even without a `jobs.yaml`: `/health` answers
`200` with `{"status": "degraded"}` so a missing catalog never turns into a
restart loop.

## How it works

A **run** is one execution of one job and always follows the same phases:

1. **Sync** — `rclone` copies the source into a per-job cache directory. The
   sync is part of the run's causal chain, so a failed sync aborts the run
   before the scan phase can see a half-synchronised tree. Local sources are
   scanned in place and skip this phase.
2. **Prepare** — the embedding dimension is probed, the collection is created
   or verified (dimension and recorded model must match), and the four
   payload indexes are (re-)created.
3. **Scan** — the tree is walked, include/exclude filters are applied, and
   each file passes the four-stage change detection (see
   [docs/modes.md](docs/modes.md)).
4. **Extract, chunk, embed, write** — per document, in that order. A
   document's state row is committed only after all of its chunks are
   upserted, so an aborted run always resumes cleanly.
5. **Reconcile** — the generation sweep (`full`) or the vanished-source
   deletion (`upsert`). Destructive phases run only after a clean scan.

Every point carries `ingest_job` and `ingest_run` in its payload. That is
what lets several jobs share one collection: a job's scope is
`ingest_job == <job.id>`, and no job ever touches another job's points.

### Payload contract

| Field | Meaning |
|---|---|
| `text` | the chunk itself |
| `source` | canonical document URI, unique per collection, keyword-indexed |
| `acl_tags` | optional access tags, keyword-indexed |
| `ingest_job` / `ingest_run` | job scope and generation, keyword-indexed |
| `title`, `file_name`, `rel_path`, `source_label` | provenance |
| `chunk_index`, `total_chunks`, `file_type`, `media_type` | chunk metadata |
| `file_mtime`, `file_size`, `content_sha256`, `ingested_at` | change tracking |
| `embedding_model`, `collection` | which model wrote this |

A single `_collection_meta` point per collection records the embedding model
and vector dimension for query-time vectorisation. Its id is
`uuid5(9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02, <collection>)` — the one
constant that must match the consuming side bit for bit.

## Layout

`src/` is its own [uv](https://docs.astral.sh/uv/) project; there is no
virtual environment in the repository root.

```text
src/
  main.py  config.py            entry point and the QI_* settings catalog
  catalog/                      jobs.yaml schema, loader, ${env:} secrets
  sources/                      rclone config and calls, filters, local scan
  extract/  chunk/              Tika client, format router, four chunkers
  embed/                        batch embedding client and rate limiting
  store/                        Qdrant writer, point ids, meta, indexes
  state/                        SQLite documents and run history
  engine/                       run execution, modes, guards, locks, service
  scheduler/                    APScheduler wiring and startup catch-up
  api/                          REST control plane
  mcp_app/                      MCP server, tools, OIDC validation
tests/                          pytest suite plus tests/functional/
docker/                         Dockerfile, compose file, .env.example
```

## Setup

### Prerequisites

- A reachable **Qdrant** instance and its api-key. The ingester writes points
  and collection metadata directly.
- An **OpenAI-compatible embeddings endpoint** with the model enabled. Many
  self-hosted stacks ship their embedding model disabled by default — verify
  the model answers before the first run, or the dimension probe fails with a
  confusing 404.
- An **Apache Tika** server (`apache/tika:3.2.3.0`). OCR for scanned PDFs
  requires the larger `-full` variant; the standard image cannot do OCR and
  those documents are recorded as `skipped_no_text`.

### Install

```bash
cd src
uv sync
```

### Configure

Every setting is an environment variable with the `QI_` prefix; see
[docs/operations.md](docs/operations.md) for the full catalog. The minimum:

```bash
QI_QDRANT_URL=http://qdrant:6333
QI_QDRANT_API_KEY=…
QI_EMBEDDING_API_URL=http://litellm:4000/v1
QI_EMBEDDING_API_KEY=…
QI_EMBEDDING_MODEL=nomic-embed-text
QI_TIKA_URL=http://tika:9998
QI_API_TOKEN=…            # required on every REST /v1 call
```

The job catalog lives at `QI_JOBS_FILE` (default `/config/jobs.yaml`) and is
documented in [docs/jobs-yaml.md](docs/jobs-yaml.md).

### Run

```bash
cd src
uv run python main.py
```

### Run with Docker

`docker/docker-compose.yml` starts the ingester together with its Tika
sidecar and expects Qdrant and the embeddings endpoint to be reachable.

## `jobs.yaml`

```yaml
version: 1

defaults:
  chunking: {words: 400, overlap: 50}
  safety:   {max_delete_ratio: 0.25, empty_source_guard: true}

jobs:
  - id: acme-reports
    source:
      type: s3
      label: acme-reports
      bucket: acme-corp-reports
      prefix: published/
      access_key_id: ${env:QI_SECRET_S3_ACCESS_KEY}
      secret_access_key: ${env:QI_SECRET_S3_SECRET_KEY}
    filters:
      include: ["**/*.pdf", "**/*.docx", "**/*.xlsx"]
    target:
      collection: corporate-knowledge
      acl_tags: ["dept:finance"]
    mode: upsert
    schedule:
      cron: "0 2 * * *"
```

**No credential value ever appears in `jobs.yaml`.** Every secret-typed field
accepts only the form `${env:QI_SECRET_<NAME>}`; a literal is a hard
validation error naming the job and field. That makes the file commit-safe by
construction, and a manipulated catalog cannot read other process variables.

Editing the file is safe at runtime: the catalog is re-read on a poll, on
`POST /v1/config/reload`, and at startup. A catalog that fails validation
never replaces a working one — the previous registry keeps serving and the
errors appear under `GET /v1/config` and in `/health`.

## REST control plane

Every `/v1` route requires `Authorization: Bearer $QI_API_TOKEN`. `/health`
is free; `/metrics` follows `QI_METRICS_AUTH`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | status, loaded jobs, config error, dependency probes |
| GET | `/metrics` | Prometheus metrics |
| GET | `/v1/jobs` · `/v1/jobs/{id}` | catalog view, secrets redacted |
| POST | `/v1/jobs/{id}/run` | trigger a run (`mode`, `dry_run`, `force`, `queue`) |
| POST | `/v1/jobs/{id}/pause` · `/resume` | runtime-only scheduling switch |
| GET | `/v1/jobs/{id}/preview` | what a run would ingest, without running |
| GET | `/v1/runs` · `/v1/runs/{id}` | run history with counters and events |
| DELETE | `/v1/runs/{id}` | cooperative abort |
| GET | `/v1/collections` | points, indexes, embedding metadata |
| GET | `/v1/config` · POST `/v1/config/reload` | catalog state and reload |
| GET | `/v1/orphans` · DELETE `/v1/orphans/{job_id}` | leftovers of renamed jobs |

`dry_run` runs sync, scan, change detection, and extraction and reports the
plan without embedding or writing anything.

## MCP tools

The MCP endpoint is served at `QI_MCP_PATH` (default `/mcp`) and is secured
with OIDC: a bearer token validated against the issuer's JWKS, carrying the
configured audience and the operator realm role.

`list_ingest_jobs` · `get_ingest_job` · `trigger_reindex` ·
`get_ingest_status` · `list_ingest_runs` · `get_ingest_run` ·
`list_ingest_collections` · `reload_ingest_config`

There is deliberately **no destructive tool**: no orphan cleanup, no
collection deletion, no run cancellation. `trigger_reindex` may only pick a
mode that is no more destructive than the configured one — `append` always,
`upsert` for upsert and full jobs, and `full` only where the job sets
`mcp_allow_full: true`. An assistant should be able to start a reindex, never
to destroy an index.

## Local development

```bash
cd src
uv run ruff check ..
uv run mypy .
uv run pytest -q
yamllint .                 # from the repository root
```

`tests/functional/` holds a self-contained Docker stack (Qdrant, Tika, a
deterministic embeddings stub, and the built image) that walks every mode
end to end. See its README; it is not part of the CI run.

## About Fidonis

Fidonis builds and operates self-hosted, OIDC-secured AI infrastructure.
More at [fidonis.de](https://fidonis.de).

## License

MIT — see [LICENSE](LICENSE).

- Trademarks: [TRADEMARK.md](TRADEMARK.md)
- Third-party licenses: [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
- Contributions are Inbound = Outbound, see [CONTRIBUTING.md](CONTRIBUTING.md)
