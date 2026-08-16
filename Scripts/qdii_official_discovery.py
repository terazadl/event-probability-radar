#!/usr/bin/env python3
"""从基金管理人官网公告页发现 QDII 候选来源。

本脚本是“发现层”，不会自动改写 Config/qdii_universe_template.csv，也不会发送通知。
候选结果写到 Reports/qdii-official-discovery.json，后续解析器需再次核对基金代码、份额、
公告生效日和直销额度后，才能升级为 official_notice。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


SYSTEM_DIR = Path(__file__).resolve().parents[1]
CSV_PATH = SYSTEM_DIR / "Config" / "qdii_universe_template.csv"
SOURCE_CONFIG = SYSTEM_DIR / "Config" / "qdii_official_sources.json"
REPORT_PATH = SYSTEM_DIR / "Reports" / "qdii-official-discovery.json"
FETCH_TIMEOUT_SECONDS = 20
KEYWORDS = ("申购", "定投", "限额", "限制", "暂停", "恢复", "纳斯达克100", "纳指100")
EXCLUDED_TITLE_HINTS = ("溢价", "停复牌", "风险提示", "交易价格")


def fetch_text(url: str) -> str:
    result = subprocess.run(
        ["curl", "-L", "--http1.1", "--ipv4", "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36", "-fsSL", "--connect-timeout", "8", "--max-time", str(FETCH_TIMEOUT_SECONDS), url],
        check=True,
        capture_output=True,
        timeout=FETCH_TIMEOUT_SECONDS + 5,
    )
    raw = result.stdout
    utf8 = raw.decode("utf-8", errors="replace")
    # 部分旧版基金官网仍返回 GBK/GB18030，若 UTF-8 解码后中文关键词几乎消失，
    # 再用 GB18030 解码，避免“官网已抓到但候选为 0”的假阴性。
    gb18030 = raw.decode("gb18030", errors="replace")
    if sum(utf8.count(k) for k in KEYWORDS) < sum(gb18030.count(k) for k in KEYWORDS):
        return gb18030
    return utf8


def read_rows() -> list[dict[str, str]]:
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("code", "").strip()]


def extract_links(page_url: str, text: str, codes: list[str]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    pattern = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
    for match in pattern.finditer(text):
        label = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(match.group(2))).split())
        href = urljoin(page_url, html.unescape(match.group(1)).strip())
        has_code = any(code in label for code in codes)
        has_product = any(keyword in label for keyword in ("纳斯达克100", "纳指100"))
        has_action = any(keyword in label for keyword in ("申购", "定投", "限额", "限制", "暂停", "恢复", "开放"))
        excluded = any(keyword in label for keyword in EXCLUDED_TITLE_HINTS)
        if label and not excluded and (has_code or (has_product and has_action)):
            links.append({"title": label, "url": href})
    return links


def extract_pdf_mentions(page_url: str, text: str, codes: list[str]) -> list[dict[str, str]]:
    """从部分官网的 PDF 直链列表中，按标题/附近文本发现候选。

    南方基金等旧版站点的公告标题可能编码异常，但 PDF URL 保留日期和文件路径，
    因此只在正文或链接附近出现纳斯达克/代码时生成候选，不直接判定额度。
    """
    candidates: list[dict[str, str]] = []
    pattern = re.compile(r"(?:href|HREF)=[\"']([^\"']+\.pdf[^\"']*)[\"']", re.I)
    for match in pattern.finditer(text):
        start = max(0, match.start() - 700)
        end = min(len(text), match.end() + 700)
        nearby = html.unescape(text[start:end])
        if not any(code in nearby for code in codes) and not any(k in nearby for k in ("纳斯达克", "纳指", "申购", "限额")):
            continue
        href = urljoin(page_url, html.unescape(match.group(1)).strip())
        label = " ".join(re.sub(r"<[^>]+>", " ", nearby).split())
        candidates.append({"title": label[-300:], "url": href})
    return candidates


def expand_index_urls(manager_config: dict[str, Any], *, deep: bool = False) -> list[str]:
    """展开固定入口和分页模板，并去重保持配置顺序。"""
    urls: list[str] = list(manager_config.get("index_urls", []))
    if not deep:
        return list(dict.fromkeys(urls))
    for pattern in manager_config.get("page_patterns", []):
        template = str(pattern.get("template", "")).strip()
        if not template:
            continue
        try:
            start = int(pattern.get("start", 1))
            end = int(pattern.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < start or end - start > 200:
            continue
        urls.extend(template.format(page=page) for page in range(start, end + 1))
    return list(dict.fromkeys(urls))


def main() -> int:
    parser = argparse.ArgumentParser(description="发现基金管理人官网 QDII 公告候选链接")
    parser.add_argument("--live", action="store_true", help="抓取配置的官方入口")
    parser.add_argument("--manager", action="append", help="只抓取指定管理人，可重复传入")
    parser.add_argument("--deep", action="store_true", help="额外抓取配置的分页模板；默认只抓固定入口")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args()
    rows = read_rows()
    config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    managers = config.get("managers", {})
    requested = set(args.manager or [])
    target_managers = sorted({row.get("manager", "").strip() for row in rows if row.get("manager") in managers and (not requested or row.get("manager") in requested)})
    report: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "live" if args.live else "dry_run",
        "managers": {},
    }
    for manager in target_managers:
        manager_rows = [row for row in rows if row.get("manager") == manager]
        codes = sorted({row["code"] for row in manager_rows})
        entry = {"codes": codes, "sources": [], "errors": []}
        for index_url in expand_index_urls(managers[manager], deep=args.deep):
            if not args.live:
                entry["sources"].append({"index_url": index_url, "status": "not_fetched"})
                continue
            try:
                body = fetch_text(index_url)
                candidates = extract_links(index_url, body, codes)
                if not candidates:
                    candidates = extract_pdf_mentions(index_url, body, codes)
                entry["sources"].append({"index_url": index_url, "status": "fetched", "candidates": candidates[:100]})
            except Exception as exc:  # noqa: BLE001 - discovery must continue per manager
                entry["errors"].append({"index_url": index_url, "error": str(exc)[:300]})
        report["managers"][manager] = entry
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DISCOVERY] 已生成：{args.output}")
    print(f"[DISCOVERY] 覆盖管理人：{len(target_managers)}，不改写 CSV，不发送通知。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
