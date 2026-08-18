# The operator web interface

A browser interface for the people who run ingestion: the job catalog, the
runs, the collections, and the leftovers. It is served by the same process as
the REST and MCP planes, mounted at `QI_UI_PATH` (default `/ui`).

It is optional. Without its configuration the process starts exactly as it did
before — the interface simply is not mounted, and the log says why.

---

## Enabling it

Four values must be present, or the interface stays off:

| Variable | Meaning |
|---|---|
| `OIDC_ISSUER` | The realm the login goes to. Shared with the MCP plane. |
| `QI_UI_PUBLIC_URL` | Where a browser reaches this service, e.g. `https://ingest.example.com`. The redirect URI is derived from it. |
| `QI_UI_CLIENT_SECRET` | Secret of the confidential OIDC client. |
| `QI_UI_SESSION_SECRET` | Signing key for the session cookie. Rotating it signs everyone out. |

Optional: `QI_UI_ENABLED` (default `true`), `QI_UI_PATH` (default `/ui`),
`QI_UI_CLIENT_ID` (default `qdrant-ingest-ui`), `QI_UI_SESSION_TTL` (seconds,
default `28800`).

The redirect URI is `${QI_UI_PUBLIC_URL}${QI_UI_PATH}/auth/callback`. It must
match the client's registered redirect URI exactly.

### The Keycloak client

A confidential client with the standard flow enabled, no service account, and
that one redirect URI. Authorization is the **existing**
`QI_OIDC_OPERATOR_ROLE` realm role (default `qdrant-ingest-operator`) — the
same one the MCP plane requires. An account without it is refused at the
callback, before a session cookie is issued.

There is deliberately no second, read-only role. Reading the catalog tells you
where the documents come from and what the credentials are named; that is not
a meaningfully lower privilege than driving a reindex.

---

## What it does not do

**It never writes `.env`.** Source credentials live in the environment, and
`jobs.yaml` may only reference them as `${env:QI_SECRET_<NAME>}`. The editor
offers the `QI_SECRET_*` variables that are set as a choice; it cannot add one,
because the bundle directory holding the `.env` is mounted read-only. Adding a
credential stays an operator task outside this interface.

**A session is not a way into the other planes.** The session cookie is scoped
to `QI_UI_PATH`, so a request to `/v1` or `/mcp` does not even carry it. Those
two keep their own guards: a static bearer token and a Keycloak token with the
operator role.

**It is not meant to be proxied wholesale.** Publish `/ui` and nothing else.
`/v1` is a mutation API behind a static token and has no business on a public
hostname.

---

## Where the catalog lives

`QI_JOBS_FILE`, default `/config/catalog/jobs.yaml`.

The catalog moved into its own subdirectory because that subdirectory is the
only part of the config bundle the container may write. The bundle root holds
the `.env` — the Qdrant api-key, the REST token, every source credential — and
stays mounted read-only. A writable bundle root would let the ingester rewrite
its own credentials.

### An older installation

If `/config/catalog/jobs.yaml` does not exist but `/config/jobs.yaml` does, the
old file is served, read-only, and a banner offers to copy it across. Nothing
happens automatically: the copy is a click, and the old file is left in place
afterwards rather than deleted.

### How a write happens

1. The candidate is staged next to the target as `.jobs.yaml.tmp`.
2. It is validated by `catalog.loader.load_catalog` — the same function the
   reload path uses, so the YAML parse, the per-job schema, the secret
   references and the cross-job rules all apply.
3. On any error the staged file is removed and the errors come back to the
   form. **The file on disk is not touched.**
4. On success the previous contents are copied to `jobs.yaml.bak` and the
   staged file is renamed over the target — atomically, within one directory.
5. The engine reloads synchronously, so the response already shows the result
   rather than waiting for the `QI_JOBS_RELOAD_INTERVAL` poll.

### The form and comments

The raw editor writes exactly the bytes you type, comments included. The job
form re-serialises the document through `yaml.safe_dump`, which **drops
comments and blank lines**. Both are offered because both are wanted: the form
for the common case, the raw editor when the file carries prose worth keeping.

---

## Running as the host user

The container writes into a host bind mount, so its uid has to match the owner
of that directory. The image builds its `app` user with `ARG APP_UID` /
`ARG APP_GID`, both defaulting to `1000`, and the Compose file passes the host
values through `user:`.

If the host uid is not 1000 **and** the `qi-cache` / `qi-state` volumes already
exist, they were created with the old ownership. The `/data` mount points are
group-writable, which covers the common case; otherwise chown the volumes once
or recreate them.

---

## Look and feel: the vendored brand layer

The interface shares its visual identity with `papaia-manager`: the same two
daisyUI themes, the same self-hosted fonts, the same mark. There is no shared
package — the files are **copied**, and each carries a stamp:

```
/* fidonis-brand: 1 -- vendored verbatim from Fidonis/papaia-manager. */
```

| File | Contents |
|---|---|
| `docker/tailwind.config.js` | The `fidonis-light` / `fidonis-dark` themes and the font families |
| `docker/tailwind.brand.css` | Tailwind directives, `@font-face`, font binding, `.brand-mark-ink`, `color-scheme` |
| `docker/tailwind.app.css` | The application shell — sidebar geometry. Adapted per interface, not shared verbatim |
| `src/ui/templating.py` | `asset_url()` fingerprinting |
| `src/ui/templates/base.html` | `<head>`, the brand mark, the theme toggle |

`tests/test_brand_layer.py` fails if a file loses its stamp or the stamps
disagree — that catches a half-finished copy. It cannot see the other
repository, so the cross-repo half is a rule rather than a check:

> **A brand change is finished when both interfaces carry it in the same
> revision.** Bump the stamp in both, in the same milestone.

One deliberate deviation from the papaia-manager copy: the `@font-face` URLs
are relative (`fonts/files/…`) rather than rooted at `/static`. This interface
is a mounted sub-application, so an absolute path would resolve against the
site root and miss. Relative resolves identically in both, and papaia-manager
should adopt it to restore byte-identity.

Extract the layer into a package when a third consumer appears. Two justify
copying; three do not.

### Building the stylesheet

`app.css` is generated *from the templates*, so it only exists after a Docker
build. To reproduce it without one:

```bash
npm install --save-dev tailwindcss@3.4.17 daisyui@4.12.23
cat docker/tailwind.brand.css docker/tailwind.app.css > tailwind.input.css
cp -r src/ui/templates ./templates
npx tailwindcss -c docker/tailwind.config.js -i tailwind.input.css -o src/ui/static/app.css --minify
```

`htmx.min.js`, `alpine.min.js`, `app.css` and `fonts/` are build artifacts and
are not committed. The interface renders without them — unstyled, but it
renders; `asset_url()` falls back to an unfingerprinted path rather than
failing, because a missing stylesheet must not take the interface down.

---

## Related documents

- [`jobs-yaml.md`](jobs-yaml.md) — the catalog schema the form is derived from
- [`modes.md`](modes.md) — what `append`, `upsert` and `full` actually do
- [`operations.md`](operations.md) — the REST control plane
