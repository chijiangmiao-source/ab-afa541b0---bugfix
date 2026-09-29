"""HTTP 冒烟测试：在运行中的容器/进程上验证页面与 API。

覆盖：
* /healthz 与首页可达；
* 一层分辨树：两个初态在同一命令下得到不同响应 → 深度 1、按响应分支收敛；
* 不可辨信念：同响应汇流 → indistinguishable 且给出不可辨信念。

成功退出码 0，任一断言失败退出码 1。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

WEB_URL = os.environ.get("WEB_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "10"))


def request(method, path, body=None, headers=None):
    data = None
    hdrs = headers or {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs = {"Content-Type": "application/json", **hdrs}
    req = urllib.request.Request(WEB_URL + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else None


def wait_for(url, attempts=50):
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.4)
    return False


def check(spec, draft):
    code, data = request("POST", "/api/check", {"draft_id": draft, "spec": spec})
    assert code == 202, f"提交失败: HTTP {code} {data}"
    job_id = data["job_id"]
    for _ in range(50):
        code, out = request("GET", f"/api/jobs/{job_id}?wait=1",
                            headers={"X-Draft-Id": draft})
        assert code == 200, f"轮询失败: HTTP {code}"
        if out["status"] == "done":
            return out["result"]
        assert out["status"] == "running", f"意外状态 {out['status']}"
        time.sleep(0.1)
    raise AssertionError("任务超时未完成")


DEPTH_ONE = {
    "states": ["S0", "S1"],
    "commands": ["a"],
    "candidates": ["S0", "S1"],
    "transitions": {
        "S0": {"a": {"next": "S0", "response": "X"}},
        "S1": {"a": {"next": "S1", "response": "Y"}},
    },
}

INDISTINGUISHABLE = {
    "states": ["S0", "S1"],
    "commands": ["a"],
    "candidates": ["S0", "S1"],
    "transitions": {
        "S0": {"a": {"next": "S0", "response": "R"}},
        "S1": {"a": {"next": "S0", "response": "R"}},
    },
}


def main():
    if not wait_for(WEB_URL + "/healthz"):
        print(f"冒烟失败：{WEB_URL}/healthz 不可达", file=sys.stderr)
        return 1
    code, health = request("GET", "/healthz")
    assert code == 200 and health.get("ok") is True

    with urllib.request.urlopen(WEB_URL + "/", timeout=TIMEOUT) as resp:
        html = resp.read().decode("utf-8")
    assert "阀组" in html and "app.js" in html, "首页内容缺失"

    # 一层分辨树。
    r1 = check(DEPTH_ONE, "smoke-depth-one")
    assert r1["status"] == "distinguishable", r1["status"]
    assert r1["worst_case_depth"] == 1, r1["worst_case_depth"]
    root = r1["tree"]
    assert root["type"] == "command" and root["command"] == "a"
    assert [(b["response"], b["node"]["initial"], b["node"]["type"])
            for b in root["branches"]] == [
        ("X", "S0", "resolved"),
        ("Y", "S1", "resolved"),
    ], "深度一响应分支与初态对应错误"

    # 不可辨信念。
    r2 = check(INDISTINGUISHABLE, "smoke-indist")
    assert r2["status"] == "indistinguishable"
    assert r2["worst_case_depth"] is None
    assert len(r2["ambiguous_beliefs"]) >= 1
    # 汇流一步后必须出现“重新混淆”信念。
    merged = [b for b in r2["beliefs"] if b["merged"]]
    assert merged, "缺少重新混淆信念"
    amb = r2["tree"]
    assert amb["type"] == "ambiguous"
    assert amb["branches"][0]["command"] == "a"

    # 陈旧草稿防护：409 且不返回结果。
    _, sub = request("POST", "/api/check",
                     {"draft_id": "smoke-stale", "spec": DEPTH_ONE})
    code, polled = request("GET", f"/api/jobs/{sub['job_id']}",
                           headers={"X-Draft-Id": "a-different-draft"})
    assert code == 409 and polled.get("stale") is True
    assert "result" not in polled

    print("HTTP 冒烟全部通过：健康检查 / 首页 / 一层分辨树 / 不可辨信念 / 陈旧草稿防护")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"冒烟断言失败：{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"冒烟异常：{exc}", file=sys.stderr)
        sys.exit(1)
