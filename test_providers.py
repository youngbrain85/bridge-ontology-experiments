"""Offline provider adapter tests. No live requests and no real credentials."""
import base64
import copy
import http.client
import io
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

import providers as p


IMAGE = {"media_type": "image/png", "data": base64.b64encode(b"synthetic fixture bytes").decode("ascii")}
SECRET = "UNIT_TEST_NOT_A_REAL_SECRET"


def settings(model="gpt-6-astra"):
    profile = next(row for row in p.catalog() if row["model"] == model)
    return {"provider": profile["provider"], "model": model, "reasoning_effort": profile["default_reasoning"], "thinking_budget_tokens": profile["default_thinking_budget"], "max_output_tokens": 32768}


def openai_response():
    return {"id": "resp_offline", "model": "gpt-6-astra", "status": "completed", "output": [
        {"type": "reasoning", "id": "rs_offline", "summary": [], "encrypted_content": "OPAQUE_DO_NOT_CHANGE"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"question":"Use the recommended option?"}'}]}],
        "usage": {"input_tokens": 100, "output_tokens": 80, "total_tokens": 180, "input_tokens_details": {"cached_tokens": 30}, "output_tokens_details": {"reasoning_tokens": 60}}}


def anthropic_response():
    return {"id": "msg_offline", "type": "message", "role": "assistant", "model": "claude-opus-5", "stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": "Synthetic thinking fixture", "signature": "OPAQUE_SIGNATURE_DO_NOT_CHANGE"},
        {"type": "redacted_thinking", "data": "OPAQUE_REDACTED_DO_NOT_CHANGE"},
        {"type": "text", "text": '{"question":"Use the recommended option?"}'}],
        "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 80, "output_tokens_details": {"thinking_tokens": 60}}}


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class ProviderTests(unittest.TestCase):
    def test_catalog_settings_and_manual_budget_guards(self):
        self.assertEqual(len(p.catalog()), 11)
        seen = set()
        for profile in p.catalog():
            key = (profile["provider"], profile["model"])
            self.assertNotIn(key, seen)
            seen.add(key)
            for effort in profile["reasoning_options"]:
                cfg = settings(profile["model"])
                cfg["reasoning_effort"] = effort
                if profile["thinking_mode"] == "manual" and effort == "disabled":
                    cfg["thinking_budget_tokens"] = None
                self.assertIsNone(p.validate_model_settings(cfg))
            cfg = settings(profile["model"])
            cfg["max_output_tokens"] = profile["max_output_tokens"] + 1
            with self.assertRaises(ValueError):
                p.validate_model_settings(cfg)
        for model, invalid in (("gpt-6-astra", "none"), ("claude-opus-4-6", "xhigh"), ("claude-sonnet-4-6", "xhigh"), ("claude-haiku-4-5-20251001", "high")):
            cfg = settings(model)
            cfg["reasoning_effort"] = invalid
            with self.assertRaises(ValueError):
                p.validate_model_settings(cfg)
        for budget in (None, True, 1023, 32768, 40000):
            cfg = settings("claude-haiku-4-5-20251001")
            cfg["thinking_budget_tokens"] = budget
            with self.assertRaises(ValueError):
                p.validate_model_settings(cfg)

    def test_fable_profiles_enforce_adaptive_thinking_and_supported_efforts(self):
        prompt = "Common independent drawing prompt."
        for model in ("claude-fable-5-1", "claude-fable-5"):
            profile = next(row for row in p.catalog() if row["model"] == model)
            self.assertEqual(profile["reasoning_options"], ["low", "medium", "high", "xhigh", "max"])
            self.assertEqual(profile["default_reasoning"], "high")
            self.assertEqual(profile["thinking_mode"], "adaptive")
            self.assertIsNone(profile["default_thinking_budget"])
            for effort in profile["reasoning_options"]:
                with self.subTest(model=model, effort=effort):
                    cfg = {**settings(model), "reasoning_effort": effort, "max_output_tokens": 128000}
                    request = p.build_request(cfg, prompt, [IMAGE])
                    self.assertEqual(request["model"], model)
                    self.assertEqual(request["max_tokens"], 128000)
                    self.assertEqual(request["thinking"], {"type": "adaptive"})
                    self.assertEqual(request["output_config"], {"effort": effort})
                    self.assertEqual(request["system"], p.SYSTEM_TEXT)
                    self.assertEqual(request["messages"], [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image", "source": {"type": "base64", **IMAGE}}]}])
                    self.assertNotIn("fallbacks", request)
                    self.assertNotIn("tools", request)
                    self.assertNotIn("tool_choice", request)
            for changes in ({"reasoning_effort": "disabled"}, {"reasoning_effort": "enabled"},
                            {"reasoning_effort": "none"}, {"reasoning_effort": "adaptive"},
                            {"thinking_budget_tokens": 16000}, {"max_output_tokens": 128001}):
                with self.subTest(model=model, invalid=changes), self.assertRaises(ValueError):
                    p.build_request({**settings(model), **changes}, prompt, [IMAGE])

    def test_fable_continuation_preserves_omitted_thinking_and_unchanged_prefix(self):
        for model in ("claude-fable-5-1", "claude-fable-5"):
            initial = p.build_request(settings(model), "Initial independent prompt", [IMAGE])
            raw = anthropic_response()
            raw["model"] = model
            raw["content"][0]["thinking"] = ""
            before = copy.deepcopy((initial, raw))
            parsed = p.parse_response("anthropic", encode(raw))
            self.assertEqual(parsed["status"], "completed")
            self.assertEqual(parsed["returned_model"], model)
            updated = p.append_continuation("anthropic", initial, raw, "Fixed automatic followup")
            self.assertEqual(updated["messages"][:len(initial["messages"])], initial["messages"])
            self.assertEqual(updated["messages"][-2], {"role": "assistant", "content": raw["content"]})
            self.assertEqual(updated["messages"][-1], {"role": "user", "content": [{"type": "text", "text": "Fixed automatic followup"}]})
            self.assertEqual({key: val for key, val in updated.items() if key != "messages"},
                             {key: val for key, val in initial.items() if key != "messages"})
            self.assertEqual((initial, raw), before)
            updated["messages"][-2]["content"][0]["signature"] = "Changed copy only"
            self.assertEqual(raw["content"][0]["signature"], "OPAQUE_SIGNATURE_DO_NOT_CHANGE")
            raw["stop_details"] = {"type": "refusal"}
            self.assertEqual(p.parse_response("anthropic", encode(raw))["status"], "refused")
            with self.assertRaises(ValueError):
                p.append_continuation("anthropic", initial, raw, "Do not retry refused generation")

    def test_fresh_payloads_share_instructions_text_and_image_bytes(self):
        prompt = "Exactly the supplied common prompt and knowledge block."
        a = p.build_request(settings(), prompt, [IMAGE])
        b = p.build_request(settings("claude-opus-5"), prompt, [IMAGE])
        self.assertEqual(a["instructions"], b["system"])
        self.assertEqual(a["input"][0]["content"][0]["text"], b["messages"][0]["content"][0]["text"])
        self.assertEqual(a["input"][0]["content"][1]["image_url"].split(",", 1)[1], b["messages"][0]["content"][1]["source"]["data"])
        self.assertEqual(len(a["input"]), 1)
        self.assertEqual(len(b["messages"]), 1)
        self.assertFalse(a["store"])
        self.assertEqual(a["tools"], [])
        self.assertNotIn("tools", b)
        self.assertNotIn("text", a)
        self.assertNotIn("format", b["output_config"])
        for payload in (a, b):
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("conversation", payload)
            self.assertNotIn("temperature", payload)
            self.assertNotIn("top_p", payload)
        self.assertEqual(b["thinking"], {"type": "adaptive"})
        manual = p.build_request(settings("claude-haiku-4-5-20251001"), prompt, [IMAGE])
        self.assertEqual(manual["thinking"], {"type": "enabled", "budget_tokens": 16000})
        self.assertNotIn("output_config", manual)
        with self.assertRaises(ValueError):
            p.build_request(settings(), prompt, [{"media_type": "image/png", "data": "not base64!"}])

    def test_openai_continuation_preserves_encrypted_output_and_does_not_mutate(self):
        initial = p.build_request(settings(), "Initial independent prompt", [IMAGE])
        raw = openai_response()
        before_initial, before_raw = copy.deepcopy(initial), copy.deepcopy(raw)
        next_request = p.append_continuation("openai", initial, raw, "Fixed automatic followup")
        self.assertEqual(next_request["input"][:1], initial["input"])
        self.assertEqual(next_request["input"][1:-1], raw["output"])
        self.assertEqual(next_request["input"][-1]["content"][0]["text"], "Fixed automatic followup")
        self.assertEqual(initial, before_initial)
        self.assertEqual(raw, before_raw)
        next_request["input"][1]["encrypted_content"] = "changed only the copied test object"
        self.assertEqual(raw["output"][0]["encrypted_content"], "OPAQUE_DO_NOT_CHANGE")
        self.assertEqual(p.build_request(settings(), "Initial independent prompt", [IMAGE]), initial)

    def test_anthropic_continuation_keeps_thinking_blocks_signatures_and_order(self):
        initial = p.build_request(settings("claude-opus-5"), "Initial independent prompt", [IMAGE])
        raw = anthropic_response()
        snapshot = copy.deepcopy(raw)
        next_request = p.append_continuation("anthropic", initial, raw, "Fixed automatic followup")
        self.assertEqual(len(next_request["messages"]), 3)
        self.assertEqual(next_request["messages"][1], {"role": "assistant", "content": raw["content"]})
        self.assertEqual(raw, snapshot)
        self.assertEqual(len(initial["messages"]), 1)
        self.assertNotIn("container", next_request)
        self.assertEqual(p.build_request(settings("claude-opus-5"), "Initial independent prompt", [IMAGE]), initial)

    def test_provider_usage_and_visible_text_normalization(self):
        a = p.parse_response("openai", encode(openai_response()))
        b = p.parse_response("anthropic", encode(anthropic_response()))
        self.assertEqual(a["status"], "completed")
        self.assertEqual(b["status"], "completed")
        self.assertNotIn("thinking", b["text"])
        self.assertEqual(a["text"], b["text"])
        self.assertEqual(a["usage"]["input_tokens"], 100)
        self.assertEqual(b["usage"]["input_tokens"], 150)
        self.assertEqual(b["usage"]["total_tokens"], 230)
        self.assertEqual(b["usage"]["input_tokens_details"]["cached_tokens"], 30)
        self.assertEqual(b["usage"]["input_tokens_details"]["cache_write_tokens"], 20)
        self.assertEqual(a["usage"]["output_tokens_details"]["reasoning_tokens"], 60)
        self.assertEqual(b["usage"]["output_tokens_details"]["reasoning_tokens"], 60)
        raw = anthropic_response()
        del raw["usage"]["output_tokens_details"]
        self.assertIsNone(p.parse_response("anthropic", encode(raw))["usage"]["output_tokens_details"]["reasoning_tokens"])

    def test_incomplete_refused_unexpected_and_invalid_responses_never_continue(self):
        examples = []
        raw = openai_response(); raw["status"] = "incomplete"
        examples.append(("openai", raw, "provider_incomplete"))
        raw = openai_response(); raw["output"] = [{"type": "function_call", "name": "forbidden"}]
        examples.append(("openai", raw, "unexpected_tool_or_output"))
        raw = openai_response(); raw["output"][1]["content"] = [{"type": "refusal", "refusal": "synthetic refusal"}]
        examples.append(("openai", raw, "refused"))
        raw = anthropic_response(); raw["stop_reason"] = "max_tokens"
        examples.append(("anthropic", raw, "provider_incomplete"))
        raw = anthropic_response(); raw["stop_details"] = {"type": "refusal"}
        examples.append(("anthropic", raw, "refused"))
        raw = anthropic_response(); raw["content"].append({"type": "tool_use", "id": "tool", "name": "forbidden", "input": {}})
        examples.append(("anthropic", raw, "unexpected_tool_or_output"))
        for provider, raw, expected in examples:
            self.assertEqual(p.parse_response(provider, encode(raw))["status"], expected)
            initial = p.build_request(settings("gpt-6-astra" if provider == "openai" else "claude-opus-5"), "Initial prompt", [])
            with self.assertRaises(ValueError):
                p.append_continuation(provider, initial, raw, "Must not be sent")
        for body in (b"not json", b"[]", b'{"x":NaN}', b'{"x":1,"x":2}'):
            self.assertEqual(p.parse_response("openai", body)["status"], "invalid_api_response")

    def test_transports_use_fixed_endpoints_one_call_and_only_matching_auth_header(self):
        class Reply:
            status = 200
            headers = {"x-request-id": "openai_offline", "request-id": "anthropic_offline"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b"{}"
        for provider in ("openai", "anthropic"):
            captured = []
            class Opener:
                def open(self, request, timeout):
                    captured.append(request)
                    return Reply()
            with patch.object(p.urllib.request, "build_opener", return_value=Opener()) as build, patch.dict(os.environ, {"OPENAI_API_KEY": "DO_NOT_READ_AMBIENT", "ANTHROPIC_API_KEY": "DO_NOT_READ_AMBIENT"}):
                body, metadata = p.send_request(provider, b"{}", SECRET, 9, "client_offline")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].full_url, p.ENDPOINTS[provider])
            self.assertEqual(captured[0].get_method(), "POST")
            headers = {key.lower(): value for key, value in captured[0].header_items()}
            if provider == "openai":
                self.assertEqual(headers["authorization"], "Bearer " + SECRET)
                self.assertNotIn("x-api-key", headers)
            else:
                self.assertEqual(headers["x-api-key"], SECRET)
                self.assertEqual(headers["anthropic-version"], "2023-06-01")
                self.assertNotIn("authorization", headers)
            self.assertNotIn(SECRET, json.dumps(metadata))
            self.assertTrue(any(isinstance(handler, p.NoRedirect) for handler in build.call_args.args))
            proxy = next(handler for handler in build.call_args.args if isinstance(handler, p.urllib.request.ProxyHandler))
            self.assertEqual(proxy.proxies, {})

    def test_http_error_has_no_retry_or_error_body_exposure(self):
        class ForbiddenBody(io.BytesIO):
            def read(self, *args): raise AssertionError("Error body must not be read")
        error = urllib.error.HTTPError(p.ENDPOINTS["anthropic"], 429, SECRET, {"request-id": "offline_error"}, ForbiddenBody(SECRET.encode()))
        class Opener:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                raise error
        opener = Opener()
        with patch.object(p.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(p.TransportError) as caught:
                p.send_request("anthropic", b"{}", SECRET, 1, "client_offline")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(caught.exception.category, "http_error")
        self.assertEqual(caught.exception.http_status, 429)
        self.assertEqual(caught.exception.request_id, "offline_error")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(p.NoRedirect().redirect_request(None, None, 302, "", {}, "https://external.invalid"))

    def test_malformed_keys_and_transport_errors_do_not_echo_secrets(self):
        with patch.object(p.urllib.request, "build_opener") as build:
            for key in (SECRET + "\nBAD", SECRET + " SPACE", SECRET + "\ud55c\uae00", ""):
                with self.assertRaises(ValueError) as caught:
                    p.send_request("openai", b"{}", key, 1, "client_offline")
                self.assertNotIn(SECRET, str(caught.exception))
            build.assert_not_called()
        class Opener:
            def open(self, *args, **kwargs):
                raise urllib.error.URLError(SECRET)
        with patch.object(p.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(p.TransportError) as caught:
                p.send_request("openai", b"{}", SECRET, 1, "client_offline")
        self.assertEqual(caught.exception.category, "transport_error_outcome_unknown")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_connection_dropped_while_reading_body_is_a_transport_error(self):
        class TruncatedReply:
            status = 200
            headers = {"request-id": "offline_truncated"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): raise http.client.IncompleteRead(SECRET.encode())
        class Opener:
            def open(self, *args, **kwargs): return TruncatedReply()
        with patch.object(p.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(p.TransportError) as caught:
                p.send_request("anthropic", b"{}", SECRET, 1, "client_offline")
        self.assertEqual(caught.exception.category, "transport_error_outcome_unknown")
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
