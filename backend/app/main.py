import base64
import contextvars
import hashlib
import hmac
import inspect
import json
import secrets
import logging
import threading
import os
import random
import re
import time
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Body, Query, Depends, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from . import security  # noqa: E402  (must come after config)
from .security import (
    hash_password,
    verify_password,
    create_admin_session,
    parse_admin_session,
    set_admin_cookie,
    clear_admin_cookie,
    require_admin,
    redact_pii_for_llm,
    mask_phone,
    verify_whatsapp_signature,
    validate_external_url,
)
from .schemas import (
    GymConfig,
    ChatMessage,
    ChatResponse,
    LeadPayload,
    LeadUpdatePayload,
    DEFAULT_SECTIONS,
)
from .pdf_generator import (
    build_pdf,
    build_rag_chunks,
    build_rag_chunks_from_resolved,
    render_pdf,
    resolve_answers,
    QA_SCHEMA,
    QA_BY_ID,
)
from .pdf_ingest import parse_knowledge_pdf, extract_text, extract_identity
from .rag_store import get_store
from . import chat_engine
from . import whatsapp
from . import migrate
from . import leads_manager
from . import google_reviews
from . import instagram
from . import site_validator
from . import content_validator

logger = logging.getLogger(__name__)

# Automatically run startup data model migrations
migrate.run_migrations()

app = FastAPI(title="Gym AI Enquiry Assistant & Admin Hub", version="2.5.0")

# Security & CORS Middleware — explicit allowlist from CORS_ALLOW_ORIGINS env
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
    max_age=600,
)

# In-memory sliding-window rate limiter per client IP
RATE_LIMIT_STORE: dict[str, list[float]] = defaultdict(list)

def _client_ip(request: Request) -> str:
    # Trust Render.com's X-Forwarded-For header when behind the proxy
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _check_rate_limit(client_ip: str, limit_per_min: int, action_name: str = "request"):
    now = time.time()
    window = 60.0
    timestamps = [t for t in RATE_LIMIT_STORE[client_ip] if now - t < window]
    if len(timestamps) >= limit_per_min:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded for {action_name}. Please slow down.")
    timestamps.append(now)
    RATE_LIMIT_STORE[client_ip] = timestamps


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    client_ip = _client_ip(request)
    path = request.url.path

    # Apply rate limiting to critical public endpoints
    if path.startswith("/api/chat") and request.method == "POST":
        _check_rate_limit(f"chat:{client_ip}", config.RATE_LIMIT_CHAT_PER_MIN, "chat")
    elif "/leads" in path and request.method == "POST":
        _check_rate_limit(f"leads:{client_ip}", config.RATE_LIMIT_LEADS_PER_MIN, "lead submission")

    response: Response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content-Security-Policy: restrict what the browser can load
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "connect-src 'self' https://generativelanguage.googleapis.com; "
        "frame-src 'self' https://www.google.com https://maps.google.com https://maps.googleapis.com; "
        "frame-ancestors 'self';"
    )
    # Allow embedding the chat widget in iframes on tarvos.fit / www.tarvos.fit
    if not (path.startswith("/chat") or path.startswith("/static/")):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response



# Frontend directory path
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))

SESSIONS: dict[str, list[dict]] = {}  # session_id -> chat history


# ------------------------------------------------------------- System / Config ---
_VALID_GYM_ID_RE = re.compile(r'^[a-z0-9][a-z0-9\-]{1,62}$')

def _validate_gym_id(gym_id: str) -> str:
    """Reject path-traversal or otherwise malformed gym IDs."""
    if not _VALID_GYM_ID_RE.match(gym_id):
        raise HTTPException(400, "Invalid gym_id format")
    return gym_id


# ------------------------------------------------------------ Branches ------
# Free / Basic: 1 branch · Pro: up to 3 · Premium: up to 10.
# Branches are kept in a sidecar file ({gym_id}.branches.json) so they survive even
# though GymConfig / Identity in schemas.py do not declare a `branches` field yet.
# Lead → branch links are kept in {gym_id}.lead_branches.json for the same reason
# (leads_manager.create_lead does not take branch arguments yet).
BRANCH_LIMITS = {"free": 1, "basic": 1, "pro": 3, "premium": 10}
MAX_STORED_BRANCHES = max(BRANCH_LIMITS.values())
_BRANCH_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_BRANCH_TEXT_FIELDS = {
    "name": 80, "city": 120, "full_address": 300, "landmark": 120,
    "phone": 20, "whatsapp": 20, "google_maps_url": 500, "opening_hours": 120,
}
_branch_file_lock = threading.Lock()
SESSION_BRANCH: dict[str, dict] = {}  # chat session_id -> {"branch_id", "branch_name"}


async def _raw_json_body(request: Request) -> dict:
    """Raw JSON body, so fields the Pydantic models drop (branches, branch_id) can still be read.
    Starlette caches the body, so this is safe alongside a normal body model."""
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _gym_tier(gym_id: str) -> str:
    # The gym account set by the platform admin is the source of truth
    try:
        acct = _load_accounts().get(gym_id) if "_load_accounts" in globals() else None
        if acct and acct.get("tier") in ("free", "basic", "pro", "premium"):
            return acct["tier"]
    except Exception:
        pass
    try:
        from .schemas import get_tier_limits
        tier = (get_tier_limits(gym_id) or {}).get("tier")
        if tier:
            return str(tier).lower()
    except Exception:
        pass
    try:
        from .schemas import _load_gym_tier
        return str(_load_gym_tier(gym_id) or "free").lower()
    except Exception:
        return "free"


def _max_branches(gym_id: str) -> int:
    return BRANCH_LIMITS.get(_gym_tier(gym_id), 1)


def _clean_branch(raw: dict, index: int) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    b = {}
    for field, max_len in _BRANCH_TEXT_FIELDS.items():
        val = raw.get(field)
        b[field] = str(val).strip()[:max_len] if val is not None else ""
    if not b["name"]:
        return None
    if b["google_maps_url"] and not re.match(r"^https?://", b["google_maps_url"], re.I):
        b["google_maps_url"] = ""
    bid = str(raw.get("id") or "").strip()
    b["id"] = bid if _BRANCH_ID_RE.match(bid) else f"branch_{index + 1}"
    b["is_primary"] = bool(raw.get("is_primary"))
    return b


def _normalize_branches(raw_list) -> list[dict]:
    if not isinstance(raw_list, list):
        return []
    out, seen = [], set()
    for i, raw in enumerate(raw_list):
        b = _clean_branch(raw, i)
        if not b or b["id"] in seen:
            continue
        seen.add(b["id"])
        out.append(b)
    if out and not any(b["is_primary"] for b in out):
        out[0]["is_primary"] = True
    primary_seen = False
    for b in out:  # exactly one primary
        if b["is_primary"] and not primary_seen:
            primary_seen = True
        else:
            b["is_primary"] = False
    out.sort(key=lambda b: 0 if b["is_primary"] else 1)  # primary first → always inside the tier cap
    return out[:MAX_STORED_BRANCHES]


def _branches_path(gym_id: str) -> str:
    return os.path.join(config.DATA_DIR, f"{gym_id}.branches.json")


def _save_branches(gym_id: str, branches: list[dict]) -> None:
    with _branch_file_lock:
        with open(_branches_path(gym_id), "w", encoding="utf-8") as f:
            json.dump(branches, f, indent=2, ensure_ascii=False)


def _load_all_branches(gym_id: str) -> list[dict]:
    """Every saved branch (extras are kept after a downgrade so they come back on upgrade)."""
    path = _branches_path(gym_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _normalize_branches(json.load(f))
        except Exception as e:
            logger.error(f"Error reading branches for {gym_id}: {e}")
    return []


def _active_branches(gym_id: str) -> list[dict]:
    """Branches live for the gym's current tier. Falls back to one branch built from identity."""
    branches = _load_all_branches(gym_id)[: _max_branches(gym_id)]
    if branches:
        return branches
    try:
        ident = _gym_identity_base(gym_id)
    except Exception:
        ident = {}
    area = str(ident.get("city") or "").split(",")[0].strip()
    name = ident.get("gym_name") or gym_id.replace("-", " ").title()
    return [{
        "id": "main",
        "name": f"{name} – {area}" if area else name,
        "city": ident.get("city") or "", "full_address": ident.get("full_address") or "",
        "landmark": ident.get("landmark") or "", "phone": ident.get("primary_phone") or "",
        "whatsapp": ident.get("whatsapp_number") or "", "google_maps_url": ident.get("google_maps_url") or "",
        "opening_hours": ident.get("opening_hours") or "", "is_primary": True,
    }]


def _public_branch(b: dict) -> dict:
    return {k: b.get(k, "") for k in ("id", "name", "city", "full_address", "landmark", "phone",
                                      "whatsapp", "google_maps_url", "opening_hours", "is_primary")}


def _resolve_branch(gym_id: str, branch_id: Optional[str], branch_name: Optional[str]) -> dict:
    """Match what the visitor sent to a live branch. The canonical name from config is used,
    so a tampered request can't write an arbitrary branch name. Single-branch gyms always
    resolve to that branch."""
    active = _active_branches(gym_id)
    bid = (branch_id or "").strip()
    bname = (branch_name or "").strip().lower()
    match = None
    if bid:
        match = next((b for b in active if b["id"] == bid), None)
    if not match and bname:
        match = next((b for b in active if b["name"].lower() == bname), None)
    if not match and len(active) == 1:
        match = active[0]
    return {"branch_id": match["id"], "branch_name": match["name"]} if match else {"branch_id": "", "branch_name": ""}


def _lead_branches_path(gym_id: str) -> str:
    return os.path.join(config.DATA_DIR, f"{gym_id}.lead_branches.json")


def _load_lead_branches(gym_id: str) -> dict:
    path = _lead_branches_path(gym_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _record_lead_branch(gym_id: str, lead_id: Optional[str], branch: dict) -> None:
    if not lead_id or not branch.get("branch_id"):
        return
    with _branch_file_lock:
        data = _load_lead_branches(gym_id)
        data[str(lead_id)] = {"branch_id": branch["branch_id"], "branch_name": branch["branch_name"]}
        with open(_lead_branches_path(gym_id), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


_BRANCH_IN_MESSAGE_RE = re.compile(r"Branch:\s*([^|]+)")


def _lead_id_of(lead) -> Optional[str]:
    if isinstance(lead, dict):
        return lead.get("id") or lead.get("lead_id")
    return getattr(lead, "id", None) or getattr(lead, "lead_id", None)


def _attach_branch(gym_id: str, lead: dict, lead_branch_map: dict) -> dict:
    """Add branch_id / branch_name to a lead dict: stored field → sidecar → 'Branch: X' in message."""
    if not isinstance(lead, dict) or lead.get("branch_id") or lead.get("branch_name"):
        return lead
    linked = lead_branch_map.get(str(_lead_id_of(lead) or ""))
    if linked:
        lead.update(linked)
        return lead
    m = _BRANCH_IN_MESSAGE_RE.search(str(lead.get("message") or ""))
    if m:
        lead.update(_resolve_branch(gym_id, None, m.group(1).strip()))
        if not lead.get("branch_name"):
            lead["branch_name"] = m.group(1).strip()
    return lead


_BRANCH_QUESTION_RE = re.compile(r"\b(branch|branches|outlet|outlets|other locations?|all locations|how many (gyms|centres|centers|locations))\b", re.I)


def _branch_list_reply(gym_id: str) -> str:
    active = _active_branches(gym_id)
    lines = [f"📍 {_gym_name(gym_id)} has {len(active)} branches:"]
    for b in active:
        line = f"• **{b['name']}**"
        if b.get("full_address") or b.get("city"):
            line += f" — {b.get('full_address') or b.get('city')}"
        if b.get("opening_hours"):
            line += f" (🕒 {b['opening_hours']})"
        if b.get("phone"):
            line += f" · 📞 {b['phone']}"
        lines.append(line)
    lines.append("Tell me which branch suits you and I can book your free trial pass there.")
    return "\n".join(lines)


@app.get("/api/system/config")
def get_system_config():
    """Returns domain information and current assistant runtime parameters."""
    return {
        "app_domain": config.APP_DOMAIN,
        "chat_subdomain": config.CHAT_SUBDOMAIN,
        "default_gym_id": config.DEFAULT_GYM_ID,
        "gemini_chat_model": config.GEMINI_CHAT_MODEL,
        "gemini_embed_model": config.GEMINI_EMBED_MODEL,
        "vector_backend": config.VECTOR_BACKEND,
        "whatsapp_configured": bool(config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID),
        "google_places_configured": bool(config.GOOGLE_PLACES_API_KEY),
        "instagram_configured": bool(config.INSTAGRAM_ACCESS_TOKEN),
        "rate_limit_chat_per_min": config.RATE_LIMIT_CHAT_PER_MIN,
        "max_input_chars": config.MAX_INPUT_CHARS,
    }


@app.get("/robots.txt", response_class=Response)
def get_robots_txt():
    content = f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /leads\nDisallow: /dashboard\nSitemap: https://{config.APP_DOMAIN}/sitemap.xml\n"
    return Response(content=content, media_type="text/plain")

@app.get("/sitemap.xml", response_class=Response)
def get_sitemap_xml():
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://{config.APP_DOMAIN}/</loc>
    <lastmod>2026-09-04</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://{config.APP_DOMAIN}/site</loc>
    <lastmod>2026-09-04</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://{config.APP_DOMAIN}/chat</loc>
    <lastmod>2026-09-04</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""
    return Response(content=xml_content, media_type="application/xml")


# ============================================================ Roles & gym accounts ===
# Two roles:
#   superadmin — the platform admin ("admin" user). Manages every gym, tiers and plans.
#   owner      — one login per gym. Sees only their gym: setup sections, leads, site, chat.
#                Cannot change tier, plan dates, quotas or other gyms.
# Sessions are an HMAC-signed HttpOnly cookie (gym_session). Gym accounts live in
# data/gym_accounts.json with bcrypt password hashes (security.hash_password).
SESSION_COOKIE = "gym_session"
SESSION_TTL_SECONDS = 12 * 3600
VALID_TIERS = ("free", "basic", "pro", "premium")
_accounts_lock = threading.Lock()


def _session_secret() -> bytes:
    env_secret = getattr(config, "SESSION_SECRET", None) or os.environ.get("SESSION_SECRET")
    if env_secret:
        return str(env_secret).encode()
    path = os.path.join(config.DATA_DIR, ".session_secret")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    return val.encode()
        val = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(val)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return val.encode()
    except Exception:
        # last resort: per-process secret (sessions reset on restart)
        global _PROCESS_SECRET
        try:
            return _PROCESS_SECRET
        except NameError:
            _PROCESS_SECRET = secrets.token_hex(32).encode()
            return _PROCESS_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _make_session_token(payload: dict) -> str:
    body = _b64(json.dumps({**payload, "exp": int(time.time()) + SESSION_TTL_SECONDS}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _read_session_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_session_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64(body))
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


def _set_session_cookie(response: Response, request: Request, payload: dict) -> None:
    response.set_cookie(
        SESSION_COOKIE, _make_session_token(payload),
        max_age=SESSION_TTL_SECONDS, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )


def _legacy_admin_session(request: Request) -> Optional[dict]:
    """Accept the existing security.require_admin cookie as superadmin (sessions created before
    this change). Only used when it can be called with just the request; fails closed otherwise."""
    # With no ADMIN_SESSION_SECRET the old cookie would be signed with an empty key and
    # could be forged — never accept it then.
    if not getattr(config, "ADMIN_SESSION_SECRET", ""):
        return None
    try:
        params = inspect.signature(require_admin).parameters
        kwargs = {}
        for name, p in params.items():
            if name == "request" or p.annotation is Request:
                kwargs[name] = request
            elif p.default is inspect.Parameter.empty:
                return None
            else:
                return None  # FastAPI-style defaults (Cookie/Header) can't be resolved here
        result = require_admin(**kwargs)
        if inspect.isawaitable(result):
            return None
        return {"role": "superadmin", "username": "admin", "gym_id": None, "legacy": True}
    except Exception:
        return None


def _current_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = _read_session_token(token)
        if sess:
            return sess
    return _legacy_admin_session(request)


def _accounts_path() -> str:
    return os.path.join(config.DATA_DIR, "gym_accounts.json")


def _load_accounts() -> dict:
    path = _accounts_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Error reading gym accounts: {e}")
    return {}


def _save_accounts(accounts: dict) -> None:
    path = _accounts_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _get_account(gym_id: str) -> Optional[dict]:
    return _load_accounts().get(gym_id)


def _find_account_by_username(username: str) -> Optional[dict]:
    uname = (username or "").strip().lower()
    for acct in _load_accounts().values():
        if str(acct.get("username", "")).lower() == uname:
            return acct
    return None


def _plan_state(acct: dict) -> dict:
    today = date.today()
    end = acct.get("plan_end") or ""
    days_left, expired = None, False
    try:
        if end:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
            days_left = (end_d - today).days
            expired = days_left < 0
    except ValueError:
        pass
    return {"plan_expired": expired, "days_left": days_left}


def _public_account(acct: dict) -> dict:
    """Account without the password hash."""
    out = {k: v for k, v in acct.items() if not k.startswith("_") and k != "password_hash"}
    out.update(_plan_state(acct))
    out["branches_count"] = len(_load_all_branches(acct["gym_id"])) if acct.get("gym_id") else 0
    out["subdomain"] = _account_subdomain(acct)
    out["site_url"] = _site_url(acct.get("gym_id", ""), acct)
    return out


# ------------------------------------------------------------ Site addresses ---
# Every gym gets <subdomain>.<SITE_BASE_DOMAIN> (subdomain defaults to its gym_id),
# e.g. gym1.arivayyaai.com, served by this one service through a wildcard DNS record
# (*.arivayyaai.com → Render). A gym can also have its own custom domain.
SITE_BASE_DOMAIN = (getattr(config, "SITE_BASE_DOMAIN", None) or os.environ.get("SITE_BASE_DOMAIN") or "arivayyaai.com").lower().strip(".")
SITE_SCHEME = os.environ.get("SITE_SCHEME", "https")
RESERVED_SUBDOMAINS = {
    "www", "app", "admin", "api", "mail", "email", "smtp", "imap", "pop", "ftp", "gym", "gyms", "static",
    "assets", "cdn", "dashboard", "setup", "leads", "chat", "site", "blog", "support", "help", "status",
    "docs", "portal", "login", "auth", "dev", "staging", "test", "demo", "arivayya", "render",
}
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


# The host of the request being handled (set by a middleware), so links can match
# the environment: on localhost there's no wildcard DNS, so /site/<gym> is used instead.
_REQUEST_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("request_origin", default=("", ""))
SITE_URL_MODE = os.environ.get("SITE_URL_MODE", "auto").lower()   # auto | subdomain | path


def _is_local_host(host: str) -> bool:
    h = (host or "").lower().split(":")[0]
    return (h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or h.startswith("192.168.") or h.startswith("10.")
            or h.endswith(".local") or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", h) is not None)


def _account_subdomain(acct: dict) -> str:
    return (acct.get("subdomain") or acct.get("gym_id") or "").lower()


def _site_url(gym_id: str, acct: Optional[dict] = None) -> str:
    """Public address of a gym's website."""
    acct = acct if acct is not None else (_get_account(gym_id) or {})
    scheme, host = _REQUEST_ORIGIN.get()
    if SITE_URL_MODE == "path" or (SITE_URL_MODE == "auto" and host and _is_local_host(host)):
        # Local testing: gym1.arivayyaai.com won't resolve until DNS is set up
        return f"{scheme or 'http'}://{host}/site/{gym_id}" if host else f"/site/{gym_id}"
    if acct.get("custom_domain"):
        return f"{SITE_SCHEME}://{acct['custom_domain']}"
    sub = _account_subdomain(acct) or gym_id
    return f"{SITE_SCHEME}://{sub}.{SITE_BASE_DOMAIN}"


def _gym_for_host(host: str) -> Optional[str]:
    """Which gym's public site this web address belongs to (None = not a gym address).
    Handles <sub>.arivayyaai.com, custom domains, and <sub>.localhost for local testing."""
    host = (host or "").lower().split(":")[0].strip(".")
    if not host:
        return None
    accounts = _load_accounts()
    for acct in accounts.values():
        cd = (acct.get("custom_domain") or "").lower()
        if cd and host in (cd, "www." + cd):
            return acct["gym_id"]
    label = None
    for base in (SITE_BASE_DOMAIN, "localhost"):
        if host.endswith("." + base):
            rest = host[: -(len(base) + 1)]
            if "." not in rest:
                label = rest
            break
    if not label or label in RESERVED_SUBDOMAINS:
        return None
    for acct in accounts.values():
        if _account_subdomain(acct) == label:
            return acct["gym_id"]
    # gyms without an account yet (e.g. the default gym) are reachable by their gym_id
    if _VALID_GYM_ID_RE.match(label) and os.path.exists(os.path.join(config.DATA_DIR, f"{label}.config.json")):
        return label
    return ""  # a gym-style address, but no such gym


def _simple_page(title: str, message: str, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>body{{margin:0;font-family:system-ui,sans-serif;background:#f5f7ff;color:#0e1b3d;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}}
.card{{max-width:460px;background:#fff;border:1px solid #dce3f7;border-radius:14px;padding:32px;text-align:center}}h1{{font-size:22px;margin:0 0 10px}}p{{color:#4a5578;line-height:1.5;margin:0 0 16px}}a{{color:#2457f5;font-weight:700}}</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p><a href="https://www.{SITE_BASE_DOMAIN}">Powered by Arivayya AI</a></div></body></html>"""
    return HTMLResponse(html, status_code=status)


def _serve_gym_site(gym_id: str):
    site = os.path.join(config.DATA_DIR, f"{gym_id}.site.html")
    if os.path.exists(site):
        return FileResponse(site, media_type="text/html")
    return _simple_page("Website coming soon", "This gym's website is being set up. Please check back shortly.")


def _apply_tier_to_gym_files(gym_id: str, tier: str, extra_identity: Optional[dict] = None) -> None:
    """Write the tier where the existing tier logic reads it: config.json and identity.json."""
    # schemas.get_tier_limits() (quotas, lead locking, AI chat, templates) reads the tier
    # from config.json, so a gym created in the console needs one before its first save.
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            gym_name = (extra_identity or {}).get("gym_name") or gym_id.replace("-", " ").title()
            data = {"gym_id": gym_id, "identity": {"gym_name": gym_name}, "answers": [], "custom_qa": []}
        data["tier"] = tier
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write tier to config for {gym_id}: {e}")
    ident_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
    try:
        ident = {}
        if os.path.exists(ident_path):
            with open(ident_path, "r", encoding="utf-8") as f:
                ident = json.load(f)
        ident["tier"] = tier
        for k, v in (extra_identity or {}).items():
            if v:
                ident[k] = v
        with open(ident_path, "w", encoding="utf-8") as f:
            json.dump(ident, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write tier to identity for {gym_id}: {e}")


@app.middleware("http")
async def _remember_request_origin(request: Request, call_next):
    token = _REQUEST_ORIGIN.set((request.url.scheme, request.headers.get("host", "")))
    try:
        return await call_next(request)
    finally:
        _REQUEST_ORIGIN.reset(token)


def require_session(request: Request) -> dict:
    sess = _current_session(request)
    if not sess:
        raise HTTPException(401, "Please sign in.")
    return sess


def require_super_admin(request: Request) -> dict:
    sess = require_session(request)
    if sess.get("role") != "superadmin":
        raise HTTPException(403, "Only the platform admin can do this.")
    return sess


def require_gym_access(request: Request, gym_id: str) -> dict:
    """Superadmin: any gym. Owner: only their own gym, and only while the account is active."""
    sess = require_session(request)
    if sess.get("role") == "superadmin":
        return sess
    if sess.get("role") == "owner" and sess.get("gym_id") == gym_id:
        acct = _get_account(gym_id)
        if not acct or acct.get("status", "active") != "active":
            raise HTTPException(403, "This gym account is not active. Please contact support.")
        return {**sess, "tier": acct.get("tier", "free")}
    raise HTTPException(403, "You don't have access to this gym.")


class AdminVerifyPayload(BaseModel):
    username: str = "admin"
    password: str = ""


@app.post("/api/admin/verify")
def verify_admin(payload: AdminVerifyPayload, request: Request, response: Response):
    """Authenticate admin with bcrypt password check + signed HttpOnly cookie."""
    # Rate-limit login attempts per IP
    client_ip = _client_ip(request)
    _check_rate_limit(f"admin-login:{client_ip}", config.RATE_LIMIT_ADMIN_LOGIN_PER_MIN, "admin login")

    if payload.username.strip().lower() != "admin":
        # Gym owner login
        acct = _find_account_by_username(payload.username)
        if not acct or not acct.get("password_hash") or not verify_password(payload.password.strip(), acct["password_hash"]):
            raise HTTPException(401, "Invalid username or password.")
        if acct.get("status", "active") != "active":
            raise HTTPException(403, "This gym account is suspended. Please contact support.")
        _set_session_cookie(response, request, {"role": "owner", "username": acct["username"], "gym_id": acct["gym_id"]})
        with _accounts_lock:
            accounts = _load_accounts()
            if acct["gym_id"] in accounts:
                accounts[acct["gym_id"]]["last_login_at"] = int(time.time())
                _save_accounts(accounts)
        return {"status": "ok", "authenticated": True, "username": acct["username"], "role": "owner",
                "gym_id": acct["gym_id"], "gym_name": acct.get("gym_name"), "tier": acct.get("tier", "free"),
                "plan_start": acct.get("plan_start"), "plan_end": acct.get("plan_end"), **_plan_state(acct)}

    # Platform admin password:
    #  • ADMIN_KEY set (Render env)  → it is the password. Changing it on Render and redeploying
    #    changes the password; any old stored hash is replaced so it can't be used any more.
    #  • ADMIN_KEY not set          → the bcrypt hash stored on first sign-in is used.
    ident_path = os.path.join(config.DATA_DIR, f"{config.DEFAULT_GYM_ID}.identity.json")
    ident: dict = {}
    try:
        with open(ident_path, "r", encoding="utf-8") as fh:
            ident = json.load(fh) or {}
    except Exception:
        ident = {}
    stored_hash = ident.get("_admin_password_hash", "")
    entered = payload.password.strip()
    admin_key = (config.ADMIN_KEY or "").strip()

    if admin_key:
        ok = hmac.compare_digest(entered.encode("utf-8"), admin_key.encode("utf-8"))
        if ok and not (stored_hash and verify_password(entered, stored_hash)):
            try:
                ident["_admin_password_hash"] = hash_password(entered)
                with open(ident_path, "w", encoding="utf-8") as fh:
                    json.dump(ident, fh, indent=2)
            except Exception as e:
                logger.error(f"Could not store admin password hash: {e}")
    elif stored_hash:
        ok = verify_password(entered, stored_hash)
    else:
        logger.error("Admin sign-in refused: ADMIN_KEY is not set and no admin password is stored.")
        ok = False

    if not ok:
        raise HTTPException(401, "Invalid username or password.")
    set_admin_cookie(response, config.DEFAULT_GYM_ID)
    _set_session_cookie(response, request, {"role": "superadmin", "username": "admin", "gym_id": None})
    return {"status": "ok", "authenticated": True, "username": "admin", "role": "superadmin"}


@app.get("/api/admin/login-check")
def admin_login_check():
    """Safe sign-in diagnostics (no secrets): shows why admin sign-in might fail."""
    checks = {"code_version": "admin-key-v2"}
    checks["admin_key_set"] = bool((config.ADMIN_KEY or "").strip())
    checks["admin_key_length"] = len((config.ADMIN_KEY or "").strip())
    ident_path = os.path.join(config.DATA_DIR, f"{config.DEFAULT_GYM_ID}.identity.json")
    try:
        with open(ident_path, "r", encoding="utf-8") as fh:
            checks["stored_password_hash"] = bool((json.load(fh) or {}).get("_admin_password_hash"))
    except Exception:
        checks["stored_password_hash"] = False
    try:
        test = os.path.join(config.DATA_DIR, ".write_test")
        with open(test, "w") as fh:
            fh.write("ok")
        os.remove(test)
        checks["data_dir_writable"] = True
    except Exception as e:
        checks["data_dir_writable"] = False
        checks["data_dir_error"] = str(e)[:120]
    checks["data_dir"] = config.DATA_DIR
    try:
        checks["bcrypt_ok"] = verify_password("x", hash_password("x"))
    except Exception as e:
        checks["bcrypt_ok"] = False
        checks["bcrypt_error"] = str(e)[:120]
    checks["session_secret_set"] = bool(getattr(config, "SESSION_SECRET", "") or os.environ.get("SESSION_SECRET"))
    checks["admin_session_secret_set"] = bool(getattr(config, "ADMIN_SESSION_SECRET", ""))
    return checks


@app.post("/api/admin/logout")
def admin_logout(response: Response):
    """Clear the admin session cookie."""
    clear_admin_cookie(response)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok", "message": "Logged out."}


@app.get("/api/auth/me")
def auth_me(request: Request):
    """Who is signed in: role, gym and active tier/plan (used by setup, leads and the admin console)."""
    sess = require_session(request)
    if sess.get("role") == "owner":
        acct = _get_account(sess.get("gym_id") or "")
        if not acct:
            raise HTTPException(401, "Account no longer exists.")
        return {"role": "owner", "username": acct["username"], "gym_id": acct["gym_id"],
                "gym_name": acct.get("gym_name"), "tier": acct.get("tier", "free"),
                "plan_start": acct.get("plan_start"), "plan_end": acct.get("plan_end"),
                "status": acct.get("status", "active"), "max_branches": BRANCH_LIMITS.get(acct.get("tier", "free"), 1),
                "site_url": _site_url(acct["gym_id"], acct), **_plan_state(acct)}
    return {"role": "superadmin", "username": sess.get("username", "admin"), "site_base_domain": SITE_BASE_DOMAIN}


class ChangePasswordPayload(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


@app.post("/api/auth/change-password")
def owner_change_password(payload: ChangePasswordPayload, request: Request):
    """Gym owners change their own password."""
    sess = require_session(request)
    if sess.get("role") != "owner":
        raise HTTPException(400, "The platform admin password is changed from the admin settings.")
    with _accounts_lock:
        accounts = _load_accounts()
        acct = accounts.get(sess.get("gym_id") or "")
        if not acct or not verify_password(payload.current_password, acct.get("password_hash", "")):
            raise HTTPException(401, "Current password is incorrect.")
        acct["password_hash"] = hash_password(payload.new_password)
        acct["updated_at"] = int(time.time())
        _save_accounts(accounts)
    return {"status": "ok"}


# ---------------------------------------------------- Gym AI Setup (superadmin) ---
class GymBranchIn(BaseModel):
    id: Optional[str] = None
    name: str = ""
    city: str = ""
    full_address: str = ""
    landmark: str = ""
    phone: str = ""
    whatsapp: str = ""
    google_maps_url: str = ""
    opening_hours: str = ""
    is_primary: bool = False


class GymAccountIn(BaseModel):
    gym_id: str
    gym_name: str = Field(..., min_length=2, max_length=100)
    owner_name: str = ""
    owner_phone: str = ""
    owner_email: str = ""
    address: str = ""
    city: str = ""
    gst_number: str = ""
    tier: str = "free"
    plan_start: str = ""
    plan_end: str = ""
    status: str = "active"
    notes: str = ""
    username: str = Field(..., min_length=3, max_length=40)
    password: Optional[str] = None      # required on create, optional on update
    branches: list[GymBranchIn] = []
    subdomain: str = ""                 # <subdomain>.arivayyaai.com; defaults to gym_id
    custom_domain: str = ""             # optional own domain, e.g. tarvosfit.com


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-@]{3,40}$")


def _validate_account_input(data: GymAccountIn, gym_id: str, creating: bool) -> None:
    if data.tier not in VALID_TIERS:
        raise HTTPException(400, f"Tier must be one of {', '.join(VALID_TIERS)}.")
    if data.status not in ("active", "suspended"):
        raise HTTPException(400, "Status must be active or suspended.")
    if not _USERNAME_RE.match(data.username) or data.username.lower() == "admin":
        raise HTTPException(400, "Username: 3-40 letters, numbers, . _ - @ (and not 'admin').")
    other = _find_account_by_username(data.username)
    if other and other["gym_id"] != gym_id:
        raise HTTPException(409, f"Username '{data.username}' is already used by another gym.")
    for label, val in (("Plan start", data.plan_start), ("Plan end", data.plan_end)):
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, f"{label} must be a date (YYYY-MM-DD).")
    if data.plan_start and data.plan_end and data.plan_end < data.plan_start:
        raise HTTPException(400, "Plan end date must be after the start date.")
    if creating and not (data.password and len(data.password) >= 8):
        raise HTTPException(400, "Set a password of at least 8 characters.")
    if data.password and len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    # Website address
    sub = (data.subdomain or gym_id).strip().lower()
    if not _SUBDOMAIN_RE.match(sub):
        raise HTTPException(400, "Website address: use lowercase letters, numbers and hyphens (not at the start or end).")
    if sub in RESERVED_SUBDOMAINS:
        raise HTTPException(400, f"'{sub}' is reserved. Choose another website address.")
    for other in _load_accounts().values():
        if other.get("gym_id") != gym_id and _account_subdomain(other) == sub:
            raise HTTPException(409, f"{sub}.{SITE_BASE_DOMAIN} is already used by {other.get('gym_name') or other.get('gym_id')}.")
    data.subdomain = sub
    cd = (data.custom_domain or "").strip().lower()
    cd = re.sub(r"^https?://", "", cd).split("/")[0]
    if cd.startswith("www."):
        cd = cd[4:]
    if cd:
        if not _DOMAIN_RE.match(cd):
            raise HTTPException(400, "Custom domain looks invalid. Example: tarvosfit.com")
        if cd == SITE_BASE_DOMAIN or cd.endswith("." + SITE_BASE_DOMAIN):
            raise HTTPException(400, f"Use the website address field for {SITE_BASE_DOMAIN} addresses.")
        for other in _load_accounts().values():
            if other.get("gym_id") != gym_id and (other.get("custom_domain") or "").lower() == cd:
                raise HTTPException(409, f"{cd} is already linked to another gym.")
    data.custom_domain = cd

    named = [b for b in data.branches if b.name.strip()]
    if len(named) > BRANCH_LIMITS[data.tier]:
        raise HTTPException(400, f"The {data.tier.title()} plan allows {BRANCH_LIMITS[data.tier]} branch(es); {len(named)} were given.")


def _store_account(gym_id: str, data: GymAccountIn, creating: bool) -> dict:
    with _accounts_lock:
        accounts = _load_accounts()
        if creating and gym_id in accounts:
            raise HTTPException(409, f"Gym id '{gym_id}' already exists.")
        if not creating and gym_id not in accounts:
            raise HTTPException(404, f"Gym '{gym_id}' not found.")
        acct = accounts.get(gym_id, {"gym_id": gym_id, "created_at": int(time.time())})
        fields = data.model_dump(exclude={"password", "branches", "gym_id"})
        acct.update(fields)
        if data.password:
            acct["password_hash"] = hash_password(data.password)
        acct["updated_at"] = int(time.time())
        accounts[gym_id] = acct
        _save_accounts(accounts)

    if data.branches:
        _save_branches(gym_id, _normalize_branches([b.model_dump() for b in data.branches]))
    _apply_tier_to_gym_files(gym_id, data.tier, {
        "gym_name": data.gym_name, "brand_name": data.gym_name, "city": data.city,
        "full_address": data.address, "primary_phone": data.owner_phone, "email": data.owner_email,
    })
    return _public_account(acct)


@app.get("/api/admin/gyms")
def list_gym_accounts(_auth: dict = Depends(require_super_admin)):
    accounts = _load_accounts()
    return sorted((_public_account(a) for a in accounts.values()), key=lambda a: a.get("gym_name", "").lower())


@app.get("/api/admin/gyms/{gym_id}")
def get_gym_account(gym_id: str, _auth: dict = Depends(require_super_admin)):
    gym_id = _validate_gym_id(gym_id)
    acct = _get_account(gym_id)
    if not acct:
        raise HTTPException(404, f"Gym '{gym_id}' not found.")
    return {**_public_account(acct), "branches": _load_all_branches(gym_id)}


@app.post("/api/admin/gyms")
def create_gym_account(data: GymAccountIn, _auth: dict = Depends(require_super_admin)):
    gym_id = _validate_gym_id(data.gym_id.strip().lower())
    _validate_account_input(data, gym_id, creating=True)
    return _store_account(gym_id, data, creating=True)


@app.put("/api/admin/gyms/{gym_id}")
def update_gym_account(gym_id: str, data: GymAccountIn, _auth: dict = Depends(require_super_admin)):
    gym_id = _validate_gym_id(gym_id)
    if data.gym_id and data.gym_id != gym_id:
        raise HTTPException(400, "gym_id cannot be changed.")
    _validate_account_input(data, gym_id, creating=False)
    return _store_account(gym_id, data, creating=False)


@app.delete("/api/admin/gyms/{gym_id}")
def delete_gym_account(
    gym_id: str,
    purge: bool = Query(False, description="Also permanently delete the gym's website, knowledge base, branches and leads"),
    confirm: str = Query("", description="Must equal the gym_id when purge=true"),
    _auth: dict = Depends(require_super_admin),
):
    """Delete a gym.
    purge=false: removes the plan record and owner login; website, chat knowledge and leads stay on disk.
    purge=true:  also deletes every file for the gym, its knowledge base, leads and chat history."""
    gym_id = _validate_gym_id(gym_id)
    if purge:
        if confirm.strip().lower() != gym_id:
            raise HTTPException(400, "To delete everything, type the gym ID exactly to confirm.")
        if gym_id == config.DEFAULT_GYM_ID:
            raise HTTPException(400, "The default gym's data can't be permanently deleted. Change DEFAULT_GYM_ID first.")

    with _accounts_lock:
        accounts = _load_accounts()
        had_account = accounts.pop(gym_id, None) is not None
        if had_account:
            _save_accounts(accounts)
    if not had_account and not purge:
        raise HTTPException(404, f"Gym '{gym_id}' not found.")

    removed = {"account": had_account}
    if purge:
        # leads
        try:
            removed["leads"] = leads_manager.clear_all_leads(gym_id=gym_id)
        except Exception as e:
            logger.error(f"Delete {gym_id}: leads: {e}")
        # chat knowledge (vector store)
        try:
            store = get_store(gym_id)
            ids = list(store.all_ids())
            if ids:
                store.replace_ids(ids)
            removed["knowledge_chunks"] = len(ids)
        except Exception as e:
            logger.error(f"Delete {gym_id}: knowledge base: {e}")
        # every file / folder named <gym_id>.* (config, identity, site, branches, lead_branches, pdf, …)
        files = 0
        for name in os.listdir(config.DATA_DIR):
            if name.startswith(gym_id + ".") or name == gym_id:
                path = os.path.join(config.DATA_DIR, name)
                try:
                    if os.path.isdir(path):
                        import shutil
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    files += 1
                except Exception as e:
                    logger.error(f"Delete {gym_id}: {name}: {e}")
        removed["files"] = files
        # chat history lines
        events = os.path.join(config.DATA_DIR, "chat_events.jsonl")
        if os.path.exists(events):
            try:
                with open(events, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                keep = []
                for line in lines:
                    try:
                        if json.loads(line).get("gym_id") == gym_id:
                            continue
                    except Exception:
                        pass
                    keep.append(line)
                with open(events, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                removed["chat_events"] = len(lines) - len(keep)
            except Exception as e:
                logger.error(f"Delete {gym_id}: chat events: {e}")
        # WhatsApp number mapping
        wa_map = os.path.join(config.DATA_DIR, "whatsapp_gym_map.json")
        if os.path.exists(wa_map):
            try:
                with open(wa_map, "r", encoding="utf-8") as f:
                    mapping = json.load(f)
                mapping = {k: v for k, v in mapping.items() if v != gym_id}
                with open(wa_map, "w", encoding="utf-8") as f:
                    json.dump(mapping, f, indent=2)
            except Exception as e:
                logger.error(f"Delete {gym_id}: whatsapp map: {e}")
    return {"status": "ok", "gym_id": gym_id, "purged": purge, "removed": removed}


class TestWhatsAppIn(BaseModel):
    to: str = Field(..., min_length=6, max_length=20)
    gym_name: str = ""


@app.post("/api/admin/test-whatsapp")
def test_whatsapp(payload: TestWhatsAppIn, _auth: dict = Depends(require_super_admin)):
    """Send a sample lead alert to a number and return Meta's answer, so setup problems show up immediately."""
    token_set = bool(getattr(config, "WHATSAPP_TOKEN", None) or os.environ.get("WHATSAPP_TOKEN"))
    phone_id_set = bool(getattr(config, "WHATSAPP_PHONE_NUMBER_ID", None) or os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))
    template = getattr(config, "WHATSAPP_LEAD_ALERT_TEMPLATE", None) or os.environ.get("WHATSAPP_LEAD_ALERT_TEMPLATE", "")
    gym_name = payload.gym_name.strip() or "Your gym"
    text = (f"✅ Test alert from Gym AI Enquiry Assistant\n\n🏢 Gym: {gym_name}\n👤 Name: Test Visitor\n"
            f"📞 Phone: 90000 00000\n🎯 Interest: Free trial\n\nNew lead alerts will arrive like this.")
    ok, info = leads_manager.send_whatsapp(
        payload.to, text, template=template,
        template_params=[gym_name, "Test Visitor", "9000000000", "Free trial", "Test message"],
    )
    hints = {
        "whatsapp_not_configured": "Set WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID on Render, then redeploy.",
        "invalid_number": "Enter the number with country code, e.g. 91 98765 43210.",
    }
    code = str(info).split(":")[0].strip()
    meta_hints = {
        "190": "The access token has expired or is invalid. Create a permanent System User token in Meta Business Settings.",
        "131030": "This number isn't allowed yet. In test mode, add it as a test recipient in Meta's API Setup, or take the app live.",
        "132001": "The template name or language doesn't match an approved template. Check WHATSAPP_LEAD_ALERT_TEMPLATE and WHATSAPP_TEMPLATE_LANG.",
        "132000": "The template's number of variables doesn't match. new_lead_alert needs 5 variables ({{1}}…{{5}}).",
        "131047": "More than 24 hours since this number messaged you. Use an approved template (WHATSAPP_LEAD_ALERT_TEMPLATE).",
        "131026": "The message couldn't be delivered — the number may not be on WhatsApp.",
        "100": "Invalid request — check WHATSAPP_PHONE_NUMBER_ID.",
    }
    hint = hints.get(info) or meta_hints.get(code) or ""
    if ok and not template:
        hint = ("Accepted by Meta. Without an approved template, it only arrives if this number messaged your "
                "WhatsApp business number in the last 24 hours.")
    return {
        "ok": ok, "result": info, "hint": hint,
        "mode": "template" if template else "text",
        "template": template or None,
        "config": {"token": token_set, "phone_number_id": phone_id_set, "alert_template": bool(template)},
    }


# --------------------------------------------------------- Privacy & retention ---
def _privacy_html(gym_id: str) -> HTMLResponse:
    ident = _gym_identity(gym_id)
    acct = _get_account(gym_id) or {}
    name = ident.get("gym_name") or acct.get("gym_name") or gym_id.replace("-", " ").title()
    contact = " / ".join(x for x in [ident.get("email") or acct.get("owner_email"),
                                      ident.get("primary_phone") or acct.get("owner_phone")] if x) or "the gym's front desk"
    days = leads_manager.retention_days() if hasattr(leads_manager, "retention_days") else 90
    months = max(1, round(days / 30))
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy notice — {esc(name)}</title>
<style>body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f5f7ff;color:#0e1b3d;line-height:1.65}}
main{{max-width:720px;margin:0 auto;padding:40px 22px 60px}}h1{{font-size:28px;margin:0 0 6px}}h2{{font-size:18px;margin:26px 0 6px}}
p,li{{font-size:16.5px;color:#33405f}}.card{{background:#fff;border:1px solid #dce3f7;border-radius:14px;padding:26px 28px}}
.muted{{color:#4a5578;font-size:14px}}a{{color:#2457f5}}</style></head><body><main><div class="card">
<h1>Privacy notice</h1><p class="muted">{esc(name)}</p>
<h2>What we collect</h2><p>Your name and phone number, and anything you choose to share: email, fitness goal, preferred plan,
time slot, branch or referral code. We also keep your chat messages with our assistant.</p>
<h2>Why</h2><p>To reply to your enquiry, arrange your free trial and tell you about memberships. If you opted in, we also send you
offers on WhatsApp.</p>
<h2>Who handles it</h2><p>{esc(name)} is responsible for your data. Our website and chat assistant are provided by Arivayya AI,
which stores and processes it on our behalf. Chat questions are answered by an AI model; your phone number, email and similar
personal details are removed before any message reaches it. We don't sell your data.</p>
<h2>How long we keep it</h2><p>Up to {months} month{'s' if months != 1 else ''} after your last contact with us, unless you become a member.</p>
<h2>Your rights</h2><p>You can ask to see, correct or delete your details, or withdraw your consent, at any time. Contact
<strong>{esc(contact)}</strong>. If you're not satisfied, you can complain to the Data Protection Board of India.</p>
<p class="muted">Notice version v1 · Website by <a href="https://www.arivayyaai.com" rel="noopener">Arivayya AI</a></p>
</div></main></body></html>"""
    return HTMLResponse(html)


@app.get("/privacy/{gym_id}", response_class=HTMLResponse)
def privacy_notice(gym_id: str):
    return _privacy_html(_validate_gym_id(gym_id))


@app.get("/privacy", response_class=HTMLResponse)
def privacy_notice_for_host(request: Request, gym: Optional[str] = Query(None)):
    gym_id = gym or _gym_for_host(request.headers.get("host", "")) or config.DEFAULT_GYM_ID
    return _privacy_html(_validate_gym_id(gym_id))


def _retention_loop():
    """Erase leads past the retention period (LEAD_RETENTION_DAYS, default 90) once a day."""
    while True:
        try:
            if hasattr(leads_manager, "purge_expired_leads"):
                leads_manager.purge_expired_leads()
        except Exception as e:
            logger.error(f"Retention purge failed: {e}")
        time.sleep(24 * 3600)


@app.on_event("startup")
def _start_retention_job():
    threading.Thread(target=_retention_loop, daemon=True, name="lead-retention").start()


# ------------------------------------------------------------- Demo data ---
# Four sample gyms, one per tier, each with an owner login and realistic sample leads.
# POST /api/admin/demo-seed (platform admin only). Safe to run again: it resets only
# gym1–gym4 (their accounts, branches and leads). Notifications are muted while seeding.
DEMO_PASSWORD = "gym@123"
DEMO_GYMS = [
    {"gym_id": "gym1", "tier": "free", "gym_name": "Gym 1 Fitness (Free demo)", "city": "Pappanamcode, Trivandrum",
     "address": "Near Pappanamcode Junction, Trivandrum 695018", "owner_name": "Arun Kumar", "leads": 5,
     "branches": [{"id": "main", "name": "Gym 1 – Pappanamcode", "city": "Pappanamcode", "is_primary": True}]},
    {"gym_id": "gym2", "tier": "basic", "gym_name": "Gym 2 Fitness (Basic demo)", "city": "Karamana, Trivandrum",
     "address": "Karamana Main Road, Trivandrum 695002", "owner_name": "Deepa Nair", "leads": 10, "expiring_soon": True,
     "branches": [{"id": "main", "name": "Gym 2 – Karamana", "city": "Karamana", "is_primary": True}]},
    {"gym_id": "gym3", "tier": "pro", "gym_name": "Gym 3 Fitness (Pro demo)", "city": "Kowdiar, Trivandrum",
     "address": "Kowdiar Square, Trivandrum 695003", "owner_name": "Suresh Menon", "leads": 18,
     "branches": [
         {"id": "kowdiar", "name": "Gym 3 – Kowdiar", "city": "Kowdiar", "is_primary": True},
         {"id": "pattom", "name": "Gym 3 – Pattom", "city": "Pattom"},
         {"id": "kazhakootam", "name": "Gym 3 – Kazhakootam", "city": "Kazhakootam"}]},
    {"gym_id": "gym4", "tier": "premium", "gym_name": "Gym 4 Fitness (Premium demo)", "city": "Vazhuthacaud, Trivandrum",
     "address": "Vazhuthacaud, Trivandrum 695014", "owner_name": "Fathima Rasheed", "leads": 30,
     "branches": [
         {"id": "vazhuthacaud", "name": "Gym 4 – Vazhuthacaud", "city": "Vazhuthacaud", "is_primary": True},
         {"id": "technopark", "name": "Gym 4 – Technopark", "city": "Kazhakootam"},
         {"id": "kesavadasapuram", "name": "Gym 4 – Kesavadasapuram", "city": "Kesavadasapuram"},
         {"id": "vellayambalam", "name": "Gym 4 – Vellayambalam", "city": "Vellayambalam"},
         {"id": "sasthamangalam", "name": "Gym 4 – Sasthamangalam", "city": "Sasthamangalam"}]},
]
_DEMO_NAMES = [
    "Arjun Nair", "Anjali Menon", "Rahul Krishnan", "Sneha Pillai", "Vishnu Das", "Fathima Nazar", "Aditya Varma",
    "Meera Suresh", "Nikhil Joseph", "Divya Mohan", "Sreejith Kumar", "Aparna Thomas", "Joel Mathew", "Lakshmi Priya",
    "Hari Shankar", "Ameen Shah", "Keerthana Ravi", "Gokul Raj", "Nimisha George", "Abhinav Sasi", "Revathy Nair",
    "Sanjay Menon", "Athira Babu", "Kiran Varghese", "Neha Rajan", "Faisal Ahmed", "Anu Jacob", "Vivek Chandran",
    "Parvathy Krishna", "Rohit Pillai",
]
_DEMO_GOALS = ["Strengthening", "Muscle Training", "Fat Loss", "Weight Gain"]
_DEMO_PLANS = ["Monthly Plan", "Quarterly Plan", "Half-Yearly Plan", "Yearly Plan", "Personal Training", "Day Pass"]
_DEMO_SLOTS = ["Morning (6 AM - 9 AM)", "Afternoon (12 PM - 4 PM)", "Evening (5 PM - 9 PM)"]
_DEMO_STATUSES = ["New", "New", "Pending", "Contacted", "Trial booked", "Joined", "Completed"]
_DEMO_CHANNELS = ["form", "web", "web", "whatsapp"]


def _demo_lead(tier: str, i: int, branch: dict, rng: random.Random) -> dict:
    name = _DEMO_NAMES[i % len(_DEMO_NAMES)]
    first = name.split()[0].lower()
    lead = {
        "name": name,
        "phone": f"90000{tier_index(tier)}{i:04d}",   # obviously-dummy 10-digit numbers
        "channel": rng.choice(_DEMO_CHANNELS),
        "preferred_time": "",
        "message_parts": [f"Branch: {branch['name']}"],
    }
    if tier == "free":
        lead["interest"] = rng.choice(["Free 1-Day Trial Pass", "Fitness Enquiry"])
    else:
        goal = rng.choice(_DEMO_GOALS)
        interest = goal
        if tier in ("pro", "premium"):
            plan = rng.choice(_DEMO_PLANS)
            slot = rng.choice(_DEMO_SLOTS)
            interest += f" ({plan}) [{slot}]"
            lead["preferred_time"] = slot
            if rng.random() < 0.7:
                lead["message_parts"].append(f"Email: {first}{i}@example.com")
        if tier == "premium" and rng.random() < 0.35:
            lead["message_parts"].append(f"Referral: FIT{rng.randint(100, 999)}")
        lead["interest"] = interest
    return lead


def tier_index(tier: str) -> int:
    return VALID_TIERS.index(tier) + 1


class _MuteNotifications:
    """Temporarily silences owner e-mail / WhatsApp alerts so seeding doesn't send 60 messages."""
    _CONFIG_KEYS = ("OWNER_NOTIFICATION_EMAIL", "SMTP_HOST", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")
    _NAME_HINTS = ("notif", "send", "email", "smtp", "whatsapp", "alert")

    def __enter__(self):
        self.saved_cfg = {k: getattr(config, k) for k in self._CONFIG_KEYS if hasattr(config, k)}
        for k in self.saved_cfg:
            setattr(config, k, "")
        self.saved_fns = {}
        for name in dir(leads_manager):
            if name in ("create_lead", "list_leads", "update_lead", "normalize_phone", "is_valid_phone"):
                continue
            fn = getattr(leads_manager, name, None)
            if callable(fn) and not isinstance(fn, type) and any(h in name.lower() for h in self._NAME_HINTS):
                self.saved_fns[name] = fn
                setattr(leads_manager, name, lambda *a, **k: (True, "muted"))
        return self

    def __exit__(self, *exc):
        for k, v in self.saved_cfg.items():
            setattr(config, k, v)
        for name, fn in self.saved_fns.items():
            setattr(leads_manager, name, fn)
        return False


def _backdate_leads(stamps: dict) -> int:
    """Spread sample leads over the last month (only if leads are stored in leads.jsonl)."""
    path = os.path.join(config.DATA_DIR, "leads.jsonl")
    if not stamps or not os.path.exists(path):
        return 0
    changed = 0
    out_lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            try:
                rec = json.loads(raw) if raw.strip() else None
            except Exception:
                rec = None
            if isinstance(rec, dict) and str(rec.get("id")) in stamps:
                ts = stamps[str(rec["id"])]
                rec["created_at"] = ts
                if "ts" in rec:
                    rec["ts"] = ts
                raw = json.dumps(rec, ensure_ascii=False)
                changed += 1
            out_lines.append(raw)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + ("\n" if out_lines else ""))
    os.replace(tmp, path)
    return changed


@app.post("/api/admin/demo-seed")
def seed_demo_gyms(_auth: dict = Depends(require_super_admin)):
    """Create/reset gym1–gym4 (Free, Basic, Pro, Premium) with owner logins and sample leads."""
    rng = random.Random(42)
    today = date.today()
    summary = []
    now = int(time.time())

    for g in DEMO_GYMS:
        gym_id, tier = g["gym_id"], g["tier"]
        start = today.fromordinal(today.toordinal() - 30)
        end = today.fromordinal(today.toordinal() + (20 if g.get("expiring_soon") else 335))

        # 1. account (password set directly: the demo password is shorter than the 8-character rule)
        with _accounts_lock:
            accounts = _load_accounts()
            clash = _find_account_by_username(gym_id)
            if clash and clash["gym_id"] != gym_id:
                raise HTTPException(409, f"Username '{gym_id}' is already used by gym '{clash['gym_id']}'.")
            acct = accounts.get(gym_id, {"gym_id": gym_id, "created_at": now})
            acct.update({
                "gym_name": g["gym_name"], "owner_name": g["owner_name"], "owner_phone": f"+91 90000 0000{tier_index(tier)}",
                "owner_email": f"{gym_id}@example.com", "address": g["address"], "city": g["city"], "gst_number": "",
                "tier": tier, "plan_start": start.isoformat(), "plan_end": end.isoformat(), "status": "active",
                "notes": "Demo gym — safe to delete.", "username": gym_id,
                "password_hash": hash_password(DEMO_PASSWORD), "updated_at": now, "is_demo": True,
            })
            accounts[gym_id] = acct
            _save_accounts(accounts)

        # 2. branches + tier/identity files
        _save_branches(gym_id, _normalize_branches(g["branches"]))
        _apply_tier_to_gym_files(gym_id, tier, {
            "gym_name": g["gym_name"], "brand_name": g["gym_name"], "city": g["city"],
            "full_address": g["address"], "primary_phone": acct["owner_phone"], "email": acct["owner_email"],
        })

        # 3. leads: clear this demo gym's old leads, then add fresh samples
        try:
            leads_manager.clear_all_leads(gym_id=gym_id)
        except Exception as e:
            logger.error(f"Demo seed: could not clear leads for {gym_id}: {e}")
        stamps = {}
        created = 0
        branches = _active_branches(gym_id)
        with _MuteNotifications():
            for i in range(g["leads"]):
                branch = branches[i % len(branches)]
                d = _demo_lead(tier, i, branch, rng)
                kwargs = dict(gym_id=gym_id, name=d["name"], phone=leads_manager.normalize_phone(d["phone"]),
                              interest=d["interest"], preferred_time=d["preferred_time"], channel=d["channel"],
                              message=" | ".join(d["message_parts"]))
                try:
                    params = inspect.signature(leads_manager.create_lead).parameters
                    if "branch_id" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                        kwargs.update(branch_id=branch["id"], branch_name=branch["name"])
                except (TypeError, ValueError):
                    pass
                lead = leads_manager.create_lead(**kwargs)
                lead_id = _lead_id_of(lead)
                _record_lead_branch(gym_id, lead_id, {"branch_id": branch["id"], "branch_name": branch["name"]})
                # Newest first: lead 0 is today, older ones spread back over ~30 days
                stamps[str(lead_id)] = now - int(i * (30 * 86400 / max(g["leads"], 1))) - rng.randint(0, 3600 * 6)
                status = "New" if (tier == "free" or i < 2) else rng.choice(_DEMO_STATUSES)
                updates = {"status": status}
                if i >= 3:
                    updates["is_read"] = True
                try:
                    if lead_id:
                        leads_manager.update_lead(gym_id=gym_id, lead_id=lead_id, updates=updates)
                except Exception as e:
                    logger.error(f"Demo seed: could not update lead {lead_id}: {e}")
                created += 1
        backdated = _backdate_leads(stamps)
        summary.append({"gym_id": gym_id, "username": gym_id, "tier": tier, "branches": len(branches),
                        "leads": created, "leads_backdated": backdated, "plan_end": end.isoformat()})

    return {"status": "ok", "password": DEMO_PASSWORD, "gyms": summary}


@app.get("/api/schema")
def get_schema():
    """Frontend wizard fetches the 137-question / 12-category schema from here."""
    return QA_SCHEMA


@app.post("/api/gym/{gym_id}/config")
def save_config(gym_id: str, cfg: GymConfig, request: Request, _auth: dict = Depends(require_gym_access),
                raw_body: dict = Depends(_raw_json_body)):
    gym_id = _validate_gym_id(gym_id)
    if cfg.gym_id != gym_id:
        raise HTTPException(400, "gym_id mismatch")

    # 0. branches (sent top-level and inside identity by the setup page)
    raw_branches = raw_body.get("branches")
    if raw_branches is None and isinstance(raw_body.get("identity"), dict):
        raw_branches = raw_body["identity"].get("branches")
    saved_branches = None
    if isinstance(raw_branches, list):
        saved_branches = _normalize_branches(raw_branches)
        _save_branches(gym_id, saved_branches)

    if cfg.theme:
        cfg.identity.theme = cfg.theme

    # 1. persist raw config (source of truth for re-editing in the wizard)
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    previous_cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                previous_cfg = json.load(f)
        except Exception:
            previous_cfg = {}
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg.model_dump_json(indent=2))

    # Tier & quota rules: owners can't change them; a superadmin tier change updates the account.
    acct = _get_account(gym_id)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        if _auth.get("role") == "owner":
            written["tier"] = (acct or {}).get("tier") or previous_cfg.get("tier") or "free"
            if "tier_limits_override" in previous_cfg or "tier_limits_override" in written:
                written["tier_limits_override"] = previous_cfg.get("tier_limits_override") or {}
        else:
            sent_tier = str(raw_body.get("tier") or written.get("tier") or "").lower()
            if sent_tier in VALID_TIERS:
                written["tier"] = sent_tier
                if acct and acct.get("tier") != sent_tier:
                    with _accounts_lock:
                        accounts = _load_accounts()
                        if gym_id in accounts:
                            accounts[gym_id]["tier"] = sent_tier
                            accounts[gym_id]["updated_at"] = int(time.time())
                            _save_accounts(accounts)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(written, f, indent=2, ensure_ascii=False)
        if written.get("tier") in VALID_TIERS:
            _apply_tier_to_gym_files(gym_id, written["tier"])
    except Exception as e:
        logger.error(f"Error applying tier rules for {gym_id}: {e}")

    # Sync identity and theme into identity.json
    ident_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
    try:
        cur_ident = {}
        if os.path.exists(ident_path):
            with open(ident_path, "r", encoding="utf-8") as f:
                cur_ident = json.load(f)
        ident_dump = cfg.identity.model_dump()
        if cfg.theme:
            ident_dump["theme"] = cfg.theme.model_dump()
            logo_data_url = getattr(cfg.theme, "logoDataUrl", None)
            if logo_data_url and not cur_ident.get("logo_url"):
                cur_ident["logo_url"] = logo_data_url
        cur_ident.update(ident_dump)
        if not cur_ident.get("logo_url") and cfg.identity.logo_url:
            cur_ident["logo_url"] = cfg.identity.logo_url
        with open(ident_path, "w", encoding="utf-8") as f:
            json.dump(cur_ident, f, indent=2)
    except Exception as e:
        logger.error(f"Error syncing identity json: {e}")

    # 2. render the review PDF (this becomes the RAG knowledge document)
    pdf_path = os.path.join(config.DATA_DIR, f"{gym_id}.knowledge.pdf")
    resolved = build_pdf(cfg, pdf_path)

    # 3. embed the resolved Q&A pairs directly and merge-upsert into vector store
    chunks = build_rag_chunks(cfg, resolved)
    store = get_store(gym_id)
    store.upsert(chunks)

    # 4. prune stale chunks
    valid_canonical_ids = {f"{gym_id}::{q['id']}" for q in QA_SCHEMA}
    just_upserted_ids = {c["id"] for c in chunks}
    stale_ids = {
        cid for cid in store.all_ids()
        if cid not in valid_canonical_ids
        and cid not in just_upserted_ids
        and "::CUSTOM_" not in cid
    }
    store.replace_ids(stale_ids)

    configured = sum(1 for r in resolved if r["configured"])
    return {
        "status": "ok",
        "gym_id": gym_id,
        "questions_configured": configured,
        "questions_total": len(resolved),
        "custom_questions": len(cfg.custom_qa),
        "pdf_url": f"/api/gym/{gym_id}/knowledge.pdf",
        "chunks_indexed": len(chunks),
        "stale_chunks_removed": len(stale_ids),
        "branches_saved": len(saved_branches) if saved_branches is not None else None,
        "branches_active": len(_active_branches(gym_id)),
        "max_branches": _max_branches(gym_id),
    }


@app.post("/api/gym/{gym_id}/reindex")
def reindex(gym_id: str, request: Request, _auth: dict = Depends(require_gym_access)):
    gym_id = _validate_gym_id(gym_id)
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if not os.path.exists(cfg_path):
        raise HTTPException(404, f"No saved config found for '{gym_id}' — save through wizard first.")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = GymConfig.model_validate_json(f.read())
    return save_config(gym_id, cfg)


@app.post("/api/gym/{gym_id}/ingest-pdf")
async def ingest_pdf(gym_id: str, request: Request, file: UploadFile = File(...), _auth: dict = Depends(require_gym_access)):
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only valid .pdf files are accepted.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large. Maximum PDF size is 10MB.")

    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(400, "Invalid PDF structure.")

    try:
        entries = parse_knowledge_pdf(pdf_bytes)
    except Exception as e:
        raise HTTPException(400, "Invalid or corrupted PDF file. Please upload a valid PDF.")
    if not entries:
        raise HTTPException(400, "No recognizable Q&A entries found in this PDF")

    # 1. Start from baseline or existing config
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            existing_cfg = GymConfig.model_validate_json(f.read())
        baseline_resolved = resolve_answers(existing_cfg)
        identity_dict = existing_cfg.identity.model_dump()
        custom_qa = existing_cfg.custom_qa
    else:
        baseline_resolved = [
            {"id": q["id"], "category": q["category"], "category_code": q["category_code"],
             "question": q["question"], "intent": q["intent"],
             "answer": "I don't have confirmed information about this yet. Would you like me to connect you with the gym team?",
             "configured": False}
            for q in QA_SCHEMA
        ]
        identity_dict = extract_identity(extract_text(pdf_bytes))
        if not identity_dict.get("gym_name") and gym_id:
            identity_dict["gym_name"] = gym_id.replace("-", " ").title()
        custom_qa = []
        identity_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
        with open(identity_path, "w", encoding="utf-8") as f:
            json.dump(identity_dict, f, indent=2)

    # 2. Overlay parsed PDF entries onto baseline
    by_id = {r["id"]: r for r in baseline_resolved}
    for e in entries:
        if e["configured"] and e["id"] in by_id:
            by_id[e["id"]] = {**by_id[e["id"]], "answer": e["answer"], "configured": True}
    merged_resolved = list(by_id.values())

    # 3. Regenerate review PDF
    pdf_path = os.path.join(config.DATA_DIR, f"{gym_id}.knowledge.pdf")
    render_pdf(identity_dict, merged_resolved, custom_qa, gym_id, pdf_path,
               source_note="PDF import merged with existing configuration")

    # 4. Embed & Index
    configured_resolved = [r for r in merged_resolved if r["configured"]]
    chunks = build_rag_chunks_from_resolved(gym_id, configured_resolved, custom_qa)
    store = get_store(gym_id)
    store.upsert(chunks)

    valid_canonical_ids = {f"{gym_id}::{q['id']}" for q in QA_SCHEMA}
    just_upserted_ids = {c["id"] for c in chunks}
    stale_ids = {
        cid for cid in store.all_ids()
        if cid not in valid_canonical_ids and cid not in just_upserted_ids and "::CUSTOM_" not in cid
    }
    store.replace_ids(stale_ids)

    configured = len(configured_resolved)
    skipped = [e["id"] for e in entries if not e["configured"]]
    return {
        "status": "ok",
        "gym_id": gym_id,
        "questions_found": len(entries),
        "questions_indexed": configured,
        "questions_total": len(merged_resolved),
        "questions_skipped_unconfigured": skipped,
        "chunks_indexed": len(chunks),
        "stale_chunks_removed": len(stale_ids),
        "entries": [{"id": e["id"], "answer": e["answer"], "configured": e["configured"]} for e in entries],
    }


@app.get("/api/gym/{gym_id}/knowledge.pdf")
def get_knowledge_pdf(gym_id: str):
    gym_id = _validate_gym_id(gym_id)
    path = os.path.join(config.DATA_DIR, f"{gym_id}.knowledge.pdf")
    if not os.path.exists(path):
        raise HTTPException(404, "Not generated yet — save the config first")
    return FileResponse(path, media_type="application/pdf", filename=f"{gym_id}_knowledge_base.pdf")


@app.get("/api/gym/{gym_id}/config")
def get_config(gym_id: str, _auth: dict = Depends(require_gym_access)):
    """Returns saved wizard configuration (identity, answers, custom QAs) for this gym."""
    gym_id = _validate_gym_id(gym_id)
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged_ident = _gym_identity(gym_id)
            if not data.get("identity"):
                data["identity"] = merged_ident
            else:
                for k, v in merged_ident.items():
                    if not data["identity"].get(k):
                        data["identity"][k] = v
            all_branches = _load_all_branches(gym_id)
            if all_branches:
                data["branches"] = all_branches          # full list for the setup page
                data["identity"]["branches"] = all_branches
            data["max_branches"] = _max_branches(gym_id)
            return data
    identity = _gym_identity(gym_id)
    return {
        "gym_id": gym_id,
        "identity": identity,
        "answers": [],
        "custom_qa": [],
        "branches": _load_all_branches(gym_id),
        "max_branches": _max_branches(gym_id),
    }


def _gym_identity(gym_id: str) -> dict:
    """Identity + the branches that are live for the gym's tier (used by /info, chat and the site)."""
    ident = _gym_identity_base(gym_id)
    try:
        ident["branches"] = [_public_branch(b) for b in _active_branches(gym_id)]
        ident["max_branches"] = _max_branches(gym_id)
    except Exception as e:
        logger.error(f"Error loading branches for {gym_id}: {e}")
    return ident


def _gym_identity_base(gym_id: str) -> dict:
    gym_id = _validate_gym_id(gym_id)
    ident = {}
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
                cfg_ident = cfg_data.get("identity", {})
                for k, v in cfg_ident.items():
                    if v is not None and v != "":
                        ident[k] = v
                if cfg_data.get("theme"):
                    ident["theme"] = cfg_data["theme"]
                if cfg_data.get("sections"):
                    ident["sections"] = cfg_data["sections"]
                if cfg_data.get("google"):
                    ident["google"] = cfg_data["google"]
                if cfg_data.get("instagram"):
                    ident["instagram"] = cfg_data["instagram"]
        except Exception:
            pass

    identity_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
    if os.path.exists(identity_path):
        try:
            with open(identity_path, "r", encoding="utf-8") as f:
                saved_ident = json.load(f)
                if isinstance(saved_ident.get("theme"), dict):
                    if not isinstance(ident.get("theme"), dict):
                        ident["theme"] = {}
                    ident["theme"].update(saved_ident["theme"])
                for k, v in saved_ident.items():
                    if k != "theme":
                        if v is not None and v != "":
                            ident[k] = v
        except Exception:
            pass

    # Ensure logo_url is preserved from theme if not set directly in identity
    if not ident.get("logo_url") and isinstance(ident.get("theme"), dict) and ident["theme"].get("logoDataUrl"):
        ident["logo_url"] = ident["theme"]["logoDataUrl"]

    if not ident.get("gym_name"):
        ident["gym_name"] = "Tarvos Fit" if "tarvos" in gym_id.lower() else gym_id.replace("-", " ").title()

    if not ident.get("website"):
        ident["website"] = f"https://{config.APP_DOMAIN}"

    if not ident.get("tier"):
        from .schemas import _load_gym_tier
        ident["tier"] = _load_gym_tier(gym_id)

    # Default Theme
    if "theme" not in ident or not ident["theme"]:
        ident["theme"] = {
            "primary_color": "#16a34a",
            "secondary_color": "#0f172a",
            "accent_color": "#16a34a",
            "background_color": "#ffffff",
            "text_color": "#0f172a",
            "button_color": "#16a34a",
            "chatbot_header_color": "#0f172a",
            "user_msg_color": "#16a34a",
            "bot_msg_color": "#f1f5f9",
            "font_family": "Inter",
            "preset_name": "emerald",
        }

    # Default Sections
    if "sections" not in ident or not ident["sections"]:
        ident["sections"] = {
            "enabled_sections": list(DEFAULT_SECTIONS),
            "section_order": list(DEFAULT_SECTIONS),
        }

    # Default Google & Instagram integrations
    if "google" not in ident or not ident["google"]:
        ident["google"] = {
            "place_id": None,
            "public_review_url": ident.get("google_maps_url"),
            "rating": 4.9,
            "user_ratings_total": 240,
            "cached_reviews": [],
            "last_synced_at": None,
        }

    if "instagram" not in ident or not ident["instagram"]:
        ident["instagram"] = {
            "instagram_username": (ident.get("instagram_url") or "").strip("/").split("/")[-1] if ident.get("instagram_url") else "",
            "instagram_url": ident.get("instagram_url"),
            "cached_media": [],
            "last_synced_at": None,
        }

    return ident


def _gym_name(gym_id: str) -> str:
    name = _gym_identity(gym_id).get("gym_name")
    if name:
        return name
    return gym_id.replace("-", " ").title()


@app.get("/api/gym/{gym_id}/info")
def get_gym_info(gym_id: str):
    """Public identity info including unified theme, logo, integration status, effective tier limits, and real-time usage stats."""
    gym_id = _validate_gym_id(gym_id)
    identity = dict(_gym_identity(gym_id))
    from .schemas import get_tier_limits, get_gym_usage_stats
    tier_limits = get_tier_limits(gym_id)
    usage_stats = get_gym_usage_stats(gym_id)

    identity["tier"] = tier_limits.get("tier", identity.get("tier", "free"))
    identity["tier_limits"] = tier_limits
    identity["usage_stats"] = usage_stats
    identity["max_monthly_leads"] = tier_limits.get("max_monthly_leads", 5)
    identity["max_monthly_chats"] = tier_limits.get("max_monthly_chats", 25)
    identity["analytics_level"] = tier_limits.get("analytics_level", "basic_counts")
    identity["analytics_name"] = tier_limits.get("analytics_name", "Basic chat and lead counts")
    identity["trial_form_fields"] = tier_limits.get("trial_form_fields", "name_phone")
    identity["trial_form_fields_label"] = tier_limits.get("trial_form_fields_label", "Name and phone")
    identity["allowed_templates"] = tier_limits.get("allowed_templates", ["classic-modern"])
    # Include saved template_id: prefer config.json, fall back to identity.json
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    saved_tpl = None
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
                saved_tpl = cfg_data.get("identity", {}).get("template_id") or cfg_data.get("template_id")
        except Exception:
            pass
    if not saved_tpl:
        saved_tpl = identity.get("template_id")
    if saved_tpl and saved_tpl in identity["allowed_templates"]:
        identity["template_id"] = saved_tpl
        identity["current_template"] = saved_tpl
    else:
        identity["template_id"] = identity["allowed_templates"][0]
        identity["current_template"] = identity["allowed_templates"][0]
    return identity


@app.post("/api/chat", response_model=ChatResponse)
def chat(msg: ChatMessage, raw_body: dict = Depends(_raw_json_body)):
    gym_id = _validate_gym_id(msg.gym_id)
    from .schemas import get_gym_usage_stats
    usage = get_gym_usage_stats(gym_id)
    if usage.get("chat_quota_reached"):
        max_c = usage.get("max_monthly_chats", 25)
        cur_c = usage.get("monthly_chats_count", 0)
        return ChatResponse(
            reply=f"🔒 Monthly AI Chat message limit for this gym has been reached ({cur_c}/{max_c}). Please upgrade your plan tier to continue AI responses.",
            session_id=msg.session_id,
            lead_capture=False,
            category="Quota Limit Reached",
        )

    # Sanitize user message to prevent prompt injection
    from .security import sanitize_input
    sanitized_message = sanitize_input(msg.message)
    history = SESSIONS.setdefault(msg.session_id, [])

    # Remember the visitor's branch for this session (chat.html sends it with every message)
    branch = _resolve_branch(gym_id, raw_body.get("branch_id"), raw_body.get("branch_name"))
    if branch["branch_id"]:
        SESSION_BRANCH[msg.session_id] = branch

    # Multi-branch gyms: answer "which branches do you have?" from the saved branch list
    if len(_active_branches(gym_id)) > 1 and _BRANCH_QUESTION_RE.search(msg.message or ""):
        reply = _branch_list_reply(gym_id)
        history.append({"role": "user", "text": msg.message})
        history.append({"role": "model", "text": reply})
        return ChatResponse(reply=reply, session_id=msg.session_id, lead_capture=False, category="Branches")

    result = chat_engine.answer(gym_id, _gym_name(gym_id), sanitized_message, history, session_id=msg.session_id, channel=msg.channel)
    history.append({"role": "user", "text": msg.message})
    history.append({"role": "model", "text": result["reply"]})

    # If the engine captured a lead from the conversation, tag it with the session's branch
    session_branch = SESSION_BRANCH.get(msg.session_id)
    if session_branch and isinstance(result, dict):
        captured = result.get("lead") if isinstance(result.get("lead"), dict) else None
        lead_id = result.get("lead_id") or (_lead_id_of(captured) if captured else None)
        _record_lead_branch(gym_id, lead_id, session_branch)
    return ChatResponse(**result)


# ----------------------------------------------------------- CRM Leads API ---
@app.post("/api/gym/{gym_id}/leads")
def submit_lead(
    gym_id: str,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    interest: Optional[str] = None,
    preferred_time: Optional[str] = "",
    channel: Optional[str] = "web",
    message: Optional[str] = "",
    branch_id: Optional[str] = None,
    branch_name: Optional[str] = None,
    payload: Optional[LeadPayload] = Body(None),
    raw_body: dict = Depends(_raw_json_body),
):
    """Direct lead capture endpoint supporting JSON body and form inputs."""
    gym_id = _validate_gym_id(gym_id)
    final_name = (payload.name if payload and payload.name else name) or "Website Visitor"
    final_phone = payload.phone if payload and payload.phone else (phone or "")
    final_interest = (payload.interest if payload and payload.interest else interest) or "General inquiry"
    final_time = (payload.preferred_time if payload and payload.preferred_time else preferred_time) or ""
    final_channel = (payload.channel if payload and payload.channel else channel) or "web"
    final_message = (payload.message if payload and payload.message else message) or ""

    if not final_phone:
        raise HTTPException(400, "Phone number is required")

    cleaned_phone = leads_manager.normalize_phone(final_phone)
    if not leads_manager.is_valid_phone(cleaned_phone):
        raise HTTPException(400, "Please enter a valid 10-digit contact number.")

    # Branch: from the JSON body (LeadPayload drops unknown fields) or query params,
    # validated against the gym's live branches for its tier.
    branch = _resolve_branch(
        gym_id,
        raw_body.get("branch_id") or branch_id,
        raw_body.get("branch_name") or branch_name,
    )
    if not branch["branch_id"]:
        m = _BRANCH_IN_MESSAGE_RE.search(final_message)  # older clients only put it in the message
        if m:
            branch = _resolve_branch(gym_id, None, m.group(1).strip())
    # No hard rejection when a multi-branch gym gets a lead without a branch: an older
    # published site may not have the branch field yet, and losing a lead is worse.
    # The website form and chat both require the choice; such leads show as "Unassigned".
    if branch["branch_name"] and "Branch:" not in final_message:
        final_message = " | ".join(filter(None, [f"Branch: {branch['branch_name']}", final_message]))

    # Consent (DPDP Act). Current forms always send consent=true; if a client says
    # consent=false we refuse. Old published sites send nothing → stored as "not recorded".
    if "consent" in raw_body and raw_body.get("consent") is not True:
        raise HTTPException(400, "Please tick the consent box so the gym can contact you.")
    consent_given = True if raw_body.get("consent") is True else None

    lead_kwargs = dict(
        gym_id=gym_id,
        name=final_name,
        phone=cleaned_phone,
        interest=final_interest,
        preferred_time=final_time,
        channel=final_channel,
        message=final_message,
    )
    try:
        _lm_params = inspect.signature(leads_manager.create_lead).parameters
        if "consent_given" in _lm_params:
            lead_kwargs.update(
                consent_given=consent_given,
                marketing_opt_in=bool(raw_body.get("marketing_opt_in")) if consent_given else False,
                consent_version=str(raw_body.get("consent_version") or "v1")[:20] if consent_given else "",
            )
    except (TypeError, ValueError):
        pass
    # Pass branch fields straight through if leads_manager.create_lead supports them
    try:
        params = inspect.signature(leads_manager.create_lead).parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        for key in ("branch_id", "branch_name"):
            if accepts_kwargs or key in params:
                lead_kwargs[key] = branch[key]
    except (TypeError, ValueError):
        pass

    lead = leads_manager.create_lead(**lead_kwargs)
    _record_lead_branch(gym_id, _lead_id_of(lead), branch)
    if isinstance(lead, dict):
        lead = {**lead, **{k: v for k, v in branch.items() if v}}
    return lead


@app.get("/api/gym/{gym_id}/leads")
def list_leads(
    gym_id: str,
    request: Request,
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    is_read: Optional[bool] = Query(None),
    limit: int = Query(200),
    branch: Optional[str] = Query(None, description="Filter by branch_id or branch name"),
    _auth: dict = Depends(require_gym_access),
):
    """Returns filtered, tenant-isolated leads list, each tagged with branch_id / branch_name."""
    gym_id = _validate_gym_id(gym_id)
    leads = leads_manager.list_leads(
        gym_id=gym_id,
        status=status,
        search=search,
        is_read=is_read,
        limit=limit,
    )
    if not isinstance(leads, list):
        return leads
    lead_branch_map = _load_lead_branches(gym_id)
    leads = [_attach_branch(gym_id, dict(l) if isinstance(l, dict) else l, lead_branch_map) for l in leads]
    if branch:
        want = branch.strip().lower()
        leads = [l for l in leads if isinstance(l, dict) and want in (
            str(l.get("branch_id") or "").lower(), str(l.get("branch_name") or "").lower())]
    return leads


@app.get("/api/gym/{gym_id}/branches")
def get_branches(gym_id: str):
    """Public list of branches live for the gym's tier (used by chat and the website)."""
    gym_id = _validate_gym_id(gym_id)
    active = _active_branches(gym_id)
    return {
        "gym_id": gym_id,
        "tier": _gym_tier(gym_id),
        "max_branches": _max_branches(gym_id),
        "branches": [_public_branch(b) for b in active],
    }


@app.patch("/api/gym/{gym_id}/leads/{lead_id}")
def update_lead_status(gym_id: str, lead_id: str, payload: LeadUpdatePayload, request: Request, _auth: dict = Depends(require_gym_access)):
    gym_id = _validate_gym_id(gym_id)
    """Updates lead status, read state, interest, or appends follow-up notes."""
    updated = leads_manager.update_lead(
        gym_id=gym_id,
        lead_id=lead_id,
        updates=payload.model_dump(exclude_unset=True),
    )
    if not updated:
        raise HTTPException(404, f"Lead '{lead_id}' not found for gym '{gym_id}'")
    return updated


@app.delete("/api/gym/{gym_id}/leads/{lead_id}")
def delete_lead(gym_id: str, lead_id: str, request: Request, _auth: dict = Depends(require_gym_access)):
    """Permanently deletes a lead from CRM storage."""
    deleted = leads_manager.delete_lead(gym_id=gym_id, lead_id=lead_id)
    if not deleted:
        raise HTTPException(404, f"Lead '{lead_id}' not found for gym '{gym_id}'")
    return {"status": "ok", "deleted_id": lead_id, "gym_id": gym_id}


@app.delete("/api/gym/{gym_id}/leads")
def clear_all_gym_leads(gym_id: str, request: Request, _auth: dict = Depends(require_gym_access)):
    """Bulk clears all leads stored for the tenant gym."""
    count = leads_manager.clear_all_leads(gym_id=gym_id)
    return {"status": "ok", "cleared_count": count, "gym_id": gym_id}



class ThemeSavePayload(BaseModel):
    model_config = {"extra": "allow"}
    theme: Optional[dict] = None
    logo_url: Optional[str] = None
    gym_name: Optional[str] = None
    font_family: Optional[str] = None
    font: Optional[str] = None
    preset_name: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    accent_color: Optional[str] = None
    template_id: Optional[str] = None


@app.post("/api/gym/{gym_id}/theme")
def save_gym_theme(gym_id: str, payload: ThemeSavePayload, request: Request, _auth: dict = Depends(require_gym_access)):
    """Saves theme customization, typography font, and logo URL globally for tenant."""
    from .schemas import get_allowed_templates
    allowed_templates = get_allowed_templates(gym_id)
    identity_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
    ident = _gym_identity(gym_id)
    if not isinstance(ident.get("theme"), dict):
        ident["theme"] = {}

    # Extract theme dict if provided
    if payload.theme and isinstance(payload.theme, dict):
        ident["theme"].update(payload.theme)

    # Extract flat attributes if provided
    extra_fields = payload.model_extra or {}
    for k, v in {**extra_fields, **payload.model_dump(exclude_unset=True)}.items():
        if k in ("preset_name", "primary_color", "secondary_color", "accent_color", "bg_color", "text_color", "font", "font_family"):
            ident["theme"][k] = v
            if k in ("font", "font_family"):
                ident["theme"]["font"] = v
                ident["theme"]["font_family"] = v

    if payload.font_family or payload.font:
        f_val = payload.font_family or payload.font
        ident["theme"]["font"] = f_val
        ident["theme"]["font_family"] = f_val

    if payload.logo_url is not None:
        ident["logo_url"] = payload.logo_url
    if payload.gym_name:
        ident["gym_name"] = payload.gym_name
    if getattr(payload, "template_id", None):
        ident["template_id"] = payload.template_id

    with open(identity_path, "w", encoding="utf-8") as f:
        json.dump(ident, f, indent=2)

    # Also update config.json if present (so _gym_identity picks it up)
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            if not cfg_data.get("identity"):
                cfg_data["identity"] = {}
            cfg_data["identity"]["template_id"] = ident.get("template_id", "")
            if payload.theme:
                cfg_data["theme"] = ident.get("theme", {})
                cfg_data["identity"]["theme"] = ident.get("theme", {})
            if payload.logo_url is not None:
                cfg_data["identity"]["logo_url"] = payload.logo_url
            if payload.gym_name:
                cfg_data["identity"]["gym_name"] = payload.gym_name
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, indent=2)
        except Exception:
            pass

    return {
        "status": "ok",
        "gym_id": gym_id,
        "theme": ident.get("theme", {}),
        "logo_url": ident.get("logo_url", ""),
        "template_id": ident.get("template_id", ""),
        "identity": ident,
        "allowed_templates": allowed_templates if 'allowed_templates' in dir() else ["classic-modern"]
    }


@app.get("/api/gym/{gym_id}/leads/unread-count")
def get_unread_lead_count(gym_id: str, request: Request, _auth: dict = Depends(require_gym_access)):
    """Returns number of unread leads for tenant."""
    return {"gym_id": gym_id, "unread_count": leads_manager.get_unread_count(gym_id)}


@app.post("/api/gym/{gym_id}/test-email")
def test_email_notification(gym_id: str, request: Request, payload: Optional[dict] = Body(None), _auth: dict = Depends(require_gym_access)):
    """Tests email notification dispatch for the gym."""
    ident = _gym_identity(gym_id)
    target_email = (payload or {}).get("email") or ident.get("email") or config.OWNER_NOTIFICATION_EMAIL
    
    if not config.SMTP_HOST:
        return {
            "status": "error",
            "smtp_configured": False,
            "message": "SMTP_HOST environment variable is not configured on Render yet.",
            "target_email": target_email or "not set"
        }
    
    if not target_email:
        return {
            "status": "error",
            "smtp_configured": True,
            "message": "No target recipient email configured. Set contact email in Website Essentials & Gym Profile.",
            "target_email": None
        }

    subject = f"🧪 Test Email Alert — {ident.get('gym_name', 'your gym')}"
    text = "This is a test notification from your Gym AI Assistant."
    html = f"""
    <div style="font-family:sans-serif;padding:20px;border:1px solid #e2e8f0;border-radius:12px;">
      <h2 style="color:#16a34a;">✅ Email Notification Test Successful!</h2>
      <p>Your Gym AI Assistant email alert pipeline is working correctly.</p>
      <p><strong>Gym:</strong> {ident.get('gym_name', 'your gym')}</p>
      <p><strong>Recipient:</strong> {target_email}</p>
    </div>
    """
    ok, msg = leads_manager._send_smtp_email(target_email, subject, text, html)
    return {
        "status": "success" if ok else "failed",
        "smtp_configured": True,
        "email_sent": ok,
        "message": msg,
        "target_email": target_email
    }



# ------------------------------------------------ Verified Integrations API ---
class GoogleSyncPayload(BaseModel):
    place_id: Optional[str] = None

@app.post("/api/gym/{gym_id}/integrations/google/sync")
def sync_google(gym_id: str, request: Request, payload: Optional[GoogleSyncPayload] = Body(None), _auth: dict = Depends(require_gym_access)):
    """Synchronizes verified Google Place rating and authentic reviews."""
    place_id = payload.place_id if payload else None
    return google_reviews.sync_google_reviews(gym_id, place_id=place_id)


class InstagramSyncPayload(BaseModel):
    access_token: Optional[str] = None

@app.post("/api/gym/{gym_id}/integrations/instagram/sync")
def sync_instagram(gym_id: str, request: Request, payload: Optional[InstagramSyncPayload] = Body(None), _auth: dict = Depends(require_gym_access)):
    """Synchronizes Instagram Graph API media feed."""
    access_token = payload.access_token if payload else None
    return instagram.sync_instagram_media(gym_id, access_token=access_token)


# --------------------------------------------- Pre-Publish Validation API ---
class ValidateSitePayload(BaseModel):
    html: str

@app.post("/api/gym/{gym_id}/validate-site")
def validate_website_content(gym_id: str, payload: ValidateSitePayload, request: Request, _auth: dict = Depends(require_gym_access)):
    """Validates generated website for placeholders, dead links, and empty sections."""
    cfg = get_config(gym_id)
    return site_validator.validate_site(payload.html, cfg)


@app.get("/api/gym/{gym_id}/stats")
def get_stats(gym_id: str, request: Request, _auth: dict = Depends(require_gym_access)):
    """Aggregates conversation volume, lead conversion, and top enquiry categories."""
    from collections import Counter, defaultdict

    def _read_gym_jsonl(filename):
        path = os.path.join(config.DATA_DIR, filename)
        rows = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("gym_id") == gym_id or not gym_id:
                            rows.append(rec)
                    except Exception:
                        continue
        return rows

    events = _read_gym_jsonl("chat_events.jsonl")
    leads = _read_gym_jsonl("leads.jsonl")

    now = time.time()
    day = 86400
    sessions = {e["session_id"] for e in events if e.get("session_id")}
    messages_today = sum(1 for e in events if now - e["ts"] < day)
    leads_today = sum(1 for l in leads if now - l.get("created_at", l.get("ts", 0)) < day)

    category_names = {q["category_code"]: q["category"] for q in QA_SCHEMA}
    cat_counter = Counter(category_names.get(e.get("category"), "Membership & Pricing") for e in events)
    top_categories = [{"category": c, "count": n} for c, n in cat_counter.most_common(8)]

    daily = defaultdict(int)
    for e in events:
        d = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        daily[d] += 1
    messages_last_7_days = []
    for i in range(6, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * day))
        messages_last_7_days.append({"date": d, "count": daily.get(d, 0)})

    conversion_rate_pct = round(len(leads) / len(sessions) * 100, 1) if sessions else 0.0

    # Leads per branch (Premium: branch-level statistics)
    lead_branch_map = _load_lead_branches(gym_id)
    branch_counter = Counter()
    for l in leads:
        tagged = _attach_branch(gym_id, dict(l), lead_branch_map)
        branch_counter[tagged.get("branch_name") or "Unassigned"] += 1
    leads_by_branch = [{"branch": b, "count": n} for b, n in branch_counter.most_common()]

    from .schemas import get_gym_usage_stats
    usage_stats = get_gym_usage_stats(gym_id)

    return {
        "total_conversations": len(sessions),
        "total_messages": len(events),
        "messages_today": messages_today,
        "total_leads": len(leads),
        "leads_today": leads_today,
        "conversion_rate_pct": conversion_rate_pct,
        "top_categories": top_categories,
        "messages_last_7_days": messages_last_7_days,
        "leads_by_branch": leads_by_branch,
        "usage_stats": usage_stats,
        "app_domain": config.APP_DOMAIN,
        "chat_subdomain": config.CHAT_SUBDOMAIN,
    }


# ---------------------------------------------------------- WhatsApp -------
app.include_router(whatsapp.router, prefix="/webhook/whatsapp")


@app.get("/health")
def health(request: Request):
    return {
        "status": "ok",
        "domain": config.APP_DOMAIN,
        "chat_subdomain": config.CHAT_SUBDOMAIN,
        "vector_backend": config.VECTOR_BACKEND,
        "chat_model": config.GEMINI_CHAT_MODEL,
    }


class PublishSitePayload(BaseModel):
    html: str
    template_id: Optional[str] = None

@app.post("/api/gym/{gym_id}/publish-site")
def publish_site(gym_id: str, payload: PublishSitePayload, request: Request, _auth: dict = Depends(require_gym_access)):
    """Saves the generated website as the live public website served at / for this domain."""
    from .schemas import get_tier_limits, get_allowed_templates
    tier_info = dict(get_tier_limits(gym_id) or {})
    allowed_templates = get_allowed_templates(gym_id)
    # The gym account (set by the platform admin) is the source of truth for the tier
    acct = _get_account(gym_id)
    if acct and acct.get("tier") in VALID_TIERS:
        tier_info["tier"] = acct["tier"]

    cfg = get_config(gym_id)
    identity = cfg.get("identity", _gym_identity(gym_id))

    # Determine template_id: from payload, saved identity, or fallback
    template_id = "classic-modern"
    if getattr(payload, "template_id", None):
        requested = payload.template_id
        if requested in allowed_templates:
            template_id = requested
        else:
            template_id = allowed_templates[0] if allowed_templates else "classic-modern"
    elif identity.get("template_id"):
        saved = identity["template_id"]
        if saved in allowed_templates:
            template_id = saved
        else:
            template_id = allowed_templates[0] if allowed_templates else "classic-modern"
    else:
        template_id = allowed_templates[0] if allowed_templates else "classic-modern"

    identity["template_id"] = template_id

    # Load template
    template_html = _load_template(template_id)

    # Render sections from config
    section_blocks: dict = {}
    try:
        from .site_renderer import render_all_sections
        section_blocks = render_all_sections(cfg, identity, tier_info)
    except Exception as e:
        print(f"[Publish] Section rendering error: {e}")

    # If payload includes raw HTML, use that (frontend-generated). Otherwise compose from template.
    if payload.html and payload.html.strip():
        html = payload.html
        # Inject tier script into any HTML
        tier_script = f"""<script>
          window.GYM_TIER = {{
            tier: "{tier_info.get('tier', 'free')}",
            max_monthly_leads: {tier_info.get('max_monthly_leads', 10)},
            max_monthly_chats: {tier_info.get('max_monthly_chats', 100)},
            max_sections: {tier_info.get('max_sections', 5)},
            llm_enabled: {str(tier_info.get('llm_enabled', False)).lower()},
            csv_export_enabled: {str(tier_info.get('csv_export_enabled', False)).lower()},
            interactive_map_enabled: {str(tier_info.get('interactive_map_enabled', False)).lower()},
            offers_enabled: {str(tier_info.get('offers_enabled', False)).lower()},
            reels_feed_enabled: {str(tier_info.get('reels_feed_enabled', False)).lower()},
            trial_form_fields: "{tier_info.get('trial_form_fields', 'basic')}",
            allowed_sections: {json.dumps(tier_info.get('allowed_sections', []))}
          }};
        </script>"""
        if "</head>" in html:
            html = html.replace("</head>", tier_script + "</head>", 1)
        elif "<body>" in html:
            html = html.replace("<body>", "<body>" + tier_script, 1)
        else:
            html = tier_script + html
    else:
        # Compose from template + rendered sections
        html = _compose_site(template_html, identity, section_blocks, tier_info)

    site_path = os.path.join(config.DATA_DIR, f"{gym_id}.site.html")
    with open(site_path, "w", encoding="utf-8") as f:
        f.write(html)

    # The shared fallback page (served at / and /site) belongs to the default gym only;
    # other gyms publishing must not overwrite it.
    if gym_id == config.DEFAULT_GYM_ID:
        try:
            public_path = os.path.join(FRONTEND_DIR, "public_site.html")
            with open(public_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"[Publish Site Warning] Could not update static frontend/public_site.html: {e}")

    return {"status": "ok", "url": f"/site/{gym_id}", "site_url": _site_url(gym_id), "gym_id": gym_id, "template_id": template_id,
            "tier": tier_info.get("tier", "free")}


# --------------------------------------------------- Template System ---
TEMPLATES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "templates"))
_REGISTRY: dict | None = None

def _load_template_registry() -> dict:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    path = os.path.join(TEMPLATES_DIR, "template-registry.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            _REGISTRY = json.load(f)
    else:
        _REGISTRY = {"templates": []}
    return _REGISTRY

def _load_template(template_id: str) -> str:
    path = os.path.join(TEMPLATES_DIR, f"{template_id}.html")
    if not os.path.exists(path):
        path = os.path.join(TEMPLATES_DIR, "classic-modern.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def _compose_site(template_html: str, identity: dict, section_blocks: dict, tier_info: dict) -> str:
    html = template_html
    theme = identity.get("theme", {})
    subs = {
        "{{GYM_NAME}}": identity.get("gym_name", ""),
        "{{SHORT_DESCRIPTION}}": identity.get("short_description", "Transform Your Fitness Journey"),
        "{{LOGO_URL}}": identity.get("logo_url", ""),
        "{{PRIMARY_COLOR}}": theme.get("primary_color", "#7c3aed"),
        "{{SECONDARY_COLOR}}": theme.get("secondary_color", "#312e81"),
        "{{ACCENT_COLOR}}": theme.get("accent_color", "#7c3aed"),
        "{{BACKGROUND_COLOR}}": theme.get("background_color", "#ffffff"),
        "{{TEXT_COLOR}}": theme.get("text_color", "#0f172a"),
        "{{BUTTON_COLOR}}": theme.get("button_color", "#7c3aed"),
        "{{FONT_FAMILY}}": theme.get("font_family", "Inter"),
        "{{CHATBOT_HEADER_COLOR}}": theme.get("chatbot_header_color", "#312e81"),
        "{{USER_MSG_BG}}": theme.get("user_msg_color", "#7c3aed"),
        "{{BOT_MSG_BG}}": theme.get("bot_msg_color", "#f1f5f9"),
        "{{OWNER_PHONE}}": identity.get("owner_phone", ""),
        "{{OWNER_EMAIL}}": identity.get("owner_email", ""),
        "{{GYM_ADDRESS}}": identity.get("address", ""),
    }
    for key, value in subs.items():
        html = html.replace(key, value or "")

    # Inject CSS custom properties so templates using CSS vars pick up theme changes
    css_vars = f"""<style id="gym-theme-override">
      :root {{
        --gym-primary: {theme.get('primary_color', '#7c3aed')};
        --gym-accent: {theme.get('accent_color', '#7c3aed')};
        --gym-bg: {theme.get('background_color', '#ffffff')};
        --gym-text: {theme.get('text_color', '#0f172a')};
        --gym-secondary: {theme.get('secondary_color', '#312e81')};
        --gym-chat-header: {theme.get('chatbot_header_color', '#312e81')};
        --gym-user-msg: {theme.get('user_msg_color', '#7c3aed')};
        --gym-bot-msg: {theme.get('bot_msg_color', '#f1f5f9')};
        --gym-font: '{theme.get('font_family', 'Inter')}', sans-serif;
      }}
      /* Override common template CSS variable names */
      :root {{
        --bg: {theme.get('background_color', '#ffffff')};
        --ink: {theme.get('text_color', '#0f172a')};
        --muted: {theme.get('secondary_color', '#312e81')};
        --v: {theme.get('primary_color', '#7c3aed')};
        --a: {theme.get('accent_color', '#7c3aed')};
        --c: {theme.get('accent_color', '#7c3aed')};
        --m: {theme.get('primary_color', '#7c3aed')};
      }}
      body {{ font-family: '{theme.get('font_family', 'Inter')}', sans-serif !important; }}
    </style>"""
    if "</head>" in html:
        html = html.replace("</head>", css_vars + "</head>", 1)
    else:
        html = css_vars + html

    for section_id, section_html in section_blocks.items():
        marker = f"<!-- SECTION:{section_id} -->"
        html = html.replace(marker, section_html)
    gym_data_js = f"<script>window.GYM_DATA = {json.dumps(identity, default=str)};</script>"
    if "{{GYM_DATA_JSON}}" in html:
        html = html.replace("{{GYM_DATA_JSON}}", gym_data_js)
    else:
        html = html.replace("</head>", gym_data_js + "</head>", 1)
    tier_script = f"""<script>
      window.GYM_TIER = {{
        tier: "{tier_info.get('tier', 'free')}",
        max_monthly_leads: {tier_info.get('max_monthly_leads', 10)},
        max_monthly_chats: {tier_info.get('max_monthly_chats', 100)},
        max_sections: {tier_info.get('max_sections', 5)},
        llm_enabled: {str(tier_info.get('llm_enabled', False)).lower()},
        csv_export_enabled: {str(tier_info.get('csv_export_enabled', False)).lower()},
        interactive_map_enabled: {str(tier_info.get('interactive_map_enabled', False)).lower()},
        offers_enabled: {str(tier_info.get('offers_enabled', False)).lower()},
        reels_feed_enabled: {str(tier_info.get('reels_feed_enabled', False)).lower()},
        trial_form_fields: "{tier_info.get('trial_form_fields', 'basic')}",
        allowed_sections: {json.dumps(tier_info.get('allowed_sections', []))}
      }};
    </script>"""
    if "{{TIER_SCRIPT_DATA}}" in html:
        html = html.replace("{{TIER_SCRIPT_DATA}}", tier_script)
    else:
        html = html.replace("</head>", tier_script + "</head>", 1)
    return html

@app.get("/api/templates")
def list_templates():
    """List all available website templates."""
    registry = _load_template_registry()
    return {"templates": registry.get("templates", [])}

@app.get("/api/templates/{template_id}")
def get_template_raw(template_id: str):
    """Fetch raw template HTML for preview/rendering."""
    html = _load_template(template_id)
    if not html:
        raise HTTPException(404, f"Template '{template_id}' not found")
    return HTMLResponse(content=html)


# --------------------------------------------------- Frontend Page Routes ---
# --------------------------------------------------- Frontend Page Routes ---
def _serve_frontend_html(filename: str):
    file_path = os.path.join(FRONTEND_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="text/html")
    raise HTTPException(404, f"File {filename} not found")


@app.get("/", response_class=FileResponse)
def root(request: Request):
    """
    On custom domain (e.g. APP_DOMAIN / tarvos.fit): serves the public generated gym website.
    On admin / direct access: serves the Admin Login page.
    """
    host = request.headers.get("host", "").lower()
    gym_from_host = _gym_for_host(host)
    if gym_from_host:
        return _serve_gym_site(gym_from_host)
    if gym_from_host == "":
        return _simple_page("Gym not found", f"There's no gym website at {host.split(':')[0]}. Check the address and try again.", 404)
    app_domain = (config.APP_DOMAIN or "").lower()
    is_public_domain = (app_domain and app_domain in host and config.CHAT_SUBDOMAIN not in host) or ("tarvos.fit" in host and "chat.tarvos.fit" not in host)
    
    if is_public_domain:
        gym_site = os.path.join(config.DATA_DIR, f"{config.DEFAULT_GYM_ID}.site.html")
        if os.path.exists(gym_site):
            return FileResponse(gym_site, media_type="text/html")
        public_site = os.path.join(FRONTEND_DIR, "public_site.html")
        if os.path.exists(public_site):
            return FileResponse(public_site, media_type="text/html")
    
    return _serve_frontend_html("admin.html")


@app.get("/site", response_class=FileResponse)
@app.get("/website", response_class=FileResponse)
def public_site_page(gym: Optional[str] = Query(None)):
    """Serves the generated public gym website with integrated web chat.
    /site?gym=<gym_id> (or /site/<gym_id>) serves that gym's published site."""
    if gym:
        gym_id = _validate_gym_id(gym.strip().lower())
        own_site = os.path.join(config.DATA_DIR, f"{gym_id}.site.html")
        if os.path.exists(own_site):
            return FileResponse(own_site, media_type="text/html")
        raise HTTPException(404, "This gym's website hasn't been published yet. Open Website Setup and click Validate & Generate Website.")
    gym_site = os.path.join(config.DATA_DIR, f"{config.DEFAULT_GYM_ID}.site.html")
    if os.path.exists(gym_site):
        return FileResponse(gym_site, media_type="text/html")
    public_site = os.path.join(FRONTEND_DIR, "public_site.html")
    if os.path.exists(public_site):
        return FileResponse(public_site, media_type="text/html")
    return _serve_frontend_html("public_site.html")


@app.get("/site/{gym_id}", response_class=FileResponse)
def public_site_for_gym(gym_id: str):
    return public_site_page(gym=gym_id)


@app.get("/leads", response_class=FileResponse)
@app.get("/leads.html", response_class=FileResponse)
def leads_page():
    """Dedicated leads management CRM page."""
    return _serve_frontend_html("leads.html")


@app.get("/admin", response_class=FileResponse)
@app.get("/admin.html", response_class=FileResponse)
def admin_page():
    """Admin login page."""
    return _serve_frontend_html("admin.html")


@app.get("/gyms", response_class=FileResponse)
@app.get("/gyms.html", response_class=FileResponse)
def gyms_console_page():
    """Gym AI Setup console (platform admin): create gyms, plans, branches and owner logins."""
    return _serve_frontend_html("gyms.html")


# ---- SEO Endpoints ----

class SEOPayload(BaseModel):
    model_config = {"extra": "allow"}
    seo_meta_title: Optional[str] = None
    seo_meta_description: Optional[str] = None
    seo_og_title: Optional[str] = None
    seo_og_description: Optional[str] = None
    seo_canonical_url: Optional[str] = None
    seo_robots: Optional[str] = None
    seo_google_verification: Optional[str] = None
    seo_gmb_place_id: Optional[str] = None

@app.get("/api/gym/{gym_id}/sitemap.xml")
def get_sitemap(gym_id: str, request: Request):
    """Auto-generated XML sitemap from enabled sections. Basic tier and above."""
    from .schemas import get_tier_limits
    gym_id = _validate_gym_id(gym_id)
    tier_info = get_tier_limits(gym_id)
    if tier_info.get("tier") in ("free",):
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><urlset></urlset>', media_type="application/xml")

    cfg = get_config(gym_id)
    identity = _gym_identity(gym_id)
    base_url = (identity.get("website") or f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
    sections = cfg.get("sections", {}).get("enabled_sections", [])
    order = cfg.get("sections", {}).get("section_order", sections)

    urls = [f'  <url>\n    <loc>{base_url}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>']

    section_paths = {
        "about": "/about", "membership": "/membership", "trainers": "/trainers",
        "timings": "/timings", "location": "/location", "faq": "/faq",
        "gallery": "/gallery", "programs": "/programs", "facilities": "/facilities",
        "equipment": "/equipment", "health_diet": "/health-diet",
        "nutrition": "/nutrition", "policies": "/policies", "trial_cta": "/trial"
    }
    for sec in order:
        if sec in sections and sec in section_paths:
            urls.append(f'  <url>\n    <loc>{base_url}{section_paths[sec]}</loc>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>')

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>"
    return Response(content=xml, media_type="application/xml")

@app.get("/api/gym/{gym_id}/seo")
def get_seo(gym_id: str):
    """Returns saved SEO config."""
    gym_id = _validate_gym_id(gym_id)
    cfg = get_config(gym_id)
    seo = cfg.get("seo", {})
    return {"seo": seo}

@app.post("/api/gym/{gym_id}/seo")
def save_seo(gym_id: str, payload: SEOPayload, _auth: dict = Depends(require_gym_access)):
    """Saves SEO configuration."""
    gym_id = _validate_gym_id(gym_id)
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
        except Exception:
            cfg_data = {}
    else:
        cfg_data = {}

    seo = {}
    for k in ["seo_meta_title","seo_meta_description","seo_og_title","seo_og_description",
              "seo_canonical_url","seo_robots","seo_google_verification","seo_gmb_place_id"]:
        val = getattr(payload, k, None)
        if val is not None:
            seo[k] = val

    cfg_data["seo"] = seo
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg_data, f, indent=2)
    return {"status": "ok", "seo": seo}


# ---- OTP Endpoints ----

class SendOTPPayload(BaseModel):
    phone: str
    channel: str = "whatsapp"

class VerifyOTPPayload(BaseModel):
    phone: str
    otp: str

_OTP_STORE: dict = {}  # phone -> {code, expiry, channel}

@app.post("/api/gym/{gym_id}/send-otp")
def send_otp(gym_id: str, payload: SendOTPPayload):
    """Sends OTP to phone number via WhatsApp. Pro tier and above."""
    from .schemas import get_tier_limits
    gym_id = _validate_gym_id(gym_id)
    tier_info = get_tier_limits(gym_id)
    if tier_info.get("tier") not in ("pro", "premium"):
        return {"success": False, "message": "OTP requires Pro plan or higher"}

    phone = payload.phone.strip()
    if not phone:
        return {"success": False, "message": "Phone number required"}

    code = str(random.randint(100000, 999999))
    expiry = time.time() + 300  # 5 minutes
    _OTP_STORE[phone] = {"code": code, "expiry": expiry, "channel": payload.channel}

    # Try WhatsApp delivery
    try:
        from .whatsapp import send_whatsapp
        msg = f"Your verification code is {code}. Valid for 5 minutes. — {gym_id.replace('-',' ').title()}"
        send_whatsapp(phone, msg, gym_id=gym_id)
    except Exception:
        pass  # OTP stored even if delivery fails

    return {"success": True, "message": f"OTP sent to {phone[-4:].rjust(len(phone), '*')}", "expires_in": 300}

@app.post("/api/gym/{gym_id}/verify-otp")
def verify_otp(gym_id: str, payload: VerifyOTPPayload):
    """Verifies OTP code."""
    gym_id = _validate_gym_id(gym_id)
    record = _OTP_STORE.get(payload.phone.strip())
    if not record:
        return {"success": False, "message": "No OTP found. Request a new one."}

    if time.time() > record["expiry"]:
        del _OTP_STORE[payload.phone.strip()]
        return {"success": False, "message": "OTP expired. Request a new one."}

    if payload.otp.strip() != record["code"]:
        return {"success": False, "message": "Invalid OTP code"}

    del _OTP_STORE[payload.phone.strip()]
    return {"success": True, "message": "Phone verified successfully"}


@app.get("/setup", response_class=FileResponse)
@app.get("/index.html", response_class=FileResponse)
def setup_page():
    """Admin owner setup wizard (protected by login)."""
    return _serve_frontend_html("index.html")


@app.get("/chat", response_class=FileResponse)
@app.get("/chat.html", response_class=FileResponse)
def chat_page():
    """Standalone visitor chat."""
    return _serve_frontend_html("chat.html")


@app.get("/dashboard", response_class=FileResponse)
@app.get("/owner-dashboard.html", response_class=FileResponse)
def dashboard_page():
    """Owner CRM lead dashboard."""
    return _serve_frontend_html("leads.html")



@app.get("/favicon.ico", response_class=FileResponse)
@app.get("/logo.png", response_class=FileResponse)
@app.get("/1000920458.png", response_class=FileResponse)
def get_favicon():
    logo_file = os.path.join(FRONTEND_DIR, "1000920458.png")
    if os.path.exists(logo_file):
        return FileResponse(logo_file, media_type="image/png")
    raise HTTPException(404, "Logo image not found")


@app.get("/qa_schema.js", response_class=FileResponse)
def get_qa_schema_js():
    file_path = os.path.join(FRONTEND_DIR, "qa_schema.js")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/javascript")
    raise HTTPException(404, "qa_schema.js not found")


@app.get("/theme-sync.js", response_class=FileResponse)
def get_theme_sync_js():
    file_path = os.path.join(FRONTEND_DIR, "theme-sync.js")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/javascript")
    raise HTTPException(404, "theme-sync.js not found")


# Mount static assets directory
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

