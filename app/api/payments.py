from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.payment import (
    PaymentCreate, PaymentResponse, PaymentListResponse,
    RejectPaymentRequest, PayableInvoice,
)
from app.services.payment_service import PaymentService
from app.services.bank_export import BankExportService

router = APIRouter(prefix="/payments", tags=["Payments"])


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.get("", response_model=List[PaymentListResponse])
def list_payments(
    state: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PaymentService(db).list_payments(current_user, state=state)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/payable", response_model=List[PayableInvoice])
def list_payable_invoices(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Approved invoices not already claimed by an open or released run.

    Offered to whoever prepares a payment so the double-payment rule is a
    visible constraint rather than a refusal discovered at release.
    """
    try:
        return PaymentService(db).payable_invoices(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def prepare_payment(
    payload: PaymentCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Prepare a draft run.

    Each line is settled at the invoice's own total: a run cannot pay a
    different figure from the one that was approved. Bank details are copied
    onto the lines now, so the instruction stays reconstructable even if the
    vendor's details change afterwards.
    """
    try:
        return PaymentResponse.for_user(
            PaymentService(db).prepare_payment(
                payload.invoice_ids, payload, current_user
            ),
            current_user,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{payment_id}", response_model=PaymentResponse)
def get_payment(
    payment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PaymentResponse.for_user(
            PaymentService(db).get_payment(payment_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{payment_id}/submit", response_model=PaymentResponse)
def submit_for_release(
    payment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Hand the run to whoever releases payments."""
    try:
        return PaymentResponse.for_user(
            PaymentService(db).submit_for_release(payment_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{payment_id}/release", response_model=PaymentResponse)
def release_payment(
    payment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Authorise the run and mark its invoices paid.

    Requires payments.release, and refuses the person who prepared it — with
    no admin exemption, because releasing your own run is the single action
    this module exists to prevent. Every line is re-checked first: a run can
    wait days, and release cannot be undone.
    """
    try:
        return PaymentResponse.for_user(
            PaymentService(db).release_payment(payment_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{payment_id}/reject", response_model=PaymentResponse)
def reject_payment(
    payment_id: UUID,
    payload: RejectPaymentRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PaymentResponse.for_user(
            PaymentService(db).reject_payment(
                payment_id, payload.reason, current_user
            ),
            current_user,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{payment_id}/bank-file", response_class=PlainTextResponse)
def download_bank_file(
    payment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The payment instruction, as a file for the bank.

    The system does not send this anywhere: it is downloaded and uploaded by a
    treasury user to their own banking portal. Only a released run can be
    exported, and the file's hash is recorded so what went to the bank is
    evidenced rather than asserted.
    """
    try:
        filename, content, digest = BankExportService(db).export_csv(
            payment_id, current_user
        )
        return PlainTextResponse(
            content=content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-SHA256": digest,
            },
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)
