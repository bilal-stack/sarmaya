"""Print the demo cheat sheet: token + the invoice IDs each scene needs.

Run right before recording and keep the output on a second monitor / phone so
you can paste IDs into Swagger without breaking the take.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
import app.models  # noqa: F401
from app.models.tenant import Tenant
from app.models.invoice import Invoice

SCENES = {
    "INV-2001": "Scene 3+4  overdue -> escalates to CFO",
    "INV-2002": "Scene 5    next-action = approve (role: cfo)",
    "INV-2003": "Scene 5,6,7 next-action = verify_vendor; approve -> 403; audit timeline",
    "INV-2004": "Scene 6    manager approve -> 403 (segregation of duties)",
    "INV-2005": "spare      next-action = mark_paid",
    "INV-2007": "spare      next-action = validate",
}

db = sessionmaker(bind=create_engine(settings.ADMIN_DATABASE_URL))()
tenant = db.query(Tenant).filter_by(slug="demo").first()
if not tenant:
    raise SystemExit("No demo tenant — run _demo_bootstrap.py then _demo_seed.py")

print("\n" + "=" * 78)
print("SARMAYA DEMO CHEAT SHEET".center(78))
print("=" * 78)
print("\nLOGINS (password: demo1234)")
for email, role in [("admin@demo.com", "admin"), ("manager@demo.com", "manager"),
                    ("cfo@demo.com", "cfo"), ("clerk@demo.com", "ap_clerk")]:
    print(f"  {email:<22} {role}")

print("\nINVOICE IDs")
for number, note in SCENES.items():
    inv = db.query(Invoice).filter_by(tenant_id=tenant.id, invoice_number=number).first()
    if inv:
        print(f"  {number}  {inv.id}")
        print(f"            {note}")

print("\nPDFs TO UPLOAD  (demo_assets/)")
print("  invoice_orion_1042.pdf       Scene 1  main upload")
print("  invoice_orion_1051.pdf       Scene 2  fuzzy duplicate of 1042")
print("  invoice_meridian_9001.pdf    spare    796,800 -> CFO routing")
print("\n" + "=" * 78 + "\n")
