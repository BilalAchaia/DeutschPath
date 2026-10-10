"""
Lightweight token/cost tracker for Gemini API calls.
Persists to usage.json in the backend root directory.
"""
import os
import json
from threading import Lock
from database import DATA_DIR

# Gemini 2.5 Flash — text (GA pricing, June 2026)
TEXT_INPUT_PER_M   = 0.30   # prompt tokens
TEXT_OUTPUT_PER_M  = 2.50   # output tokens
TEXT_THOUGHT_PER_M = 2.50   # thinking tokens (billed same as output)

# gemini-2.5-flash-preview-tts — no free tier
TTS_INPUT_PER_M  =  0.50   # text tokens sent to TTS model
TTS_OUTPUT_PER_M = 10.00   # audio output tokens (~11 h per 1M tokens)

USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
_lock = Lock()

_EMPTY = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "thought_tokens": 0,
    "tts_calls": 0,
    "tts_input_tokens": 0,
    "tts_output_tokens": 0,
}


def _read() -> dict:
    if not os.path.exists(USAGE_FILE):
        return dict(_EMPTY)
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in _EMPTY.items():
                data.setdefault(k, v)
            return data
    except Exception:
        return dict(_EMPTY)


def _write(data: dict):
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def record(input_tokens: int, output_tokens: int, thought_tokens: int = 0):
    with _lock:
        data = _read()
        data["calls"] += 1
        data["input_tokens"] += input_tokens
        data["output_tokens"] += output_tokens
        data["thought_tokens"] += thought_tokens
        _write(data)


def record_tts(input_tokens: int, output_tokens: int):
    with _lock:
        data = _read()
        data["tts_calls"] += 1
        data["tts_input_tokens"] += input_tokens
        data["tts_output_tokens"] += output_tokens
        _write(data)


def get_stats() -> dict:
    with _lock:
        data = _read()
    input_tok   = data["input_tokens"]
    output_tok  = data["output_tokens"]
    thought_tok = data["thought_tokens"]
    tts_in      = data["tts_input_tokens"]
    tts_out     = data["tts_output_tokens"]

    text_cost = (
        input_tok   / 1_000_000 * TEXT_INPUT_PER_M +
        output_tok  / 1_000_000 * TEXT_OUTPUT_PER_M +
        thought_tok / 1_000_000 * TEXT_THOUGHT_PER_M
    )
    tts_cost = (
        tts_in  / 1_000_000 * TTS_INPUT_PER_M +
        tts_out / 1_000_000 * TTS_OUTPUT_PER_M
    )
    return {
        "calls":              data["calls"],
        "input_tokens":       input_tok,
        "output_tokens":      output_tok,
        "thought_tokens":     thought_tok,
        "total_tokens":       input_tok + output_tok + thought_tok,
        "tts_calls":          data["tts_calls"],
        "tts_input_tokens":   tts_in,
        "tts_output_tokens":  tts_out,
        "estimated_cost_usd": round(text_cost + tts_cost, 6),
        "text_cost_usd":      round(text_cost, 6),
        "tts_cost_usd":       round(tts_cost, 6),
        "model":              "gemini-2.5-flash",
    }


def reset():
    with _lock:
        _write(dict(_EMPTY))
