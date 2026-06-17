"""One-off: build schema + seed a demo tenant/user/config for the local demo.
Safe to re-run (idempotent). Not part of the app."""
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
import app.models  # noqa: F401 - register all models
from app.models.base import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.core.enums import UserRole
from app.core.security import get_password_hash
from app.services.config_provisioning import ConfigProvisioningService

engine = create_engine(settings.ADMIN_DATABASE_URL)
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

t = db.query(Tenant).filter_by(slug="demo").first()
if not t:
    t = Tenant(id=uuid.uuid4(), name="Demo Company", slug="demo")
    db.add(t)
    db.flush()

EMAIL, PW = "admin@demo.com", "demo1234"
u = db.query(User).filter_by(tenant_id=t.id, email=EMAIL).first()
if not u:
    u = User(
        id=uuid.uuid4(), tenant_id=t.id, email=EMAIL, full_name="Demo Admin",
        password=get_password_hash(PW), role=UserRole.ADMIN, is_active=True,
    )
    db.add(u)
    db.commit()

cu = {"id": str(u.id), "tenant_id": str(t.id), "email": u.email, "role": "admin"}
try:
    res = ConfigProvisioningService(db).initialize_defaults(cu)
    db.commit()
except Exception as e:
    res = "config init skipped: %s: %s" % (type(e).__name__, e)

print("OK | tenant=demo | user=%s | pw=%s | config=%s" % (EMAIL, PW, res))
