"""
Security utilities: password hashing, admin session management,
PII redaction for outbound LLM calls, webhook signature verification.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Optional

from fastapi import HTTPException, Request, Response

from . import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return a bcrypt hash string for the given plaintext password."""
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the bcrypt *hashed* digest."""
    import bcrypt
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Admin sessions (signed, HttpOnly cookies)
# ---------------------------------------------------------------------------

_ADMIN_COOKIE = "gym_admin_session"
_SESSION_TTL = 24 * 3600  # 24 hours


def _sign(data: str) -> str:
    """HMAC-SHA256 of *data* using ADMIN_SESSION_SECRET."""
    return hmac.new(
        config.ADMIN_SESSION_SECRET.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_admin_session(gym_id: str) -> str:
    """Return a signed session token string (payload.signature)."""
    ts = str(int(time.time()))
    payload = f"{gym_id}:{ts}"
    return f"{payload}.{_sign(payload)}"


def parse_admin_session(token: str) -> Optional[dict]:
    """Return {"gym_id", "ts"} if the token is valid, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = _sign(payload)
        if not hmac.compare_digest(sig, expected):
            return None
        gym_id, ts = payload.split(":", 1)
        if time.time() - int(ts) > _SESSION_TTL:
            return None
        return {"gym_id": gym_id, "ts": int(ts)}
    except (ValueError, KeyError):
        return None


def set_admin_cookie(response: Response, gym_id: str) -> None:
    """Attach an HttpOnly, Secure, SameSite=Lax admin session cookie."""
    token = create_admin_session(gym_id)
    is_prod = os.getenv("ENVIRONMENT", "").lower() in ("production", "prod", "live")
    response.set_cookie(
        key=_ADMIN_COOKIE,
        value=token,
        httponly=True,
        secure=is_prod,
        samesite="lax",
        max_age=_SESSION_TTL,
        path="/",
    )


def clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(key=_ADMIN_COOKIE, path="/")


def get_admin_session(request: Request) -> dict:
    """Read and verify the admin session cookie. Raise 401 if missing/invalid."""
    token = request.cookies.get(_ADMIN_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated. Please log in.")
    session = parse_admin_session(token)
    if session is None:
        raise HTTPException(401, "Session expired. Please log in again.")
    return session


# ---------------------------------------------------------------------------
# PII redaction for outbound LLM calls
# ---------------------------------------------------------------------------

# Patterns cover the major Indian + global PII types the system is likely to
# encounter.  Each regex substitutes a safe placeholder.

PHONE_RE = re.compile(
    r"(?:(?:\+?91[\s-]?)|0)?"
    r"(?:(?:6|7|8|9)\d{9})",
)

# International E.164 phones: +[1-9] followed by 7–14 digits
INTL_PHONE_RE = re.compile(
    r"\+[1-9]\d{6,14}(?!\d)",
)

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# UPI ID: word chars @ upi / paytm / ybl / okaxis / etc.
UPI_RE = re.compile(
    r"[A-Za-z0-9._%+\-]{3,}@[A-Za-z0-9]{2,}",
)

# Aadhaar: 4-4-4 digit grouping with optional spaces
AADHAAR_RE = re.compile(
    r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
)

# PAN: 5 uppercase letters, 4 digits, 1 uppercase letter
PAN_RE = re.compile(
    r"\b[A-Z]{5}\d{4}[A-Z]\b",
)

# Credit / debit card number (13-19 digits, possibly separated)
CARD_RE = re.compile(
    r"\b(?:\d{4}[\s-]){2,4}\d{3,4}\b",
)

NAME_PLACEHOLDER_RE = re.compile(
    r"(?:my name is|i am|call me|i'm)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
    re.IGNORECASE,
)


def redact_pii_for_llm(text: str) -> str:
    """Return *text* with all recognised PII replaced by safe placeholders.

    Order matters — broad patterns go first to avoid partial overlaps.
    """
    if not text:
        return text
    # 1. Names introduced via conversational phrases
    text = NAME_PLACEHOLDER_RE.sub(
        lambda m: m.group(0).split(" ", 1)[0] + " [NAME]", text
    )
    # 2. International phones
    text = INTL_PHONE_RE.sub("[PHONE_NUMBER]", text)
    # 3. Aadhaar
    text = AADHAAR_RE.sub("[AADHAAR]", text)
    # 4. PAN
    text = PAN_RE.sub("[PAN]", text)
    # 5. Indian phones (after INTL to avoid consuming the leading +91)
    text = PHONE_RE.sub("[PHONE_NUMBER]", text)
    # 6. UPI
    text = UPI_RE.sub("[UPI_ID]", text)
    # 7. Cards
    text = CARD_RE.sub("[CARD_NUMBER]", text)
    # 8. Emails
    text = EMAIL_RE.sub("[EMAIL]", text)
    return text


# ---------------------------------------------------------------------------
# Phone masking for user-facing output
# ---------------------------------------------------------------------------

def mask_phone(phone: str) -> str:
    """Return a masked version of an Indian phone for display to the user."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 10:
        return digits[:2] + "******" + digits[-2:]
    return "******"


# ---------------------------------------------------------------------------
# Input sanitization (prompt injection defense)
# ---------------------------------------------------------------------------

# Patterns commonly used in prompt-injection attacks. Matched against user
# input going into LLM prompts. When detected, the offending substring is
# replaced with a safe placeholder so the model can't be tricked into
# ignoring system instructions.
_INJECTION_PATTERNS = [
    (re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)"), "[FILTERED]"),
    (re.compile(r"(?i)disregard\s+(all\s+)?(previous|prior|above)"), "[FILTERED]"),
    (re.compile(r"(?i)forget\s+(all\s+)?(previous|prior|above)"), "[FILTERED]"),
    (re.compile(r"(?i)you\s+are\s+(now|a|an)\s+"), "[FILTERED]"),
    (re.compile(r"(?i)pretend\s+(to\s+be|you\s+are)"), "[FILTERED]"),
    (re.compile(r"(?i)act\s+as\s+(a|an|if)"), "[FILTERED]"),
    (re.compile(r"(?i)system\s*prompt"), "[FILTERED]"),
    (re.compile(r"(?i)reveal\s+(your|the)\s+(system|instructions?|prompt)"), "[FILTERED]"),
    (re.compile(r"(?i)jailbreak"), "[FILTERED]"),
    (re.compile(r"(?i)<script\b[^>]*>"), "[FILTERED]"),
    (re.compile(r"(?i)</script\s*>"), "[FILTERED]"),
    (re.compile(r"(?i)javascript\s*:"), "[FILTERED]"),
    (re.compile(r"(?i)on(error|load|click)\s*="), "[FILTERED]"),
]


def sanitize_input(text: str) -> str:
    """Strip prompt-injection markers and XSS-style payloads from user input
    *before* it reaches an LLM or is stored in chat history."""
    if not text:
        return text
    cleaned = text
    for pattern, replacement in _INJECTION_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def safe_error_message(detail: str, is_admin: bool = False) -> str:
    """Return a generic error to clients while preserving the real one in
    server-side logs only when is_admin is True."""
    if is_admin:
        return detail
    return "An internal error occurred. Please try again later."


# ---------------------------------------------------------------------------
# WhatsApp webhook HMAC verification (Meta Cloud API)
# ---------------------------------------------------------------------------

def verify_whatsapp_signature(
    raw_body: bytes,
    signature_header: Optional[str],
) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta's WhatsApp Cloud API.

    Returns True if the signature is valid.  If no signature is present or the
    secret is not configured, this function returns **False** (reject by
    default).
    """
    app_secret = config.WHATSAPP_APP_SECRET
    if not app_secret or not signature_header:
        return False
    try:
        algo, received_sig = signature_header.split("=", 1)
        if algo.lower() != "sha256":
            return False
        expected = hmac.new(
            app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, received_sig)
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------------------
# URL validation helpers
# ---------------------------------------------------------------------------

_INSTAGRAM_HOSTS = {"www.instagram.com", "instagram.com", "mobile.twitter.com"}


def validate_external_url(url: str, allowed_hosts: set[str]) -> str:
    """Return the URL if it looks like a safe http(s) link to an allowed host.

    Raises HTTPException(400) otherwise.
    """
    if not url:
        return url
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    host_match = re.search(r"https?://([^/]+)", url)
    if not host_match:
        raise HTTPException(400, "Invalid URL format")
    host = host_match.group(1).lower().split(":")[0]
    if host not in allowed_hosts:
        raise HTTPException(400, f"URL host '{host}' is not allowed")
    return url


# ---------------------------------------------------------------------------
# Admin-required FastAPI dependency factory
# ---------------------------------------------------------------------------

def require_admin(request: Request):
    """FastAPI dependency that verifies the admin session cookie.

    Usage::

        @app.get("/something")
        def handler(request: Request, _auth: dict = Depends(require_admin)):
            ...
    """
    return get_admin_session(request)
