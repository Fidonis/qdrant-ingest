# qdrant-ingest

Scheduled multi-source document ingestion into an existing Qdrant instance.
Maintained by **Fidonis**.

`qdrant-ingest` synchronises documents (PDF, Word, Excel, PowerPoint, Markdown,
and more) from remote or local sources (S3, WebDAV, SFTP, local directories, …),
extracts their text through an Apache Tika sidecar, chunks and embeds them
against an OpenAI-compatible embeddings endpoint, and writes the vectors into a
Qdrant collection. Ingestion jobs are declared in a single `jobs.yaml`, run on
cron schedules or manual triggers, and support three modes: `full`, `append`,
and `upsert`.

Full documentation follows with the first release.

## License

MIT — see [LICENSE](LICENSE).
