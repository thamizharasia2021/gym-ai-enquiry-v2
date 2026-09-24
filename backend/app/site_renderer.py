"""
Dynamic site section renderer.
Each function returns an HTML string for one section.
Called by publish-site and by the live preview endpoint.
"""
import json
import os
from typing import Any

from .pdf_generator import QA_SCHEMA
try:
    from .pdf_generator import resolve_answers
except ImportError:
    def resolve_answers(cfg):
        return []
from . import config


def render_all_sections(cfg: dict, identity: dict, tier_info: dict) -> dict[str, str]:
    """Render all enabled sections. Returns {section_id: html_string}."""
    enabled = cfg.get("sections", {}).get("enabled_sections", [])
    order = cfg.get("sections", {}).get("section_order", [])

    # Respect allowed_sections from tier limits if set
    allowed = tier_info.get("allowed_sections")
    if allowed is not None:
        enabled = [s for s in enabled if s in allowed]
        order = [s for s in order if s in allowed]

    # Preserve order, only render enabled sections
    sections = [s for s in order if s in enabled]

    resolved = resolve_answers(cfg)
    result = {}
    for section_id in sections:
        renderer = SECTION_RENDERERS.get(section_id)
        if renderer:
            try:
                result[section_id] = renderer(cfg, resolved, identity, tier_info)
            except Exception as e:
                result[section_id] = f'<!-- section {section_id}: render error: {e} -->'
        else:
            result[section_id] = f"<!-- section {section_id}: no renderer -->"
    return result


SECTION_RENDERERS: dict[str, Any] = {}


def _register(name):
    """Decorator to register a section renderer."""
    def decorator(fn):
        SECTION_RENDERERS[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

@_register("hero")
def render_hero(cfg, resolved, identity, tier_info):
    gym_name = identity.get("gym_name", "Your Gym")
    tagline = identity.get("short_description") or "Transform Your Fitness Journey"
    return f"""
<section id="hero">
  <div class="hero-inner">
    <div class="hero-badge">🏋️ Premium Fitness Experience</div>
    <h1>Welcome to {_esc(gym_name)}</h1>
    <p>{_esc(tagline)}</p>
    <div class="btnrow">
      <a href="#trial" class="btn btn-primary">Start Free Trial</a>
      <a href="#plans" class="btn btn-secondary">View Plans</a>
    </div>
  </div>
</section>"""


@_register("trust_strip")
def render_trust_strip(cfg, resolved, identity, tier_info):
    items = []
    for qa in resolved:
        if qa.get("configured") and qa.get("category") in ("Equipment", "Facilities", "Trainers"):
            ans = qa.get("answer", "")
            if ans and ans not in ("0", "No", "None"):
                items.append(ans[:80])
        if len(items) >= 4:
            break
    while len(items) < 4:
        items.append("Premium Fitness Experience")

    badges = "\n".join(f'<div class="trust-item"><span class="trust-icon">✓</span><div><strong>{_esc(items[i])}</strong><span>Verified</span></div></div>' for i in range(min(4, len(items))))
    return f"""<section class="trust-strip"><div class="trust-container">{badges}</div></section>"""


@_register("about")
def render_about(cfg, resolved, identity, tier_info):
    desc = identity.get("detailed_description") or identity.get("short_description") or "We are dedicated to helping you achieve your fitness goals."
    return f"""
<section id="about">
  <div class="section-head">
    <div class="section-tag">About Us</div>
    <h2 class="section-title">About {_esc(identity.get('gym_name', 'Our Gym'))}</h2>
    <p class="section-sub">{_esc(desc)}</p>
  </div>
  <div class="grid-4">
    <div class="card-box"><div class="icon">🏋️</div><h4>Expert Training</h4><p>Certified trainers to guide your fitness journey with personalized workout plans.</p></div>
    <div class="card-box"><div class="icon">💪</div><h4>Modern Equipment</h4><p>38+ state-of-the-art machines and free weights for comprehensive workouts.</p></div>
    <div class="card-box"><div class="icon">🥗</div><h4>Diet Guidance</h4><p>Custom nutrition plans tailored to your goals — weight loss, muscle gain, or maintenance.</p></div>
    <div class="card-box"><div class="icon">🎯</div><h4>Goal Focused</h4><p>Track progress, set milestones, and celebrate achievements with our community.</p></div>
  </div>
</section>"""


@_register("equipment")
def render_equipment(cfg, resolved, identity, tier_info):
    items = []
    for qa in resolved:
        if qa.get("category") == "Equipment" and qa.get("configured") and qa.get("answer") not in ("0", "No", "None"):
            items.append(qa["answer"])
    if not items:
        items = ["Treadmills", "Stationary Cycles", "Dumbbells", "Barbells", "Cable Machines", "Smith Machine", "Bench Press", "Pull-up Bar", "Kettlebells", "Racks"]

    def make_card(text):
        return f'<div class="card-box"><div class="icon">🏋️</div><h4>{_esc(text[:40])}</h4><p>Available for all members</p></div>'

    cards = "\n".join(make_card(i) for i in items[:9])
    return f"""
<section id="equipment" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Equipment</div>
    <h2 class="section-title">Our Equipment</h2>
    <p class="section-sub">Top-quality machines and free weights for every workout style</p>
  </div>
  <div class="grid-3">{cards}</div>
</section>"""


@_register("health_diet")
def render_health_diet(cfg, resolved, identity, tier_info):
    return """
<section id="health-diet" style="background:#ffffff;">
  <div class="section-head">
    <div class="section-tag">Health & Diet</div>
    <h2 class="section-title">Health & Nutrition Support</h2>
    <p class="section-sub">Expert guidance on diet, injury recovery, and overall wellness</p>
  </div>
  <div class="grid-3">
    <div class="card-box"><div class="icon">🥗</div><h4>Custom Diet Plans</h4><p>Personalized nutrition plans aligned with your fitness goals and lifestyle.</p></div>
    <div class="card-box"><div class="icon">🏥</div><h4>Injury Recovery</h4><p>Rehab-focused exercises and modifications designed with health professional input.</p></div>
    <div class="card-box"><div class="icon">💊</div><h4>Supplement Guidance</h4><p>Evidence-based supplement recommendations for optimal results.</p></div>
  </div>
</section>"""


@_register("nutrition")
def render_nutrition(cfg, resolved, identity, tier_info):
    return """
<section id="nutrition" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Nutrition</div>
    <h2 class="section-title">Nutrition Products & Guidance</h2>
    <p class="section-sub">Quality supplements and nutritional products to fuel your performance</p>
  </div>
  <div class="grid-3">
    <div class="card-box"><div class="icon">🥤</div><h4>Protein Supplements</h4><p>Whey protein, plant-based options, and BCAAs for muscle recovery.</p></div>
    <div class="card-box"><div class="icon">🍫</div><h4>Healthy Snacks</h4><p>Nutritious snack options to keep you energized between meals.</p></div>
    <div class="card-box"><div class="icon">💧</div><h4>Hydration Drinks</h4><p>Electrolyte-rich drinks for optimal hydration during workouts.</p></div>
  </div>
</section>"""


@_register("policies")
def render_policies(cfg, resolved, identity, tier_info):
    return """
<section id="policies" style="background:#ffffff;">
  <div class="section-head">
    <div class="section-tag">Policies</div>
    <h2 class="section-title">Gym Policies & Guidelines</h2>
    <p class="section-sub">Transparent rules to ensure a great experience for everyone</p>
  </div>
  <div class="grid-3">
    <div class="card-box"><div class="icon">🧹</div><h4>Hygiene Standards</h4><p>Clean equipment policy, sanitization stations, and mandatory towel use.</p></div>
    <div class="card-box"><div class="icon">👕</div><h4>Dress Code</h4><p>Appropriate workout attire required. No outdoor shoes on the gym floor.</p></div>
    <div class="card-box"><div class="icon">💳</div><h4>Refund Policy</h4><p>7-day cooling-off period. Pro-rata refunds available for medical grounds.</p></div>
  </div>
</section>"""


@_register("programs")
def render_programs(cfg, resolved, identity, tier_info):
    return """
<section id="programs" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Programs</div>
    <h2 class="section-title">Our Programs</h2>
    <p class="section-sub">From weight loss to muscle building, we have a program for you</p>
  </div>
  <div class="grid-3">
    <div class="card-box"><div class="icon">🔥</div><h4>Weight Loss</h4><p>Cardio-focused program with diet tracking and progress monitoring.</p></div>
    <div class="card-box"><div class="icon">💪</div><h4>Muscle Building</h4><p>Progressive strength training with personalized split routines.</p></div>
    <div class="card-box"><div class="icon">🧘</div><h4>Flexibility & Mobility</h4><p>Yoga-inspired stretching and mobility work for injury prevention.</p></div>
  </div>
</section>"""


@_register("facilities")
def render_facilities(cfg, resolved, identity, tier_info):
    return """
<section id="facilities" style="background:#ffffff;">
  <div class="section-head">
    <div class="section-tag">Facilities</div>
    <h2 class="section-title">World-Class Amenities</h2>
    <p class="section-sub">Everything you need for a complete workout experience</p>
  </div>
  <div class="facility-grid">
    <div class="facility-item"><span class="facility-icon">❄️</span> Fully Air Conditioned</div>
    <div class="facility-item"><span class="facility-icon">🚿</span> Separate Changing Rooms</div>
    <div class="facility-item"><span class="facility-icon">🔒</span> Free Daily Lockers</div>
    <div class="facility-item"><span class="facility-icon">🅿️</span> Parking Available</div>
    <div class="facility-item"><span class="facility-icon">💧</span> Pure Water Dispensers</div>
    <div class="facility-item"><span class="facility-icon">📶</span> Free WiFi</div>
  </div>
</section>"""


@_register("membership")
def render_membership(cfg, resolved, identity, tier_info):
    return """
<section id="plans">
  <div class="section-head">
    <div class="section-tag">Membership</div>
    <h2 class="section-title">Choose Your Plan</h2>
    <p class="section-sub">Flexible plans designed around your schedule and budget</p>
  </div>
  <div class="grid-3">
    <div class="plan-card">
      <h4>Monthly</h4>
      <div class="price">₹999<span>/month</span></div>
      <p>No commitment. Cancel anytime.</p>
    </div>
    <div class="plan-card featured">
      <div class="plan-badge">Most Popular</div>
      <h4>Quarterly</h4>
      <div class="price">₹2,499<span>/3 months</span></div>
      <p>Save ₹500. Best value.</p>
    </div>
    <div class="plan-card">
      <h4>Yearly</h4>
      <div class="price">₹8,999<span>/year</span></div>
      <p>Maximum savings. Best deal.</p>
    </div>
  </div>
</section>"""


@_register("trainers")
def render_trainers(cfg, resolved, identity, tier_info):
    return """
<section id="trainers" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Trainers</div>
    <h2 class="section-title">Meet Our Coaches</h2>
    <p class="section-sub">Certified professionals dedicated to your success</p>
  </div>
  <div class="grid-3">
    <div class="trainer-card">
      <div class="trainer-avatar">👨‍🏫</div>
      <span class="trainer-tag">Strength</span>
      <h4>Expert Trainer</h4>
      <p>5+ years experience in strength & conditioning</p>
    </div>
    <div class="trainer-card">
      <div class="trainer-avatar">👩‍🏫</div>
      <span class="trainer-tag">Cardio</span>
      <h4>Fitness Coach</h4>
      <p>Specialist in weight loss and endurance training</p>
    </div>
    <div class="trainer-card">
      <div class="trainer-avatar">🧑‍🏫</div>
      <span class="trainer-tag">Yoga</span>
      <h4>Yoga Instructor</h4>
      <p>Certified yoga instructor for flexibility & wellness</p>
    </div>
  </div>
</section>"""


@_register("timings")
def render_timings(cfg, resolved, identity, tier_info):
    return """
<section id="timings" style="background:#ffffff;">
  <div class="section-head">
    <div class="section-tag">Timings</div>
    <h2 class="section-title">Opening Hours</h2>
    <p class="section-sub">We're open 6 days a week to fit your schedule</p>
  </div>
  <div style="max-width:500px;margin:0 auto;background:#fff;border:1px solid var(--border-color);border-radius:16px;overflow:hidden;">
    <div style="display:flex;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--border-color);"><span style="font-weight:600;">Monday – Friday</span><span style="color:var(--primary);font-weight:700;">5:00 AM – 10:00 PM</span></div>
    <div style="display:flex;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--border-color);"><span style="font-weight:600;">Saturday</span><span style="color:var(--primary);font-weight:700;">6:00 AM – 8:00 PM</span></div>
    <div style="display:flex;justify-content:space-between;padding:12px 20px;"><span style="font-weight:600;">Sunday</span><span style="color:var(--primary);font-weight:700;">Closed</span></div>
  </div>
</section>"""


@_register("location")
def render_location(cfg, resolved, identity, tier_info):
    city = identity.get("city", "")
    addr = identity.get("google_maps_url", "")
    return f"""
<section id="location" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Location</div>
    <h2 class="section-title">Find Us</h2>
    <p class="section-sub">{_esc(identity.get('gym_name', 'Our Gym'))} — {_esc(city or 'Your City')}</p>
  </div>
  <div style="text-align:center;">
    <p style="font-size:15px;margin-bottom:16px;">📍 {_esc(identity.get('gym_name', 'Visit us today for a free trial!'))}</p>
    {f'<a href="{_esc(addr)}" target="_blank" class="btn btn-primary">Get Directions</a>' if addr else ''}
  </div>
</section>"""


@_register("faq")
def render_faq(cfg, resolved, identity, tier_info):
    return """
<section id="faq" style="background:#ffffff;">
  <div class="section-head">
    <div class="section-tag">FAQ</div>
    <h2 class="section-title">Frequently Asked Questions</h2>
  </div>
  <div class="faq-list">
    <details><summary>What are your membership plans?</summary><p>We offer monthly, quarterly, and yearly plans starting from ₹999/month. Visit our membership section for details.</p></details>
    <details><summary>Do you offer free trial passes?</summary><p>Yes! Fill out the trial form and our team will reach out to schedule your free session.</p></details>
    <details><summary>Are trainers certified?</summary><p>All our trainers are certified professionals with years of experience in their specialties.</p></details>
    <details><summary>What are the gym timings?</summary><p>Monday-Friday: 5 AM to 10 PM, Saturday: 6 AM to 8 PM. We're closed on Sundays.</p></details>
    <details><summary>Is parking available?</summary><p>Yes, we have dedicated parking space for all our members.</p></details>
  </div>
</section>"""


@_register("trial_cta")
def render_trial_cta(cfg, resolved, identity, tier_info):
    return """
<section id="trial" style="background:linear-gradient(135deg, var(--secondary) 0%, #1e293b 100%);color:#fff;text-align:center;">
  <div style="max-width:560px;margin:0 auto;padding:clamp(32px,5vw,48px) 16px;">
    <h2 style="font-family:'Outfit',sans-serif;font-size:clamp(22px,4.5vw,30px);font-weight:800;margin-bottom:10px;">Start Your Free Trial</h2>
    <p style="opacity:.9;margin-bottom:24px;">Experience our gym first-hand. No commitment required.</p>
    <form class="trial-form" id="leadCaptureForm" onsubmit="handleWebsiteLead(event)">
      <input type="text" id="formName" placeholder="Your Full Name *" required />
      <input type="tel" id="formPhone" placeholder="Mobile Number *" required maxlength="16" />
      <button type="submit" id="formSubmitBtn">Claim Free Pass Now 🚀</button>
      <div id="formMsg" style="display:none;font-size:13.5px;font-weight:700;margin-top:10px;"></div>
    </form>
  </div>
</section>"""


@_register("gallery")
def render_gallery(cfg, resolved, identity, tier_info):
    return """
<section id="gallery" style="background:#f8fafc;">
  <div class="section-head">
    <div class="section-tag">Gallery</div>
    <h2 class="section-title">Our Gym</h2>
  </div>
  <div class="gallery-grid" id="websiteGalleryGrid"></div>
</section>"""


# ---------------------------------------------------------------------------
# Template-specific extra sections
# ---------------------------------------------------------------------------

@_register("stats_counter")
def render_stats_counter(cfg, resolved, identity, tier_info):
    return """
<div class="stats-counter-bar">
  <div class="stat-counter"><span class="stat-num">500+</span><span class="stat-label">Active Members</span></div>
  <div class="stat-counter"><span class="stat-num">10+</span><span class="stat-label">Expert Trainers</span></div>
  <div class="stat-counter"><span class="stat-num">38+</span><span class="stat-label">Equipment</span></div>
  <div class="stat-counter"><span class="stat-num">5+</span><span class="stat-label">Years Experience</span></div>
</div>"""


@_register("reviews")
def render_reviews(cfg, resolved, identity, tier_info):
    return """
<section id="reviews">
  <div class="section-head">
    <div class="section-tag">Reviews</div>
    <h2 class="section-title">What Our Members Say</h2>
  </div>
  <div class="grid-3">
    <div class="review-card"><p>"Best gym in the area! The trainers are amazing and the equipment is top-notch."</p><strong>— Vikram S.</strong></div>
    <div class="review-card"><p>"Lost 15 kg in 3 months. The diet guidance was a game-changer for me."</p><strong>— Priya R.</strong></div>
    <div class="review-card"><p>"Clean, spacious, and great atmosphere. Love the morning yoga sessions!"</p><strong>— Arun K.</strong></div>
  </div>
</section>"""


@_register("instagram")
def render_instagram(cfg, resolved, identity, tier_info):
    return """
<section id="instagram-feed">
  <div class="section-head">
    <div class="section-tag">Follow Us</div>
    <h2 class="section-title">@instagram</h2>
  </div>
  <div class="insta-grid">
    <div class="insta-item">📸</div><div class="insta-item">💪</div><div class="insta-item">🏋️</div>
    <div class="insta-item">🥗</div><div class="insta-item">🧘</div><div class="insta-item">🎯</div>
  </div>
</section>"""


@_register("cta_banner")
def render_cta_banner(cfg, resolved, identity, tier_info):
    return """
<section class="cta-banner">
  <h2>Ready to Transform Your Body?</h2>
  <p>Join {gym} today and start your fitness journey.</p>
  <a href="#trial" class="btn btn-primary">Get Started Now</a>
</section>""".replace("{gym}", identity.get("gym_name", "Us"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Minimal HTML escape for safe insertion into section HTML."""
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
