"""Records are withdrawn, not destroyed.

Build Book non-negotiable: *immutable audit — guardrails to prevent hard
deletes*. Deleting a vendor, a draft invoice or an approval policy removed the
row while the audit entry describing the deletion stayed behind, pointing at an
id nothing resolved. The trail said something happened to something that, as
far as the database was concerned, never existed.

Both halves are tested here: withdrawn rows must disappear from ordinary
queries (or the deletion does not work), and must still be reachable when the
audit trail needs them (or keeping them achieved nothing).
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.database import HardDeleteRefused, include_deleted
from app.core.enums import InvoiceState, UserRole, VendorStatus
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.vendor import VendorCreate
from app.services.invoice_service import InvoiceService
from app.services.vendor_service import VendorService

pytestmark = pytest.mark.integration

REASON = "Created in error during the data migration."


def _vendor(db, tenant_id, name=None):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id,
        legal_name=name or f"V-{uuid.uuid4().hex[:6]}", status=VendorStatus.ACTIVE,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _draft_invoice(db, tenant_id, created_by, vendor=None):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name if vendor else "Acme",
        vendor_id=vendor.id if vendor else None,
        invoice_date=date(2026, 8, 1), total_amount=Decimal("1000"),
        current_state=InvoiceState.DRAFT, created_by=created_by,
    )
    db.add(invoice)
    db.flush()
    return invoice


class TestAWithdrawnRecordIsGone:
    def test_it_disappears_from_reads(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(
            VendorCreate(legal_name="Withdrawn Ltd"), admin
        )

        service.delete_vendor(vendor.id, admin, REASON)

        with pytest.raises(ValueError, match="not found"):
            service.get_vendor(vendor.id, admin)

    def test_it_disappears_from_lists(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Listed Ltd"), admin)
        service.delete_vendor(vendor.id, admin, REASON)

        names = {v.legal_name for v in service.list_vendors(admin)[0]}
        assert "Listed Ltd" not in names

    def test_it_disappears_from_a_plain_orm_query_too(self, db, tenant, make_user):
        """The filter is global, not something each repository remembers. A
        query written tomorrow in a module that knows nothing about deletion
        still excludes withdrawn rows."""
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Direct Ltd"), admin)
        service.delete_vendor(vendor.id, admin, REASON)

        assert db.query(Vendor).filter(Vendor.id == vendor.id).first() is None

    def test_a_withdrawn_invoice_stops_appearing(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        invoice = _draft_invoice(db, tenant.id, admin["id"])
        db.commit()

        InvoiceService(db).delete_invoice(invoice.id, admin, REASON)

        assert db.query(Invoice).filter(Invoice.id == invoice.id).first() is None


class TestButTheRowSurvives:
    def test_the_audit_entry_still_resolves_to_something(
        self, db, tenant, make_user
    ):
        """The whole point. The deletion event names an object; that object has
        to still be there for the trail to mean anything."""
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Traceable Ltd"), admin)
        service.delete_vendor(vendor.id, admin, REASON)

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == vendor.id, AuditLog.action == "deleted")
            .first()
        )
        assert entry is not None

        include_deleted(db)
        try:
            row = db.query(Vendor).filter(Vendor.id == vendor.id).first()
        finally:
            include_deleted(db, False)

        assert row is not None, "the audit entry points at a row that is gone"
        assert row.legal_name == "Traceable Ltd"

    def test_it_records_who_when_and_why(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Explained Ltd"), admin)
        service.delete_vendor(vendor.id, admin, REASON)

        include_deleted(db)
        try:
            row = db.query(Vendor).filter(Vendor.id == vendor.id).first()
        finally:
            include_deleted(db, False)

        assert row.deleted_at is not None
        assert str(row.deleted_by) == str(admin["id"])
        assert row.deletion_reason == REASON
        assert row.is_deleted

    def test_the_reason_is_on_the_audit_entry_as_well(self, db, tenant, make_user):
        """So a reader following the trail never has to fetch the withdrawn
        row to learn why it went."""
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Commented Ltd"), admin)
        service.delete_vendor(vendor.id, admin, REASON)

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == vendor.id, AuditLog.action == "deleted")
            .first()
        )
        assert entry.comment == REASON
        assert entry.before_value["legal_name"] == "Commented Ltd"


class TestAReasonIsRequired:
    """A deletion is the one event nobody can reconstruct from what is left:
    every other action leaves the changed record behind to be read."""

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_an_empty_reason_is_refused(self, db, tenant, make_user, bad):
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Unexplained Ltd"), admin)

        with pytest.raises(ValueError, match="reason"):
            service.delete_vendor(vendor.id, admin, bad)

    def test_and_nothing_is_withdrawn_when_it_is(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = VendorService(db)
        vendor = service.create_vendor(VendorCreate(legal_name="Intact Ltd"), admin)

        with pytest.raises(ValueError):
            service.delete_vendor(vendor.id, admin, "")

        assert service.get_vendor(vendor.id, admin) is not None

    def test_the_api_refuses_a_reason_too_short_to_explain_anything(
        self, db, tenant, client, as_user, make_user
    ):
        """"x" satisfies "required" and explains nothing."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        as_user(admin)

        response = client.request(
            "DELETE", f"/api/v1/vendors/{vendor.id}", json={"reason": "x"}
        )
        assert response.status_code == 422


class TestTheGuardrail:
    """Build Book: guardrails to *prevent* hard deletes — not a convention that
    the services happen to follow."""

    def test_a_stray_session_delete_is_refused(self, db, tenant, make_user):
        make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)

        db.delete(vendor)
        with pytest.raises(HardDeleteRefused, match="cannot be hard-deleted"):
            db.flush()
        db.rollback()

    def test_the_refusal_names_the_alternative(self, db, tenant, make_user):
        """Whoever hits this is trying to do something reasonable and should
        not have to go reading source to find out how."""
        make_user(UserRole.ADMIN)
        invoice = _draft_invoice(db, tenant.id, make_user(UserRole.ADMIN)["id"])

        db.delete(invoice)
        with pytest.raises(HardDeleteRefused, match="[Ww]ithdraw"):
            db.flush()
        db.rollback()

    def test_it_covers_every_model_carrying_the_mixin(self, db):
        """Not a list maintained by hand — the guard reads the mixin, so a new
        model is protected by declaring it."""
        from app.core.database import _soft_delete_mappers

        protected = {m.__name__ for m in _soft_delete_mappers()}
        assert {"Vendor", "Invoice", "Policy"} <= protected


class TestWhatIsStillDestroyed:
    def test_an_orphaned_upload_is_genuinely_removed(self, db):
        """The exception, and deliberately so. `delete_file` runs only when OCR
        or invoice creation failed after the file was saved — a file that never
        became evidence of anything. Soft-deleting there would leave a row and
        a file on disk forever, with nothing that would ever clean either up.
        """
        import inspect

        from app.models.file import File as FileModel
        from app.models.base import SoftDeleteMixin
        from app.services.invoice_service import InvoiceService

        assert not issubclass(FileModel, SoftDeleteMixin)
        # And it is reachable from exactly one place: the upload rollback.
        source = inspect.getsource(InvoiceService)
        assert source.count("self.file_service.delete_file(") == 1
