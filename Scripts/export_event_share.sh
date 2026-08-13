#!/bin/bash
# 生成公开快照和 1080×1350 分享图；不发送通知，也不改雷达状态。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
PUBLIC_HTML="$SYSTEM_DIR/Reports/事件概率雷达·公开快照.html"
CARD_HTML="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.html"
OUTPUT_DIR="${1:-$SYSTEM_DIR/exports}"
CARD_PNG="$OUTPUT_DIR/polymarket-observatory.png"
CHROME="${EVENT_RADAR_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目 Python 环境：$PYTHON" >&2
  exit 127
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
mkdir -p "$OUTPUT_DIR"
cp "$TEMP_CARD_PNG" "$CARD_PNG"
cp "$PUBLIC_HTML" "$OUTPUT_DIR/polymarket-observatory.html"

echo "公开页面：$OUTPUT_DIR/polymarket-observatory.html"
echo "分享图片：$CARD_PNG"
