#!/bin/bash
# Polymarket观测站入口；从仓库自身位置解析路径。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$SYSTEM_DIR" || exit 1

if [[ ! -x "$PYTHON" ]]; then
    echo "缺少项目 Python 环境：$PYTHON" >&2
    exit 127
fi

exec "$PYTHON" "$SCRIPT_DIR/event_radar.py" "$@"
