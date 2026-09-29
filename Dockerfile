# 深海阀组控制器初态辨识页面 —— 零第三方依赖，仅需 Python 3.11 标准库。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUIET=1

WORKDIR /app

COPY app/ ./app/
COPY static/ ./static/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

# 镜像内自检：构建时跑一遍核心测试，坏镜像无法发布。
RUN chmod +x scripts/verify.sh && python -m compileall -q app tests scripts && \
    python -m unittest discover -s tests

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); \
sys.exit(0 if json.load(r).get('ok') else 1)"

CMD ["python", "-m", "app.server"]
