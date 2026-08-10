#!/bin/bash
# launchd 调用的入口。launchd 不继承交互式 shell 的完整环境，
# 所以这里显式设置 PATH，并由 plist 把 stdout/stderr 写进日志。
set -uo pipefail

VAULT="/Users/tera/Desktop/My pages/ObsidianVault"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$VAULT" || exit 1

# SendKey 优先从 Scripts/.secrets.json 读，这里不放明文密钥。
exec python3 "$VAULT/00 投研系统/Scripts/run_daily.py" "$@"
