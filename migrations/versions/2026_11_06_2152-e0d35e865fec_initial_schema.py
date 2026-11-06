"""initial_schema

Revision ID: e0d35e865fec
Revises:
Create Date: 2026-11-06 21:52:51.362997+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e0d35e865fec"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. users
    # -------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False
        ),
        sa.Column(
            "age_18_plus_declared", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("age_declared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("age_declared_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
        sa.UniqueConstraint("public_id", name="uq_users_public_id"),
        sa.UniqueConstraint(
            "id",
            "age_18_plus_declared",
            "age_declared_date",
            name="uq_users_id_age_declaration",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'BANNED', 'DELETED')",
            name="chk_users_status_valid",
        ),
        sa.CheckConstraint(
            "(age_18_plus_declared = FALSE AND age_declared_at IS NULL AND age_declared_date IS NULL) OR "
            "(age_18_plus_declared = TRUE AND age_declared_at IS NOT NULL AND age_declared_date IS NOT NULL)",
            name="chk_users_age_declaration_consistency",
        ),
        sa.CheckConstraint(
            "age_declared_at IS NULL OR age_declared_at >= created_at - INTERVAL '5 seconds'",
            name="chk_users_declaration_timestamp_ordering",
        ),
    )
    op.create_index("idx_users_public_id", "users", ["public_id"])
    op.create_index(
        "idx_users_status",
        "users",
        ["status"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # -------------------------------------------------------------------------
    # 2. user_profiles
    # -------------------------------------------------------------------------
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "age_18_plus_declared", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("age_declared_date", sa.Date(), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("display_name", sa.String(length=50), nullable=False),
        sa.Column("gender", sa.String(length=20), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("bio", sa.String(length=500), nullable=True),
        sa.Column(
            "completion_status",
            sa.String(length=20),
            server_default=sa.text("'INCOMPLETE'"),
            nullable=False,
        ),
        sa.Column(
            "visibility", sa.String(length=20), server_default=sa.text("'PUBLIC'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_profiles"),
        sa.ForeignKeyConstraint(
            ["user_id", "age_18_plus_declared", "age_declared_date"],
            ["users.id", "users.age_18_plus_declared", "users.age_declared_date"],
            name="fk_user_profiles_users_declaration",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "age_18_plus_declared = TRUE",
            name="chk_user_profiles_age_declared_true",
        ),
        sa.CheckConstraint(
            "birth_date <= (age_declared_date - INTERVAL '18 years')",
            name="chk_user_profiles_birth_date_18_plus",
        ),
        sa.CheckConstraint(
            "gender IN ('MALE', 'FEMALE', 'NON_BINARY', 'OTHER')",
            name="chk_user_profiles_gender_valid",
        ),
        sa.CheckConstraint(
            "completion_status IN ('INCOMPLETE', 'COMPLETED')",
            name="chk_user_profiles_completion_status_valid",
        ),
        sa.CheckConstraint(
            "visibility IN ('PUBLIC', 'ANONYMOUS', 'HIDDEN')",
            name="chk_user_profiles_visibility_valid",
        ),
    )
    op.create_index(
        "idx_profiles_matching",
        "user_profiles",
        ["gender", "city", "completion_status"],
        postgresql_where=sa.text("visibility = 'PUBLIC'"),
    )

    # -------------------------------------------------------------------------
    # 3. user_preferences
    # -------------------------------------------------------------------------
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "preferred_genders",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY[]::TEXT[]"),
            nullable=False,
        ),
        sa.Column("min_age", sa.SmallInteger(), server_default=sa.text("18"), nullable=False),
        sa.Column("max_age", sa.SmallInteger(), server_default=sa.text("99"), nullable=False),
        sa.Column(
            "preferred_cities",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("ARRAY[]::TEXT[]"),
            nullable=False,
        ),
        sa.Column(
            "required_filters",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "relaxable_filters",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_preferences"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_preferences_user_id", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "min_age >= 18 AND max_age <= 99 AND min_age <= max_age",
            name="chk_user_preferences_age_range",
        ),
    )

    # -------------------------------------------------------------------------
    # 4. direct_requests
    # -------------------------------------------------------------------------
    op.create_table(
        "direct_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_direct_requests"),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["users.id"],
            name="fk_direct_requests_requester_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["users.id"], name="fk_direct_requests_target_id", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "id",
            "status",
            "requester_id",
            "target_id",
            name="uq_direct_requests_composite_state",
        ),
        sa.CheckConstraint("requester_id <> target_id", name="chk_direct_requests_different_users"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="chk_direct_requests_status_valid",
        ),
    )
    op.create_index(
        "uq_direct_requests_pending_pair",
        "direct_requests",
        ["requester_id", "target_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "idx_direct_requests_inbox",
        "direct_requests",
        ["target_id", "status"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "idx_direct_requests_expiry",
        "direct_requests",
        ["status", "expires_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # -------------------------------------------------------------------------
    # 5. user_connections
    # -------------------------------------------------------------------------
    op.create_table(
        "user_connections",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("connected_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False
        ),
        sa.Column(
            "established_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_connections"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_connections_user_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["connected_user_id"],
            ["users.id"],
            name="fk_user_connections_connected_user_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("user_id", "connected_user_id", name="uq_user_connections_pair"),
        sa.CheckConstraint(
            "user_id <> connected_user_id", name="chk_user_connections_different_users"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REMOVED')", name="chk_user_connections_status_valid"
        ),
    )
    op.create_index(
        "idx_user_connections_list",
        "user_connections",
        ["user_id", "status"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # -------------------------------------------------------------------------
    # 6. user_blocks
    # -------------------------------------------------------------------------
    op.create_table(
        "user_blocks",
        sa.Column("blocker_id", sa.BigInteger(), nullable=False),
        sa.Column("blocked_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("blocker_id", "blocked_id", name="pk_user_blocks"),
        sa.ForeignKeyConstraint(
            ["blocker_id"], ["users.id"], name="fk_user_blocks_blocker_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["blocked_id"], ["users.id"], name="fk_user_blocks_blocked_id", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("blocker_id <> blocked_id", name="chk_user_blocks_different_users"),
    )
    op.create_index("idx_user_blocks_reverse", "user_blocks", ["blocked_id", "blocker_id"])

    # -------------------------------------------------------------------------
    # 7. chat_sessions
    # -------------------------------------------------------------------------
    op.create_table(
        "chat_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("chat_type", sa.String(length=20), nullable=False),
        sa.Column("direct_request_id", sa.BigInteger(), nullable=True),
        sa.Column("direct_request_status", sa.String(length=20), nullable=True),
        sa.Column("participant_one_id", sa.BigInteger(), nullable=False),
        sa.Column("participant_two_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'INITIATED'"), nullable=False
        ),
        sa.Column(
            "participant_one_sent_msg",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "participant_two_sent_msg",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("is_charged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
        sa.ForeignKeyConstraint(
            ["participant_one_id"],
            ["users.id"],
            name="fk_chat_sessions_participant_one_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["participant_two_id"],
            ["users.id"],
            name="fk_chat_sessions_participant_two_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "direct_request_id",
                "direct_request_status",
                "participant_one_id",
                "participant_two_id",
            ],
            [
                "direct_requests.id",
                "direct_requests.status",
                "direct_requests.requester_id",
                "direct_requests.target_id",
            ],
            name="fk_chat_sessions_direct_request_accepted",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "participant_one_id <> participant_two_id",
            name="chk_chat_sessions_different_participants",
        ),
        sa.CheckConstraint(
            "chat_type IN ('RANDOM', 'DIRECT')",
            name="chk_chat_sessions_type_valid",
        ),
        sa.CheckConstraint(
            "(chat_type = 'RANDOM' AND direct_request_id IS NULL AND direct_request_status IS NULL) OR "
            "(chat_type = 'DIRECT' AND direct_request_id IS NOT NULL AND direct_request_status = 'ACCEPTED')",
            name="chk_chat_sessions_direct_linkage_rules",
        ),
        sa.CheckConstraint(
            "status IN ('INITIATED', 'ACTIVE', 'ENDED')",
            name="chk_chat_sessions_status_valid",
        ),
    )
    op.create_index(
        "idx_chat_sessions_history",
        "chat_sessions",
        ["participant_one_id", "participant_two_id", "status"],
    )
    op.create_index(
        "idx_chat_sessions_direct_request",
        "chat_sessions",
        ["direct_request_id"],
        postgresql_where=sa.text("direct_request_id IS NOT NULL"),
    )

    # -------------------------------------------------------------------------
    # 8. active_chat_participants
    # -------------------------------------------------------------------------
    op.create_table(
        "active_chat_participants",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_active_chat_participants"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_active_chat_participants_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["chat_sessions.id"],
            name="fk_active_chat_participants_chat_id",
            ondelete="CASCADE",
        ),
    )

    # -------------------------------------------------------------------------
    # 9. chat_messages
    # -------------------------------------------------------------------------
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("chat_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("message_type", sa.String(length=20), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=255), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        sa.UniqueConstraint("id", "chat_id", name="uq_chat_messages_id_chat"),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chat_sessions.id"], name="fk_chat_messages_chat_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"], ["users.id"], name="fk_chat_messages_sender_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name="fk_chat_messages_recipient_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "message_type IN ('TEXT', 'PHOTO', 'VOICE', 'VIDEO', 'STICKER', 'ANIMATION')",
            name="chk_chat_messages_type_valid",
        ),
        sa.CheckConstraint(
            "message_type <> 'VIDEO' OR file_size_bytes < 10485760",
            name="chk_chat_messages_video_size_ceiling",
        ),
        sa.CheckConstraint(
            "expires_at <= created_at + INTERVAL '30 days'",
            name="chk_chat_messages_retention_ceiling",
        ),
    )
    op.create_index("idx_chat_messages_chat_time", "chat_messages", ["chat_id", "created_at"])
    op.create_index("idx_chat_messages_cleanup", "chat_messages", ["expires_at"])

    # -------------------------------------------------------------------------
    # 10. reports
    # -------------------------------------------------------------------------
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("reporter_id", sa.BigInteger(), nullable=False),
        sa.Column("reported_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reported_message_id", sa.BigInteger(), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("moderator_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
        sa.ForeignKeyConstraint(
            ["reporter_id"], ["users.id"], name="fk_reports_reporter_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reported_user_id"],
            ["users.id"],
            name="fk_reports_reported_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chat_session_id"],
            ["chat_sessions.id"],
            name="fk_reports_chat_session_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reported_message_id"],
            ["chat_messages.id"],
            name="fk_reports_reported_message_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["moderator_id"], ["users.id"], name="fk_reports_moderator_id", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("reporter_id <> reported_user_id", name="chk_reports_different_users"),
        sa.CheckConstraint(
            "category IN ('HARASSMENT', 'SPAM', 'INAPPROPRIATE_CONTENT', 'SCAM_FRAUD', 'UNDERAGE_SUSPICION', 'OTHER')",
            name="chk_reports_category_valid",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'REVIEWED', 'ACTIONED', 'DISMISSED')",
            name="chk_reports_status_valid",
        ),
    )
    op.create_index(
        "idx_reports_queue",
        "reports",
        ["status", "created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # -------------------------------------------------------------------------
    # 11. admin_users
    # -------------------------------------------------------------------------
    op.create_table(
        "admin_users",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("assigned_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_admin_users"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_admin_users_user_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"], ["users.id"], name="fk_admin_users_assigned_by", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('SUPER_ADMIN', 'SUPPORT', 'MODERATOR')",
            name="chk_admin_users_role_valid",
        ),
    )

    # -------------------------------------------------------------------------
    # 12. message_evidence_holds
    # -------------------------------------------------------------------------
    op.create_table(
        "message_evidence_holds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_content_hash", sa.String(length=64), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=True),
        sa.Column("legal_reference_id", sa.String(length=100), nullable=True),
        sa.Column("hold_type", sa.String(length=30), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_system_actor", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("released_by_admin_id", sa.BigInteger(), nullable=True),
        sa.Column("released_by_system_actor", sa.String(length=50), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_message_evidence_holds"),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name="fk_evidence_holds_message_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["chat_session_id"],
            ["chat_sessions.id"],
            name="fk_evidence_holds_chat_session_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["reports.id"], name="fk_evidence_holds_report_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admin_users.user_id"],
            name="fk_evidence_holds_created_admin_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["released_by_admin_id"],
            ["admin_users.user_id"],
            name="fk_evidence_holds_released_admin_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "hold_type IN ('USER_REPORT', 'LEGAL_HOLD', 'SAFETY_AUDIT')",
            name="chk_evidence_holds_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED')",
            name="chk_evidence_holds_status_valid",
        ),
        sa.CheckConstraint(
            "(created_by_admin_id IS NOT NULL AND created_by_system_actor IS NULL) OR "
            "(created_by_admin_id IS NULL AND created_by_system_actor IS NOT NULL)",
            name="chk_evidence_holds_creator_attribution",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND released_at IS NULL AND released_by_admin_id IS NULL AND released_by_system_actor IS NULL AND release_notes IS NULL) OR "
            "(status = 'RELEASED' AND released_at IS NOT NULL AND release_notes IS NOT NULL AND "
            "((released_by_admin_id IS NOT NULL AND released_by_system_actor IS NULL) OR (released_by_admin_id IS NULL AND released_by_system_actor IS NOT NULL)))",
            name="chk_evidence_holds_release_consistency",
        ),
        sa.CheckConstraint(
            "(hold_type = 'LEGAL_HOLD' AND legal_reference_id IS NOT NULL AND report_id IS NULL) OR "
            "(hold_type = 'USER_REPORT' AND report_id IS NOT NULL AND legal_reference_id IS NULL) OR "
            "(hold_type = 'SAFETY_AUDIT')",
            name="chk_evidence_holds_type_attributes",
        ),
    )
    op.create_index(
        "uq_evidence_holds_message_report",
        "message_evidence_holds",
        ["message_id", "report_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL AND report_id IS NOT NULL"),
    )
    op.create_index(
        "uq_evidence_holds_message_legal",
        "message_evidence_holds",
        ["message_id", "legal_reference_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL AND legal_reference_id IS NOT NULL"),
    )
    op.create_index(
        "idx_evidence_holds_active",
        "message_evidence_holds",
        ["message_id"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "idx_evidence_holds_report",
        "message_evidence_holds",
        ["report_id"],
        postgresql_where=sa.text("report_id IS NOT NULL"),
    )
    op.create_index(
        "idx_evidence_holds_legal",
        "message_evidence_holds",
        ["legal_reference_id"],
        postgresql_where=sa.text("legal_reference_id IS NOT NULL"),
    )

    # -------------------------------------------------------------------------
    # 13. moderation_actions
    # -------------------------------------------------------------------------
    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_actions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_moderation_actions_user_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["moderator_id"],
            ["users.id"],
            name="fk_moderation_actions_moderator_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name="fk_moderation_actions_report_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "action_type IN ('WARNING', 'TEMPORARY_SUSPENSION', 'PERMANENT_BAN', 'CHAT_RESTRICTION')",
            name="chk_moderation_actions_type_valid",
        ),
    )
    op.create_index("idx_moderation_actions_user", "moderation_actions", ["user_id", "expires_at"])

    # -------------------------------------------------------------------------
    # 14. wallets
    # -------------------------------------------------------------------------
    op.create_table(
        "wallets",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("purchased_coins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_wallets"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_wallets_user_id", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("purchased_coins >= 0", name="chk_wallets_purchased_coins_non_negative"),
    )

    # -------------------------------------------------------------------------
    # 15. wallet_daily_allowances
    # -------------------------------------------------------------------------
    op.create_table(
        "wallet_daily_allowances",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("allowance_date", sa.Date(), nullable=False),
        sa.Column("allocated_credits", sa.Integer(), nullable=False),
        sa.Column("used_credits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "remaining_credits",
            sa.Integer(),
            sa.Computed("allocated_credits - used_credits", persisted=True),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wallet_daily_allowances"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_wallet_daily_allowances_user_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id", "allowance_date", name="uq_wallet_daily_allowances_user_date"
        ),
        sa.UniqueConstraint(
            "id", "user_id", "allowance_date", name="uq_wallet_daily_allowances_composite_lineage"
        ),
        sa.CheckConstraint(
            "allocated_credits >= 0", name="chk_daily_allowance_allocated_non_negative"
        ),
        sa.CheckConstraint(
            "used_credits >= 0 AND used_credits <= allocated_credits",
            name="chk_daily_allowance_used_bounds",
        ),
    )

    # -------------------------------------------------------------------------
    # 16. wallet_ledgers
    # -------------------------------------------------------------------------
    op.create_table(
        "wallet_ledgers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("wallet_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("transaction_type", sa.String(length=30), nullable=False),
        sa.Column("credit_type", sa.String(length=20), nullable=False),
        sa.Column("allowance_id", sa.BigInteger(), nullable=True),
        sa.Column("allowance_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("purchased_balance_before", sa.Integer(), nullable=False),
        sa.Column("purchased_balance_after", sa.Integer(), nullable=False),
        sa.Column("free_balance_before", sa.Integer(), nullable=False),
        sa.Column("free_balance_after", sa.Integer(), nullable=False),
        sa.Column("reference_id", sa.String(length=64), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_wallet_ledgers"),
        sa.UniqueConstraint("idempotency_key", name="uq_wallet_ledgers_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["wallet_user_id"],
            ["users.id"],
            name="fk_wallet_ledgers_wallet_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["allowance_id", "wallet_user_id", "allowance_date"],
            [
                "wallet_daily_allowances.id",
                "wallet_daily_allowances.user_id",
                "wallet_daily_allowances.allowance_date",
            ],
            name="fk_wallet_ledgers_allowance_lineage",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_wallet_ledgers_actor_id", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "credit_type IN ('FREE', 'PURCHASED')", name="chk_wallet_ledgers_credit_type_valid"
        ),
        sa.CheckConstraint(
            "purchased_balance_before >= 0 AND purchased_balance_after >= 0",
            name="chk_wallet_ledgers_purchased_balance_bounds",
        ),
        sa.CheckConstraint(
            "free_balance_before >= 0 AND free_balance_after >= 0",
            name="chk_wallet_ledgers_free_balance_bounds",
        ),
        sa.CheckConstraint(
            "(credit_type = 'FREE' AND allowance_id IS NOT NULL AND allowance_date IS NOT NULL) OR "
            "(credit_type = 'PURCHASED' AND allowance_id IS NULL AND allowance_date IS NULL)",
            name="chk_wallet_ledgers_allowance_consistency",
        ),
    )
    op.create_index(
        "idx_wallet_ledgers_user", "wallet_ledgers", ["wallet_user_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "idx_wallet_ledgers_allowance",
        "wallet_ledgers",
        ["allowance_id"],
        postgresql_where=sa.text("allowance_id IS NOT NULL"),
    )

    # -------------------------------------------------------------------------
    # 17. system_configurations
    # -------------------------------------------------------------------------
    op.create_table(
        "system_configurations",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_system_configurations"),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_system_configurations_updated_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "category IN ('FINANCIAL', 'CHAT', 'MEDIA', 'SYSTEM')",
            name="chk_system_configurations_category_valid",
        ),
        sa.CheckConstraint(
            "key <> 'video_size_limit_bytes' OR ("
            "jsonb_typeof(value) = 'object' AND "
            "value ? 'limit' AND "
            "jsonb_typeof(value->'limit') = 'number' AND "
            "(value->>'limit') ~ '^[1-9][0-9]{0,7}$' AND "
            "(value->>'limit')::integer <= 10485760)",
            name="chk_system_configurations_video_size_ceiling",
        ),
        sa.CheckConstraint(
            "key <> 'message_retention_days' OR ("
            "jsonb_typeof(value) = 'object' AND "
            "value ? 'days' AND "
            "jsonb_typeof(value->'days') = 'number' AND "
            "(value->>'days') ~ '^[1-9][0-9]{0,1}$' AND "
            "(value->>'days')::integer <= 30)",
            name="chk_system_configurations_retention_days_ceiling",
        ),
    )

    # -------------------------------------------------------------------------
    # 18. admin_audit_logs
    # -------------------------------------------------------------------------
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_type", sa.String(length=30), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("payload_before", postgresql.JSONB(), nullable=True),
        sa.Column("payload_after", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_audit_logs"),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["users.id"],
            name="fk_admin_audit_logs_admin_user_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "idx_admin_audit_logs_search",
        "admin_audit_logs",
        ["admin_user_id", "action", sa.text("created_at DESC")],
    )

    # -------------------------------------------------------------------------
    # PostgreSQL Triggers and Immutability Functions
    # -------------------------------------------------------------------------

    # 1. users age declaration guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_users_age_declaration_immutability()
    RETURNS TRIGGER AS $$
    DECLARE
        v_db_clock TIMESTAMPTZ := clock_timestamp();
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.age_18_plus_declared IS TRUE THEN
                NEW.age_declared_at := v_db_clock;
                NEW.age_declared_date := (v_db_clock AT TIME ZONE 'Asia/Tehran')::date;
            ELSE
                NEW.age_18_plus_declared := FALSE;
                NEW.age_declared_at := NULL;
                NEW.age_declared_date := NULL;
            END IF;

        ELSIF TG_OP = 'UPDATE' THEN
            IF OLD.age_18_plus_declared IS NOT TRUE AND NEW.age_18_plus_declared IS TRUE THEN
                NEW.age_declared_at := v_db_clock;
                NEW.age_declared_date := (v_db_clock AT TIME ZONE 'Asia/Tehran')::date;

            ELSIF OLD.age_18_plus_declared IS TRUE THEN
                IF NEW.age_18_plus_declared IS DISTINCT FROM TRUE OR
                   NEW.age_declared_at IS DISTINCT FROM OLD.age_declared_at OR
                   NEW.age_declared_date IS DISTINCT FROM OLD.age_declared_date THEN
                    RAISE EXCEPTION 'Adult age declaration, declaration timestamp, and declaration date are permanent and immutable.';
                END IF;
            ELSE
                NEW.age_declared_at := NULL;
                NEW.age_declared_date := NULL;
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_users_declaration_guard
    BEFORE INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION trg_users_age_declaration_immutability();
    """)

    # 2. user_profiles age immutability guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_user_profiles_age_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.age_declared_date IS DISTINCT FROM OLD.age_declared_date THEN
            RAISE EXCEPTION 'age_declared_date is immutable once established.';
        END IF;
        IF NEW.age_18_plus_declared IS DISTINCT FROM OLD.age_18_plus_declared THEN
            RAISE EXCEPTION 'age_18_plus_declared cannot be modified.';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_user_profiles_age_guard
    BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION trg_user_profiles_age_immutability();
    """)

    # 3. chat_sessions lifecycle & origin guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_chat_sessions_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id OR
           NEW.chat_type IS DISTINCT FROM OLD.chat_type OR
           NEW.participant_one_id IS DISTINCT FROM OLD.participant_one_id OR
           NEW.participant_two_id IS DISTINCT FROM OLD.participant_two_id OR
           NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Chat session core identifiers, type, participants, and creation timestamp are immutable.';
        END IF;

        IF OLD.chat_type = 'DIRECT' THEN
            IF NEW.direct_request_id IS DISTINCT FROM OLD.direct_request_id OR
               NEW.direct_request_status IS DISTINCT FROM OLD.direct_request_status THEN
                RAISE EXCEPTION 'Direct chat request reference (direct_request_id, direct_request_status) is permanent and immutable.';
            END IF;
        END IF;

        IF OLD.chat_type = 'RANDOM' THEN
            IF NEW.direct_request_id IS NOT NULL OR NEW.direct_request_status IS NOT NULL THEN
                RAISE EXCEPTION 'Random chat sessions cannot be converted into direct chats.';
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_chat_sessions_lifecycle_guard
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION trg_chat_sessions_immutability();
    """)

    # 4. chat_messages immutability guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_chat_messages_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id OR
           NEW.chat_id IS DISTINCT FROM OLD.chat_id OR
           NEW.sender_id IS DISTINCT FROM OLD.sender_id OR
           NEW.recipient_id IS DISTINCT FROM OLD.recipient_id OR
           NEW.created_at IS DISTINCT FROM OLD.created_at OR
           NEW.message_type IS DISTINCT FROM OLD.message_type OR
           NEW.content_hash IS DISTINCT FROM OLD.content_hash OR
           NEW.telegram_file_id IS DISTINCT FROM OLD.telegram_file_id OR
           NEW.file_size_bytes IS DISTINCT FROM OLD.file_size_bytes OR
           NEW.duration_seconds IS DISTINCT FROM OLD.duration_seconds THEN
            RAISE EXCEPTION 'Chat message content, participants, session linkage, and cryptographic fingerprint are immutable.';
        END IF;

        IF NEW.expires_at > OLD.expires_at THEN
            RAISE EXCEPTION 'Message retention expiration cannot be extended.';
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_chat_messages_guard
    BEFORE UPDATE ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION trg_chat_messages_immutability();
    """)

    # 5. message_evidence_holds consistency guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_evidence_holds_session_consistency()
    RETURNS TRIGGER AS $$
    DECLARE
        v_msg_chat_id UUID;
        v_msg_hash VARCHAR(64);
    BEGIN
        IF NEW.message_id IS NOT NULL THEN
            SELECT chat_id, content_hash
            INTO v_msg_chat_id, v_msg_hash
            FROM chat_messages
            WHERE id = NEW.message_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Referenced message % does not exist in chat_messages.', NEW.message_id;
            END IF;

            IF v_msg_chat_id IS DISTINCT FROM NEW.chat_session_id THEN
                RAISE EXCEPTION 'Evidence hold session mismatch: message % belongs to chat session %, but hold specifies chat session %.',
                    NEW.message_id, v_msg_chat_id, NEW.chat_session_id;
            END IF;

            IF NEW.message_content_hash IS NULL THEN
                NEW.message_content_hash := v_msg_hash;
            ELSIF NEW.message_content_hash IS DISTINCT FROM v_msg_hash THEN
                RAISE EXCEPTION 'Evidence hold content hash mismatch for message %.', NEW.message_id;
            END IF;
        END IF;

        IF TG_OP = 'UPDATE' THEN
            IF NEW.chat_session_id IS DISTINCT FROM OLD.chat_session_id THEN
                RAISE EXCEPTION 'chat_session_id is permanent and immutable on evidence holds.';
            END IF;

            IF NEW.message_content_hash IS DISTINCT FROM OLD.message_content_hash THEN
                RAISE EXCEPTION 'message_content_hash is permanent and immutable on evidence holds.';
            END IF;

            IF OLD.message_id IS NOT NULL AND NEW.message_id IS NOT NULL AND NEW.message_id IS DISTINCT FROM OLD.message_id THEN
                RAISE EXCEPTION 'Evidence hold message_id cannot be reassigned to a different message.';
            END IF;

            IF OLD.message_id IS NULL AND NEW.message_id IS NOT NULL THEN
                RAISE EXCEPTION 'Cannot reattach message_id to an evidence hold whose message content was scrubbed.';
            END IF;
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_message_evidence_holds_guard
    BEFORE INSERT OR UPDATE ON message_evidence_holds
    FOR EACH ROW EXECUTE FUNCTION trg_evidence_holds_session_consistency();
    """)

    # 6. wallet_ledgers append-only guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_wallet_ledgers_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'wallet_ledgers is strictly append-only. UPDATE and DELETE operations are prohibited.';
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_wallet_ledgers_append_only
    BEFORE UPDATE OR DELETE ON wallet_ledgers
    FOR EACH ROW EXECUTE FUNCTION trg_wallet_ledgers_immutability();
    """)

    # 7. admin_audit_logs append-only guard
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_admin_audit_logs_immutability()
    RETURNS TRIGGER AS $$
    BEGIN
        RAISE EXCEPTION 'admin_audit_logs is strictly append-only. UPDATE and DELETE operations are prohibited.';
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_admin_audit_logs_append_only
    BEFORE UPDATE OR DELETE ON admin_audit_logs
    FOR EACH ROW EXECUTE FUNCTION trg_admin_audit_logs_immutability();
    """)

    # 8. Database role permissions (conditional on app_user existing)
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            REVOKE UPDATE, DELETE, TRUNCATE ON wallet_ledgers FROM app_user;
            REVOKE UPDATE, DELETE, TRUNCATE ON admin_audit_logs FROM app_user;
            GRANT SELECT, INSERT ON wallet_ledgers TO app_user;
            GRANT SELECT, INSERT ON admin_audit_logs TO app_user;
        END IF;
    END;
    $$;
    """)


def downgrade() -> None:
    # Drop triggers and functions
    op.execute("DROP TRIGGER IF EXISTS trg_admin_audit_logs_append_only ON admin_audit_logs;")
    op.execute("DROP FUNCTION IF EXISTS trg_admin_audit_logs_immutability();")

    op.execute("DROP TRIGGER IF EXISTS trg_wallet_ledgers_append_only ON wallet_ledgers;")
    op.execute("DROP FUNCTION IF EXISTS trg_wallet_ledgers_immutability();")

    op.execute("DROP TRIGGER IF EXISTS trg_message_evidence_holds_guard ON message_evidence_holds;")
    op.execute("DROP FUNCTION IF EXISTS trg_evidence_holds_session_consistency();")

    op.execute("DROP TRIGGER IF EXISTS trg_chat_messages_guard ON chat_messages;")
    op.execute("DROP FUNCTION IF EXISTS trg_chat_messages_immutability();")

    op.execute("DROP TRIGGER IF EXISTS trg_chat_sessions_lifecycle_guard ON chat_sessions;")
    op.execute("DROP FUNCTION IF EXISTS trg_chat_sessions_immutability();")

    op.execute("DROP TRIGGER IF EXISTS trg_user_profiles_age_guard ON user_profiles;")
    op.execute("DROP FUNCTION IF EXISTS trg_user_profiles_age_immutability();")

    op.execute("DROP TRIGGER IF EXISTS trg_users_declaration_guard ON users;")
    op.execute("DROP FUNCTION IF EXISTS trg_users_age_declaration_immutability();")

    # Drop tables in reverse dependency order
    op.drop_table("admin_audit_logs")
    op.drop_table("system_configurations")
    op.drop_table("wallet_ledgers")
    op.drop_table("wallet_daily_allowances")
    op.drop_table("wallets")
    op.drop_table("moderation_actions")
    op.drop_table("message_evidence_holds")
    op.drop_table("admin_users")
    op.drop_table("reports")
    op.drop_table("chat_messages")
    op.drop_table("active_chat_participants")
    op.drop_table("chat_sessions")
    op.drop_table("user_blocks")
    op.drop_table("user_connections")
    op.drop_table("direct_requests")
    op.drop_table("user_preferences")
    op.drop_table("user_profiles")
    op.drop_table("users")
