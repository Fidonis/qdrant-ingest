# The job catalog (`jobs.yaml`)

One file declares every ingestion job. It is read at startup, on
`POST /v1/config/reload`, and by an mtime+size poll every
`QI_JOBS_RELOAD_INTERVAL` seconds (`0` disables the poll). There is no
inotify watch — inotify propagation across bind mounts is unreliable.

## Reload semantics

Parse → validate → build the new registry → diff it against the scheduler.

- A **running job is never interrupted**; a changed definition takes effect
  at its next firing.
- If validation fails, the **previous registry keeps serving**. Errors appear
  under `GET /v1/config` and in `/health.config_error`. Not running at all
  because of a typo would be worse than running the last valid catalog.
- At the very first load there is no previous registry, so the valid subset
  is accepted and the failing jobs are reported.

## Secrets

Every secret-typed field accepts exactly one form:

```yaml
secret_access_key: ${env:QI_SECRET_S3_SECRET_KEY}
```

A literal in such a field is a hard validation error naming the job and the
field. Only names matching `QI_SECRET_[A-Z0-9_]+` resolve, so a manipulated
catalog can read neither `QI_QDRANT_API_KEY` nor `QI_API_TOKEN`.

## Top-level structure

```yaml
version: 1          # required, must be 1

defaults:           # merged under every job; job keys win
  embedding: {...}
  chunking:  {...}
  filters:   {...}
  schedule:  {...}
  safety:    {...}

jobs:
  - id: ...
```

## Job fields

| Field | Default | Meaning |
|---|---|---|
| `id` | — | lowercase slug, unique, appears in every point's payload |
| `enabled` | `true` | disabled jobs are neither scheduled nor validated against siblings |
| `description` | `""` | free text |
| `source` | — | see below |
| `filters` | `{}` | `include` / `exclude` globs, `max_file_bytes` |
| `target` | — | `collection`, `acl_tags`, `extra_payload` |
| `mode` | — | `full`, `append`, or `upsert`, see [modes.md](modes.md) |
| `full_scope` | `job` | `job` or `collection` (full runs only) |
| `append_probe` | `auto` | `auto`, `state`, or `qdrant` (append runs only) |
| `schedule` | `{}` | `cron` *or* `every`, plus timezone and jitter |
| `chunking` | `{}` | `strategy`, `words`, `overlap` |
| `embedding` | `{}` | `model`, `batch_size` |
| `safety` | `{}` | `max_delete_ratio`, `empty_source_guard` |
| `mcp_allow_full` | `false` | may an assistant trigger a full run for this job |
| `expand_embedded` | `false` | index mail attachments as separate documents |
| `source_template` | `{scheme}://{label}/{rel_path}` | shape of the payload `source` |

Unknown keys are rejected everywhere, so a typo surfaces as a named error
instead of a silently ignored setting.

## Source types

`local` is scanned in place; every other type is an rclone backend, which
makes a new source a configuration question rather than a code change.

| Type | Required | Secret fields |
|---|---|---|
| `local` | `path` (below `/data/local`) | — |
| `s3` | `bucket` | `access_key_id`, `secret_access_key` |
| `webdav` | `url` | `pass` |
| `sftp` | `host` | `pass`, `key_file` (PEM content, not a path) |
| `smb` | `host`, `share` | `pass` |
| `ftp` | `host` | `pass` |
| `gdrive` | — | `service_account_json`, `token` |
| `azureblob` | `account`, `container` | `key`, `sas_url` |
| `http` | `url` | — |

Every source takes a `label` (the authority in the `source` URI) and
optional `rclone_flags`.

## Filters

```yaml
filters:
  include: ["**/*.pdf", "**/*.docx"]
  exclude: ["**/drafts/**", "**/~$*"]
  max_file_bytes: 209715200
```

`*` matches within one path segment, `?` one character, `**` crosses
segments, and a leading `**/` also matches zero segments. Excludes win; a
non-empty `include` list acts as a whitelist. The same patterns become
rclone `--filter` rules, so the sync and the scan see the same file set by
construction.

## Scheduling

```yaml
schedule:
  cron: "0 2 * * *"        # or: every: 15m
  timezone: Europe/Berlin
  jitter_seconds: 30
  misfire_grace_seconds: 300
  run_on_startup: if_missed  # never | if_missed | always
```

`cron` and `every` are mutually exclusive; omitting both makes the job
manual-only. `if_missed` fires once at startup when the last success is older
than 1.5× the nominal interval — that catches up a nightly window the
container slept through.

## Cross-job validation

Checked at load time, before any run:

- job ids are unique;
- no job targets a system collection;
- two enabled jobs serving the same collection must use different `label`s,
  so their `source` URIs stay disjoint;
- all enabled jobs serving one collection must agree on the embedding model —
  a collection records exactly one model.

## Worked example

```yaml
version: 1

defaults:
  embedding:
    model: nomic-embed-text
    batch_size: 32
  chunking:
    words: 400
    overlap: 50
  filters:
    exclude: ["**/.DS_Store", "**/*.tmp", "**/*.part", "**/~$*", "**/.git/**"]
  schedule:
    timezone: Europe/Berlin
    jitter_seconds: 30
  safety:
    max_delete_ratio: 0.25
    empty_source_guard: true

jobs:

  # S3 bucket into a shared knowledge base, nightly upsert.
  - id: acme-reports
    description: "Quarterly reports from the corporate S3 bucket"
    source:
      type: s3
      label: acme-reports
      bucket: acme-corp-reports
      prefix: published/
      region: eu-central-1
      access_key_id: ${env:QI_SECRET_S3_ACCESS_KEY}
      secret_access_key: ${env:QI_SECRET_S3_SECRET_KEY}
      rclone_flags: ["--s3-no-check-bucket"]
    filters:
      include: ["**/*.pdf", "**/*.docx", "**/*.xlsx", "**/*.pptx"]
      exclude: ["**/drafts/**"]
    target:
      collection: corporate-knowledge
      acl_tags: ["dept:finance", "confidentiality:internal"]
      extra_payload:
        origin: "s3"
    mode: upsert
    schedule:
      cron: "0 2 * * *"
    chunking:
      words: 512
      overlap: 64

  # WebDAV into the same collection, a different slice of it.
  - id: hr-policies
    source:
      type: webdav
      label: nextcloud-hr
      url: https://cloud.example.com/remote.php/dav/files/svc-ingest/HR
      vendor: nextcloud
      user: svc-ingest
      pass: ${env:QI_SECRET_NEXTCLOUD_APP_PASSWORD}
    filters:
      include: ["**/*.md", "**/*.pdf", "**/*.docx"]
    target:
      collection: corporate-knowledge
      acl_tags: ["dept:hr", "confidentiality:internal"]
    mode: upsert
    schedule:
      every: 30m
    mcp_allow_full: false

  # Local mount, its own collection, weekly rebuild.
  - id: ops-runbooks
    source:
      type: local
      label: ops-runbooks
      path: /data/local/runbooks
    filters:
      include: ["**/*.md", "**/*.txt", "**/*.csv"]
    target:
      collection: ops-runbooks
    mode: full
    full_scope: job
    schedule:
      cron: "0 4 * * 0"
    chunking:
      strategy: markdown
    mcp_allow_full: true

  # SFTP archive, append-only, manual trigger only.
  - id: legal-archive
    source:
      type: sftp
      label: legal-archive
      host: sftp.partner.example.com
      user: fidonis
      key_file: ${env:QI_SECRET_SFTP_PRIVATE_KEY}
      path: /export/legal
    target:
      collection: legal-archive
      acl_tags: ["dept:legal", "confidentiality:restricted"]
    mode: append
    append_probe: auto
    schedule: {}
```
