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

def analyze_concall_text(text: str) -> dict:
    """
    Feeds the extracted transcript text to an LLM to generate the structured JSON.
    First tries Gemini, then OpenAI using pure REST requests to avoid heavy dependencies.
    """
    if not text or len(text) < 100:
        return {"error": "Text too short or empty."}

    # Truncate text to avoid massive token limits (keep first 80k chars roughly)
    if len(text) > 80000:
        text = text[:80000]

    # Try Gemini first via REST
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
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
                    return json.loads(content_str)
                except Exception as e:
                    return {"error": f"Failed to parse Gemini response: {e}"}
            else:
                return {"error": f"Gemini API Error: {res.text}"}
        except Exception as e:
            logger.error(f"Gemini API failed: {e}")
            return {"error": f"Gemini Request Failed: {str(e)}"}

    # Try OpenAI via REST
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gpt-4o-mini",
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
                    return json.loads(content_str)
                except Exception as e:
                    return {"error": f"Failed to parse OpenAI response: {e}"}
            else:
                return {"error": f"OpenAI API Error: {res.text}"}
        except Exception as e:
            logger.error(f"OpenAI API failed: {e}")
            return {"error": f"OpenAI Request Failed: {str(e)}"}

    return {"error": "No LLM API keys found. Please set GEMINI_API_KEY or OPENAI_API_KEY in your environment."}
