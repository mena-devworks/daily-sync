"""Gemini (free tier) with JSON output, model auto-pick, pacing and a per-run call budget."""
import json, os, re, time, requests

BASE = "https://generativelanguage.googleapis.com/v1beta"
PREFERRED = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash-lite"]


class LLMError(Exception):
    pass


class Gemini:
    def __init__(self, max_calls=150, min_gap=4.5):
        self.key = os.environ.get("GEMINI_API_KEY", "")
        if not self.key:
            raise LLMError("GEMINI_API_KEY missing")
        self.max_calls, self.min_gap, self.calls, self.last = max_calls, min_gap, 0, 0.0
        self.models = self._pick()

    def _pick(self):
        try:
            r = requests.get(f"{BASE}/models", params={"key": self.key, "pageSize": 200}, timeout=30).json()
            have = {m["name"].split("/")[-1] for m in r.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])}
        except Exception:
            have = set()
        order = [m for m in PREFERRED if m in have]
        if not order:  # newest flash the key can see
            order = sorted([m for m in have if "flash" in m and "image" not in m and "tts" not in m and "live" not in m], reverse=True)[:3]
        return order or PREFERRED[:2]

    def json(self, prompt, temperature=0.2):
        if self.calls >= self.max_calls:
            raise LLMError("AI call budget for this run used up")
        last_err = None
        for model in self.models:
            for attempt in range(3):
                wait = self.min_gap - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                self.last = time.time()
                try:
                    r = requests.post(f"{BASE}/models/{model}:generateContent", params={"key": self.key}, timeout=120, json={
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
                    })
                except Exception as e:
                    last_err = str(e); time.sleep(5); continue
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"{model} {r.status_code}"; time.sleep(15 * (attempt + 1)); continue
                if r.status_code != 200:
                    last_err = f"{model} {r.status_code} {r.text[:200]}"; break
                self.calls += 1
                try:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return parse_json(text)
                except Exception as e:
                    last_err = f"bad JSON from {model}: {e}"
        raise LLMError(last_err or "Gemini failed")


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if m:
            return json.loads(m.group(1))
        raise
