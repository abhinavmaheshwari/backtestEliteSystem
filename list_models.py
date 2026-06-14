import requests
import os
import sys

# Extract the key from .env manually or from the environment
env_file = ".env"
gemini_key = None
if os.path.exists(env_file):
    with open(env_file, 'r') as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                gemini_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not gemini_key:
    print("Could not find GEMINI_API_KEY in .env")
    sys.exit(1)

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}"
r = requests.get(url)
if r.status_code == 200:
    models = r.json().get("models", [])
    for m in models:
        name = m.get("name")
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            print(name)
else:
    print(f"Error: {r.status_code}")
    print(r.text)
