"""Open Knowledge Format (OKF) interop for AKB.

OKF (https://github.com/GoogleCloudPlatform/knowledge-catalog, spec v0.1) is a
vendor-neutral standard for sharing curated knowledge with AI agents: a
directory tree of markdown files, each with a YAML frontmatter block whose only
required field is ``type``. AKB's per-vault git repo is already a tree of
``.md`` + YAML-frontmatter files, so an AKB vault is *almost* an OKF bundle
already — this module makes the relationship exact and machine-checkable.

Two halves, both pure (no DB / git / network), so they unit-test without infra:

* **Export** — turn AKB records (documents, tables, files) into an OKF bundle:
  remap AKB's frontmatter keys to OKF's recommended names (``summary`` →
  ``description``, ``updated_at`` → ``timestamp``, add ``resource`` = the
  ``akb://`` URI), render tables/files as *concept documents* (schema /
  metadata + a ``resource`` pointer — OKF carries the description of an asset,
  not its bytes/rows), and synthesise the reserved ``index.md`` (progressive
  disclosure) and ``log.md`` (changelog) files plus a root ``okf_version``.

* **Conformance** — validate any directory tree (or in-memory file map)
  against the three OKF v0.1 MUST rules. Deliberately *permissive*: per the
  spec a consumer MUST NOT reject a bundle for unknown ``type`` values, unknown
  keys, broken cross-links, or missing ``index.md`` — so this checker never
  flags those.

See ``okf/README.md`` at the repo root for the AKB ↔ OKF positioning.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

OKF_VERSION = "0.1"

# Reserved filenames carry directory-level structure, not concepts. They are
# exempt from the "every .md needs frontmatter + type" rule.
RESERVED_FILENAMES = frozenset({"index.md", "log.md"})

# Frontmatter key order for emitted concept docs: OKF's required field first,
# then its recommended fields in spec order, then AKB-specific extras (which
# OKF treats as permitted additional keys a consumer must preserve, not reject).
_OKF_KEY_ORDER = ("type", "title", "description", "resource", "tags", "timestamp")


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────
def _iso8601(value: Any) -> str | None:
    """Normalise an AKB timestamp to an ISO-8601 string (``T`` separator).

    AKB serialises timestamps as ``2026-06-12 01:36:48.823107+00:00`` (space
    separator, from ``datetime.isoformat()`` round-tripped through asyncpg /
    JSON). OKF recommends ISO-8601; ``datetime.isoformat`` already is, we only
    need to swap the space the str form may carry for a ``T``.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text.replace(" ", "T", 1)


def _iso_date(value: Any) -> str | None:
    """First 10 chars of an ISO timestamp → ``YYYY-MM-DD`` (OKF log heading)."""
    iso = _iso8601(value)
    if iso is None:
        return None
    head = iso[:10]
    try:
        _dt.date.fromisoformat(head)
    except ValueError:
        return None
    return head


def _dump_frontmatter(meta: Mapping[str, Any]) -> str:
    """Serialise a frontmatter mapping to a ``---``-delimited YAML block.

    ``sort_keys=False`` preserves our intentional key order; ``allow_unicode``
    keeps Korean/CJK titles readable rather than ``\\uXXXX``-escaped.
    """
    body = yaml.safe_dump(
        dict(meta), sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"---\n{body}---\n"


def _compose(meta: Mapping[str, Any], body: str) -> str:
    return f"{_dump_frontmatter(meta)}\n{body.rstrip()}\n"


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, bool]:
    """Split markdown into (frontmatter dict | None, body, had_delimited_block).

    ``had_delimited_block`` distinguishes "no ``---`` fence at all" from "a
    fence that parsed to something non-mapping" — the conformance checker needs
    that distinction to report a useful message.
    """
    if not text.startswith("---"):
        return None, text, False
    # Frontmatter opens with a line that is exactly '---'.
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return None, text, False
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw = "".join(lines[1:idx])
            body = "".join(lines[idx + 1 :])
            try:
                parsed = yaml.safe_load(raw)
            except yaml.YAMLError:
                return None, body, True
            if isinstance(parsed, dict):
                return parsed, body, True
            return None, body, True
    return None, text, False


# ─────────────────────────────────────────────────────────────────────────────
# Export: AKB records → OKF concept documents
# ─────────────────────────────────────────────────────────────────────────────
def okf_frontmatter(
    *,
    type_: str,
    title: str | None = None,
    description: str | None = None,
    resource: str | None = None,
    tags: Sequence[str] | None = None,
    timestamp: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OKF frontmatter mapping in canonical key order.

    ``type`` is always present (OKF's sole MUST). Recommended fields are
    included only when non-empty. AKB-specific keys land after the recommended
    block as permitted additional keys.
    """
    meta: dict[str, Any] = {"type": type_ or "note"}
    if title:
        meta["title"] = title
    if description:
        meta["description"] = description
    if resource:
        meta["resource"] = resource
    if tags:
        meta["tags"] = list(tags)
    ts = _iso8601(timestamp)
    if ts:
        meta["timestamp"] = ts
    if extra:
        for key, value in extra.items():
            if key in meta or value in (None, "", [], {}):
                continue
            meta[key] = value
    # Re-order to spec order, extras trailing.
    ordered = {k: meta[k] for k in _OKF_KEY_ORDER if k in meta}
    for key, value in meta.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _doc_extra(rec: Mapping[str, Any]) -> dict[str, Any]:
    """AKB fields preserved as additional OKF keys (not lost in translation)."""
    extra: dict[str, Any] = {}
    for key in ("status", "domain"):
        if rec.get(key):
            extra[key] = rec[key]
    created = _iso8601(rec.get("created_at"))
    if created:
        extra["created_at"] = created
    if rec.get("uri"):
        extra["akb_uri"] = rec["uri"]
    return extra


def concept_from_document(rec: Mapping[str, Any]) -> tuple[str, str]:
    """An AKB document record → (bundle-relative path, OKF markdown)."""
    path = _normalise_path(rec.get("path") or rec.get("name") or "untitled.md")
    meta = okf_frontmatter(
        type_=str(rec.get("type") or rec.get("doc_type") or "note"),
        title=rec.get("title"),
        description=rec.get("summary") or rec.get("description"),
        resource=rec.get("uri"),
        tags=rec.get("tags"),
        timestamp=rec.get("updated_at") or rec.get("created_at"),
        extra=_doc_extra(rec),
    )
    body = str(rec.get("content") or rec.get("body") or "").strip()
    return path, _compose(meta, body)


def _schema_table(columns: Iterable[Mapping[str, Any]]) -> str:
    rows = ["| Column | Type | Description |", "| --- | --- | --- |"]
    for col in columns:
        name = str(col.get("name", "")).strip()
        ctype = str(col.get("type", "")).strip()
        desc = str(col.get("description", "") or "").strip().replace("\n", " ")
        rows.append(f"| {name} | {ctype} | {desc} |")
    return "\n".join(rows)


def concept_from_table(rec: Mapping[str, Any]) -> tuple[str, str]:
    """An AKB table record → an OKF *concept document* describing the table.

    OKF carries the table's schema and a ``resource`` pointer to the live
    asset, not its rows — exactly the BigQuery-table concept the spec models.
    """
    raw = rec.get("path") or rec.get("sql_name") or rec.get("name") or "table"
    path = _normalise_path(str(raw))
    columns = rec.get("columns") or []
    parts: list[str] = []
    if rec.get("description"):
        parts.append(str(rec["description"]).strip())
    if columns:
        parts.append("# Schema\n\n" + _schema_table(columns))
    row_count = rec.get("row_count")
    if row_count is not None:
        parts.append(f"# Rows\n\n{row_count} rows (data lives in AKB; this concept carries the schema).")
    meta = okf_frontmatter(
        type_="table",
        title=rec.get("title") or rec.get("name") or rec.get("sql_name"),
        description=rec.get("description"),
        resource=rec.get("uri"),
        tags=rec.get("tags"),
        timestamp=rec.get("updated_at") or rec.get("created_at"),
        extra={k: rec[k] for k in ("sql_name", "row_count") if rec.get(k) is not None},
    )
    return path, _compose(meta, "\n\n".join(parts))


def concept_from_file(rec: Mapping[str, Any]) -> tuple[str, str]:
    """An AKB file record → an OKF concept document referencing the asset.

    OKF bundles are markdown-only; a binary file is represented by a concept
    doc whose ``resource`` points at the file (e.g. its ``akb://`` URI), with
    mime/size as metadata — the bytes themselves stay in AKB.
    """
    raw = rec.get("path") or rec.get("name") or "file"
    path = _normalise_path(_with_md_suffix(str(raw)))
    uri = rec.get("uri")
    body_lines = ["This concept references a file asset stored in AKB."]
    if uri:
        body_lines.append("")
        body_lines.append(f"Resource: [{rec.get('name') or raw}]({uri})")
    meta = okf_frontmatter(
        type_="file",
        title=rec.get("title") or rec.get("name"),
        description=rec.get("description") or rec.get("summary"),
        resource=uri,
        tags=rec.get("tags"),
        timestamp=rec.get("updated_at") or rec.get("created_at"),
        extra={k: rec[k] for k in ("mime_type", "size_bytes") if rec.get(k) is not None},
    )
    return path, _compose(meta, "\n".join(body_lines))


def _with_md_suffix(path: str) -> str:
    return path if path.endswith(".md") else f"{path}.md"


def _normalise_path(path: str) -> str:
    """Bundle-relative concept path: forward slashes, no leading slash, ``.md``."""
    cleaned = path.strip().lstrip("/").replace("\\", "/")
    cleaned = _with_md_suffix(cleaned)
    return cleaned


# ── reserved files: index.md (progressive disclosure) & log.md (changelog) ──
@dataclass
class _Entry:
    path: str  # bundle-relative, e.g. "specs/api.md"
    title: str
    description: str
    timestamp: str | None = None


def _link_line(entry: _Entry) -> str:
    desc = f" - {entry.description}" if entry.description else ""
    # Absolute, bundle-root links are the OKF-recommended form.
    return f"* [{entry.title}](/{entry.path}){desc}"


def build_index(entries: Sequence[_Entry], *, okf_version: str | None = None) -> str:
    """Render an ``index.md``. Only the bundle-root index may carry frontmatter
    (and it is the only place ``okf_version`` is declared)."""
    by_section: dict[str, list[_Entry]] = {}
    for entry in entries:
        section = entry.path.split("/", 1)[0] if "/" in entry.path else "Documents"
        by_section.setdefault(section, []).append(entry)
    parts: list[str] = []
    for section in sorted(by_section):
        parts.append(f"# {section}")
        parts.append("")
        for entry in sorted(by_section[section], key=lambda e: e.title.lower()):
            parts.append(_link_line(entry))
        parts.append("")
    body = "\n".join(parts).rstrip() + "\n"
    if okf_version is not None:
        return _compose({"okf_version": okf_version}, body)
    return body


def build_log(entries: Sequence[_Entry]) -> str:
    """Render a ``log.md``: date-grouped, newest first, ISO ``YYYY-MM-DD``."""
    by_date: dict[str, list[_Entry]] = {}
    for entry in entries:
        day = _iso_date(entry.timestamp) or "0000-00-00"
        by_date.setdefault(day, []).append(entry)
    parts = ["# Directory Update Log", ""]
    for day in sorted((d for d in by_date if d != "0000-00-00"), reverse=True):
        parts.append(f"## {day}")
        parts.append("")
        for entry in sorted(by_date[day], key=lambda e: e.title.lower()):
            parts.append(f"* **Update**: [{entry.title}](/{entry.path})")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_bundle(
    documents: Sequence[Mapping[str, Any]] | None = None,
    tables: Sequence[Mapping[str, Any]] | None = None,
    files: Sequence[Mapping[str, Any]] | None = None,
    *,
    okf_version: str = OKF_VERSION,
    with_log: bool = True,
) -> dict[str, str]:
    """Assemble a complete OKF bundle (path → content) from AKB records."""
    out: dict[str, str] = {}
    entries: list[_Entry] = []

    def _add(maker: Any, rec: Mapping[str, Any]) -> None:
        path, content = maker(rec)
        out[path] = content
        meta, _, _ = split_frontmatter(content)
        meta = meta or {}
        entries.append(
            _Entry(
                path=path,
                title=str(meta.get("title") or path[:-3]),
                description=str(meta.get("description") or ""),
                timestamp=meta.get("timestamp"),
            )
        )

    for rec in documents or []:
        _add(concept_from_document, rec)
    for rec in tables or []:
        _add(concept_from_table, rec)
    for rec in files or []:
        _add(concept_from_file, rec)

    out["index.md"] = build_index(entries, okf_version=okf_version)
    if with_log:
        out["log.md"] = build_log(entries)
    return out


def write_bundle(out_dir: Path, files: Mapping[str, str]) -> int:
    """Write a bundle (path → content) under ``out_dir``. Returns file count."""
    for rel, content in files.items():
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return len(files)


def _doc_uri(vault: str, rel_path: str) -> str:
    """Reconstruct a document's ``akb://`` URI from its bundle-relative path."""
    rel = rel_path.replace("\\", "/").lstrip("/")
    if "/" in rel:
        coll, filename = rel.rsplit("/", 1)
        return f"akb://{vault}/coll/{coll}/doc/{filename}"
    return f"akb://{vault}/doc/{rel}"


def records_from_git_tree(worktree: Path, vault: str) -> list[dict[str, Any]]:
    """Read an AKB vault git worktree into document records for ``build_bundle``.

    An AKB vault repo is already a tree of ``.md`` + frontmatter files, so this
    just parses each, lifts AKB's frontmatter keys, and reconstructs the
    ``akb://`` resource URI from the path. (Tables/files live in PG/S3, not git;
    include those via the record-based ``build_bundle`` path instead.)
    """
    records: list[dict[str, Any]] = []
    for path in sorted(worktree.rglob("*.md")):
        if ".git" in path.parts:
            continue
        rel = path.relative_to(worktree).as_posix()
        if rel.rsplit("/", 1)[-1] in RESERVED_FILENAMES:
            continue
        meta, body, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        meta = meta or {}
        records.append(
            {
                "path": rel,
                "uri": _doc_uri(vault, rel),
                "title": meta.get("title"),
                "type": meta.get("type"),
                "status": meta.get("status"),
                "summary": meta.get("summary"),
                "domain": meta.get("domain"),
                "tags": meta.get("tags"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "content": body,
            }
        )
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Import: OKF concept documents → AKB document records
# ─────────────────────────────────────────────────────────────────────────────
# AKB's document `type` is free-form (recommended vocabulary, not a hard enum),
# matching OKF's open, producer-defined `type`. So an OKF type imports verbatim
# — no clamping, no lossy `okf-type:` tag. `status` stays a small closed set
# (it drives browse/archive semantics), so an unknown status falls back to draft.
AKB_DOC_STATUSES = frozenset({"draft", "active", "archived"})


def okf_doc_to_record(rel_path: str, meta: Mapping[str, Any], body: str) -> dict[str, Any]:
    """One OKF concept doc → an AKB-shaped document record (for import).

    Reverses the export field mapping: ``description`` → ``summary``. AKB sets
    its own ``created_at``/``updated_at`` on write, so OKF ``timestamp`` is
    informational only. ``type`` is carried through unchanged (AKB types are
    open); an unrecognised ``status`` falls back to ``draft``.
    """
    coll, _, fname = rel_path.replace("\\", "/").rpartition("/")
    slug = fname[:-3] if fname.endswith(".md") else fname
    status = str(meta.get("status") or "draft")
    if status not in AKB_DOC_STATUSES:
        status = "draft"
    return {
        "path": _normalise_path(rel_path),
        "collection": coll,
        "slug": slug,
        "title": str(meta.get("title") or slug),
        "type": str(meta.get("type") or "note").strip() or "note",
        "status": status,
        "summary": meta.get("description") or meta.get("summary"),
        "domain": meta.get("domain"),
        "tags": list(meta.get("tags") or []),
        "content": body.strip(),
    }


def parse_okf_bundle(files: Mapping[str, str]) -> list[dict[str, Any]]:
    """An OKF bundle (path → content) → AKB document records.

    Permissive consumer (per OKF spec): reserved files are skipped, and a
    concept file with no/unparseable frontmatter is still imported (as a
    ``reference`` note) rather than rejected.
    """
    records: list[dict[str, Any]] = []
    for rel, text in sorted(files.items()):
        if not rel.endswith(".md"):
            continue
        name = rel.rsplit("/", 1)[-1]
        if name in RESERVED_FILENAMES:
            continue
        meta, body, _ = split_frontmatter(text)
        records.append(okf_doc_to_record(rel, meta or {}, body if meta is not None else text))
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Conformance: validate a bundle against OKF v0.1 MUST rules
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    level: str  # "error" (a MUST violation) — the only level emitted today
    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.path}: {self.message} ({self.code})"


@dataclass
class ConformanceReport:
    files_checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def summary(self) -> str:
        errors = sum(1 for f in self.findings if f.level == "error")
        verdict = "CONFORMANT" if self.ok else "NON-CONFORMANT"
        return f"OKF v{OKF_VERSION}: {verdict} — {self.files_checked} markdown file(s), {errors} error(s)"


def _is_root_index(rel_path: str) -> bool:
    return rel_path == "index.md"


def _check_concept(rel: str, text: str, out: list[Finding]) -> None:
    meta, _, had_block = split_frontmatter(text)
    if meta is None:
        if had_block:
            out.append(Finding("error", rel, "okf.frontmatter.unparseable",
                               "frontmatter block is not a parseable YAML mapping"))
        else:
            out.append(Finding("error", rel, "okf.frontmatter.missing",
                               "concept document has no YAML frontmatter block"))
        return
    type_value = meta.get("type")
    if type_value is None or (isinstance(type_value, str) and not type_value.strip()):
        out.append(Finding("error", rel, "okf.type.missing",
                           "frontmatter has no non-empty `type` field"))


def _check_index(rel: str, text: str, out: list[Finding]) -> None:
    meta, _, had_block = split_frontmatter(text)
    if had_block and not _is_root_index(rel):
        out.append(Finding("error", rel, "okf.index.frontmatter",
                           "only the bundle-root index.md may carry frontmatter"))


_DATE_PREFIX = "## "


def _check_log(rel: str, text: str, out: list[Finding]) -> None:
    _, _, had_block = split_frontmatter(text)
    if had_block:
        out.append(Finding("error", rel, "okf.log.frontmatter",
                           "log.md must not carry frontmatter"))
    for line in text.splitlines():
        if line.startswith(_DATE_PREFIX):
            heading = line[len(_DATE_PREFIX):].strip()
            try:
                _dt.date.fromisoformat(heading)
            except ValueError:
                out.append(Finding("error", rel, "okf.log.date",
                                   f"date heading '{heading}' is not ISO 8601 YYYY-MM-DD"))


def check_bundle(files: Mapping[str, str]) -> ConformanceReport:
    """Validate an in-memory bundle (bundle-relative path → content)."""
    report = ConformanceReport()
    for rel, text in sorted(files.items()):
        if not rel.endswith(".md"):
            continue
        report.files_checked += 1
        name = rel.rsplit("/", 1)[-1]
        if name == "index.md":
            _check_index(rel, text, report.findings)
        elif name == "log.md":
            _check_log(rel, text, report.findings)
        else:
            _check_concept(rel, text, report.findings)
    return report


def check_dir(root: Path) -> ConformanceReport:
    """Validate every ``.md`` under ``root`` (recursively)."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return check_bundle(files)
