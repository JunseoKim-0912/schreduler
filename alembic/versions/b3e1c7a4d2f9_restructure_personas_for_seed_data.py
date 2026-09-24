"""restructure personas for seed data

Revision ID: b3e1c7a4d2f9
Revises: 9af9a5e0d514
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e1c7a4d2f9'
down_revision: Union[str, Sequence[str], None] = '9af9a5e0d514'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # PK는 batch(테이블 재생성)가 아닌 RENAME COLUMN으로 바꿔야 users/persona_conversations의 FK 참조도 함께 갱신된다.
    op.execute('ALTER TABLE personas RENAME COLUMN persona_id TO name')

    with op.batch_alter_table('personas', schema=None) as batch_op:
        batch_op.alter_column('tone_description', new_column_name='description')
        batch_op.alter_column(
            'sample_lines', new_column_name='example_lines', existing_type=sa.JSON(), nullable=True
        )
        batch_op.add_column(sa.Column('backstory', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE personas SET example_lines = '{}' WHERE example_lines IS NULL")

    with op.batch_alter_table('personas', schema=None) as batch_op:
        batch_op.drop_column('backstory')
        batch_op.alter_column(
            'example_lines', new_column_name='sample_lines', existing_type=sa.JSON(), nullable=False
        )
        batch_op.alter_column('description', new_column_name='tone_description')

    op.execute('ALTER TABLE personas RENAME COLUMN name TO persona_id')
