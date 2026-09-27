"""persona fallback_lines, persona_conversations date and created_at

Revision ID: f9c3a1d2e4b5
Revises: e8b2c4d6f1a3
Create Date: 2026-09-27 10:00:00.000000

"""
import json
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9c3a1d2e4b5'
down_revision: Union[str, Sequence[str], None] = 'e8b2c4d6f1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('personas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fallback_lines', sa.JSON(), nullable=True))
    with op.batch_alter_table('persona_conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('conversation_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))

    # 기존 대화는 첫 메시지 시각으로 날짜를 채운다 ("오늘 대화"를 찾을 때 쓰인다).
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, messages FROM persona_conversations")).all()
    for conversation_id, messages in rows:
        items = json.loads(messages) if isinstance(messages, str) else (messages or [])
        if not items or not items[0].get("created_at"):
            continue
        started = datetime.fromisoformat(items[0]["created_at"])
        bind.execute(
            sa.text("UPDATE persona_conversations SET conversation_date = :day, created_at = :at WHERE id = :id"),
            {"day": started.date(), "at": started, "id": conversation_id},
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('persona_conversations', schema=None) as batch_op:
        batch_op.drop_column('created_at')
        batch_op.drop_column('conversation_date')
    with op.batch_alter_table('personas', schema=None) as batch_op:
        batch_op.drop_column('fallback_lines')
