"""important_date_ranges AUTOINCREMENT (undo re-inserts deleted ranges with their original ids)

Revision ID: e8b2c4d6f1a3
Revises: d7a1f3c9e2b4
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e8b2c4d6f1a3'
down_revision: Union[str, Sequence[str], None] = 'd7a1f3c9e2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite는 AUTOINCREMENT가 없으면 지운 최대 id를 재사용해, 되돌리기로 다시 넣을 기간과 새 기간의 id가 겹칠 수 있다.
# Postgres 시퀀스는 재사용하지 않으므로 SQLite에서만 테이블을 다시 만든다.
def _recreate(autoincrement: bool) -> None:
    if op.get_bind().dialect.name != 'sqlite':
        return
    table_kwargs = {'sqlite_autoincrement': True} if autoincrement else {}
    with op.batch_alter_table('important_date_ranges', schema=None, recreate='always', table_kwargs=table_kwargs):
        pass


def upgrade() -> None:
    """Upgrade schema."""
    _recreate(True)


def downgrade() -> None:
    """Downgrade schema."""
    _recreate(False)
