# Ingestion modes

Every point carries two keyword-indexed payload fields:

| Field | Meaning |
|---|---|
| `ingest_job` | the job that wrote it — a job's scope is `ingest_job == <job.id>` |
| `ingest_run` | the run that last wrote it — the *generation* |

All three modes reduce to filter expressions over these two fields, and no
mode ever touches another job's data.

## `full` — complete revectorisation

The naive reading ("delete the collection, rebuild it") has a blind window
and destroys sibling jobs. Instead, a **generation sweep**:

```text
run_id = uuid4()
take the collection lock EXCLUSIVE
sync()                        # must succeed
ensure_collection()           # dimension check, meta check, payload indexes

if full_scope == "collection":
    refuse if other active jobs serve this collection (unless forced)
    delete + recreate the collection, forget its state rows
else:                         # full_scope == "job" (default)
    forget this job's state rows, so every document re-extracts

for each file:
    extract → chunk → embed → delete_by(source) → upsert with ingest_run = run_id
    commit the state row

on success:
    scope=job:        delete where ingest_job == job AND ingest_run != run_id
    scope=collection: delete where ingest_run != run_id
    upsert _collection_meta
release the lock
```

Properties:

- **No blind window.** Unchanged documents are overwritten under the same
  deterministic point ids; search keeps working throughout.
- **Crash-safe.** A crashed run never sweeps. Nothing is lost; at worst stale
  points survive one cycle longer.
- **Shrinking documents are handled twice over** — the per-document
  `delete_by(source)` before the upsert fixes it within a run, and the sweep
  catches anything left.
- `full_scope: collection` with a different `embedding.model` is the intended
  way to change a collection's model: only that path recreates the collection
  with a new dimension and rewrites `_collection_meta`.

`_collection_meta` is never deleted — it lives in its own collection and is
rewritten idempotently at the end of every successful run. Payload indexes
are recreated inside `ensure_collection` on every call, because deleting a
collection destroys them.

## `append` — add only

"New" means: this `source` is not known yet. Two probes:

| `append_probe` | Behaviour |
|---|---|
| `state` | the `source` is missing from the job's state rows. Cheap, the normal case |
| `qdrant` | one facet over `source` scoped to `ingest_job` rebuilds the known set from the collection itself |
| `auto` (default) | `state`, **except** when the state is empty while the collection already holds points for this job |

That `auto` exception is the important protection: an empty state with a
non-empty collection means the state volume was lost. Without the detection
the whole corpus would be appended a second time. `auto` notices, warns in
the run log, and reconstructs the known set from the facet.

`append` never updates and never deletes. A changed document is counted as
`docs_skipped_changed` in the run report, so operators can see the index
drifting away from the source — the usual surprise with append-only
pipelines.

## `upsert` — add and update

```text
run_id = uuid4()
take the collection lock SHARED
sync(); ensure_collection()

for each file:
    unchanged?  → skip (see change detection)
    otherwise   → extract → chunk → embed
                  → delete_by(ingest_job == job AND source == s) → upsert
                  → commit the state row

if the scan completed without an aborting error:
    vanished = state_sources - seen
    empty_source_guard: abort if nothing was seen but state is non-empty
    ratio guard:        abort if |vanished| / |state_sources| > max_delete_ratio
    otherwise delete each vanished source from Qdrant and from the state
```

Both guards answer the same failure: a source that looks empty for reasons
`rclone` cannot see (wrong path, unmounted volume, a typo in the prefix).
`rclone sync` additionally always receives a `--max-delete` derived from
`max_delete_ratio` — that is the first line of defense, the guards are the
second. `force` overrides the ratio guard deliberately.

## Change detection

After `rclone sync` onto a local target the local file carries the *source's*
modification time for every backend that supports modtimes. ETags and remote
checksums are not reconstructible from the local filesystem, so what is
available is `(size, mtime_ns)` and — if you pay for it — the content.

| Stage | Check | Result |
|---|---|---|
| 1 | `(size, mtime_ns)` equal to the state? | unchanged, skip. Zero I/O |
| 2 | otherwise: `sha256(file)` equal to `content_sha`? | touch-only change. Advance the stat, **do not** re-embed |
| 3 | otherwise: extract, `sha256(text)` equal to `text_sha`? | the binary changed, the text did not (a re-saved PDF). Advance the hash, **do not** re-embed |
| 4 | `params_sha` differs? | **always** re-embed, regardless of stages 1–3 |

Stage 2 matters more than it looks: losing the cache volume makes rclone
re-download everything with fresh modtimes. Without it, that would re-embed
the entire corpus — metered and rate-limited.

Stage 4 covers `params_sha = sha256(model, strategy, words, overlap,
CHUNKER_VERSION, tika options, sheet rows, …)`. Raising `words` from 400 to
512 therefore re-chunks the corpus correctly on the next run, with no manual
full reindex.

## Behaviour under partial failure

| | scan dies mid-run | one document fails extraction | embeddings endpoint dies |
|---|---|---|---|
| `full` | committed documents stay; **no sweep**; stale points survive to the next full run | row with `failed_extract`; **not** treated as vanished | run aborts after retries; no sweep; the next run catches up |
| `append` | committed documents stay | row `failed_extract`, no retry until the file changes | run aborts; the rest follows next run |
| `upsert` | committed documents stay; **the delete phase is skipped** | row `failed_extract`; excluded from `vanished` | run aborts; the delete phase is skipped |

The two invariants behind that table: a document's state row is committed
only after all of its chunks are upserted, and destructive phases run only
after a clean scan.

## Overlap protection

Three layers that do not replace each other:

1. **`max_instances=1` + `coalesce=True`** — a cron firing during the same
   running job is dropped; a burst of missed firings collapses into one.
2. **Non-blocking job lock** — manual triggers bypass the scheduler
   entirely. A second trigger gets REST `409 {"error": "already_running"}`,
   or with `queue: true` exactly one follow-up run is enqueued.
3. **Per-collection reader/writer lock** — `full` runs take it exclusively,
   everything else shared. This is a correctness requirement, not a nicety:
   points written by a concurrent upsert would carry a foreign `ingest_run`
   and be deleted by the full run's sweep. A waiting upsert gives up after
   `QI_LOCK_TIMEOUT` with `status=aborted_lock` and runs at the next tick.
