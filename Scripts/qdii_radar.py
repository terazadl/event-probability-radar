#!/usr/bin/env python3
"""纳指100 QDII 额度雷达。

这个模块只记录基金管理人公告或公开基金详情页中的申购状态，不连接交易账户，
不提交订单，也不把“公开开放”解释成某个投资者一定可以下单。

默认运行使用配置中最近一次人工核验的状态；只有显式传入 ``--live`` 时，才会
请求可解析的基金管理人直销详情页，并做保守的关键词/额度归一化。公告型 PDF
仍以最近一次已核验公告为基线，避免把二进制 PDF 或代销汇总误当成实时直销额度。
日报覆盖清单中全部人民币场外份额，图片逐只列出额度；消息正文只发送统计摘要。

用法：
    .venv/bin/python3 Scripts/qdii_radar.py --dry-run
    .venv/bin/python3 Scripts/qdii_radar.py --daily-now --dry-run
    .venv/bin/python3 Scripts/qdii_radar.py --export-share
    .venv/bin/python3 Scripts/qdii_radar.py --live --dry-run
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import notify
from review_qdii_before_push import host_is_official, source_review_flags


SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SYSTEM_DIR / "Config" / "qdii_watchlist.json"
DATA_DIR = SYSTEM_DIR / "Data" / "qdii"
STATE_PATH = DATA_DIR / "state.json"
LOCK_PATH = DATA_DIR / "radar.lock"
HISTORY_DIR = DATA_DIR / "history"
REPORT_PATH = SYSTEM_DIR / "Reports" / "纳指100 QDII申购日报.md"
PUBLIC_REPORT_PATH = SYSTEM_DIR / "Reports" / "纳指100 QDII申购雷达·公开快照.html"
SHARE_CARD_PATH = SYSTEM_DIR / "Reports" / "纳指100 QDII申购雷达·分享卡片.html"

PRODUCT_NAME_ZH = "纳指100 QDII 额度雷达"
CUSTOMER_TIMEZONE_NAME = "Asia/Shanghai"
CUSTOMER_TIMEZONE_LABEL = "北京时间"
SOURCE_POLICY_ZH = "图片区分“已核验”和“待核验”记录；已核验额度只采用基金管理人直销平台或基金管理人公告（指定信息披露报刊的公告原文可作为披露载体），不使用天天基金等代销平台额度。待核验记录仅用于覆盖清单展示，不视为可用额度。个人账户实际可下单额度，以基金管理人直销渠道当日页面为准。"
SUPPORTED_SOURCE_MODES = {"official_direct_html", "official_direct", "official_notice", "official_notice_publication"}
STATE_VERSION = 2
FETCH_TIMEOUT_SECONDS = 12

STATUS_NORMAL = "正常开放"
STATUS_LIMITED = "限制申购"
STATUS_SUSPENDED = "暂停申购"
STATUS_CLOSED = "境外休市"
STATUS_UNKNOWN = "未确认"
STATUS_ORDER = [
    STATUS_NORMAL,
    STATUS_LIMITED,
    STATUS_SUSPENDED,
    STATUS_CLOSED,
    STATUS_UNKNOWN,
]
STATUS_CLASS = {
    STATUS_NORMAL: "normal",
    STATUS_LIMITED: "limited",
    STATUS_SUSPENDED: "suspended",
    STATUS_CLOSED: "closed",
    STATUS_UNKNOWN: "unknown",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def customer_timestamp(value: datetime) -> str:
    local = value.astimezone(ZoneInfo(CUSTOMER_TIMEZONE_NAME))
    return f"{local:%Y-%m-%d %H:%M}（{CUSTOMER_TIMEZONE_LABEL}）"


def customer_report_date(value: datetime) -> str:
    """返回面向中国用户的日报日期，格式为 YYYYMMDD。"""
    local = value.astimezone(ZoneInfo(CUSTOMER_TIMEZONE_NAME))
    return f"{local:%Y%m%d}"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def normalize_state(state: Any) -> dict[str, Any]:
    """Upgrade compatible state snapshots without dropping their baseline."""
    if isinstance(state, dict) and state.get("version") == 1 and isinstance(state.get("funds"), dict):
        return {**state, "version": STATE_VERSION}
    if isinstance(state, dict) and state.get("version") == STATE_VERSION and isinstance(state.get("funds"), dict):
        return state
    return {"version": STATE_VERSION, "funds": {}}


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").replace("¥", "").replace("￥", ""))
    except (TypeError, ValueError):
        return default


def parse_limit_rmb(value: Any) -> Optional[float]:
    """把 5万元、50000、"不限" 等配置统一成金额或 None。"""
    if value is None or str(value).strip() in {"", "-", "未披露", "不限", "无限制"}:
        return None
    text = str(value).strip().replace(",", "")
    amount = as_float(text)
    if amount is not None:
        return amount
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(万|万元)", text)
    if match:
        return float(match.group(1)) * 10_000
    return None


def format_limit_rmb(value: Any) -> str:
    amount = parse_limit_rmb(value)
    if amount is None:
        raw = str(value or "").strip()
        return raw if raw in {"不限", "无限制"} else "未披露"
    if amount >= 10000 and amount % 10000 == 0:
        return f"¥{amount / 10000:.0f}万/日"
    return f"¥{amount:,.0f}/日"


def normalize_status(value: Any, text: str = "") -> str:
    """将人工录入或官方页面中的常见表述映射为有限状态集合。"""
    raw = str(value or "").strip()
    exact = {
        STATUS_NORMAL: STATUS_NORMAL,
        STATUS_LIMITED: STATUS_LIMITED,
        STATUS_SUSPENDED: STATUS_SUSPENDED,
        STATUS_CLOSED: STATUS_CLOSED,
        STATUS_UNKNOWN: STATUS_UNKNOWN,
        "开放申购": STATUS_NORMAL,
        "恢复申购": STATUS_NORMAL,
        "开放": STATUS_NORMAL,
        "限制大额申购": STATUS_LIMITED,
        "限购": STATUS_LIMITED,
        "暂停": STATUS_SUSPENDED,
        "暂停大额申购": STATUS_LIMITED,
        "休市": STATUS_CLOSED,
    }
    # “未确认”是兜底值；当调用方同时提供官方页面文本时，必须先尝试解析文本。
    if raw in exact and raw != STATUS_UNKNOWN:
        return exact[raw]
    haystack = f"{raw} {text}".replace(" ", "")
    # 顺序很重要：暂停申购不能被“申购”或“开放”误判。
    if re.search(r"(?:暂停|停止)(?:办理)?(?:申购|定投)|暂停申购", haystack):
        return STATUS_SUSPENDED
    if re.search(r"境外(?:市场)?(?:休市|节假日)|海外市场休市", haystack):
        return STATUS_CLOSED
    if re.search(r"限制(?:大额)?申购|限购|大额申购.{0,20}(?:上限|限制)|单日.{0,15}申购上限", haystack):
        return STATUS_LIMITED
    if re.search(r"(?:恢复|开放)(?:办理)?(?:申购|定投)|正常开放|开放申购", haystack):
        return STATUS_NORMAL
    if raw in exact:
        return exact[raw]
    return STATUS_UNKNOWN


def fetch_text(url: str) -> str:
    command = [
        "curl", "-L", "--http1.1", "--ipv4", "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36", "-fsSL",
        "--connect-timeout", "4", "--max-time", str(FETCH_TIMEOUT_SECONDS),
        "--retry", "1", "--retry-all-errors", url,
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=FETCH_TIMEOUT_SECONDS + 8,
    )
    return result.stdout.decode("utf-8", errors="replace")


def parse_direct_trade_state(text: str, fund_code: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Parse a manager direct-sales page without relying on one site's markup.

    Manager pages vary widely, so this parser is deliberately conservative: it
    only extracts a status from the normalized visible text and a quota when a
    nearby ``上限``/``限额`` expression contains an explicit RMB amount.  It
    never treats a missing amount as unlimited.
    """
    # Some manager direct-sales pages expose the current quota through a small
    # JSON endpoint rather than rendering it into the HTML.  Guangfa's
    # fund-person-limit/fund-org-limit endpoints use MAX_ALLOT_BALA for the
    # current purchase ceiling.  Treat a positive explicit ceiling as a
    # limited-subscription state; do not infer "normal" from a missing/zero
    # value because that could turn an unavailable quota into a false claim.
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and "MAX_ALLOT_BALA" in payload:
        max_value = parse_limit_rmb(payload.get("MAX_ALLOT_BALA"))
        if max_value is not None and max_value > 0:
            return {
                "status": STATUS_LIMITED,
                "purchase_limit_rmb": max_value,
                "raw_status": f"官方直销额度API MAX_ALLOT_BALA={payload.get('MAX_ALLOT_BALA')}",
            }

    visible = html.unescape(re.sub(r"<[^>]+>", " ", text))
    visible = " ".join(visible.replace("\xa0", " ").split())
    if fund_code:
        # Fund-manager pages such as 嘉实'额度表' contain many products and
        # historical statuses.  Restrict parsing to the nearest code-specific
        # block so a neighboring fund cannot overwrite today's baseline.
        positions = [m.start() for m in re.finditer(re.escape(str(fund_code)), visible)]
        if positions:
            def evidence_score(snippet: str) -> int:
                score = 0
                for term, weight in (
                    ("交易状态", 5), ("申购状态", 5), ("暂停申购", 5),
                    ("限额申购", 5), ("限制申购", 5), ("开放申购", 5),
                    ("暂停定投", 2), ("开放定投", 2), ("当前", 2),
                ):
                    score += snippet.count(term) * weight
                score -= snippet.count("历史公告") * 4
                return score

            # For each occurrence, stop at the first transaction-status phrase
            # after the code.  This keeps a neighboring row's status from
            # leaking into the selected fund (common on manager status tables).
            snippets = []
            row_status = re.compile(
                r"暂停申购|停止申购|限制(?:大额)?申购|限额(?:申购|直销)|限购|"
                r"单日.{0,20}申购上限|开放申购|恢复(?:办理)?申购|正常申购"
            )
            for pos in positions:
                after = visible[pos: min(len(visible), pos + 360)]
                status_match = row_status.search(after)
                if status_match:
                    row_end = status_match.end() + 80
                    # If another six-digit fund code follows shortly after
                    # the first status, it marks the next table row.  Stop
                    # before it so its status cannot be mixed into this row.
                    next_code = re.search(r"(?<!\d)\d{6}(?!\d)", after[status_match.end():])
                    if next_code and next_code.start() < 180:
                        row_end = status_match.end() + next_code.start()
                    end = pos + row_end
                else:
                    end = pos + 260
                snippets.append(visible[max(0, pos - 20): min(len(visible), end)])
            best = max(snippets, key=evidence_score)
            if evidence_score(best) > 0:
                visible = best
    if not visible:
        return None
    status_terms = {
        STATUS_SUSPENDED: r"(?:暂停|停止)(?:办理)?(?:申购|定投)|暂停申购",
        # Require a purchase/action word after 限额/限制.  Matching the bare
        # word "限额" would make a page with "限额...，开放定投" ambiguous.
        STATUS_LIMITED: r"(?:限制(?:大额)?申购|限额(?:申购|直销)|限大额申购|限购)|单日.{0,20}申购上限",
        STATUS_NORMAL: r"(?:恢复|开放)(?:办理)?(?:申购|定投)|正常(?:开放|申购)|开放申购",
        STATUS_CLOSED: r"境外(?:市场)?(?:休市|节假日)|海外市场休市",
    }
    candidates: list[tuple[str, str, int, bool]] = []
    for status, pattern in status_terms.items():
        for match in re.finditer(pattern, visible):
            start = max(0, match.start() - 35)
            end = min(len(visible), match.end() + 80)
            context = visible[start:end]
            prefix = visible[start:match.start()]
            preferred = int(
                bool(re.search(r"当前|交易状态|申购状态|网上直销|销售状态", prefix))
                and not re.search(r"历史|曾经|过去|公告", prefix)
            )
            candidates.append((status, context, preferred, "申购" in match.group()))
    if not candidates:
        return None
    preferred = [item for item in candidates if item[2]]
    selected = preferred or candidates
    # Purchase availability is the primary signal for this radar.  Do not let
    # a separate "开放/暂停定投" label override a purchase restriction.
    purchase_selected = [item for item in selected if item[3]]
    if purchase_selected:
        selected = purchase_selected
    statuses = {item[0] for item in selected}
    if len(statuses) != 1:
        # A page containing both current and historical statuses is ambiguous;
        # refusing to guess is safer than turning an old notice into today's state.
        return None
    status = selected[0][0]
    context = selected[0][1]
    limit: Optional[float] = None
    limit_pattern = re.compile(
        r"(?:单日|每日|每个基金账户|单个基金账户|累计)?[^。；，,]{0,35}?(?:上限|限额)"
        r"[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*(万|万元|元)"
    )
    match = limit_pattern.search(context)
    if match:
        limit = float(match.group(1))
        if match.group(2) in {"万", "万元"}:
            limit *= 10_000
    if status == STATUS_SUSPENDED:
        # A stale quota can remain in a page's static text after suspension.
        limit = None
    return {"status": status, "purchase_limit_rmb": limit, "raw_status": visible[:500]}


def parse_eastmoney_trade_state(text: str) -> Optional[dict[str, Any]]:
    """解析公开基金详情页的交易状态卡片。

    该页面会把“暂停申购（单日累计购买上限100元）”同时显示出来。对用户而言这
    仍然存在一个可购买额度，因此归入“限制申购”，同时保留 raw_status 供审计。
    """
    match = re.search(r"交易状态：</span><span[^>]*>(.*?)</span>", text, re.S)
    if not match:
        return None
    raw_status = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    raw_status = " ".join(raw_status.replace("\xa0", " ").split()).strip()
    limit: Optional[float] = None
    limit_match = re.search(
        r"(?:上限|限额)\s*([0-9]+(?:\.[0-9]+)?)\s*(万|万元|元)", raw_status
    )
    if limit_match:
        limit = float(limit_match.group(1))
        if limit_match.group(2) in {"万", "万元"}:
            limit *= 10_000
    if "暂停申购" in raw_status:
        status = STATUS_SUSPENDED
    elif "限大额" in raw_status:
        status = STATUS_LIMITED
    elif "开放申购" in raw_status or "恢复申购" in raw_status:
        status = STATUS_NORMAL
    elif "休市" in raw_status:
        status = STATUS_CLOSED
    else:
        status = normalize_status("", raw_status)
    return {"status": status, "purchase_limit_rmb": limit, "raw_status": raw_status}


def read_universe_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle) if str(row.get("code", "")).strip()]
    except (OSError, csv.Error):
        return []


def load_funds(config: dict) -> list[dict[str, Any]]:
    """加载 JSON 覆盖项，并保留监测清单中的全部人民币场外份额。"""
    rows: dict[str, dict[str, Any]] = {}
    csv_value = str(config.get("universe_csv", "")).strip()
    if csv_value:
        csv_path = Path(csv_value)
        if not csv_path.is_absolute():
            csv_path = SYSTEM_DIR / csv_path
        for row in read_universe_csv(csv_path):
            rows[str(row["code"]).strip()] = row
    for record in config.get("funds", []):
        code = str(record.get("code", "")).strip()
        if code:
            rows[code] = {**rows.get(code, {}), **record}
    funds = []
    for code, record in rows.items():
        row = dict(record)
        row["code"] = code
        row["name"] = str(row.get("name") or f"基金 {code}").strip()
        row["manager"] = str(row.get("manager") or "待核验").strip()
        row["share_class"] = str(row.get("share_class") or "待核验").strip()
        row["product_type"] = str(row.get("product_type") or "OTC").strip()
        row["status"] = normalize_status(row.get("status"))
        row["direct_sales"] = str(row.get("direct_sales", "true")).lower() not in {"false", "0", "no"}
        row["purchase_limit_rmb"] = parse_limit_rmb(row.get("purchase_limit_rmb"))
        row["regular_investment"] = str(row.get("regular_investment") or "未披露").strip()
        row["effective_date"] = str(row.get("effective_date") or "未披露").strip()
        row["status_verified_at"] = str(row.get("status_verified_at") or "未核验").strip()
        row["source_url"] = str(row.get("source_url") or row.get("status_url") or "").strip()
        row["status_note"] = str(row.get("status_note") or "").strip()
        row["source_mode"] = str(row.get("source_mode") or row.get("status_source") or "manual").strip()
        row["status_source"] = str(row.get("status_source") or row["source_mode"] or "manual").strip()
        row["channel"] = str(row.get("channel") or "").strip()
        row["quota_scope"] = str(row.get("quota_scope") or "per_share").strip()
        row["review_flags"] = source_review_flags(row)
        row["review_status"] = "已核验" if not row["review_flags"] else "待核验"
        # 严格直销口径：只有基金管理人直销渠道或适用于直销渠道的基金管理人
        # 公告才能进入日报；代销平台页面不能作为直销额度的替代来源。
        if (
            row["direct_sales"]
            and row["channel"] == "manager_direct"
            and row["source_mode"] in SUPPORTED_SOURCE_MODES
        ):
            funds.append(row)
    return sorted(funds, key=display_sort_key)


def display_sort_key(row: dict[str, Any]) -> tuple:
    """Keep status as the primary grouping, then show usable quotas first.

    A larger quota is useful within the limited-subscription section, but it
    must never outrank status or an unresolved source review. Unknown/pending
    quotas are placed last instead of being treated as zero.
    """
    status = row.get("status", STATUS_UNKNOWN)
    status_rank = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
    pending_rank = 1 if row.get("review_status") == "待核验" else 0
    amount = parse_limit_rmb(row.get("purchase_limit_rmb"))
    quota_rank = -amount if amount is not None else float("inf")
    if status != STATUS_LIMITED:
        quota_rank = 0
    return (
        status_rank,
        pending_rank,
        quota_rank,
        str(row.get("manager", "")),
        str(row.get("name", "")),
        str(row.get("code", "")),
    )


def quota_sort_key(row: dict[str, Any]) -> tuple:
    """Order the daily full-universe view by usable quota, descending.

    Rows without a currently usable numeric quota (normal/open without a
    disclosed ceiling, suspended, closed, or unknown) stay at the bottom.
    Pending-review rows are always last so an unverified number can never look
    more actionable than a verified direct-sales quota.
    """
    pending_rank = 1 if row.get("review_status") == "待核验" else 0
    status = row.get("status", STATUS_UNKNOWN)
    usable_status = status not in {STATUS_SUSPENDED, STATUS_CLOSED, STATUS_UNKNOWN}
    amount = parse_limit_rmb(row.get("purchase_limit_rmb")) if usable_status else None
    undisclosed_rank = 1 if amount is None else 0
    amount_rank = -amount if amount is not None else 0
    status_rank = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
    return (
        pending_rank,
        undisclosed_rank,
        amount_rank,
        status_rank,
        str(row.get("manager", "")),
        str(row.get("name", "")),
        str(row.get("code", "")),
    )


def collect_fund(record: dict[str, Any], *, live: bool, checked_at: datetime) -> dict[str, Any]:
    row = dict(record)
    source_url = str(row.get("source_url") or "").strip()
    source_mode = str(row.get("source_mode") or row.get("status_source") or "manual").strip()
    source_text = ""
    fetch_error = ""
    fetched = False
    if live and source_mode in {"official_direct_html", "official_direct"} and source_url:
        try:
            parsed_url = urlparse(source_url)
            if parsed_url.scheme != "https" or not host_is_official(source_url):
                raise ValueError("实时抓取仅允许 HTTPS 基金管理人官方域名")
            source_text = fetch_text(source_url)
            parsed = parse_direct_trade_state(source_text, fund_code=str(row.get("code") or ""))
            if not parsed:
                raise ValueError("未找到可确认的申购状态")
            row["status"] = parsed["status"]
            # A successful page without an explicit amount means “未披露”,
            # not “keep yesterday's quota”.
            row["purchase_limit_rmb"] = parsed["purchase_limit_rmb"]
            row["raw_trade_status"] = parsed["raw_status"]
            row["status_source"] = source_mode
            row["status_verified_at"] = customer_timestamp(checked_at)
            row["freshness_status"] = "live_verified"
            fetched = True
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            fetch_error = str(exc)[:300]
        except ValueError as exc:
            fetch_error = str(exc)[:300]
        if fetch_error:
            # Preserve the last configured value on a transient source failure;
            # do not turn a network outage into a false status-change alert.
            row["status_source"] = "official_html_stale"
            row["freshness_status"] = "stale_fetch_failed"
    elif live:
        row["freshness_status"] = "notice_baseline" if source_mode in {"official_notice", "official_notice_publication"} else "baseline_not_fetched"
    else:
        row["freshness_status"] = "dry_run_baseline"
    row["checked_at"] = isoformat_utc(checked_at)
    row["source_text_hash"] = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16] if source_text else ""
    if fetch_error:
        # A page parser/network failure is a data-quality warning, not a new
        # market state. The configured baseline remains visible and is marked
        # stale so the publisher can continue without a false alert.
        row["warning"] = fetch_error
        row["status_note"] = f"官方页面抓取失败：{fetch_error}"
    if fetched and not row.get("status_note"):
        row["status_note"] = "基金管理人直销页面实时核验"
    return row


def collect_funds(records: list[dict[str, Any]], *, live: bool, checked_at: datetime) -> list[dict[str, Any]]:
    """Collect rows with bounded parallelism so one slow manager site cannot
    consume the entire scheduler window."""
    if not live or len(records) < 2:
        return [collect_fund(row, live=live, checked_at=checked_at) for row in records]
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="qdii-fetch") as pool:
        futures = [pool.submit(collect_fund, row, live=True, checked_at=checked_at) for row in records]
        return [future.result() for future in futures]


def compare_fields(row: dict[str, Any], previous: Optional[dict[str, Any]]) -> list[str]:
    if not previous:
        return []
    fields = ["status", "purchase_limit_rmb", "regular_investment", "effective_date"]
    return [field for field in fields if row.get(field) != previous.get(field)]


def build_snapshot(funds: list[dict[str, Any]], previous: dict[str, Any], checked_at: datetime) -> dict[str, Any]:
    previous_funds = previous.get("funds", {})
    records = []
    for fund in funds:
        code = fund["code"]
        prior = previous_funds.get(code)
        row = dict(fund)
        row["review_flags"] = row.get("review_flags") or source_review_flags(row)
        row["review_status"] = "已核验" if not row["review_flags"] else "待核验"
        changed_fields = compare_fields(row, prior)
        row["changed"] = bool(prior and changed_fields)
        row["changed_fields"] = changed_fields
        row["previous_status"] = prior.get("status") if prior else ""
        records.append(row)
    records.sort(key=display_sort_key)
    counts = {status: 0 for status in STATUS_ORDER}
    for row in records:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "checked_at": isoformat_utc(checked_at),
        "records": records,
        "total": len(records),
        "counts": counts,
        "changed_count": sum(1 for row in records if row["changed"]),
        "review_pending": sum(1 for row in records if row.get("review_status") == "待核验"),
        "freshness_counts": {
            "live_verified": sum(1 for row in records if row.get("freshness_status") == "live_verified"),
            "notice_baseline": sum(1 for row in records if row.get("freshness_status") == "notice_baseline"),
            "baseline_not_fetched": sum(1 for row in records if row.get("freshness_status") in {"baseline_not_fetched", "dry_run_baseline"}),
            "stale_fetch_failed": sum(1 for row in records if row.get("freshness_status") == "stale_fetch_failed"),
        },
        "errors": [
            {"code": row["code"], "error": row["error"]}
            for row in records if row.get("error")
        ],
        "warnings": [
            {"code": row["code"], "warning": row["warning"]}
            for row in records if row.get("warning")
        ],
    }


def daily_digest_due(config: dict, state: dict, now: datetime) -> bool:
    settings = config.get("daily_digest", {})
    if not settings.get("enabled", True):
        return False
    local = now.astimezone(ZoneInfo(settings.get("timezone", CUSTOMER_TIMEZONE_NAME)))
    start = int(settings.get("hour", 8)) * 60 + int(settings.get("minute", 0))
    window = max(1, int(settings.get("send_window_minutes", 60)))
    current = local.hour * 60 + local.minute
    return start <= current < start + window and state.get("last_daily_digest_date") != local.date().isoformat()


def summary_lines(snapshot: dict[str, Any]) -> list[str]:
    counts = snapshot["counts"]
    lines = [
        f"全量监测：{snapshot['total']} 个人民币份额",
        f"正常开放：{counts.get(STATUS_NORMAL, 0)}",
        f"限制申购：{counts.get(STATUS_LIMITED, 0)}",
        f"暂停申购：{counts.get(STATUS_SUSPENDED, 0)}",
        f"待核验：{snapshot.get('review_pending', 0)}",
        f"状态变化：{snapshot['changed_count']}",
    ]
    freshness = snapshot.get("freshness_counts", {})
    if freshness.get("live_verified") or freshness.get("stale_fetch_failed"):
        lines.append(f"实时核验：{freshness.get('live_verified', 0)}；抓取失败保留基线：{freshness.get('stale_fetch_failed', 0)}")
    elif freshness.get("notice_baseline") or freshness.get("baseline_not_fetched"):
        lines.append(f"公告/人工基线：{freshness.get('notice_baseline', 0) + freshness.get('baseline_not_fetched', 0)}（本次未改写为实时状态）")
    extra = counts.get(STATUS_CLOSED, 0) + counts.get(STATUS_UNKNOWN, 0)
    if extra:
        lines.append(f"其他/未确认：{extra}")
    return lines


def changed_text(row: dict[str, Any]) -> str:
    previous = row.get("previous_status") or "首次记录"
    current = row.get("status") or STATUS_UNKNOWN
    fields = row.get("changed_fields") or []
    if "status" in fields:
        return f"{previous} → {current}"
    if fields:
        return "；".join(fields) + "更新"
    return ""


def build_daily_digest(snapshot: dict[str, Any], *, image_url: str = "", public_url: str = "", checked_at: Optional[datetime] = None) -> tuple[str, str]:
    checked = checked_at or parse_timestamp(snapshot["checked_at"])
    report_date = customer_report_date(checked)
    title = f"{PRODUCT_NAME_ZH}｜{report_date}"
    lines = [
        f"# {PRODUCT_NAME_ZH}｜{report_date}",
        "> 每天北京时间08:00固定更新；其余时间仅在状态发生变化时提醒。",
        "> 附图为全量额度排行：已核验直销额度从高到低，暂停/未披露/待核验置后。",
        "",
        f"日报时间：{report_date}｜数据时间：{customer_timestamp(checked)}",
        "",
        *[f"- {line}" for line in summary_lines(snapshot)],
    ]
    changes = [row for row in snapshot["records"] if row["changed"]]
    if changes:
        lines += ["", "**今日状态变化**", ""]
        for row in changes[:10]:
            lines.append(f"- {row['name']}（{row['code']}）：{changed_text(row)}")
        if len(changes) > 10:
            lines.append(f"- 另有 {len(changes) - 10} 项变化，见全量图片。")
    if image_url:
        separator = "&" if "?" in image_url else "?"
        lines += ["", f"![{PRODUCT_NAME_ZH}全量快照]({image_url}{separator}v={checked:%Y%m%d%H%M})"]
    if public_url:
        lines += ["", f"[查看公开快照]({public_url})"]
    lines += [
        "",
        SOURCE_POLICY_ZH,
        "本产品仅做申购状态观察，不构成投资建议。",
    ]
    return title, "\n".join(lines)


def build_change_alert(snapshot: dict[str, Any]) -> tuple[str, str]:
    changes = [row for row in snapshot["records"] if row["changed"]]
    title = f"⚠️ {PRODUCT_NAME_ZH}：{len(changes)} 项状态变化"
    lines = [f"数据时间：{customer_timestamp(parse_timestamp(snapshot['checked_at']))}", ""]
    for row in changes[:15]:
        lines.append(f"- {row['name']}（{row['code']}）：{changed_text(row)}")
    if len(changes) > 15:
        lines.append(f"- 另有 {len(changes) - 15} 项变化，见日报。")
    lines += ["", SOURCE_POLICY_ZH, "公开状态不代表个人账户一定可以下单。"]
    return title, "\n".join(lines)


def report_row(row: dict[str, Any]) -> str:
    change = f" · 今日变化：{changed_text(row)}" if row.get("changed") else ""
    source = f"[{row['source_url']}]({row['source_url']})" if row.get("source_url") else "待补官方来源"
    review = "（待核验）" if row.get("review_status") == "待核验" else ""
    quota = "待核验" if review else format_limit_rmb(row.get("purchase_limit_rmb"))
    effective_date = row.get("effective_date", "未披露")
    freshness_note = "（沿用基线）" if row.get("freshness_status") == "stale_fetch_failed" else ""
    if row.get("source_mode") in {"official_direct", "official_direct_html"} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(effective_date)):
        effective_date = "页面核验"
    return (
        f"| {row['name']} | {row['code']} | {row['manager']} | {row['status']}{review} | "
        f"{quota} | {row['regular_investment']} | "
        f"{effective_date} | {row.get('status_verified_at', '未核验')}{freshness_note}{change} | {source} |"
    )


def build_report(snapshot: dict[str, Any]) -> str:
    timestamp = customer_timestamp(parse_timestamp(snapshot["checked_at"]))
    lines = [
        f"# {PRODUCT_NAME_ZH}｜{customer_report_date(parse_timestamp(snapshot['checked_at']))}",
        "",
        f"> 数据时间：{timestamp}",
        "> 图片逐只列出监测清单中全部人民币场外份额的申购状态与额度。",
        "",
        *[f"- {line}" for line in summary_lines(snapshot)],
        "",
        "## 状态表",
        "",
    ]
    for status in STATUS_ORDER:
        rows = sorted((row for row in snapshot["records"] if row["status"] == status), key=display_sort_key)
        if not rows:
            continue
        lines += [f"### {status}（{len(rows)}）", "", "| 基金 | 代码 | 管理人 | 状态 | 单日申购上限 | 定投 | 生效日/页面核验 | 最近核验 | 来源 |", "|---|---:|---|---|---:|---|---|---|---|"]
        lines.extend(report_row(row) for row in rows)
        lines.append("")
    lines += [
        "## 口径",
        "",
        "- 状态变化是横跨当前状态表的标记，不另建重复分组。",
        "- “限制申购”只表示公开页面存在限额/大额限制；“未披露”不是“无限额”。",
        "- 公告型来源（包括指定信息披露报刊）必须有生效日；官方详情页若没有公告生效日，显示“页面核验”。",
        "- 每个状态组内，限制申购按已核验的直销额度从高到低排列；待核验记录置后。",
        "- 本产品是公开信息整理工具，不提供交易、申购或投资建议。",
        "",
    ]
    return "\n".join(lines)


def html_status_class(status: str) -> str:
    return STATUS_CLASS.get(status, "unknown")


def html_source(row: dict[str, Any]) -> str:
    url = str(row.get("source_url") or "").strip()
    if not url:
        return "待补官方来源"
    label = "直销/公告" if row.get("review_status") != "待核验" else "来源待核验"
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">{label}</a>'


def quota_scope_label(row: dict[str, Any]) -> str:
    labels = {
        "per_share": "",
        "combined_ac": "（A/C合计）",
        "combined_aci": "（A/C/I合计）",
        "cross_channel_per_share": "（全渠道合计）",
    }
    return labels.get(str(row.get("quota_scope") or "per_share"), "")


def html_row(row: dict[str, Any]) -> str:
    changed = '<span class="qdii-change">今日变化</span>' if row.get("changed") else ""
    review_pending = row.get("review_status") == "待核验"
    review_badge = '<span class="qdii-review">待核验</span>' if review_pending else ""
    stale_badge = '<span class="qdii-stale">沿用基线</span>' if row.get("freshness_status") == "stale_fetch_failed" else ""
    if review_pending:
        quota = "待核验"
    elif row["status"] == STATUS_SUSPENDED:
        quota = "暂停申购"
    elif row["status"] == STATUS_CLOSED:
        quota = "休市暂停"
    else:
        quota = format_limit_rmb(row.get("purchase_limit_rmb"))
        if quota == "未披露" and row["status"] == STATUS_NORMAL:
            quota = "正常开放"
        elif quota != "未披露":
            quota += quota_scope_label(row)
    note = html.escape(row.get("status_note") or "", quote=True)
    return (
        f'<div class="qdii-row qdii-row--{html_status_class(row["status"])}">'
        f'<div class="qdii-fund"><strong>{html.escape(row["name"])}</strong>'
        f'<small>{html.escape(row["code"])} · {html.escape(row["manager"])} · {html.escape(row["share_class"])}</small></div>'
        f'<div class="qdii-state"><span class="qdii-dot"></span>{html.escape(row["status"])}{review_badge}{stale_badge}{changed}</div>'
        f'<div class="qdii-quota" title="{note}"><small>直销申购额度</small><strong>{html.escape(quota)}</strong></div>'
        f'<div class="qdii-source">{html_source(row)}</div>'
        "</div>"
    )


def public_styles(*, card: bool = False) -> str:
    width = "width:1080px;" if card else "max-width:1160px;width:calc(100% - 40px);"
    body = "body{margin:0;width:1080px;min-height:2600px;overflow:hidden;}" if card else "body{margin:0;}"
    return f"""
<style>
{body}
*{{box-sizing:border-box}}
body{{background:#f5f1e8;color:#201e1a;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC",sans-serif}}
.qdii-shell{{{width}margin:0 auto;padding:{'36px 44px' if card else '42px 0 64px'}}}
.qdii-head{{align-items:end;border-bottom:1px solid #211f1b;display:flex;justify-content:space-between;gap:24px;padding-bottom:14px;margin-bottom:14px}}
.qdii-kicker{{color:#2f55d4;font-size:12px;font-weight:800;letter-spacing:.14em;margin:0 0 8px;text-transform:uppercase}}
.qdii-head h1{{font-family:Georgia,"Noto Serif SC",serif;font-size:{'46px' if card else 'clamp(38px,5vw,60px)'};font-weight:400;letter-spacing:-.04em;line-height:1;margin:0}}
.qdii-time{{color:#706a60;font-size:14px;line-height:1.45;margin:0;text-align:right;white-space:nowrap}}
.qdii-summary{{display:grid;grid-template-columns:repeat(6,1fr);gap:7px;margin:0 0 16px}}
.qdii-metric{{border:1px solid #b8afa0;padding:9px 11px;background:rgba(255,255,255,.25)}}
.qdii-metric strong{{display:block;font:600 26px Georgia,"Noto Serif SC",serif;line-height:1.05}}
.qdii-metric span{{color:#706a60;font-size:12px;display:block;margin-top:3px}}
.qdii-section{{margin:12px 0 0}}
.qdii-section h2{{align-items:center;border-bottom:1px solid #b8afa0;display:flex;font:600 21px Georgia,"Noto Serif SC",serif;justify-content:space-between;margin:0;padding:0 0 6px}}
.qdii-section h2 small{{color:#706a60;font:normal 13px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
.qdii-row{{display:grid;grid-template-columns:minmax(320px,1.7fr) 112px 126px 86px;align-items:center;gap:9px;border-bottom:1px solid #d6cfc2;min-height:46px;padding:5px 0}}
.qdii-fund strong{{display:block;font-size:15px;line-height:1.25}}
.qdii-fund small{{color:#706a60;display:block;font-size:12px;margin-top:2px}}
.qdii-state{{font-size:13px;font-weight:700;white-space:nowrap}}
.qdii-dot{{background:#8e8a80;border-radius:50%;display:inline-block;height:7px;margin-right:6px;width:7px}}
.qdii-row--normal .qdii-dot{{background:#288a54}} .qdii-row--limited .qdii-dot{{background:#d27a18}} .qdii-row--suspended .qdii-dot{{background:#c6463c}} .qdii-row--closed .qdii-dot{{background:#4d76b8}}
.qdii-quota small{{color:#706a60;display:block;font-size:10px;letter-spacing:.04em}}
.qdii-quota strong{{display:block;font:600 15px Georgia,"Noto Serif SC",serif;margin-top:1px}}
.qdii-source{{font-size:12px;text-align:right;white-space:nowrap}}
.qdii-source a{{color:#2f55d4;text-decoration:none;border-bottom:1px solid #2f55d4}}
.qdii-change{{background:#2f55d4;color:#fff;font-size:10px;font-weight:700;margin-left:5px;padding:2px 4px;vertical-align:1px}}
.qdii-review{{background:#a96518;color:#fff;font-size:10px;font-weight:700;margin-left:5px;padding:2px 4px;vertical-align:1px}}
.qdii-stale{{background:#766a5a;color:#fff;font-size:10px;font-weight:700;margin-left:5px;padding:2px 4px;vertical-align:1px}}
.qdii-note{{color:#706a60;font-size:13px;line-height:1.5;margin:13px 0 0}}
.qdii-actions{{border-top:1px solid #211f1b;display:flex;gap:12px;margin-top:18px;padding-top:14px}}
.qdii-action{{background:#2f55d4;border:1px solid #2f55d4;color:#fff;cursor:pointer;font-size:12px;font-weight:700;padding:11px 16px}}
.qdii-action--secondary{{background:transparent;color:#2f55d4}}
@media(max-width:820px){{.qdii-summary{{grid-template-columns:repeat(2,1fr)}}.qdii-head{{align-items:start;flex-direction:column;gap:12px}}.qdii-time{{text-align:left}}.qdii-row{{grid-template-columns:1fr 100px;gap:5px 12px}}.qdii-state{{grid-column:1}}.qdii-quota{{grid-column:2;grid-row:1}}.qdii-source{{grid-column:2;text-align:right}}}}
</style>"""


def metric_html(label: str, value: Any) -> str:
    return f'<div class="qdii-metric"><strong>{html.escape(str(value))}</strong><span>{html.escape(label)}</span></div>'


def build_public_page(
    snapshot: dict[str, Any],
    *,
    public_url: str = "",
    card: bool = False,
    sort_mode: str = "status",
) -> str:
    checked = parse_timestamp(snapshot["checked_at"])
    sections = []
    if sort_mode == "quota":
        rows = sorted(snapshot["records"], key=quota_sort_key)
        rows_html = "".join(html_row(row) for row in rows)
        sections.append(
            f'<section class="qdii-section"><h2>额度排序（高→低）<small>{len(rows)} 项</small></h2>{rows_html}</section>'
        )
    else:
        for status in STATUS_ORDER:
            rows = sorted((row for row in snapshot["records"] if row["status"] == status), key=display_sort_key)
            if not rows:
                continue
            rows_html = "".join(html_row(row) for row in rows)
            sections.append(
                f'<section class="qdii-section"><h2>{html.escape(status)}<small>{len(rows)} 项</small></h2>{rows_html}</section>'
            )
    summary = snapshot["counts"]
    summary_html = "".join([
        metric_html("全量人民币份额", snapshot["total"]),
        metric_html(STATUS_NORMAL, summary.get(STATUS_NORMAL, 0)),
        metric_html(STATUS_LIMITED, summary.get(STATUS_LIMITED, 0)),
        metric_html(STATUS_SUSPENDED, summary.get(STATUS_SUSPENDED, 0)),
        metric_html("状态变化", snapshot["changed_count"]),
        metric_html("待核验", snapshot.get("review_pending", 0)),
    ])
    summary_text = "\n".join([
        f"{PRODUCT_NAME_ZH}｜{customer_report_date(checked)}",
        customer_timestamp(checked),
        *summary_lines(snapshot),
        SOURCE_POLICY_ZH,
        "公开状态不代表个人账户一定可以下单。",
    ])
    public_url = html.escape(public_url, quote=True)
    public_link = f'<a class="qdii-action qdii-action--secondary" href="{public_url}">公开链接</a>' if public_url else ""
    actions = "" if card else (
        '<div class="qdii-actions"><button class="qdii-action" type="button" '
        f'data-copy>复制摘要</button>{public_link}<span class="qdii-note" '
        'data-status></span></div>'
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{PRODUCT_NAME_ZH}｜{customer_report_date(checked)}</title>{public_styles(card=card)}</head>
<body><main class="qdii-shell" aria-labelledby="qdii-title">
<header class="qdii-head"><div><p class="qdii-kicker">Nasdaq-100 QDII · Subscription Quota</p><h1 id="qdii-title">{PRODUCT_NAME_ZH}｜{customer_report_date(checked)}</h1></div><p class="qdii-time">日报时间<br><strong>{customer_report_date(checked)}</strong><br><small>{html.escape(customer_timestamp(checked))}</small></p></header>
<div class="qdii-summary">{summary_html}</div>
{''.join(sections)}
<p class="qdii-note">图片列出全部监测份额，并按已核验直销额度从高到低排列；无可用数字额度、暂停申购和待核验记录置后。不使用天天基金等代销平台额度。“A/C合计”或“A/C/I合计”表示共享额度，不能按份额类别重复计算。待核验记录不视为已确认额度；个人账户实际可下单额度以基金管理人直销渠道当日页面为准；数据不构成投资建议。</p>
{actions}
</main>
<script>
(() => {{ const text = {json.dumps(summary_text, ensure_ascii=False)}; const status = document.querySelector('[data-status]'); document.querySelector('[data-copy]')?.addEventListener('click', async () => {{ try {{ await navigator.clipboard.writeText(text); status.textContent = '摘要已复制'; }} catch (_) {{ status.textContent = '复制失败，请手动选择'; }} }}); }})();
</script></body></html>"""


def build_share_card(snapshot: dict[str, Any]) -> str:
    return build_public_page(snapshot, card=True, sort_mode="quota")


def append_history(snapshot: dict[str, Any]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    checked = parse_timestamp(snapshot["checked_at"])
    path = HISTORY_DIR / f"{checked:%Y-%m}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控纳指100 QDII 人民币份额的公开直销申购状态。")
    parser.add_argument("--dry-run", action="store_true", help="只生成预览，不写状态、不推送")
    parser.add_argument("--live", action="store_true", help="抓取配置中的基金管理人来源或公开基金详情页")
    parser.add_argument("--baseline", action="store_true", help="把当前抓取结果写为基线，不发送通知")
    parser.add_argument("--export-share", action="store_true", help="生成公开快照与长图 HTML，不推送")
    parser.add_argument(
        "--allow-review-pending",
        action="store_true",
        help="仅手动试发时允许带待核验记录生成/发送；定时任务默认禁止",
    )
    parser.add_argument("--daily-now", action="store_true", help="立即发送一次完整日报，用于验收")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="QDII 配置文件")
    return parser.parse_args(argv)


def run(argv: list[str], *, now: Optional[datetime] = None) -> int:
    args = parse_args(argv)
    checked_at = now or utc_now()
    config = load_json(args.config, {})
    if config.get("schema_version") != 1:
        print(f"配置无效：{args.config}", file=sys.stderr)
        return 2
    funds_config = load_funds(config)
    if not funds_config:
        print(f"配置中没有基金：{args.config}", file=sys.stderr)
        return 2
    # Version 1 used the same per-fund comparison fields as version 2; retain
    # that baseline instead of silently treating it as empty.  Reset only
    # malformed/unknown versions, otherwise historic quota changes disappear
    # from the digest and every run reports zero changes.
    state = normalize_state(load_json(STATE_PATH, {"version": STATE_VERSION, "funds": {}}))
    if args.baseline:
        state = {"version": STATE_VERSION, "funds": {}}
    funds = collect_funds(funds_config, live=args.live, checked_at=checked_at)
    snapshot = build_snapshot(funds, state, checked_at)
    print(f"[OK] {PRODUCT_NAME_ZH}：{snapshot['total']} 个份额，状态变化 {snapshot['changed_count']} 项")
    for status in STATUS_ORDER:
        print(f"  - {status}：{snapshot['counts'].get(status, 0)}")
    for error in snapshot["errors"]:
        print(f"[FAIL] {error['code']}：{error['error']}", file=sys.stderr)
    for warning in snapshot.get("warnings", []):
        print(f"[WARN] {warning['code']}：{warning['warning']}", file=sys.stderr)
    if snapshot["errors"]:
        print("[BLOCK] 数据采集存在结构性错误，本次不发送通知。", file=sys.stderr)
        return 1

    public_url = str(config.get("public_share_url", "")).strip() if config.get("public_share_enabled") else ""
    image_url = str(config.get("public_image_url", "")).strip() if config.get("public_share_enabled") else ""
    if config.get("public_share_enabled"):
        image_url = os.environ.get("QDII_PUBLIC_IMAGE_URL", "").strip() or image_url
    if args.export_share:
        if (
            args.live
            and config.get("enforce_review_gate", True)
            and snapshot.get("review_pending")
            and not args.allow_review_pending
        ):
            print(f"[BLOCK] 公开快照包含 {snapshot['review_pending']} 条待核验记录；先通过审核门禁。", file=sys.stderr)
            return 1
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(build_report(snapshot), encoding="utf-8")
        PUBLIC_REPORT_PATH.write_text(build_public_page(snapshot, public_url=public_url, sort_mode="quota"), encoding="utf-8")
        SHARE_CARD_PATH.write_text(build_share_card(snapshot), encoding="utf-8")
        print(f"[SHARE] 公开快照：{PUBLIC_REPORT_PATH}")
        print(f"[SHARE] 分享卡片：{SHARE_CARD_PATH}")
        return 1 if snapshot["errors"] else 0

    if args.baseline:
        state["funds"] = {
            row["code"]: {
                "status": row["status"],
                "purchase_limit_rmb": row.get("purchase_limit_rmb"),
                "regular_investment": row.get("regular_investment"),
                "effective_date": row.get("effective_date"),
                "checked_at": row.get("checked_at"),
            }
            for row in snapshot["records"]
        }
        write_json_atomic(STATE_PATH, state)
        append_history(snapshot)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(build_report(snapshot), encoding="utf-8")
        PUBLIC_REPORT_PATH.write_text(build_public_page(snapshot, public_url=public_url, sort_mode="quota"), encoding="utf-8")
        SHARE_CARD_PATH.write_text(build_share_card(snapshot), encoding="utf-8")
        print("[BASELINE] 已写入当前真实状态，不发送通知。")
        return 1 if snapshot["errors"] else 0

    if (
        args.live
        and not args.dry_run
        and config.get("enforce_review_gate", True)
        and snapshot.get("review_pending")
        and not args.allow_review_pending
    ):
        print(f"[BLOCK] 通知门禁：{snapshot['review_pending']} 条记录来源待核验，未发送日报。", file=sys.stderr)
        return 1

    notification = {"ok": True, "reason": "no alerts", "kind": "none"}
    digest_due = args.daily_now or daily_digest_due(config, state, checked_at)
    if digest_due:
        title, body = build_daily_digest(snapshot, image_url=image_url, public_url=public_url, checked_at=checked_at)
        notification = notify.push(
            title,
            body,
            dry_run=args.dry_run,
            timezone_name=config.get("daily_digest", {}).get("timezone", CUSTOMER_TIMEZONE_NAME),
        )
        notification["kind"] = "daily_digest"
        if notification.get("ok") and not args.dry_run:
            local = checked_at.astimezone(ZoneInfo(config.get("daily_digest", {}).get("timezone", CUSTOMER_TIMEZONE_NAME)))
            state["last_daily_digest_date"] = local.date().isoformat()
    elif snapshot["changed_count"]:
        title, body = build_change_alert(snapshot)
        notification = notify.push(
            title,
            body,
            dry_run=args.dry_run,
            timezone_name=config.get("daily_digest", {}).get("timezone", CUSTOMER_TIMEZONE_NAME),
        )
        notification["kind"] = "status_change"

    if args.dry_run:
        print("[DRY-RUN] 不写状态、不写历史、不生成持久化报告。")
        return 0 if notification.get("ok") else 1

    if notification.get("kind") != "none":
        outcome = "成功" if notification.get("ok") else "失败"
        print(f"[NOTIFY] {notification['kind']}：{outcome} · {notification.get('reason', '')}")

    state["funds"] = {
        row["code"]: {
            "status": row["status"],
            "purchase_limit_rmb": row.get("purchase_limit_rmb"),
            "regular_investment": row.get("regular_investment"),
            "effective_date": row.get("effective_date"),
            "checked_at": row.get("checked_at"),
        }
        for row in snapshot["records"]
    }
    write_json_atomic(STATE_PATH, state)
    append_history(snapshot)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(build_report(snapshot), encoding="utf-8")
    PUBLIC_REPORT_PATH.write_text(build_public_page(snapshot, public_url=public_url, sort_mode="quota"), encoding="utf-8")
    SHARE_CARD_PATH.write_text(build_share_card(snapshot), encoding="utf-8")
    return 1 if snapshot["errors"] or not notification.get("ok") else 0


def main(argv: list[str]) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{PRODUCT_NAME_ZH}已有实例在运行，本次跳过。")
            return 0
        return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
