"""add event_type to events (scheduled/deadline)

Revision ID: c4d8e2f1a7b3
Revises: b3e1c7a4d2f9
Create Date: 2026-09-24 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2f1a7b3'
down_revision: Union[str, Sequence[str], None] = 'b3e1c7a4d2f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EVENT_TYPE_START_TIME_CHECK = (
    "(event_type = 'SCHEDULED' AND start_time IS NOT NULL) "
    "OR (event_type = 'DEADLINE' AND start_time IS NULL)"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'event_type',
                sa.Enum('SCHEDULED', 'DEADLINE', name='event_type', native_enum=False),
                nullable=False,
                server_default='SCHEDULED',
            )
        )
        batch_op.alter_column('start_time', existing_type=sa.DateTime(), nullable=True)

    op.execute("UPDATE events SET event_type = 'SCHEDULED' WHERE event_type IS NULL")

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_check_constraint('ck_events_event_type_start_time', EVENT_TYPE_START_TIME_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    deadline_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM events WHERE event_type = 'DEADLINE'")
    ).scalar()
    if deadline_count:
        raise RuntimeError(
            f"deadline 이벤트 {deadline_count}개는 start_time이 없어 이전 스키마로 되돌릴 수 없습니다. "
            "먼저 삭제하거나 scheduled로 바꾸세요."
        )

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_constraint('ck_events_event_type_start_time', type_='check')
        batch_op.alter_column('start_time', existing_type=sa.DateTime(), nullable=False)
        batch_op.drop_column('event_type')
