from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.bank_statement import (
    StatementImportRequest, ConfirmMatchRequest, UnmatchRequest,
    BankStatementResponse, BankStatementListResponse, BankStatementLineResponse,
    MatchCandidateResponse, UnexplainedDebitResponse, OutstandingPaymentResponse,
    ReconciliationSummary,
)
from app.services.reconciliation import ReconciliationService
from app.utils.datetime_helpers import utc_now

router = APIRouter(prefix="/bank-statements", tags=["Bank Reconciliation"])

#: Statement files are small — a month of transactions is a few hundred KB.
#: The cap is here so an oversized upload is refused before it is read into
#: memory and hashed.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def _candidate_payload(candidate) -> MatchCandidateResponse:
    payment = candidate.payment
    return MatchCandidateResponse(
        payment_id=payment.id,
        payment_number=payment.payment_number,
        payment_date=payment.payment_date,
        total_amount=payment.total_amount,
        currency=payment.currency,
        score=candidate.score,
        confidence=candidate.confidence,
        reasons=candidate.reasons,
    )


@router.get("", response_model=List[BankStatementListResponse])
def list_statements(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ReconciliationService(db).list_statements(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/reconciliation", response_model=ReconciliationSummary)
def reconciliation_summary(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What has not reconciled, in both directions.

    Released runs the bank never confirmed, and bank debits no instruction
    explains. The second list is the one that finds fraud: an unexplained debit
    cannot be produced by any mistake inside the workflow.
    """
    try:
        result = ReconciliationService(db).unreconciled(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)

    today = utc_now().date()
    return ReconciliationSummary(
        instructed_not_cleared=[
            OutstandingPaymentResponse(
                id=p.id,
                payment_number=p.payment_number,
                payment_date=p.payment_date,
                total_amount=p.total_amount,
                currency=p.currency,
                current_state=p.current_state,
                released_at=p.released_at,
                days_outstanding=(today - p.payment_date).days,
            )
            for p in result["instructed_not_cleared"]
        ],
        cleared_not_instructed=[
            UnexplainedDebitResponse(
                **BankStatementLineResponse.model_validate(line).model_dump(),
                statement_reference=(
                    line.statement.statement_reference if line.statement else None
                ),
                candidates=[_candidate_payload(c) for c in line.candidates],
            )
            for line in result["cleared_not_instructed"]
        ],
    )


@router.post("", response_model=BankStatementResponse, status_code=status.HTTP_201_CREATED)
def import_statement(
    payload: StatementImportRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Import a statement supplied as text.

    The file's hash is checked first: importing the same statement twice would
    duplicate every transaction and make the reconciliation confidently wrong.
    """
    try:
        return ReconciliationService(db).import_statement(
            payload.content,
            current_user,
            source_format=payload.source_format,
            filename=payload.filename,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post(
    "/upload", response_model=BankStatementResponse, status_code=status.HTTP_201_CREATED
)
async def upload_statement(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Import a statement file straight from the bank's download.

    The format is detected from the content rather than the filename, because
    banks name these files anything.
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Statement files are capped at {MAX_UPLOAD_BYTES // (1024 * 1024)}MB. "
                "This one is larger than any real statement should be."
            ),
        )
    try:
        # Statement files are Latin-1 or UTF-8 depending on the bank; decoding
        # with replacement keeps a stray byte in a vendor name from rejecting
        # an otherwise readable statement.
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1", errors="replace")

    try:
        return ReconciliationService(db).import_statement(
            content, current_user, filename=file.filename
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{statement_id}", response_model=BankStatementResponse)
def get_statement(
    statement_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ReconciliationService(db).get_statement(statement_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/lines/{line_id}/suggestions", response_model=List[MatchCandidateResponse])
def match_suggestions(
    line_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Ranked payments that could explain this line, with the reasoning.

    Suggestions only. Nothing is matched until a human confirms it — a wrong
    automatic match marks a payment cleared that did not clear, and hides the
    unexplained debit next to it by consuming it.
    """
    try:
        _line, candidates = ReconciliationService(db).suggestions_for_line(
            line_id, current_user
        )
        return [_candidate_payload(c) for c in candidates]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/lines/{line_id}/match", response_model=BankStatementLineResponse)
def confirm_match(
    line_id: UUID,
    payload: ConfirmMatchRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Confirm that this bank line settles this payment.

    Requires bank_statements.reconcile, and refuses the person who released the
    run: reconciliation is the check on the release, and one person holding
    both controls the instruction and the evidence for it.

    What the system suggested — score and reasons — is recorded alongside the
    confirmation, so a match a human made against a weak suggestion is
    findable later.
    """
    try:
        return ReconciliationService(db).confirm_match(
            line_id, payload.payment_id, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/lines/{line_id}/unmatch", response_model=BankStatementLineResponse)
def unmatch(
    line_id: UUID,
    payload: UnmatchRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Undo a match. A reason is required — a match that was wrong is a finding."""
    try:
        return ReconciliationService(db).unmatch(line_id, payload.reason, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
