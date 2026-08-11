"""Create the first tenant and its administrator on a fresh deployment.

Needed because a production database is deliberately empty. Migration 002 seeds
a demo tenant whose five accounts share a password published in this repository,
so it only runs when explicitly asked (SEED_DEMO_DATA); and `/auth/register`
cannot bootstrap on its own — it requires an existing tenant and hands out the
default clerk role, never admin.

Credentials come from the environment, never from source:

    BOOTSTRAP_TENANT_NAME    e.g. "Acme Holdings"
    BOOTSTRAP_TENANT_SLUG    e.g. "acme"           (default: derived from name)
    BOOTSTRAP_ADMIN_EMAIL    e.g. "ops@acme.com"
    BOOTSTRAP_ADMIN_PASSWORD the initial password  (change it after first login)

Run after `alembic upgrade head`:

    python -m scripts.bootstrap_tenant

Refuses to run against a database that already has users. Bootstrapping is a
one-time act, and a script that quietly adds administrators to a live system is
a backdoor rather than a convenience.
"""
import os
import re
import sys
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import ENGINE_CONNECT_ARGS
from app.core.enums import UserRole
from app.core.security import get_password_hash
from app.models.tenant import Tenant
from app.models.user import User
from app.services.config_provisioning import ConfigProvisioningService

#: Short enough not to be theatre, long enough that the first account is not the
#: weakest thing on the server. The operator chooses the value; this only
#: refuses the obviously indefensible.
MIN_PASSWORD_LENGTH = 12


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "tenant"


def _required(key: str) -> str:
    value = (os.getenv(key) or "").strip()
    if not value:
        sys.exit(f"{key} is not set. See the module docstring for what is needed.")
    return value


def main() -> None:
    tenant_name = _required("BOOTSTRAP_TENANT_NAME")
    admin_email = _required("BOOTSTRAP_ADMIN_EMAIL")
    admin_password = _required("BOOTSTRAP_ADMIN_PASSWORD")
    tenant_slug = (os.getenv("BOOTSTRAP_TENANT_SLUG") or _slugify(tenant_name)).strip()

    if len(admin_password) < MIN_PASSWORD_LENGTH:
        sys.exit(
            f"BOOTSTRAP_ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} "
            "characters. This account can approve and release payments."
        )

    # The privileged URL: this runs before any tenant context exists, so the
    # RLS policies have nothing to match against.
    engine = create_engine(settings.ADMIN_DATABASE_URL, connect_args=ENGINE_CONNECT_ARGS)
    db = sessionmaker(bind=engine)()

    try:
        if db.query(User).first() is not None:
            sys.exit(
                "This database already has users. Bootstrapping is a one-time "
                "act; create further accounts through the application, where "
                "they are subject to permission checks and the audit trail."
            )

        tenant = Tenant(
            id=uuid.uuid4(),
            name=tenant_name,
            slug=tenant_slug,
            isolation_level="rls",
            is_active=True,
        )
        db.add(tenant)
        db.flush()

        admin = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=admin_email,
            password=get_password_hash(admin_password),
            full_name=os.getenv("BOOTSTRAP_ADMIN_NAME") or "Administrator",
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.flush()

        # Without this the tenant has no workflow states and no approval matrix,
        # so every routing decision silently falls back to the hardcoded
        # defaults and nothing in the config screens reflects reality.
        current_user = {
            "id": str(admin.id),
            "tenant_id": str(tenant.id),
            "email": admin.email,
            "role": UserRole.ADMIN.value,
        }
        created = ConfigProvisioningService(db).initialize_defaults(current_user)
        db.commit()

        print(f"Tenant  : {tenant.name} ({tenant.slug})  {tenant.id}")
        print(f"Admin   : {admin.email}")
        print(f"Config  : {created}")
        print()
        print("Sign in and change this password before doing anything else.")
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
