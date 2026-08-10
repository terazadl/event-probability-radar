#!/bin/bash
# launchd 调用的入口。存在的理由：launchd 的环境变量极简，
# 直接让它跑 python3 经常会因为 PATH 找不到解释器而静默失败。
set -uo pipefail

VAULT="/Users/tera/Desktop/My pages/ObsidianVault"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$VAULT" || exit 1

# SendKey 优先从 Scripts/.secrets.json 读，这里不放明文密钥。
exec python3 "$VAULT/00 投研系统/Scripts/run_daily.py" "$@"
