"""One ingestion run, end to end.

Invariants this module enforces:

- A document's state row is committed only after all of its chunks are
  upserted successfully; any earlier exit leaves the previous row intact, so
  the next run retries the document.
- Destructive phases (the generation sweep, the vanished-source deletion)
  run only after a clean scan — a failed or interrupted run never deletes.
- The sync step is part of the run's causal chain: a failed sync aborts the
  run before the scan phase ever sees a half-synchronised tree.
"""

import logging
import math
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from qdrant_client.models import PointStruct

from catalog.schema import JobConfig, LocalSource
from chunk import chunk_paragraphs
from config import Settings
from embed.client import EmbeddingUnavailableError
from engine.guards import check_vanished_deletion
from engine.modes import job_params_sha, sha256_file, sha256_text
from extract import ProcessedFile, TikaClient, TikaError, process_file
from sources import ScannedFile, scan_tree, sync_job
from sources.rclone import SyncResult
from state import DocumentRow, RunRow, StateStore, now_iso
from state.models import RunTrigger
from store import QdrantWriter, point_id

log = logging.getLogger("engine")

Mode = Literal["full", "append", "upsert"]
FullScope = Literal["job", "collection"]


class EmbedderProtocol(Protocol):
    def probe_dimension(self) -> int: ...

    def embed_all(self, texts: list[str], batch_size: int) -> list[list[float]]: ...


SyncFn = Callable[[JobConfig, Settings], SyncResult]
ShouldAbort = Callable[[], bool]


class JobRunner:
    """Executes single runs. Locking and scheduling live one layer above."""

    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        writer: QdrantWriter,
        tika: TikaClient,
        embedder_factory: Callable[[str], EmbedderProtocol],
        sync_fn: SyncFn | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._writer = writer
        self._tika = tika
        self._embedder_factory = embedder_factory
        self._sync: SyncFn = sync_fn if sync_fn is not None else sync_job

    # ── public entry ─────────────────────────────────────────────────────────

    def run_job(
        self,
        job: JobConfig,
        trigger: RunTrigger,
        *,
        mode: Mode | None = None,
        full_scope: FullScope | None = None,
        force: bool = False,
        dry_run: bool = False,
        skip_sync: bool = False,
        should_abort: ShouldAbort | None = None,
        sibling_job_ids: Sequence[str] = (),
    ) -> RunRow:
        effective_mode: Mode = mode or job.mode
        effective_scope: FullScope = full_scope or job.full_scope
        run = RunRow(
            run_id=str(uuid.uuid4()),
            job_id=job.id,
            mode=effective_mode,
            full_scope=effective_scope if effective_mode == "full" else None,
            trigger=trigger,
            started_at=now_iso(),
            status="running",
        )
        self._state.create_run(run)
        try:
            self._execute(
                job,
                run,
                effective_mode,
                effective_scope,
                force=force,
                dry_run=dry_run,
                skip_sync=skip_sync,
                should_abort=should_abort or (lambda: False),
                sibling_job_ids=sibling_job_ids,
            )
        except EmbeddingUnavailableError as exc:
            run.status = "failed"
            run.error = str(exc)
            self._state.add_event(run.run_id, "error", str(exc), source="embed")
        except Exception as exc:
            log.exception("[%s] run %s crashed", job.id, run.run_id)
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            self._state.add_event(run.run_id, "error", run.error)

        if run.status == "running":
            run.status = "success"
        run.finished_at = now_iso()
        self._state.update_run(run)
        self._state.prune_runs(job.id, self._settings.run_history_limit)
        return run

    # ── run phases ───────────────────────────────────────────────────────────

    def _execute(
        self,
        job: JobConfig,
        run: RunRow,
        mode: Mode,
        scope: FullScope,
        *,
        force: bool,
        dry_run: bool,
        skip_sync: bool,
        should_abort: ShouldAbort,
        sibling_job_ids: Sequence[str],
    ) -> None:
        settings = self._settings
        collection = job.target.collection

        if mode == "full" and scope == "collection" and sibling_job_ids and not force:
            run.status = "failed"
            run.error = (
                f"full_scope: collection would drop data of jobs "
                f"{', '.join(sorted(sibling_job_ids))} serving '{collection}'; "
                "re-run with force to override"
            )
            self._state.add_event(run.run_id, "error", run.error)
            return

        # Phase 1: sync — inside the causal chain of this run.
        if skip_sync or isinstance(job.source, LocalSource):
            run.sync_status = "skipped"
        else:
            sync_result = self._sync(job, settings)
            run.sync_status = "ok" if sync_result.ok else "failed"
            if sync_result.stderr_tail:
                run.sync_stderr_tail = sync_result.stderr_tail
            if not sync_result.ok:
                run.status = "failed"
                run.error = f"sync failed with exit code {sync_result.returncode}"
                self._state.add_event(run.run_id, "error", run.error, source="sync")
                return

        # Phase 2: embedding probe and collection preparation.
        model = job.embedding.model or settings.embedding_model
        embedder = self._embedder_factory(model)
        vector_dim = embedder.probe_dimension()
        run.embed_calls += 1

        if not dry_run:
            if mode == "full" and scope == "collection":
                # The supported way to change a collection's model/dimension.
                self._writer.recreate_collection(collection, vector_dim)
                self._state.delete_documents_for_collection(collection)
                self._writer.upsert_meta(collection, model, vector_dim)
            elif mode == "full":
                # Forget job state so every document re-extracts and re-embeds.
                self._state.delete_documents_for_job(job.id)
            self._writer.ensure_collection(collection, vector_dim, model)

        # Phase 3: scan.
        scan_root = (
            Path(job.source.path)
            if isinstance(job.source, LocalSource)
            else Path(settings.cache_dir) / job.source.label
        )
        files = scan_tree(scan_root, job.filters)
        run.files_seen = len(files)

        params_sha = job_params_sha(job, settings)
        max_bytes = job.filters.max_file_bytes or settings.max_file_bytes

        known_sources: set[str] = set()
        if mode == "append":
            known_sources = self._append_known_sources(job, run, collection)

        seen: set[str] = set()
        for file in files:
            if should_abort():
                run.status = "interrupted"
                run.error = "interrupted by shutdown"
                self._state.add_event(run.run_id, "warning", run.error)
                return
            source = job.source_uri(file.rel_path)
            seen.add(source)
            if mode == "append" and source in known_sources:
                self._count_append_drift(job, run, file, source)
                continue
            seen |= self._process_one(
                job,
                run,
                file,
                source,
                params_sha,
                max_bytes,
                model,
                vector_dim,
                dry_run=dry_run,
            )

        # Phase 4: destructive phases — only after a clean scan.
        if mode == "upsert" and not dry_run:
            self._delete_vanished(job, run, collection, seen, force)
            if run.status != "running":
                return
        if mode == "full" and not dry_run:
            sweep_job = None if scope == "collection" else job.id
            self._writer.sweep_stale(collection, run.run_id, sweep_job)

        if not dry_run:
            self._writer.upsert_meta(collection, model, vector_dim)

    def _append_known_sources(
        self, job: JobConfig, run: RunRow, collection: str
    ) -> set[str]:
        state_sources = self._state.list_sources(job.id)
        probe = job.append_probe
        if probe == "state":
            return state_sources
        if probe == "qdrant":
            return self._facet_sources_if_possible(collection, job.id)
        # auto: state, unless the state is empty while the collection is not —
        # that combination means the state volume was lost, and appending
        # everything again would duplicate the corpus.
        if not state_sources and self._collection_has_job_points(collection, job.id):
            message = (
                "state is empty but the collection already holds points for this "
                "job; reconstructing the known-source set from the source facet"
            )
            log.warning("[%s] %s", job.id, message)
            self._state.add_event(run.run_id, "warning", message)
            return self._facet_sources_if_possible(collection, job.id)
        return state_sources

    def _collection_has_job_points(self, collection: str, job_id: str) -> bool:
        if collection not in self._writer.collection_names():
            return False
        return self._writer.count_points(collection, job_id) > 0

    def _facet_sources_if_possible(self, collection: str, job_id: str) -> set[str]:
        if collection not in self._writer.collection_names():
            return set()
        return self._writer.facet_sources(collection, job_id)

    def _count_append_drift(
        self, job: JobConfig, run: RunRow, file: ScannedFile, source: str
    ) -> None:
        """append never updates; make silent drift from the source visible."""
        existing = self._state.get_document(job.id, source)
        if existing is not None and (
            existing.size != file.size or existing.mtime_ns != file.mtime_ns
        ):
            run.docs_skipped_changed += 1
        else:
            run.docs_unchanged += 1

    # ── per-file pipeline ────────────────────────────────────────────────────

    def _process_one(
        self,
        job: JobConfig,
        run: RunRow,
        file: ScannedFile,
        source: str,
        params_sha: str,
        max_bytes: int,
        model: str,
        vector_dim: int,
        *,
        dry_run: bool,
    ) -> set[str]:
        """Returns every source this file accounts for (itself + embedded)."""
        state_row = self._state.get_document(job.id, source)

        if file.size > max_bytes:
            if not dry_run:
                self._save_row(
                    job, run, file, source, "skipped_too_large", params_sha, content_sha="-"
                )
            return {source}

        # Stage 1: same stat and same params — untouched, zero I/O.
        if (
            state_row is not None
            and state_row.params_sha == params_sha
            and state_row.size == file.size
            and state_row.mtime_ns == file.mtime_ns
        ):
            run.docs_unchanged += 1
            return {source} | self._embedded_sources_in_state(job, source)

        content_sha = sha256_file(file.abs_path)
        run.bytes_read += file.size

        # Stage 2: touch-only change — advance the stat, do not re-embed.
        if (
            state_row is not None
            and state_row.params_sha == params_sha
            and state_row.content_sha == content_sha
        ):
            if not dry_run:
                state_row.size = file.size
                state_row.mtime_ns = file.mtime_ns
                self._state.upsert_document(state_row)
            run.docs_unchanged += 1
            return {source} | self._embedded_sources_in_state(job, source)

        try:
            processed = process_file(
                file.abs_path, file.rel_path, job.chunking, self._settings, self._tika
            )
        except TikaError as exc:
            run.docs_failed += 1
            if not dry_run:
                self._save_row(
                    job,
                    run,
                    file,
                    source,
                    "failed_extract",
                    params_sha,
                    content_sha=content_sha,
                    last_error=str(exc),
                )
            self._state.add_event(
                run.run_id, "error", f"extraction failed: {exc}", source=source
            )
            return {source}

        if processed.status == "unsupported":
            if not dry_run:
                self._save_row(
                    job,
                    run,
                    file,
                    source,
                    "skipped_unsupported",
                    params_sha,
                    content_sha=content_sha,
                    media_type=processed.media_type,
                )
            return {source}

        if processed.status == "no_text":
            # Logged once; the state row keeps it from being retried until the
            # file itself changes.
            if state_row is None or state_row.status != "skipped_no_text":
                self._state.add_event(
                    run.run_id,
                    "info",
                    "no extractable text (scanned or empty document)",
                    source=source,
                )
            if not dry_run:
                self._save_row(
                    job,
                    run,
                    file,
                    source,
                    "skipped_no_text",
                    params_sha,
                    content_sha=content_sha,
                    media_type=processed.media_type,
                )
            return {source}

        text_sha = sha256_text(processed.text)

        # Stage 3: binary changed, text identical — advance hashes, no re-embed.
        if (
            state_row is not None
            and state_row.params_sha == params_sha
            and state_row.text_sha == text_sha
            and state_row.status == "indexed"
        ):
            if not dry_run:
                state_row.size = file.size
                state_row.mtime_ns = file.mtime_ns
                state_row.content_sha = content_sha
                self._state.upsert_document(state_row)
            run.docs_unchanged += 1
            return {source} | self._embedded_sources_in_state(job, source)

        if dry_run:
            run.docs_indexed += 1
            self._state.add_event(
                run.run_id,
                "info",
                f"dry run: would index {len(processed.chunks)} chunks",
                source=source,
            )
            return {source}

        produced = {source}
        indexed = self._index_document(
            job,
            run,
            file,
            source,
            file.rel_path,
            processed.chunks,
            processed,
            params_sha,
            content_sha,
            text_sha,
            model,
        )
        if indexed and job.expand_embedded and processed.embedded:
            produced |= self._index_embedded(
                job, run, file, source, processed, params_sha, content_sha, model
            )
        return produced

    def _index_document(
        self,
        job: JobConfig,
        run: RunRow,
        file: ScannedFile,
        source: str,
        rel_path: str,
        chunks: list[str],
        processed: ProcessedFile,
        params_sha: str,
        content_sha: str,
        text_sha: str,
        model: str,
    ) -> bool:
        embedder = self._embedder_factory(model)
        batch_size = job.embedding.batch_size or self._settings.embed_batch_size
        vectors = embedder.embed_all(chunks, batch_size)
        run.embed_calls += math.ceil(len(chunks) / max(1, batch_size))

        if len(vectors) != len(chunks):
            run.docs_failed += 1
            self._save_row(
                job,
                run,
                file,
                source,
                "failed_embed",
                params_sha,
                content_sha=content_sha,
                text_sha=text_sha,
                media_type=processed.media_type,
                last_error=f"{len(chunks)} chunks but {len(vectors)} vectors",
            )
            return False

        # Shrinking documents: stale higher-index chunks go away here, inside
        # the same run.
        self._writer.delete_by_source(job.target.collection, job.id, source)

        file_mtime_iso = datetime.fromtimestamp(file.mtime_ns / 1e9, tz=UTC).isoformat()
        ingested_at = now_iso()
        file_name = rel_path.rsplit("/", 1)[-1]
        points = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            payload: dict[str, object] = {
                "text": chunk,
                "source": source,
                "ingest_job": job.id,
                "ingest_run": run.run_id,
                "collection": job.target.collection,
                "title": processed.title,
                "file_name": file_name,
                "rel_path": rel_path,
                "source_label": job.source.label,
                "chunk_index": index,
                "total_chunks": len(chunks),
                "file_type": Path(file_name).suffix.lstrip(".").lower(),
                "media_type": processed.media_type,
                "file_mtime": file_mtime_iso,
                "file_size": file.size,
                "content_sha256": content_sha,
                "ingested_at": ingested_at,
                "embedding_model": model,
            }
            if job.target.acl_tags:
                payload["acl_tags"] = list(job.target.acl_tags)
            payload.update(job.target.extra_payload)
            points.append(
                PointStruct(id=point_id(job.id, source, index), vector=vector, payload=payload)
            )

        self._writer.upsert_points(job.target.collection, points)
        run.docs_indexed += 1
        run.chunks_upserted += len(points)

        # The state row commits only now — after every chunk is upserted.
        self._save_row(
            job,
            run,
            file,
            source,
            "indexed",
            params_sha,
            content_sha=content_sha,
            text_sha=text_sha,
            media_type=processed.media_type,
            chunk_count=len(points),
            rel_path=rel_path,
        )
        return True

    def _index_embedded(
        self,
        job: JobConfig,
        run: RunRow,
        file: ScannedFile,
        parent_source: str,
        processed: ProcessedFile,
        params_sha: str,
        content_sha: str,
        model: str,
    ) -> set[str]:
        """Each attachment becomes its own document under parent!name."""
        produced: set[str] = set()
        current: set[str] = set()
        for embedded in processed.embedded:
            embedded_source = f"{parent_source}!{embedded.name}"
            current.add(embedded_source)
            chunks = chunk_paragraphs(
                embedded.text, job.chunking.words, job.chunking.overlap
            )
            if not chunks:
                continue
            sub = ProcessedFile(
                status="ok",
                chunks=chunks,
                text=embedded.text,
                media_type=embedded.media_type,
                title=embedded.name,
            )
            if self._index_document(
                job,
                run,
                file,
                embedded_source,
                f"{file.rel_path}!{embedded.name}",
                chunks,
                sub,
                params_sha,
                content_sha,
                sha256_text(embedded.text),
                model,
            ):
                produced.add(embedded_source)

        # Attachments that existed before but are gone from the container now.
        stale = {
            source
            for source in self._embedded_sources_in_state(job, parent_source)
            if source not in current
        }
        for source in sorted(stale):
            self._writer.delete_by_source(job.target.collection, job.id, source)
            self._state.delete_document(job.id, source)
            run.docs_deleted += 1
        return produced

    def _embedded_sources_in_state(self, job: JobConfig, parent_source: str) -> set[str]:
        prefix = f"{parent_source}!"
        return {
            source
            for source in self._state.list_sources(job.id)
            if source.startswith(prefix)
        }

    # ── deletion phase ───────────────────────────────────────────────────────

    def _delete_vanished(
        self,
        job: JobConfig,
        run: RunRow,
        collection: str,
        seen: set[str],
        force: bool,
    ) -> None:
        state_sources = self._state.list_sources(job.id)
        # Embedded documents vanish with their parent, never on their own.
        vanished = {
            source
            for source in state_sources - seen
            if source.split("!", 1)[0] not in seen
        }
        if not vanished:
            return
        decision = check_vanished_deletion(seen, state_sources, job.safety, force)
        if not decision.allowed:
            run.status = "aborted_guard"
            run.error = decision.reason
            self._state.add_event(run.run_id, "error", decision.reason or "guard")
            return
        for source in sorted(vanished):
            self._writer.delete_by_source(collection, job.id, source)
            self._state.delete_document(job.id, source)
            run.docs_deleted += 1

    # ── state row helper ─────────────────────────────────────────────────────

    def _save_row(
        self,
        job: JobConfig,
        run: RunRow,
        file: ScannedFile,
        source: str,
        status: str,
        params_sha: str,
        *,
        content_sha: str,
        text_sha: str | None = None,
        media_type: str | None = None,
        chunk_count: int = 0,
        last_error: str | None = None,
        rel_path: str | None = None,
    ) -> None:
        self._state.upsert_document(
            DocumentRow(
                job_id=job.id,
                collection=job.target.collection,
                source=source,
                rel_path=rel_path or file.rel_path,
                size=file.size,
                mtime_ns=file.mtime_ns,
                content_sha=content_sha,
                text_sha=text_sha,
                params_sha=params_sha,
                media_type=media_type,
                chunk_count=chunk_count,
                status=status,  # type: ignore[arg-type]
                last_error=last_error,
                last_run_id=run.run_id,
                indexed_at=now_iso(),
            )
        )
