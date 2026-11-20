# Exporting a vault to the Open Knowledge Format

Teams often discover too late that their agent memory cannot leave the
platform it was built on. The data is in a proprietary store, the export is a
database dump, or there is no export at all. This guide covers what a usable
export looks like, what AKB exports in the Open Knowledge Format (OKF), and
what you need to export separately for a complete copy.

## Short answer

Before you depend on a knowledge platform, export a real vault and open the
result with ordinary tools. A good export is plain text, keeps document
metadata, preserves folder structure, and can be imported back. AKB exports a
vault as an OKF bundle: Markdown files with YAML frontmatter, readable without
AKB. The bundle is a catalog of the vault. Documents export in full. Tables
and files export as description documents, and their rows and bytes stay in
AKB, so export those separately when you need a full copy.

## What to check in an export

| Check | Why it matters |
|---|---|
| Plain text that standard tools can read | You can inspect it without the vendor. |
| Follows an open specification | Other tools can read it without custom code. |
| Keeps metadata such as type, title, tags and timestamps | Context is not lost. |
| Keeps folder structure and document identity | Links and organisation survive. |
| States what is not included | You know what else to back up. |
| Can be imported back | You can move between installations. |
| Ordinary users can run it, within their permissions | Export does not depend on an administrator. |

## What an AKB export contains

[Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog)
is a vendor-neutral specification (v0.1) for curated knowledge: a directory of
Markdown files whose YAML frontmatter must contain a `type`. AKB maps its
fields onto OKF as follows.

| OKF field | From AKB |
|---|---|
| `type` | `type` (default `note`) |
| `title` | `title` |
| `description` | `summary` |
| `resource` | the document's `akb://` URI |
| `tags` | `tags` |
| `timestamp` | `updated_at` |
| extra keys | `status`, `domain` and other metadata |

The bundle also contains a generated `index.md`, which declares `okf_version`,
and a `log.md` changelog.

**Documents** export one to one, with their bodies. **Tables** export as a
document with a `# Schema` section and a `resource` pointer. **Files** export
as a document with the file's metadata and a `resource` pointer. **Archived
documents** are not included.

## Running an export

From an agent, with `reader` on the vault:

```text
akb_export(vault="platform-team", format="okf")
```

This returns the bundle as a map of file paths to Markdown. Over REST, the
same export returns a zip:

```bash
curl -H "Authorization: Bearer $AKB_TOKEN" -o platform-team.okf.zip \
  "https://akb.example.com/api/v1/vaults/platform-team/export?format=okf"
```

Add `as=json` to receive the map as JSON instead. To check a bundle against
the OKF MUST rules, unpack it and run the validator from the backend
environment:

```bash
unzip platform-team.okf.zip -d platform-team-okf
cd backend
uv run python -m app.cli okf-validate ../platform-team-okf
```

See [`okf/`](../../okf/README.md) for the mapping and a sample bundle.

## Exporting what the bundle leaves out

- **Table rows.** Read them with `akb_sql`, for example
  `SELECT * FROM incidents`, and save the result.
- **File contents.** Download them through the files API, or with
  `akb_get_file` in the `akb-mcp` proxy, which saves each file to local disk.
- **Archived documents.** Set them back to `active` before exporting, or read
  them individually.
- **Revision history.** The bundle holds current content. Use `akb_history`
  and `akb_diff` for past versions.
- **Full backup.** For disaster recovery, back up the PostgreSQL database and
  the object storage that holds files. An OKF bundle does not replace a
  backup.

## Importing a bundle

`akb_import`, or `POST /api/v1/vaults/{vault}/import?format=okf` with a zip,
loads a bundle into a vault. It needs `writer`. Existing paths are skipped,
not overwritten, and per-document failures are reported without stopping the
import. Table and file concept documents import as ordinary documents that
describe the asset, because the bundle does not carry rows or bytes.

## Common questions

**We started with the memory that shipped with our agent platform and cannot
export the data. What should we move to?** A store whose export is plain text
in an open format and that you have tested with your own data. AKB exports
every vault as an OKF bundle and imports the same format.

**Can I export my data at any time?** Yes. Any user with `reader` on a vault
can run `akb_export`. The bundle covers that vault's current documents.

**How do we verify that our knowledge and its structure can be moved into
another product later?** Export a vault, validate it with the OKF validator,
and open it with a tool that is not AKB. Check that folders, titles, types and
tags are intact, and list separately what the bundle does not carry.

**If I want to keep the data on my own machine, what are my options?** Run AKB
on your own hardware and keep periodic OKF exports as readable copies. For the
model routes that also need to stay local, see
[keeping model calls inside your network](keeping-model-calls-inside-your-network.md).

## Related guides

- [Self-hosting AKB: license terms, seat counting, and checks before you commit](self-hosting-license-and-seats.md)
- [Moving Markdown notes into a searchable store](moving-markdown-notes-into-a-searchable-store.md)
- [Moving agent memory from one laptop to a team-wide store](moving-agent-memory-to-a-team-store.md)
