# Contributing to qdrant-ingest

Thanks for considering a contribution to qdrant-ingest! This document describes how to file issues, propose changes, and what is expected from contributors.

## Code of Conduct

Please follow common sense: be kind, assume good intent, and keep discussions focused on the project.

## Where to ask questions

- **General questions, ideas, brainstorming** → [GitHub Discussions](https://github.com/Fidonis/qdrant-ingest/discussions)
- **Bugs, feature requests, documentation issues** → [GitHub Issues](https://github.com/Fidonis/qdrant-ingest/issues), using the [issue templates](https://github.com/Fidonis/qdrant-ingest/issues/new/choose)
- **Security vulnerabilities** → use [Private vulnerability reporting](https://github.com/Fidonis/qdrant-ingest/security/advisories/new) instead of a public issue

## Reporting bugs and requesting features

We use GitHub Issue Forms. When you click *New issue*, you'll see these entry points:

- **Bug report** — for reproducible bugs
- **Feature request** — for new functionality or enhancements
- **Documentation** — for missing, wrong, or unclear docs
- **Question / Discussion** (link) — redirects to Discussions

Each form prefills the right labels and structure, so please use them rather than blank issues.

## Pull request workflow

1. Open or comment on the issue you intend to work on, so duplicate effort can be avoided.
2. Create a feature branch from `main`. Branch naming:
   - `feat/<short-name>` — new features
   - `fix/<short-name>` — bug fixes
   - `docs/<short-name>` — documentation
   - `refactor/<short-name>` — refactoring without behavior change
   - `test/<short-name>` — adding or fixing tests
   - `ci/<short-name>` — CI/CD changes
   - `chore/<short-name>` — maintenance
3. Make your change in small, reviewable commits.
4. Open a pull request against `main`. The PR template is filled in automatically; please complete each section, especially **Linked issues**, **Type of change**, and **Test plan**.
5. CI runs lint, type-check, test, and PR-title checks. Address any failures. Once green, request a review.
6. PRs are merged via **Squash & Merge**. The PR title becomes the squash commit message — make sure it follows Conventional Commits (see below).

## Commit and PR title convention

We use [Conventional Commits](https://www.conventionalcommits.org/) for PR titles, which are squashed into the merge commit. This drives release notes (via release-drafter) and version bumps automatically.

Format: `<type>[(<scope>)][!]: <subject>`

| Type | Use for |
|---|---|
| `feat` | New user-facing feature (minor version bump) |
| `fix` | Bug fix (patch version bump) |
| `docs` | Documentation changes |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `style` | Formatting only, no code change |
| `test` | Adding or fixing tests |
| `ci` | CI configuration |
| `build` | Build system / dependencies |
| `chore` | Maintenance tasks |
| `revert` | Reverts a previous commit |

Examples:

- `feat: add an sftp source type`
- `fix(engine): skip the sweep when the scan aborts`
- `docs: clarify the append probe semantics`
- `feat!: drop support for Python 3.11`

A `!` after the type or scope marks a **breaking change** and triggers a major version bump.

The subject must be lowercase, in imperative mood (*"add"*, not *"added"* or *"adds"*), without a trailing period.

A CI check enforces this on PR titles.

## Code style

Linters run on every push and pull request:

- **Python** — [`ruff`](https://docs.astral.sh/ruff/) for linting and import ordering, [`mypy`](https://mypy-lang.org/) (strict mode) for type checking
- **YAML** — [`yamllint`](https://yamllint.readthedocs.io/)

Run the checks locally before pushing:

```bash
# YAML lint — from the repository root
yamllint .

# Python lint + type check — from src/
cd src
uv run ruff check ..
uv run mypy .

# Tests — from src/
uv run pytest -q
```

Ruff and mypy configuration live in `src/pyproject.toml`; the yamllint configuration is in `.yamllint`.

## Local development

See the [README](./README.md) for full setup. The short version:

```bash
cd src
uv sync                        # create src/.venv with dependencies
uv run python main.py
```

The service expects a reachable Qdrant, an OpenAI-compatible embeddings endpoint, and an Apache Tika server; `tests/functional/` contains a self-contained Docker Compose stack that provides all three for end-to-end verification. `src/` is a uv project with its own `pyproject.toml` and `.venv` — there is no virtual environment in the repository root.

## License of your contributions (Inbound = Outbound)

This project is published under the [MIT license](LICENSE). By submitting a pull request, comment with a code suggestion, or any other contribution to this repository, **you agree that your contribution is licensed under the same MIT license** that the project itself uses ("Inbound = Outbound"). No separate Contributor License Agreement (CLA) is required.

You also confirm that:

- You have the right to license the contribution under MIT — either because you wrote it yourself, or because the original author has explicitly licensed it under a compatible permissive license (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC) and you preserve the original attribution.
- Your contribution does not knowingly infringe a third party's copyright, patent or trademark.

### Copyleft licenses are not accepted

To keep the project freely redistributable under MIT, **contributions containing or directly depending on code under a copyleft license are not accepted** without prior written approval from the Fidonis management. This includes (non-exhaustive list):

- GNU General Public License (GPL), any version
- GNU Lesser General Public License (LGPL), any version
- GNU Affero General Public License (AGPL), any version
- Mozilla Public License (MPL) 2.0 when used in a way that would make the project a covered work
- Server Side Public License (SSPL)
- Open Software License (OSL)
- European Union Public Licence (EUPL)

If you believe an exception is justified (for example, an isolated build-time tool that does not ship with the binary), please open an issue first to discuss it before opening the PR. The CI license-check workflow will fail the PR if a copyleft dependency is added.

## Trademarks

"Fidonis" and "qdrant-ingest" are trademarks of Fidonis GmbH (in Gründung). See [TRADEMARK.md](TRADEMARK.md) for the rules on how you may and may not use them. The MIT license grants you broad rights to the code; the trademark notice governs the project's name and identity.

---

Thanks again for contributing!
