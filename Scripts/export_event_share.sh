#!/bin/bash
# 生成公开快照并发布到 GitHub Pages；不发送通知，也不改雷达状态。
set -euo pipefail

SYSTEM_DIR="/Users/tera/Projects/toushi-system"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
BLOG_DIR="${1:-/Users/tera/Desktop/My pages/ObsidianVault/outputs/terazadl-publish}"
PUBLIC_HTML="$SYSTEM_DIR/Reports/事件概率雷达·公开快照.html"
CARD_HTML="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.html"
CARD_PNG="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.png"
PUBLISH_SCRIPT="$SYSTEM_DIR/Scripts/publish_event_share.sh"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目 Python 环境：$PYTHON" >&2
  exit 127
fi
if [[ ! -d "$BLOG_DIR/source" ]]; then
  echo "不是可用的 Hexo 博客目录：$BLOG_DIR" >&2
  exit 2
fi
if [[ ! -x "$PUBLISH_SCRIPT" ]]; then
  echo "缺少发布脚本：$PUBLISH_SCRIPT" >&2
  exit 127
fi

"$PYTHON" "$SYSTEM_DIR/Scripts/event_radar.py" --export-share

TERA_EVENT_BLOG_DIR="$BLOG_DIR" exec "$PUBLISH_SCRIPT" \
  "https://terazadl.github.io/images/event-radar-latest.png" \
  "$(date '+%Y%m%d%H%M')"
