"""
Central configuration. All values are read from environment variables so the
same code runs locally, in Docker, or on any host.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Gemini (AI answers on Pro/Premium). Free and Basic never call Gemini: that's decided per
# gym by its tier (schemas.TIER_CONFIGS llm_enabled), not by blanking the key.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# AI chat engine defaults (Free tier uses deterministic rule-based matching only)
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash")
CHAT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.0"))
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

# Domain & Routing configuration
APP_DOMAIN = os.getenv("APP_DOMAIN", "tarvos.fit")
CHAT_SUBDOMAIN = os.getenv("CHAT_SUBDOMAIN", f"chat.{APP_DOMAIN}")
DEFAULT_GYM_ID = os.getenv("DEFAULT_GYM_ID", "tarvos-fit")
GYM_NAME = os.getenv("GYM_NAME", "")
GYM_LOCATION = os.getenv("GYM_LOCATION", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")

# Security & Rate Limiting
RATE_LIMIT_CHAT_PER_MIN = int(os.getenv("RATE_LIMIT_CHAT_PER_MIN", "30"))
RATE_LIMIT_LEADS_PER_MIN = int(os.getenv("RATE_LIMIT_LEADS_PER_MIN", "10"))
RATE_LIMIT_ADMIN_LOGIN_PER_MIN = int(os.getenv("RATE_LIMIT_ADMIN_LOGIN_PER_MIN", "5"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "500"))

# CORS: comma-separated allowlist — never use wildcard in production
_raw_cors = os.getenv("CORS_ALLOW_ORIGINS", "")
if _raw_cors:
    CORS_ALLOW_ORIGINS = [o.strip() for o in _raw_cors.split(",") if o.strip()]
else:
    _default = [
        f"https://{APP_DOMAIN}",
        f"https://www.{APP_DOMAIN}",
        f"https://{CHAT_SUBDOMAIN}",
    ]
    CORS_ALLOW_ORIGINS = _default

# Allowed hosts for external-URL validation (comma-separated)
_raw_hosts = os.getenv("ALLOWED_EXTERNAL_HOSTS", "instagram.com,www.instagram.com,facebook.com,www.facebook.com,wa.me,maps.google.com,www.google.com,goo.gl")
ALLOWED_EXTERNAL_HOSTS = {h.strip() for h in _raw_hosts.split(",") if h.strip()}

# WhatsApp app secret for Cloud API HMAC verification
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "faiss")  # "faiss" | "qdrant"
QDRANT_URL = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")

DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))  # Render: /var/data (persistent disk)
os.makedirs(DATA_DIR, exist_ok=True)

# WhatsApp Cloud API (Meta) — https://developers.facebook.com/docs/whatsapp/cloud-api
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "gym_ai_verify_token")

# Lead capture webhook & notifications
LEAD_WEBHOOK_URL = os.getenv("LEAD_WEBHOOK_URL", "")
OWNER_NOTIFICATION_EMAIL = os.getenv("OWNER_NOTIFICATION_EMAIL", "")
OWNER_NOTIFICATION_WHATSAPP = os.getenv("OWNER_NOTIFICATION_WHATSAPP", "")

# SMTP Email Configuration (for lead email alerts)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() in ("true", "1", "yes")

# Google Places API (Official Reviews Integration)
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Instagram Graph API (Official Media Feed Integration)
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID", "")

CHUNK_SIZE_CHARS = int(os.getenv("CHUNK_SIZE_CHARS", "1200"))
CHUNK_OVERLAP_CHARS = int(os.getenv("CHUNK_OVERLAP_CHARS", "150"))
TOP_K = int(os.getenv("TOP_K", "5"))

# --- Multi-gym hosting (read in main.py) -------------------------------------------
SESSION_SECRET = os.getenv("SESSION_SECRET", "")          # signs login cookies; set in production
SITE_BASE_DOMAIN = os.getenv("SITE_BASE_DOMAIN", "arivayyaai.com")   # gym1.arivayyaai.com
SITE_SCHEME = os.getenv("SITE_SCHEME", "https")
SITE_URL_MODE = os.getenv("SITE_URL_MODE", "auto")         # auto | subdomain | path

# --- WhatsApp templates & lead retention (read in leads_manager.py) -----------------
WHATSAPP_LEAD_ALERT_TEMPLATE = os.getenv("WHATSAPP_LEAD_ALERT_TEMPLATE", "")
WHATSAPP_LEAD_WELCOME_TEMPLATE = os.getenv("WHATSAPP_LEAD_WELCOME_TEMPLATE", "")
WHATSAPP_TEMPLATE_LANG = os.getenv("WHATSAPP_TEMPLATE_LANG", "en")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v20.0")
LEAD_RETENTION_DAYS = int(os.getenv("LEAD_RETENTION_DAYS", "90"))
