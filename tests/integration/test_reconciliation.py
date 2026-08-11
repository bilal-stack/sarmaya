"""Bank reconciliation: comparing what we instructed against what the bank did.

Two findings matter, and the second is why the module exists:

  * a released run the bank never confirmed — the vendor is unpaid and nobody
    noticed;
  * a debit no instruction explains — which cannot be produced by any mistake
    inside the workflow.

Matching is a suggestion a human confirms. These tests hold that line: nothing
matches itself, a wrong-amount debit is never offered, and the person who
released a payment cannot be the one who certifies it cleared.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import UserRole, InvoiceState, PaymentState, VendorStatus
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.payment import PaymentCreate
from app.services.config_provisioning import ConfigProvisioningService
from app.services.payment_service import PaymentService
from app.services.reconciliation import ReconciliationService
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Payable Vendor",
        status=VendorStatus.ACTIVE, bank_account_name="Payable Vendor Ltd",
        bank_account_number="0123456789", bank_name="Demo Bank",
        iban="PK36SCBL0000001123456702", swift_code="SCBLPKKX",
    )
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant,
        "vendor": vendor,
        "clerk": make_user(UserRole.AP_CLERK),
        "other_clerk": make_user(UserRole.AP_CLERK),
        "cfo": make_user(UserRole.CFO),
        "admin": make_user(UserRole.ADMIN),
    }


def _released_payment(db, setup, amount="80000"):
    """A run that has actually been authorised — the only thing a bank debit
    can legitimately match."""
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        vendor_id=setup["vendor"].id, vendor_name=setup["vendor"].legal_name,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
        current_state=InvoiceState.APPROVED, created_by=setup["clerk"]["id"],
    )
    db.add(invoice)
    db.flush()

    service = PaymentService(db)
    payment = service.prepare_payment(
        [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
    )
    service.submit_for_release(payment.id, setup["clerk"])
    return service.release_payment(payment.id, setup["cfo"])


def _statement_csv(payment_number, amount, payment_date, counterparty="Payable Vendor"):
    return (
        "date,description,counterparty,reference,amount\n"
        f"{payment_date.isoformat()},Payment {payment_number},{counterparty},"
        f"{payment_number},-{amount}\n"
    )


def _import(db, setup, content, actor=None):
    return ReconciliationService(db).import_statement(
        content, actor or setup["clerk"], filename="statement.csv"
    )


def _actions(db, object_id):
    return [
        a.action for a in
        db.query(AuditLog).filter(AuditLog.object_id == object_id).all()
    ]


class TestImporting:

    def test_a_statement_becomes_lines(self, db, setup):
        statement = _import(db, setup, _statement_csv("PAY-00001", "80000.00", date(2026, 8, 5)))

        assert statement.source_format == "csv"
        assert len(statement.lines) == 1
        assert statement.lines[0].is_debit is True
        assert statement.lines[0].amount == Decimal("80000.00")

    def test_the_same_file_cannot_be_imported_twice(self, db, setup):
        """Duplicating every transaction invents money that never moved, and
        offers a second candidate for a payment already reconciled — the
        reconciliation would then be confidently wrong."""
        content = _statement_csv("PAY-00001", "80000.00", date(2026, 8, 5))
        _import(db, setup, content)

        with pytest.raises(ValueError, match="already imported"):
            _import(db, setup, content)

    def test_an_unreadable_file_is_refused(self, db, setup):
        with pytest.raises(ValueError, match="Could not read the statement"):
            _import(db, setup, "this is not a statement at all")

    def test_the_import_is_audited_with_the_file_hash(self, db, setup):
        statement = _import(db, setup, _statement_csv("PAY-1", "100.00", date(2026, 8, 5)))
        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == statement.id, AuditLog.action == "imported")
            .first()
        )
        assert entry is not None
        assert entry.after_value["sha256"] == statement.file_hash

    def test_a_viewer_cannot_import(self, db, setup):
        """Reading the bank's record and adding to it are different rights."""
        with pytest.raises(PermissionError, match="import bank statements"):
            _import(db, setup, _statement_csv("PAY-1", "100.00", date(2026, 8, 5)),
                    actor=setup["cfo"])


class TestSuggestions:

    def test_a_matching_debit_suggests_its_payment(self, db, setup):
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))

        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert candidates
        assert candidates[0].payment.id == payment.id
        assert candidates[0].confidence == "high"

    def test_the_reasoning_is_returned_not_just_a_score(self, db, setup):
        """A reconciler asked to confirm needs to see why. An opaque number
        makes confirmation a formality."""
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))

        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        reasons = " ".join(candidates[0].reasons)
        assert payment.payment_number in reasons
        assert "Amount matches exactly" in candidates[0].reasons

    def test_a_different_amount_is_never_suggested(self, db, setup):
        """The strongest disqualifier there is: it cannot be outweighed by a
        date and a name that happen to fit."""
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "12345.00", payment.payment_date
        ))

        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert candidates == []

    def test_an_unreleased_run_is_never_suggested(self, db, setup):
        """A draft was never instructed, so a debit resembling one is a finding
        rather than a match."""
        payment = _released_payment(db, setup, "80000")
        payment.current_state = PaymentState.DRAFT
        db.flush()

        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert candidates == []

    def test_a_credit_has_no_candidates(self, db, setup):
        """A credit did not pay anyone."""
        _released_payment(db, setup, "80000")
        statement = _import(db, setup, "date,amount\n2026-08-05,80000.00\n")

        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert candidates == []

    def test_a_near_miss_is_offered_and_says_so(self, db, setup):
        """Bank charges deducted at source are the usual explanation, and are
        worth surfacing rather than hiding."""
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "79950.00", payment.payment_date
        ))

        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert candidates
        assert any("bank charges" in r for r in candidates[0].reasons)

    def test_nothing_is_matched_by_merely_suggesting(self, db, setup):
        """The whole design: a suggestion changes no state."""
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )

        db.refresh(statement.lines[0])
        assert statement.lines[0].matched_payment_id is None


class TestConfirmingAMatch:

    def _pair(self, db, setup, amount="80000"):
        payment = _released_payment(db, setup, amount)
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, f"{Decimal(amount):.2f}", payment.payment_date
        ))
        return payment, statement.lines[0]

    def test_a_human_confirms_and_the_line_is_matched(self, db, setup):
        payment, line = self._pair(db, setup)
        matched = ReconciliationService(db).confirm_match(
            line.id, payment.id, setup["clerk"]
        )

        assert matched.matched_payment_id == payment.id
        assert matched.matched_by is not None
        assert matched.matched_at is not None

    def test_the_releaser_cannot_certify_their_own_release(self, db, setup):
        """Reconciliation is the check on the release. One person holding both
        controls the instruction and the evidence for it."""
        payment, line = self._pair(db, setup)

        # The CFO released it; give them reconcile rights so it is the SoD rule
        # that refuses, not a missing permission.
        releaser_with_rights = {**setup["cfo"], "role": UserRole.ADMIN.value}
        with pytest.raises(PermissionError, match="Segregation of duties"):
            ReconciliationService(db).confirm_match(
                line.id, payment.id, releaser_with_rights
            )

    def test_a_refused_match_is_audited(self, db, setup):
        payment, line = self._pair(db, setup)
        releaser_with_rights = {**setup["cfo"], "role": UserRole.ADMIN.value}
        with pytest.raises(PermissionError):
            ReconciliationService(db).confirm_match(
                line.id, payment.id, releaser_with_rights
            )

        assert "reconciliation_blocked" in _actions(db, payment.id)

    def test_the_suggestion_that_was_shown_is_recorded(self, db, setup):
        """A match a human made against a weak suggestion is exactly what a
        later reviewer wants to find, and it is unrecoverable if not written
        down at the moment of confirmation."""
        payment, line = self._pair(db, setup)
        ReconciliationService(db).confirm_match(line.id, payment.id, setup["clerk"])

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == payment.id, AuditLog.action == "reconciled")
            .first()
        )
        assert entry is not None
        assert entry.after_value["suggested_score"] > 0
        assert entry.after_value["suggested_reasons"]

    def test_confirming_against_no_suggestion_is_flagged(self, db, setup):
        """A human may match anything; the system records that it disagreed."""
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            "UNRELATED", "12345.00", payment.payment_date
        ))
        ReconciliationService(db).confirm_match(
            statement.lines[0].id, payment.id, setup["clerk"]
        )

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == payment.id, AuditLog.action == "reconciled")
            .first()
        )
        assert entry.after_value["confirmed_against_suggestion"] is True

    def test_an_unreleased_payment_cannot_be_matched(self, db, setup):
        payment, line = self._pair(db, setup)
        payment.current_state = PaymentState.DRAFT
        db.flush()

        with pytest.raises(ValueError, match="not released"):
            ReconciliationService(db).confirm_match(line.id, payment.id, setup["clerk"])

    def test_a_credit_cannot_settle_a_payment(self, db, setup):
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, "date,amount\n2026-08-05,80000.00\n")

        with pytest.raises(ValueError, match="credit did not pay anyone"):
            ReconciliationService(db).confirm_match(
                statement.lines[0].id, payment.id, setup["clerk"]
            )

    def test_one_payment_cannot_be_matched_to_two_debits(self, db, setup):
        """Two bank debits for one instruction is a duplicate payment, not a
        reconciliation — and matching both would hide it."""
        payment, line = self._pair(db, setup)
        service = ReconciliationService(db)
        service.confirm_match(line.id, payment.id, setup["clerk"])

        second = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date + timedelta(days=1)
        ))
        with pytest.raises(ValueError, match="already matched to another"):
            service.confirm_match(second.lines[0].id, payment.id, setup["clerk"])

    def test_a_matched_line_cannot_be_rematched(self, db, setup):
        payment, line = self._pair(db, setup)
        other = _released_payment(db, setup, "80000")
        service = ReconciliationService(db)
        service.confirm_match(line.id, payment.id, setup["clerk"])

        with pytest.raises(ValueError, match="already matched"):
            service.confirm_match(line.id, other.id, setup["clerk"])

    def test_undoing_a_match_needs_a_reason(self, db, setup):
        payment, line = self._pair(db, setup)
        service = ReconciliationService(db)
        service.confirm_match(line.id, payment.id, setup["clerk"])

        with pytest.raises(ValueError, match="reason is required"):
            service.unmatch(line.id, "   ", setup["clerk"])

    def test_undoing_a_match_frees_the_line_and_is_audited(self, db, setup):
        payment, line = self._pair(db, setup)
        service = ReconciliationService(db)
        service.confirm_match(line.id, payment.id, setup["clerk"])
        service.unmatch(line.id, "Matched the wrong run", setup["clerk"])

        db.refresh(line)
        assert line.matched_payment_id is None
        assert "reconciliation_undone" in _actions(db, payment.id)


class TestTheUnreconciledReport:

    def test_a_released_run_nobody_confirmed_is_reported(self, db, setup):
        """The file was never uploaded, the bank rejected it, or someone
        dropped it. The vendor is unpaid and nobody knows."""
        payment = _released_payment(db, setup, "80000")
        payment.payment_date = (utc_now() - timedelta(days=20)).date()
        db.flush()

        result = ReconciliationService(db).unreconciled(setup["clerk"])
        assert payment.id in {p.id for p in result["instructed_not_cleared"]}

    def test_a_run_that_just_went_out_is_not_yet_chased(self, db, setup):
        """Settlement is not same-day, and crying wolf over a normal cycle
        trains the reconciler to ignore the report."""
        payment = _released_payment(db, setup, "80000")
        payment.payment_date = utc_now().date()
        db.flush()

        result = ReconciliationService(db).unreconciled(setup["clerk"])
        assert payment.id not in {p.id for p in result["instructed_not_cleared"]}

    def test_a_matched_run_leaves_the_report(self, db, setup):
        payment = _released_payment(db, setup, "80000")
        payment.payment_date = (utc_now() - timedelta(days=20)).date()
        db.flush()
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        ReconciliationService(db).confirm_match(
            statement.lines[0].id, payment.id, setup["clerk"]
        )

        result = ReconciliationService(db).unreconciled(setup["clerk"])
        assert payment.id not in {p.id for p in result["instructed_not_cleared"]}

    def test_a_debit_nothing_explains_is_reported_with_no_candidates(self, db, setup):
        """The finding this module exists for. An empty candidate list is the
        meaningful part: it separates "not reconciled yet" from "no instruction
        in this system could have produced this"."""
        statement = _import(db, setup, _statement_csv(
            "NOT-OURS", "999999.00", date(2026, 8, 5), counterparty="Unknown Party"
        ))

        result = ReconciliationService(db).unreconciled(setup["clerk"])
        found = [
            line for line in result["cleared_not_instructed"]
            if line.id == statement.lines[0].id
        ]
        assert found, "an unexplained debit must be surfaced"
        assert found[0].candidates == []

    def test_credits_are_not_reported_as_unexplained(self, db, setup):
        """Money arriving is not an AP finding."""
        statement = _import(db, setup, "date,amount\n2026-08-05,50000.00\n")

        result = ReconciliationService(db).unreconciled(setup["clerk"])
        assert statement.lines[0].id not in {
            line.id for line in result["cleared_not_instructed"]
        }

    def test_both_directions_come_back_together(self, db, setup):
        """A reconciler looking only at outstanding payments never sees the
        debit nobody instructed."""
        result = ReconciliationService(db).unreconciled(setup["clerk"])
        assert "instructed_not_cleared" in result
        assert "cleared_not_instructed" in result


class TestTenantIsolation:
    """Scoping comes from the tenant bound to the session, which the request
    dependency sets per call — so these tests bind it the same way rather than
    relying on the unbound session the other tests use."""

    def test_another_tenants_statement_is_invisible(self, db, setup, other_tenant_user):
        from app.core.database import set_tenant_context

        statement = _import(db, setup, _statement_csv(
            "PAY-00001", "80000.00", date(2026, 8, 5)
        ))

        set_tenant_context(db, other_tenant_user["tenant_id"])
        with pytest.raises(ValueError, match="not found"):
            ReconciliationService(db).get_statement(statement.id, other_tenant_user)

    def test_another_tenants_line_cannot_be_scored(self, db, setup, other_tenant_user):
        from app.core.database import set_tenant_context

        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        line_id = statement.lines[0].id

        set_tenant_context(db, other_tenant_user["tenant_id"])
        with pytest.raises(ValueError, match="not found"):
            ReconciliationService(db).suggestions_for_line(line_id, other_tenant_user)

    def test_another_tenants_payment_is_never_a_candidate(self, db, setup, other_tenant_user):
        """Scoring reads payments directly. An outsider's run matching our
        amount to the cent must not be offered as the explanation for our
        debit — that would invite a reconciler to close a real finding."""
        from app.core.database import set_tenant_context
        from app.models.payment import Payment

        statement = _import(db, setup, _statement_csv(
            "PAY-99999", "80000.00", date(2026, 8, 5)
        ))
        outsider_payment = Payment(
            id=uuid.uuid4(), tenant_id=other_tenant_user["tenant_id"],
            payment_number="PAY-99999", payment_date=date(2026, 8, 5),
            total_amount=Decimal("80000"), current_state=PaymentState.RELEASED,
            prepared_by=other_tenant_user["id"], released_by=other_tenant_user["id"],
        )
        db.add(outsider_payment)
        db.flush()

        set_tenant_context(db, setup["tenant"].id)
        _line, candidates = ReconciliationService(db).suggestions_for_line(
            statement.lines[0].id, setup["clerk"]
        )
        assert outsider_payment.id not in {c.payment.id for c in candidates}


class TestItJoinsTheGovernanceLayer:

    def test_the_payment_chain_verifies_after_reconciliation(self, db, setup):
        from app.services.audit_integrity import verify_object_chain

        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        ReconciliationService(db).confirm_match(
            statement.lines[0].id, payment.id, setup["clerk"]
        )

        assert verify_object_chain(db, "payment", payment.id)["verified"] is True

    def test_the_statement_timeline_is_readable_by_a_statement_viewer(self, db, setup):
        from app.services.audit_service import AuditService

        statement = _import(db, setup, _statement_csv(
            "PAY-1", "100.00", date(2026, 8, 5)
        ))
        timeline = AuditService(db).get_timeline(
            "bank_statement", statement.id, setup["clerk"]
        )
        assert timeline["total_events"] >= 1

    def test_reconciliation_appears_on_the_payments_timeline(self, db, setup):
        """Settlement is part of the payment's story, not a separate ledger."""
        from app.services.audit_service import AuditService

        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        ReconciliationService(db).confirm_match(
            statement.lines[0].id, payment.id, setup["clerk"]
        )

        timeline = AuditService(db).get_timeline("payment", payment.id, setup["cfo"])
        assert any(
            e.get("action") == "reconciled" for e in timeline["events"]
        )


class TestTheHttpContract:
    """The service tests cannot see how an endpoint is wired.

    The reconciliation summary in particular is assembled by hand from two
    different shapes with a dynamically attached candidate list — the sort of
    thing that types cleanly and then 500s on the first real request.
    """

    BASE = "/api/v1/bank-statements"

    def test_a_statement_is_imported_over_http(self, client, db, setup, as_user):
        as_user(setup["clerk"])
        response = client.post(self.BASE, json={
            "content": _statement_csv("PAY-00001", "80000.00", date(2026, 8, 5)),
            "filename": "august.csv",
        })

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["source_format"] == "csv"
        assert len(body["lines"]) == 1

    def test_a_file_upload_is_accepted(self, client, db, setup, as_user):
        as_user(setup["clerk"])
        content = _statement_csv("PAY-00002", "5000.00", date(2026, 8, 6))
        response = client.post(
            f"{self.BASE}/upload",
            files={"file": ("statement.txt", content.encode(), "text/plain")},
        )

        assert response.status_code == 201, response.text
        # Detected from the content, not the .txt extension.
        assert response.json()["source_format"] == "csv"

    def test_a_duplicate_import_is_a_400_not_a_500(self, client, db, setup, as_user):
        as_user(setup["clerk"])
        payload = {"content": _statement_csv("PAY-1", "100.00", date(2026, 8, 5))}
        client.post(self.BASE, json=payload)

        response = client.post(self.BASE, json=payload)
        assert response.status_code == 400
        assert "already imported" in response.json()["detail"]

    def test_the_summary_serialises(self, client, db, setup, as_user):
        """Assembled by hand from two shapes, so it is exercised end to end."""
        as_user(setup["clerk"])
        payment = _released_payment(db, setup, "80000")
        payment.payment_date = (utc_now() - timedelta(days=20)).date()
        db.flush()
        _import(db, setup, _statement_csv(
            "NOT-OURS", "999999.00", date(2026, 8, 5), counterparty="Unknown"
        ))

        response = client.get(f"{self.BASE}/reconciliation")
        assert response.status_code == 200, response.text
        body = response.json()
        assert any(
            p["payment_number"] == payment.payment_number
            for p in body["instructed_not_cleared"]
        )
        unexplained = body["cleared_not_instructed"]
        assert unexplained
        assert unexplained[0]["statement_reference"]
        assert unexplained[0]["candidates"] == []

    def test_suggestions_and_confirmation_round_trip(self, client, db, setup, as_user):
        as_user(setup["clerk"])
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))
        line_id = str(statement.lines[0].id)

        suggestions = client.get(f"{self.BASE}/lines/{line_id}/suggestions")
        assert suggestions.status_code == 200, suggestions.text
        top = suggestions.json()[0]
        assert top["payment_id"] == str(payment.id)
        assert top["reasons"]

        confirmed = client.post(
            f"{self.BASE}/lines/{line_id}/match", json={"payment_id": str(payment.id)}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["matched_payment_id"] == str(payment.id)

    def test_the_releaser_is_refused_with_403(self, client, db, setup, as_user):
        payment = _released_payment(db, setup, "80000")
        statement = _import(db, setup, _statement_csv(
            payment.payment_number, "80000.00", payment.payment_date
        ))

        # The CFO released it; as an admin they hold every permission, so a 403
        # here can only come from the SoD rule.
        as_user({**setup["cfo"], "role": UserRole.ADMIN.value})
        response = client.post(
            f"{self.BASE}/lines/{statement.lines[0].id}/match",
            json={"payment_id": str(payment.id)},
        )
        assert response.status_code == 403
        assert "Segregation of duties" in response.json()["detail"]

    def test_a_statement_id_is_not_confused_with_the_lines_route(self, client, db, setup, as_user):
        """/lines/... sits under the same prefix as /{statement_id}."""
        as_user(setup["clerk"])
        statement = _import(db, setup, _statement_csv(
            "PAY-7", "700.00", date(2026, 8, 5)
        ))

        response = client.get(f"{self.BASE}/{statement.id}")
        assert response.status_code == 200, response.text
        assert response.json()["statement_reference"] == statement.statement_reference
