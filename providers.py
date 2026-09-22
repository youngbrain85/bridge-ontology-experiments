"""Documented OpenAI/Anthropic wire adapters; no credential discovery or retries.

Each initial request is independent. A continuation receives only its caller's
current payload and raw response. The runner owns slot isolation and call caps.
No provider-specific JSON mode is enabled: both use the same system text.
"""
from __future__ import annotations

import base64
import copy
import json
import math
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.request


VERSION = "0.3.0"
SYSTEM_TEXT = "Use only this request's supplied material. Return one JSON object matching the supplied output contract. Do not grade the result."
ENDPOINTS = {"openai": "https://api.openai.com/v1/responses", "anthropic": "https://api.anthropic.com/v1/messages"}
ANTHROPIC_VERSION = "2023-06-01"
CATALOG_PATH = Path(__file__).with_name("model_catalog.json")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def catalog():
    """Return fresh profiles; never call an account's models API."""
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["models"]


def _profile(provider, model):
    for profile in catalog():
        if profile["provider"] == provider and profile["model"] == model:
            return profile
    raise ValueError("Unknown provider/model selection; choose a documented catalog profile.")


def validate_model_settings(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("Model settings must be an object.")
    profile = _profile(cfg.get("provider"), cfg.get("model"))
    if cfg.get("reasoning_effort") not in profile["reasoning_options"]:
        raise ValueError("Unsupported reasoning setting for this model.")
    output = cfg.get("max_output_tokens")
    if type(output) is not int or not 1 <= output <= profile["max_output_tokens"]:
        raise ValueError("max_output_tokens must be a positive integer within this model's documented limit.")
    budget = cfg.get("thinking_budget_tokens")
    if profile["thinking_mode"] == "manual" and cfg["reasoning_effort"] == "enabled":
        if type(budget) is not int or not 1024 <= budget < output:
            raise ValueError("Manual thinking_budget_tokens must be at least 1024 and less than max_output_tokens.")
    elif budget is not None:
        raise ValueError("thinking_budget_tokens must be null unless manual thinking is enabled.")
    if cfg.get("temperature") is not None or cfg.get("top_p") is not None:
        raise ValueError("This common experiment protocol does not send temperature or top_p.")


def _images(images):
    if not isinstance(images, list):
        raise ValueError("images must be an ordered array.")
    clean = []
    for image in images:
        if not isinstance(image, dict) or set(image) != {"media_type", "data"} or image.get("media_type") not in IMAGE_TYPES or not isinstance(image.get("data"), str):
            raise ValueError("Each image needs a supported media_type and base64 data.")
        try:
            decoded = base64.b64decode(image["data"], validate=True)
        except (ValueError, TypeError):
            raise ValueError("Image data is not valid base64.") from None
        if not decoded:
            raise ValueError("Image data is empty.")
        clean.append(dict(image))
    return clean


def build_request(cfg, prompt, images):
    """Construct a fresh initial request; no conversation state is accepted."""
    validate_model_settings(cfg)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty common prompt is required.")
    images = _images(images)
    profile = _profile(cfg["provider"], cfg["model"])
    if cfg["provider"] == "openai":
        content = [{"type": "input_text", "text": prompt}]
        content.extend({"type": "input_image", "detail": "high", "image_url": "data:" + image["media_type"] + ";base64," + image["data"]} for image in images)
        return {"model": cfg["model"], "instructions": SYSTEM_TEXT,
                "input": [{"role": "user", "content": content}],
                "store": False, "stream": False, "tools": [], "tool_choice": "none", "truncation": "disabled",
                "reasoning": {"effort": cfg["reasoning_effort"]}, "max_output_tokens": cfg["max_output_tokens"],
                "include": ["reasoning.encrypted_content"], "service_tier": "default"}
    content = [{"type": "text", "text": prompt}]
    content.extend({"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}} for image in images)
    payload = {"model": cfg["model"], "system": SYSTEM_TEXT,
               "messages": [{"role": "user", "content": content}],
               "max_tokens": cfg["max_output_tokens"], "stream": False}
    if profile["thinking_mode"] == "adaptive":
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": cfg["reasoning_effort"]}
    else:
        payload["thinking"] = {"type": cfg["reasoning_effort"]}
        if cfg["reasoning_effort"] == "enabled":
            payload["thinking"]["budget_tokens"] = cfg["thinking_budget_tokens"]
    return payload


def append_continuation(provider, payload, response, followup_text):
    """Deep-copy same-slot history, including opaque reasoning/signature blocks.

    The caller must enforce that response belongs to this payload and slot.
    No response IDs are dereferenced and no server conversation is attached.
    """
    if provider not in ENDPOINTS or not isinstance(payload, dict) or not isinstance(response, dict):
        raise ValueError("Invalid continuation input.")
    if not isinstance(followup_text, str) or not followup_text.strip():
        raise ValueError("Continuation requires the frozen followup text.")
    if "previous_response_id" in payload or "conversation" in payload:
        raise ValueError("Stored conversation references are forbidden.")
    _profile(provider, payload.get("model"))
    normalized = parse_response(provider, json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if normalized["status"] != "completed":
        raise ValueError("Only a completed assistant response can receive an automatic continuation.")
    result = copy.deepcopy(payload)
    if provider == "openai":
        if result.get("store") is not False or not isinstance(result.get("input"), list):
            raise ValueError("Continuation requires stateless OpenAI input history.")
        result["input"].extend(copy.deepcopy(response["output"]))
        result["input"].append({"role": "user", "content": [{"type": "input_text", "text": followup_text}]})
    else:
        if not isinstance(result.get("messages"), list):
            raise ValueError("Continuation requires Anthropic message history.")
        result["messages"].append({"role": "assistant", "content": copy.deepcopy(response["content"])})
        result["messages"].append({"role": "user", "content": [{"type": "text", "text": followup_text}]})
    return result


def _counter(value):
    return value if type(value) is int and value >= 0 else None


def _normalized_usage(provider, usage):
    normalized = {"input_tokens": None, "output_tokens": None, "total_tokens": None,
                  "input_tokens_details": {"cached_tokens": None, "cache_write_tokens": None},
                  "output_tokens_details": {"reasoning_tokens": None}}
    if not isinstance(usage, dict):
        return normalized
    input_count = _counter(usage.get("input_tokens"))
    output_count = _counter(usage.get("output_tokens"))
    if provider == "openai":
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        if not isinstance(input_details, dict) or not isinstance(output_details, dict):
            return normalized
        normalized["input_tokens"] = input_count
        normalized["input_tokens_details"]["cached_tokens"] = _counter(input_details.get("cached_tokens"))
        normalized["input_tokens_details"]["cache_write_tokens"] = _counter(input_details.get("cache_write_tokens"))
        normalized["output_tokens_details"]["reasoning_tokens"] = _counter(output_details.get("reasoning_tokens"))
    else:
        # Anthropic input_tokens excludes cache reads/writes. Include all three
        # reported categories in the common total; missing cache fields mean 0.
        cache_read = _counter(usage.get("cache_read_input_tokens", 0))
        cache_write = _counter(usage.get("cache_creation_input_tokens", 0))
        if None not in (input_count, cache_read, cache_write):
            normalized["input_tokens"] = input_count + cache_read + cache_write
        normalized["input_tokens_details"] = {"cached_tokens": cache_read, "cache_write_tokens": cache_write, "uncached_tokens": input_count}
        output_details = usage.get("output_tokens_details") or {}
        if isinstance(output_details, dict):
            normalized["output_tokens_details"]["reasoning_tokens"] = _counter(output_details.get("thinking_tokens"))
    normalized["output_tokens"] = output_count
    if normalized["input_tokens"] is not None and output_count is not None:
        normalized["total_tokens"] = normalized["input_tokens"] + output_count
    return normalized


def _json_object(body):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError("Nonfinite JSON number")

    value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Response is not an object")
    return value


def parse_response(provider, body):
    """Normalize provider completion state and visible text; do not repair JSON."""
    if provider not in ENDPOINTS:
        raise ValueError("Unsupported provider.")
    result = {"status": "invalid_api_response", "text": "", "returned_model": None,
              "usage": _normalized_usage(provider, None), "provider_status": None,
              "response_id": None, "raw": None, "refusal": False, "unexpected_output_types": []}
    try:
        raw = _json_object(body)
        result["raw"] = raw
        result["returned_model"] = raw.get("model") if isinstance(raw.get("model"), str) else None
        result["response_id"] = raw.get("id") if isinstance(raw.get("id"), str) else None
        result["usage"] = _normalized_usage(provider, raw.get("usage"))
        result["provider_status"] = raw.get("status") if provider == "openai" else raw.get("stop_reason")
        if raw.get("error") or raw.get("type") == "error":
            result["status"] = "provider_failed"
            return result
        texts, unexpected = [], []
        if provider == "openai":
            output = raw.get("output")
            if not isinstance(output, list) or not isinstance(raw.get("status"), str):
                return result
            for item in output:
                if not isinstance(item, dict):
                    return result
                kind = item.get("type")
                if kind == "reasoning":
                    continue
                if kind != "message" or item.get("role") != "assistant":
                    unexpected.append(kind if isinstance(kind, str) else "unknown")
                    continue
                if not isinstance(item.get("content"), list):
                    return result
                for part in item["content"]:
                    if not isinstance(part, dict):
                        return result
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif part.get("type") == "refusal":
                        result["refusal"] = True
                    else:
                        unexpected.append(part.get("type") if isinstance(part.get("type"), str) else "unknown")
            state = "completed" if raw["status"] == "completed" else "provider_incomplete" if raw["status"] == "incomplete" else "provider_failed"
        else:
            content = raw.get("content")
            if raw.get("role") != "assistant" or not isinstance(content, list) or not isinstance(raw.get("stop_reason"), str):
                return result
            for part in content:
                if not isinstance(part, dict):
                    return result
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif kind in ("thinking", "redacted_thinking"):
                    continue
                else:
                    unexpected.append(kind if isinstance(kind, str) else "unknown")
            details = raw.get("stop_details") or {}
            result["refusal"] = raw["stop_reason"] == "refusal" or (isinstance(details, dict) and details.get("type") == "refusal")
            state = "completed" if raw["stop_reason"] in ("end_turn", "stop_sequence") else "provider_incomplete" if raw["stop_reason"] in ("max_tokens", "model_context_window_exceeded", "pause_turn") else "provider_failed"
        result["text"] = "".join(texts)
        result["unexpected_output_types"] = unexpected
        result["status"] = "refused" if result["refusal"] else "unexpected_tool_or_output" if unexpected else state
        return result
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        return result


class TransportError(Exception):
    def __init__(self, category, http_status=None, request_id=None):
        super().__init__(category)
        self.category = category
        self.http_status = http_status
        self.request_id = request_id


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _header_value(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(name + " is missing or invalid.")
    value = value.strip()
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ValueError(name + " contains unsupported characters.")
    return value


def _request_id(headers, provider):
    value = headers.get("x-request-id" if provider == "openai" else "request-id")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        return value
    return None


def send_request(provider, payload_bytes, key, timeout, request_id):
    """Send once to a fixed official endpoint; never inspect env/files for keys."""
    if provider not in ENDPOINTS:
        raise ValueError("Unsupported provider.")
    if not isinstance(payload_bytes, bytes):
        raise ValueError("Request payload must be bytes.")
    key = _header_value(key, "API key", 4096)
    request_id = _header_value(request_id, "Client request ID", 200)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Request timeout must be a finite positive number.")
    headers = {"Content-Type": "application/json", "X-Client-Request-Id": request_id, "User-Agent": "bridge-ontology-multi-provider/" + VERSION}
    if provider == "openai":
        headers["Authorization"] = "Bearer " + key
    else:
        headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    request = urllib.request.Request(ENDPOINTS[provider], data=payload_bytes, method="POST", headers=headers)
    # Disable environment-derived proxies and all redirects. Credentials are
    # attached only to the selected official HTTPS endpoint, with TLS validation.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            return body, {"http_status": response.status, "request_id": _request_id(response.headers, provider), "provider": provider}
    except urllib.error.HTTPError as error:
        metadata_id = _request_id(error.headers, provider) if error.headers is not None else None
        code = error.code
        error.close()
        raise TransportError("http_error", code, metadata_id) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise TransportError("transport_error_outcome_unknown") from None
