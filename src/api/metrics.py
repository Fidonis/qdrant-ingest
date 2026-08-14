"""Prometheus metrics for runs and documents."""

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from state import RunRow


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.runs_total = Counter(
            "qdrant_ingest_runs_total",
            "Finished ingestion runs",
            labelnames=("job", "status"),
            registry=self.registry,
        )
        self.docs_indexed_total = Counter(
            "qdrant_ingest_docs_indexed_total",
            "Documents indexed",
            labelnames=("job",),
            registry=self.registry,
        )
        self.docs_failed_total = Counter(
            "qdrant_ingest_docs_failed_total",
            "Documents that failed extraction or embedding",
            labelnames=("job",),
            registry=self.registry,
        )
        self.chunks_upserted_total = Counter(
            "qdrant_ingest_chunks_upserted_total",
            "Chunks upserted into Qdrant",
            labelnames=("job",),
            registry=self.registry,
        )
        self.jobs_loaded = Gauge(
            "qdrant_ingest_jobs_loaded",
            "Jobs in the active catalog",
            registry=self.registry,
        )

    def record_run(self, run: RunRow) -> None:
        self.runs_total.labels(job=run.job_id, status=run.status).inc()
        if run.docs_indexed:
            self.docs_indexed_total.labels(job=run.job_id).inc(run.docs_indexed)
        if run.docs_failed:
            self.docs_failed_total.labels(job=run.job_id).inc(run.docs_failed)
        if run.chunks_upserted:
            self.chunks_upserted_total.labels(job=run.job_id).inc(run.chunks_upserted)

    def render(self) -> bytes:
        return generate_latest(self.registry)
