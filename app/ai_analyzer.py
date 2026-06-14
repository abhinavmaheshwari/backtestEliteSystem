import os
import json
import logging
import requests

logger = logging.getLogger(__name__)

# Prompt for the LLM
SYSTEM_PROMPT = """You are an expert financial equity research analyst.
You will be provided with the text extracted from a company's earnings concall transcripts or investor presentations.
You may receive TWO transcripts separated by "--- LATEST QUARTER ---" and "--- PREVIOUS QUARTER ---". 
Your job is to read them carefully and extract specific forward-looking guidance and deep fundamental commentary.
Provide highly detailed, analytical summaries for each field. Extract as much quantitative data (margins, revenue targets, capex numbers, timeline) as possible. Do not limit sentence length; provide thorough, research-grade context.
If a specific topic is not discussed in the text, return exactly the string "Not Mentioned". DO NOT hallucinate.

For the 'management_confidence' score: Be HIGHLY critical. Start at a baseline of 5. Add points ONLY for explicit upward guidance, record margins, or major debt reduction. Subtract points for headwinds, margin pressure, or missed targets. Do not default to 8. A score of 8, 9, or 10 must be exceptionally rare and reserved ONLY for massive, undeniable growth guidance.

Return the result as a strict JSON object with EXACTLY these keys:
{
    "management_confidence": (integer 1-10, be highly critical, do not default to 8),
    "guidance_delta": (string summary comparing the explicit numeric guidance given in the latest quarter vs the previous quarter. Explicitly highlight if management upgraded or downgraded their outlook. If no previous quarter text is provided, summarize any changes from previous expectations mentioned),
    "top_line_guidance": (string summary of explicit revenue or volume guidance),
    "bottom_line_guidance": (string summary of EBITDA, net profit, or margin expansion/contraction guidance),
    "demand_environment": (string summary of broader industry tailwinds, market share gains, or macro demand shifts),
    "volume_vs_pricing": (string summary of whether growth is driven by volume expansion or pricing realization),
    "capex_and_launches": (string summary of major capital expenditures, R&D, or new product pipelines),
    "working_capital_debt": (string summary of inventory levels, cash flow efficiency, or debt reduction plans),
    "key_risks": (array of strings, listing top 1-3 risks/headwinds mentioned)
}"""

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
    res = requests.post(url, json=payload, timeout=90)
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
    res = requests.post(url, headers=headers, json=payload, timeout=90)
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
    gemini_key_str = os.getenv("GEMINI_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY")

    if not gemini_key_str and not openai_key:
        return {"error": "No LLM API keys found. Please set GEMINI_API_KEY or OPENAI_API_KEY."}

    # Fallback Chain 1: Gemini Models
    if gemini_key_str:
        gemini_keys = [k.strip() for k in gemini_key_str.split(",") if k.strip()]
        gemini_models = ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash-8b", "gemini-2.0-flash-exp"]
        
        for model in gemini_models:
            success = False
            for i, gemini_key in enumerate(gemini_keys):
                try:
                    logger.info(f"Attempting AI analysis with {model} (Key {i+1}/{len(gemini_keys)})...")
                    result = _try_gemini_model(model, gemini_key, text)
                    result["key_used"] = f"Key {i+1}"
                    return result
                except Exception as e:
                    import time
                    err_str = str(e).replace(gemini_key, "[REDACTED_KEY]")
                    if "429" in err_str or "Quota" in err_str:
                        logger.warning(f"{model} hit rate limit on Key {i+1}.")
                        if i == len(gemini_keys) - 1:
                            logger.warning(f"All Gemini keys exhausted for {model}. Sleeping 30s before trying next model/retry...")
                            time.sleep(30)
                            # We don't return here, we let it break and go to the next model in `gemini_models`
                    else:
                        logger.warning(f"{model} failed: {err_str}")
                        errors.append(f"{model}: {err_str}")
                        break # Skip to next model if it's not a quota issue

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
