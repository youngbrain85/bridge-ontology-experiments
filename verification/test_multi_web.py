# -*- coding: utf-8 -*-
"""Offline multi-model orchestration/security tests. No real keys or paid calls.

HTTP is loopback only. Every Popen and provider transport is mocked, and all
writable fixtures live in TemporaryDirectory under this work directory.
"""
import contextlib
import copy
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "package" if (HERE / "package").is_dir() else HERE.parent
APP = PACKAGE / "webapp"
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(APP))
import providers
import experiment as engine
import batch_protocol
import frozen_runtime
import app_service
import local_common


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load("multi_web_server_test", APP / "server.py")
worker = load("multi_web_worker_test", APP / "worker.py")
KEYS = {"openai": "OFFLINE_OPENAI_SENTINEL_ONLY", "anthropic": "OFFLINE_CLAUDE_SENTINEL_ONLY"}
SELECTIONS = [
    {"provider": "openai", "model": "gpt-6-astra", "reasoning_effort": "high", "thinking_budget_tokens": None},
    {"provider": "anthropic", "model": "claude-opus-5", "reasoning_effort": "high", "thinking_budget_tokens": None},
]


class Sink(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class FakeProcess:
    """Popen replacement: stdin consumed normally, stdout waits on release."""
    def __init__(self, args, kwargs, failed=False):
        self.args, self.kwargs = args, kwargs
        self.stdin = Sink()
        self.release = threading.Event()
        self.failed = failed
        self.stdout = self.lines()
        self.returncode = None
        self.job_dir = Path(args[args.index("--job-dir") + 1])

    def lines(self):
        self.release.wait(4)
        if not self.stdin.closed:
            raise AssertionError("Worker stdin was not closed")
        result = {"ok": not self.failed, "result": {"status": "all_slots_attempted"}}
        if self.failed:
            result = {"ok": False, "error": "Offline model failure"}
        local_common.write_json(self.job_dir / "result.json", result)
        yield b'{"event":"call_finished","run_id":"r0001","status":"completed","turn":0,"raw_text":"MUST_NOT_PERSIST"}\n'
        yield b'{"event":"unrecognized","raw_text":"MUST_NOT_PERSIST"}\n'
        yield b'not json\n'

    def wait(self):
        self.returncode = 1 if self.failed else 0
        return self.returncode

    def poll(self):
        return self.returncode


class OfflineCredentialStore:
    """In-memory stand-in for the Windows DPAPI store on other hosts; writes nothing to disk."""
    def __init__(self):
        self.keys = {}

    def status(self):
        return {p: {"saved": p in self.keys, "available": p in self.keys, "error": None} for p in ("openai", "anthropic")}

    def get(self, provider):
        return self.keys.get(provider)

    def save_many(self, keys):
        self.keys.update({p: local_common.normalized_key(v) for p, v in keys.items()})

    def delete(self, provider):
        self.keys.pop(provider, None)


class MultiWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline_multi_web_", dir=HERE)
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        # Application imports already loaded real modules; this is only an existence marker.
        (self.package / "experiment.py").write_text("# Offline fixture only\n", encoding="utf-8")
        (self.package / "batch_protocol.py").write_text("# Offline fixture only\n", encoding="utf-8")
        self.models = []
        for i, selection in enumerate(SELECTIONS, 1):
            ident = "fixture_m%02d" % i
            exp = self.package / "experiments" / ident
            self.models.append({"experiment_id": ident, **selection})
            self.put(exp / "config.json", {**selection, "repetitions": 1, "max_output_tokens": 4096,
                "auto_continue_limit": 2, "cases": [{"case_id": "OFFLINE", "images": ["frozen/image.png"]}]})
            self.put(exp / "manifest.json", {"offline_fixture_only": True})
            self.put(exp / "schedule.json", {"runs": [
                {"run_id": "r0001", "condition": "C", "case_id": "OFFLINE", "repetition": 1},
                {"run_id": "r0002", "condition": "A", "case_id": "OFFLINE", "repetition": 1}]})
            self.put(exp / "admin/review_key.json", {"slots": [
                {"review_id": "S001", "condition": "C", "run_id": "r0001", "case_id": "OFFLINE", "repetition": 1},
                {"review_id": "S002", "condition": "A", "run_id": "r0002", "case_id": "OFFLINE", "repetition": 1}]})
            self.put(exp / "review/S001/run_summary.json", {"execution_status": "completed", "conversion_status": "ok", "condition": "HIDDEN_CONDITION_SENTINEL", "raw_text": "PRIVATE_RAW_SENTINEL"})
            (exp / "review/S001/model.glb").write_bytes(b"OFFLINE_ANONYMOUS_GEOMETRY")
            (exp / "frozen").mkdir()
            (exp / "frozen/image.png").write_bytes(b"OFFLINE_IMAGE_BYTES")
        self.batch = {"id": "fixture", "models": self.models, "model_count": 2}
        batch_file = self.package / "batches/fixture.json"
        self.put(batch_file, self.batch)
        batch_file.with_suffix(".sha256").write_text(engine.sha(batch_file), encoding="ascii")
        self.put(self.package / "config.json", {"cases": [{"case_id": "OFFLINE"}]})
        # The real store needs Windows DPAPI; elsewhere an in-memory store keeps the launch path testable.
        store = None if sys.platform == "win32" else OfflineCredentialStore()
        self.app = app_service.Application(self.package, app_dir=APP, jobs_dir=self.root / "jobs", credential_store=store)
        self.processes = []
        self.fail_models = set()
        self.env_guard = mock.patch.dict(os.environ, {"PATH": "OFFLINE_SAFE_PATH", "OPENAI_API_KEY": "AMBIENT_KEY_SENTINEL", "ANTHROPIC_API_KEY": "AMBIENT_CLAUDE_SENTINEL", "OTHER_TOKEN": "AMBIENT_TOKEN_SENTINEL"}, clear=True)
        self.env_guard.start()
        self.no_paid = mock.patch.object(providers, "send_request", side_effect=AssertionError("Paid transport forbidden"))
        self.paid = self.no_paid.start()
        self.no_process = mock.patch.object(app_service.subprocess, "Popen", side_effect=self.fake_popen)
        self.popen = self.no_process.start()
        self.http = server.create_server(self.app, port=0)
        self.server_thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.server_thread.start()
        self.host = "127.0.0.1:" + str(self.http.server_port)
        self.origin = "http://" + self.host

    def tearDown(self):
        for process in self.processes:
            process.release.set()
        self.wait_for(lambda: not self.app.is_busy())
        self.http.shutdown()
        self.http.server_close()
        self.server_thread.join(2)
        self.paid.assert_not_called()
        self.no_process.stop()
        self.no_paid.stop()
        self.env_guard.stop()
        self.temp.cleanup()

    def put(self, file, value):
        local_common.write_json(file, value)

    def fake_popen(self, args, **kwargs):
        exp = args[args.index("--experiment") + 1] if "--experiment" in args else None
        process = FakeProcess(args, kwargs, exp in self.fail_models)
        self.processes.append(process)
        return process

    def wait_for(self, check, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if check():
                return True
            time.sleep(.01)
        return bool(check())

    def passed(self):
        self.app.integrity = {m["experiment_id"]: {"passed": True} for m in self.models}

    def request(self, method, path, value=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else b"")
        request_headers = {"Host": self.host}
        if method == "POST":
            request_headers.update({"Origin": self.origin, "X-CSRF-Token": self.app.csrf_token, "Content-Type": "application/json"})
        for key, val in (headers or {}).items():
            if val is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = val
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_01_host_origin_csrf_and_response_security_headers(self):
        self.assertEqual(self.http.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf_token"], self.app.csrf_token)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for host in ("attacker.invalid", self.host + ".attacker.invalid", "localhost", "127.0.0.1:1"):
            self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": host})[0], 403)
        for origin in (None, "null", "https://attacker.invalid", self.origin + ".attacker.invalid"):
            self.assertEqual(self.request("POST", "/api/check", {"batch_id": "fixture"}, headers={"Origin": origin})[0], 403)
        for token in (None, "incorrect"):
            self.assertEqual(self.request("POST", "/api/check", {"batch_id": "fixture"}, headers={"X-CSRF-Token": token})[0], 403)
        self.assertEqual(self.request("OPTIONS", "/api/run")[0], 403)
        self.popen.assert_not_called()

    def test_02_duplicate_headers_bad_json_and_unknown_fields_are_rejected(self):
        for name, value in (("Host", self.host), ("Origin", self.origin), ("X-CSRF-Token", self.app.csrf_token), ("Content-Length", "2")):
            conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
            try:
                conn.putrequest("POST", "/api/check", skip_host=True)
                for key, val in {"Host": self.host, "Origin": self.origin, "X-CSRF-Token": self.app.csrf_token, "Content-Length": "2", "Content-Type": "application/json"}.items():
                    conn.putheader(key, val)
                conn.putheader(name, value)
                conn.endheaders(b"{}")
                response = conn.getresponse()
                self.assertIn(response.status, (400, 403))
                response.read()
            finally:
                conn.close()
        for raw in (b'{"batch_id":"fixture","batch_id":"fixture"}', b'{"batch_id":NaN}', b'[]', b'not-json'):
            self.assertEqual(self.request("POST", "/api/check", raw=raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/check", {"batch_id": "fixture", "endpoint": "https://evil.invalid"})[0], 400)
        self.assertEqual(self.request("POST", "/api/check?x=1", {"batch_id": "fixture"})[0], 400)
        self.assertEqual(self.request("POST", "/api/check", {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/check", raw=b"x" * 16385)[0], 413)
        self.popen.assert_not_called()

    def test_03_conditions_are_visible_while_raw_files_and_paths_stay_protected(self):
        for path in ("/api/bootstrap", "/api/status?batch_id=fixture", "/files/fixture_m01/S001/model.glb"):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertNotIn(b"HIDDEN_CONDITION_SENTINEL", body)
            self.assertNotIn(b"PRIVATE_RAW_SENTINEL", body)
        for path in ("/api/mapping?batch_id=fixture", "/experiments/fixture_m01/admin/review_key.json",
                     "/files/fixture_m01/S001/run_summary.json", "/files/fixture_m01/../../admin/review_key.json",
                     "/files/fixture_m01/%2e%2e/admin/review_key.json", "/%2e%2e/config.json", "/config.json"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/api/status?batch_id=..%2Ffixture")[0], 400)
        self.assertEqual(self.request("GET", "http://attacker.invalid/api/bootstrap")[0], 400)
        self.assertEqual(self.request("GET", "/api/source?experiment_id=fixture_m01")[2], b"OFFLINE_IMAGE_BYTES")
        public = json.loads(self.request("GET", "/api/status?batch_id=fixture")[2])
        self.assertEqual(public['review_mode'], 'conditions_visible')
        self.assertEqual(public['models'][0]['review']['slots'][0]['condition'], 'C')
        self.assertEqual(public['models'][0]['review']['slots'][0]['run_id'], 'r0001')
        status, _, body = self.request("POST", "/api/mapping", {"batch_id": "fixture"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['slots'][0]['condition'], 'C')
        self.assertNotIn(b"PRIVATE_RAW_SENTINEL", body)
        self.popen.assert_not_called()

    def test_04_missing_any_key_and_integrity_lock_gates_launch_nothing(self):
        body = {"batch_id": "fixture", "keys": dict(KEYS), "max_new_slots": 1}
        self.assertEqual(self.request("POST", "/api/run", body)[0], 409)
        self.passed()
        for keys in ({"openai": KEYS["openai"]}, {"anthropic": KEYS["anthropic"]}, {**KEYS, "anthropic": "bad key"}, {**KEYS, "other": "x"}):
            self.assertEqual(self.request("POST", "/api/run", {**body, "keys": keys})[0], 400)
        exp = self.package / "experiments/fixture_m01"
        (exp / "run.lock").write_text("offline")
        self.assertEqual(self.request("POST", "/api/run", body)[0], 409)
        (exp / "run.lock").unlink()
        self.put(exp / "admin/protocol_violation.json", {"reason": "fixture"})
        self.put(self.package / "experiments/fixture_m02/admin/protocol_violation.json", {"reason": "fixture"})
        self.assertEqual(self.request("POST", "/api/run", body)[0], 409)
        self.popen.assert_not_called()

    def test_05_concurrent_workers_get_only_own_key_and_one_failure_is_isolated(self):
        self.passed()
        self.fail_models.add("fixture_m01")
        status, _, body = self.request("POST", "/api/run", {"batch_id": "fixture", "keys": dict(KEYS), "max_new_slots": 1})
        self.assertEqual(status, 202)
        self.assertEqual(len(json.loads(body)["job_ids"]), 2)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 2 and all(p.stdin.closed for p in self.processes)))
        self.assertEqual(sum(j["status"] == "running" for j in self.app.jobs.values()), 2)
        for process in self.processes:
            exp_id = process.args[process.args.index("--experiment") + 1]
            provider = next(row["provider"] for row in self.models if row["experiment_id"] == exp_id)
            self.assertEqual(json.loads(process.stdin.saved), {"api_key": KEYS[provider], "max_new_slots": 1})
            self.assertEqual(process.kwargs["stderr"], app_service.subprocess.DEVNULL)
            self.assertEqual(process.kwargs["env"]["PATH"], "OFFLINE_SAFE_PATH")
            for secret in [*KEYS.values(), "AMBIENT_KEY_SENTINEL", "AMBIENT_CLAUDE_SENTINEL", "AMBIENT_TOKEN_SENTINEL"]:
                self.assertNotIn(secret, repr(process.args))
                self.assertNotIn(secret, repr(process.kwargs["env"]))
            process.release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        states = {j["experiment_id"]: j["status"] for j in self.app.jobs.values()}
        self.assertEqual(states, {"fixture_m01": "failed", "fixture_m02": "completed"})
        public = self.request("GET", "/api/status?batch_id=fixture")[2]
        saved = b"".join(p.read_bytes() for p in self.root.rglob("*.json"))
        for secret in [*KEYS.values(), "AMBIENT_KEY_SENTINEL", "AMBIENT_CLAUDE_SENTINEL", "AMBIENT_TOKEN_SENTINEL", "MUST_NOT_PERSIST"]:
            self.assertNotIn(secret.encode(), public + saved)

    def test_06_individual_and_group_stop_preserve_other_worker_and_busy_gate(self):
        self.passed()
        self.assertEqual(self.request("POST", "/api/run", {"batch_id": "fixture", "keys": dict(KEYS), "max_new_slots": 0})[0], 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 2))
        for route in ("check", "run", "review", "shutdown"):
            self.assertEqual(self.request("POST", "/api/" + route, {} if route == "shutdown" else {"batch_id": "fixture"})[0], 409)
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(self.request("POST", "/api/stop", {"batch_id": "fixture", "experiment_id": "fixture_m01"})[0], 200)
        jobs = {j["experiment_id"]: j for j in self.app.jobs.values()}
        self.assertEqual(jobs["fixture_m01"]["status"], "stopping")
        self.assertEqual(jobs["fixture_m02"]["status"], "running")
        self.assertTrue((self.app.jobs_dir / jobs["fixture_m01"]["id"] / "stop.requested").exists())
        self.assertFalse((self.app.jobs_dir / jobs["fixture_m02"]["id"] / "stop.requested").exists())
        self.assertEqual(self.request("POST", "/api/stop", {"batch_id": "fixture"})[0], 200)
        self.assertTrue(all(j["status"] == "stopping" for j in jobs.values()))
        self.assertTrue(all((self.app.jobs_dir / j["id"] / "stop.requested").exists() for j in jobs.values()))

    def test_07_completed_models_skip_key_requirement_and_check_has_no_credentials(self):
        exp = self.package / "experiments/fixture_m01"
        for ident in ("r0001", "r0002"):
            self.put(exp / "runs" / ident / "attempt.json", {"offline": True})
            self.put(exp / "runs" / ident / "result.json", {"status": "completed"})
        self.passed()
        status = self.request("POST", "/api/run", {"batch_id": "fixture", "keys": {"anthropic": KEYS["anthropic"]}, "max_new_slots": 1})[0]
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 1 and self.processes[0].stdin.closed))
        self.assertIn("fixture_m02", self.processes[0].args)
        self.processes[0].release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertEqual(self.request("POST", "/api/check", {"batch_id": "fixture"})[0], 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 3 and all(p.stdin.closed for p in self.processes)))
        for process in self.processes[1:]:
            self.assertEqual(json.loads(process.stdin.saved), {})
            process.release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertTrue(all(self.app.integrity[m["experiment_id"]]["passed"] for m in self.models))

    def test_08_prepare_validates_models_and_reserved_ids_before_start(self):
        body = {"batch_id": "newbatch", "models": copy.deepcopy(SELECTIONS), "repetitions": 1, "max_output_tokens": 4096, "auto_continue_limit": 2}
        for changes in ({"models": [SELECTIONS[0], SELECTIONS[0]]}, {"models": []}, {"repetitions": True}, {"auto_continue_limit": 6}, {"max_output_tokens": 128001}):
            self.assertEqual(self.request("POST", "/api/prepare", {**body, **changes})[0], 400)
        self.popen.assert_not_called()
        self.assertEqual(self.request("POST", "/api/prepare", body)[0], 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 1 and self.processes[0].stdin.closed))
        request = json.loads(self.processes[0].stdin.saved)
        self.assertEqual(request["models"], SELECTIONS)
        self.assertNotIn("api_key", request)
        self.processes[0].release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertEqual(self.request("POST", "/api/prepare", body)[0], 409)

    def test_09_source_containment_blocks_absolute_and_parent_escape(self):
        outside = self.root / "private.png"
        outside.write_bytes(b"PRIVATE_OUTSIDE_BYTES")
        file = self.package / "experiments/fixture_m01/config.json"
        cfg = local_common.read_json(file)
        for path in (str(outside), "../../../private.png"):
            cfg["cases"][0]["images"] = [path]
            self.put(file, cfg)
            result = self.request("GET", "/api/source?experiment_id=fixture_m01")
            self.assertEqual(result[0], 403)
            self.assertNotIn(b"PRIVATE_OUTSIDE_BYTES", result[2])

    def test_13_protocol_deviation_excludes_only_that_model_and_its_provider_key(self):
        excluded = self.package / "experiments/fixture_m01"
        eligible = self.package / "experiments/fixture_m02"
        self.put(excluded / "admin/protocol_violation.json", {"reason": "returned_model_changed", "offline_fixture_only": True})
        for exp, status in ((excluded, "protocol_deviation"), (eligible, "http_error")):
            self.put(exp / "runs/r0001/attempt.json", {"offline_fixture_only": True})
            self.put(exp / "runs/r0001/result.json", {"status": status})
        # Both still have an unattempted slot. Only the eligible model needs
        # a passed check and its own provider key when the batch is resumed.
        self.app.integrity = {"fixture_m02": {"passed": True}}
        tracked = list(excluded.rglob("*")) + list((eligible / "runs").rglob("*"))
        before = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in tracked if file.is_file()}
        status, _, body = self.request("POST", "/api/run", {
            "batch_id": "fixture", "keys": {"anthropic": KEYS["anthropic"]}, "max_new_slots": 1})
        self.assertEqual(status, 202)
        self.assertEqual(len(json.loads(body)["job_ids"]), 1)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 1 and self.processes[0].stdin.closed))
        process = self.processes[0]
        self.assertIn("fixture_m02", process.args)
        self.assertNotIn("fixture_m01", process.args)
        self.assertEqual(json.loads(process.stdin.saved), {"api_key": KEYS["anthropic"], "max_new_slots": 1})
        process.release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(before, {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in before})
        models = json.loads(self.request("GET", "/api/status?batch_id=fixture")[2])["models"]
        excluded_status = next(model for model in models if model["experiment"]["id"] == "fixture_m01")
        self.assertTrue(excluded_status["protocol_deviation"])
        self.assertEqual(excluded_status["progress"]["remaining"], 1)
        self.assertIsNone(excluded_status["job"])


    def test_14_failed_preparation_releases_the_batch_name_and_removes_only_its_own_experiment_folders(self):
        body = {"batch_id": "newbatch", "models": copy.deepcopy(SELECTIONS), "repetitions": 1, "max_output_tokens": 4096, "auto_continue_limit": 2}
        older = self.package / "experiments/newbatch_m02"
        self.put(older / "config.json", {"offline_fixture_only": True})
        self.fail_models.add(None)
        self.assertEqual(self.request("POST", "/api/prepare", body)[0], 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 1 and self.processes[0].stdin.closed))
        orphan = self.package / "experiments/newbatch_m01"
        self.put(orphan / "config.json", {"offline_fixture_only": True})
        self.assertTrue((self.package / "batches/newbatch.reserved").is_file())
        self.processes[0].release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertEqual(self.app.preparation["status"], "failed")
        self.assertFalse((self.package / "batches/newbatch.reserved").exists())
        self.assertFalse(orphan.exists())
        self.assertTrue((older / "config.json").is_file())
        self.fail_models.clear()
        self.assertEqual(self.request("POST", "/api/prepare", body)[0], 202)
        self.assertTrue(self.wait_for(lambda: len(self.processes) == 2 and self.processes[1].stdin.closed))
        self.processes[1].release.set()
        self.assertTrue(self.wait_for(lambda: not self.app.is_busy()))
        self.assertEqual(self.app.preparation["status"], "completed")
        self.assertTrue((older / "config.json").is_file())


    def test_15_reparse_points_that_do_not_redirect_the_path_keep_the_store_usable(self):
        import credential_store
        directory, link = stat.S_IFDIR, stat.S_IFLNK
        cases = [
            (types.SimpleNamespace(st_mode=directory), False),
            (types.SimpleNamespace(st_mode=link), True),
            # Cloud placeholder (OneDrive Files On-Demand) and deduplicated file: reparse points that resolve in place.
            (types.SimpleNamespace(st_mode=directory, st_file_attributes=0x400, st_reparse_tag=0x9000001A), False),
            (types.SimpleNamespace(st_mode=directory, st_file_attributes=0x410, st_reparse_tag=0x9000301A), False),
            (types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400, st_reparse_tag=0x80000013), False),
            # Junction/mount point and symbolic link tags are name surrogates.
            (types.SimpleNamespace(st_mode=directory, st_file_attributes=0x400, st_reparse_tag=0xA0000003), True),
            (types.SimpleNamespace(st_mode=directory, st_file_attributes=0x400, st_reparse_tag=0xA000000C), True),
            # A reparse point whose tag is unavailable stays rejected.
            (types.SimpleNamespace(st_mode=directory, st_file_attributes=0x400), True),
        ]
        for info, expected in cases:
            with self.subTest(info=vars(info)):
                self.assertEqual(credential_store._linked(info), expected)


class WorkerAndBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="offline_multi_worker_", dir=HERE)
        self.root = Path(self.temp.name)
        self.no_paid = mock.patch.object(providers, "send_request", side_effect=AssertionError("Paid transport forbidden"))
        self.paid = self.no_paid.start()
        self.no_process = mock.patch("subprocess.Popen", side_effect=AssertionError("Real process forbidden"))
        self.popen = self.no_process.start()
        # Python's platform.platform() can spawn Windows `ver`; keep even that
        # process out of this suite and use an explicit synthetic runtime.
        self.runtime = mock.patch.object(engine, "runtime_info", return_value={
            "python": "offline-fixture", "platform": "offline-fixture", "packages": {}})
        self.runtime.start()

    def tearDown(self):
        try:
            self.paid.assert_not_called()
            self.popen.assert_not_called()
        finally:
            self.no_paid.stop()
            self.no_process.stop()
            self.runtime.stop()
            self.temp.cleanup()

    def fixture(self):
        package = self.root / "package"
        package.mkdir()
        image = package / "offline.png"
        image.write_bytes(b"OFFLINE_IMAGE_NOT_A_REAL_DRAWING")
        cfg = engine.read_json(PACKAGE / "config.json")
        for key in ("knowledge_source", "common_instruction", "output_schema", "geometry_contract", "backend_dir"):
            cfg[key] = str(PACKAGE / cfg[key])
        for case in cfg["cases"]:
            for key in ("scope", "text", "provenance"):
                case[key] = str(PACKAGE / case[key])
            case["images"] = [str(image)]
        engine.write_json(package / "config.json", cfg)
        job = self.root / "jobs"
        job.mkdir()
        request = {"models": copy.deepcopy(SELECTIONS), "repetitions": 1, "max_output_tokens": 4096, "auto_continue_limit": 2}
        batch_protocol.prepare_batch(package, "fixture", request, job)
        return package, job

    def test_10_batch_freezes_equal_inputs_and_rejects_changed_inputs_or_model(self):
        package, _ = self.fixture()
        self.assertEqual(batch_protocol.verify_batch(package, "fixture"), {"passed": True, "models": 2})
        batch = batch_protocol.read_batch(package, "fixture")
        a, b = [package / "experiments" / row["experiment_id"] for row in batch["models"]]
        self.assertEqual(batch_protocol.common_fingerprint(a), batch_protocol.common_fingerprint(b))
        self.assertEqual(engine.read_json(a / "schedule.json"), engine.read_json(b / "schedule.json"))
        cases = engine.read_json(a / "config.json")["cases"]
        for case in cases:
            for condition in "ABC":
                file = "frozen/cases/" + case["case_id"] + "/" + condition + "_prompt.txt"
                self.assertEqual((a / file).read_bytes(), (b / file).read_bytes())
        cfg_file = a / "config.json"
        original = cfg_file.read_bytes()
        cfg = engine.read_json(cfg_file)
        cfg["model"] = "gpt-5.6-sol"
        engine.write_json(cfg_file, cfg)
        with self.assertRaises(ValueError):
            batch_protocol.verify_batch(package, "fixture")
        cfg_file.write_bytes(original)
        image = next((a / "frozen/cases" / cases[0]["case_id"]).glob("image_*.png"))
        image.write_bytes(b"CHANGED_OFFLINE_FIXTURE")
        with self.assertRaises(ValueError):
            batch_protocol.verify_batch(package, "fixture")

    def run_worker(self, package, job, action="run", exp="fixture_m01", outcome=None):
        args = ["worker.py", "--package", str(package), "--action", action, "--batch", "fixture", "--experiment", exp, "--job-dir", str(job)]
        request = {"api_key": KEYS["openai"], "max_new_slots": 1} if action == "run" else {}
        capture = io.StringIO()
        fake_run = mock.Mock(return_value=outcome or {"status": "stopped_after_current_request", "new_calls": 1})
        # Frozen module dispatch is covered in the version compatibility suite;
        # keep this unit test's runner/key boundary in the current mocked module.
        with mock.patch.object(sys, "argv", args), mock.patch.object(sys, "stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(capture), mock.patch.object(frozen_runtime, "load_in_worker", return_value=engine), mock.patch.object(engine, "run_batch", fake_run), mock.patch.object(worker, "export_review", return_value={"status": "ok"}) as export:
            code = worker.main()
        return code, capture.getvalue(), fake_run, export

    def test_11_worker_uses_own_stdin_key_limit_and_stop_boundary_without_logging_key(self):
        package, job = self.fixture()
        code, output, run, export = self.run_worker(package, job)
        self.assertEqual(code, 0)
        args, kwargs = run.call_args
        self.assertEqual(args, (package / "experiments/fixture_m01",))
        self.assertEqual(kwargs, {"api_key": KEYS["openai"], "max_new_calls": 1, "stop_file": job / "stop.requested"})
        self.assertEqual(export.call_count, 1)
        self.assertNotIn(KEYS["openai"], output + (job / "result.json").read_text(encoding="utf-8"))
        self.assertIn("stopped_after_current_request", output)

    def test_12_worker_check_and_wrong_experiment_never_call_runner(self):
        package, job = self.fixture()
        code, _, run, export = self.run_worker(package, job, action="check")
        self.assertEqual(code, 0)
        run.assert_not_called()
        export.assert_not_called()
        code, output, run, _ = self.run_worker(package, job, exp="other_experiment")
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertNotIn(KEYS["openai"], output + (job / "result.json").read_text(encoding="utf-8"))
        self.assertNotIn("Traceback", output)
        for text in (KEYS["openai"], "api key " + KEYS["openai"], "Runner code differs " + KEYS["openai"]):
            self.assertNotIn(KEYS["openai"], worker.safe_error(ValueError(text)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
