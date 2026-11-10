"""FSM States for user profile creation and editing."""

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
