"""HTTP API 测试：提交、轮询、草稿陈旧防护、取消。"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import Handler


def _depth_one_spec():
    return {
        "states": ["S0", "S1"],
        "commands": ["a"],
        "candidates": ["S0", "S1"],
        "transitions": {
            "S0": {"a": {"next": "S0", "response": "X"}},
            "S1": {"a": {"next": "S1", "response": "Y"}},
        },
    }


def _indistinguishable_spec():
    return {
        "states": ["S0", "S1"],
        "commands": ["a"],
        "candidates": ["S0", "S1"],
        "transitions": {
            "S0": {"a": {"next": "S0", "response": "R"}},
            "S1": {"a": {"next": "S1", "response": "R"}},
        },
    }


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None, timeout=5):
        data = None
        hdrs = headers or {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs = {"Content-Type": "application/json", **hdrs}
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def submit(self, spec, draft_id="d1"):
        return self.request("POST", "/api/check",
                            {"draft_id": draft_id, "spec": spec})

    def wait_done(self, job_id, draft_id="d1"):
        for _ in range(50):
            code, data = self.request(
                "GET", f"/api/jobs/{job_id}?wait=1",
                headers={"X-Draft-Id": draft_id},
            )
            if data["status"] in ("done", "error", "cancelled", "superseded"):
                return code, data
            time.sleep(0.02)
        self.fail("任务未在限定时间内结束")


class TestApiFlow(ServerTestBase):
    def test_health_and_index(self):
        code, data = self.request("GET", "/healthz")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("阀组", html)
        self.assertEqual(resp.status, 200)

    def test_validation_error_is_400(self):
        spec = _depth_one_spec()
        spec["commands"] = []
        code, data = self.submit(spec)
        self.assertEqual(code, 400)
        self.assertIn("命令", data["error"])

    def test_depth_one_job(self):
        code, data = self.submit(_depth_one_spec())
        self.assertEqual(code, 202)
        job_id = data["job_id"]
        code, final = self.wait_done(job_id)
        self.assertEqual(code, 200)
        result = final["result"]
        self.assertEqual(result["status"], "distinguishable")
        self.assertEqual(result["worst_case_depth"], 1)
        root = result["tree"]
        self.assertEqual(root["command"], "a")
        # 深度一即按响应分支收敛到唯一初态。
        self.assertEqual(
            [(b["response"], b["node"]["initial"]) for b in root["branches"]],
            [("X", "S0"), ("Y", "S1")],
        )

    def test_indistinguishable_job(self):
        _, data = self.submit(_indistinguishable_spec(), draft_id="d2")
        _, final = self.wait_done(data["job_id"], draft_id="d2")
        result = final["result"]
        self.assertEqual(result["status"], "indistinguishable")
        self.assertIsNone(result["worst_case_depth"])
        self.assertTrue(result["ambiguous_beliefs"])
        self.assertEqual(result["tree"]["type"], "ambiguous")

    def test_stale_draft_poll_gets_409_and_result_not_adopted(self):
        _, data = self.submit(_depth_one_spec(), draft_id="draft-A")
        code, polled = self.wait_done(data["job_id"], draft_id="draft-A")
        self.assertEqual(code, 200)
        # 草稿已经更换：再轮询同一任务必须 409 + stale=True。
        code2, data2 = self.request(
            "GET", f"/api/jobs/{data['job_id']}",
            headers={"X-Draft-Id": "draft-B"},
        )
        self.assertEqual(code2, 409)
        self.assertTrue(data2["stale"])
        self.assertNotIn("result", data2)

    def test_resubmit_same_draft_supersedes_old_job(self):
        _, first = self.submit(_depth_one_spec(), draft_id="draft-X")
        _, second = self.submit(_depth_one_spec(), draft_id="draft-X")
        self.assertNotEqual(first["job_id"], second["job_id"])
        _, final2 = self.wait_done(second["job_id"], draft_id="draft-X")
        self.assertEqual(final2["status"], "done")
        # 旧任务即使查询也应已被取代。
        _, old = self.request("GET", f"/api/jobs/{first['job_id']}",
                              headers={"X-Draft-Id": "draft-X"})
        self.assertEqual(old["status"], "superseded")
        self.assertNotIn("result", old)

    def test_cancel_endpoint(self):
        _, data = self.submit(_depth_one_spec())
        code, _ = self.request("POST", f"/api/jobs/{data['job_id']}/cancel")
        self.assertIn(code, (200, 404))

    def test_unknown_job_404(self):
        code, _ = self.request("GET", "/api/jobs/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
