#!/bin/sh
# verify 服务入口：代码测试 + 构建检查 + HTTP 冒烟，随后以退出码报告。
set -e

if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

echo "== [1/3] 构建检查：字节码编译 =="
"$PY" -m compileall -q app tests scripts

echo "== [2/3] 单元测试（一层分辨树、不可辨信念、稳定裁决、取消、API）=="
"$PY" -m unittest discover -s tests -v

echo "== [3/3] HTTP 冒烟（WEB_URL=${WEB_URL:-http://web:8000}）=="
"$PY" scripts/smoke.py

echo "== verify 全部通过 =="
