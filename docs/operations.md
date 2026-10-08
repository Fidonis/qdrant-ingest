# Operations

## Environment catalog

All variables carry the `QI_` prefix; `OIDC_ISSUER` is the one exception, so
it can be shared verbatim with the surrounding stack.

### Required

| Variable | Meaning |
|---|---|
| `QI_EMBEDDING_API_URL` | OpenAI-compatible base URL |
| `QI_EMBEDDING_API_KEY` | bearer token for the embeddings endpoint |
| `QI_API_TOKEN` | bearer token required on every REST `/v1` call |

The Qdrant instances jobs write to are **not** environment variables. They are
declared in `connections.yaml` — see [connections.md](connections.md). The embedding model is a `jobs.yaml` concern
too (`defaults.embedding.model` or per job).

### Common

| Variable | Default | Meaning |
|---|---|---|
| `QI_CONNECTIONS_SECRET` | — | key the encryption of the connection api-keys and of the source credentials in `secrets.yaml` is derived from; needed to store and to read such a value; a catalog that stores none works without it |
| `QI_TIKA_URL` | `http://qdrant-ingest-tika:9998` | Tika server |
| `QI_JOBS_FILE` | `/config/catalog/jobs.yaml` | job catalog path |
| `QI_JOBS_FILE_LEGACY` | `/config/jobs.yaml` | older catalog location, served only while `QI_JOBS_FILE` does not exist |
| `QI_SECRETS_FILE` | `/config/catalog/secrets.yaml` | encrypted source credentials, read at run time (needs `QI_CONNECTIONS_SECRET`) |
| `QI_CONNECTIONS_FILE` | `/config/catalog/connections.yaml` | connection list path |
| `QI_JOBS_RELOAD_INTERVAL` | `30` | poll of the catalog, connections and secrets files in seconds, `0` disables it |
| `QI_EMBED_META_COLLECTION` | `_collection_meta` | collection that holds the embedding contract record of every collection; a job cannot target it |
| `QI_RBAC_ACL_COLLECTION` | `_rbac_acl` | access-control collection of the surrounding RBAC layer; a job cannot target it |
| `QI_TIMEZONE` | `UTC` | scheduler timezone (IANA name) |
| `QI_HTTP_HOST` / `QI_HTTP_PORT` | `0.0.0.0` / `8300` | control-plane bind |
| `QI_MCP_PATH` | `/mcp` | MCP endpoint path |
| `QI_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`QI_STATE_DIR` (`/data/state`), `QI_CACHE_DIR` (`/data/cache`) and
`QI_LOCAL_DIR` (`/data/local`) are the container's data paths. They are fixed in
the image and meant to be reached through volume mounts, not changed: local
sources must live below `QI_LOCAL_DIR`.

The compose file reads two more variables that the service never sees:
`QI_CONFIG_DIR` (default `./config`) is the host directory mounted at `/config`,
and `QI_TIKA_HEAP` (default `2g`) sizes the Tika sidecar's Java heap.

### Throughput and limits

| Variable | Default | Meaning |
|---|---|---|
| `QI_MAX_CONCURRENT_JOBS` | `2` | scheduler worker threads |
| `QI_EMBED_CONCURRENCY` | `2` | concurrent embedding requests, globally |
| `QI_EMBED_BATCH_SIZE` | `32` | chunks per embedding request |
| `QI_EMBED_RETRIES` | `3` | retries before a run fails |
| `QI_EMBED_RPS` | `0` | token-bucket rate limit, `0` disables it |
| `QI_MAX_FILE_BYTES` | `209715200` | larger files become `skipped_too_large` |
| `QI_SHEET_ROWS` | `40` | data rows per spreadsheet chunk |
| `QI_MIN_CHARS_PER_PAGE` | `100` | scanned-PDF heuristic |
| `QI_LOCK_TIMEOUT` | `60` | seconds to wait for the collection lock |
| `QI_RUN_HISTORY_LIMIT` | `200` | runs kept per job |
| `QI_SHUTDOWN_GRACE` | `30` | seconds to let running jobs finish |

Job parallelism and embedding parallelism are separate axes on purpose: the
embeddings endpoint is one shared bottleneck no matter how many jobs run, so
four jobs may sync and extract at once while only two embed.

### Extraction

| Variable | Default | Meaning |
|---|---|---|
| `QI_TIKA_TIMEOUT` | `300` | client timeout in seconds |
| `QI_TIKA_OCR_LANGUAGE` | `deu+eng` | OCR languages (needs the `-full` image) |
| `QI_TIKA_PDF_OCR_STRATEGY` | `auto` | Tika PDF OCR strategy |
| `QI_TIKA_SNIFF_UNKNOWN` | `false` | send unknown extensions to Tika and route by detected type |

### Security

| Variable | Default | Meaning |
|---|---|---|
| `QI_REST_AUTH` | `token` | reserved axis; `token` is the only implemented value |
| `QI_METRICS_ENABLED` | `true` | expose `/metrics` |
| `QI_METRICS_AUTH` | `true` | require the bearer token on `/metrics` |
| `OIDC_ISSUER` | — | issuer URL; **unset disables the MCP endpoint** |
| `QI_OIDC_AUDIENCE` | `mcp-qdrant-ingest` | expected `aud` claim |
| `QI_OIDC_OPERATOR_ROLE` | `qdrant-ingest-operator` | required realm role |
| `QI_OIDC_JWKS_CACHE_TTL` | `3600` | discovery and JWKS cache TTL |

Do not publish the control plane on a public hostname. It is a mutation API
authenticated by a static token; its threat model is "another container on
this bridge", not the open internet.

## Format coverage

| Class | Formats | Extraction | Chunker |
|---|---|---|---|
| markdown | `.md .markdown .mdx` | direct UTF-8 read, **never Tika** | heading-aware |
| plaintext | `.txt .log .rst .adoc` | direct read | paragraph window |
| data | `.json .yaml .yml` | direct read, pretty-printed | paragraph window |
| document | `.pdf .doc .docx .odt .rtf .epub` | Tika `/rmeta/text` | paragraph window |
| spreadsheet | `.xlsx .xls .ods` / `.csv .tsv` | Tika / direct | row groups with a repeated header |
| presentation | `.pptx .ppt .odp` | Tika | one chunk per slide, deck title prefixed |
| web | `.html .htm .xml` | Tika | paragraph window |
| email | `.eml .msg` | Tika | body; attachments with `expand_embedded` |
| image | `.png .jpg .jpeg .tiff` | Tika OCR (`-full` only) | paragraph window |
| archive | `.zip .tar .7z` | not ingested | — |

Markdown bypassing Tika is a correctness requirement, not an optimisation:
Tika's plaintext handler strips the `#` markers, which are the only signal
the heading-aware chunker has.

`CHUNKER_VERSION` is a module constant that feeds `params_sha`. Bumping it in
a release forces a clean re-embedding on the next run.

## Failure scenarios

| Scenario | Behaviour |
|---|---|
| **Embeddings endpoint dies mid-run** | the batch is retried `QI_EMBED_RETRIES` times, then the run fails. Committed documents stay; **no sweep, no delete phase**. The next run resumes at the first uncommitted document |
| **Dimension conflict** | `ensure_collection` raises before any write, naming the existing dimension, the model's dimension, and the way out (`full_scope: collection`) |
| **Two jobs, one collection, different models** | a validation error at load time, visible under `GET /v1/config`. Second line of defense: `ensure_collection` compares against `_collection_meta` |
| **rclone auth failure** | non-zero exit ⇒ `status=failed`, `sync_stderr_tail` stored, the scan phase is never entered, **nothing is deleted** |
| **Tika OOM on a huge PDF** | never reached — `QI_MAX_FILE_BYTES` skips it. For smaller files the forked parser child dies, the server survives, the ingester sees a 5xx, retries twice, and records `failed_extract` for that one document |
| **Tika hangs** | `QI_TIKA_TIMEOUT` client-side; the document becomes `failed_extract` and the run continues |
| **Full run collides with a scheduled upsert** | the exclusive collection lock serialises them. The upsert waits `QI_LOCK_TIMEOUT`, then records `aborted_lock` and runs at the next tick |
| **Container restart mid-run** | SIGTERM stops cooperatively between documents and marks the run `interrupted`. SIGKILL leaves a `running` row, which is reconciled to `interrupted` at the next startup. At most one document's work is lost, and that document is detected as changed next time |
| **State volume lost** | `upsert` re-extracts and re-embeds everything (correct, expensive) and deletes nothing (an empty state means nothing vanished). `append` with `append_probe: auto` detects it and rebuilds from the source facet. `full` is unaffected by construction |
| **Cache volume lost** | rclone re-downloads with fresh modtimes ⇒ stage 1 says "changed" ⇒ stage 2 says "unchanged" ⇒ modtimes are advanced and **nothing is re-embedded** |
| **A stored secret cannot be decrypted** | the jobs that reference it are not valid (`the stored secret 'X' cannot be decrypted`), `/health` is `degraded` and names `secrets.yaml`. Usually `QI_CONNECTIONS_SECRET` changed; store the secret again |
| **`jobs.yaml` edited badly** | the previous registry keeps serving; errors under `GET /v1/config` and `/health.config_error` |
| **Job renamed** | its old points become orphans, are detected at reload, listed under `GET /v1/orphans`, and cleaned with `DELETE /v1/orphans/{job_id}?confirm=true` |
| **Qdrant unreachable at startup** | the API stays up in `degraded` state so operators can read configuration and history; jobs fail with a clear message rather than crash-looping |

## Renamed jobs and orphans

A job id is part of every point's payload and of its point ids. Renaming a
job in `jobs.yaml` therefore leaves points behind that no job will ever
sweep. The reload detects any `job_id` present in the state but absent from
the catalog and logs it; `GET /v1/orphans` lists them with collection, point
count, and state rows, and `DELETE /v1/orphans/{job_id}?confirm=true` removes
both. This is deliberately **not** exposed over MCP. The job's run history is not
part of it; see [Deleting run history](#deleting-run-history).

An orphan's job is gone from the catalog, so which connection it used is
unknown. Orphan point counts and deletions are therefore attempted across
**every** connection currently defined in `connections.yaml`; a connection
that has since been removed leaves its orphan points untouched (the state
rows are still cleaned).

## Deleting run history

Every run keeps a row and an event log in the state database. After each run a
job is cut back to its newest `QI_RUN_HISTORY_LIMIT` runs (200 by default), but
only by one of its own runs, so a job that was removed from the catalog keeps
its history for good. `DELETE /v1/jobs/{job_id}/runs` removes it on request:

```
DELETE /v1/jobs/{job_id}/runs?since=2026-01-01T00:00:00Z&until=2026-04-01T00:00:00Z&confirm=true
```

- `since` is inclusive, `until` exclusive, and both compare the time a run
  *started*. Leave both out for the whole history of the job. A `since` later
  than `until` is a 422.
- Without `confirm=true` the call is refused. With `dry_run=true` it counts and
  deletes nothing: `matched` runs and `matched_events` events would go.
- A run that is `running` is never deleted (the answer counts it as
  `skipped_running`); abort it first with `DELETE /v1/runs/{id}` and delete it
  afterwards.
- Qdrant and the `documents` table are not touched. What a job embedded is
  removed with the leftovers flow above, and deleting it is a different act:
  change detection relies on those rows.

What changes for a job that is still in the catalog: its last run and its last
successful run are the newest ones that are left, so a job with
`run_on_startup: if_missed` whose successful runs are all gone is treated as
never run and starts at the next start of the service. `last_run_id` of a
document, and the `run_id` filter of `GET /v1/jobs/{id}/documents`, can name a
run that no longer exists; that is cosmetic.

## Metrics

`GET /metrics` (Prometheus text format):

- `qdrant_ingest_runs_total{job,status}`
- `qdrant_ingest_docs_indexed_total{job}`
- `qdrant_ingest_docs_failed_total{job}`
- `qdrant_ingest_chunks_upserted_total{job}`
- `qdrant_ingest_jobs_loaded`

## Health

`/health` answers `200` whenever the process can serve, so a degraded
container is never restart-looped by its own healthcheck. Read the body:

```json
{
  "status": "degraded",
  "version": "1.0.0",
  "jobs_loaded": 0,
  "config_error": "jobs_file: jobs.yaml not found",
  "deps": {"qdrant": true, "embeddings": true, "tika": false},
  "deps_checked_at": "2026-08-14T10:58:23.112000+00:00",
  "features": ["run_progress", "documents", "validate", "delete_runs", "secret_store"]
}
```

`features` lists what this build offers beyond the original REST surface, so a
client can adapt to an older release without comparing version numbers.

`status` is `degraded` when the catalog has errors or any dependency probe
fails.

The dependency probes do **not** run on the request path. A background thread
refreshes them every 15 seconds and `/health` returns the cached snapshot, so
the endpoint answers in milliseconds no matter how slow an unreachable
dependency is. That is what keeps the container healthcheck meaningful: it
reports whether this process is serving, not whether every external system is
up. `deps_checked_at` is the timestamp of the last probe, and it is `null`
until the first one completes — dependencies are then *unknown* rather than
down, which is why a freshly started container does not report itself
degraded on that basis.

## Debugging a job

1. `GET /v1/jobs/{id}/preview` — what the filters actually match, without
   running anything.
2. `POST /v1/jobs/{id}/run {"dry_run": true}` — sync, scan, change detection,
   and extraction with a full report, but no embedding and no writes. This is
   what makes the mode semantics debuggable.
3. `GET /v1/runs/{run_id}` — counters plus the per-document event log.
   While the run works the row also carries `phase`, `files_seen`, `files_done`
   and `current`; `dry_run` marks a dry run.
4. `GET /v1/jobs/{id}/documents` — what happened to each file the job tracks:
   `indexed`, `skipped_no_text`, `skipped_too_large`, `skipped_unsupported`,
   `failed_extract` or `failed_embed`, with the last error. `status=` filters,
   `q=` searches the path, `run_id=` lists what one run touched (a file the run
   found unchanged is not touched and is not listed).
5. `POST /v1/config/validate` with `{"raw": "<catalog text>"}` — whether a
   candidate catalog would load, with the errors per job and field. Nothing is
   written and the running catalog is not touched.
