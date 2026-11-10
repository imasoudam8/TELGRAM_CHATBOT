"""replace_birth_date_with_age

Revision ID: 1bd79760419b
Revises: e0d35e865fec
Create Date: 2026-11-10 07:06:54.269381+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1bd79760419b"
down_revision: str | None = "e0d35e865fec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Fail clearly if existing profiles have records in online execution,
    # preventing data corruption or invented ages without human review.
    conn = op.get_bind()
    if conn is not None:
        result = conn.execute(sa.text("SELECT COUNT(*) FROM user_profiles"))
        profile_count = result.scalar() if result is not None else 0
        if profile_count and profile_count > 0:
            raise RuntimeError(
                f"Cannot automatically convert {profile_count} existing user_profiles records: "
                "converting birth_date to integer age requires explicit data migration strategy. "
                "Failing migration to prevent silent data corruption."
            )

    # 2. Drop the existing birth_date check constraint
    op.drop_constraint(
        constraint_name="chk_user_profiles_birth_date_18_plus",
        table_name="user_profiles",
        type_="check",
    )

    # 3. Drop birth_date column
    op.drop_column("user_profiles", "birth_date")

    # 4. Add integer age column (NOT NULL)
    op.add_column(
        "user_profiles",
        sa.Column("age", sa.Integer(), nullable=False),
    )

    # 5. Add age range check constraint (18 to 99)
    op.create_check_constraint(
        constraint_name="chk_user_profiles_age_range",
        table_name="user_profiles",
        condition="age >= 18 AND age <= 99",
    )


def downgrade() -> None:
    # 1. Drop age range check constraint
    op.drop_constraint(
        constraint_name="chk_user_profiles_age_range",
        table_name="user_profiles",
        type_="check",
    )

    # 2. Drop age column
    op.drop_column("user_profiles", "age")

    # 3. Re-add birth_date column
    op.add_column(
        "user_profiles",
        sa.Column("birth_date", sa.Date(), nullable=False),
    )

    # 4. Re-create birth_date check constraint
    op.create_check_constraint(
        constraint_name="chk_user_profiles_birth_date_18_plus",
        table_name="user_profiles",
        condition="birth_date <= (age_declared_date - INTERVAL '18 years')",
    )
