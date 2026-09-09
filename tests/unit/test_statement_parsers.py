"""Statement parsing.

Three formats reduced to one shape. These tests care about the things that
quietly corrupt a reconciliation rather than crash it: a debit read as a
credit, a comma decimal read as a thousands separator, a date parsed with the
wrong century, and a namespace change that silently yields zero transactions.
"""
from decimal import Decimal

import pytest

from app.services.statement_parsers import (
    parse_statement, parse_camt053, parse_mt940, parse_csv, StatementParseError,
)

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Id>STMT-2026-08</Id>
      <Acct><Id><IBAN>PK36SCBL0000001123456702</IBAN></Id><Ccy>PKR</Ccy></Acct>
      <Bal>
        <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="PKR">500000.00</Amt>
        <Dt><Dt>2026-08-01</Dt></Dt>
      </Bal>
      <Bal>
        <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="PKR">420000.00</Amt>
        <Dt><Dt>2026-08-31</Dt></Dt>
      </Bal>
      <Ntry>
        <Amt Ccy="PKR">80000.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-08-05</Dt></BookgDt>
        <ValDt><Dt>2026-08-05</Dt></ValDt>
        <AcctSvcrRef>BANKREF-001</AcctSvcrRef>
        <NtryDtls><TxDtls>
          <Refs><EndToEndId>PAY-00001</EndToEndId></Refs>
          <RltdPties><Cdtr><Nm>Payable Vendor</Nm></Cdtr></RltdPties>
          <RmtInf><Ustrd>PAY-00001 INV-1001</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="PKR">15000.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-08-06</Dt></BookgDt>
        <AddtlNtryInf>Customer receipt</AddtlNtryInf>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""

MT940 = """:20:STMT-940-08
:25:PK36SCBL0000001123456702
:28C:00008/001
:60F:C260801PKR500000,00
:61:2608050805D80000,00NTRFPAY-00001//BANKREF-001
:86:PAYMENT TO PAYABLE VENDOR PAY-00001
:61:2608060806C15000,00NTRFCUSTOMER//BANKREF-002
:86:CUSTOMER RECEIPT
:62F:C260831PKR435000,00
"""

CSV = """date,description,counterparty,reference,amount
2026-08-05,Payment to vendor,Payable Vendor,PAY-00001,-80000.00
2026-08-06,Customer receipt,Some Customer,RCPT-1,15000.00
"""


class TestCamt053:

    def test_it_reads_the_statement_header(self):
        parsed = parse_camt053(CAMT)
        assert parsed.statement_reference == "STMT-2026-08"
        assert parsed.account_identifier == "PK36SCBL0000001123456702"
        assert parsed.currency == "PKR"
        assert parsed.opening_balance == Decimal("500000.00")
        assert parsed.closing_balance == Decimal("420000.00")

    def test_dbit_is_a_debit_and_crdt_is_not(self):
        """The direction decides whether a line can settle a payment at all.
        Reading it backwards would offer customer receipts as candidates."""
        debit, credit = parse_camt053(CAMT).lines
        assert debit.is_debit is True
        assert credit.is_debit is False

    def test_amounts_are_positive_regardless_of_direction(self):
        for line in parse_camt053(CAMT).lines:
            assert line.amount > 0

    def test_it_carries_the_references_matching_depends_on(self):
        debit = parse_camt053(CAMT).lines[0]
        assert debit.bank_reference == "BANKREF-001"
        assert "PAY-00001" in debit.description
        assert debit.counterparty == "Payable Vendor"

    def test_a_different_namespace_version_still_parses(self):
        """Tags are matched on local name. A bank upgrading camt.053.001.02 to
        .08 must not silently produce an empty statement — which is the failure
        that looks like "no transactions this month" rather than an error."""
        upgraded = CAMT.replace("camt.053.001.02", "camt.053.001.08")
        assert len(parse_camt053(upgraded).lines) == 2

    def test_malformed_xml_is_refused_clearly(self):
        with pytest.raises(StatementParseError, match="Not valid XML"):
            parse_camt053("<Document><unclosed>")

    def test_xml_that_is_not_a_statement_is_refused(self):
        with pytest.raises(StatementParseError, match="No statement found"):
            parse_camt053("<Document><Something/></Document>")


class TestAHostileStatementCannotExhaustTheParser:
    """A statement is a file somebody uploaded, so it is untrusted input.

    `xml.etree` expands internal entities. Measured: the payload below is 481
    bytes and expands to 300,000 characters, and the upload size cap is no
    defence because the point of the attack is that the file is tiny.

    It is not unbounded — libexpat refuses past a certain amplification factor,
    which stops the classic billion-laughs on its own. These tests exist
    because that limit belongs to whichever libexpat the base image ships, and
    a security property that depends on a version nobody tracks is one worth
    holding down here as well.
    """

    #: Five levels, each ten copies of the last. Deliberately under the
    #: amplification limit — a payload the runtime rejects on its own would
    #: pass these tests whether or not the guard existed, which is the one
    #: thing they must not do.
    BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
]>
<Document>&lol5;</Document>"""

    def test_an_entity_bomb_is_refused_rather_than_expanded(self):
        with pytest.raises(StatementParseError, match="DOCTYPE"):
            parse_camt053(self.BILLION_LAUGHS)

    def test_it_is_refused_through_the_dispatcher_too(self):
        """Not only when somebody names the format directly — the upload path
        detects the format and routes it."""
        with pytest.raises(StatementParseError):
            parse_statement(self.BILLION_LAUGHS, "camt053")

    def test_a_lowercase_doctype_is_refused_as_well(self):
        """The declaration is `<!DOCTYPE` by the spec, and nothing stops a
        hostile file writing it otherwise."""
        with pytest.raises(StatementParseError, match="DOCTYPE"):
            parse_camt053(self.BILLION_LAUGHS.replace("<!DOCTYPE", "<!doctype"))

    def test_a_real_statement_still_parses(self):
        """The guard must not cost anything a bank actually sends."""
        assert parse_camt053(CAMT).lines


class TestMt940:

    def test_it_reads_the_header_and_balances(self):
        parsed = parse_mt940(MT940)
        assert parsed.statement_reference == "STMT-940-08"
        assert parsed.account_identifier == "PK36SCBL0000001123456702"
        assert parsed.currency == "PKR"
        assert parsed.opening_balance == Decimal("500000.00")

    def test_the_comma_is_a_decimal_point(self):
        """80000,00 is eighty thousand, not eight million. MT940 uses the
        comma as the decimal separator, and reading it as a thousands
        separator inflates every amount by a hundred."""
        debit = parse_mt940(MT940).lines[0]
        assert debit.amount == Decimal("80000.00")

    def test_the_d_and_c_marks_set_the_direction(self):
        debit, credit = parse_mt940(MT940).lines
        assert debit.is_debit is True
        assert credit.is_debit is False

    def test_descriptions_pair_with_their_transaction(self):
        debit, credit = parse_mt940(MT940).lines
        assert "PAY-00001" in debit.description
        assert "CUSTOMER RECEIPT" in credit.description

    def test_the_two_digit_year_is_this_century(self):
        assert parse_mt940(MT940).lines[0].value_date.year == 2026

    def test_text_without_tags_is_refused(self):
        with pytest.raises(StatementParseError, match="No MT940 tags"):
            parse_mt940("just some text")


class TestCsv:

    def test_a_negative_amount_is_a_debit(self):
        debit, credit = parse_csv(CSV).lines
        assert debit.is_debit is True
        assert debit.amount == Decimal("80000.00")
        assert credit.is_debit is False

    def test_column_order_does_not_matter(self):
        """Mapped by header name, because no two bank exports agree on order."""
        reordered = "amount,reference,date\n-500.00,PAY-9,2026-08-05\n"
        line = parse_csv(reordered).lines[0]
        assert line.amount == Decimal("500.00")
        assert line.bank_reference == "PAY-9"

    def test_an_explicit_indicator_column_wins_over_the_sign(self):
        content = "date,amount,type\n2026-08-05,500.00,DEBIT\n"
        assert parse_csv(content).lines[0].is_debit is True

    def test_a_row_with_no_amount_is_skipped_not_imported_as_zero(self):
        """A zero-value line silently distorts a reconciliation instead of
        announcing itself."""
        content = "date,amount\n2026-08-05,100.00\n2026-08-06,\n"
        assert len(parse_csv(content).lines) == 1

    def test_a_file_without_an_amount_column_is_refused(self):
        with pytest.raises(StatementParseError, match="No amount column"):
            parse_csv("date,description\n2026-08-05,something\n")


class TestFormatDetection:
    """Banks name these files anything, so the format is read from the content.
    A .txt holding CAMT XML is common."""

    def test_camt_is_detected(self):
        assert parse_statement(CAMT).source_format == "camt053"

    def test_mt940_is_detected(self):
        assert parse_statement(MT940).source_format == "mt940"

    def test_csv_is_detected(self):
        assert parse_statement(CSV).source_format == "csv"

    def test_an_explicit_format_overrides_detection(self):
        with pytest.raises(StatementParseError):
            parse_statement(CSV, source_format="camt053")

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(StatementParseError, match="Unsupported format"):
            parse_statement(CSV, source_format="ofx")
