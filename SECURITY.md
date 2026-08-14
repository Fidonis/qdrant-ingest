# Security policy

We take the security of `qdrant-ingest` seriously. Thanks for helping
us keep it safe.

## Supported versions

Security fixes are issued for the latest published `0.x` release.
Older `0.x` releases receive only critical-severity fixes on a
best-effort basis.

| Version | Status |
|---|---|
| `0.x` (latest) | ✅ supported |
| Older `0.x` | 🟡 critical fixes only |

A separate policy will be added once a stable `1.0` ships.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Please report vulnerabilities through GitHub's
[Private Vulnerability Reporting](https://github.com/Fidonis/qdrant-ingest/security/advisories/new).
This routes the report directly to the maintainers in a private
advisory.

If for some reason you cannot use the private reporting flow, contact
the maintainers at `security@fidonis.de` and we will open the private
advisory on your behalf.

Please include:

- A clear description of the vulnerability and its impact
- Steps to reproduce (a minimal proof of concept is ideal)
- The version / commit affected
- Any suggested mitigation or fix, if you have one

## What to expect

- **Acknowledgement** within 3 working days of your report.
- **Initial triage** (severity assessment, confirmation, scope) within
  10 working days.
- **Coordinated disclosure**: once a fix is ready, we publish a GitHub
  Security Advisory and a patched release. Embargo periods are agreed
  with the reporter on a case-by-case basis; 90 days is the default
  upper bound.
- **Credit**: with your permission, your name (or handle) is listed in
  the advisory and the release notes.

## Out of scope

- Vulnerabilities in third-party software listed under
  [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) — please report
  those to the respective upstream project. We will, of course, ship an
  updated dependency as soon as a fix is available.
- Issues that require attacker-controlled OIDC issuer configuration
  (configuring the service to trust a malicious identity provider is
  equivalent to letting the attacker mint tokens; this is by design).
- Issues that require write access to `jobs.yaml` or to the process
  environment — whoever controls the job catalog or the container
  environment already operates the service.
- Denial of service via resource exhaustion of the underlying Qdrant,
  Tika, or embeddings services — those boundaries are owned by the
  respective service.

Anything else, including subtle authentication and authorization bugs,
is in scope and we want to hear about it.
