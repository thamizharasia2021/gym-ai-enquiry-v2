"""
WhatsApp Cloud API (Meta) integration — the same chat_engine.answer() used
by the web widget, just fronted by a webhook instead of an HTTP call from
the browser. This means the RAG knowledge base is shared automatically:
whatever the owner configures in the wizard answers questions on both
channels with zero duplicate logic.

Setup:
  1. Create a Meta developer app -> WhatsApp product ->
     https://developers.facebook.com/docs/whatsapp/cloud-api/get-started
  2. Set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN
     in your .env
  3. Point the Meta webhook URL at:
     https://<your-domain>/webhook/whatsapp
  4. Map each gym's WhatsApp phone_number_id -> gym_id in
     data/whatsapp_gym_map.json, e.g.:
     { "1234567890123456": "downtown-fitness" }
     (a gym can also be provided via a query param during local testing)
"""
import json
import logging
import os
import httpx
from fastapi import APIRouter, Request, Response

from . import config
from . import chat_engine

router = APIRouter()
logger = logging.getLogger(__name__)

SESSIONS: dict[str, list[dict]] = {}


def _gym_for_phone_number_id(phone_number_id: str) -> str:
    map_path = os.path.join(config.DATA_DIR, "whatsapp_gym_map.json")
    if os.path.exists(map_path):
        with open(map_path) as f:
            mapping = json.load(f)
        if phone_number_id in mapping:
            return mapping[phone_number_id]
    # single-gym deployments fall back to the default gym
    return getattr(config, "DEFAULT_GYM_ID", None) or "default-gym"


def _gym_name(gym_id: str) -> str:
    cfg_path = os.path.join(config.DATA_DIR, f"{gym_id}.config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            return json.load(f).get("identity", {}).get("gym_name", gym_id)
    return gym_id


def _send_whatsapp_message(to: str, body: str, phone_number_id: str):
    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        res = httpx.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code >= 300:
            logger.error(f"WhatsApp send failed ({res.status_code}): {res.text[:300]}")
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")


@router.get("")
def verify(request: Request):
    """Meta's webhook verification handshake."""
    params = request.query_params
    if params.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@router.post("")
async def incoming(request: Request):
    # Verify Meta WhatsApp webhook HMAC signature
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not config.WHATSAPP_APP_SECRET:
        # Reject if no secret configured — never accept unsigned webhooks
        return Response(status_code=403, content="Webhook secret not configured")
    from .security import verify_whatsapp_signature
    if not verify_whatsapp_signature(raw_body, signature):
        return Response(status_code=403, content="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
        entry = payload["entry"][0]["changes"][0]["value"]
        phone_number_id = entry["metadata"]["phone_number_id"]
        message = entry["messages"][0]
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")
    except (KeyError, IndexError):
        return {"status": "ignored"}  # delivery receipts / non-text events

    if not text:
        return {"status": "ignored"}

    gym_id = _gym_for_phone_number_id(phone_number_id)
    session_key = f"{gym_id}:{from_number}"
    history = SESSIONS.setdefault(session_key, [])

    # Same monthly chat quota as the web chat
    try:
        from .schemas import get_gym_usage_stats
        usage = get_gym_usage_stats(gym_id)
        if usage.get("chat_quota_reached"):
            _send_whatsapp_message(
                from_number,
                f"Thanks for messaging {_gym_name(gym_id)}! Our team will reply to you personally soon.",
                phone_number_id,
            )
            return {"status": "quota_reached"}
    except Exception as e:
        logger.error(f"WhatsApp quota check failed: {e}")

    result = chat_engine.answer(gym_id, _gym_name(gym_id), text, history, session_id=session_key, channel="whatsapp")
    history.append({"role": "user", "text": text})
    history.append({"role": "model", "text": result["reply"]})

    _send_whatsapp_message(from_number, result["reply"], phone_number_id)
    return {"status": "sent"}
