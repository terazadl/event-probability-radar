#!/bin/bash
# 生成全量 QDII 长图，更新公开 Release 图片，再通过 Server酱发送摘要 + 图片。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$SYSTEM_DIR/.venv/bin/python3"
EXPORT_DIR="$SYSTEM_DIR/exports"
IMAGE_PATH="$EXPORT_DIR/nasdaq100-qdii-radar.png"
REPORT_DATE="$(TZ=Asia/Shanghai date +%Y%m%d)"
VERSIONED_IMAGE_NAME="nasdaq100-qdii-radar-${REPORT_DATE}.png"
VERSIONED_IMAGE_PATH="$EXPORT_DIR/$VERSIONED_IMAGE_NAME"
REPOSITORY="terazadl/event-probability-radar"
RELEASE_TAG="qdii-latest"
VERSIONED_IMAGE_URL="https://github.com/$REPOSITORY/releases/download/$RELEASE_TAG/$VERSIONED_IMAGE_NAME"
REVIEW_ARGS=()
if [[ "${QDII_ALLOW_REVIEW_PENDING:-0}" == "1" ]]; then
  REVIEW_ARGS+=("--allow-review-pending")
fi

if [[ ! -x "$PYTHON" ]]; then PYTHON="$(command -v python3 || true)"; fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then echo "找不到 Python 3。" >&2; exit 127; fi
if ! command -v gh >/dev/null 2>&1; then echo "找不到 GitHub CLI，无法发布日报图片。" >&2; exit 127; fi

# 安信乐只作为二级发现源：先抓取并写入独立参考快照，再与本地官方口径
# 做差异审核。任何冲突默认阻断推送，避免把代销/场内口径当成直销额度。
if ! "$PYTHON" "$SCRIPT_DIR/qdii_reference.py" --live; then
  echo "[WARN] 安信乐参考源抓取失败；继续使用官方来源，但不会覆盖主数据。" >&2
fi
STRICT_REFERENCE="${QDII_STRICT_REFERENCE:-1}"
if [[ "$STRICT_REFERENCE" == "1" ]]; then
  "$PYTHON" "$SCRIPT_DIR/review_qdii_before_push.py" --strict-reference
else
  "$PYTHON" "$SCRIPT_DIR/review_qdii_before_push.py"
fi

bash "$SCRIPT_DIR/export_qdii_share.sh" "$EXPORT_DIR"
cp "$IMAGE_PATH" "$VERSIONED_IMAGE_PATH"

if ! gh release view "$RELEASE_TAG" --repo "$REPOSITORY" >/dev/null 2>&1; then
  gh release create "$RELEASE_TAG" --repo "$REPOSITORY" --target main \
    --title "Nasdaq-100 QDII daily snapshot" \
    --notes "Latest full-universe subscription-quota image used by the daily ServerChan digest."
fi
gh release upload "$RELEASE_TAG" "$IMAGE_PATH" "$VERSIONED_IMAGE_PATH" --repo "$REPOSITORY" --clobber

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

EXPECTED_HASH="$(sha256_file "$IMAGE_PATH")"
VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT
DOWNLOADED_PATH="$VERIFY_DIR/$VERSIONED_IMAGE_NAME"
VERIFIED=0
for _ in {1..10}; do
  if curl -L -fsS --max-time 10 "${VERSIONED_IMAGE_URL}?v=$(date +%s)" -o "$DOWNLOADED_PATH" \
      && [[ -s "$DOWNLOADED_PATH" ]] \
      && [[ "$(sha256_file "$DOWNLOADED_PATH")" == "$EXPECTED_HASH" ]]; then
    VERIFIED=1
    break
  fi
  sleep 2
done
if [[ "$VERIFIED" != "1" ]]; then
  echo "[BLOCK] GitHub Release 图片未通过内容 hash 校验，停止发送日报。" >&2
  exit 1
fi

QDII_PUBLIC_IMAGE_URL="$VERSIONED_IMAGE_URL" \
  exec "$PYTHON" "$SCRIPT_DIR/qdii_radar.py" --live --daily-now "${REVIEW_ARGS[@]+${REVIEW_ARGS[@]}}"
