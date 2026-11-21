"""
Ledger synchronization across revised PDF versions.

Suppliers periodically send revised editions of PDF ledgers. Because each
revision is re-paginated, record changes cannot be detected by page/line
numbers. This module adds *ledger sync* to pdfplumber:

- Tables are pulled out with pdfplumber's real table extraction, and records
  are identified by *declared business keys* instead of positions.
- Each incoming version of a declared range is processed as a full snapshot
  and compared against the currently effective version, producing additions,
  updates, and deletions, each annotated with old/new cell positions.
- A pure position move does not count as a content change.
- Deletions are only recognized when integrity checks pass; on key conflicts
  or missing pages the current data is kept.
- Re-applying the same version does not re-emit changes, and a late-arriving
  old version never overwrites newer data.
- The official snapshot and its change records are committed in a single
  transaction, and every record stays traceable to its source PDF (path,
  SHA-256, page, and cell bounding boxes).

Basic usage::

    from pdfplumber.ledger import ColumnSpec, LedgerSync, RangeSpec

    spec = RangeSpec(
        name="ap-ledger",
        key=("supplier", "invoice"),
        columns=(
            ColumnSpec("supplier", header="Supplier"),
            ColumnSpec("invoice", header="Invoice No"),
            ColumnSpec("amount", header="Amount"),
        ),
        pages=(1, 2, 3),
    )

    sync = LedgerSync("ledger.db")
    result = sync.sync(spec, "ledger-2026-11.pdf", version="2026-11-01")
    if result.status == "applied":
        for change in result.changes:
            print(change.kind, change.key, change.old_position, change.new_position)
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ._typing import T_bbox, T_num
from .pdf import PDF
from .table import TableSettings

__all__ = [
    "APPLIED",
    "DUPLICATE",
    "STALE",
    "REJECTED",
    "ADDED",
    "UPDATED",
    "REMOVED",
    "ColumnSpec",
    "RangeSpec",
    "IntegrityIssue",
    "IntegrityReport",
    "CellPosition",
    "LedgerRecord",
    "Change",
    "ChangeEvent",
    "VersionInfo",
    "RecordView",
    "SyncResult",
    "LedgerSync",
    "extract_range",
    "diff_records",
    "normalize_text",
]

# Sync outcomes
APPLIED = "applied"
DUPLICATE = "duplicate"
STALE = "stale"
REJECTED = "rejected"

# Change kinds
ADDED = "added"
UPDATED = "updated"
REMOVED = "removed"

T_version = Union[int, str]
T_key = Tuple[str, ...]

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: Optional[str]) -> str:
    """Collapse runs of whitespace and strip; ``None`` becomes ``""``."""
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", str(value)).strip()


@dataclass(frozen=True)
class ColumnSpec:
    """
    Declaration of one ledger column.

    ``name`` is the canonical field name used in snapshots and change
    records. ``header`` is the header text to match in the PDF (defaults to
    ``name``). ``index`` pins the column to a zero-based table column for
    positional extraction when no reliable header row exists; it is only
    valid together with ``RangeSpec(header="none")``.
    """

    name: str
    header: Optional[str] = None
    required: bool = True
    index: Optional[int] = None

    @property
    def header_text(self) -> str:
        return self.header if self.header is not None else self.name


@dataclass
class RangeSpec:
    """
    Declaration of one ledger range: a named, independently versioned
    section of a ledger (for example one account or one book).

    - ``key``: canonical column names forming the declared business key.
    - ``columns``: the declared columns.
    - ``pages``: the expected 1-indexed pages of this range; ``None`` means
      "all pages of the PDF". Declared pages that are absent from the PDF
      fail integrity ("missing pages").
    - ``header``: ``"first"`` (default) reads the column mapping from the
      first table's first non-blank row and skips repeated header rows;
      ``"each"`` requires every table to start with a header row (tolerates
      column reordering between pages); ``"none"`` uses the positional
      ``ColumnSpec.index`` mapping and treats every row as data.
    - ``table_settings``: passed through to ``Page.find_tables``.
    - ``require_table_per_page``: a declared page yielding no table fails
      integrity.
    - ``allow_empty``: allow a version that contains zero data records.
    """

    name: str
    key: Sequence[str]
    columns: Sequence[ColumnSpec]
    pages: Optional[Sequence[int]] = None
    header: str = "first"
    table_settings: Optional[Dict[str, Any]] = None
    require_table_per_page: bool = True
    allow_empty: bool = False

    def __post_init__(self) -> None:
        self.key = tuple(self.key)
        self.columns = tuple(self.columns)
        if self.pages is not None:
            self.pages = tuple(sorted({int(p) for p in self.pages}))
        if not self.key:
            raise ValueError("RangeSpec.key must name at least one column")
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("duplicate column names in RangeSpec.columns")
        unknown = [k for k in self.key if k not in names]
        if unknown:
            raise ValueError(f"key columns not declared in columns: {unknown}")
        optional_keys = [k for k in self.key if not self._column(k).required]
        if optional_keys:
            raise ValueError(f"key columns must be required: {optional_keys}")
        if self.header not in ("first", "each", "none"):
            raise ValueError("header must be 'first', 'each', or 'none'")
        if self.header == "none":
            missing = [c.name for c in self.columns if c.index is None]
            if missing:
                raise ValueError(
                    f"columns need an explicit index when header='none': {missing}"
                )
        else:
            pinned = [c.name for c in self.columns if c.index is not None]
            if pinned:
                raise ValueError(
                    f"ColumnSpec.index is only valid with header='none': {pinned}"
                )
            headers = [normalize_text(c.header_text) for c in self.columns]
            if len(set(headers)) != len(headers):
                raise ValueError("duplicate header text in RangeSpec.columns")

    def _column(self, name: str) -> ColumnSpec:
        return next(c for c in self.columns if c.name == name)


@dataclass
class IntegrityIssue:
    """One integrity problem found while extracting a version."""

    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass
class IntegrityReport:
    """Outcome of the integrity checks for one extracted version."""

    issues: List[IntegrityIssue] = field(default_factory=list)
    pages_checked: List[int] = field(default_factory=list)
    tables_found: int = 0
    rows_found: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "pages_checked": list(self.pages_checked),
            "tables_found": self.tables_found,
            "rows_found": self.rows_found,
        }


@dataclass
class CellPosition:
    """
    Location of one record in the source PDF: 1-indexed ``page``, zero-based
    ``table`` (within the page) and ``row`` (within the table), the row's
    bounding box, and per-column cell bounding boxes keyed by canonical
    column name. Sufficient to crop or highlight the record in the original
    PDF.
    """

    page: int
    table: int
    row: int
    bbox: Tuple[T_num, T_num, T_num, T_num]
    cells: Dict[str, Optional[T_bbox]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "table": self.table,
            "row": self.row,
            "bbox": list(self.bbox),
            "cells": {
                k: (list(v) if v is not None else None) for k, v in self.cells.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CellPosition":
        return cls(
            page=int(data["page"]),
            table=int(data["table"]),
            row=int(data["row"]),
            bbox=tuple(data["bbox"]),  # type: ignore[arg-type]
            cells={
                k: (tuple(v) if v is not None else None)  # type: ignore[arg-type]
                for k, v in data.get("cells", {}).items()
            },
        )


@dataclass
class LedgerRecord:
    """One extracted record: business key, field values, position, hash."""

    key: T_key
    fields: Dict[str, str]
    position: CellPosition
    content_hash: str


@dataclass
class Change:
    """
    One record-level change between the current effective version and an
    incoming version, annotated with old/new cell positions. ``kind`` is
    ``"added"``, ``"updated"``, or ``"removed"``.
    """

    kind: str
    key: T_key
    old_fields: Optional[Dict[str, str]] = None
    new_fields: Optional[Dict[str, str]] = None
    old_position: Optional[CellPosition] = None
    new_position: Optional[CellPosition] = None
    changed_columns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "key": list(self.key),
            "old_fields": self.old_fields,
            "new_fields": self.new_fields,
            "old_position": (
                self.old_position.to_dict() if self.old_position else None
            ),
            "new_position": (
                self.new_position.to_dict() if self.new_position else None
            ),
            "changed_columns": list(self.changed_columns),
        }


@dataclass
class VersionInfo:
    """One logged sync event for a range (applied or not)."""

    id: int
    range_name: str
    version: T_version
    status: str
    pdf_path: Optional[str]
    pdf_sha256: Optional[str]
    page_count: Optional[int]
    record_count: Optional[int]
    applied_seq: Optional[int]
    received_at: str
    detail: Dict[str, Any]


@dataclass
class RecordView:
    """A currently effective record together with its provenance."""

    range_name: str
    key: T_key
    fields: Dict[str, str]
    position: CellPosition
    version: T_version
    version_id: int
    pdf_path: Optional[str]
    pdf_sha256: Optional[str]


@dataclass
class ChangeEvent:
    """A persisted change record together with its version's provenance."""

    change: Change
    range_name: str
    version: T_version
    version_id: int
    pdf_path: Optional[str]
    pdf_sha256: Optional[str]
    received_at: str


@dataclass
class SyncResult:
    """
    Outcome of one ``LedgerSync.sync`` call. ``changes`` is non-empty only
    when ``status == "applied"``; a ``"duplicate"`` retry re-emits nothing.
    """

    range_name: str
    version: T_version
    status: str
    changes: List[Change] = field(default_factory=list)
    integrity: IntegrityReport = field(default_factory=IntegrityReport)
    version_id: Optional[int] = None
    effective_version: Optional[T_version] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.status == APPLIED

    @property
    def added(self) -> List[Change]:
        return [c for c in self.changes if c.kind == ADDED]

    @property
    def updated(self) -> List[Change]:
        return [c for c in self.changes if c.kind == UPDATED]

    @property
    def removed(self) -> List[Change]:
        return [c for c in self.changes if c.kind == REMOVED]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "range_name": self.range_name,
            "version": self.version,
            "status": self.status,
            "changes": [c.to_dict() for c in self.changes],
            "integrity": self.integrity.to_dict(),
            "version_id": self.version_id,
            "effective_version": self.effective_version,
            "warnings": list(self.warnings),
        }


def _content_hash(key: T_key, fields: Mapping[str, str]) -> str:
    """Position-independent fingerprint of a record's content."""
    payload = json.dumps(
        {"key": list(key), "fields": [[k, fields[k]] for k in sorted(fields)]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _match_header(
    cells: List[str], spec: RangeSpec
) -> Tuple[Dict[str, Optional[int]], List[IntegrityIssue]]:
    """Map canonical column names to table column indices via header text."""
    mapping: Dict[str, Optional[int]] = {}
    issues: List[IntegrityIssue] = []
    used: Dict[int, str] = {}
    for col in spec.columns:
        target = normalize_text(col.header_text)
        idx: Optional[int] = None
        for i, cell in enumerate(cells):
            if i in used:
                continue
            if cell == target:
                idx = i
                break
        if idx is None:
            if col.required:
                issues.append(
                    IntegrityIssue(
                        code="missing_column",
                        message=(
                            f"required column {col.name!r} (header {target!r}) "
                            "not found in header row"
                        ),
                        details={"column": col.name, "header": target},
                    )
                )
            mapping[col.name] = None
        else:
            used[idx] = col.name
            mapping[col.name] = idx
    return mapping, issues


def _build_record(
    spec: RangeSpec,
    mapping: Mapping[str, Optional[int]],
    norm: List[str],
    row_cells: Sequence[Optional[T_bbox]],
    row_bbox: T_bbox,
    page_number: int,
    table_index: int,
    row_index: int,
    report: IntegrityReport,
) -> Optional[LedgerRecord]:
    fields: Dict[str, str] = {}
    for col in spec.columns:
        idx = mapping.get(col.name)
        fields[col.name] = norm[idx] if idx is not None and idx < len(norm) else ""
    key = tuple(fields[k] for k in spec.key)
    if any(part == "" for part in key):
        # If a key column is unmapped, the missing_column issue already
        # covers it; otherwise this row itself lacks key data.
        if all(mapping.get(k) is not None for k in spec.key):
            report.issues.append(
                IntegrityIssue(
                    code="missing_key",
                    message=(
                        f"row at page {page_number} table {table_index} "
                        f"row {row_index} has an empty business key component"
                    ),
                    details={
                        "page": page_number,
                        "table": table_index,
                        "row": row_index,
                    },
                )
            )
        return None
    cells: Dict[str, Optional[T_bbox]] = {}
    for col in spec.columns:
        idx = mapping.get(col.name)
        bbox: Optional[T_bbox] = None
        if idx is not None and idx < len(row_cells):
            cell = row_cells[idx]
            bbox = tuple(cell) if cell is not None else None  # type: ignore[arg-type]
        cells[col.name] = bbox
    position = CellPosition(
        page=page_number,
        table=table_index,
        row=row_index,
        bbox=tuple(row_bbox),  # type: ignore[arg-type]
        cells=cells,
    )
    return LedgerRecord(
        key=key,
        fields=fields,
        position=position,
        content_hash=_content_hash(key, fields),
    )


def extract_range(
    pdf: PDF, spec: RangeSpec
) -> Tuple[List[LedgerRecord], IntegrityReport]:
    """
    Extract the full record set of one range from an open pdfplumber ``PDF``
    using real table extraction and the declared column mapping.

    Returns ``(records, integrity_report)``. The records are returned even
    when integrity fails (for inspection); the caller decides whether they
    may be applied.
    """
    report = IntegrityReport()
    page_total = len(pdf.pages)
    if spec.pages is None:
        page_numbers = list(range(1, page_total + 1))
    else:
        page_numbers = list(spec.pages)
    missing_pages = [p for p in page_numbers if p < 1 or p > page_total]
    if missing_pages:
        report.issues.append(
            IntegrityIssue(
                code="missing_pages",
                message=(
                    f"declared pages missing from PDF: {missing_pages} "
                    f"(PDF has {page_total} page(s))"
                ),
                details={"pages": missing_pages, "page_count": page_total},
            )
        )
    present_pages = [p for p in page_numbers if 1 <= p <= page_total]
    report.pages_checked = present_pages

    records: List[LedgerRecord] = []
    seen: Dict[T_key, CellPosition] = {}
    tset = TableSettings.resolve(spec.table_settings)
    text_settings = tset.text_settings or {}

    mapping: Optional[Dict[str, Optional[int]]] = None
    if spec.header == "none":
        mapping = {c.name: c.index for c in spec.columns}
    header_signature: Optional[List[str]] = None

    for page_number in present_pages:
        page = pdf.pages[page_number - 1]
        tables = page.find_tables(tset)
        report.tables_found += len(tables)
        if not tables:
            if spec.require_table_per_page:
                report.issues.append(
                    IntegrityIssue(
                        code="no_table_on_page",
                        message=f"page {page_number} yielded no tables",
                        details={"page": page_number},
                    )
                )
            continue
        for table_index, tbl in enumerate(tables):
            grid = tbl.extract(**text_settings)
            table_mapping = mapping if spec.header != "each" else None
            table_signature: Optional[List[str]] = None
            for row_index, (row_obj, cells_text) in enumerate(zip(tbl.rows, grid)):
                norm = [normalize_text(c) for c in cells_text]
                if not any(norm):
                    continue  # fully blank row
                if table_mapping is None:
                    # The first non-blank row of the range (header="first")
                    # or of this table (header="each") sets the column mapping.
                    table_mapping, issues = _match_header(norm, spec)
                    report.issues.extend(issues)
                    if spec.header == "first":
                        mapping = table_mapping
                        header_signature = norm
                    else:
                        table_signature = norm
                    continue
                signature = (
                    header_signature if spec.header == "first" else table_signature
                )
                if signature is not None and norm == signature:
                    continue  # repeated header row
                record = _build_record(
                    spec,
                    table_mapping,
                    norm,
                    row_obj.cells,
                    row_obj.bbox,
                    page_number,
                    table_index,
                    row_index,
                    report,
                )
                if record is None:
                    continue
                first_seen = seen.get(record.key)
                if first_seen is not None:
                    report.issues.append(
                        IntegrityIssue(
                            code="duplicate_key",
                            message=(
                                f"business key {record.key!r} at page "
                                f"{page_number} conflicts with page "
                                f"{first_seen.page}"
                            ),
                            details={
                                "key": list(record.key),
                                "first_page": first_seen.page,
                                "again_page": page_number,
                            },
                        )
                    )
                    continue
                seen[record.key] = record.position
                records.append(record)
                report.rows_found += 1
    if not records and not spec.allow_empty:
        report.issues.append(
            IntegrityIssue(
                code="empty_range",
                message=f"range {spec.name!r} produced no data records",
            )
        )
    return records, report


def diff_records(
    current: Mapping[T_key, LedgerRecord],
    incoming: Mapping[T_key, LedgerRecord],
) -> List[Change]:
    """
    Compare two full snapshots by business key. A pure position move is not
    a content change: records whose content hash is unchanged yield no
    ``Change`` even when their position differs.
    """
    changes: List[Change] = []
    for key in sorted(set(current) | set(incoming)):
        cur = current.get(key)
        inc = incoming.get(key)
        if inc is not None and cur is None:
            changes.append(
                Change(ADDED, key, new_fields=inc.fields, new_position=inc.position)
            )
        elif cur is not None and inc is None:
            changes.append(
                Change(REMOVED, key, old_fields=cur.fields, old_position=cur.position)
            )
        elif (
            cur is not None and inc is not None and cur.content_hash != inc.content_hash
        ):
            changed_columns = [
                name
                for name in sorted(set(cur.fields) | set(inc.fields))
                if cur.fields.get(name) != inc.fields.get(name)
            ]
            changes.append(
                Change(
                    UPDATED,
                    key,
                    old_fields=cur.fields,
                    new_fields=inc.fields,
                    old_position=cur.position,
                    new_position=inc.position,
                    changed_columns=changed_columns,
                )
            )
    return changes


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    range_name TEXT NOT NULL,
    version_label TEXT NOT NULL,
    version_type TEXT NOT NULL CHECK (version_type IN ('int', 'str')),
    status TEXT NOT NULL
        CHECK (status IN ('applied', 'rejected', 'stale', 'duplicate')),
    pdf_path TEXT,
    pdf_sha256 TEXT,
    page_count INTEGER,
    record_count INTEGER,
    applied_seq INTEGER,
    detail TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_version_range
    ON ledger_version (range_name, status, applied_seq);

CREATE TABLE IF NOT EXISTS ledger_record (
    range_name TEXT NOT NULL,
    record_key TEXT NOT NULL,
    fields TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    position TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES ledger_version (id),
    PRIMARY KEY (range_name, record_key)
);

CREATE TABLE IF NOT EXISTS ledger_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES ledger_version (id),
    range_name TEXT NOT NULL,
    record_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('added', 'updated', 'removed')),
    old_fields TEXT,
    new_fields TEXT,
    old_position TEXT,
    new_position TEXT,
    changed_columns TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_ledger_change_range
    ON ledger_change (range_name, record_key);
"""


def _version_type(version: T_version) -> str:
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        raise TypeError(
            "version must be an int or a str, e.g. 3 or '2026-11-01'; "
            "string versions are ordered lexicographically, so use ISO-8601 "
            "dates or zero-padded sequence numbers"
        )
    return "int" if isinstance(version, int) else "str"


def _decode_version(label: str, version_type: str) -> T_version:
    return int(label) if version_type == "int" else label


def _encode_key(key: T_key) -> str:
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def _decode_key(text: str) -> T_key:
    return tuple(json.loads(text))


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


class LedgerSync:
    """
    Synchronizes declared ledger ranges across revised PDF versions into a
    SQLite store.

    Every ``sync`` call is logged in the ``ledger_version`` table with its
    outcome status. Only ``applied`` versions create change records and
    mutate the official snapshot, and they do so in a single transaction.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LedgerSync":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # synchronization
    # ------------------------------------------------------------------

    def sync(
        self,
        spec: RangeSpec,
        pdf_path: Union[str, os.PathLike],
        version: T_version,
        *,
        pdf_bytes: Optional[bytes] = None,
    ) -> SyncResult:
        """
        Process one full-version PDF for a range.

        The incoming version is identified by ``version`` (ordered per
        range), checked against the currently effective version, extracted
        with real table extraction, integrity-checked, diffed against the
        current snapshot, and — only when all of that passes — committed
        together with its change records in one transaction.
        """
        vtype = _version_type(version)
        label = str(version)
        with self._lock:
            if pdf_bytes is None:
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
            sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            path_str = str(pdf_path)

            current_row = self._current_applied(spec.name)
            current_version: Optional[T_version] = None
            if current_row is not None:
                if current_row["version_type"] != vtype:
                    raise TypeError(
                        f"range {spec.name!r} uses "
                        f"{current_row['version_type']} versions; "
                        f"got a {vtype} version"
                    )
                current_version = _decode_version(current_row["version_label"], vtype)

            # Same-version retry: a pure no-op that re-emits no changes.
            applied = self._applied_by_label(spec.name, label)
            if applied is not None:
                warnings = []
                if applied["pdf_sha256"] and applied["pdf_sha256"] != sha256:
                    warnings.append(
                        "retry PDF bytes differ from the originally applied "
                        f"copy (sha256 {applied['pdf_sha256']} -> {sha256})"
                    )
                version_id = self._record_attempt(
                    spec.name,
                    label,
                    vtype,
                    DUPLICATE,
                    path_str,
                    sha256,
                    detail={"original_version_id": applied["id"]},
                )
                return SyncResult(
                    range_name=spec.name,
                    version=version,
                    status=DUPLICATE,
                    version_id=version_id,
                    effective_version=current_version,
                    warnings=warnings,
                )

            # A late old version must not overwrite newer data.
            is_stale = current_version is not None and not (
                version > current_version  # type: ignore[operator]
            )
            if is_stale:
                version_id = self._record_attempt(
                    spec.name,
                    label,
                    vtype,
                    STALE,
                    path_str,
                    sha256,
                    detail={"current_version": current_version},
                )
                return SyncResult(
                    range_name=spec.name,
                    version=version,
                    status=STALE,
                    version_id=version_id,
                    effective_version=current_version,
                    warnings=[
                        f"version {version!r} does not supersede current "
                        f"version {current_version!r}; current data kept"
                    ],
                )

            # Real table extraction of the full incoming version.
            records: List[LedgerRecord] = []
            try:
                with PDF.open(io.BytesIO(pdf_bytes)) as pdf:
                    page_count: Optional[int] = len(pdf.pages)
                    records, report = extract_range(pdf, spec)
            except Exception as exc:
                page_count = None
                report = IntegrityReport(
                    issues=[
                        IntegrityIssue(
                            code="extraction_error",
                            message=(
                                f"failed to extract range: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                    ]
                )

            # Integrity gate: deletions are only recognized when integrity
            # passes; on key conflicts or missing pages the current data
            # is kept.
            if not report.ok:
                version_id = self._record_attempt(
                    spec.name,
                    label,
                    vtype,
                    REJECTED,
                    path_str,
                    sha256,
                    page_count=page_count,
                    record_count=len(records),
                    detail={"issues": [i.to_dict() for i in report.issues]},
                )
                return SyncResult(
                    range_name=spec.name,
                    version=version,
                    status=REJECTED,
                    integrity=report,
                    version_id=version_id,
                    effective_version=current_version,
                    warnings=["integrity checks failed; current data kept"],
                )

            # Diff against the currently effective snapshot.
            current_records = self._load_snapshot(spec.name)
            incoming = {r.key: r for r in records}
            changes = diff_records(current_records, incoming)

            # Commit the official snapshot and its change records in one
            # transaction.
            counts = {
                "added": 0,
                "updated": 0,
                "removed": 0,
            }
            for change in changes:
                counts[change.kind] += 1
            with self._conn:
                seq = self._next_applied_seq(spec.name)
                cur = self._conn.execute(
                    "INSERT INTO ledger_version (range_name, version_label,"
                    " version_type, status, pdf_path, pdf_sha256, page_count,"
                    " record_count, applied_seq, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        spec.name,
                        label,
                        vtype,
                        APPLIED,
                        path_str,
                        sha256,
                        page_count,
                        len(records),
                        seq,
                        _dumps({"changes": counts}),
                    ),
                )
                version_id = cur.lastrowid
                for change in changes:
                    self._insert_change_tx(spec.name, version_id, change)
                self._apply_snapshot_tx(spec.name, version_id, changes, incoming)
            return SyncResult(
                range_name=spec.name,
                version=version,
                status=APPLIED,
                changes=changes,
                integrity=report,
                version_id=version_id,
                effective_version=version,
            )

    def sync_many(
        self,
        specs: Sequence[RangeSpec],
        pdf_path: Union[str, os.PathLike],
        version: T_version,
        *,
        pdf_bytes: Optional[bytes] = None,
    ) -> List[SyncResult]:
        """
        Sync several ranges from one PDF revision. Each range is applied in
        its own transaction, so one range's rejection does not block the
        others.
        """
        if pdf_bytes is None:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
        return [
            self.sync(spec, pdf_path, version, pdf_bytes=pdf_bytes) for spec in specs
        ]

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def current(self, range_name: str) -> List[RecordView]:
        """The currently effective records of a range, in position order."""
        rows = self._conn.execute(
            "SELECT r.*, v.version_label, v.version_type, v.pdf_path,"
            " v.pdf_sha256 FROM ledger_record r"
            " JOIN ledger_version v ON v.id = r.version_id"
            " WHERE r.range_name = ?",
            (range_name,),
        ).fetchall()
        views = [self._row_to_view(range_name, r) for r in rows]
        views.sort(key=lambda v: (v.position.page, v.position.table, v.position.row))
        return views

    def get(self, range_name: str, key: Sequence[str]) -> Optional[RecordView]:
        """One currently effective record by business key, or ``None``."""
        row = self._conn.execute(
            "SELECT r.*, v.version_label, v.version_type, v.pdf_path,"
            " v.pdf_sha256 FROM ledger_record r"
            " JOIN ledger_version v ON v.id = r.version_id"
            " WHERE r.range_name = ? AND r.record_key = ?",
            (range_name, _encode_key(tuple(normalize_text(k) for k in key))),
        ).fetchone()
        return None if row is None else self._row_to_view(range_name, row)

    def provenance(
        self, range_name: str, key: Sequence[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Trace one record back to its source PDF: path, SHA-256, page, and
        cell bounding boxes in the version that last touched the record.
        """
        view = self.get(range_name, key)
        if view is None:
            return None
        row = self._conn.execute(
            "SELECT received_at FROM ledger_version WHERE id = ?",
            (view.version_id,),
        ).fetchone()
        return {
            "range_name": range_name,
            "key": list(view.key),
            "version": view.version,
            "version_id": view.version_id,
            "pdf_path": view.pdf_path,
            "pdf_sha256": view.pdf_sha256,
            "page": view.position.page,
            "bbox": list(view.position.bbox),
            "cells": {
                k: (list(v) if v is not None else None)
                for k, v in view.position.cells.items()
            },
            "received_at": row["received_at"] if row else None,
        }

    def versions(
        self, range_name: str, status: Optional[str] = None
    ) -> List[VersionInfo]:
        """The logged sync events of a range, oldest first."""
        sql = "SELECT * FROM ledger_version WHERE range_name = ?"
        params: List[Any] = [range_name]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        return [self._row_to_version_info(r) for r in self._conn.execute(sql, params)]

    def changes(
        self, range_name: str, version: Optional[T_version] = None
    ) -> List[ChangeEvent]:
        """Persisted change records of a range, optionally for one version."""
        sql = (
            "SELECT c.*, v.version_label, v.version_type, v.pdf_path,"
            " v.pdf_sha256, v.received_at FROM ledger_change c"
            " JOIN ledger_version v ON v.id = c.version_id"
            " WHERE c.range_name = ?"
        )
        params: List[Any] = [range_name]
        if version is not None:
            sql += " AND v.version_label = ?"
            params.append(str(version))
        sql += " ORDER BY c.id"
        return [self._row_to_change_event(r) for r in self._conn.execute(sql, params)]

    def history(self, range_name: str, key: Sequence[str]) -> List[ChangeEvent]:
        """All change records of one record, oldest first."""
        sql = (
            "SELECT c.*, v.version_label, v.version_type, v.pdf_path,"
            " v.pdf_sha256, v.received_at FROM ledger_change c"
            " JOIN ledger_version v ON v.id = c.version_id"
            " WHERE c.range_name = ? AND c.record_key = ? ORDER BY c.id"
        )
        key_json = _encode_key(tuple(normalize_text(k) for k in key))
        return [
            self._row_to_change_event(r)
            for r in self._conn.execute(sql, (range_name, key_json))
        ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _current_applied(self, range_name: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM ledger_version WHERE range_name = ?"
            " AND status = 'applied' ORDER BY applied_seq DESC LIMIT 1",
            (range_name,),
        ).fetchone()

    def _applied_by_label(self, range_name: str, label: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM ledger_version WHERE range_name = ?"
            " AND version_label = ? AND status = 'applied'"
            " ORDER BY applied_seq DESC LIMIT 1",
            (range_name, label),
        ).fetchone()

    def _next_applied_seq(self, range_name: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(applied_seq), 0) + 1 AS seq"
            " FROM ledger_version WHERE range_name = ? AND status = 'applied'",
            (range_name,),
        ).fetchone()
        return int(row["seq"])

    def _record_attempt(
        self,
        range_name: str,
        label: str,
        vtype: str,
        status: str,
        pdf_path: Optional[str],
        pdf_sha256: Optional[str],
        page_count: Optional[int] = None,
        record_count: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO ledger_version (range_name, version_label,"
                " version_type, status, pdf_path, pdf_sha256, page_count,"
                " record_count, applied_seq, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    range_name,
                    label,
                    vtype,
                    status,
                    pdf_path,
                    pdf_sha256,
                    page_count,
                    record_count,
                    _dumps(detail or {}),
                ),
            )
        return int(cur.lastrowid)

    def _load_snapshot(self, range_name: str) -> Dict[T_key, LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_record WHERE range_name = ?", (range_name,)
        ).fetchall()
        snapshot: Dict[T_key, LedgerRecord] = {}
        for r in rows:
            key = _decode_key(r["record_key"])
            snapshot[key] = LedgerRecord(
                key=key,
                fields=json.loads(r["fields"]),
                position=CellPosition.from_dict(json.loads(r["position"])),
                content_hash=r["content_hash"],
            )
        return snapshot

    def _insert_change_tx(
        self, range_name: str, version_id: int, change: Change
    ) -> None:
        self._conn.execute(
            "INSERT INTO ledger_change (version_id, range_name, record_key,"
            " kind, old_fields, new_fields, old_position, new_position,"
            " changed_columns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                range_name,
                _encode_key(change.key),
                change.kind,
                _dumps(change.old_fields) if change.old_fields is not None else None,
                _dumps(change.new_fields) if change.new_fields is not None else None,
                (
                    _dumps(change.old_position.to_dict())
                    if change.old_position is not None
                    else None
                ),
                (
                    _dumps(change.new_position.to_dict())
                    if change.new_position is not None
                    else None
                ),
                _dumps(change.changed_columns),
            ),
        )

    def _apply_snapshot_tx(
        self,
        range_name: str,
        version_id: int,
        changes: List[Change],
        incoming: Mapping[T_key, LedgerRecord],
    ) -> None:
        for change in changes:
            key_json = _encode_key(change.key)
            if change.kind == REMOVED:
                self._conn.execute(
                    "DELETE FROM ledger_record"
                    " WHERE range_name = ? AND record_key = ?",
                    (range_name, key_json),
                )
            else:
                rec = incoming[change.key]
                self._conn.execute(
                    "INSERT OR REPLACE INTO ledger_record (range_name,"
                    " record_key, fields, content_hash, position, version_id)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        range_name,
                        key_json,
                        _dumps(rec.fields),
                        rec.content_hash,
                        _dumps(rec.position.to_dict()),
                        version_id,
                    ),
                )
        changed_keys = {_encode_key(c.key) for c in changes}
        for key, rec in incoming.items():
            key_json = _encode_key(key)
            if key_json in changed_keys:
                continue
            # Unchanged content: refresh the provenance silently. A pure
            # move is not a content change, so no change record is created.
            self._conn.execute(
                "UPDATE ledger_record SET position = ?, version_id = ?"
                " WHERE range_name = ? AND record_key = ?",
                (_dumps(rec.position.to_dict()), version_id, range_name, key_json),
            )

    def _row_to_view(self, range_name: str, row: sqlite3.Row) -> RecordView:
        return RecordView(
            range_name=range_name,
            key=_decode_key(row["record_key"]),
            fields=json.loads(row["fields"]),
            position=CellPosition.from_dict(json.loads(row["position"])),
            version=_decode_version(row["version_label"], row["version_type"]),
            version_id=row["version_id"],
            pdf_path=row["pdf_path"],
            pdf_sha256=row["pdf_sha256"],
        )

    def _row_to_version_info(self, row: sqlite3.Row) -> VersionInfo:
        return VersionInfo(
            id=row["id"],
            range_name=row["range_name"],
            version=_decode_version(row["version_label"], row["version_type"]),
            status=row["status"],
            pdf_path=row["pdf_path"],
            pdf_sha256=row["pdf_sha256"],
            page_count=row["page_count"],
            record_count=row["record_count"],
            applied_seq=row["applied_seq"],
            received_at=row["received_at"],
            detail=json.loads(row["detail"]),
        )

    def _row_to_change_event(self, row: sqlite3.Row) -> ChangeEvent:
        change = Change(
            kind=row["kind"],
            key=_decode_key(row["record_key"]),
            old_fields=(json.loads(row["old_fields"]) if row["old_fields"] else None),
            new_fields=(json.loads(row["new_fields"]) if row["new_fields"] else None),
            old_position=(
                CellPosition.from_dict(json.loads(row["old_position"]))
                if row["old_position"]
                else None
            ),
            new_position=(
                CellPosition.from_dict(json.loads(row["new_position"]))
                if row["new_position"]
                else None
            ),
            changed_columns=json.loads(row["changed_columns"] or "[]"),
        )
        return ChangeEvent(
            change=change,
            range_name=row["range_name"],
            version=_decode_version(row["version_label"], row["version_type"]),
            version_id=row["version_id"],
            pdf_path=row["pdf_path"],
            pdf_sha256=row["pdf_sha256"],
            received_at=row["received_at"],
        )
