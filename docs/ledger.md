# Ledger synchronization

Suppliers periodically send revised editions of PDF ledgers. Because every
revision is re-paginated, record changes cannot be detected by page/line
numbers. `pdfplumber.ledger` adds ledger synchronization for this workflow:

- Records are extracted with pdfplumber's real table extraction and
  identified by **declared business keys**, not positions.
- Each incoming version of a declared **range** is processed as a full
  snapshot and compared against the currently effective version, producing
  additions, updates, and deletions — each annotated with old/new cell
  positions.
- A pure position move does **not** count as a content change.
- Deletions are only recognized when **integrity checks pass**; on key
  conflicts or missing pages the current data is kept.
- Re-applying the same version does not re-emit changes, and a late-arriving
  old version never overwrites newer data.
- The official snapshot and its change records are committed in a **single
  transaction**, and every record stays traceable to its source PDF (path,
  SHA-256, page, and cell bounding boxes).

## Declaring a range

A *range* is a named, independently versioned section of a ledger — for
example one account or one book. A `RangeSpec` declares its columns, its
business key, and the pages it is expected to occupy:

```python
from pdfplumber.ledger import ColumnSpec, LedgerSync, RangeSpec

spec = RangeSpec(
    name="ap-ledger",
    key=("supplier", "invoice"),          # the declared business key
    columns=(
        ColumnSpec("supplier", header="Supplier"),
        ColumnSpec("invoice", header="Invoice No"),
        ColumnSpec("amount", header="Amount"),
        ColumnSpec("due", header="Due Date", required=False),
    ),
    pages=(1, 2, 3),                       # expected 1-indexed pages
    table_settings=None,                   # passed to Page.find_tables
)
```

Notes:

- `pages=None` means "all pages of the PDF". Declaring explicit pages
  enables the missing-page integrity check.
- `header="first"` (default) reads the column mapping from the first
  table's first non-blank row and skips repeated header rows. Use
  `header="each"` when the supplier may reorder columns between pages
  (every table must then start with a header row), or `header="none"`
  together with `ColumnSpec(index=...)` for headerless tables.
- Key columns must be declared `required=True` (the default).

## Syncing versions

```python
sync = LedgerSync("ledger.db")             # SQLite store; ":memory:" works
result = sync.sync(spec, "ledger-2026-11.pdf", version="2026-11-01")

print(result.status)                       # applied | duplicate | stale | rejected
for change in result.changes:              # non-empty only when applied
    print(change.kind, change.key)
    print("  was:", change.old_fields, "at", change.old_position)
    print("  now:", change.new_fields, "at", change.new_position)
```

`version` is ordered per range and may be an `int` or a `str` (string
versions are ordered lexicographically — use ISO-8601 dates or zero-padded
sequence numbers, and don't mix types within a range).

Outcomes:

| status       | meaning                                                                 |
|--------------|--------------------------------------------------------------------------|
| `"applied"`  | integrity passed; snapshot + change records committed in one transaction |
| `"duplicate"`| this version was already applied; a pure no-op, `changes` is empty        |
| `"stale"`    | the version does not supersede the current one; current data kept         |
| `"rejected"` | integrity checks failed; current data kept                                |

Every `sync` call is logged in the `ledger_version` table with its status,
so rejected/stale/duplicate deliveries remain auditable. A rejected version
is *not* consumed: the supplier can resend the same version label after
fixing the file.

`sync.sync_many([spec_a, spec_b], path, version)` syncs several ranges from
one PDF revision; each range commits in its own transaction.

## Integrity checks

A version is rejected (and the current data kept) when any of these fail:

- `missing_pages` — a declared page is absent from the PDF.
- `no_table_on_page` — a declared page yielded no table
  (disable with `require_table_per_page=False`).
- `missing_column` — a required column's header was not found.
- `duplicate_key` — the same business key appears twice in the range.
- `missing_key` — a data row has an empty business-key component.
- `empty_range` — no data records at all (allow with `allow_empty=True`).
- `extraction_error` — the PDF could not be parsed.

Only when all checks pass may records missing from the incoming version be
recognized as deletions.

## Querying and tracing back to the PDF

```python
sync.current("ap-ledger")                  # effective records (RecordView)
sync.get("ap-ledger", ("Acme", "INV-001")) # one record by business key
sync.versions("ap-ledger")                 # all logged sync events
sync.changes("ap-ledger", version="2026-11-01")  # persisted change records
sync.history("ap-ledger", ("Acme", "INV-001"))   # one record's change chain

prov = sync.provenance("ap-ledger", ("Acme", "INV-001"))
# -> {"pdf_path": ..., "pdf_sha256": ..., "page": 2, "bbox": [...],
#     "cells": {"supplier": [...], ...}, "version": ..., ...}
```

The provenance locates the record in the original PDF, e.g.:

```python
import pdfplumber

with pdfplumber.open(prov["pdf_path"]) as pdf:
    page = pdf.pages[prov["page"] - 1]
    print(page.crop(tuple(prov["bbox"])).extract_text())
```

## Lower-level pieces

- `extract_range(pdf, spec)` — extract a range's full record set from an
  open `pdfplumber.PDF`, returning `(records, integrity_report)`.
- `diff_records(current, incoming)` — compare two snapshots keyed by
  business key; pure position moves yield no changes.
