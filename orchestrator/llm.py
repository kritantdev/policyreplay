"""Minimal OpenAI-compatible client for Vultr Serverless Inference (stdlib only).

The key is read from the environment and is never logged or put in an exception message.
chat_json: temperature 0, strip markdown fences, validate, retry ONCE with the rejection reason.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def strip_fences(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    return re.sub(r"\s*```$", "", t).strip()


def extract_json_text(raw):
    """Tolerate <think> blocks, markdown fences and a sentence before/after the JSON.
    Leniency here is safe: whatever comes out is still validated strictly afterwards."""
    t = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    t = strip_fences(t)
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if starts:
        a = min(starts)
        try:                                   # take the FIRST complete JSON value, ignore any trailing text
            _, end = json.JSONDecoder().raw_decode(t[a:])
            return t[a:a + end]
        except ValueError:
            return t
    return t


def configured():
    return bool(os.environ.get("VULTR_INFERENCE_KEY") and os.environ.get("PR_TEXT_MODEL"))


def http_transport(messages, model, timeout=60):
    key = os.environ.get("VULTR_INFERENCE_KEY")
    if not key or not model:
        raise LLMError("VULTR_INFERENCE_KEY or PR_TEXT_MODEL not set")
    base = os.environ.get("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1").rstrip("/")
    body = {"model": model, "temperature": 0, "messages": messages}
    if os.environ.get("PR_JSON_MODE") == "1":      # only if /v1 supports response_format
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise LLMError("model returned empty content (reasoning-only or truncated reply)")
        return content
    except urllib.error.HTTPError as e:
        raise LLMError(f"inference HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"inference request failed: {type(e).__name__}") from None


def chat_json(messages, validate, transport=None, model=None):
    """validate(obj) returns the clean result or raises ValueError (incl. pydantic errors)."""
    transport = transport or http_transport
    model = model or os.environ.get("PR_TEXT_MODEL", "")
    msgs, err = list(messages), None
    for _ in range(2):                              # first try + exactly one retry
        raw = transport(msgs, model)
        if os.environ.get("PR_DEBUG") == "1":        # model OUTPUT only; the key is never in scope here
            print(f"[PR_DEBUG] model returned {len(raw)} chars:\n{raw[:3000]}\n[/PR_DEBUG]", file=sys.stderr)
        try:
            return validate(json.loads(extract_json_text(raw)))
        except ValueError as e:
            err = str(e)[:600]
            msgs += [{"role": "assistant", "content": raw[:4000]},
                     {"role": "user", "content": f"That output was rejected: {err}\n"
                                                 "Return only the corrected JSON, nothing else."}]
    raise LLMError(f"model output rejected twice: {err}")
