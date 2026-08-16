#!/usr/bin/env python3
"""推送前 QDII 数据门禁。

只做本地审查，不抓取、不写入状态、不发送通知。
退出码 0 表示可以继续；退出码 1 表示必须先修正或人工确认。
"""

from __future__ import annotations

import csv
import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


SYSTEM_DIR = Path(__file__).resolve().parents[1]
CSV_PATH = SYSTEM_DIR / "Config" / "qdii_universe_template.csv"
REFERENCE_PATH = SYSTEM_DIR / "Data" / "qdii" / "reference_latest.json"
ALLOWED_SOURCE_MODES = {
    "official_direct_html",
    "official_direct",
    "official_notice",
    "official_notice_publication",
}
ALLOWED_STATUS = {"正常开放", "限制申购", "暂停申购", "境外休市", "未确认"}

# 只允许基金管理人常用官方域名；公告镜像、代销平台和信息聚合站点必须人工替换。
OFFICIAL_HOST_SUFFIXES = {
    "ccbfund.cn",          # 建信
    "eid.csrc.gov.cn",     # 中国证监会基金信息披露监管平台
    "thfund.com.cn",       # 天弘
    "jpmorgan.com",        # 摩根基金/摩根资产管理
    "cifm.com",            # 摩根基金管理（中国）有限公司
    "wjasset.com",         # 万家
    "huatai-pb.com",       # 华泰柏瑞
    "byfunds.com",         # 宝盈
    "gtfund.com",          # 国泰
    "efunds.com.cn",       # 易方达
    "chinaamc.com",        # 华夏
    "cmfchina.com",        # 招商
    "bosera.com",          # 博时
    "dcfund.com.cn",       # 大成
    "gffunds.com.cn",      # 广发
    "huaan.com.cn",        # 华安
    "jsfund.cn",           # 嘉实
    "southernfund.com",    # 南方
    "nffund.com",          # 南方新版站点
    "99fund.com",          # 汇添富
    "stcn.com",            # 证券时报指定信息披露报刊电子版
}

BLOCKED_HOST_HINTS = {
    "howbuy.com", "10jqka.com.cn", "dfcfw.com", "eastmoney.com",
    "cs.com.cn", "cninfo.com.cn", "天天基金",
}
NOTICE_MAX_AGE_DAYS = 180


def source_review_flags(row: dict[str, str], *, today: Optional[date] = None) -> list[str]:
    """返回单条记录的来源/时效警告；这些警告不会阻断其他基金。"""
    flags: list[str] = []
    url = row.get("source_url", "").strip()
    if not url:
        flags.append("source_missing")
    elif (urlparse(url).scheme or "").lower() != "https":
        flags.append("source_non_https")
    elif not host_is_official(url):
        flags.append("source_non_official")
    # 官方详情页记录的是“最近页面核验时间”，不一定存在公告生效日；
    # 只有公告型来源才强制要求 ISO 格式的 effective_date。
    source_mode = row.get("source_mode", "").strip()
    if source_mode in {"official_notice", "official_notice_publication"}:
        effective = row.get("effective_date", "").strip()
        try:
            effective_date = date.fromisoformat(effective)
            if effective_date > (today or date.today()):
                flags.append("effective_date_future")
            elif effective_date < (today or date.today()) - timedelta(days=NOTICE_MAX_AGE_DAYS):
                flags.append("effective_date_old")
        except ValueError:
            flags.append("effective_date_missing")
    return flags


def host_is_official(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if any(hint in host for hint in BLOCKED_HOST_HINTS if "." in hint):
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in OFFICIAL_HOST_SUFFIXES)


def reference_limit(value: object) -> Optional[float]:
    if value is None or str(value).strip() in {"", "-", "未披露", "不限", "无限制"}:
        return None
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(万|万元|元)", text)
    if not match:
        return None
    amount = float(match.group(1))
    return amount * 10_000 if match.group(2) in {"万", "万元"} else amount


def load_reference(path: Path) -> tuple[dict[str, dict], Optional[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, "reference_missing"
    if not payload.get("ok", True):
        return {}, "reference_fetch_failed"
    rows = payload.get("records", payload.get("funds", []))
    if not isinstance(rows, list):
        return {}, "reference_invalid"
    return {str(row.get("code", "")).strip(): row for row in rows if row.get("code")}, None


def main() -> int:
    parser = argparse.ArgumentParser(description="审查纳指100 QDII 数据后再生成日报")
    parser.add_argument("--reference", type=Path, default=REFERENCE_PATH)
    parser.add_argument(
        "--strict-reference",
        action="store_true",
        help="将安信乐二级来源与本地值的冲突升级为阻断；不自动把二级来源写入 CSV",
    )
    args = parser.parse_args()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.DictReader(handle) if row.get("code", "").strip()]
    except OSError as exc:
        print(f"[BLOCK] 无法读取 CSV：{exc}")
        return 1

    required = {"code", "name", "manager", "share_class", "direct_sales", "channel", "status", "source_url", "source_mode", "effective_date", "status_verified_at"}
    missing_columns = required - set(rows[0]) if rows else required
    if missing_columns:
        errors.append("缺少字段：" + ", ".join(sorted(missing_columns)))

    codes = [row.get("code", "").strip() for row in rows]
    duplicates = sorted(code for code, count in Counter(codes).items() if count > 1)
    if duplicates:
        errors.append("代码重复：" + ", ".join(duplicates))

    reference, reference_error = load_reference(args.reference)
    if reference_error:
        warnings.append(f"安信乐二级来源：{reference_error}；不覆盖官方直销口径")

    today = date.today()
    for row in rows:
        code = row.get("code", "").strip()
        prefix = f"{code} {row.get('name', '').strip()}"
        official_authoritative = (
            row.get("source_mode", "").strip() in ALLOWED_SOURCE_MODES
            and host_is_official(row.get("source_url", ""))
        )
        if row.get("direct_sales", "").strip().lower() not in {"true", "1", "yes"}:
            errors.append(f"{prefix}：不是直销标记")
        if row.get("channel", "").strip() != "manager_direct":
            errors.append(f"{prefix}：channel 不是 manager_direct")
        if row.get("source_mode", "").strip() not in ALLOWED_SOURCE_MODES:
            errors.append(f"{prefix}：source_mode 不受支持")
        if row.get("status", "").strip() not in ALLOWED_STATUS:
            errors.append(f"{prefix}：状态不在允许枚举中")
        row_flags = source_review_flags(row, today=today)
        for flag in row_flags:
            host = urlparse(row.get("source_url", "")).netloc or "空链接"
            label = {
                "source_missing": "缺少来源链接",
                "source_non_official": f"来源不是基金管理人或指定披露报刊域名（{host}）",
                "source_non_https": "来源不是 HTTPS，禁止实时抓取",
                "effective_date_future": "effective_date 晚于当前日期",
                "effective_date_missing": "effective_date 无法解析",
                "effective_date_old": f"公告生效日超过 {NOTICE_MAX_AGE_DAYS} 天，需确认是否仍有效",
            }[flag]
            warnings.append(f"{prefix}：{label}")
        ref = reference.get(code)
        if ref:
            ref_status = str(ref.get("status") or "").strip()
            # 安信乐存在“场内交易”和代销汇总，只有明确写出直销额度时才
            # 比较金额；否则仅报告发现，不把它升级为官方事实。
            direct_limit = reference_limit(ref.get("direct_limit_rmb"))
            local_limit = reference_limit(row.get("purchase_limit_rmb"))
            if direct_limit is not None and local_limit is not None and not math.isclose(direct_limit, local_limit):
                message = f"{prefix}：二级来源直销额度 ¥{direct_limit:g} 与本地 ¥{local_limit:g} 冲突"
                (errors if args.strict_reference else warnings).append(message)
            if ref_status and ref_status not in {"场内交易", "交易"}:
                normalized = {
                    "开放": "正常开放",
                    "开放申购": "正常开放",
                    "限大额": "限制申购",
                    "限制申购": "限制申购",
                    "暂停": "暂停申购",
                    "暂停申购": "暂停申购",
                }.get(ref_status)
                if normalized and normalized != row.get("status", "").strip():
                    message = f"{prefix}：二级来源状态“{ref_status}”与本地“{row.get('status', '').strip()}”冲突"
                    (errors if args.strict_reference else warnings).append(message)
            channel_note = str(ref.get("channel_note") or "")
            if re.search(r"直销[^；。,，]{0,20}暂停申购", channel_note) and row.get("status", "").strip() != "暂停申购":
                message = f"{prefix}：二级来源注明直销暂停申购，但官方来源状态为“{row.get('status', '').strip()}”；保留官方口径"
                (warnings if official_authoritative or not args.strict_reference else errors).append(message)
        limit = row.get("purchase_limit_rmb", "").strip()
        if row.get("status", "").strip() == "限制申购":
            try:
                if float(limit) <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{prefix}：限制申购额度必须为正数")
        if row.get("status", "").strip() == "暂停申购" and limit:
            errors.append(f"{prefix}：暂停申购不应填写额度")

    counts = Counter(row.get("status", "").strip() for row in rows)
    print(f"[REVIEW] rows={len(rows)} unique_codes={len(set(codes))} counts={dict(counts)}")
    if warnings:
        print(f"[WARN] 审查发现 {len(warnings)} 项来源/二级口径提醒；它们不会覆盖本地官方基线：")
        for warning in warnings:
            print(f"- {warning}")
    if errors:
        print(f"[BLOCK] 推送前审查失败，共 {len(errors)} 项结构性错误：")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"[PASS] 结构、状态和额度检查通过；可继续生成/发布。审查提醒：{len(warnings)} 项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
