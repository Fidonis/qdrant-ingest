# Database connections (`connections.yaml`)

Every Qdrant a job may write to is declared once here, by a unique name. Jobs
reference a connection through `target.connection`; the ingester keeps one
client per connection.

The file lives next to `jobs.yaml` in the writable catalog directory
(`QI_CONNECTIONS_FILE`, default `/config/catalog/connections.yaml`). It is read
at startup, on `POST /v1/config/reload`, and by the same mtime+size poll that
watches `jobs.yaml`. A reload is transactional: a `connections.yaml` that does
not validate leaves the previous set serving, and the errors surface under
`GET /v1/config` and `/health.config_error`.

## Format

```yaml
version: 1          # required, must be 1

connections:
  - name: primary            # unique, ^[a-z0-9][a-z0-9_-]{0,63}$
    url: http://qdrant:6333  # http:// or https://
    api_key: "enc:1:…"       # optional; encrypted, see below

  - name: research
    url: https://qdrant.research.internal:6333
    # no api_key: an unauthenticated instance
```

Unknown keys are rejected. Two connections may not share a name.

## API keys are encrypted at rest

A connection's `api_key` is never stored in the clear. The web interface takes
the plaintext key, encrypts it with Fernet, and writes the `enc:1:…` token. The
loader decrypts it back for use. A literal (non-`enc:1:`) value in the field is
a validation error — set the key through the interface, not by hand.

The encryption key is derived from `QI_CONNECTIONS_SECRET` (any sufficiently
random string; the deployment tooling generates it). It is only needed to
**store or read** a key — connections without one work without it.

**Rotation:** changing `QI_CONNECTIONS_SECRET` invalidates every stored key, the
same way rotating `QI_UI_SESSION_SECRET` signs every operator out. Re-enter the
keys through the interface afterwards. The same key encrypts the stored source
credentials in `secrets.yaml` (see [jobs-yaml.md](jobs-yaml.md#secrets)), so a
rotation invalidates those as well.

## Managing connections from the web interface

`Connections` in the sidebar lists every connection with the jobs that use it.
From there:

- **New / Edit** — name, url, and an optional api-key field. On an edit the
  field is blank and left blank keeps the stored key.
- **Delete** — refused while any job still names the connection; repoint or
  remove those jobs first.
- **Test connection** — reaches the instance (`GET /collections`) with a short
  timeout and reports reachable / the error. This is the one place the service
  talks to Qdrant on a request; everywhere else the health probe runs on a
  background thread.

## Interaction with the job catalog

- `target.connection` is **required** on every job.
- A collection lives in exactly one Qdrant, so all enabled jobs serving one
  collection must name the same connection (checked at load time, alongside the
  existing "one embedding model per collection" rule).
- Orphan cleanup (`GET`/`DELETE /v1/orphans`) works across every currently
  defined connection — see [operations.md](operations.md).
