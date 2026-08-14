# Functional test harness

A fully isolated end-to-end verification of the service: a real Qdrant, a
real Apache Tika, a deterministic OpenAI-shaped embeddings stub, and the
ingest image built from this repository — no external services, no metered
embeddings.

Not collected by pytest and not run in CI (it needs Docker). Run it from the
repository root:

```bash
uv run --project src python tests/functional/verify.py
```

The driver generates the test documents (Markdown, PDF, DOCX, XLSX) into
`work/`, starts the stack, and walks through every mode:

1. Startup without `jobs.yaml` → `/health` answers 200 with `degraded`.
2. Config install + reload → three jobs; unauthenticated `/v1` calls → 401.
3. `fx-full` run → payload contract, KEYWORD indexes, `_collection_meta`
   point id.
4. Change detection on `fx-upsert`: unchanged and touch-only re-runs must
   not re-embed (asserted via the stub's request counter).
5. Generation sweep: a deleted file's points vanish after the next full run.
6. Upsert guards: vanished-source deletion, `empty_source_guard`, and the
   `max_delete_ratio` abort.
7. Append semantics: only new documents embed; changed documents are counted
   as `skipped_changed` and keep their old text.

`work/` and `config-live/` are runtime state and gitignored. The stack is
torn down with volumes at the end of a successful run; after a failure it is
left up for inspection (`docker compose down -v` in this directory cleans
up).

Host ports are chosen to avoid clashes: ingest on 18300, Qdrant on 16333,
the embeddings stub on 18090.
