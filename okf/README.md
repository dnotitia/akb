# AKB × Open Knowledge Format (OKF)

[**Open Knowledge Format (OKF)**](https://github.com/GoogleCloudPlatform/knowledge-catalog)
is a vendor-neutral spec (v0.1, from Google Cloud) for sharing curated knowledge
with AI agents. An OKF *bundle* is just a directory tree of markdown files, each
with a YAML frontmatter block whose **only required field is `type`** — no SDK,
no database, no server. "If you can `cat` a file, you can read OKF; if you can
`git clone` a repo, you can ship it."

AKB and OKF are **complementary**, in the same way MCP and OKF are: OKF
standardises *how curated knowledge is written down*; AKB is a *platform* that
stores, versions, searches, governs, and serves that knowledge to agents over
MCP and REST. This directory makes AKB a first-class OKF citizen — AKB can
**export** any vault as a conformant OKF bundle, and ships a standalone
**conformance validator** anyone can run against any bundle.

## Why AKB is already ~an OKF bundle

An AKB vault is stored as a per-vault git repo of `.md` files with YAML
frontmatter, and a document's identity is its path — exactly OKF's model
(`tables/users.md` → concept ID `tables/users`). AKB independently arrived at
OKF's core design before the spec existed; the remaining gaps are naming and a
couple of reserved files, which the exporter closes.

| OKF v0.1 | AKB native | Exporter does |
| --- | --- | --- |
| `type` (**required**) | `type` (defaults `note`) | passthrough |
| `title` | `title` | passthrough |
| `description` | `summary` | **rename** `summary` → `description` |
| `resource` (asset URI) | `akb://` URI | emit `resource` = `akb://…` |
| `tags` | `tags` | passthrough |
| `timestamp` (ISO 8601) | `created_at` / `updated_at` | emit `timestamp` = `updated_at` |
| *(additional keys allowed)* | `status`, `domain`, … | preserved as extra keys |
| `index.md` (progressive disclosure) | — (lives in PG) | **generated** |
| `log.md` (changelog) | — (lives in `git log`) | **generated** |
| root `okf_version` | — | **generated** (`0.1`) |

### Documents, tables, and files

OKF bundles are markdown-only and represent an asset by a *concept document*
(its description + a `resource` pointer), not by its bytes or rows. AKB's
tri-store maps onto that cleanly:

- **documents** → OKF concept docs (1:1).
- **tables** → a concept doc with a `# Schema` section (columns) + `resource`;
  the rows stay in AKB (OKF carries the schema, like its BigQuery-table example).
- **files** → a concept doc referencing the asset via `resource` + mime/size;
  the bytes stay in AKB.

So an AKB → OKF export is a faithful *catalog* of the vault at OKF's intended
abstraction level, not a lossy dump.

## Conformance status

AKB-authored bundles satisfy all three OKF v0.1 **MUST** rules (every
non-reserved `.md` has a parseable YAML frontmatter block with a non-empty
`type`; reserved files follow their structure). The validator in this repo
proves it mechanically — see below. The recommended-field name differences
(`summary` vs `description`, `created_at`/`updated_at` vs `timestamp`) are
**SHOULD**-level and are reconciled by the exporter.

## Usage

The implementation lives in [`backend/app/services/okf.py`](../backend/app/services/okf.py)
(pure, unit-tested in `backend/tests/test_okf_unit.py`) and is exposed via the
backend CLI.

```bash
# Validate any directory tree against OKF v0.1
python -m app.cli okf-validate path/to/bundle/

# Export an AKB vault git worktree as an OKF bundle, then self-validate
python -m app.cli okf-export --from-git /data/vaults/_worktrees/<vault> \
    --vault <vault> --out ./okf-out/
```

A small, hand-written, conformant example bundle lives in
[`sample-bundle/`](sample-bundle/) — run the validator against it to see a green
report.

## Feedback to the OKF project

AKB is an independent implementation of the OKF pattern at platform scale; our
operational notes (type vocabularies, typed relationships/graph edges beyond
plain links, RBAC and hybrid search as the layer *above* the format) are offered
upstream as spec feedback, not as a competing format. See the OKF repo's issues.
