#!/bin/bash
# 独立事件雷达入口；不继承交互式 shell 环境，也不访问 Desktop 真实目录。
set -uo pipefail

SYSTEM_DIR="/Users/tera/Projects/toushi-system"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$SYSTEM_DIR" || exit 1

if [[ ! -x "$PYTHON" ]]; then
    echo "缺少项目 Python 环境：$PYTHON" >&2
    exit 127
fi

exec "$PYTHON" "$SYSTEM_DIR/Scripts/event_radar.py" "$@"
