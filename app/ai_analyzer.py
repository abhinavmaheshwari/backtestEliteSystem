import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

# Prompt for the LLM
SYSTEM_PROMPT = """You are a highly skilled financial analyst. Your task is to read the following text extracted from an official earnings concall transcript or investor presentation.
Extract the following exact metrics and return ONLY a valid JSON object matching this structure exactly (no markdown formatting, just raw JSON):

{
  "management_confidence": 8.5,
  "growth_outlook": "Short 1-sentence summary of revenue/profit growth expectations.",
  "margin_outlook": "Improving / Contracting / Stable",
  "key_risks": ["Risk 1", "Risk 2"],
  "capex_plans": "Summary of major capital expenditures or expansions.",
  "debt_reduction": "Summary of any debt payoff or leveraging plans."
}

If a specific metric is not mentioned at all in the text, put "Not Mentioned" or null.
"""

def _try_gemini_model(model_name: str, gemini_key: str, text: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\nTRANSCRIPT TEXT:\n" + text}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    res = requests.post(url, json=payload, timeout=30)
    if res.status_code == 200:
        data = res.json()
        try:
            content_str = data["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(content_str)
            result["model_used"] = model_name
            return result
        except Exception as e:
            raise Exception(f"Failed to parse response: {e}")
    else:
        raise Exception(f"API Error ({res.status_code}): {res.text}")


def _try_openai_model(model_name: str, openai_key: str, text: str) -> dict:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "response_format": { "type": "json_object" },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "TRANSCRIPT TEXT:\n" + text}
        ]
    }
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    if res.status_code == 200:
        data = res.json()
        try:
            content_str = data["choices"][0]["message"]["content"]
            result = json.loads(content_str)
            result["model_used"] = model_name
            return result
        except Exception as e:
            raise Exception(f"Failed to parse response: {e}")
    else:
        raise Exception(f"API Error ({res.status_code}): {res.text}")


def analyze_concall_text(text: str) -> dict:
    """
    Feeds the extracted transcript text to an LLM to generate the structured JSON.
    Implements a robust fallback chain starting with the best Pro models.
    """
    if not text or len(text) < 100:
        return {"error": "Text too short or empty."}

    # Truncate text to avoid massive token limits (keep first 80k chars roughly)
    if len(text) > 80000:
        text = text[:80000]

    errors = []
    gemini_key = os.getenv("GEMINI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if not gemini_key and not openai_key:
        return {"error": "No LLM API keys found. Please set GEMINI_API_KEY or OPENAI_API_KEY."}

    # Fallback Chain 1: Gemini Models
    if gemini_key:
        gemini_models = ["gemini-1.5-pro", "gemini-2.5-flash", "gemini-1.5-flash"]
        for model in gemini_models:
            try:
                logger.info(f"Attempting AI analysis with {model}...")
                return _try_gemini_model(model, gemini_key, text)
            except Exception as e:
                logger.warning(f"{model} failed: {e}")
                errors.append(f"{model}: {str(e)}")

    # Fallback Chain 2: OpenAI Models
    if openai_key:
        openai_models = ["gpt-4o-mini", "gpt-3.5-turbo"]
        for model in openai_models:
            try:
                logger.info(f"Attempting AI analysis with {model}...")
                return _try_openai_model(model, openai_key, text)
            except Exception as e:
                logger.warning(f"{model} failed: {e}")
                errors.append(f"{model}: {str(e)}")

    return {"error": "All AI models in the fallback chain failed.", "details": errors}
