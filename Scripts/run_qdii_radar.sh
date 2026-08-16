#!/bin/bash
# 纳指100 QDII 申购雷达入口。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$SYSTEM_DIR" || exit 1
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "找不到 Python 3；请先创建 .venv 或安装 python3。" >&2
  exit 127
fi

# Scheduled/real sends must pass the same secondary-source conflict gate as the
# manual publisher. A dry-run remains available for diagnostics without blocking
# on the current review queue.
if [[ " $* " != *" --dry-run "* && " $* " != *" --baseline "* ]]; then
  if ! "$PYTHON" "$SCRIPT_DIR/qdii_reference.py" --live; then
    echo "[WARN] 安信乐参考源抓取失败；主数据仍只采用官方来源。" >&2
  fi
  "$PYTHON" "$SCRIPT_DIR/review_qdii_before_push.py" --strict-reference
fi
exec "$PYTHON" "$SCRIPT_DIR/qdii_radar.py" --live "$@"
