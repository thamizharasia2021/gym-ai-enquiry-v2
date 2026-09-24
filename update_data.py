import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

def main():
    root_cfg_path = os.path.join(os.path.dirname(__file__), "tarvos-fit_config (12).json")
    with open(root_cfg_path, "r", encoding="utf-8") as f:
        root_data = json.load(f)

    cfg_path = os.path.join(os.path.dirname(__file__), "backend", "data", "tarvos-fit.config.json")
    ident_path = os.path.join(os.path.dirname(__file__), "backend", "data", "tarvos-fit.identity.json")

    answers = root_data.get("answers", [])
    ans_by_id = {a["id"]: a for a in answers}

    ans_by_id["FAC_001"] = {
        "id": "FAC_001",
        "situation_label": "Yes, capacity known",
        "field_values": {
            "car_capacity": "4"
        }
    }
    ans_by_id["FAC_011"] = {
        "id": "FAC_011",
        "situation_label": "Not available",
        "field_values": {}
    }
    ans_by_id["LOC_001"] = {
        "id": "LOC_001",
        "situation_label": "Full details",
        "field_values": {
            "full_address": "Tarvos Fit, Pappanamcode, Trivandrum, Kerala 695018",
            "landmark": "Pappanamcode Junction",
            "bus_stop": "Pappanamcode Bus Stand"
        }
    }
    ans_by_id["LOC_002"] = {
        "id": "LOC_002",
        "situation_label": "Link available",
        "field_values": {
            "google_maps_url": "https://maps.app.goo.gl/p6y2t84SyxYGhxnBA"
        }
    }

    updated_answers = list(ans_by_id.values())

    identity = root_data.get("identity", {})
    identity["gym_name"] = "Tarvos Fit"
    identity["brand_name"] = "Tarvos Fit"
    identity["city"] = "Pappanamcode, Trivandrum"
    identity["primary_phone"] = "+91 70128 54261"
    identity["whatsapp_number"] = "+91 73565 51321"
    identity["email"] = "admin@tarvosfitness.com"
    identity["google_maps_url"] = "https://maps.app.goo.gl/p6y2t84SyxYGhxnBA"
    identity["instagram_url"] = "https://instagram.com/tarvosfit"
    identity["short_description"] = "Premier Fitness & Personal Training Center in Pappanamcode, Trivandrum"
    identity["detailed_description"] = "Transform your fitness with certified personal training, advanced equipment, and goal-oriented workout plans in Pappanamcode, Trivandrum."

    theme = root_data.get("theme", {
        "primary_color": "#dc2626",
        "secondary_color": "#1f2937",
        "accent_color": "#b91c1c",
        "background_color": "#ffffff",
        "text_color": "#0f172a",
        "button_color": "#dc2626",
        "chatbot_header_color": "#1f2937",
        "user_msg_color": "#dc2626",
        "bot_msg_color": "#fef2f2",
        "font_family": "Outfit",
        "preset_name": "crimson"
    })
    identity["theme"] = theme

    target_config = {
        "gym_id": "tarvos-fit",
        "identity": identity,
        "plans": root_data.get("plans", {}),
        "offers": root_data.get("offers", []),
        "answers": updated_answers,
        "custom_qa": root_data.get("custom_qa", []),
        "theme": theme,
        "sections": root_data.get("sections", {})
    }

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(target_config, f, indent=2)

    with open(ident_path, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2)

    print("✓ Updated tarvos-fit config & identity files.")

    # Save and reindex
    from backend.app.schemas import GymConfig
    from backend.app.main import save_config

    gym_cfg = GymConfig.model_validate(target_config)
    res = save_config("tarvos-fit", gym_cfg)
    print("✓ Reindexed tarvos-fit successfully:", res)

    # Update map iframe and footer in public_site.html and tarvos-fit.site.html
    new_footer = '''<footer id="contact" style="background:#0f172a;color:#ffffff;padding:50px 20px 30px;border-top:2px solid rgba(255,255,255,0.1);">
  <div style="max-width:1100px;margin:0 auto;">
    <div style="text-align:center;margin-bottom:36px;">
      <span style="background:rgba(34,197,94,0.15);color:#22c55e;font-size:12px;font-weight:800;letter-spacing:1.5px;padding:6px 14px;border-radius:20px;text-transform:uppercase;">Get In Touch</span>
      <h2 style="color:#ffffff;font-size:32px;font-weight:900;margin:12px 0 8px;letter-spacing:-0.5px;">Contact Us — Tarvos Fit</h2>
      <p style="color:#94a3b8;font-size:16px;max-width:600px;margin:0 auto;">Have questions about membership, trial passes, or personal training? Reach out to our front desk instantly via any channel below.</p>
    </div>

    <div class="contact-card-grid" style="display:grid;grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));gap:18px;margin-bottom:40px;">
      <a href="https://wa.me/917356551321?text=Hi%20Tarvos%20Fit%2C%20I%20have%20an%20enquiry%20regarding%20gym%20membership." target="_blank" style="display:flex;flex-direction:column;align-items:center;padding:24px 16px;background:linear-gradient(135deg, #16a34a 0%, #15803d 100%);border-radius:16px;color:#ffffff;text-decoration:none;transition:transform 0.2s ease, box-shadow 0.2s ease;box-shadow:0 8px 20px rgba(22,163,74,0.3);text-align:center;">
        <span style="font-size:36px;margin-bottom:10px;">💬</span>
        <strong style="font-size:18px;font-weight:800;margin-bottom:4px;">WhatsApp Us</strong>
        <span style="font-size:13px;opacity:0.9;font-weight:600;">+91 73565 51321</span>
        <span style="font-size:11px;margin-top:8px;background:rgba(255,255,255,0.2);padding:4px 10px;border-radius:12px;font-weight:700;letter-spacing:0.5px;">⚡ INSTANT CHAT</span>
      </a>

      <a href="tel:+917012854261" style="display:flex;flex-direction:column;align-items:center;padding:24px 16px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);border-radius:16px;color:#ffffff;text-decoration:none;transition:transform 0.2s ease, border-color 0.2s ease;text-align:center;">
        <span style="font-size:36px;margin-bottom:10px;">📞</span>
        <strong style="font-size:18px;font-weight:800;margin-bottom:4px;">Call Desk</strong>
        <span style="font-size:13px;color:#cbd5e1;font-weight:600;">+91 70128 54261</span>
        <span style="font-size:11px;margin-top:8px;background:rgba(255,255,255,0.1);padding:4px 10px;border-radius:12px;color:#94a3b8;font-weight:600;">CALL DIRECTLY</span>
      </a>

      <a href="https://instagram.com/tarvosfit" target="_blank" style="display:flex;flex-direction:column;align-items:center;padding:24px 16px;background:linear-gradient(135deg, #833ab4 0%, #fd1d1d 50%, #fcb045 100%);border-radius:16px;color:#ffffff;text-decoration:none;transition:transform 0.2s ease;box-shadow:0 8px 20px rgba(253,29,29,0.25);text-align:center;">
        <span style="font-size:36px;margin-bottom:10px;">📷</span>
        <strong style="font-size:18px;font-weight:800;margin-bottom:4px;">Instagram</strong>
        <span style="font-size:13px;opacity:0.9;font-weight:600;">@tarvosfit</span>
        <span style="font-size:11px;margin-top:8px;background:rgba(255,255,255,0.25);padding:4px 10px;border-radius:12px;font-weight:700;">FOLLOW & DM</span>
      </a>

      <a href="mailto:admin@tarvosfitness.com" style="display:flex;flex-direction:column;align-items:center;padding:24px 16px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);border-radius:16px;color:#ffffff;text-decoration:none;transition:transform 0.2s ease;text-align:center;">
        <span style="font-size:36px;margin-bottom:10px;">✉️</span>
        <strong style="font-size:18px;font-weight:800;margin-bottom:4px;">Email Us</strong>
        <span style="font-size:13px;color:#cbd5e1;font-weight:600;">admin@tarvosfitness.com</span>
        <span style="font-size:11px;margin-top:8px;background:rgba(255,255,255,0.1);padding:4px 10px;border-radius:12px;color:#94a3b8;font-weight:600;">SEND EMAIL</span>
      </a>

      <a href="https://maps.app.goo.gl/p6y2t84SyxYGhxnBA" target="_blank" style="display:flex;flex-direction:column;align-items:center;padding:24px 16px;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);border-radius:16px;color:#ffffff;text-decoration:none;transition:transform 0.2s ease;text-align:center;">
        <span style="font-size:36px;margin-bottom:10px;">📍</span>
        <strong style="font-size:18px;font-weight:800;margin-bottom:4px;">Google Maps</strong>
        <span style="font-size:13px;color:#cbd5e1;font-weight:600;">Pappanamcode, Trivandrum</span>
        <span style="font-size:11px;margin-top:8px;background:rgba(255,255,255,0.1);padding:4px 10px;border-radius:12px;color:#94a3b8;font-weight:600;">GET DIRECTIONS</span>
      </a>
    </div>

    <p style="border-top:1px solid rgba(255,255,255,0.08);padding-top:20px;text-align:center;color:#64748b;font-size:13px;">© 2026 Tarvos Fit. All Rights Reserved. Built with Gym AI Assistant.</p>
  </div>
</footer>'''

    for fname in ["frontend/public_site.html", "backend/data/tarvos-fit.site.html"]:
        p = os.path.join(os.path.dirname(__file__), fname)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                content = f.read()
            # Replace map iframe src
            content = content.replace("q=Tarvos%20Fit%20Trivandrum", "q=Tarvos%20Fit%20Pappanamcode%20Trivandrum")
            content = content.replace("Tarvos Fit, Trivandrum", "Tarvos Fit, Pappanamcode, Trivandrum")
            # Replace footer
            if '<footer id="contact"' in content:
                start_idx = content.find('<footer id="contact"')
                end_idx = content.find('</footer>', start_idx) + len('</footer>')
                content = content[:start_idx] + new_footer + content[end_idx:]
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"✓ Updated map & footer in {fname}.")

if __name__ == "__main__":
    main()
