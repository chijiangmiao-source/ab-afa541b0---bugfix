"""零依赖 HTTP 服务：阀组初态自适应辨识页面 + 异步复核 API。

并发/陈旧结果防护：
* 每次“开始复核”生成 job；草稿以 draft_id 标识。
* 草稿在复核开始后被修改，前端换发新的 draft_id；旧任务即使晚完成，
  轮询时 draft_id 不匹配，服务端返回 superseded，前端不得采纳。
* 同一 draft_id 重复提交会令旧 job 取消并标记 superseded。
* 取消通过 threading.Event 传入求解器，求解器在枚举/分层中周期检查。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .ident import AnalysisLimit, Cancelled, SpecError, analyze_payload

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "static")

MAX_BODY = 4 * 1024 * 1024
JOB_RETENTION = 64


class Job:
    def __init__(self, job_id: str, draft_id: str, payload: dict):
        self.job_id = job_id
        self.draft_id = draft_id
        self.payload = payload
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.started_at = time.time()
        self.status = "running"  # running | done | error | cancelled | superseded
        self.result: dict | None = None
        self.error: str | None = None
        self._superseded = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _publish(self, status: str, **fields) -> None:
        # 被取代的任务不得再把结论写回。
        if self._superseded:
            self.status = "superseded"
        else:
            self.status = status
            for key, value in fields.items():
                setattr(self, key, value)
        self.done.set()

    def _run(self) -> None:
        try:
            result = analyze_payload(self.payload, self.cancel_event.is_set)
            self._publish("done", result=result)
        except Cancelled:
            self._publish("cancelled")
        except (SpecError, AnalysisLimit) as exc:
            self._publish("error", error=str(exc))
        except Exception as exc:  # pragma: no cover - 防御性
            self._publish("error", error=f"内部错误：{exc}")

    def cancel(self, supersede: bool = False) -> None:
        if supersede:
            self._superseded = True
            self.cancel_event.set()
            self.status = "superseded"
            self.done.set()
        elif self.status == "running":
            self.cancel_event.set()


class Registry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._by_draft: dict[str, str] = {}
        self._lock = threading.Lock()

    def submit(self, draft_id: str, payload: dict) -> Job:
        with self._lock:
            old_id = self._by_draft.get(draft_id)
            if old_id is not None:
                old = self._jobs.get(old_id)
                if old is not None:
                    # 同一草稿重新提交：旧任务无论是否已完成都标记为被取代，
                    # 其结论永不再返回给前端。
                    old.cancel(supersede=True)
            job = Job(uuid.uuid4().hex, draft_id, payload)
            self._jobs[job.job_id] = job
            self._by_draft[draft_id] = job.job_id
            if len(self._jobs) > JOB_RETENTION:
                # 清理最老且已结束的任务。
                for jid in list(self._jobs):
                    job = self._jobs[jid]
                    if job.status != "running":
                        self._jobs.pop(jid, None)
                        if len(self._jobs) <= JOB_RETENTION // 2:
                            break
            job.thread.start()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status == "running":
                job.cancel()
            return job


REGISTRY = Registry()


class Handler(BaseHTTPRequestHandler):
    server_version = "ValveIdent/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.environ.get("QUIET"):
            return
        super().log_message(fmt, *args)

    # ---------- 工具 ----------
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Content-Length 非法"})
            return None
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "请求体为空或过大"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "请求不是合法 JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "请求体必须是 JSON 对象"})
            return None
        return data

    def _serve_file(self, rel: str, content_type: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            self._json(404, {"error": "not found"})
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            self._json(200, {"ok": True})
        elif path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._serve_file("app.js", "application/javascript; charset=utf-8")
        elif path == "/style.css":
            self._serve_file("style.css", "text/css; charset=utf-8")
        elif path.startswith("/api/jobs/"):
            self._poll(path[len("/api/jobs/"):])
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/check":
            data = self._read_json()
            if data is None:
                return
            draft_id = data.get("draft_id")
            if not isinstance(draft_id, str) or not draft_id:
                self._json(400, {"error": "draft_id 缺失"})
                return
            spec = data.get("spec")
            if not isinstance(spec, dict):
                self._json(400, {"error": "spec 缺失"})
                return
            # 录入错误同步返回 400，不占用后台任务。
            try:
                from .ident import build_spec
                build_spec(spec)
            except SpecError as exc:
                self._json(400, {"error": str(exc)})
                return
            job = REGISTRY.submit(draft_id, spec)
            self._json(202, {"job_id": job.job_id, "draft_id": draft_id,
                             "status": "running"})
        elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path[len("/api/jobs/"):-len("/cancel")]
            job = REGISTRY.cancel(job_id)
            if job is None:
                self._json(404, {"error": "任务不存在"})
            else:
                self._json(200, {"job_id": job_id, "status": job.status})
        else:
            self._json(404, {"error": "not found"})

    def _poll(self, tail: str) -> None:
        job_id = tail
        wait = 0.0
        if "?" in job_id:
            job_id, query = job_id.split("?", 1)
            for kv in query.split("&"):
                if kv.startswith("wait="):
                    try:
                        wait = max(0.0, min(2.0, float(kv[5:])))
                    except ValueError:
                        pass
        job = REGISTRY.get(job_id)
        if job is None:
            self._json(404, {"error": "任务不存在"})
            return
        current_draft = self.headers.get("X-Draft-Id", "")
        if wait and job.status == "running":
            job.done.wait(timeout=wait)
        stale = bool(current_draft) and current_draft != job.draft_id
        if stale and job.status == "running":
            # 草稿已切换：服务器代为取消旧任务。
            job.cancel(supersede=True)
        resp: dict = {"job_id": job_id, "draft_id": job.draft_id,
                      "stale": stale, "status": job.status}
        if stale:
            self._json(409, resp)
            return
        if job.status == "done":
            resp["result"] = job.result
        elif job.status == "error":
            resp["error"] = job.error
        self._json(200, resp)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"阀组辨识服务监听 {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
