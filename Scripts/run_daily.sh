#!/bin/bash
# launchd 调用的入口。launchd 不继承交互式 shell 的完整环境，
# 所以这里显式设置 PATH，并由 plist 把 stdout/stderr 写进日志。
set -uo pipefail

SYSTEM_DIR="/Users/tera/Projects/toushi-system"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$SYSTEM_DIR" || exit 1

if [[ ! -x "$PYTHON" ]]; then
    echo "缺少项目 Python 环境：$PYTHON" >&2
    echo "请先运行：/usr/bin/python3 -m venv \"$SYSTEM_DIR/.venv\"" >&2
    exit 127
fi

# SendKey 优先从 Scripts/.secrets.json 读，这里不放明文密钥。
exec "$PYTHON" "$SYSTEM_DIR/Scripts/run_daily.py" "$@"
