#!/bin/bash
# 生成公开快照，并导出到 Hexo 博客；不发送通知，也不改雷达状态。
set -euo pipefail

SYSTEM_DIR="/Users/tera/Projects/toushi-system"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
BLOG_DIR="${1:-/Users/tera/Desktop/My pages/ObsidianVault/outputs/terazadl-publish}"
PUBLIC_HTML="$SYSTEM_DIR/Reports/事件概率雷达·公开快照.html"
CARD_HTML="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.html"
CARD_PNG="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.png"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目 Python 环境：$PYTHON" >&2
  exit 127
fi
if [[ ! -d "$BLOG_DIR/source" ]]; then
  echo "不是可用的 Hexo 博客目录：$BLOG_DIR" >&2
  exit 2
fi
if [[ ! -x "$CHROME" ]]; then
  echo "找不到 Google Chrome，无法生成朋友圈图片。" >&2
  exit 127
fi

"$PYTHON" "$SYSTEM_DIR/Scripts/event_radar.py" --export-share

TASK_TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TASK_TEMP_DIR"' EXIT
TEMP_CARD_PNG="$TASK_TEMP_DIR/event-radar-latest.png"

"$CHROME" \
  --headless=new \
  --disable-background-networking \
  --disable-component-update \
  --disable-gpu \
  --hide-scrollbars \
  --force-device-scale-factor=1 \
  --window-size=1080,1350 \
  --user-data-dir="$TASK_TEMP_DIR/chrome" \
  --screenshot="$TEMP_CARD_PNG" \
  "file://$CARD_HTML" &
CHROME_PID=$!

# 某些 macOS Chrome 版本截图后仍保留后台进程；图片落盘后主动结束该临时实例。
for _ in {1..25}; do
  if [[ -s "$TEMP_CARD_PNG" ]]; then
    break
  fi
  sleep 1
done
if kill -0 "$CHROME_PID" 2>/dev/null; then
  kill "$CHROME_PID" 2>/dev/null || true
fi
wait "$CHROME_PID" 2>/dev/null || true
if [[ ! -s "$TEMP_CARD_PNG" ]]; then
  echo "Chrome 未能生成朋友圈图片：$CARD_PNG" >&2
  exit 1
fi
cp "$TEMP_CARD_PNG" "$CARD_PNG"

mkdir -p "$BLOG_DIR/source/event-radar" "$BLOG_DIR/source/images"
cp "$PUBLIC_HTML" "$BLOG_DIR/source/event-radar/index.html"
cp "$CARD_PNG" "$BLOG_DIR/source/images/event-radar-latest.png"

echo "博客页面已更新：$BLOG_DIR/source/event-radar/index.html"
echo "朋友圈图片已更新：$BLOG_DIR/source/images/event-radar-latest.png"
echo "下一步：在博客目录运行 npm run build 预览；确认后再提交发布。"
