"""in-app notifications, alongside email, in one table

Revision ID: 033_in_app_notifications
Revises: 032_notification_outbox
Create Date: 2026-08-19 00:00:00.000000

Email is where a notification goes to be missed. The people who approve things
are not sitting in a shared AP mailbox all day, and until now "the approver was
told" meant exactly that — one channel, unread, with no way for the person to
see on their next login that anything had happened.

Both channels live in one table on purpose. For a product whose argument is
that governance events are recorded, "was this approver told, and how" should
be one query rather than a join across two designs. It also means a third
channel later — a Slack card, a webhook — is a new value in `channel` rather
than a new table and a second set of retry semantics.

`to_email` becomes nullable, because an in-app notification has no address; it
has a user. Existing rows are backfilled to channel 'email', which is what they
all were.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '033_in_app_notifications'
down_revision = '032_notification_outbox'
branch_labels = None
depends_on = None

TABLE = 'notification_outbox'


def upgrade():
    op.add_column(TABLE, sa.Column(
        'channel', sa.String(20), nullable=False, server_default='email'
    ))
    op.add_column(TABLE, sa.Column(
        'user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'),
        nullable=True,
    ))
    op.add_column(TABLE, sa.Column('read_at', sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column('link', sa.String(500), nullable=True))

    op.create_index(f'ix_{TABLE}_channel', TABLE, ['channel'])

    # The bell's only query: what has this person not read?
    op.create_index(
        f'ix_{TABLE}_unread', TABLE, ['user_id', 'created_at'],
        postgresql_where=sa.text('read_at IS NULL'),
    )

    # An in-app row has a user, not an address.
    op.alter_column(TABLE, 'to_email', existing_type=sa.String(255), nullable=True)


def downgrade():
    # In-app rows have no email address, so they cannot survive a column that
    # requires one. They are notifications people have already seen; dropping
    # them is the honest downgrade rather than inventing an address.
    op.execute(f"DELETE FROM {TABLE} WHERE channel = 'in_app'")
    op.alter_column(TABLE, 'to_email', existing_type=sa.String(255), nullable=False)
    op.drop_index(f'ix_{TABLE}_unread', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_channel', table_name=TABLE)
    op.drop_column(TABLE, 'link')
    op.drop_column(TABLE, 'read_at')
    op.drop_column(TABLE, 'user_id')
    op.drop_column(TABLE, 'channel')
