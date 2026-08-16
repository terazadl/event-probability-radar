#!/bin/bash
# 发布当前 Reports 中已生成的事件雷达页面和图片。
# 该脚本不重新抓取市场，确保图片与本次日报使用同一批数据。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BLOG_DIR="${TERA_EVENT_BLOG_DIR:-}"
DEFAULT_REPO="https://github.com/terazadl/terazadl.github.io.git"
PAGES_REPO="${TERA_EVENT_PAGES_REPO:-$DEFAULT_REPO}"
BLOG_SOURCE_REPO="${TERA_EVENT_BLOG_REPO_URL:-$PAGES_REPO}"
BLOG_SOURCE_BRANCH="${TERA_EVENT_BLOG_SOURCE_BRANCH:-hexo-src}"
BLOG_PAGES_BRANCH="${TERA_EVENT_BLOG_PAGES_BRANCH:-master}"
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
GIT="$(command -v git || true)"

if [[ ! -s "$PUBLIC_HTML" || ! -s "$CARD_HTML" ]]; then
  echo "缺少当前日报页面或分享卡片，请先生成 Reports 文件。" >&2
  exit 2
fi
if [[ -n "$BLOG_DIR" && ! -d "$BLOG_DIR/source" ]]; then
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
if [[ -z "$GIT" ]]; then
  echo "找不到 git，无法克隆 Hexo 源码或 GitHub Pages 仓库。" >&2
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

# launchd 默认不读取 Desktop，而是从 GitHub 的 Hexo 源码分支递归克隆到临时目录。
# 这样构建过程不依赖 macOS TCC 对 Desktop 的授权，也不会遗漏主题子模块。
if [[ -n "$BLOG_DIR" ]]; then
  mkdir -p "$build_dir"
  rsync -a --exclude='.git' --exclude='.deploy_git' --exclude='public' \
    "$BLOG_DIR/" "$build_dir/"
else
  echo "[SHARE] 从 ${BLOG_SOURCE_REPO} 的 ${BLOG_SOURCE_BRANCH} 分支克隆 Hexo 源码。"
  "$GIT" clone --quiet --recurse-submodules --branch "$BLOG_SOURCE_BRANCH" \
    --single-branch "$BLOG_SOURCE_REPO" "$build_dir"
fi
if [[ ! -s "$build_dir/package.json" || ! -s "$build_dir/_config.yml" ]]; then
  echo "克隆的目录不是完整的 Hexo 源码：$BLOG_SOURCE_REPO#$BLOG_SOURCE_BRANCH" >&2
  exit 2
fi
if [[ ! -x "$build_dir/node_modules/.bin/hexo" ]]; then
  echo "[SHARE] 临时目录缺少 Hexo 依赖，执行 npm ci。"
  if ! (cd "$build_dir" && "$NPM" ci --no-audit --no-fund --ignore-scripts); then
    echo "Hexo 依赖安装失败，未发送日报。" >&2
    exit 1
  fi
fi
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

"$GIT" clone --quiet --branch "$BLOG_PAGES_BRANCH" --single-branch "$PAGES_REPO" "$pages_dir"
mkdir -p "$pages_dir/event-radar" "$pages_dir/images"
cp "$build_dir/public/event-radar/index.html" "$pages_dir/event-radar/index.html"
cp "$build_dir/public/images/event-radar-latest.png" "$pages_dir/images/event-radar-latest.png"

"$GIT" -C "$pages_dir" add event-radar/index.html images/event-radar-latest.png
if "$GIT" -C "$pages_dir" diff --cached --quiet; then
  echo "[SHARE] 公开文件没有变化，继续校验现有图片。"
else
  "$GIT" -C "$pages_dir" commit -m "Refresh event radar daily snapshot"
  "$GIT" -C "$pages_dir" push origin "HEAD:$BLOG_PAGES_BRANCH"
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
