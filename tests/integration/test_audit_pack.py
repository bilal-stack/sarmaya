"""Audit packs covering a period, or one control across a period.

Build Book, Audit/Compliance: "One-click audit pack export per period and per
control."

The distinction these tests are about is the one between this and the chain
pack in test_evidence_pack.py. That one answers "what happened to this
invoice" and refuses to seal an empty result, because an empty chain means the
caller asked about something that does not exist or cannot see — a lookup
failure dressed as a certified document. This one seals an empty result on
purpose, because "nothing was refused this quarter" is a computed finding over
a well-formed scope, and an auditor specifically wants it on the record.

The two cases look alike and are opposites, which is exactly why both
behaviours are pinned here rather than left to whoever reads the code next.
"""
import uuid
from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.models.evidence_pack import EvidencePack, SCOPE_CONTROL, SCOPE_PERIOD
from app.services.audit_pack import AuditPackService, CONTROLS

pytestmark = pytest.mark.integration

PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 9, 30)


def _entry(db, tenant, user, action, at, **kw):
    row = AuditLog(
        id=uuid.uuid4(), tenant_id=tenant.id, user_id=user["id"],
        user_email=user["email"], user_role=user["role"],
        object_type=kw.pop("object_type", "invoice"),
        object_id=kw.pop("object_id", uuid.uuid4()),
        action=action, timestamp=at, custom_metadata={}, **kw,
    )
    db.add(row)
    db.flush()
    return row


def _section(pack, control):
    """The substantive data lives under "content" — that is the part the
    pack_hash covers, with generated_at deliberately left outside it."""
    return next(
        c for c in pack["content"]["controls"] if c["control"] == control
    )


class TestWhatAControlPackReports:
    def test_it_counts_applications_blocks_and_overrides_apart(
        self, db, tenant, make_user
    ):
        """Three different facts about one control. A single total would say
        a control was busy without saying whether it ever refused anything."""
        admin = make_user(UserRole.ADMIN)
        at = datetime(2026, 8, 1, 12, 0)

        _entry(db, tenant, admin, "approved", at)
        _entry(db, tenant, admin, "released", at)
        _entry(db, tenant, admin, "approval_blocked", at,
               after_value={"reason": "sod_self_approval"})

        pack = AuditPackService(db).build(
            PERIOD_START, PERIOD_END, admin, control="segregation_of_duties",
        )
        section = _section(pack, "segregation_of_duties")

        assert section["applied"] == 2
        assert section["blocked"] == 1
        assert section["overridden"] == 0
        assert section["operated"] is True

    def test_a_block_is_sampled_with_who_and_why(self, db, tenant, make_user):
        """The exceptions are what an auditor reads. A count alone cannot be
        followed up."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        _entry(db, tenant, manager, "release_blocked", datetime(2026, 8, 2, 9, 0),
               object_type="payment", after_value={"reason": "self_release"})

        pack = AuditPackService(db).build(
            PERIOD_START, PERIOD_END, admin, control="segregation_of_duties",
        )
        sample = _section(pack, "segregation_of_duties")["blocked_sample"]

        assert len(sample) == 1
        assert sample[0]["who"] == manager["email"]
        assert sample[0]["reason"] == "self_release"
        assert sample[0]["object_type"] == "payment"

    def test_ordinary_applications_are_counted_but_not_shipped(
        self, db, tenant, make_user
    ):
        """A quarter of approvals would bury the five records that matter in
        ten thousand that do not."""
        admin = make_user(UserRole.ADMIN)
        for _ in range(40):
            _entry(db, tenant, admin, "approved", datetime(2026, 8, 3, 10, 0))

        section = _section(
            AuditPackService(db).build(
                PERIOD_START, PERIOD_END, admin, control="segregation_of_duties",
            ),
            "segregation_of_duties",
        )

        assert section["applied"] == 40
        assert section["blocked_sample"] == []

    def test_a_period_pack_covers_every_control(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _entry(db, tenant, admin, "approved", datetime(2026, 8, 1, 12, 0))

        pack = AuditPackService(db).build(PERIOD_START, PERIOD_END, admin)

        assert pack["scope"] == SCOPE_PERIOD
        assert pack["control"] is None
        assert {c["control"] for c in pack["content"]["controls"]} == set(CONTROLS)


class TestThePeriodBoundary:
    def test_the_last_day_is_included(self, db, tenant, make_user):
        """Comparing a timestamp against a bare date silently drops the final
        day's activity from every pack — the kind of error that surfaces only
        when somebody reconciles a total against the ledger."""
        admin = make_user(UserRole.ADMIN)
        _entry(db, tenant, admin, "approved",
               datetime(2026, 9, 30, 23, 30))

        pack = AuditPackService(db).build(PERIOD_START, PERIOD_END, admin)

        assert pack["counts"]["audit_entries"] == 1

    def test_activity_outside_the_window_is_excluded(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _entry(db, tenant, admin, "approved", datetime(2026, 6, 30, 23, 59))
        _entry(db, tenant, admin, "approved", datetime(2026, 10, 1, 0, 1))

        pack = AuditPackService(db).build(PERIOD_START, PERIOD_END, admin)

        assert pack["counts"]["audit_entries"] == 0

    def test_a_backwards_period_is_refused(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError, match="ends before it starts"):
            AuditPackService(db).build(PERIOD_END, PERIOD_START, admin)


class TestAnEmptyPackIsAFinding:
    """The deliberate difference from the chain pack, which refuses to seal an
    empty result. Both behaviours are correct and they are opposites."""

    def test_a_control_that_never_fired_is_sealed_and_says_so(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)

        pack = AuditPackService(db).generate(
            PERIOD_START, PERIOD_END, admin, control="segregation_of_duties",
        )

        assert pack["pack_id"] is not None
        assert pack["pack_hash"]
        section = _section(pack, "segregation_of_duties")
        assert section["operated"] is False
        assert section["applied"] == section["blocked"] == 0

    def test_the_chain_pack_still_refuses_an_empty_one(self, db, tenant, make_user):
        """Guards the distinction from being "simplified" away later: an empty
        chain pack certifies an absence it never checked."""
        from app.services.evidence_pack import EvidencePackService

        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError, match="nothing"):
            EvidencePackService(db).generate(uuid.uuid4(), admin)


class TestSealing:
    def test_generating_records_the_scope_it_covered(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _entry(db, tenant, admin, "approved", datetime(2026, 8, 1, 12, 0))

        pack = AuditPackService(db).generate(
            PERIOD_START, PERIOD_END, admin, control="approval_routing",
        )

        row = db.query(EvidencePack).filter(
            EvidencePack.id == pack["pack_id"]
        ).first()
        assert row.scope == SCOPE_CONTROL
        assert row.control == "approval_routing"
        assert row.period_start == PERIOD_START
        assert row.period_end == PERIOD_END
        assert row.correlation_id is None
        assert row.pack_hash == pack["pack_hash"]

    def test_the_same_period_seals_to_the_same_hash(self, db, tenant, make_user):
        """What makes the seal worth anything: re-running it later and getting
        a different hash means something underneath changed."""
        admin = make_user(UserRole.ADMIN)
        _entry(db, tenant, admin, "approved", datetime(2026, 8, 1, 12, 0))
        service = AuditPackService(db)

        first = service.build(PERIOD_START, PERIOD_END, admin)
        second = service.build(PERIOD_START, PERIOD_END, admin)

        # generated_at differs between the two, so the hash covers the content
        # rather than the moment — otherwise no two packs could ever agree.
        assert first["pack_hash"] == second["pack_hash"]

    def test_a_new_block_changes_the_hash(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = AuditPackService(db)
        before = service.build(PERIOD_START, PERIOD_END, admin)["pack_hash"]

        _entry(db, tenant, admin, "release_blocked", datetime(2026, 8, 4, 8, 0),
               after_value={"reason": "self_release"})

        assert service.build(PERIOD_START, PERIOD_END, admin)["pack_hash"] != before

    def test_a_pack_cannot_disagree_with_its_own_scope(self, db, tenant, make_user):
        """A sealed document is meant to be pointed at later. One claiming to
        cover a period while carrying a chain id is worth refusing at the
        database rather than trusting every future caller."""
        admin = make_user(UserRole.ADMIN)
        db.add(EvidencePack(
            id=uuid.uuid4(), tenant_id=tenant.id, scope=SCOPE_PERIOD,
            correlation_id=uuid.uuid4(),   # not allowed for a period pack
            period_start=PERIOD_START, period_end=PERIOD_END,
            pack_hash="0" * 64, manifest={}, generated_by=admin["id"],
        ))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


class TestTheControlRegistry:
    def test_every_control_names_actions_that_are_actually_written(self):
        """A registry entry nothing emits produces a pack reporting zero
        forever, which reads as "the control never fired" rather than "nobody
        wired this entry up" — the failure mode a hand-kept list always has."""
        import pathlib
        import re

        source = " ".join(
            p.read_text(encoding="utf-8")
            for p in pathlib.Path("app").rglob("*.py")
        )
        emitted = set(re.findall(r'action="([a-z_]+)"', source))

        missing = {}
        for key, spec in CONTROLS.items():
            named = set(spec["applied"] + spec["blocked"] + spec["overridden"])
            unknown = named - emitted
            if unknown:
                missing[key] = sorted(unknown)

        assert not missing, (
            f"Controls naming audit actions nothing writes: {missing}. "
            "Either the action was renamed, or the entry was guessed at."
        )

    def test_an_unknown_control_is_refused(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError, match="not a control"):
            AuditPackService(db).build(
                PERIOD_START, PERIOD_END, admin, control="wishful_thinking",
            )

    def test_the_registry_is_readable(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        listed = AuditPackService(db).list_controls(admin)
        assert {c["control"] for c in listed} == set(CONTROLS)
        assert all(c["what"] for c in listed), "a control with no explanation"


class TestWhoMayGenerateOne:
    def test_an_ordinary_role_cannot(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            AuditPackService(db).build(PERIOD_START, PERIOD_END, clerk)

    def test_nor_read_the_control_list(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            AuditPackService(db).list_controls(clerk)

    def test_an_auditor_can(self, db, tenant, make_user):
        auditor = make_user(UserRole.AUDITOR)
        pack = AuditPackService(db).build(PERIOD_START, PERIOD_END, auditor)
        assert pack["scope"] == SCOPE_PERIOD
