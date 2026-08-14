#!/bin/bash
# 发布当前 Reports 中已生成的事件雷达页面和图片。
# 该脚本不重新抓取市场，确保图片与本次日报使用同一批数据。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BLOG_DIR="${TERA_EVENT_BLOG_DIR:-}"
BLOG_REPO_URL="${TERA_EVENT_BLOG_REPO_URL:-}"
BLOG_PAGES_BRANCH="${TERA_EVENT_BLOG_PAGES_BRANCH:-master}"
PAGES_REPO="${TERA_EVENT_PAGES_REPO:-$BLOG_REPO_URL}"
PUBLIC_HTML="$SYSTEM_DIR/Reports/事件概率雷达·公开快照.html"
CARD_HTML="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.html"
CARD_PNG="$SYSTEM_DIR/Reports/事件概率雷达·分享卡片.png"
CHROME="${EVENT_RADAR_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
IMAGE_URL="${1:-}"
CACHE_KEY="${2:-$(date '+%Y%m%d%H%M')}"

USER_HOME="${HOME:-}"
if [[ -n "${EVENT_RADAR_NODE_BIN:-}" ]]; then
  export PATH="$EVENT_RADAR_NODE_BIN:$PATH"
elif [[ -n "$USER_HOME" && -d "$USER_HOME/.local/bin" ]]; then
  export PATH="$USER_HOME/.local/bin:$PATH"
fi
NPM="$(command -v npm || true)"

if [[ ! -s "$PUBLIC_HTML" || ! -s "$CARD_HTML" ]]; then
  echo "缺少当前日报页面或分享卡片，请先生成 Reports 文件。" >&2
  exit 2
fi
if [[ -z "$BLOG_DIR" ]]; then
  echo "请设置 TERA_EVENT_BLOG_DIR 指向 Hexo 博客目录。" >&2
  exit 2
fi
if [[ -z "$BLOG_REPO_URL" ]]; then
  echo "请设置 TERA_EVENT_BLOG_REPO_URL，避免误推送到错误仓库。" >&2
  exit 2
fi
if [[ ! -d "$BLOG_DIR/source" ]]; then
  echo "不是可用的 Hexo 博客目录：$BLOG_DIR" >&2
  exit 2
fi
if [[ ! -x "$CHROME" ]]; then
  echo "找不到 Google Chrome，无法生成日报图片。" >&2
  exit 127
fi
if [[ -z "$NPM" ]]; then
  echo "找不到 npm，无法构建公开页面。" >&2
  exit 127
fi
if [[ -z "$IMAGE_URL" ]]; then
  echo "请提供公开图片 URL 作为第一个参数。" >&2
  exit 2
fi

task_temp_dir="$(mktemp -d /private/tmp/terazadl-event-share-XXXXXX)"
build_dir="$task_temp_dir/blog"
pages_dir="$task_temp_dir/pages-master"
cleanup() {
  rm -rf "$task_temp_dir"
}
trap cleanup EXIT

temp_card_png="$task_temp_dir/event-radar-latest.png"
"$CHROME" \
  --headless=new \
  --disable-background-networking \
  --disable-component-update \
  --disable-gpu \
  --hide-scrollbars \
  --force-device-scale-factor=1 \
  --window-size=1080,1350 \
  --user-data-dir="$task_temp_dir/chrome" \
  --screenshot="$temp_card_png" \
  "file://$CARD_HTML" &
chrome_pid=$!

for _ in {1..25}; do
  if [[ -s "$temp_card_png" ]]; then
    break
  fi
  sleep 1
done
if kill -0 "$chrome_pid" 2>/dev/null; then
  kill "$chrome_pid" 2>/dev/null || true
fi
wait "$chrome_pid" 2>/dev/null || true
if [[ ! -s "$temp_card_png" ]]; then
  echo "Chrome 未能生成日报图片。" >&2
  exit 1
fi
cp "$temp_card_png" "$CARD_PNG"

# launchd 可能没有 macOS Desktop 的自动化写入权限。复制到临时构建目录，
# 避免把日报发布绑定到直接改写 Desktop 下的 Hexo source/public。
mkdir -p "$build_dir"
rsync -a --exclude='.git' --exclude='.deploy_git' --exclude='public' \
  "$BLOG_DIR/" "$build_dir/"
mkdir -p "$build_dir/source/event-radar" "$build_dir/source/images"
cp "$PUBLIC_HTML" "$build_dir/source/event-radar/index.html"
cp "$CARD_PNG" "$build_dir/source/images/event-radar-latest.png"

if ! (cd "$build_dir" && "$NPM" run build); then
  echo "Hexo 构建失败，未发送日报。" >&2
  exit 1
fi
if [[ ! -s "$build_dir/public/event-radar/index.html" || ! -s "$build_dir/public/images/event-radar-latest.png" ]]; then
  echo "Hexo 构建后缺少事件雷达发布文件。" >&2
  exit 1
fi

if ! git -C "$BLOG_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$BLOG_DIR" remote add origin "$BLOG_REPO_URL"
else
  blog_origin="$(git -C "$BLOG_DIR" remote get-url origin)"
  if [[ "$blog_origin" != "$BLOG_REPO_URL" ]]; then
    echo "博客 origin 与 TERA_EVENT_BLOG_REPO_URL 不一致：$blog_origin" >&2
    exit 2
  fi
fi
git clone --quiet --branch "$BLOG_PAGES_BRANCH" --single-branch "$PAGES_REPO" "$pages_dir"
mkdir -p "$pages_dir/event-radar" "$pages_dir/images"
cp "$build_dir/public/event-radar/index.html" "$pages_dir/event-radar/index.html"
cp "$build_dir/public/images/event-radar-latest.png" "$pages_dir/images/event-radar-latest.png"

git -C "$pages_dir" add event-radar/index.html images/event-radar-latest.png
if git -C "$pages_dir" diff --cached --quiet; then
  echo "[SHARE] 公开文件没有变化，继续校验现有图片。"
else
  git -C "$pages_dir" commit -m "Refresh event radar daily snapshot"
  git -C "$pages_dir" push origin "HEAD:$BLOG_PAGES_BRANCH"
fi

separator='?'
if [[ "$IMAGE_URL" == *\?* ]]; then
  separator='&'
fi
probe_url="${IMAGE_URL}${separator}v=${CACHE_KEY}"
expected_hash="$(shasum -a 256 "$build_dir/public/images/event-radar-latest.png" | awk '{print $1}')"
probe_path="$task_temp_dir/public-image.png"
for _ in {1..24}; do
  if curl -fsSL --max-time 20 "$probe_url" -o "$probe_path"; then
    actual_hash="$(shasum -a 256 "$probe_path" | awk '{print $1}')"
    if [[ "$actual_hash" == "$expected_hash" ]]; then
      echo "[SHARE] GitHub Pages 图片已校验：$probe_url"
      exit 0
    fi
  fi
  sleep 5
done

echo "GitHub Pages 尚未返回本次新图片，已阻止发送日报：$probe_url" >&2
exit 1
