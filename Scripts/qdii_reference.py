#!/usr/bin/env python3
"""Build a non-authoritative Nasdaq-100 QDII reference snapshot.

The reference page is useful for discovering coverage gaps and possible changes,
but it is *not* a fund-manager direct-sales source.  This script never writes to
the monitored universe CSV or the radar state; it only writes a separate,
explicitly secondary reference snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
SOURCE_URL = "https://anxinletech.com/instrument-qdii.html"
OUTPUT_PATH = SYSTEM_DIR / "Data" / "qdii" / "reference_latest.json"
SOURCE_TIER = "secondary_discovery_only"
FETCH_TIMEOUT_SECONDS = 12


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: str) -> str:
    return " ".join(unescape(value).replace("\xa0", " ").split())


def parse_amount_rmb(value: str) -> float | None:
    """Return the first RMB amount in text, accepting values such as 1万元."""
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元)", value)
    if not match:
        return None
    amount = float(match.group(1))
    return amount * 10_000 if match.group(2) in {"万", "万元"} else amount


def labeled_amount(value: str, label: str) -> float | None:
    # Stop before the other channel label when both channel values occur in one cell.
    match = re.search(rf"{re.escape(label)}\s*([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元)", value)
    if not match:
        return None
    amount = float(match.group(1))
    return amount * 10_000 if match.group(2) in {"万", "万元"} else amount


def normalize_status(value: str) -> str:
    text = clean_text(value)
    if "场内交易" in text:
        return "场内交易"
    if "暂停申购" in text:
        return "暂停申购"
    if "限大额" in text or "限额申购" in text or "限制申购" in text:
        return "限制申购"
    if "开放申购" in text or "正常申购" in text:
        return "正常开放"
    return "未确认"


class NasdaqTableParser(HTMLParser):
    """Collect the first ``table.readout`` immediately following Nasdaq-100 h3."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h3 = False
        self._h3_parts: list[str] = []
        self._want_table = False
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._cell_links: list[str] = []
        self._row: list[dict[str, Any]] = []
        self.rows: list[list[dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h3" and not self._in_table:
            self._in_h3 = True
            self._h3_parts = []
        elif tag == "table" and self._want_table and "readout" in (attributes.get("class") or "").split():
            self._in_table = True
            self._want_table = False
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._row = []
        elif tag == "td" and self._in_row:
            self._in_cell = True
            self._cell_parts = []
            self._cell_links = []
        elif tag == "a" and self._in_cell:
            href = attributes.get("href")
            if href:
                self._cell_links.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_h3:
            self._h3_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._in_h3:
            self._in_h3 = False
            self._want_table = "纳斯达克100" in clean_text("".join(self._h3_parts))
        elif tag == "td" and self._in_cell:
            self._row.append({"text": clean_text("".join(self._cell_parts)), "links": self._cell_links})
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            # Header rows use th, so only retain rows with the five td cells.
            if len(self._row) >= 5:
                self.rows.append(self._row)
            self._in_row = False
        elif tag == "table" and self._in_table:
            self._in_table = False


def extract_page_date(source_html: str) -> str | None:
    match = re.search(r"最新一期\s*[·•]\s*(20\d{2}-\d{2}-\d{2})", source_html)
    return match.group(1) if match else None


def parse_reference_html(source_html: str) -> dict[str, Any]:
    """Parse the page into a deterministic, source-preserving reference payload."""
    parser = NasdaqTableParser()
    parser.feed(source_html)
    records: list[dict[str, Any]] = []
    for cells in parser.rows:
        fund_text, status_text, quota_text, channel_note, notice_cell = (cell["text"] for cell in cells[:5])
        code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", fund_text)
        if not code_match:
            continue
        code = code_match.group(1)
        name = clean_text(fund_text[: code_match.start()].rstrip(" （("))
        direct = labeled_amount(quota_text, "直销")
        agent = labeled_amount(quota_text, "代销")
        standalone = parse_amount_rmb(quota_text)
        # A row like 021000 has one amount but the channel note explicitly says it
        # is available only in manager direct sales.
        if direct is None and "直销" in channel_note and (
            "代销" not in channel_note or "代销无此额度" in channel_note or "仅" in channel_note
        ):
            direct = standalone
        if agent is None and direct is None:
            agent = standalone
        notice_links = cells[4].get("links") or []
        tier = "secondary_aggregated"
        if "✓公告直核" in status_text:
            tier = "secondary_announcement_checked"
        elif "✓人工核实" in status_text:
            tier = "secondary_manual_checked"
        elif "✓双源一致" in status_text:
            tier = "secondary_cross_checked"
        records.append({
            "code": code,
            "name": name,
            "status": normalize_status(status_text),
            "agent_limit_rmb": agent,
            "direct_limit_rmb": direct,
            "channel_note": channel_note if channel_note != "—" else "",
            "notice_url": notice_links[-1] if notice_links else "",
            "page_date": extract_page_date(source_html),
            "source_tier": tier,
        })
    return {
        "source_url": SOURCE_URL,
        "source_tier": SOURCE_TIER,
        "page_date": extract_page_date(source_html),
        "records": records,
    }


def fetch_source_html() -> str:
    result = subprocess.run(
        ["curl", "-L", "--http1.1", "--ipv4", "-A", "Mozilla/5.0", "-fsSL", "--connect-timeout", "4", "--max-time", str(FETCH_TIMEOUT_SECONDS), SOURCE_URL],
        check=True,
        capture_output=True,
        timeout=FETCH_TIMEOUT_SECONDS + 8,
    )
    return result.stdout.decode("utf-8", errors="replace")


def build_snapshot(source_html: str, *, fetched_at: str | None = None) -> dict[str, Any]:
    payload = parse_reference_html(source_html)
    if not payload["records"]:
        raise ValueError("未找到纳斯达克100 table.readout；页面结构可能已变化")
    payload["fetched_at"] = fetched_at or utc_timestamp()
    payload["content_hash"] = hashlib.sha256(source_html.encode("utf-8")).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser(description="抓取安信乐纳指100 QDII二级参考数据（不改雷达主数据）")
    mode = argument_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="抓取公开静态页面并写参考快照")
    mode.add_argument("--dry-run", action="store_true", help="不联网、不写文件，仅显示说明")
    argument_parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = argument_parser.parse_args(argv)
    if args.dry_run:
        print("[DRY-RUN] 安信乐数据仅作二级发现；不会抓取、不会写入 CSV 或雷达状态。")
        return 0
    try:
        snapshot = build_snapshot(fetch_source_html())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"[ERROR] 抓取参考页面失败：{exc}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "ok": False,
                    "source_url": SOURCE_URL,
                    "source_tier": SOURCE_TIER,
                    "fetched_at": utc_timestamp(),
                    "error": str(exc)[:300],
                    "records": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[REFERENCE] rows={len(snapshot['records'])} page_date={snapshot['page_date']} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
