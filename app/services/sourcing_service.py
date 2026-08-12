"""Sourcing: taking an approved need to market and choosing a supplier.

The decision made here is discretionary, happens before any money is
committed, and is effectively unreviewable by the time an invoice arrives — so
it is the step most worth recording precisely. Four controls carry that:

  * **An RFQ needs an approved requisition.** Without one, a buyer is
    approaching the market on their own authority.
  * **Quotes lock when the RFQ closes.** A quote that can be edited once the
    field is known is not a quote. Back-dating a losing bid downwards is the
    cheapest way to make a rigged award look competitive, so after closing
    nobody may add or alter one — including the buyer who captured them.
  * **Awarding is a separate permission from running the tender.** The person
    who collects the quotes must not be the one who decides which wins.
  * **Not choosing the cheapest requires a written reason.** It is legitimate
    and common — lead time, terms, quality — and it is also the single most
    examined decision in procurement, so the reason is stored on the award
    rather than left in somebody's inbox.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.requisition import PurchaseRequisition
from app.models.rfq import RFQ, RFQVendor, Quote, QuoteLine
from app.models.vendor import Vendor
from app.core.enums import RFQState, QuoteState, RequisitionState, VendorStatus, Currency
from app.core.roles import (
    has_permission, PERM_VIEW_REQUISITION, PERM_MANAGE_SOURCING, PERM_AWARD_SOURCING,
)
from app.services.workflow import transition_state
from app.services.audit import log_audit
from app.services.delegation import resolve_permission
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "rfq"


class SourcingService:
    def __init__(self, db: Session):
        self.db = db

    # --- reads ---------------------------------------------------------------

    def list_rfqs(self, current_user: dict, state: Optional[str] = None) -> List[RFQ]:
        self._require(current_user, PERM_VIEW_REQUISITION, "view RFQs")
        query = self.db.query(RFQ)
        if state:
            query = query.filter(RFQ.current_state == state)
        return query.order_by(RFQ.created_at.desc()).limit(200).all()

    def get_rfq(self, rfq_id: UUID, current_user: dict) -> RFQ:
        self._require(current_user, PERM_VIEW_REQUISITION, "view RFQs")
        return self._get(rfq_id)

    def compare_quotes(self, rfq_id: UUID, current_user: dict) -> Dict:
        """Quotes side by side, with the cheapest compliant one identified.

        The comparison names the lowest compliant quote rather than the lowest
        quote: a cheaper bid for the wrong specification is not a better offer,
        and letting it set the benchmark would make every compliant award look
        like an override that needs justifying.
        """
        self._require(current_user, PERM_VIEW_REQUISITION, "view RFQs")
        rfq = self._get(rfq_id)

        rows = []
        for quote in rfq.quotes:
            rows.append({
                "quote_id": quote.id,
                "vendor_id": quote.vendor_id,
                "vendor_name": quote.vendor_name,
                "total_amount": quote.total_amount,
                "currency": quote.currency,
                "lead_time_days": quote.lead_time_days,
                "payment_terms": quote.payment_terms,
                "is_compliant": quote.is_compliant,
                "non_compliance_reason": quote.non_compliance_reason,
                "state": quote.current_state,
                "lines": len(quote.lines),
            })

        compliant = [r for r in rows if r["is_compliant"]]
        lowest = min(compliant, key=lambda r: Decimal(r["total_amount"] or 0), default=None)

        requisition = rfq.requisition
        estimate = Decimal(requisition.estimated_amount or 0) if requisition else None

        return {
            "rfq_id": rfq.id,
            "rfq_number": rfq.rfq_number,
            "state": rfq.current_state,
            "invited_count": len(rfq.invited_vendors),
            "quoted_count": len(rows),
            # Who was asked and stayed silent. A tender answered by one of five
            # invitees is a different decision from one answered by all five.
            "no_response_vendors": [
                v.vendor_name for v in rfq.invited_vendors
                if v.vendor_id not in {r["vendor_id"] for r in rows}
            ],
            "lowest_compliant_quote_id": lowest["quote_id"] if lowest else None,
            "requisition_estimate": estimate,
            #: Whether the market came back above what the approval covered.
            "lowest_exceeds_estimate": (
                bool(lowest and estimate is not None
                     and Decimal(lowest["total_amount"] or 0) > estimate)
            ),
            "quotes": rows,
        }

    # --- writes --------------------------------------------------------------

    def create_rfq(self, data, current_user: dict) -> RFQ:
        """Open a tender against an approved requisition."""
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")

        requisition = (
            self.db.query(PurchaseRequisition)
            .filter(PurchaseRequisition.id == data.requisition_id)
            .first()
        )
        if not requisition:
            raise ValueError("Requisition not found")

        state = str(getattr(
            requisition.current_state, "value", requisition.current_state
        )).lower()
        if state != RequisitionState.APPROVED.value:
            raise ValueError(
                f"Requisition {requisition.requisition_number} is {state}, not "
                "approved. Going to market on an unapproved need commits the "
                "company's name to a purchase nobody authorised."
            )

        rfq = RFQ(
            tenant_id=current_user["tenant_id"],
            rfq_number=self._next_number(),
            title=data.title or requisition.title,
            requisition_id=requisition.id,
            issued_date=getattr(data, "issued_date", None),
            closes_at=getattr(data, "closes_at", None),
            currency=getattr(data, "currency", None) or requisition.currency or Currency.PKR,
            # Inherited, so sourcing appears in the same story as the need.
            correlation_id=requisition.correlation_id,
            current_state=RFQState.DRAFT,
            created_by=current_user["id"],
        )
        self.db.add(rfq)
        self.db.flush()

        for vendor_id in (getattr(data, "vendor_ids", None) or []):
            self._invite(rfq, vendor_id, current_user)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="created",
            workflow_type=OBJECT_TYPE,
            after_value={
                "rfq_number": rfq.rfq_number,
                "requisition_number": requisition.requisition_number,
                "invited": len(rfq.invited_vendors),
            },
        )
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    def invite_vendor(self, rfq_id: UUID, vendor_id: UUID, current_user: dict) -> RFQ:
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")
        rfq = self._get(rfq_id)
        self._refuse_if_closed(rfq, "invite further vendors")

        self._invite(rfq, vendor_id, current_user)
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    def issue_rfq(self, rfq_id: UUID, current_user: dict) -> RFQ:
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")
        rfq = self._get(rfq_id)

        if not transition_state(
            self.db, rfq, RFQState.ISSUED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        rfq.issued_date = rfq.issued_date or utc_now().date()
        self.db.add(rfq)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="issued",
            workflow_step=self._state(rfq),
            workflow_type=OBJECT_TYPE,
            after_value={
                "invited_vendors": [v.vendor_name for v in rfq.invited_vendors],
            },
        )
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    def record_quote(self, rfq_id: UUID, data, current_user: dict) -> Quote:
        """Capture what one vendor offered.

        Entered by the buyer, since vendors do not log in — so the record says
        who typed it and when, and the audit trail carries the rest.
        """
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")
        rfq = self._get(rfq_id)
        self._refuse_if_closed(rfq, "record further quotes")

        vendor = (
            self.db.query(Vendor).filter(Vendor.id == data.vendor_id).first()
        )
        if not vendor:
            raise ValueError("Vendor not found")

        invited = {v.vendor_id for v in rfq.invited_vendors}
        if vendor.id not in invited:
            raise ValueError(
                f"{vendor.legal_name} was not invited to this RFQ. A quote from "
                "an uninvited vendor did not come from this tender."
            )

        existing = next((q for q in rfq.quotes if q.vendor_id == vendor.id), None)
        if existing:
            raise ValueError(
                f"{vendor.legal_name} has already quoted on this RFQ. Withdraw "
                "the existing quote before recording a revision, so both are "
                "visible rather than one silently replacing the other."
            )

        lines = getattr(data, "lines", None) or []
        total = Decimal("0")
        quote = Quote(
            tenant_id=current_user["tenant_id"],
            rfq_id=rfq.id,
            vendor_id=vendor.id,
            vendor_name=vendor.legal_name,
            quote_reference=getattr(data, "quote_reference", None),
            quote_date=getattr(data, "quote_date", None) or utc_now().date(),
            valid_until=getattr(data, "valid_until", None),
            currency=getattr(data, "currency", None) or rfq.currency,
            total_amount=Decimal("0"),
            lead_time_days=getattr(data, "lead_time_days", None),
            payment_terms=getattr(data, "payment_terms", None),
            notes=getattr(data, "notes", None),
            is_compliant=getattr(data, "is_compliant", True),
            non_compliance_reason=getattr(data, "non_compliance_reason", None),
            current_state=QuoteState.RECEIVED,
            correlation_id=rfq.correlation_id,
            captured_by=current_user["id"],
        )
        self.db.add(quote)
        self.db.flush()

        for index, line in enumerate(lines, start=1):
            amount = Decimal(str(line.quantity)) * Decimal(str(line.unit_price))
            total += amount
            self.db.add(QuoteLine(
                tenant_id=current_user["tenant_id"],
                quote_id=quote.id,
                line_number=index,
                description=line.description,
                product_code=getattr(line, "product_code", None),
                quantity=Decimal(str(line.quantity)),
                unit_price=Decimal(str(line.unit_price)),
                amount=amount,
            ))

        # A quote with no priced lines still has a headline figure the vendor
        # gave; take it rather than recording zero.
        quote.total_amount = total if lines else Decimal(str(
            getattr(data, "total_amount", None) or 0
        ))
        if not quote.total_amount or quote.total_amount <= 0:
            raise ValueError("A quote must state an amount greater than zero")

        self.db.add(quote)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="quote_recorded",
            workflow_type=OBJECT_TYPE,
            comment=f"Quote from {vendor.legal_name}.",
            after_value={
                "quote_id": str(quote.id),
                "vendor_name": vendor.legal_name,
                "total_amount": str(quote.total_amount),
                "is_compliant": quote.is_compliant,
                "captured_by": str(current_user["id"]),
            },
        )
        self.db.commit()
        self.db.refresh(quote)
        return quote

    def close_rfq(self, rfq_id: UUID, current_user: dict) -> RFQ:
        """End quoting. After this no quote may be added or altered."""
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")
        rfq = self._get(rfq_id)

        if not transition_state(
            self.db, rfq, RFQState.CLOSED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="closed",
            workflow_step=self._state(rfq),
            workflow_type=OBJECT_TYPE,
            comment="Quoting closed; quotes are now locked.",
            after_value={
                "quotes": [
                    {"vendor": q.vendor_name, "amount": str(q.total_amount),
                     "compliant": q.is_compliant}
                    for q in rfq.quotes
                ],
            },
        )
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    def award_quote(
        self, rfq_id: UUID, quote_id: UUID, justification: Optional[str],
        current_user: dict,
    ) -> RFQ:
        """Pick the winner.

        Requires sourcing.award, which the buyer who ran the tender deliberately
        does not hold. Awarding anything other than the lowest compliant quote
        requires a written reason — recorded on the award and in the audit
        trail, because it is the decision an auditor will always ask about.
        """
        can_award, delegation = resolve_permission(
            self.db, current_user, PERM_AWARD_SOURCING
        )
        if not can_award:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "award sourcing decisions"
            )

        rfq = self._get(rfq_id)
        quote = next((q for q in rfq.quotes if q.id == quote_id), None)
        if not quote:
            raise ValueError("Quote not found on this RFQ")

        if not quote.is_compliant:
            raise ValueError(
                f"{quote.vendor_name}'s quote is marked non-compliant "
                f"({quote.non_compliance_reason or 'no reason recorded'}). "
                "Awarding it means the requirement was wrong or the assessment "
                "was — settle that first."
            )

        compliant = [q for q in rfq.quotes if q.is_compliant]
        lowest = min(compliant, key=lambda q: Decimal(q.total_amount or 0), default=None)
        is_lowest = lowest is not None and lowest.id == quote.id

        if not is_lowest and not (justification or "").strip():
            raise ValueError(
                f"{quote.vendor_name} is not the lowest compliant quote "
                f"({lowest.vendor_name} quoted {lowest.total_amount:,.2f} "
                f"against {quote.total_amount:,.2f}). Record why this one wins."
            )

        if not transition_state(
            self.db, rfq, RFQState.AWARDED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")

        rfq.awarded_quote_id = quote.id
        rfq.awarded_by = current_user["id"]
        rfq.awarded_at = utc_now()
        rfq.award_justification = (justification or "").strip() or None
        quote.current_state = QuoteState.AWARDED
        for other in rfq.quotes:
            if other.id != quote.id:
                other.current_state = QuoteState.REJECTED
        self.db.add(rfq)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="awarded",
            workflow_step=self._state(rfq),
            workflow_type=OBJECT_TYPE,
            comment=rfq.award_justification or "Lowest compliant quote.",
            after_value={
                "awarded_vendor": quote.vendor_name,
                "awarded_amount": str(quote.total_amount),
                "was_lowest_compliant": is_lowest,
                # The figure the award was measured against, kept so a later
                # reader does not have to reconstruct the field from the quotes
                # as they stand afterwards.
                "lowest_compliant_amount": (
                    str(lowest.total_amount) if lowest else None
                ),
                "quotes_considered": len(rfq.quotes),
                "awarded_by": str(current_user["id"]),
                "acted_under_delegation": bool(delegation),
            },
        )
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    def cancel_rfq(self, rfq_id: UUID, reason: str, current_user: dict) -> RFQ:
        self._require(current_user, PERM_MANAGE_SOURCING, "run sourcing")
        rfq = self._get(rfq_id)

        if not transition_state(
            self.db, rfq, RFQState.CANCELLED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        rfq.cancellation_reason = reason
        self.db.add(rfq)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=rfq.id,
            action="cancelled",
            workflow_step=self._state(rfq),
            workflow_type=OBJECT_TYPE,
            comment=reason,
        )
        self.db.commit()
        self.db.refresh(rfq)
        return rfq

    # --- helpers -------------------------------------------------------------

    def _invite(self, rfq: RFQ, vendor_id, current_user: dict) -> None:
        vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
        if not vendor:
            raise ValueError("Vendor not found")

        status = getattr(vendor.status, "value", vendor.status)
        if status != VendorStatus.ACTIVE.value:
            # Says the actual status rather than assuming "blocked": an
            # unverified vendor and a blocked one need different responses
            # from the buyer, and telling them the wrong one sends them to
            # argue with the wrong person.
            raise ValueError(
                f"{vendor.legal_name} is {status}, not active. Only an active "
                "vendor may be invited to quote — inviting one that is not is "
                "how a vendor nobody approved ends up winning work."
            )
        if any(v.vendor_id == vendor.id for v in rfq.invited_vendors):
            return

        self.db.add(RFQVendor(
            tenant_id=current_user["tenant_id"],
            rfq_id=rfq.id,
            vendor_id=vendor.id,
            vendor_name=vendor.legal_name,
        ))
        self.db.flush()

    def _refuse_if_closed(self, rfq: RFQ, action: str) -> None:
        """Nothing about the field changes after closing.

        This is the control that makes the quotes evidence rather than a
        record of what someone decided to write down afterwards.
        """
        state = self._state(rfq)
        if state in (RFQState.CLOSED.value, RFQState.AWARDED.value,
                     RFQState.CANCELLED.value):
            raise ValueError(
                f"RFQ {rfq.rfq_number} is {state}; you cannot {action}. "
                "Quotes are locked once quoting has ended."
            )

    def _get(self, rfq_id: UUID) -> RFQ:
        rfq = self.db.query(RFQ).filter(RFQ.id == rfq_id).first()
        if not rfq:
            raise ValueError("RFQ not found")
        return rfq

    @staticmethod
    def _state(rfq: RFQ) -> str:
        return str(getattr(rfq.current_state, "value", rfq.current_state)).lower()

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _next_number(self) -> str:
        existing = self.db.query(RFQ).count()
        return f"RFQ-{existing + 1:05d}"

    # --- conversion ----------------------------------------------------------

    def convert_award_to_order(self, rfq_id: UUID, current_user: dict):
        """Raise the purchase order the award decided on.

        This is where the upstream half joins the downstream one. Three things
        have to hold, and none of them is checked anywhere else:

          * The RFQ must actually be awarded. An order raised from an
            un-awarded tender is the buyer picking the winner themselves.
          * The order must not exceed the approved requisition's estimate. The
            approval was granted against that number; letting the order exceed
            it means the approval covers a figure nobody agreed to. The market
            coming back higher is normal and legitimate — it just needs the
            requisition re-approved rather than quietly absorbed.
          * The requisition is marked converted, so one approval cannot be
            spent twice.

        The order inherits the requisition's correlation id, which is what
        finally makes the whole story readable end to end: need, tender,
        quotes, award, order, receipt, invoice, payment, bank line.
        """
        from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
        from app.core.enums import PurchaseOrderState

        self._require(current_user, PERM_MANAGE_SOURCING, "convert awards to orders")

        rfq = self._get(rfq_id)
        if self._state(rfq) != RFQState.AWARDED.value:
            raise ValueError(
                f"RFQ {rfq.rfq_number} is {self._state(rfq)}, not awarded. "
                "There is no decision to raise an order against."
            )

        quote = next((q for q in rfq.quotes if q.id == rfq.awarded_quote_id), None)
        if not quote:
            raise ValueError("The awarded quote is missing from this RFQ")

        requisition = rfq.requisition
        if requisition is None:
            raise ValueError("Requisition not found for this RFQ")

        req_state = str(getattr(
            requisition.current_state, "value", requisition.current_state
        )).lower()
        if req_state == RequisitionState.CONVERTED.value:
            raise ValueError(
                f"Requisition {requisition.requisition_number} has already been "
                "converted to an order. One approval cannot cover two orders."
            )
        if req_state != RequisitionState.APPROVED.value:
            raise ValueError(
                f"Requisition {requisition.requisition_number} is {req_state}, "
                "not approved."
            )

        estimate = Decimal(requisition.estimated_amount or 0)
        awarded = Decimal(quote.total_amount or 0)
        if estimate and awarded > estimate:
            raise ValueError(
                f"The awarded quote is {awarded:,.2f} against an approved "
                f"estimate of {estimate:,.2f}. Send the requisition back for "
                "re-approval at the real figure rather than raising an order "
                "the approval does not cover."
            )

        order = PurchaseOrder(
            tenant_id=current_user["tenant_id"],
            po_number=self._next_po_number(),
            vendor_id=quote.vendor_id,
            vendor_name=quote.vendor_name,
            order_date=utc_now().date(),
            currency=quote.currency,
            description=f"Awarded from {rfq.rfq_number} ({rfq.title}).",
            current_state=PurchaseOrderState.DRAFT,
            # Inherited, not minted: the order belongs to the story that began
            # with the requisition.
            correlation_id=rfq.correlation_id,
            created_by=current_user["id"],
            tax_amount=Decimal("0"),
            subtotal_amount=awarded,
            total_amount=awarded,
        )
        self.db.add(order)
        self.db.flush()

        for line in quote.lines:
            self.db.add(PurchaseOrderLine(
                tenant_id=current_user["tenant_id"],
                purchase_order_id=order.id,
                line_number=line.line_number,
                description=line.description,
                product_code=line.product_code,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            ))

        # A quote captured as a headline figure has no lines to copy; the order
        # still needs one, or po_has_lines refuses it at approval.
        if not quote.lines:
            self.db.add(PurchaseOrderLine(
                tenant_id=current_user["tenant_id"],
                purchase_order_id=order.id,
                line_number=1,
                description=rfq.title,
                quantity=Decimal("1"),
                unit_price=awarded,
                amount=awarded,
            ))

        transition_state(
            self.db, requisition, RequisitionState.CONVERTED.value, current_user["id"]
        )
        self.db.add(requisition)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="purchase_order",
            object_id=order.id,
            action="created_from_award",
            workflow_type="purchase_order",
            comment=(
                f"Raised from {rfq.rfq_number}, awarded to {quote.vendor_name}."
            ),
            after_value={
                "po_number": order.po_number,
                "rfq_number": rfq.rfq_number,
                "requisition_number": requisition.requisition_number,
                "vendor_name": quote.vendor_name,
                "total_amount": str(awarded),
                "approved_estimate": str(estimate),
            },
        )
        # Written onto the requisition's own timeline too, so the need shows
        # what became of it without anyone having to follow the chain forward.
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="requisition",
            object_id=requisition.id,
            action="converted",
            workflow_step=RequisitionState.CONVERTED.value,
            workflow_type="requisition",
            comment=f"Converted to {order.po_number} via {rfq.rfq_number}.",
            after_value={"po_number": order.po_number, "amount": str(awarded)},
        )
        self.db.commit()
        self.db.refresh(order)
        return order

    def _next_po_number(self) -> str:
        from app.models.purchase_order import PurchaseOrder

        existing = self.db.query(PurchaseOrder).count()
        return f"PO-{existing + 1:05d}"
