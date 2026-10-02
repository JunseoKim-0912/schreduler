"""demo accounts

Revision ID: b7e4d2a9c1f3
Revises: c4f8a2e6d0b1
Create Date: 2026-10-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4d2a9c1f3'
down_revision: Union[str, Sequence[str], None] = 'c4f8a2e6d0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_demo', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('demo_expires_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_is_demo'), ['is_demo'], unique=False)

    with op.batch_alter_table('llm_usage_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_demo', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('llm_usage_logs', schema=None) as batch_op:
        batch_op.drop_column('is_demo')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_is_demo'))
        batch_op.drop_column('demo_expires_at')
        batch_op.drop_column('is_demo')
