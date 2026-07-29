"""The read-only users directory that the delegate picker depends on.

It exposes colleagues' names, emails and roles, so it is gated by users.view
rather than being open to any authenticated caller.
"""
import pytest

from app.core.enums import UserRole
from app.models.user import User
from app.core.roles import has_permission, PERM_VIEW_USERS

pytestmark = pytest.mark.integration


class TestUsersDirectory:
    def test_permission_is_required(self):
        # AP clerks and managers must not be able to enumerate the directory.
        assert has_permission("ap_clerk", PERM_VIEW_USERS) is False
        assert has_permission("manager", PERM_VIEW_USERS) is False
        # Admins and auditors legitimately need it.
        assert has_permission("admin", PERM_VIEW_USERS) is True
        assert has_permission("auditor", PERM_VIEW_USERS) is True

    def test_active_filter_excludes_deactivated_users(self, db, tenant, make_user):
        make_user(UserRole.MANAGER)
        clerk = make_user(UserRole.AP_CLERK)
        row = db.query(User).filter(User.id == clerk["id"]).first()
        row.is_active = False
        db.flush()

        active = db.query(User).filter(User.is_active.is_(True)).all()
        assert all(u.is_active for u in active)
        assert clerk["id"] not in [str(u.id) for u in active]
