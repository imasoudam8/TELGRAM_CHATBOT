"""FSM States for profile management, discovery filters, and direct requests."""

from aiogram.fsm.state import State, StatesGroup


class ProfileCreationSG(StatesGroup):
    """Conversation states for creating a new user profile."""

    waiting_for_name = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_city = State()
    waiting_for_bio = State()
    review_and_confirm = State()


class ProfileEditSG(StatesGroup):
    """Conversation states for editing an existing user profile."""

    editing_display_name = State()
    editing_age = State()
    editing_gender = State()
    editing_city = State()
    editing_bio = State()


class DiscoveryPrefsSG(StatesGroup):
    """Conversation states for editing discovery/matching filters and ID lookup."""

    waiting_for_age_range = State()
    waiting_for_cities = State()
    waiting_for_lookup_public_id = State()


class DirectRequestSG(StatesGroup):
    """Conversation states for sending a direct request by public ID."""

    waiting_for_target_public_id = State()


class ReportSG(StatesGroup):
    """Conversation states for submitting an abuse/safety report."""

    selecting_category = State()
    entering_description = State()


class AdminSG(StatesGroup):
    """Conversation states for authenticated Telegram admin operations."""

    waiting_for_user_public_id = State()
    waiting_for_report_notes = State()
    waiting_for_moderation_duration = State()
    waiting_for_moderation_reason = State()
    waiting_for_pair_block_input = State()
    waiting_for_pair_unblock_input = State()
    waiting_for_wallet_public_id = State()
    waiting_for_wallet_amount = State()
    waiting_for_wallet_reason = State()
    confirming_wallet_op = State()
    waiting_for_config_value = State()
    waiting_for_config_reason = State()
    confirming_config_update = State()
    waiting_for_role_public_id = State()
    waiting_for_role_reason = State()
    confirming_role_change = State()
