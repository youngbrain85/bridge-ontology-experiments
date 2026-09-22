# -*- coding: utf-8 -*-
"""Offline v0.3 runner tests. No real keys, provider calls, or prepared results."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "package" if (HERE / "package").is_dir() else HERE.parent
sys.path.insert(0, str(PACKAGE))
import experiment as engine
import providers

MODEL = {"schema_version": "0.1.0", "model_id": "OFFLINE_MODEL_SENTINEL",
         "entities": [], "relations": [], "unknowns": []}
QUESTION = {"status": "needs_clarification", "question": "Should I proceed with my recommended assumption?"}


def response(provider="openai", text=None, model=None, status=None, reasoning=False, tokens=10):
    text = json.dumps(MODEL) if text is None else text
    if provider == "openai":
        output = [{"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": text}]}]
        if reasoning:
            output.insert(0, {"type": "reasoning", "id": "rs_fixture",
                              "encrypted_content": "OPAQUE_REASONING_SENTINEL", "summary": []})
        raw = {"id": "resp_fixture", "model": model or "gpt-6-astra", "status": status or "completed",
               "output": output,
               "usage": {"input_tokens": tokens, "output_tokens": tokens // 2,
                         "total_tokens": tokens + tokens // 2,
                         "input_tokens_details": {"cached_tokens": 2},
                         "output_tokens_details": {"reasoning_tokens": 1}}}
    else:
        content = [{"type": "text", "text": text}]
        if reasoning:
            content.insert(0, {"type": "thinking", "thinking": "OFFLINE_THINKING",
                               "signature": "OPAQUE_SIGNATURE_SENTINEL"})
        raw = {"id": "msg_fixture", "type": "message", "role": "assistant",
               "model": model or "claude-opus-5", "stop_reason": status or "end_turn", "content": content,
               "usage": {"input_tokens": tokens, "output_tokens": tokens // 2,
                         "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3,
                         "output_tokens_details": {"thinking_tokens": 1}}}
    return json.dumps(raw).encode("utf-8"), {"http_status": 200, "request_id": "offline-request-id"}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline_runner_", dir=HERE)
        self.root = Path(self.temp.name).resolve()
        (self.root / "image.png").write_bytes(b"OFFLINE_IMAGE_NOT_A_REAL_DRAWING")
        self.counter = 0
        self.paid_guard = mock.patch.object(
            providers, "send_request", side_effect=AssertionError("Live provider calls are forbidden in tests"))
        self.no_paid = self.paid_guard.start()

    def tearDown(self):
        self.paid_guard.stop()
        self.temp.cleanup()

    def prepare(self, provider="openai", limit=2, model=None, **changes):
        self.counter += 1
        cfg = engine.read_json(PACKAGE / "config.json")
        cfg.update({"provider": provider, "model": model or ("gpt-6-astra" if provider == "openai" else "claude-opus-5"),
                    "repetitions": 1, "max_output_tokens": 4096, "auto_continue_limit": limit})
        for key in ("knowledge_source", "common_instruction", "output_schema", "geometry_contract", "backend_dir"):
            cfg[key] = str(PACKAGE / cfg[key])
        for case in cfg["cases"]:
            for key in ("scope", "text", "provenance"):
                case[key] = str(PACKAGE / case[key])
            case["images"] = [str(self.root / "image.png")]
        cfg.update(changes)
        config = self.root / ("config_%02d.json" % self.counter)
        exp = self.root / ("exp_%02d" % self.counter)
        engine.write_json(config, cfg)
        engine.prepare(config, exp)
        return exp

    def execute(self, exp, transport, limit=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run_batch(exp, transport=transport, max_new_calls=limit, test_mode=True, **kwargs)

    def first(self, exp, index=0):
        return exp / "runs" / engine.read_json(exp / "schedule.json")["runs"][index]["run_id"]

    def test_01_frozen_provider_files_and_common_abc_inputs_across_models(self):
        a = self.prepare()
        b = self.prepare("anthropic")
        for exp in (a, b):
            self.assertTrue(engine.verify_frozen(exp)["passed"])
            manifest = engine.read_json(exp / "manifest.json")
            self.assertIn("software/providers.py", manifest["files"])
            self.assertIn("software/model_catalog.json", manifest["files"])
            self.assertEqual(manifest["continuation_text"], engine.CONTINUATION_TEXT)
            hashes = {row["knowledge_masked_payload_sha256"]
                      for row in engine.read_json(exp / "input_integrity.json")["rows"]}
            self.assertEqual(len(hashes), 1)
        for case in engine.read_json(a / "config.json")["cases"]:
            for condition in ("A", "B", "C"):
                path = "frozen/cases/" + case["case_id"] + "/" + condition + "_prompt.txt"
                self.assertEqual((a / path).read_bytes(), (b / path).read_bytes())
                self.assertIn(engine.AUTONOMOUS_INSTRUCTION, (a / path).read_text(encoding="utf-8"))
        self.no_paid.assert_not_called()

    def test_02_completed_slots_never_receive_other_slot_or_model_output(self):
        a = self.prepare()
        b = self.prepare(model="gpt-5.6-sol")
        requests = []
        def transport(body, key, timeout, request_id):
            payload = json.loads(body)
            requests.append(payload)
            self.assertEqual(len(payload["input"]), 1)
            self.assertNotIn("OFFLINE_MODEL_SENTINEL", body.decode())
            return response(model=payload["model"])
        result_a = self.execute(a, transport)
        result_b = self.execute(b, transport, limit=1)
        self.assertEqual((result_a["new_calls"], result_a["new_api_turns"]), (3, 3))
        self.assertEqual((result_b["new_calls"], result_b["new_api_turns"]), (1, 1))
        self.assertEqual(len(requests), 4)
        self.assertEqual(engine.read_json(self.first(a) / "model_input.json"), MODEL)
        self.assertEqual(engine.read_json(self.first(b) / "model_input.json"), MODEL)
        self.no_paid.assert_not_called()

    def test_03_openai_clarification_preserves_every_turn_and_fixed_history(self):
        exp = self.prepare()
        requests = []
        first_response = response(text=json.dumps(QUESTION), reasoning=True, tokens=10)
        def transport(body, key, timeout, request_id):
            requests.append(json.loads(body))
            return first_response if len(requests) == 1 else response(tokens=20)
        result = self.execute(exp, transport, limit=1)
        run = self.first(exp)
        saved = engine.read_json(run / "result.json")
        self.assertEqual((result["new_calls"], result["new_api_turns"]), (1, 2))
        self.assertEqual((saved["api_turns"], saved["continuation_count"]), (2, 1))
        self.assertEqual(saved["usage"]["input_tokens"], 30)
        self.assertEqual(saved["usage"]["output_tokens"], 15)
        self.assertEqual(saved["usage"]["input_tokens_details"]["cached_tokens"], 4)
        self.assertEqual(requests[1]["input"][0], requests[0]["input"][0])
        self.assertEqual(requests[1]["input"][1:-1], json.loads(first_response[0])["output"])
        self.assertEqual(requests[1]["input"][-1]["content"][0]["text"], engine.CONTINUATION_TEXT)
        self.assertNotIn("previous_response_id", requests[1])
        for index in (0, 1):
            folder = run / "turns" / ("turn_%02d" % index)
            for name in ("request.json", "attempt.json", "response.raw.json", "raw_response.txt", "result.json"):
                self.assertTrue((folder / name).is_file(), name)
        self.assertEqual((run / "request.json").read_bytes(), (run / "turns/turn_00/request.json").read_bytes())
        self.assertEqual((run / "turns/turn_00/response.raw.json").read_bytes(), first_response[0])
        self.assertEqual(engine.read_json(run / "model_input.json"), MODEL)

    def test_04_anthropic_clarification_keeps_signed_thinking_same_slot(self):
        exp = self.prepare("anthropic")
        requests = []
        first_response = response("anthropic", text="\uc774 \uac00\uc815\uc744 \uc0ac\uc6a9\ud574\uc11c \uc9c4\ud589\ud560\uae4c\uc694?", reasoning=True)
        def transport(body, key, timeout, request_id):
            requests.append(json.loads(body))
            return first_response if len(requests) == 1 else response("anthropic")
        result = self.execute(exp, transport, limit=1)
        self.assertEqual(result["new_api_turns"], 2)
        self.assertEqual(requests[1]["messages"][0], requests[0]["messages"][0])
        self.assertEqual(requests[1]["messages"][1]["content"], json.loads(first_response[0])["content"])
        self.assertEqual(requests[1]["messages"][-1]["content"][0]["text"], engine.CONTINUATION_TEXT)
        saved = engine.read_json(self.first(exp) / "result.json")
        self.assertEqual(saved["usage"]["input_tokens"], 30)
        self.assertEqual(saved["usage"]["output_tokens_details"]["reasoning_tokens"], 2)

    def test_05_continuation_limit_is_not_a_retry_or_repair_budget(self):
        for limit in (0, 2):
            exp = self.prepare(limit=limit)
            transport = mock.Mock(return_value=response(text=json.dumps(QUESTION)))
            result = self.execute(exp, transport, limit=1)
            self.assertEqual(transport.call_count, limit + 1)
            self.assertEqual(result["new_api_turns"], limit + 1)
            saved = engine.read_json(self.first(exp) / "result.json")
            self.assertEqual(saved["status"], "clarification_limit_reached")
            self.assertFalse((self.first(exp) / "model_input.json").exists())

    def test_06_model_objects_and_invalid_json_are_never_repaired(self):
        samples = [
            ('{"entities":"WRONG_SCHEMA","question":"Should I continue?"}', "completed"),
            ('{"status":[],"question":{}}', "completed"),
            ('{"schema_version":"bad","entities":[]}', "completed"),
            ('{"entities":', "invalid_model_json"),
            ('[]', "invalid_model_json"),
            ('{"x":NaN}', "invalid_model_json"),
            ('{"x":1,"x":2}', "invalid_model_json"),
            ("This drawing is difficult to read.", "invalid_model_json"),
        ]
        for text, status in samples:
            with self.subTest(text=text):
                exp = self.prepare()
                transport = mock.Mock(return_value=response(text=text))
                self.execute(exp, transport, limit=1)
                self.assertEqual(transport.call_count, 1)
                saved = engine.read_json(self.first(exp) / "result.json")
                self.assertEqual(saved["status"], status)
                self.assertEqual((self.first(exp) / "raw_response.txt").read_text(encoding="utf-8"), text)
                if status == "completed":
                    self.assertEqual(engine.read_json(self.first(exp) / "model_input.json"), json.loads(text))

    def test_07_model_change_during_continuation_pauses_and_blocks_resume(self):
        exp = self.prepare()
        transport = mock.Mock(side_effect=[
            response(text=json.dumps(QUESTION)),
            response(model="different-returned-model"),
        ])
        result = self.execute(exp, transport)
        self.assertEqual(result["status"], "paused_after_model_change")
        self.assertEqual((result["new_calls"], result["new_api_turns"]), (1, 2))
        self.assertEqual(engine.read_json(self.first(exp) / "result.json")["status"], "protocol_deviation")
        with self.assertRaisesRegex(ValueError, "Protocol deviation"):
            self.execute(exp, transport)
        self.assertEqual(transport.call_count, 2)

    def test_08_transport_failure_keeps_turns_and_resume_starts_next_slot(self):
        exp = self.prepare()
        transport = mock.Mock(side_effect=[
            response(text=json.dumps(QUESTION)),
            engine.TransportError("http_error", 429, "offline-error"),
        ])
        result = self.execute(exp, transport)
        self.assertEqual(result["status"], "paused_after_transport_failure")
        self.assertEqual((result["new_calls"], result["new_api_turns"]), (1, 2))
        failed = engine.read_json(self.first(exp) / "result.json")
        self.assertEqual((failed["status"], failed["api_turns"]), ("http_error", 2))
        self.assertEqual(failed["usage"]["input_tokens"], 10)
        self.assertTrue((self.first(exp) / "turns/turn_00/response.raw.json").is_file())
        self.assertFalse((self.first(exp) / "turns/turn_01/response.raw.json").exists())
        def fresh(body, key, timeout, request_id):
            self.assertEqual(len(json.loads(body)["input"]), 1)
            return response()
        resumed = self.execute(exp, fresh, limit=1)
        self.assertEqual((resumed["new_calls"], resumed["new_api_turns"]), (1, 1))
        self.assertEqual(engine.read_json(self.first(exp) / "result.json"), failed)
        self.assertEqual(engine.read_json(self.first(exp, 1) / "result.json")["status"], "completed")

    def test_09_ambiguous_interruption_never_replays_a_slot(self):
        exp = self.prepare()
        transport = mock.Mock(side_effect=[response(text=json.dumps(QUESTION)), RuntimeError("offline crash")])
        with self.assertRaisesRegex(RuntimeError, "offline crash"):
            self.execute(exp, transport)
        run = self.first(exp)
        request_hash = engine.sha(run / "turns/turn_01/request.json")
        self.assertFalse((run / "result.json").exists())
        resumed = self.execute(exp, mock.Mock(return_value=response()), limit=1)
        saved = engine.read_json(run / "result.json")
        self.assertEqual((saved["status"], saved["api_turns"], saved["continuation_count"]),
                         ("interrupted_outcome_unknown", 2, 1))
        self.assertEqual(engine.sha(run / "turns/turn_01/request.json"), request_hash)
        self.assertEqual(resumed["new_api_turns"], 1)

    def test_10_stop_prevents_initial_or_followup_request_and_resume_skips_attempt(self):
        stop = self.root / "stop.requested"
        exp = self.prepare()
        stop.touch()
        transport = mock.Mock(return_value=response())
        result = self.execute(exp, transport, stop_file=stop)
        self.assertEqual((result["new_calls"], result["new_api_turns"]), (0, 0))
        transport.assert_not_called()
        self.assertFalse((self.first(exp) / "attempt.json").exists())
        stop.unlink()
        def question_then_stop(body, key, timeout, request_id):
            stop.touch()
            return response(text=json.dumps(QUESTION))
        result = self.execute(exp, question_then_stop, stop_file=stop)
        self.assertEqual((result["status"], result["new_api_turns"]), ("stopped_after_current_request", 1))
        self.assertEqual(engine.read_json(self.first(exp) / "result.json")["status"], "stopped_before_continuation")
        stop.unlink()
        resumed = self.execute(exp, transport, limit=1, stop_file=stop)
        self.assertEqual(resumed["new_calls"], 1)
        self.assertFalse((self.first(exp) / "turns/turn_01").exists())

    def test_11_stop_during_input_verification_is_seen_before_request(self):
        exp = self.prepare()
        stop = self.root / "stop.requested"
        original = engine.verify_frozen
        checks = []
        def verify(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            checks.append(path)
            if len(checks) == 3:
                stop.touch()
            return value
        transport = mock.Mock(return_value=response())
        with mock.patch.object(engine, "verify_frozen", side_effect=verify):
            result = self.execute(exp, transport, stop_file=stop)
        self.assertEqual((result["new_calls"], result["new_api_turns"]), (0, 0))
        self.assertFalse((self.first(exp) / "attempt.json").exists())
        transport.assert_not_called()

    def test_12_test_mode_and_key_boundaries(self):
        exp = self.prepare()
        sentinel = "OFFLINE_SECRET_SENTINEL_DO_NOT_PERSIST"
        with self.assertRaisesRegex(ValueError, "injected offline"):
            engine.run_batch(exp, test_mode=True)
        with self.assertRaisesRegex(ValueError, "Live transport"):
            engine.run_batch(exp, test_mode=True, transport=providers.send_request)
        with self.assertRaisesRegex(ValueError, "restricted"):
            engine.run_batch(exp, transport=lambda *args: response())
        for key in ("has space", "has\nnewline", "\ud55c\uae00", "", None):
            with self.subTest(key_type=type(key).__name__):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    with self.assertRaisesRegex(ValueError, "API key"):
                        engine.run_batch(exp, api_key=key)
        seen = []
        def transport(body, key, timeout, request_id):
            seen.append(key)
            self.assertNotIn(sentinel, body.decode())
            return response()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": sentinel, "ANTHROPIC_API_KEY": sentinel}):
            self.execute(exp, transport, limit=1, api_key=sentinel)
        self.assertEqual(seen, ["OFFLINE_TEST_NO_CREDENTIAL"])
        for file in exp.rglob("*"):
            if file.is_file():
                self.assertNotIn(sentinel.encode(), file.read_bytes())
        with self.assertRaisesRegex(ValueError, "Cannot mix"):
            engine.run_batch(exp, api_key=sentinel)
        self.no_paid.assert_not_called()

    def test_13_frozen_policy_runtime_and_model_settings_fail_before_network(self):
        for name in ("software/providers.py", "software/model_catalog.json", "config.json"):
            with self.subTest(name=name):
                exp = self.prepare()
                path = exp / name
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, "Frozen files changed"):
                    self.execute(exp, mock.Mock(return_value=response()))
        exp = self.prepare()
        engine.write_json(exp / "admin/execution_runtime.json", {"changed": True})
        with self.assertRaisesRegex(ValueError, "runtime changed"):
            self.execute(exp, mock.Mock(return_value=response()))
        cfg = engine.read_json(PACKAGE / "config.json")
        for bad in (True, -1, 6, 1.5, None):
            with self.subTest(limit=bad):
                with self.assertRaisesRegex(ValueError, "auto_continue_limit"):
                    engine.validate_config({**cfg, "auto_continue_limit": bad})
        self.no_paid.assert_not_called()

    def test_14_incomplete_or_refused_provider_response_is_not_continued(self):
        fixtures = [
            response(text=json.dumps(QUESTION), status="incomplete"),
            response("anthropic", text=json.dumps(QUESTION), status="max_tokens"),
        ]
        for provider, fixture in zip(("openai", "anthropic"), fixtures):
            exp = self.prepare(provider)
            transport = mock.Mock(return_value=fixture)
            result = self.execute(exp, transport, limit=1)
            self.assertEqual(result["new_api_turns"], 1)
            self.assertEqual(engine.read_json(self.first(exp) / "result.json")["status"], "provider_incomplete")
            transport.assert_called_once()
        raw = json.loads(response()[0])
        raw["output"][0]["content"] = [{"type": "refusal", "refusal": "OFFLINE_REFUSAL"}]
        exp = self.prepare()
        self.execute(exp, mock.Mock(return_value=(json.dumps(raw).encode(), {})), limit=1)
        self.assertEqual(engine.read_json(self.first(exp) / "result.json")["status"], "refused")

    def test_15_question_detection_is_explicit_and_does_not_extract_model_fragments(self):
        for text in ("What units should I use?", "Which coordinate system should I choose?",
                     "Please choose option A or B.", "\ub2e8\uc704\ub97c \ud655\uc778 \ubd80\ud0c1\ub4dc\ub9bd\ub2c8\ub2e4.", "\uc774\ub300\ub85c \uc9c4\ud589\ud560\uae4c\uc694?",
                     "\uc5b4\ub5a4 \ub2e8\uc704\uc778\uac00\uc694?"):
            with self.subTest(text=text):
                self.assertEqual(engine.clarification_kind(text), "explicit_plain_question")
        for text in ("Finished. No further clarification is needed.", "The supplied information is incomplete.",
                     "What units should I use? {\"entities\":[]}", "Would you like me to continue?\n{\"x\":"):
            with self.subTest(text=text):
                self.assertIsNone(engine.clarification_kind(text))
        self.assertIsNone(engine.clarification_kind("", {"status": [], "question": {}}))
        self.assertIsNone(engine.clarification_kind("", {"entities": [], "question": "Proceed?"}))

    def test_16_question_with_recommendation_continues_without_adding_knowledge(self):
        exp = self.prepare()
        question = {**QUESTION, "recommendation": "Continue with the documented assumptions.",
                    "recommended_option": "first", "recommended_approach": {"id": "first", "description": "Use supplied evidence."},
                    "choices": [{"id": "first"}, {"id": "second"}]}
        requests = []
        def transport(body, key, timeout, request_id):
            requests.append(json.loads(body))
            return response(text=json.dumps(question)) if len(requests) == 1 else response()
        result = self.execute(exp, transport, limit=1)
        self.assertEqual(result["new_api_turns"], 2)
        self.assertEqual(requests[1]["input"][-1]["content"][0]["text"], engine.CONTINUATION_TEXT)
        self.assertEqual(engine.read_json(self.first(exp) / "model_input.json"), MODEL)
        self.assertIsNone(engine.clarification_kind("", {**question, "entities": []}))
        self.assertIsNone(engine.clarification_kind("", {**question, "recommendation": {"geometry": {}}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
