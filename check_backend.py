import urllib.request
import json

try:
    with urllib.request.urlopen("http://localhost:8000/health", timeout=3) as resp:
        print("Backend Response:", resp.status, resp.read().decode("utf-8"))
except Exception as e:
    print("Backend Error:", e)
