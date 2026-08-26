"""Parsing bank statements into a common shape.

Three formats, because banks disagree and a customer cannot choose:

  * **CAMT.053** — ISO 20022 XML, the modern standard and the counterpart to
    the pain.001 the payment export is built toward.
  * **MT940** — SWIFT's fixed-format text, decades old and still what many
    banks hand out.
  * **CSV** — the fallback for the bank that offers neither, mapped by header
    name so column order does not matter.

Every parser returns the same neutral rows, so the reconciliation logic never
learns which format it came from. Sign convention is decided here — debits are
positive with `is_debit` true — because the formats disagree and resolving it
once at the boundary beats every reader guessing.

Parsers are deliberately forgiving about what they ignore and strict about what
they claim: a line without an amount is skipped rather than imported as zero,
because a zero-value line silently distorts a reconciliation.
"""
import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


@dataclass
class ParsedLine:
    line_number: int
    amount: Decimal
    is_debit: bool
    value_date: Optional[date] = None
    booking_date: Optional[date] = None
    description: Optional[str] = None
    counterparty: Optional[str] = None
    bank_reference: Optional[str] = None


@dataclass
class ParsedStatement:
    source_format: str
    statement_reference: str
    account_identifier: Optional[str] = None
    statement_date: Optional[date] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    currency: Optional[str] = None
    lines: List[ParsedLine] = field(default_factory=list)


class StatementParseError(ValueError):
    """The file could not be read as the format it claimed to be."""


def _decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _date(value: str, fmt: str) -> Optional[date]:
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except (ValueError, AttributeError):
        return None


# --- CAMT.053 ---------------------------------------------------------------

#: Matched before parsing, not after: by the time ElementTree has read a DTD it
#: has already done the expanding. Case-insensitive because the declaration is
#: `<!DOCTYPE` by the spec but nothing stops a hostile file shouting it.
_DOCTYPE = re.compile(r"<!DOCTYPE", re.IGNORECASE)

def parse_camt053(content: str) -> ParsedStatement:
    """ISO 20022 bank-to-customer statement.

    Namespaces vary by version (camt.053.001.02 through .08), so tags are
    matched on local name rather than a hardcoded namespace — otherwise a bank
    upgrading its schema silently produces an empty statement.
    """
    # A statement is a file somebody uploaded, so it is untrusted input, and
    # `xml.etree` does expand internal entities. Measured on this interpreter:
    # 481 bytes of nested entity definitions expand to 300,000 characters, a
    # 625x amplification, and the upload size cap is no help because the point
    # of the attack is that the file is small.
    #
    # It is not unbounded — modern libexpat refuses past a certain
    # amplification factor ("limit on input amplification factor breached"),
    # which stops the classic billion-laughs outright. So this guard is
    # defence in depth rather than the only thing standing in the way, and it
    # is worth having for two reasons: that limit is a property of whichever
    # libexpat the base image ships, so relying on it alone makes a security
    # property depend on a version nobody is tracking; and it turns a
    # confusing "no statement found" into a message that says what was wrong
    # with the file.
    #
    # Free to enforce: entity expansion needs a DTD, and a CAMT.053 statement
    # from a real bank has no reason to carry one.
    if _DOCTYPE.search(content):
        raise StatementParseError(
            "The file declares a DOCTYPE, which a bank statement does not "
            "need and which can be used to make a parser consume unbounded "
            "memory. Export the statement again without one."
        )

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise StatementParseError(f"Not valid XML: {exc}") from exc

    def local(element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    def find(parent, *names):
        """Walk a path of local names, returning the first match."""
        current = parent
        for name in names:
            current = next(
                (c for c in list(current) if local(c) == name), None
            ) if current is not None else None
        return current

    def text(parent, *names) -> Optional[str]:
        node = find(parent, *names)
        return node.text.strip() if node is not None and node.text else None

    # `is None`, not truthiness: an Element with no children is falsy, so an
    # `or` here would discard a real but empty <Stmt> and report the file as
    # not a statement at all.
    statement = find(root, "BkToCstmrStmt", "Stmt")
    if statement is None:
        statement = find(root, "Stmt")
    if statement is None:
        raise StatementParseError(
            "No statement found. Expected a CAMT.053 document containing "
            "BkToCstmrStmt/Stmt."
        )

    parsed = ParsedStatement(
        source_format="camt053",
        statement_reference=text(statement, "Id") or "STATEMENT",
        account_identifier=text(statement, "Acct", "Id", "IBAN")
        or text(statement, "Acct", "Id", "Othr", "Id"),
        currency=text(statement, "Acct", "Ccy"),
    )

    for balance in [c for c in list(statement) if local(c) == "Bal"]:
        code = text(balance, "Tp", "CdOrPrtry", "Cd")
        amount = _decimal(text(balance, "Amt"))
        # OPBD/PRCD open the statement; CLBD closes it.
        if code in ("OPBD", "PRCD"):
            parsed.opening_balance = amount
        elif code == "CLBD":
            parsed.closing_balance = amount
            parsed.statement_date = _date(
                (text(balance, "Dt", "Dt") or ""), "%Y-%m-%d"
            )

    for index, entry in enumerate(
        [c for c in list(statement) if local(c) == "Ntry"], start=1
    ):
        amount = _decimal(text(entry, "Amt"))
        if amount is None:
            logger.warning("CAMT entry %d has no amount; skipped", index)
            continue

        details = find(entry, "NtryDtls", "TxDtls")
        parsed.lines.append(ParsedLine(
            line_number=index,
            amount=abs(amount),
            # DBIT means money left the account.
            is_debit=(text(entry, "CdtDbtInd") or "DBIT").upper() == "DBIT",
            booking_date=_date(text(entry, "BookgDt", "Dt") or "", "%Y-%m-%d"),
            value_date=_date(text(entry, "ValDt", "Dt") or "", "%Y-%m-%d"),
            description=(
                text(details, "RmtInf", "Ustrd") if details is not None else None
            ) or text(entry, "AddtlNtryInf"),
            counterparty=(
                text(details, "RltdPties", "Cdtr", "Nm") if details is not None else None
            ),
            bank_reference=text(entry, "AcctSvcrRef")
            or (text(details, "Refs", "EndToEndId") if details is not None else None),
        ))

    return parsed


# --- MT940 ------------------------------------------------------------------

_MT940_TRANSACTION = re.compile(
    r"^:61:(\d{6})(\d{4})?([CD])([A-Z])?([\d,.]+)", re.MULTILINE
)


def parse_mt940(content: str) -> ParsedStatement:
    """SWIFT MT940. Fixed-format text, parsed by tag.

    :61: opens a transaction and :86: carries its description on the following
    lines, so the two are paired in order rather than looked up.
    """
    if ":61:" not in content and ":20:" not in content:
        raise StatementParseError(
            "No MT940 tags found. Expected :20: and :61: records."
        )

    def tag(name: str) -> Optional[str]:
        match = re.search(rf"^:{name}:(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else None

    parsed = ParsedStatement(
        source_format="mt940",
        statement_reference=tag("20") or "STATEMENT",
        account_identifier=tag("25"),
    )

    opening = tag("60F") or tag("60M")
    closing = tag("62F") or tag("62M")
    for raw, attr in ((opening, "opening_balance"), (closing, "closing_balance")):
        if not raw:
            continue
        # e.g. C260801PKR1234,56 — mark, date, currency, amount
        match = re.match(r"^([CD])(\d{6})([A-Z]{3})([\d,.]+)$", raw.replace(" ", ""))
        if match:
            value = _decimal(match.group(4).replace(",", "."))
            setattr(parsed, attr, value)
            parsed.currency = parsed.currency or match.group(3)
            if attr == "closing_balance":
                parsed.statement_date = _date(match.group(2), "%y%m%d")

    # Descriptions follow their transaction, so pair them positionally.
    descriptions = re.findall(r"^:86:(.+?)(?=^:\d{2}|\Z)", content,
                              re.MULTILINE | re.DOTALL)

    for index, match in enumerate(_MT940_TRANSACTION.finditer(content), start=1):
        value_date, _entry_date, mark, _funds, raw_amount = match.groups()
        amount = _decimal(raw_amount.replace(",", "."))
        if amount is None:
            logger.warning("MT940 transaction %d has no readable amount; skipped", index)
            continue

        description = (
            descriptions[index - 1].replace("\n", " ").strip()
            if index <= len(descriptions) else None
        )
        parsed.lines.append(ParsedLine(
            line_number=index,
            amount=abs(amount),
            is_debit=mark.upper() == "D",
            value_date=_date(value_date, "%y%m%d"),
            description=description,
            bank_reference=description.split("//")[-1].strip() if description else None,
        ))

    return parsed


# --- CSV --------------------------------------------------------------------

#: Accepted header spellings, because no two bank exports agree.
_CSV_FIELDS = {
    "amount": ("amount", "value", "transaction_amount"),
    "date": ("date", "value_date", "transaction_date", "booking_date"),
    "description": ("description", "details", "narrative", "remittance_information"),
    "counterparty": ("counterparty", "beneficiary", "creditor_name", "payee"),
    "reference": ("reference", "bank_reference", "payment_reference", "end_to_end_id"),
    "type": ("type", "debit_credit", "dc", "indicator"),
}


def parse_csv(content: str) -> ParsedStatement:
    """A plain CSV export, mapped by header name so column order is irrelevant.

    A negative amount is treated as a debit when no explicit indicator column
    is present, which is the common convention in bank CSV exports.
    """
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise StatementParseError("The CSV has no header row.")

    headers = {(h or "").strip().lower(): h for h in reader.fieldnames}

    def column(kind: str) -> Optional[str]:
        for candidate in _CSV_FIELDS[kind]:
            if candidate in headers:
                return headers[candidate]
        return None

    amount_column = column("amount")
    if not amount_column:
        raise StatementParseError(
            "No amount column found. Expected one of: "
            + ", ".join(_CSV_FIELDS["amount"])
        )

    parsed = ParsedStatement(source_format="csv", statement_reference="CSV-IMPORT")
    date_column = column("date")
    type_column = column("type")

    for index, row in enumerate(reader, start=1):
        amount = _decimal(row.get(amount_column))
        if amount is None:
            logger.warning("CSV row %d has no readable amount; skipped", index)
            continue

        if type_column and row.get(type_column):
            is_debit = row[type_column].strip().upper().startswith("D")
        else:
            is_debit = amount < 0

        value_date = None
        if date_column and row.get(date_column):
            raw = row[date_column].strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
                value_date = _date(raw, fmt)
                if value_date:
                    break

        parsed.lines.append(ParsedLine(
            line_number=index,
            amount=abs(amount),
            is_debit=is_debit,
            value_date=value_date,
            description=row.get(column("description") or "", None),
            counterparty=row.get(column("counterparty") or "", None),
            bank_reference=row.get(column("reference") or "", None),
        ))

    if not parsed.lines:
        raise StatementParseError("The CSV contained no readable transactions.")
    return parsed


PARSERS = {
    "camt053": parse_camt053,
    "mt940": parse_mt940,
    "csv": parse_csv,
}


def parse_statement(content: str, source_format: Optional[str] = None) -> ParsedStatement:
    """Parse a statement, detecting the format when not told.

    Detection is by content rather than file extension: banks name these files
    anything, and a `.txt` holding CAMT XML is common.
    """
    if source_format:
        parser = PARSERS.get(source_format.lower())
        if not parser:
            raise StatementParseError(f"Unsupported format: {source_format}")
        return parser(content)

    head = content.lstrip()[:400].lower()
    if head.startswith("<?xml") or "camt.053" in head or "bktocstmrstmt" in head:
        return parse_camt053(content)
    if ":20:" in content[:2000] or ":61:" in content[:4000]:
        return parse_mt940(content)
    return parse_csv(content)
