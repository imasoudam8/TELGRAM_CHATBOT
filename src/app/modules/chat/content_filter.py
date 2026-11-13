"""Privacy and safety content filtering for anonymous chat messages."""

import re
from dataclasses import dataclass

# Normalize Persian and Arabic-Indic digits to ASCII digits
_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# URL / protocol / www / Telegram invite patterns
_URL_RE = re.compile(
    r"(?:https?://|ftp://|tg://|www\.|t\.me/|telegram\.me/|telegram\.dog/|wa\.me/)",
    re.IGNORECASE,
)

# Telegram @username mentions (3+ chars starting with letter or standard username)
_USERNAME_RE = re.compile(r"(?:^|[\s(\[«\"'\-])@[A-Za-z][A-Za-z0-9_]{2,}", re.IGNORECASE)

# Email addresses
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.IGNORECASE)

# Bare domains with explicit common TLDs (avoiding false positives on normal punctuation/numbers)
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|[\s(/\[«\"'])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|ir|io|me|co|info|biz|xyz|online|site|app|dev|tv|cc|gg|ly|to|"
    r"ru|uk|de|fr|edu|gov|ai|shop|store|club|vip|in|us|ca|eu|asia|pro|tech|link)"
    r"(?:[/:?#\s)\]»\"']|$)",
    re.IGNORECASE,
)

# Spaced/obfuscated Telegram link like "t . me"
_OBFUSCATED_TME_RE = re.compile(r"\bt\s*\.\s*me\b", re.IGNORECASE)

# Phone number candidates after Persian/Arabic digit normalization:
# 1. International format starting with + followed by 7-15 digits (separators allowed)
# 2. 0098 followed by 9-10 digits
# 3. 989 followed by 9 digits (12 digits total starting 989)
# 4. Iranian mobile 09xxxxxxxxx (11 digits, separators allowed)
# 5. Iranian landline 0[1-8]xxxxxxxxx (11 digits, separators allowed)
_PHONE_CANDIDATE_RE = re.compile(
    r"(?:\+\s*(?:\d[\s\-.()]*){7,15}\d|"
    r"(?<!\d)(?:0098|09|0[1-8]\d)[\s\-.()]*(?:\d[\s\-.()]*){7,12}\d(?!\d)|"
    r"(?<!\d)989[\s\-.()]*(?:\d[\s\-.()]*){8}\d(?!\d))"
)


@dataclass(frozen=True)
class ContentFilterResult:
    """Outcome of message text/caption safety inspection."""

    allowed: bool
    violation_type: str | None = None
    reason_persian: str | None = None


def normalize_digits(text: str) -> str:
    """Convert Persian and Arabic numerals to ASCII digits."""
    return text.translate(_DIGIT_TRANSLATION)


def validate_message_text(text: str | None) -> ContentFilterResult:
    """Validate message text or media caption against anti-link/contact sharing rules.

    Blocks URLs, Telegram links (t.me, @username), emails, bare domains, and phone numbers
    without blocking normal harmless conversation.
    """
    if not text:
        return ContentFilterResult(allowed=True)

    normalized = normalize_digits(text)

    if _URL_RE.search(normalized) or _OBFUSCATED_TME_RE.search(normalized):
        return ContentFilterResult(
            allowed=False,
            violation_type="LINK_OR_TELEGRAM_URL",
            reason_persian="ارسال لینک، آدرس اینترنتی یا لینک تلگرام در گفتگوی ناشناس مجاز نیست.",
        )

    if _EMAIL_RE.search(normalized):
        return ContentFilterResult(
            allowed=False,
            violation_type="EMAIL_ADDRESS",
            reason_persian="ارسال نشانی ایمیل یا اطلاعات تماس در گفتگوی ناشناس مجاز نیست.",
        )

    if _USERNAME_RE.search(normalized):
        return ContentFilterResult(
            allowed=False,
            violation_type="USERNAME_MENTION",
            reason_persian="ارسال نام کاربری (@username) در گفتگوی ناشناس مجاز نیست.",
        )

    if _BARE_DOMAIN_RE.search(normalized):
        return ContentFilterResult(
            allowed=False,
            violation_type="BARE_DOMAIN",
            reason_persian="ارسال دامنه یا وب‌سایت در گفتگوی ناشناس مجاز نیست.",
        )

    for match in _PHONE_CANDIDATE_RE.finditer(normalized):
        raw_span = match.group(0)
        digits_only = re.sub(r"\D", "", raw_span)
        is_intl_plus = raw_span.lstrip().startswith("+")
        if is_intl_plus and 8 <= len(digits_only) <= 15:
            return ContentFilterResult(
                allowed=False,
                violation_type="PHONE_NUMBER",
                reason_persian="ارسال شماره تماس در گفتگوی ناشناس مجاز نیست.",
            )
        if digits_only.startswith("09") and len(digits_only) == 11:
            return ContentFilterResult(
                allowed=False,
                violation_type="PHONE_NUMBER",
                reason_persian="ارسال شماره تماس در گفتگوی ناشناس مجاز نیست.",
            )
        if digits_only.startswith("0098") and len(digits_only) in (13, 14):
            return ContentFilterResult(
                allowed=False,
                violation_type="PHONE_NUMBER",
                reason_persian="ارسال شماره تماس در گفتگوی ناشناس مجاز نیست.",
            )
        if digits_only.startswith("989") and len(digits_only) == 12:
            return ContentFilterResult(
                allowed=False,
                violation_type="PHONE_NUMBER",
                reason_persian="ارسال شماره تماس در گفتگوی ناشناس مجاز نیست.",
            )
        if (
            digits_only.startswith("0")
            and not digits_only.startswith("00")
            and len(digits_only) == 11
        ):
            return ContentFilterResult(
                allowed=False,
                violation_type="PHONE_NUMBER",
                reason_persian="ارسال شماره تماس در گفتگوی ناشناس مجاز نیست.",
            )

    return ContentFilterResult(allowed=True)
