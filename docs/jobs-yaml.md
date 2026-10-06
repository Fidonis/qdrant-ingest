# The job catalog (`jobs.yaml`)

One file declares every ingestion job. It is read at startup, on
`POST /v1/config/reload`, and by an mtime+size poll every
`QI_JOBS_RELOAD_INTERVAL` seconds (`0` disables the poll). There is no
inotify watch — inotify propagation across bind mounts is unreliable.

Each job names the Qdrant it writes to through `target.connection`; the
connections themselves are declared in a sibling `connections.yaml` and
managed from the web interface — see [connections.md](connections.md). The
same poll picks up changes to either file.

## Reload semantics

Parse → validate → build the new registry → diff it against the scheduler.

- A **running job is never interrupted**; a changed definition takes effect
  at its next firing.
- A job whose **schedule did not change keeps its timer**. Only a changed
  schedule is applied again, so editing the catalog (or reloading it) does not
  push back an `every:` job by a whole interval, and a paused job stays
  paused.
- If validation fails, the **previous registry keeps serving**. Errors appear
  under `GET /v1/config` and in `/health.config_error`. Not running at all
  because of a typo would be worse than running the last valid catalog.
- At the very first load there is no previous registry, so the valid subset
  is accepted and the failing jobs are reported.

## Secrets

Every secret-typed **source** field accepts exactly one form:

```yaml
secret_access_key: ${env:QI_SECRET_S3_SECRET_KEY}
```

A literal in such a field is a hard validation error naming the job and the
field. Only names matching `QI_SECRET_[A-Z0-9_]+` resolve, so a manipulated
catalog can read neither the connection keys nor `QI_API_TOKEN`.

Qdrant api-keys are handled differently: they live in `connections.yaml`,
encrypted at rest — see [connections.md](connections.md).

### Where the value comes from

A reference is answered from the **process environment first** and from the
**encrypted secret store second**. Both homes use the same reference, so a file
written for the environment keeps working and nothing about the syntax changes.
A variable that is set but empty does not hide a stored value.

The store is `secrets.yaml` next to `jobs.yaml` (`QI_SECRETS_FILE`, default
`/config/catalog/secrets.yaml`):

```yaml
version: 1
secrets:
  - name: QI_SECRET_S3_KEY
    value: enc:1:gAAAA...
```

- Names follow the same rule as the references: `QI_SECRET_[A-Z0-9_]+`.
- Values are stored encrypted like the connection api-keys (Fernet, the key is
  derived from `QI_CONNECTIONS_SECRET`). A plaintext value is a validation
  error, and so is an unknown key.
- The file is read when a secret is needed, and the catalog poll watches it:
  a secret that is added makes the jobs that were waiting for it valid
  without a restart. Nothing has to be put into the container environment.
- **Rotating `QI_CONNECTIONS_SECRET` makes every stored value unreadable**, the
  same as it does for the connection api-keys. A job that references an
  unreadable secret is reported as such (`the stored secret 'X' cannot be
  decrypted`), not as "not set", and `/health` is `degraded` while the store
  has an unreadable entry.

What this does and does not protect: the key sits in the same `.env` as
`QI_API_TOKEN`, and a backup carries both files. The store keeps a credential
out of casual sight (a copy of the file, a diff, a screenshot, a log). It does
not keep it from someone who can read both files, which is the same level as
the api-keys in `connections.yaml`.

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
| `target` | — | `collection`, `connection` (**required** — a name from `connections.yaml`), `acl_tags`, `extra_payload` |
| `mode` | — | `full`, `append`, or `upsert`, see [modes.md](modes.md) |
| `full_scope` | `job` | `job` or `collection` (full runs only) |
| `append_probe` | `auto` | `auto`, `state`, or `qdrant` (append runs only) |
| `schedule` | `{}` | `cron` *or* `every`, plus timezone and jitter |
| `chunking` | `{}` | `strategy`, `words`, `overlap` |
| `embedding` | `{}` | `model`, `batch_size`. `model` comes from here or from `defaults.embedding.model`; there is no environment fallback, and an enabled job that resolves to no model is a load error |
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

`cron` is parsed by APScheduler's `from_crontab`, whose day-of-week field
counts **from Monday**: `0` is Monday, `6` is Sunday, and `7` is rejected. The
names `mon`–`sun` work too and are less easy to misread.

The web form builds this block for you from a mode picker and a time — see
[the schedule builder](ui.md#the-schedule-builder) — and drops to a raw `cron`
field for expressions the presets do not cover.

## Cross-job validation

Checked at load time, before any run:

- job ids are unique;
- no job targets a system collection;
- every `target.connection` names a connection that exists in `connections.yaml`;
- two enabled jobs serving the same collection must use different `label`s,
  so their `source` URIs stay disjoint;
- all enabled jobs serving one collection must agree on the embedding model —
  a collection records exactly one model;
- all enabled jobs serving one collection must name the same `connection` —
  a collection lives in exactly one Qdrant.

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
      connection: primary
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
      connection: primary
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
      connection: primary
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
      connection: primary
      acl_tags: ["dept:legal", "confidentiality:restricted"]
    mode: append
    append_probe: auto
    schedule: {}
```
