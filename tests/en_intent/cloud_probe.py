"""Optional English cloud integration probe.

Reuses the Runtime-compatible OpenAI endpoint settings. No request is sent
unless CHAT_API_KEY (or OPENAI_API_KEY) is present in the environment.
"""
from __future__ import annotations
import os, sys
from pathlib import Path

def main() -> int:
    key = os.getenv("CHAT_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        print("SKIP: set CHAT_API_KEY or OPENAI_API_KEY to run cloud verification")
        return 0
    try:
        from openai import OpenAI
    except ImportError:
        print("SKIP: openai package is not installed")
        return 0
    base = os.getenv("CHAT_BASE_URL", "https://api.rvcompute.com:60000/v1")
    model = os.getenv("CHAT_MODEL", "deepseek-v4-flash")
    client = OpenAI(api_key=key, base_url=base)
    cases = [
        "What is quantum entanglement? Answer in English.",
        "Explain why the sky is blue in one sentence.",
    ]
    for text in cases:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are Bibit. Answer accurately and in English."},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        answer = (response.choices[0].message.content or "").strip()
        ascii_words = sum(ch.isascii() and ch.isalpha() for ch in answer)
        print(f"OK: {text} -> {len(answer)} chars, english_letters={ascii_words}")
        if not answer or ascii_words < 10:
            return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
