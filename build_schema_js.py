import json
import os

def main():
    json_path = os.path.join(os.path.dirname(__file__), "backend", "app", "qa_schema.json")
    out_path = os.path.join(os.path.dirname(__file__), "frontend", "qa_schema.js")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.DEFAULT_QA_SCHEMA = " + json.dumps(data) + ";\n")
    print(f"✓ Generated frontend/qa_schema.js ({len(data)} items, {os.path.getsize(out_path)} bytes)")

if __name__ == "__main__":
    main()
