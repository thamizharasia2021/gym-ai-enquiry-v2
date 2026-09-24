"""
Pydantic models for the owner-configuration payload, theme styling,
website sections, verified external integrations, and CRM leads management.
"""
import json
import os
import time
import uuid
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class QuestionAnswerSelection(BaseModel):
    """One answer the owner picked for one canonical question."""
    id: str                      # e.g. "LOC_001"
    situation_label: str         # e.g. "Full address known"
    field_values: dict[str, str] = Field(default_factory=dict)  # placeholder -> value


class CustomQA(BaseModel):
    """A fully owner-authored question/answer pair outside canonical schema."""
    question: str
    answer: str


class ThemeConfig(BaseModel):
    """Unified theme configuration shared across website and chatbot."""
    # Default look for gyms that haven't chosen colours: Arivayya blue
    primary_color: str = "#2457f5"
    secondary_color: str = "#0e1b3d"
    accent_color: str = "#1b3fc4"
    background_color: str = "#ffffff"
    text_color: str = "#0e1b3d"
    button_color: str = "#2457f5"
    chatbot_header_color: str = "#0e1b3d"
    user_msg_color: str = "#2457f5"
    bot_msg_color: str = "#f1f5f9"
    font_family: str = "Inter"
    preset_name: Optional[str] = "arivayya"


DEFAULT_SECTIONS = [
    "hero",
    "trust_strip",
    "about",
    "equipment",
    "health_diet",
    "nutrition",
    "policies",
    "programs",
    "facilities",
    "membership",
    "trainers",
    "gallery",
    "timings",
    "location",
    "faq",
    "trial_cta",
]


class SectionConfig(BaseModel):
    """Owner's selected website sections and their custom display order."""
    enabled_sections: list[str] = Field(default_factory=lambda: list(DEFAULT_SECTIONS))
    section_order: list[str] = Field(default_factory=lambda: list(DEFAULT_SECTIONS))

    @model_validator(mode="before")
    @classmethod
    def handle_aliases(cls, data):
        if isinstance(data, dict):
            if "enabled" in data and "enabled_sections" not in data:
                data["enabled_sections"] = data["enabled"]
            if "order" in data and "section_order" not in data:
                data["section_order"] = data["order"]
        return data


class GoogleIntegrationConfig(BaseModel):
    """Official Google Places API integration metadata and cached verified reviews."""
    place_id: Optional[str] = None
    public_review_url: Optional[str] = None
    rating: Optional[float] = None          # only real, synced values are shown
    user_ratings_total: Optional[int] = None
    last_synced_at: Optional[float] = None
    cached_reviews: list[dict] = Field(default_factory=list)


class InstagramIntegrationConfig(BaseModel):
    """Official Instagram Graph API & oEmbed integration metadata."""
    instagram_username: Optional[str] = None
    instagram_url: Optional[str] = None
    transformation_url: Optional[str] = None   # URL to post/Reel showing member transformations
    events_url: Optional[str] = None           # URL to post/Reel showing gym events & challenges
    about_url: Optional[str] = None            # URL to post/Reel showing gym tour / about us
    last_synced_at: Optional[float] = None
    cached_media: list[dict] = Field(default_factory=list)


class GymIdentity(BaseModel):
    gym_name: str
    brand_name: Optional[str] = None
    short_description: Optional[str] = None
    detailed_description: Optional[str] = None
    primary_phone: Optional[str] = None
    whatsapp_number: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    supported_languages: list[str] = Field(default_factory=lambda: ["en"])
    currency: str = "INR"
    city: Optional[str] = None
    google_maps_url: Optional[str] = None
    instagram_url: Optional[str] = None
    instagram_transformation_url: Optional[str] = None
    instagram_events_url: Optional[str] = None
    instagram_about_url: Optional[str] = None
    logo_url: Optional[str] = None  # data: URL or hosted URL
    member_count_range: Optional[str] = None  # "<100" | "100-500" | ">500"
    gallery: list[dict] = Field(default_factory=list)  # list of {id, url, caption, category}
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    sections: SectionConfig = Field(default_factory=SectionConfig)
    google: GoogleIntegrationConfig = Field(default_factory=GoogleIntegrationConfig)
    instagram: InstagramIntegrationConfig = Field(default_factory=InstagramIntegrationConfig)


class SaaSTier(str, Enum):
    FREE = "free"
    BASIC = "basic"
    PRO = "pro"
    PREMIUM = "premium"


# Feature flags and limits per tier
TIER_CONFIGS = {
    "free": {
        "max_monthly_leads": 5,
        "max_monthly_chats": 25,
        "analytics_level": "basic_counts",
        "analytics_name": "Basic chat and lead counts",
        "max_sections": 5,
        "llm_enabled": False,
        "csv_export_enabled": False,
        "interactive_map_enabled": False,
        "offers_enabled": False,
        "reels_feed_enabled": False,
        "trial_form_fields": "name_phone",
        "trial_form_fields_label": "Name and phone",
        "allowed_sections": ["hero", "about", "membership", "timings", "location"],
        "allowed_templates": ["tarvos-fit-ironworks"],
    },
    "basic": {
        "max_monthly_leads": 10,
        "max_monthly_chats": 100,
        "analytics_level": "lead_enquiry",
        "analytics_name": "Lead and enquiry analytics",
        "max_sections": 10,
        "llm_enabled": False,
        "csv_export_enabled": False,
        "interactive_map_enabled": True,
        "offers_enabled": True,
        "reels_feed_enabled": False,
        "trial_form_fields": "goal",
        "trial_form_fields_label": "Name, phone and fitness goal",
        "allowed_sections": ["hero", "about", "membership", "timings", "location",
                             "facilities", "equipment", "trainers", "gallery", "programs"],
        "allowed_templates": ["tarvos-fit-ironworks"],
    },
    "pro": {
        "max_monthly_leads": 50,
        "max_monthly_chats": 250,
        "analytics_level": "conversion_source",
        "analytics_name": "Conversion and source analytics",
        "max_sections": 16,
        "llm_enabled": True,
        "csv_export_enabled": True,
        "interactive_map_enabled": True,
        "offers_enabled": True,
        "reels_feed_enabled": True,
        "trial_form_fields": "pro_full",
        "trial_form_fields_label": "Name, phone, email, goal, plan and time slot",
        "allowed_sections": None,
        "allowed_templates": ["classic-modern", "tarvos-fit-pulse", "tarvos-fit-ironworks", "tarvos-fit-form-function", "tarvos-fit-sage-house"],
    },
    "premium": {
        "max_monthly_leads": 100,
        "max_monthly_chats": 500,
        "analytics_level": "campaign_branch",
        "analytics_name": "Branch level campaign statistics",
        "max_sections": 16,
        "llm_enabled": True,
        "csv_export_enabled": True,
        "interactive_map_enabled": True,
        "offers_enabled": True,
        "reels_feed_enabled": True,
        "trial_form_fields": "premium_referrals",
        "trial_form_fields_label": "All pro fields plus referrals and custom fields",
        "allowed_sections": None,
        "allowed_templates": ["classic-modern", "tarvos-fit-pulse", "tarvos-fit-ironworks", "tarvos-fit-form-function", "tarvos-fit-sage-house"],
    },
}


def _load_gym_tier(gym_id: str) -> str:
    """Read the saved config JSON and return the gym's tier (default: free)."""
    from .config import DATA_DIR
    cfg_path = os.path.join(DATA_DIR, f"{gym_id}.config.json")
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            t = data.get("tier", "free")
            if t in TIER_CONFIGS:
                return t
    except Exception:
        pass
    return "free"


def get_allowed_templates(gym_id: str = "default") -> list:
    """Returns list of template IDs the gym can use based on its tier."""
    tier = _load_gym_tier(gym_id)
    config = TIER_CONFIGS.get(tier, TIER_CONFIGS["free"])
    return config.get("allowed_templates", ["classic-modern"])


def get_tier_limits(gym_id: str = "default") -> dict:
    """Returns effective limits for a gym's saved tier (defaults to free), merging custom configurable limits if saved."""
    from .config import DATA_DIR
    tier = _load_gym_tier(gym_id)
    config = dict(TIER_CONFIGS.get(tier, TIER_CONFIGS["free"]))
    config["tier"] = tier

    cfg_path = os.path.join(DATA_DIR, f"{gym_id}.config.json")
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            overrides = data.get("tier_limits_override", {})
            if isinstance(overrides, dict):
                tier_overrides = overrides.get(tier)
                if tier_overrides is None and "*" in overrides:
                    tier_overrides = overrides["*"]
                if isinstance(tier_overrides, dict):
                    for k, v in tier_overrides.items():
                        if k in config and isinstance(v, (int, float, str, bool)):
                            config[k] = v
    except Exception:
        pass

    return config


def get_gym_usage_stats(gym_id: str = "default") -> dict:
    """Returns monthly usage stats (leads & chats) vs active tier limits for a gym."""
    from .config import DATA_DIR
    tier_limits = get_tier_limits(gym_id)
    max_leads = tier_limits.get("max_monthly_leads", 5)
    max_chats = tier_limits.get("max_monthly_chats", 25)

    now = time.time()
    month_start_tuple = time.localtime(now)
    month_start_ts = time.mktime((
        month_start_tuple.tm_year, month_start_tuple.tm_mon, 1, 0, 0, 0, 0, 0, -1
    ))

    # 1. Monthly Leads Count
    leads_path = os.path.join(DATA_DIR, "leads.jsonl")
    monthly_leads = 0
    if os.path.exists(leads_path):
        try:
            with open(leads_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("gym_id") == gym_id and rec.get("created_at", 0) >= month_start_ts:
                            monthly_leads += 1
                    except Exception:
                        continue
        except Exception:
            pass

    # 2. Monthly Chat Messages Count
    events_path = os.path.join(DATA_DIR, "chat_events.jsonl")
    monthly_chats = 0
    if os.path.exists(events_path):
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("gym_id") == gym_id and rec.get("ts", 0) >= month_start_ts:
                            monthly_chats += 1
                    except Exception:
                        continue
        except Exception:
            pass

    leads_over_quota = max(0, monthly_leads - max_leads) if max_leads > 0 else 0
    chats_over_quota = max(0, monthly_chats - max_chats) if max_chats > 0 else 0
    chat_quota_reached = (max_chats > 0 and monthly_chats >= max_chats)

    return {
        "tier": tier_limits.get("tier", "free"),
        "monthly_leads_count": monthly_leads,
        "max_monthly_leads": max_leads,
        "leads_over_quota": leads_over_quota,
        "monthly_chats_count": monthly_chats,
        "max_monthly_chats": max_chats,
        "chats_over_quota": chats_over_quota,
        "chat_quota_reached": chat_quota_reached,
    }


def is_feature_enabled(feature: str, gym_id: str = "default") -> bool:
    """Check if a feature flag is enabled for the gym's tier."""
    limits = get_tier_limits(gym_id)
    key = f"{feature}_enabled"
    return bool(limits.get(key, False))


class GymConfig(BaseModel):
    gym_id: str                          # slug, used as the tenant/collection key
    tier: str = "free"                   # "free" | "basic" | "pro" | "premium"
    tier_limits_override: Optional[dict] = None
    identity: GymIdentity
    answers: list[QuestionAnswerSelection] = Field(default_factory=list)
    custom_qa: list[CustomQA] = Field(default_factory=list)
    theme: Optional[ThemeConfig] = None
    sections: Optional[SectionConfig] = None
    google: Optional[GoogleIntegrationConfig] = None
    instagram: Optional[InstagramIntegrationConfig] = None


class ChatMessage(BaseModel):
    gym_id: str
    session_id: str
    message: str
    channel: str = "web"  # "web" | "whatsapp"


class ChatResponse(BaseModel):
    reply: str
    lead_capture_prompt: bool = False
    sources: list[str] = Field(default_factory=list)


class LeadStatus(str, Enum):
    NEW = "New"
    PENDING = "Pending"
    CONTACTED = "Contacted"
    INTERESTED = "Interested"
    TRIAL_BOOKED = "Trial booked"
    JOINED = "Joined"
    COMPLETED = "Completed"
    CONVERTED = "Converted"
    CLOSED = "Closed"


class LeadNote(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    created_at: float = Field(default_factory=time.time)
    author: str = "Gym Owner"


class Lead(BaseModel):
    id: str = Field(default_factory=lambda: "LEAD-" + str(uuid.uuid4())[:8].upper())
    gym_id: str
    name: str = "Website Visitor"
    phone: str
    source: str = "Website Form"   # "Website Form" | "Website Chatbot" | "WhatsApp Business API" | "Trial Booking"
    interest: str = "General inquiry"
    preferred_time: Optional[str] = ""
    message: Optional[str] = ""
    status: str = "New"
    is_read: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    notes: list[dict] = Field(default_factory=list)
    notification_sent: bool = False
    delivery_status: str = "pending"  # "sent" | "delivered" | "failed" | "skipped"
    delivery_error: Optional[str] = None


class LeadPayload(BaseModel):
    name: Optional[str] = "Website Visitor"
    phone: str
    interest: Optional[str] = "General inquiry"
    preferred_time: Optional[str] = ""
    message: Optional[str] = ""
    channel: Optional[str] = "web"


class LeadUpdatePayload(BaseModel):
    status: Optional[str] = None
    is_read: Optional[bool] = None
    note: Optional[str] = None
    interest: Optional[str] = None
    preferred_time: Optional[str] = None
