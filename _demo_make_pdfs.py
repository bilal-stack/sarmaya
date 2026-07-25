"""One-off: generate realistic sample invoice PDFs for demos into demo_assets/.

Three files:
  * invoice_orion_1042.pdf      — the main on-camera upload (Orion, 185,000)
  * invoice_orion_1051.pdf      — fuzzy duplicate of 1042 (same vendor, ~same
                                  amount, nearby date, different number)
  * invoice_meridian_9001.pdf   — 750,000: routes to CFO under the 250k policy

Vendor names match the seeded vendor master records exactly so uploads link to
ACTIVE vendors. Safe to re-run (overwrites).
"""
import os
from fpdf import FPDF

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_assets")
os.makedirs(OUT, exist_ok=True)


def make_invoice(filename, vendor, address, number, inv_date, due_date, items, tax_rate=0.05):
    pdf = FPDF()
    pdf.add_page()

    pdf.set_font("helvetica", "B", 22)
    pdf.cell(0, 12, "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", "B", 14)
    pdf.cell(0, 8, vendor, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    for line in address:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("helvetica", "", 11)
    pdf.cell(0, 6, f"Invoice Number: {number}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Invoice Date: {inv_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Due Date: {due_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Currency: PKR", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Bill To: Demo Company, 12 Industrial Estate, Karachi", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("helvetica", "B", 10)
    pdf.set_fill_color(235, 235, 235)
    pdf.cell(90, 8, "Description", border=1, fill=True)
    pdf.cell(25, 8, "Qty", border=1, fill=True, align="C")
    pdf.cell(35, 8, "Unit Price", border=1, fill=True, align="R")
    pdf.cell(35, 8, "Amount", border=1, fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("helvetica", "", 10)
    subtotal = 0.0
    for desc, qty, unit in items:
        amount = qty * unit
        subtotal += amount
        pdf.cell(90, 8, desc, border=1)
        pdf.cell(25, 8, f"{qty:g}", border=1, align="C")
        pdf.cell(35, 8, f"{unit:,.2f}", border=1, align="R")
        pdf.cell(35, 8, f"{amount:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    tax = round(subtotal * tax_rate, 2)
    total = subtotal + tax
    pdf.ln(4)
    pdf.set_font("helvetica", "", 11)
    pdf.cell(150, 7, "Subtotal:", align="R")
    pdf.cell(35, 7, f"{subtotal:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(150, 7, f"Tax / VAT ({tax_rate:.0%}):", align="R")
    pdf.cell(35, 7, f"{tax:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(150, 8, "Total Amount (PKR):", align="R")
    pdf.cell(35, 8, f"{total:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)
    pdf.set_font("helvetica", "I", 9)
    pdf.cell(0, 5, "Payment terms: Net 30. Please quote the invoice number on remittance.",
             new_x="LMARGIN", new_y="NEXT")

    path = os.path.join(OUT, filename)
    pdf.output(path)
    print(f"wrote {path}  (total {total:,.2f})")


# Main demo invoice — Orion, totals 185,000.25 after 5% VAT
make_invoice(
    "invoice_orion_1042.pdf",
    "Orion Supplies Ltd",
    ["Plot 47, Korangi Industrial Area", "Karachi, Pakistan", "orion.supplies@example.com"],
    "INV-1042", "2026-07-20", "2026-08-19",
    [
        ("Safety Helmet - Red, Hard Shell", 40, 1850.00),
        ("Industrial Work Gloves (pair)", 120, 385.50),
        ("Steel-Toe Boots, Size 41-46", 25, 2274.00),
    ],
)

# Fuzzy duplicate: same vendor, amount within 5%, date within 7 days, new number
make_invoice(
    "invoice_orion_1051.pdf",
    "Orion Supplies Ltd",
    ["Plot 47, Korangi Industrial Area", "Karachi, Pakistan", "orion.supplies@example.com"],
    "INV-1051", "2026-07-23", "2026-08-22",
    [
        ("Safety Helmet - Red, Hard Shell", 40, 1850.00),
        ("Industrial Work Gloves (pair)", 122, 385.50),
        ("Steel-Toe Boots, Size 41-46", 25, 2265.00),
    ],
)

# Big invoice: over the 250k threshold, routes to CFO
make_invoice(
    "invoice_meridian_9001.pdf",
    "Meridian Tech (Pvt) Ltd",
    ["Suite 902, Emerald Tower, Clifton", "Karachi, Pakistan", "billing@meridiantech.example.com"],
    "INV-9001", "2026-07-22", "2026-08-21",
    [
        ("Dell PowerEdge R660 Server", 2, 285000.00),
        ("48-Port Managed Switch", 3, 42000.00),
        ("Rack Installation & Cabling Service", 1, 62857.14),
    ],
)
