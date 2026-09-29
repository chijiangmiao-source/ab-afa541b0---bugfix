"""HTTP 冒烟测试：在运行中的容器/进程上验证页面与 API。

覆盖：
* /healthz 与首页可达；
* 一层分辨树：两个初态在同一命令下得到不同响应 → 深度 1、按响应分支收敛；
* 不可辨信念：同响应汇流 → indistinguishable 且给出不可辨信念；
* 多路共同回执（五候选 A-E）：规范首命令 b，共同回执分支内
  候选初态与中继位置的关联必须正确，后续 z 回执归属随之正确。

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

# 五候选 A-E + 中继 X/Y/Z/U/V，命令 a/b/c/z。
# b：A、B、C 同一回执 r 但分别进入 Y、X、Z；D、E 立即得 d、e。
# a：A、B、C 同回执 r 进入 X、Y、Z；D、E 同回执 s 进入 U、V（需额外两步）。
# z：X/Y/Z 回 x/y/z，可一步分辨 {A,B,C}。c：ABC 汇流，不可取。
# 规范首命令必须是 b（唯一最坏两步），且共同回执分支关联必须是
# {A→Y, B→X, C→Z}，随后 z 的 y 定 A、x 定 B、z 定 C。
MULTI_COMMON = {
    "states": ["A", "B", "C", "D", "E", "X", "Y", "Z", "U", "V"],
    "commands": ["a", "b", "c", "z"],
    "candidates": ["A", "B", "C", "D", "E"],
    "transitions": {
        "A": {"a": {"next": "X", "response": "r"},
              "b": {"next": "Y", "response": "r"},
              "c": {"next": "A", "response": "p"},
              "z": {"next": "A", "response": "p"}},
        "B": {"a": {"next": "Y", "response": "r"},
              "b": {"next": "X", "response": "r"},
              "c": {"next": "A", "response": "p"},
              "z": {"next": "B", "response": "p"}},
        "C": {"a": {"next": "Z", "response": "r"},
              "b": {"next": "Z", "response": "r"},
              "c": {"next": "A", "response": "p"},
              "z": {"next": "C", "response": "p"}},
        "D": {"a": {"next": "U", "response": "s"},
              "b": {"next": "D", "response": "d"},
              "c": {"next": "D", "response": "t"},
              "z": {"next": "D", "response": "t"}},
        "E": {"a": {"next": "V", "response": "s"},
              "b": {"next": "E", "response": "e"},
              "c": {"next": "E", "response": "t"},
              "z": {"next": "E", "response": "t"}},
        "X": {"a": {"next": "X", "response": "r"},
              "b": {"next": "X", "response": "r"},
              "c": {"next": "X", "response": "r"},
              "z": {"next": "X", "response": "x"}},
        "Y": {"a": {"next": "Y", "response": "r"},
              "b": {"next": "Y", "response": "r"},
              "c": {"next": "Y", "response": "r"},
              "z": {"next": "Y", "response": "y"}},
        "Z": {"a": {"next": "Z", "response": "r"},
              "b": {"next": "Z", "response": "r"},
              "c": {"next": "Z", "response": "r"},
              "z": {"next": "Z", "response": "z"}},
        "U": {"a": {"next": "U", "response": "g"},
              "b": {"next": "U", "response": "g"},
              "c": {"next": "U", "response": "g"},
              "z": {"next": "X", "response": "w"}},
        "V": {"a": {"next": "V", "response": "g"},
              "b": {"next": "V", "response": "g"},
              "c": {"next": "V", "response": "g"},
              "z": {"next": "Y", "response": "w"}},
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

    # 多路共同回执：规范首命令 b，共同回执分支关联必须正确、可连续复算。
    r3 = check(MULTI_COMMON, "smoke-multi-common")
    assert r3["status"] == "distinguishable", r3["status"]
    assert r3["worst_case_depth"] == 2, r3["worst_case_depth"]
    root = r3["tree"]
    assert root["type"] == "command" and root["command"] == "b", \
        f"规范首命令应为 b，实为 {root.get('command')}"
    by_resp = {b["response"]: b for b in root["branches"]}
    assert [b["response"] for b in root["branches"]] == ["d", "e", "r"]
    # D、E 由不同回执立即确定。
    assert by_resp["d"]["node"]["initial"] == "D"
    assert by_resp["e"]["node"]["initial"] == "E"
    # 共同回执 r：A→Y、B→X、C→Z（A/B 位置绝不能对调）。
    shared = by_resp["r"]
    assert sorted(tuple(p) for p in shared["pairs"]) == [
        ("A", "Y"), ("B", "X"), ("C", "Z")], shared["pairs"]
    inner = shared["node"]
    assert inner["type"] == "command" and inner["command"] == "z"
    # 后续 z 回执归属：x 定 B、y 定 A、z 定 C。
    assert [(x["response"], x["node"]["initial"],
             [tuple(p) for p in x["pairs"]]) for x in inner["branches"]] == [
        ("x", "B", [("B", "X")]),
        ("y", "A", [("A", "Y")]),
        ("z", "C", [("C", "Z")]),
    ], inner["branches"]
    # 五个候选各成为恰好一个叶子。
    leaves = []

    def collect(node):
        if node["type"] == "resolved":
            leaves.append(node["initial"])
        for bch in node.get("branches", []):
            collect(bch.get("node", {}))
            for rsp in bch.get("responses", []):
                collect(rsp.get("node", {}))

    collect(root)
    assert sorted(leaves) == ["A", "B", "C", "D", "E"], leaves

    # 陈旧草稿防护：409 且不返回结果。
    _, sub = request("POST", "/api/check",
                     {"draft_id": "smoke-stale", "spec": DEPTH_ONE})
    code, polled = request("GET", f"/api/jobs/{sub['job_id']}",
                           headers={"X-Draft-Id": "a-different-draft"})
    assert code == 409 and polled.get("stale") is True
    assert "result" not in polled

    print("HTTP 冒烟全部通过：健康检查 / 首页 / 一层分辨树 / 不可辨信念 / "
          "多路共同回执关联 / 陈旧草稿防护")
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
