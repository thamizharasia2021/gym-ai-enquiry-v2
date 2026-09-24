"""
Dedicated Multi-Tenant Lead Lifecycle and Notification Engine.
Provides complete CRUD, status transitions, follow-up notes, read/unread tracking,
and multi-channel notification dispatch with duplicate prevention.
"""
import json
import logging
import os
import re
import smtplib
import threading
import time
import urllib.parse
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Any
import httpx

from . import config
from .schemas import Lead, LeadNote, LeadStatus, get_tier_limits

logger = logging.getLogger(__name__)
_leads_file_lock = threading.RLock()


def _cfg(name: str, default=""):
    """Setting from config.py, falling back to the environment (Render env vars)."""
    val = getattr(config, name, None)
    if val in (None, ""):
        val = os.environ.get(name, default)
    return val


LEAD_STATUS_VALUES = [
    "New",
    "Contacted",
    "Interested",
    "Trial booked",
    "Converted",
    "Closed",
]


def _get_monthly_lead_count(gym_id: str) -> int:
    """Count leads submitted this calendar month for a gym."""
    now = time.time()
    month_start = time.localtime(now)
    month_start_ts = time.mktime((
        month_start.tm_year, month_start.tm_mon, 1, 0, 0, 0, 0, 0, -1
    ))
    all_leads = _read_all_leads()
    return sum(
        1 for l in all_leads
        if l.get("gym_id") == gym_id and l.get("created_at", 0) >= month_start_ts
    )


def _check_lead_limit(gym_id: str) -> tuple[bool, str]:
    """Check if the gym has exceeded its monthly lead limit.
    Returns (allowed, error_message)."""
    tier_info = get_tier_limits(gym_id)
    max_leads = tier_info.get("max_monthly_leads", 10)
    # 0 means unlimited (only if an admin override sets it; no tier ships unlimited)
    if max_leads > 0:
        current = _get_monthly_lead_count(gym_id)
        if current >= max_leads:
            return False, f"Monthly lead limit reached ({current}/{max_leads}). Please upgrade your plan to continue receiving leads."
    return True, ""


def _leads_file_path() -> str:
    return os.path.join(config.DATA_DIR, "leads.jsonl")


def _read_all_leads() -> list[dict]:
    path = _leads_file_path()
    leads = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    leads.append(json.loads(line))
                except Exception:
                    continue
    return leads


def _write_all_leads(leads: list[dict]):
    path = _leads_file_path()
    tmp = path + ".tmp"
    with _leads_file_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            for lead in leads:
                f.write(json.dumps(lead) + "\n")
        os.replace(tmp, path)


def _persist_lead_fields(gym_id: str, lead_id: str, fields: dict) -> None:
    """Save notification results (delivery status, score, duplicate flag) onto the stored lead."""
    with _leads_file_lock:
        leads = _read_all_leads()
        for l in leads:
            if l.get("id") == lead_id and l.get("gym_id") == gym_id:
                l.update(fields)
                _write_all_leads(leads)
                return


def normalize_phone(phone: str) -> str:
    """Indian numbers → 10 digits (98401 99887). Other countries keep their country code
    as digits (e.g. 447700900123), so they're never cut down to the wrong number."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return raw
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return digits[1:]
    if len(digits) == 13 and digits.startswith("091") and digits[3] in "6789":
        return digits[3:]
    if len(digits) > 10 and digits.startswith("00"):   # 00 international prefix
        return digits[2:]
    return digits


def is_valid_phone(phone: str) -> bool:
    """Validates whether phone number is a valid contact number (7 to 15 digits, checking mobile prefixes)."""
    if not phone:
        return False
    digits = re.sub(r"\D", "", phone)
    if not (7 <= len(digits) <= 15):
        return False
    if len(set(digits)) == 1:
        return False
    if digits == "1234567890":
        return False
    if len(digits) == 10 and digits[0] not in "6789":
        return False
    return True


def create_lead(
    gym_id: str,
    name: str = "Website Visitor",
    phone: str = "",
    interest: str = "General inquiry",
    preferred_time: str = "",
    channel: str = "web",
    message: str = "",
    consent_given: Optional[bool] = None,
    marketing_opt_in: bool = False,
    consent_version: str = "",
    branch_id: str = "",
    branch_name: str = "",
) -> dict:
    """
    Creates a new lead with tenant isolation, sets initial 'New' status,
    marks unread, persists to storage, and dispatches notifications.
    Stores lead even if over-quota, tagging is_over_quota status.
    """
    allowed, limit_error = _check_lead_limit(gym_id)
    clean_phone = normalize_phone(phone)
    lead_id = f"LEAD-{gym_id.replace('-', '')[:4].upper()}-{str(uuid.uuid4())[:6].upper()}"
    now = time.time()

    # Source mapping
    source_map = {
        "web": "Website Chatbot",
        "chat": "Website Chatbot",
        "form": "Website Enquiry Form",
        "trial": "Website Free Trial Form",
        "whatsapp": "WhatsApp Business API",
    }
    source_name = source_map.get(channel.lower(), channel.capitalize())

    lead_record = {
        "id": lead_id,
        "gym_id": gym_id,
        "name": (name or "Website Visitor").strip(),
        "phone": clean_phone,
        "source": source_name,
        "interest": (interest or "General inquiry").strip(),
        "preferred_time": (preferred_time or "").strip(),
        "message": (message or "").strip(),
        "status": "New",
        "is_read": False,
        "is_over_quota": not allowed,
        "created_at": now,
        "updated_at": now,
        "notes": [],
        "notification_sent": False,
        "delivery_status": "pending",
        "delivery_error": None,
        "ts": now,  # legacy compatibility
        "channel": channel,  # legacy compatibility
        # Consent record (DPDP Act): None = collected before consent tracking / old site
        "consent_given": consent_given,
        "consent_at": now if consent_given else None,
        "consent_version": consent_version or ("v1" if consent_given else ""),
        "marketing_opt_in": bool(marketing_opt_in) if consent_given else False,
    }
    if branch_id or branch_name:
        lead_record["branch_id"] = branch_id
        lead_record["branch_name"] = branch_name

    # Duplicate check BEFORE saving, so the new lead doesn't match itself
    tier = get_tier_limits(gym_id).get("tier", "free")
    if tier in ("basic", "pro", "premium") and clean_phone:
        dup_of = _find_recent_duplicate(gym_id, clean_phone)
        if dup_of:
            lead_record["is_duplicate"] = True
            lead_record["duplicate_of"] = dup_of
            lead_record["duplicate_note"] = "Same phone number within the last 7 days"

    # Append to leads file
    path = _leads_file_path()
    with _leads_file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead_record) + "\n")

    # Notifications run in the background so the visitor's form submits instantly.
    # Results are written back onto the stored lead (delivery_status etc.).
    threading.Thread(target=_notify_and_persist, args=(dict(lead_record),), daemon=True).start()
    return lead_record


def _notify_and_persist(lead: dict) -> None:
    before = set(lead.keys())
    try:
        dispatch_notifications(lead)
    except Exception as e:
        logger.exception("Lead notification failed")
        lead["delivery_status"] = "failed"
        lead["delivery_error"] = str(e)[:300]
    fields = {k: v for k, v in lead.items() if k not in before or k in (
        "notification_sent", "delivery_status", "delivery_error", "lead_score", "lead_priority")}
    try:
        _persist_lead_fields(lead["gym_id"], lead["id"], fields)
    except Exception:
        logger.exception("Could not save notification results")


def list_leads(
    gym_id: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 200,
) -> list[dict]:
    """Retrieves tenant-isolated leads with dynamic tier quota locking & filtering."""
    all_leads = _read_all_leads()
    gym_leads = [l for l in all_leads if not gym_id or l.get("gym_id") == gym_id]

    tier_limits = get_tier_limits(gym_id)
    max_leads = tier_limits.get("max_monthly_leads", 5)

    now = time.time()
    month_start_tuple = time.localtime(now)
    month_start_ts = time.mktime((
        month_start_tuple.tm_year, month_start_tuple.tm_mon, 1, 0, 0, 0, 0, 0, -1
    ))

    # Identify current month leads in file insertion order to determine order index
    current_month_leads = [l for l in gym_leads if l.get("created_at", l.get("ts", 0)) >= month_start_ts]

    locked_ids = set()
    if max_leads > 0:
        for idx, l in enumerate(current_month_leads, start=1):
            if idx > max_leads:
                locked_ids.add(l.get("id"))

    # Apply masking to over-quota leads — use copies to avoid mutating storage
    # Build a paired list so filters can reference original (unmasked) data for search
    processed = []
    for l in gym_leads:
        lead_copy = dict(l)
        lead_id = l.get("id")
        if lead_id in locked_ids:
            lead_copy["is_locked"] = True
            orig_name = l.get("name", "Prospect")
            orig_phone = str(l.get("phone", ""))
            masked_name = f"{orig_name[0]}***" if orig_name else "P***"
            masked_phone = f"{orig_phone[:2]}****{orig_phone[-2:]}" if len(orig_phone) >= 8 else "**********"
            lead_copy["name"] = f"🔒 {masked_name} (Over Quota)"
            lead_copy["phone"] = f"🔒 {masked_phone}"
            lead_copy["interest"] = "🔒 Details Locked (Upgrade Tier)"
            lead_copy["message"] = "🔒 Content Hidden (Upgrade Plan to View)"
            lead_copy["preferred_time"] = "🔒 Hidden"
            lead_copy["_orig_name"] = orig_name
            lead_copy["_orig_phone"] = l.get("phone", "")
            lead_copy["_orig_interest"] = l.get("interest", "")
            lead_copy["_orig_source"] = l.get("source", "")
            lead_copy["_orig_id"] = l.get("id", "")
        else:
            lead_copy["is_locked"] = False
            lead_copy["_orig_name"] = l.get("name", "")
            lead_copy["_orig_phone"] = l.get("phone", "")
            lead_copy["_orig_interest"] = l.get("interest", "")
            lead_copy["_orig_source"] = l.get("source", "")
            lead_copy["_orig_id"] = l.get("id", "")
        processed.append(lead_copy)

    # Apply status, search & read filters — search uses original (unmasked) values
    results = []
    search_term = search.strip().lower() if search else None
    target_status = status.strip() if status else None

    for lead in processed:
        if target_status and target_status.lower() != "all" and lead.get("status", "").lower() != target_status.lower():
            continue

        if is_read is not None and lead.get("is_read", False) != is_read:
            continue

        if search_term:
            name_m = search_term in str(lead.get("_orig_name", "")).lower()
            phone_m = search_term in str(lead.get("_orig_phone", "")).lower()
            int_m = search_term in str(lead.get("_orig_interest", "")).lower()
            src_m = search_term in str(lead.get("_orig_source", "")).lower()
            id_m = search_term in str(lead.get("_orig_id", "")).lower()
            if not (name_m or phone_m or int_m or src_m or id_m):
                continue

        # Strip internal search helpers before returning
        for key in ("_orig_name", "_orig_phone", "_orig_interest", "_orig_source", "_orig_id"):
            lead.pop(key, None)
        results.append(lead)

    results.sort(key=lambda r: r.get("created_at", r.get("ts", 0)), reverse=True)
    return results[:limit]


def get_lead(gym_id: str, lead_id: str) -> Optional[dict]:
    """Finds a single lead by ID within the tenant scope, applying lock masking if over quota."""
    leads = list_leads(gym_id, limit=1000)
    for l in leads:
        if l.get("id") == lead_id:
            return l
    return None


def update_lead(gym_id: str, lead_id: str, updates: dict) -> Optional[dict]:
    """Thread-safe wrapper (notifications update leads from a background thread)."""
    with _leads_file_lock:
        return _update_lead_unlocked(gym_id, lead_id, updates)


def _update_lead_unlocked(gym_id: str, lead_id: str, updates: dict) -> Optional[dict]:
    """Updates lead fields (status, is_read, notes, etc.) within tenant boundary."""
    all_leads = _read_all_leads()
    found_idx = None
    target_lead = None

    for idx, lead in enumerate(all_leads):
        if lead.get("gym_id") == gym_id and lead.get("id") == lead_id:
            found_idx = idx
            target_lead = lead
            break

    if found_idx is None or target_lead is None:
        return None

    now = time.time()
    old_status = target_lead.get("status", "New")
    if "status" in updates and updates["status"]:
        new_status = updates["status"]
        target_lead["status"] = new_status
        if new_status != old_status and not updates.get("note"):
            note_text = f"Human Action: Status updated to {new_status}"
            note_obj = {
                "id": str(uuid.uuid4())[:8],
                "text": note_text,
                "created_at": now,
                "author": updates.get("author", "Human Admin"),
            }
            notes_list = target_lead.get("notes", [])
            notes_list.append(note_obj)
            target_lead["notes"] = notes_list

    if "is_read" in updates and updates["is_read"] is not None:
        target_lead["is_read"] = bool(updates["is_read"])
    if "interest" in updates and updates["interest"]:
        target_lead["interest"] = updates["interest"]
    if "preferred_time" in updates:
        target_lead["preferred_time"] = updates["preferred_time"]

    # Append note if explicitly provided
    if "note" in updates and updates["note"]:
        note_obj = {
            "id": str(uuid.uuid4())[:8],
            "text": updates["note"].strip(),
            "created_at": now,
            "author": updates.get("author", "Human Admin"),
        }
        notes_list = target_lead.get("notes", [])
        notes_list.append(note_obj)
        target_lead["notes"] = notes_list

    target_lead["updated_at"] = now
    all_leads[found_idx] = target_lead
    _write_all_leads(all_leads)
    # Return the lead as the dashboard sees it: over-quota leads stay masked
    return get_lead(gym_id, lead_id) or {"id": lead_id, "status": target_lead.get("status")}


def delete_lead(gym_id: str, lead_id: str) -> bool:
    """Thread-safe wrapper (notifications update leads from a background thread)."""
    with _leads_file_lock:
        return _delete_lead_unlocked(gym_id, lead_id)


def _delete_lead_unlocked(gym_id: str, lead_id: str) -> bool:
    """Permanently deletes a lead within tenant boundary."""
    all_leads = _read_all_leads()
    initial_len = len(all_leads)
    remaining_leads = [
        l for l in all_leads
        if not (l.get("id") == lead_id and (not gym_id or l.get("gym_id") == gym_id))
    ]
    if len(remaining_leads) == initial_len:
        return False
    _write_all_leads(remaining_leads)
    return True


def clear_all_leads(gym_id: Optional[str] = None) -> int:
    """Thread-safe wrapper (notifications update leads from a background thread)."""
    with _leads_file_lock:
        return _clear_all_leads_unlocked(gym_id)


def _clear_all_leads_unlocked(gym_id: Optional[str] = None) -> int:
    """Bulk clears all lead entries (or leads belonging to a specific tenant gym_id)."""
    all_leads = _read_all_leads()
    if not gym_id:
        count = len(all_leads)
        _write_all_leads([])
        return count
    else:
        remaining = [l for l in all_leads if l.get("gym_id") != gym_id]
        cleared_count = len(all_leads) - len(remaining)
        _write_all_leads(remaining)
        return cleared_count



def retention_days() -> int:
    try:
        return max(1, int(_cfg("LEAD_RETENTION_DAYS", 90) or 90))
    except (TypeError, ValueError):
        return 90


# Leads that became members are kept; everything else is erased after the retention period
_RETAIN_STATUSES = {"joined", "converted"}


def purge_expired_leads(days: Optional[int] = None) -> int:
    """Delete leads with no activity for `days` (default LEAD_RETENTION_DAYS, 90) unless they joined."""
    days = days or retention_days()
    cutoff = time.time() - days * 86400
    with _leads_file_lock:
        leads = _read_all_leads()
        keep = []
        for l in leads:
            last = l.get("updated_at") or l.get("created_at") or l.get("ts") or 0
            if last < cutoff and str(l.get("status", "")).lower() not in _RETAIN_STATUSES:
                continue
            keep.append(l)
        removed = len(leads) - len(keep)
        if removed:
            _write_all_leads(keep)
    if removed:
        logger.info(f"Retention: removed {removed} lead(s) older than {days} days")
    return removed


def get_monthly_leads_count(gym_id: str) -> int:
    """Returns number of leads submitted for the tenant in the current calendar month."""
    leads = _read_all_leads()
    now = time.localtime()
    count = 0
    for l in leads:
        if l.get("gym_id") == gym_id:
            ts = l.get("created_at") or l.get("ts") or 0
            if ts:
                try:
                    t = time.localtime(ts)
                    if t.tm_year == now.tm_year and t.tm_mon == now.tm_mon:
                        count += 1
                except Exception:
                    pass
    return count


def get_unread_count(gym_id: str) -> int:
    """Returns number of unread leads for the tenant."""
    leads = _read_all_leads()
    return sum(1 for l in leads if l.get("gym_id") == gym_id and not l.get("is_read", False))


def _get_gym_identity(gym_id: str) -> dict:
    """Loads identity information for a specific gym tenant."""
    if not gym_id:
        return {}
    ident_path = os.path.join(config.DATA_DIR, f"{gym_id}.identity.json")
    if os.path.exists(ident_path):
        try:
            with open(ident_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

    config_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "identity" in data:
                    return data["identity"]
        except Exception:
            pass
    return {}


def _send_smtp_email(to_email: str, subject: str, body_text: str, body_html: str) -> tuple[bool, str]:
    """Dispatches lead notification email using configured SMTP server."""
    host = _cfg("SMTP_HOST")
    if not host or not to_email:
        return False, "smtp_not_configured" if not host else "no_recipient"
    port = int(_cfg("SMTP_PORT", 587) or 587)
    user = _cfg("SMTP_USER")
    password = _cfg("SMTP_PASSWORD")
    sender = _cfg("SMTP_FROM") or user
    use_ssl = str(_cfg("SMTP_USE_SSL", "")).lower() in ("1", "true", "yes") or port == 465
    use_tls = str(_cfg("SMTP_USE_TLS", "true")).lower() in ("1", "true", "yes")
    if not sender:
        return False, "smtp_from_missing"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
        try:
            if user and password:
                server.login(user, password)
            server.sendmail(sender, [to_email], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, "email_sent_ok"
    except smtplib.SMTPAuthenticationError:
        return False, "smtp_login_failed (check SMTP_USER / app password)"
    except Exception as e:
        return False, f"email_err: {e}"[:200]


def dispatch_notifications(lead: dict, gym_id: Optional[str] = None) -> dict:
    """
    Sends owner notifications across configured channels based on gym tier:
    - Free: Owner WhatsApp alert only
    - Basic: + Owner email + duplicate detection
    - Pro: + Auto-reply WhatsApp to lead + lead scoring
    - Premium: + Welcome SMS + multi-language templates
    Prevents duplicate notifications and records delivery status.
    """
    if lead.get("notification_sent"):
        return {"status": "already_dispatched", "delivery_status": lead.get("delivery_status")}

    target_gym_id = gym_id or lead.get("gym_id") or config.DEFAULT_GYM_ID
    identity = _get_gym_identity(target_gym_id)

    from .schemas import get_tier_limits
    tier_info = get_tier_limits(target_gym_id)
    tier = tier_info.get("tier", "free")

    account = _get_gym_account(target_gym_id)
    gym_name = identity.get("gym_name") or account.get("gym_name") or target_gym_id.replace("-", " ").title()
    # Alerts go to the owner's contacts from the Gyms & Plans console first, then the gym's public contacts
    gym_whatsapp = (account.get("owner_phone") or identity.get("whatsapp_number") or identity.get("primary_phone")
                    or _cfg("OWNER_NOTIFICATION_WHATSAPP"))
    gym_email = account.get("owner_email") or identity.get("email") or _cfg("OWNER_NOTIFICATION_EMAIL")

    lead_name = lead.get("name", "Website Visitor")
    lead_phone = lead.get("phone", "")
    lead_interest = lead.get("interest", "General Inquiry")
    lead_source = lead.get("source", "Website")
    lead_time_str = time.strftime("%H:%M", time.localtime(lead.get("created_at", time.time())))
    lead_id = lead.get("id", "")

    # ===== DUPLICATE DETECTION (Basic+) — flagged in create_lead; no repeat alerts =====
    if lead.get("is_duplicate"):
        lead["notification_sent"] = True
        lead["delivery_status"] = "skipped: duplicate lead (same phone within 7 days)"
        return {"status": "duplicate", "delivery_status": lead["delivery_status"]}

    # ===== LEAD SCORING (Pro+) =====
    lead_score = 0
    if tier in ("pro", "premium"):
        lead_score = _calculate_lead_score(lead, tier_info)
        lead["lead_score"] = lead_score
        lead["lead_priority"] = "hot" if lead_score >= 70 else "warm" if lead_score >= 40 else "cold"

    delivery_statuses = []
    chat_lang = "en"
    theme_data = identity.get("theme")
    if isinstance(theme_data, dict):
        chat_lang = theme_data.get("chat_language", "en")

    # --- WhatsApp to Owner (all tiers) ---
    if tier == "premium":
        owner_alert_msg = (
            f"🔔 *New Lead - {lead.get('lead_priority','warm').upper()}*\n"
            f"Score: {lead_score}/100\n\n"
            f"🏢 {gym_name}\n"
            f"👤 {lead_name}\n"
            f"📞 {lead_phone}\n"
            f"🎯 {lead_interest}\n"
            f"📍 {lead_source}\n"
            f"⏰ {lead_time_str}"
        )
    else:
        owner_alert_msg = (
            f"🚨 *New Gym Lead Received!*\n\n"
            f"🏢 *Gym:* {gym_name}\n"
            f"👤 *Name:* {lead_name}\n"
            f"📞 *Phone:* {lead_phone}\n"
            f"🎯 *Interest:* {lead_interest}\n"
            f"📌 *Source:* {lead_source}\n"
            f"⏰ *Received:* {lead_time_str}\n"
            f"🔗 *CRM Lead ID:* {lead_id}"
        )

    clean_gym_wa = re.sub(r"\D", "", gym_whatsapp or "")
    if clean_gym_wa and len(clean_gym_wa) == 10:
        clean_gym_wa = "91" + clean_gym_wa

    if clean_gym_wa:
        lead["whatsapp_alert_url"] = f"https://wa.me/{clean_gym_wa}?text={urllib.parse.quote(owner_alert_msg)}"
    else:
        lead["whatsapp_alert_url"] = f"https://wa.me/?text={urllib.parse.quote(owner_alert_msg)}"

    # --- WhatsApp to Owner (all tiers) via WhatsApp Cloud API ---
    # Business-initiated messages need an approved template (WHATSAPP_LEAD_ALERT_TEMPLATE).
    # Without one, a plain text message is tried; Meta only delivers it if the owner
    # messaged the business number in the last 24 hours.
    if clean_gym_wa:
        ok, info = send_whatsapp(
            clean_gym_wa, owner_alert_msg,
            template=_cfg("WHATSAPP_LEAD_ALERT_TEMPLATE"),
            template_params=[gym_name, lead_name, lead_phone, lead_interest, lead_source],
        )
        delivery_statuses.append("whatsapp_owner_sent" if ok else f"whatsapp_owner_failed ({info})")
    else:
        delivery_statuses.append("whatsapp_owner_skipped (no owner number)")

    # --- Email to Owner (Basic+) ---
    if tier in ("basic", "pro", "premium"):
        if not gym_email:
            delivery_statuses.append("email_skipped (no owner email)")
        else:
            subject = f"New lead: {lead_name} — {gym_name}"
            rows = [("Name", lead_name), ("Phone", lead_phone), ("Interest", lead_interest),
                    ("Source", lead_source), ("Received", lead_time_str), ("Lead ID", lead_id)]
            if lead.get("message"):
                rows.insert(3, ("Details", lead.get("message")))
            if tier in ("pro", "premium"):
                rows.append(("Lead score", f"{lead_score}/100 ({lead.get('lead_priority', 'warm')})"))
            body = f"New lead received for {gym_name}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
            body_html = (f"<h2 style='font-family:sans-serif'>New lead for {_esc(gym_name)}</h2>"
                         "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse'>"
                         + "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{_esc(k)}</td><td style='padding:4px 0'><b>{_esc(str(v))}</b></td></tr>" for k, v in rows)
                         + "</table>")
            ok, info = _send_smtp_email(gym_email, subject, body, body_html)
            delivery_statuses.append("email_sent" if ok else f"email_failed ({info})")

    # --- Auto-reply WhatsApp to Lead (Pro+) ---
    if tier in ("pro", "premium") and lead_phone:
        clean_prospect = re.sub(r"\D", "", lead_phone or "")
        if len(clean_prospect) == 10:
            clean_prospect = "91" + clean_prospect
        if len(clean_prospect) >= 10:
            welcome_msgs = {
                "en": f"Hi {lead_name}! Thanks for contacting {gym_name}. Our team will reach out shortly. 🏋️",
                "ta": f"Vanakkam {lead_name}! {gym_name} thodarbu seyya nandri.",
                "ml": f"Namaskaram {lead_name}! {gym_name} samsarikkan nandi.",
                "hi": f"Namaste {lead_name}! {gym_name} se sampark karne ke liye dhanyavad."
            }
            prospect_msg = welcome_msgs.get(chat_lang, welcome_msgs["en"])
            lead["prospect_whatsapp_url"] = f"https://wa.me/{clean_prospect}?text={urllib.parse.quote(prospect_msg)}"
            # A first message to a new contact must use an approved template
            welcome_tpl = _cfg("WHATSAPP_LEAD_WELCOME_TEMPLATE")
            if welcome_tpl:
                ok, info = send_whatsapp(clean_prospect, prospect_msg, template=welcome_tpl,
                                         template_params=[lead_name, gym_name])
                delivery_statuses.append("auto_reply_sent" if ok else f"auto_reply_failed ({info})")
            else:
                delivery_statuses.append("auto_reply_link_only (set WHATSAPP_LEAD_WELCOME_TEMPLATE)")

    # --- Welcome SMS (Premium) --- no SMS provider is connected yet
    if tier == "premium" and lead_phone:
        delivery_statuses.append("sms_not_configured")

    lead["notification_sent"] = True
    lead["delivery_status"] = ", ".join(delivery_statuses)
    lead["notification_tier"] = tier
    lead["lead_score"] = lead_score if tier in ("pro", "premium") else None
    lead["notification_channels"] = delivery_statuses
    return {"status": "dispatched", "delivery_status": lead["delivery_status"], "tier": tier}


def _find_recent_duplicate(gym_id: str, phone: str, days: int = 7) -> Optional[str]:
    """Id of an earlier lead with the same phone number in the last `days` days (Basic+)."""
    clean = re.sub(r"\D", "", phone or "")[-10:]
    if not clean:
        return None
    cutoff = time.time() - days * 86400
    for l in reversed(_read_all_leads()):
        if l.get("gym_id") != gym_id:
            continue
        if (l.get("created_at") or l.get("ts") or 0) < cutoff:
            continue
        if re.sub(r"\D", "", str(l.get("phone", "")))[-10:] == clean:
            return l.get("id")
    return None


def _check_duplicate_lead(gym_id: str, phone: str) -> bool:
    """Kept for compatibility."""
    return _find_recent_duplicate(gym_id, phone) is not None


def _get_gym_account(gym_id: str) -> dict:
    """Owner contact details saved in the Gyms & Plans console (data/gym_accounts.json)."""
    path = os.path.join(config.DATA_DIR, "gym_accounts.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                acct = (json.load(f) or {}).get(gym_id) or {}
                return acct if isinstance(acct, dict) else {}
    except Exception:
        pass
    return {}


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send_whatsapp(to: str, text: str, template: str = "", template_params: Optional[list] = None,
                  lang: str = "") -> tuple[bool, str]:
    """Send a WhatsApp message through the Meta WhatsApp Cloud API.

    With `template`, sends that approved template with `template_params` as body variables
    ({{1}}, {{2}}, …). Otherwise sends plain text, which Meta only delivers inside the
    24-hour customer-service window."""
    token = _cfg("WHATSAPP_TOKEN")
    phone_number_id = _cfg("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_number_id:
        return False, "whatsapp_not_configured"
    to = re.sub(r"\D", "", to or "")
    if len(to) == 10:
        to = "91" + to
    if len(to) < 11:
        return False, "invalid_number"
    version = _cfg("WHATSAPP_API_VERSION", "v20.0")
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    if template:
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {
                "name": template,
                "language": {"code": lang or _cfg("WHATSAPP_TEMPLATE_LANG", "en")},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)[:1000] or "-"} for p in (template_params or [])],
                }],
            },
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
                   "text": {"body": text[:4000], "preview_url": False}}
    try:
        res = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
        if res.status_code < 300:
            return True, "sent"
        try:
            err = res.json().get("error", {})
            return False, f"{err.get('code', res.status_code)}: {err.get('message', '')[:120]}"
        except Exception:
            return False, f"http_{res.status_code}"
    except Exception as e:
        return False, f"network_error: {e}"[:150]


def _calculate_lead_score(lead: dict, tier_info: dict) -> int:
    """Calculate lead score 0-100 (Pro+)."""
    score = 50
    if lead.get("phone") and len(re.sub(r"\D", "", lead.get("phone", ""))) >= 10:
        score += 15
    if lead.get("email"):
        score += 10
    if lead.get("interest") and lead.get("interest") not in ("General inquiry", "General Inquiry"):
        score += 10
    source = lead.get("source", "").lower()
    if "walk" in source or "visit" in source:
        score += 15
    elif "referral" in source or "friend" in source:
        score += 10
    hour = time.localtime(lead.get("created_at", time.time())).tm_hour
    if 9 <= hour <= 21:
        score += 10
    return min(score, 100)


def _send_welcome_sms(phone: str, name: str, gym_name: str, lang: str = "en") -> None:
    """Send welcome SMS to lead (Premium)."""
    msgs = {
        "en": f"Welcome {name}! Thanks for choosing {gym_name}. Reply STOP to opt out.",
        "ta": f"Vanakkam {name}! {gym_name}ai thirupthiya seithi nandri.",
        "ml": f"Namaskaram {name}! {gym_name}ye terangedukal nandi.",
        "hi": f"Swagat hai {name}! {gym_name} chune ke liye dhanyavad."
    }
    # SMS gateway integration point - integrate Twilio/MSG91 here


def send_owner_email(to_email: str, subject: str, body: str) -> None:
    """Send an email to the gym owner (raises on failure)."""
    ok, info = _send_smtp_email(to_email, subject, body, "<pre style='font-family:sans-serif'>" + _esc(body) + "</pre>")
    if not ok:
        raise RuntimeError(info)
