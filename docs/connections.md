# Database connections (`connections.yaml`)

Every Qdrant a job may write to is declared once here, by a unique name. Jobs
reference a connection through `target.connection`; the ingester keeps one
client per connection.

The file lives next to `jobs.yaml` in the catalog directory
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

A connection's `api_key` is never stored in the clear. It is an `enc:1:…`
token: the plaintext key encrypted with Fernet. The loader decrypts it back for
use. A literal (non-`enc:1:`) value in the field is a validation error.

`papaia-manager` writes the token for you when you enter a key on its
Connections page. Without it, produce one with the service's own code, in an
environment where `QI_CONNECTIONS_SECRET` is set. Against a running container:

```bash
docker compose -f docker/docker-compose.yml exec qdrant-ingest python -c \
  "import getpass, os; from connections.crypto import encrypt; print(encrypt(getpass.getpass('api-key: '), os.environ['QI_CONNECTIONS_SECRET']))"
```

From a source checkout, run the same code in `src/` with
`QI_CONNECTIONS_SECRET` exported (`uv run python -c "…"`). The key is read
from the prompt, so it stays out of the shell history. Paste the printed
`enc:1:…` value into `api_key`.

The encryption key is derived from `QI_CONNECTIONS_SECRET` (any sufficiently
random string; the deployment tooling generates it). It is only needed to
**store or read** a key — connections without one work without it.

**Rotation:** changing `QI_CONNECTIONS_SECRET` invalidates every stored key.
Encrypt the keys again with the new secret afterwards. The same key encrypts
the stored source credentials in `secrets.yaml` (see
[jobs-yaml.md](jobs-yaml.md#secrets)), so a rotation invalidates those as well.

## Interaction with the job catalog

- `target.connection` is **required** on every job.
- A collection lives in exactly one Qdrant, so all enabled jobs serving one
  collection must name the same connection (checked at load time, alongside the
  existing "one embedding model per collection" rule).
- Orphan cleanup (`GET`/`DELETE /v1/orphans`) works across every currently
  defined connection — see [operations.md](operations.md).
