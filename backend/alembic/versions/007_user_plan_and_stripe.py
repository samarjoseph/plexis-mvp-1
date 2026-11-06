"""Add user plan and stripe columns

Revision ID: 007
Revises: 006
Create Date: 2026-11-06
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR(50) DEFAULT 'free' NOT NULL;")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255);")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255);")


def downgrade() -> None:
    op.drop_column('users', 'stripe_subscription_id')
    op.drop_column('users', 'stripe_customer_id')
    op.drop_column('users', 'plan')
