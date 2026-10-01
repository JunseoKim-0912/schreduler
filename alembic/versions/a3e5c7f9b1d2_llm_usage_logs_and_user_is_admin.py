"""add llm_usage_logs and users.is_admin

Revision ID: a3e5c7f9b1d2
Revises: 186280b241a6
Create Date: 2026-10-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3e5c7f9b1d2'
down_revision: Union[str, Sequence[str], None] = '186280b241a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('llm_usage_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('feature', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('reasoning_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('llm_usage_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_usage_logs_created_at'), ['created_at'], unique=False)
        batch_op.create_index('ix_llm_usage_logs_user_id_created_at', ['user_id', 'created_at'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_admin', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('is_admin')

    with op.batch_alter_table('llm_usage_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_llm_usage_logs_user_id_created_at')
        batch_op.drop_index(batch_op.f('ix_llm_usage_logs_created_at'))

    op.drop_table('llm_usage_logs')
