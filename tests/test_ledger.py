#!/usr/bin/env python
import hashlib
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pytest

import pdfplumber
from pdfplumber.ledger import (
    ADDED,
    APPLIED,
    DUPLICATE,
    REJECTED,
    REMOVED,
    STALE,
    UPDATED,
    CellPosition,
    Change,
    ColumnSpec,
    LedgerRecord,
    LedgerSync,
    RangeSpec,
    diff_records,
    extract_range,
    normalize_text,
)

logging.disable(logging.ERROR)

HERE = os.path.abspath(os.path.dirname(__file__))

PAGE_W, PAGE_H = 612, 792
MARGIN = 50
ROW_H = 24


def _escape(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(pages):
    """
    Build a minimal valid PDF. ``pages`` is a list of pages; each page is
    either None (a blank page) or a list of rows, where each row is a list
    of cell strings drawn inside a ruled grid. Returns the PDF bytes.
    """
    objects = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append(
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("latin-1"))
    )
    objects.append((3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    for i, rows in enumerate(pages):
        page_obj = 4 + 2 * i
        content_obj = 5 + 2 * i
        objects.append(
            (
                page_obj,
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {content_obj} 0 R >>"
                ).encode("latin-1"),
            )
        )
        cmds = ["1 w"]
        if rows:
            ncols = max(len(r) for r in rows)
            col_w = (PAGE_W - 2 * MARGIN) / ncols
            for ri, row in enumerate(rows):
                y = PAGE_H - MARGIN - (ri + 1) * ROW_H
                for ci in range(ncols):
                    x = MARGIN + ci * col_w
                    cmds.append(f"{x:.2f} {y:.2f} {col_w:.2f} {ROW_H:.2f} re S")
                    text = row[ci] if ci < len(row) else ""
                    if text:
                        cmds.append(
                            f"BT /F1 10 Tf {x + 3:.2f} {y + 8:.2f} "
                            f"Td ({_escape(text)}) Tj ET"
                        )
        stream = "\n".join(cmds).encode("latin-1")
        objects.append(
            (
                content_obj,
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
            )
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\n" f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


HEADER = ["Supplier", "Invoice", "Amount", "Due"]

V1_PAGES = [
    [
        HEADER,
        ["Acme", "INV-001", "120.00", "2026-12-01"],
        ["Globex", "INV-002", "80.50", "2026-12-05"],
    ],
    [
        HEADER,
        ["Initech", "INV-003", "42.00", "2026-12-10"],
    ],
]

# Same records as V1, re-paginated: positions changed, content identical.
V2_PAGES = [
    [
        HEADER,
        ["Globex", "INV-002", "80.50", "2026-12-05"],
    ],
    [
        HEADER,
        ["Initech", "INV-003", "42.00", "2026-12-10"],
        ["Acme", "INV-001", "120.00", "2026-12-01"],
    ],
]

# INV-002 amount changed, INV-004 added, INV-003 removed, INV-001 moved.
V3_PAGES = [
    [
        HEADER,
        ["Globex", "INV-002", "99.00", "2026-12-05"],
        ["Umbrella", "INV-004", "10.00", "2026-12-20"],
    ],
    [
        HEADER,
        ["Acme", "INV-001", "120.00", "2026-12-01"],
    ],
]

# Same logical content as V1 but regenerated (extra blank row): different
# bytes, same records. Used to test same-version retries with new bytes.
V1_REGENERATED = [page + [["", "", "", ""]] for page in V1_PAGES]

DUPLICATE_KEY_PAGES = [
    [
        HEADER,
        ["Acme", "INV-001", "120.00", "2026-12-01"],
        ["Acme", "INV-001", "120.00", "2026-12-01"],
    ],
    [HEADER],
]

MISSING_KEY_PAGES = [
    [
        HEADER,
        ["Acme", "INV-001", "120.00", "2026-12-01"],
        ["Globex", "", "80.50", "2026-12-05"],
    ],
    [HEADER],
]

HEADER_ONLY_PAGES = [[HEADER], [HEADER]]


def ap_spec(**overrides):
    kwargs = dict(
        name="ap",
        key=("supplier", "invoice"),
        columns=(
            ColumnSpec("supplier", header="Supplier"),
            ColumnSpec("invoice", header="Invoice"),
            ColumnSpec("amount", header="Amount"),
            ColumnSpec("due", header="Due"),
        ),
        pages=(1, 2),
    )
    kwargs.update(overrides)
    return RangeSpec(**kwargs)


class TestLedgerSync(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sync = LedgerSync(":memory:")

    def tearDown(self):
        self.sync.close()
        shutil.rmtree(self.tmpdir)

    def write_pdf(self, name, pages):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(build_pdf(pages))
        return path

    def apply(self, pages, name, version, spec=None):
        path = self.write_pdf(name, pages)
        return self.sync.sync(spec or ap_spec(), path, version)

    # --------------------------------------------------------------
    # basic application and change output
    # --------------------------------------------------------------

    def test_initial_load_applies_additions(self):
        result = self.apply(V1_PAGES, "v1.pdf", 1)
        assert result.status == APPLIED
        assert result.applied
        assert len(result.changes) == 3
        assert all(c.kind == ADDED for c in result.changes)
        by_key = {c.key: c for c in result.changes}
        inv1 = by_key[("Acme", "INV-001")]
        assert inv1.new_fields["amount"] == "120.00"
        assert inv1.new_position.page == 1
        inv3 = by_key[("Initech", "INV-003")]
        assert inv3.new_position.page == 2
        current = self.sync.current("ap")
        assert len(current) == 3
        assert [v.key for v in current][0] == ("Acme", "INV-001")

    def test_repagination_is_not_a_content_change(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        result = self.apply(V2_PAGES, "v2.pdf", 2)
        assert result.status == APPLIED
        # A pure position move is not a content change.
        assert result.changes == []
        # ... but the provenance follows the record to its new position.
        view = self.sync.get("ap", ("Acme", "INV-001"))
        assert view.position.page == 2
        assert view.version == 2
        # No new change records were created for the move.
        assert len(self.sync.changes("ap")) == 3

    def test_update_add_remove_with_old_and_new_positions(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        self.apply(V2_PAGES, "v2.pdf", 2)
        result = self.apply(V3_PAGES, "v3.pdf", 3)
        assert result.status == APPLIED
        assert len(result.added) == 1
        assert len(result.updated) == 1
        assert len(result.removed) == 1

        updated = result.updated[0]
        assert updated.key == ("Globex", "INV-002")
        assert updated.changed_columns == ["amount"]
        assert updated.old_fields["amount"] == "80.50"
        assert updated.new_fields["amount"] == "99.00"
        assert updated.old_position.page == 1
        assert updated.new_position.page == 1

        added = result.added[0]
        assert added.key == ("Umbrella", "INV-004")
        assert added.new_position.page == 1

        removed = result.removed[0]
        assert removed.key == ("Initech", "INV-003")
        assert removed.old_fields["amount"] == "42.00"
        assert removed.old_position.page == 2

        keys = {v.key for v in self.sync.current("ap")}
        assert keys == {
            ("Acme", "INV-001"),
            ("Globex", "INV-002"),
            ("Umbrella", "INV-004"),
        }

    # --------------------------------------------------------------
    # integrity gate
    # --------------------------------------------------------------

    def test_missing_pages_rejected_and_current_kept(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        spec = ap_spec(pages=(1, 2, 3))
        path = self.write_pdf("short.pdf", V2_PAGES)  # only 2 pages
        result = self.sync.sync(spec, path, 2)
        assert result.status == REJECTED
        assert result.changes == []
        codes = {i.code for i in result.integrity.issues}
        assert "missing_pages" in codes
        # Current data is kept untouched.
        assert len(self.sync.current("ap")) == 3
        applied = self.sync.versions("ap", status=APPLIED)
        assert [v.version for v in applied] == [1]

    def test_blank_declared_page_rejected(self):
        pages = [V1_PAGES[0], None]  # second declared page has no table
        result = self.apply(pages, "blank.pdf", 1)
        assert result.status == REJECTED
        codes = {i.code for i in result.integrity.issues}
        assert "no_table_on_page" in codes

    def test_duplicate_key_rejected_and_current_kept(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        result = self.apply(DUPLICATE_KEY_PAGES, "dup.pdf", 2)
        assert result.status == REJECTED
        codes = {i.code for i in result.integrity.issues}
        assert "duplicate_key" in codes
        # The conflicting version does not touch the snapshot.
        assert len(self.sync.current("ap")) == 3
        assert self.sync.get("ap", ("Acme", "INV-001")).version == 1

    def test_missing_key_component_rejected(self):
        result = self.apply(MISSING_KEY_PAGES, "nokey.pdf", 1)
        assert result.status == REJECTED
        codes = {i.code for i in result.integrity.issues}
        assert "missing_key" in codes

    def test_empty_range_rejected_unless_allowed(self):
        result = self.apply(HEADER_ONLY_PAGES, "empty.pdf", 1)
        assert result.status == REJECTED
        codes = {i.code for i in result.integrity.issues}
        assert "empty_range" in codes

        # With allow_empty, integrity passes and every current record is
        # recognized as deleted.
        self.apply(V1_PAGES, "v1.pdf", 1)
        spec = ap_spec(allow_empty=True)
        path = self.write_pdf("empty2.pdf", HEADER_ONLY_PAGES)
        result = self.sync.sync(spec, path, 2)
        assert result.status == APPLIED
        assert len(result.removed) == 3
        assert self.sync.current("ap") == []

    def test_rejected_version_can_be_resubmitted_after_fix(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        bad = self.apply(DUPLICATE_KEY_PAGES, "v2-bad.pdf", 2)
        assert bad.status == REJECTED
        good = self.apply(V2_PAGES, "v2-fixed.pdf", 2)
        assert good.status == APPLIED
        applied = self.sync.versions("ap", status=APPLIED)
        assert [v.version for v in applied] == [1, 2]

    def test_corrupt_pdf_rejected(self):
        path = os.path.join(self.tmpdir, "corrupt.pdf")
        with open(path, "wb") as f:
            f.write(b"this is not a pdf")
        result = self.sync.sync(ap_spec(), path, 1)
        assert result.status == REJECTED
        codes = {i.code for i in result.integrity.issues}
        assert "extraction_error" in codes

    # --------------------------------------------------------------
    # idempotency and version ordering
    # --------------------------------------------------------------

    def test_same_version_retry_emits_no_changes(self):
        first = self.apply(V1_PAGES, "v1.pdf", 1)
        assert first.status == APPLIED
        retry = self.apply(V1_PAGES, "v1-copy.pdf", 1)
        assert retry.status == DUPLICATE
        assert retry.changes == []
        # No new change records, no snapshot mutation.
        assert len(self.sync.changes("ap")) == 3
        assert len(self.sync.current("ap")) == 3
        statuses = [v.status for v in self.sync.versions("ap")]
        assert statuses == [APPLIED, DUPLICATE]

    def test_same_version_retry_with_different_bytes_warns(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        result = self.apply(V1_REGENERATED, "v1-regen.pdf", 1)
        assert result.status == DUPLICATE
        assert result.changes == []
        assert any("differ" in w for w in result.warnings)

    def test_stale_version_cannot_overwrite_newer_data(self):
        self.apply(V1_PAGES, "v1.pdf", 10)
        # A late old version (never applied) must not overwrite newer data.
        result = self.apply(V3_PAGES, "old.pdf", 5)
        assert result.status == STALE
        assert result.changes == []
        assert result.effective_version == 10
        current = self.sync.current("ap")
        assert len(current) == 3
        assert all(v.version == 10 for v in current)
        assert len(self.sync.changes("ap")) == 3

    def test_version_type_mismatch_raises(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        path = self.write_pdf("v2.pdf", V2_PAGES)
        with pytest.raises(TypeError):
            self.sync.sync(ap_spec(), path, "2")

    def test_invalid_version_type_raises(self):
        path = self.write_pdf("v1.pdf", V1_PAGES)
        with pytest.raises(TypeError):
            self.sync.sync(ap_spec(), path, 1.5)
        with pytest.raises(TypeError):
            self.sync.sync(ap_spec(), path, True)

    def test_string_versions_order_lexicographically(self):
        self.apply(V1_PAGES, "v1.pdf", "2026-10-01")
        result = self.apply(V2_PAGES, "v2.pdf", "2026-11-01")
        assert result.status == APPLIED
        stale = self.apply(V3_PAGES, "v3.pdf", "2026-09-01")
        assert stale.status == STALE

    # --------------------------------------------------------------
    # atomicity
    # --------------------------------------------------------------

    def test_snapshot_and_changes_commit_atomically(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        with mock.patch.object(
            self.sync, "_insert_change_tx", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError):
                self.apply(V3_PAGES, "v3.pdf", 2)
        # The failed application left no trace: no version row, no change
        # records, no snapshot mutation.
        assert [v.version for v in self.sync.versions("ap")] == [1]
        assert len(self.sync.changes("ap")) == 3
        current = self.sync.current("ap")
        assert len(current) == 3
        assert all(v.version == 1 for v in current)
        # And the range still accepts the fixed version afterwards.
        result = self.apply(V3_PAGES, "v3.pdf", 2)
        assert result.status == APPLIED

    # --------------------------------------------------------------
    # traceability
    # --------------------------------------------------------------

    def test_provenance_traces_back_to_source_pdf(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        v3_path = self.write_pdf("v3.pdf", V3_PAGES)
        self.sync.sync(ap_spec(), v3_path, 2)

        prov = self.sync.provenance("ap", ("Acme", "INV-001"))
        assert prov["version"] == 2
        assert prov["pdf_path"] == v3_path
        with open(v3_path, "rb") as f:
            assert prov["pdf_sha256"] == hashlib.sha256(f.read()).hexdigest()
        assert prov["page"] == 2

        # The recorded cell position locates the record in the original PDF.
        with pdfplumber.open(v3_path) as pdf:
            page = pdf.pages[prov["page"] - 1]
            text = page.crop(tuple(prov["bbox"])).extract_text()
        assert "Acme" in text
        assert "INV-001" in text

    def test_history_chains_versions(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        self.apply(V3_PAGES, "v3.pdf", 2)
        history = self.sync.history("ap", ("Globex", "INV-002"))
        assert [h.change.kind for h in history] == [ADDED, UPDATED]
        assert [h.version for h in history] == [1, 2]
        assert history[1].change.old_fields["amount"] == "80.50"
        assert history[1].change.new_fields["amount"] == "99.00"
        assert history[1].pdf_path.endswith("v3.pdf")

    def test_changes_query_filters_by_version(self):
        self.apply(V1_PAGES, "v1.pdf", 1)
        self.apply(V3_PAGES, "v3.pdf", 2)
        assert len(self.sync.changes("ap")) == 3 + 3
        v2_changes = self.sync.changes("ap", version=2)
        assert {c.change.kind for c in v2_changes} == {ADDED, UPDATED, REMOVED}

    # --------------------------------------------------------------
    # ranges and extraction modes
    # --------------------------------------------------------------

    def test_multiple_ranges_in_one_pdf(self):
        pages = [
            [
                HEADER,
                ["Acme", "INV-001", "120.00", "2026-12-01"],
            ],
            [
                ["Customer", "Receipt", "Amount"],
                ["Wayne", "RCPT-1", "55.00"],
            ],
        ]
        ar_spec = RangeSpec(
            name="ar",
            key=("receipt",),
            columns=(
                ColumnSpec("customer", header="Customer"),
                ColumnSpec("receipt", header="Receipt"),
                ColumnSpec("amount", header="Amount"),
            ),
            pages=(2,),
        )
        spec = ap_spec(pages=(1,))
        path = self.write_pdf("book.pdf", pages)
        results = self.sync.sync_many([spec, ar_spec], path, 1)
        assert [r.status for r in results] == [APPLIED, APPLIED]
        assert len(self.sync.current("ap")) == 1
        assert len(self.sync.current("ar")) == 1
        # Versions are tracked per range.
        assert self.sync.versions("ap")[0].range_name == "ap"
        assert self.sync.versions("ar")[0].range_name == "ar"

    def test_positional_columns_without_header(self):
        pages = [
            [
                ["Acme", "INV-001", "120.00"],
                ["Globex", "INV-002", "80.50"],
            ]
        ]
        spec = RangeSpec(
            name="pos",
            key=("invoice",),
            columns=(
                ColumnSpec("supplier", index=0),
                ColumnSpec("invoice", index=1),
                ColumnSpec("amount", index=2),
            ),
            pages=(1,),
            header="none",
        )
        result = self.apply(pages, "pos.pdf", 1, spec=spec)
        assert result.status == APPLIED
        assert len(result.changes) == 2
        view = self.sync.get("pos", ("INV-001",))
        assert view.fields == {
            "supplier": "Acme",
            "invoice": "INV-001",
            "amount": "120.00",
        }

    def test_header_each_tolerates_column_reordering(self):
        pages = [
            [
                ["Supplier", "Invoice", "Amount", "Due"],
                ["Acme", "INV-001", "120.00", "2026-12-01"],
            ],
            [
                ["Invoice", "Supplier", "Amount", "Due"],
                ["INV-002", "Globex", "80.50", "2026-12-05"],
            ],
        ]
        spec = ap_spec(header="each")
        result = self.apply(pages, "reordered.pdf", 1, spec=spec)
        assert result.status == APPLIED
        view = self.sync.get("ap", ("Globex", "INV-002"))
        assert view.fields["supplier"] == "Globex"
        assert view.fields["invoice"] == "INV-002"

    def test_table_settings_are_passed_through(self):
        spec = ap_spec(table_settings={"vertical_strategy": "lines"})
        result = self.apply(V1_PAGES, "v1.pdf", 1, spec=spec)
        assert result.status == APPLIED

    def test_extract_range_directly(self):
        path = self.write_pdf("v1.pdf", V1_PAGES)
        with pdfplumber.open(path) as pdf:
            records, report = extract_range(pdf, ap_spec())
        assert report.ok
        assert report.tables_found == 2
        assert report.rows_found == 3
        assert report.pages_checked == [1, 2]
        by_key = {r.key: r for r in records}
        assert by_key[("Acme", "INV-001")].fields["due"] == "2026-12-01"
        # Per-column cell bboxes are recorded for traceability.
        cells = by_key[("Acme", "INV-001")].position.cells
        assert set(cells) == {"supplier", "invoice", "amount", "due"}
        assert all(b is not None for b in cells.values())

    def test_lazy_submodule_attribute(self):
        assert pdfplumber.ledger.LedgerSync is LedgerSync


class TestDeclarations(unittest.TestCase):
    def test_unknown_key_column_rejected(self):
        with pytest.raises(ValueError):
            ap_spec(key=("nope",))

    def test_optional_key_column_rejected(self):
        with pytest.raises(ValueError):
            ap_spec(
                columns=(
                    ColumnSpec("supplier", header="Supplier"),
                    ColumnSpec("invoice", header="Invoice", required=False),
                ),
                key=("invoice",),
            )

    def test_index_requires_header_none(self):
        with pytest.raises(ValueError):
            ap_spec(columns=(ColumnSpec("a", index=0),), key=("a",))

    def test_header_none_requires_index(self):
        with pytest.raises(ValueError):
            RangeSpec(
                name="x",
                key=("a",),
                columns=(ColumnSpec("a"),),
                header="none",
            )

    def test_duplicate_header_text_rejected(self):
        with pytest.raises(ValueError):
            ap_spec(
                columns=(
                    ColumnSpec("a", header="Same"),
                    ColumnSpec("b", header="Same"),
                ),
                key=("a",),
            )

    def test_pages_normalized(self):
        spec = ap_spec(pages=[3, 1, 2, 2])
        assert spec.pages == (1, 2, 3)


class TestUnits(unittest.TestCase):
    def test_normalize_text(self):
        assert normalize_text(None) == ""
        assert normalize_text("  a\t b\n c ") == "a b c"
        assert normalize_text("供应商 甲") == "供应商 甲"

    def _record(self, key, amount, page=1):
        return LedgerRecord(
            key=key,
            fields={"invoice": key[0], "amount": amount},
            position=CellPosition(page=page, table=0, row=1, bbox=(0, 0, 1, 1)),
            content_hash="",
        )

    def test_diff_records_ignores_pure_moves(self):
        cur = self._record(("INV-1",), "10.00", page=1)
        inc = self._record(("INV-1",), "10.00", page=5)
        # Hashes are computed from content only, so a move is not a change.
        from pdfplumber.ledger import _content_hash

        cur.content_hash = _content_hash(cur.key, cur.fields)
        inc.content_hash = _content_hash(inc.key, inc.fields)
        assert cur.content_hash == inc.content_hash
        assert diff_records({cur.key: cur}, {inc.key: inc}) == []

    def test_diff_records_kinds(self):
        from pdfplumber.ledger import _content_hash

        def rec(key, amount, page=1):
            r = self._record(key, amount, page)
            r.content_hash = _content_hash(r.key, r.fields)
            return r

        current = {
            ("A",): rec(("A",), "1"),
            ("B",): rec(("B",), "2"),
            ("C",): rec(("C",), "3"),
        }
        incoming = {
            ("B",): rec(("B",), "2", page=9),  # moved only
            ("C",): rec(("C",), "30"),  # updated
            ("D",): rec(("D",), "4"),  # added
        }
        changes = diff_records(current, incoming)
        by_key = {c.key: c for c in changes}
        assert by_key[("A",)].kind == REMOVED
        assert by_key[("C",)].kind == UPDATED
        assert by_key[("C",)].changed_columns == ["amount"]
        assert by_key[("D",)].kind == ADDED
        assert ("B",) not in by_key

    def test_cell_position_dict_roundtrip(self):
        pos = CellPosition(
            page=2,
            table=1,
            row=3,
            bbox=(1.0, 2.0, 3.0, 4.0),
            cells={"a": (1.0, 2.0, 3.0, 4.0), "b": None},
        )
        again = CellPosition.from_dict(pos.to_dict())
        assert again == pos

    def test_sync_result_to_dict(self):
        result = self_test_sync_result()
        data = result.to_dict()
        assert data["status"] == APPLIED
        assert data["changes"][0]["kind"] == UPDATED


def self_test_sync_result():
    from pdfplumber.ledger import SyncResult

    return SyncResult(
        range_name="ap",
        version=1,
        status=APPLIED,
        changes=[Change(UPDATED, ("K",), changed_columns=["amount"])],
    )
