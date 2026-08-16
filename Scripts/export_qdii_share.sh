#!/bin/bash
# 生成 QDII 公开快照和全量长图；不推送，也不改状态。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
OUTPUT_DIR="${1:-$SYSTEM_DIR/exports}"
CARD_HTML="$SYSTEM_DIR/Reports/纳指100 QDII申购雷达·分享卡片.html"
CARD_PNG="$OUTPUT_DIR/nasdaq100-qdii-radar.png"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REVIEW_ARGS=()
if [[ "${QDII_ALLOW_REVIEW_PENDING:-0}" == "1" ]]; then
  REVIEW_ARGS+=("--allow-review-pending")
fi

if [[ ! -x "$PYTHON" ]]; then PYTHON="$(command -v python3 || true)"; fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then echo "找不到 Python 3；请先创建 .venv 或安装 python3。" >&2; exit 127; fi
if [[ ! -x "$CHROME" ]]; then echo "找不到 Google Chrome，无法生成图片。" >&2; exit 127; fi

"$PYTHON" "$SYSTEM_DIR/Scripts/qdii_radar.py" --live --export-share "${REVIEW_ARGS[@]+${REVIEW_ARGS[@]}}"
ROW_COUNT="$(awk '{count += gsub(/class="qdii-row /, "&")} END {print count + 0}' "$CARD_HTML")"
CARD_HEIGHT=$((580 + ROW_COUNT * 50))
if (( CARD_HEIGHT < 900 )); then CARD_HEIGHT=900; fi
if (( CARD_HEIGHT > 6000 )); then CARD_HEIGHT=6000; fi
TASK_TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TASK_TEMP_DIR"' EXIT
TEMP_CARD_PNG="$TASK_TEMP_DIR/qdii-radar.png"

"$CHROME" --headless=new --disable-background-networking --disable-component-update \
  --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size="1080,$CARD_HEIGHT" --user-data-dir="$TASK_TEMP_DIR/chrome" \
  --screenshot="$TEMP_CARD_PNG" "file://$CARD_HTML" &
CHROME_PID=$!
for _ in {1..30}; do [[ -s "$TEMP_CARD_PNG" ]] && break; sleep 1; done
if kill -0 "$CHROME_PID" 2>/dev/null; then kill "$CHROME_PID" 2>/dev/null || true; fi
wait "$CHROME_PID" 2>/dev/null || true
if [[ ! -s "$TEMP_CARD_PNG" ]]; then echo "Chrome 未能生成图片：$CARD_PNG" >&2; exit 1; fi
mkdir -p "$OUTPUT_DIR"
cp "$TEMP_CARD_PNG" "$CARD_PNG"
cp "$CARD_HTML" "$OUTPUT_DIR/nasdaq100-qdii-radar.html"
echo "分享图片：$CARD_PNG"
echo "分享 HTML：$OUTPUT_DIR/nasdaq100-qdii-radar.html"
