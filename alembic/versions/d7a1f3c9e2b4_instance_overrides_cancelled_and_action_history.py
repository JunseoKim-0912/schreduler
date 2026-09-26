"""instance time overrides, CANCELLED status, and action_history

Revision ID: d7a1f3c9e2b4
Revises: c4d8e2f1a7b3
Create Date: 2026-09-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a1f3c9e2b4'
down_revision: Union[str, Sequence[str], None] = 'c4d8e2f1a7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUS = sa.Enum('PENDING', 'DONE', 'MISSED', name='event_instance_status', native_enum=False)
# CANCELLED(9자)가 들어가면서 VARCHAR(7) → VARCHAR(9). SQLite는 길이를 무시하지만 Postgres는 넘치면 거부한다.
NEW_STATUS = sa.Enum('PENDING', 'DONE', 'MISSED', 'CANCELLED', name='event_instance_status', native_enum=False)


# 되돌리기는 삭제된 행을 원래 id로 다시 넣는다. SQLite는 AUTOINCREMENT가 없으면 지운 최대 id를 재사용해
# 되돌릴 행과 새 행의 id가 겹치므로, 복원 대상 테이블을 AUTOINCREMENT로 다시 만든다. (Postgres 시퀀스는 재사용하지 않음)
AUTOINCREMENT_TABLES = ('events', 'compliance_reports')


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == 'sqlite'


def _recreate_kwargs(autoincrement: bool) -> dict:
    if not _is_sqlite():
        return {}
    return {'recreate': 'always', 'table_kwargs': {'sqlite_autoincrement': True} if autoincrement else {}}


def upgrade() -> None:
    """Upgrade schema."""
    for table in AUTOINCREMENT_TABLES:
        if _is_sqlite():
            with op.batch_alter_table(table, schema=None, **_recreate_kwargs(True)):
                pass

    with op.batch_alter_table('event_instances', schema=None, **_recreate_kwargs(True)) as batch_op:
        batch_op.alter_column('status', existing_type=OLD_STATUS, type_=NEW_STATUS, existing_nullable=False)
        batch_op.add_column(sa.Column('start_time_override', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('end_time_override', sa.DateTime(), nullable=True))

    op.create_table(
        'action_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('action_type', sa.Enum('CREATE', 'DELETE', 'UPDATE', name='action_type', native_enum=False), nullable=False),
        sa.Column('source', sa.Enum('NL', 'UI', name='action_source', native_enum=False), nullable=False),
        sa.Column('summary_text', sa.String(length=500), nullable=False),
        sa.Column('snapshot_before', sa.JSON(), nullable=False),
        sa.Column('affected_ids', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('undone_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    cancelled = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM event_instances WHERE status = 'CANCELLED'")
    ).scalar()
    if cancelled:
        raise RuntimeError(
            f"취소된 회차 {cancelled}개가 있어 이전 스키마로 되돌릴 수 없습니다. 먼저 삭제하거나 다른 상태로 바꾸세요."
        )

    op.drop_table('action_history')
    with op.batch_alter_table('event_instances', schema=None, **_recreate_kwargs(False)) as batch_op:
        batch_op.drop_column('end_time_override')
        batch_op.drop_column('start_time_override')
        batch_op.alter_column('status', existing_type=NEW_STATUS, type_=OLD_STATUS, existing_nullable=False)

    for table in AUTOINCREMENT_TABLES:
        if _is_sqlite():
            with op.batch_alter_table(table, schema=None, **_recreate_kwargs(False)):
                pass
