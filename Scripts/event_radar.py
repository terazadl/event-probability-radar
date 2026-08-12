#!/usr/bin/env python3
"""只读事件概率雷达：抓公开市场数据、过滤噪音、按需推送微信。

不连接钱包，不包含下单接口。第一次运行只建立基线；后续只有确认后的
概率跳变、关键阈值穿越、规则变化或市场关闭才会触发通知。

用法：
    .venv/bin/python3 Scripts/event_radar.py --dry-run
    .venv/bin/python3 Scripts/event_radar.py
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import notify


SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SYSTEM_DIR / "Config" / "event_watchlist.json"
DATA_DIR = SYSTEM_DIR / "Data" / "events"
STATE_PATH = DATA_DIR / "state.json"
LOCK_PATH = DATA_DIR / "radar.lock"
HISTORY_DIR = DATA_DIR / "history"
REPORT_PATH = SYSTEM_DIR / "Reports" / "事件概率雷达.md"
PUBLIC_REPORT_PATH = SYSTEM_DIR / "Reports" / "事件概率雷达·公开快照.html"
SHARE_CARD_PATH = SYSTEM_DIR / "Reports" / "事件概率雷达·分享卡片.html"

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/{market_id}"
CURL_TIMEOUT_SECONDS = 30
STATE_VERSION = 2
PRODUCT_NAME_ZH = "Polymarket观测站"
CUSTOMER_TIMEZONE_NAME = "Asia/Shanghai"
CUSTOMER_TIMEZONE_LABEL = "北京时间"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def fetch_market(market_id: str) -> dict:
    url = GAMMA_MARKET_URL.format(market_id=market_id)
    command = [
        "curl", "-L", "--http1.1", "--ipv4", "-fsSL",
        "--connect-timeout", "10", "--max-time", str(CURL_TIMEOUT_SECONDS),
        "--retry", "2", "--retry-delay", "1", "--retry-all-errors", url,
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=CURL_TIMEOUT_SECONDS + 10,
    )
    payload = json.loads(result.stdout)
    if str(payload.get("id")) != str(market_id):
        raise RuntimeError(f"市场 ID 不匹配：请求 {market_id}，返回 {payload.get('id')}")
    return payload


def parse_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    raise ValueError("市场字段不是列表")


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def yes_quote(market: dict) -> tuple[float, float, float]:
    """返回 Yes 的 bid、ask、mid；盘口缺失时才退回 outcomePrices。"""
    bid = as_float(market.get("bestBid"))
    ask = as_float(market.get("bestAsk"))
    if bid is None or ask is None or ask < bid:
        outcomes = parse_json_list(market.get("outcomes"))
        prices = parse_json_list(market.get("outcomePrices"))
        try:
            price = float(prices[outcomes.index("Yes")])
        except (ValueError, IndexError, TypeError) as exc:
            raise ValueError("无法取得 Yes 报价") from exc
        bid = ask = price
    return bid, ask, (bid + ask) / 2.0


def component_quote(component: dict, market: dict) -> dict:
    yes_bid, yes_ask, yes_mid = yes_quote(market)
    outcome = component["outcome"]
    if outcome == "Yes":
        bid, ask, midpoint = yes_bid, yes_ask, yes_mid
    elif outcome == "No":
        bid, ask, midpoint = 1.0 - yes_ask, 1.0 - yes_bid, 1.0 - yes_mid
    else:
        raise ValueError(f"不支持的 outcome：{outcome}")

    description = str(market.get("description") or "")
    return {
        "market_id": str(market.get("id")),
        "question": str(market.get("question") or ""),
        "slug": str(market.get("slug") or ""),
        "label": component["label"],
        "outcome": outcome,
        "bid": bid,
        "ask": ask,
        "probability": midpoint,
        "spread": max(0.0, ask - bid),
        "liquidity_usd": as_float(market.get("liquidity"), 0.0) or 0.0,
        "volume_24h_usd": as_float(market.get("volume24hr"), 0.0) or 0.0,
        "open": bool(market.get("active"))
        and not bool(market.get("closed"))
        and bool(market.get("acceptingOrders")),
        "description": description,
    }


def event_components(event: dict) -> list[dict]:
    """返回事件依赖的全部底层市场组件。"""
    if event.get("mode", "scalar") == "distribution":
        return [
            component
            for bucket in event["buckets"]
            for component in bucket["components"]
        ]
    return event["components"]


def display_percentages(probabilities: list[float], digits: int = 1) -> list[float]:
    """用最大余数法四舍五入，保证客户看到的百分比合计恰好为100%。"""
    total = sum(probabilities)
    if total <= 0:
        raise ValueError("概率合计必须大于零")
    scale = 10 ** digits
    target_units = 100 * scale
    raw_units = [probability / total * target_units for probability in probabilities]
    units = [int(value) for value in raw_units]
    remainder = target_units - sum(units)
    order = sorted(
        range(len(raw_units)),
        key=lambda index: raw_units[index] - units[index],
        reverse=True,
    )
    for index in order[:remainder]:
        units[index] += 1
    return [value / scale for value in units]


def build_snapshot(event: dict, markets: dict[str, dict], now: datetime) -> dict:
    mode = event.get("mode", "scalar")
    components = []
    distribution = []
    normalization_total = None

    if mode == "distribution":
        for bucket in event["buckets"]:
            rows = [
                component_quote(component, markets[str(component["market_id"])])
                for component in bucket["components"]
            ]
            components.extend(rows)
            distribution.append({
                "id": bucket["id"],
                "label_zh": bucket["label_zh"],
                "raw_probability": sum(row["probability"] for row in rows),
                "spread": sum(row["spread"] for row in rows),
            })
        normalization_total = sum(row["raw_probability"] for row in distribution)
        if normalization_total <= 0:
            raise ValueError("互斥结果的概率合计必须大于零")
        for row in distribution:
            row["probability"] = row["raw_probability"] / normalization_total
            row["probability_pct"] = row["probability"] * 100
            row["spread_pp"] = row.pop("spread") * 100
        outcome_probabilities = [
            row["probability"] / normalization_total for row in components
        ]
        outcome_display_percentages = display_percentages(outcome_probabilities)
        outcomes = [
            {
                "market_id": row["market_id"],
                "label_zh": row["label"],
                "raw_probability": row["probability"],
                "probability": probability,
                "probability_pct": probability * 100,
                "display_probability_pct": display_probability_pct,
            }
            for row, probability, display_probability_pct in zip(
                components, outcome_probabilities, outcome_display_percentages
            )
        ]
        spread = max(row["spread_pp"] for row in distribution) / 100
        leader = max(distribution, key=lambda row: row["probability"])
        outcome_leader = max(outcomes, key=lambda row: row["probability"])
    elif mode == "scalar":
        components = [
            component_quote(component, markets[str(component["market_id"])])
            for component in event["components"]
        ]
        probability = min(1.0, max(0.0, sum(row["probability"] for row in components)))
        spread = sum(row["spread"] for row in components)
        leader = None
    else:
        raise ValueError(f"不支持的事件模式：{mode}")

    liquidity = sum(row["liquidity_usd"] for row in components)
    volume_24h = sum(row["volume_24h_usd"] for row in components)
    is_open = all(row["open"] for row in components)

    quality = event["quality"]
    quality_reasons = []
    if not is_open:
        quality_reasons.append("至少一个底层市场未开放")
    if liquidity < float(quality["min_liquidity_usd"]):
        quality_reasons.append("流动性低于门槛")
    if spread * 100 > float(quality["max_spread_pp"]):
        quality_reasons.append("买卖价差超过门槛")
    if mode == "distribution":
        raw_total_pct = normalization_total * 100
        if raw_total_pct < float(quality["min_raw_total_pct"]):
            quality_reasons.append("互斥结果原始概率合计过低")
        if raw_total_pct > float(quality["max_raw_total_pct"]):
            quality_reasons.append("互斥结果原始概率合计过高")

    rules_text = "\n".join(row["description"] for row in components)
    rules_hash = hashlib.sha256(rules_text.encode("utf-8")).hexdigest()[:16]
    snapshot = {
        "event_id": event["id"],
        "label_zh": event["label_zh"],
        "mode": mode,
        "timestamp": isoformat_utc(now),
        "spread_pp": spread * 100,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "open": is_open,
        "quality_ok": not quality_reasons,
        "quality_reasons": quality_reasons,
        "rules_hash": rules_hash,
        "components": components,
        "source_name": event["source_name"],
        "source_url": event["source_url"],
        "resolution_source_url": event["resolution_source_url"],
        "resolution_summary_zh": event["resolution_summary_zh"],
        "notification_title_zh": event.get("notification_title_zh", event["label_zh"]),
        "customer_question_zh": event.get("customer_question_zh", event["label_zh"]),
        "resolution_source_name": event.get("resolution_source_name", "最终判定依据"),
        "resolution_metric_zh": event.get(
            "resolution_metric_zh", event["resolution_summary_zh"]
        ),
        "resolution_rule_zh": event.get(
            "resolution_rule_zh", event["resolution_summary_zh"]
        ),
        "source_explainer_zh": event.get("source_explainer_zh", ""),
    }
    if mode == "distribution":
        snapshot.update({
            "distribution": distribution,
            "outcomes": outcomes,
            "probabilities": {row["id"]: row["probability"] for row in distribution},
            "normalization_total": normalization_total,
            "leader": {
                "id": leader["id"],
                "label_zh": leader["label_zh"],
                "probability": leader["probability"],
            },
            "outcome_leader": {
                "market_id": outcome_leader["market_id"],
                "label_zh": outcome_leader["label_zh"],
                "probability": outcome_leader["probability"],
                "display_probability_pct": outcome_leader["display_probability_pct"],
            },
        })
    else:
        snapshot.update({
            "probability": probability,
            "probability_pct": probability * 100,
        })
    return snapshot


def compact_sample(snapshot: dict) -> dict:
    sample = {
        "timestamp": snapshot["timestamp"],
        "quality_ok": snapshot["quality_ok"],
        "open": snapshot["open"],
        "rules_hash": snapshot["rules_hash"],
    }
    if snapshot["mode"] == "distribution":
        sample["probabilities"] = snapshot["probabilities"]
        sample["leader_id"] = snapshot["leader"]["id"]
    else:
        sample["probability"] = snapshot["probability"]
    return sample


def sample_probability(sample: dict, bucket_id: Optional[str] = None) -> Optional[float]:
    if bucket_id is None:
        return as_float(sample.get("probability"))
    return as_float(sample.get("probabilities", {}).get(bucket_id))


def reference_sample(samples: list[dict], at: datetime, hours: int) -> Optional[dict]:
    target = at - timedelta(hours=hours)
    eligible = [sample for sample in samples if parse_timestamp(sample["timestamp"]) <= target]
    if not eligible:
        return None
    reference = max(eligible, key=lambda item: parse_timestamp(item["timestamp"]))
    age = at - parse_timestamp(reference["timestamp"])
    if age > timedelta(hours=hours * 1.5 + 0.5):
        return None
    return reference


def confirmed_change(
    samples: list[dict], *, hours: int, threshold_pp: float, confirmations: int,
    bucket_id: Optional[str] = None,
) -> Optional[dict]:
    if len(samples) < confirmations:
        return None
    recent = samples[-confirmations:]
    deltas = []
    for sample in recent:
        if not sample.get("quality_ok"):
            return None
        at = parse_timestamp(sample["timestamp"])
        reference = reference_sample(samples, at, hours)
        if reference is None or not reference.get("quality_ok"):
            return None
        current_probability = sample_probability(sample, bucket_id)
        reference_probability = sample_probability(reference, bucket_id)
        if current_probability is None or reference_probability is None:
            return None
        deltas.append((current_probability - reference_probability) * 100)
    if all(delta >= threshold_pp for delta in deltas):
        return {"kind": f"change_{hours}h_up", "delta_pp": deltas[-1]}
    if all(delta <= -threshold_pp for delta in deltas):
        return {"kind": f"change_{hours}h_down", "delta_pp": deltas[-1]}
    return None


def confirmed_threshold_crossings(
    samples: list[dict], thresholds_pct: list[float], confirmations: int,
    bucket_id: Optional[str] = None,
) -> list[dict]:
    if len(samples) < confirmations + 1:
        return []
    before = samples[-confirmations - 1]
    recent = samples[-confirmations:]
    if not before.get("quality_ok") or not all(row.get("quality_ok") for row in recent):
        return []
    triggers = []
    for threshold in thresholds_pct:
        boundary = float(threshold) / 100.0
        before_probability = sample_probability(before, bucket_id)
        recent_probabilities = [sample_probability(row, bucket_id) for row in recent]
        if before_probability is None or any(value is None for value in recent_probabilities):
            continue
        if before_probability < boundary and all(value >= boundary for value in recent_probabilities):
            triggers.append({"kind": "threshold_up", "threshold_pct": float(threshold)})
        elif before_probability > boundary and all(value <= boundary for value in recent_probabilities):
            triggers.append({"kind": "threshold_down", "threshold_pct": float(threshold)})
    return triggers


def current_delta(
    samples: list[dict], hours: int, bucket_id: Optional[str] = None
) -> Optional[float]:
    if not samples:
        return None
    current = samples[-1]
    reference = reference_sample(samples, parse_timestamp(current["timestamp"]), hours)
    if reference is None:
        return None
    current_probability = sample_probability(current, bucket_id)
    reference_probability = sample_probability(reference, bucket_id)
    if current_probability is None or reference_probability is None:
        return None
    return (current_probability - reference_probability) * 100


def confirmed_leader_change(samples: list[dict], confirmations: int) -> Optional[dict]:
    if len(samples) < confirmations + 1:
        return None
    before = samples[-confirmations - 1]
    recent = samples[-confirmations:]
    if not before.get("quality_ok") or not all(row.get("quality_ok") for row in recent):
        return None
    previous_leader = before.get("leader_id")
    current_leader = recent[-1].get("leader_id")
    if (
        previous_leader
        and current_leader
        and previous_leader != current_leader
        and all(row.get("leader_id") == current_leader for row in recent)
    ):
        return {
            "kind": "leader_changed",
            "from_bucket_id": previous_leader,
            "bucket_id": current_leader,
        }
    return None


def detect_triggers(event: dict, event_state: dict, snapshot: dict) -> tuple[list[dict], list[dict]]:
    previous_samples = list(event_state.get("samples", []))
    previous = previous_samples[-1] if previous_samples else None
    samples = previous_samples + [compact_sample(snapshot)]
    cutoff = parse_timestamp(snapshot["timestamp"]) - timedelta(hours=36)
    samples = [row for row in samples if parse_timestamp(row["timestamp"]) >= cutoff]
    if previous is None:
        return [], samples

    triggers: list[dict] = []
    settings = event["triggers"]
    confirmations = int(settings["confirmation_samples"])
    if snapshot["quality_ok"]:
        buckets = snapshot.get("distribution") or [{"id": None, "label_zh": None}]
        for bucket in buckets:
            bucket_id = bucket["id"]
            bucket_label = bucket["label_zh"]
            bucket_triggers = []
            for hours, key in ((1, "change_1h_pp"), (24, "change_24h_pp")):
                change = confirmed_change(
                    samples,
                    hours=hours,
                    threshold_pp=float(settings[key]),
                    confirmations=confirmations,
                    bucket_id=bucket_id,
                )
                if change:
                    bucket_triggers.append(change)
            bucket_triggers.extend(
                confirmed_threshold_crossings(
                    samples,
                    settings["thresholds_pct"],
                    confirmations,
                    bucket_id=bucket_id,
                )
            )
            for trigger in bucket_triggers:
                if bucket_id is not None:
                    trigger["bucket_id"] = bucket_id
                    trigger["bucket_label"] = bucket_label
                triggers.append(trigger)
        if snapshot["mode"] == "distribution":
            leader_change = confirmed_leader_change(samples, confirmations)
            if leader_change:
                labels = {row["id"]: row["label_zh"] for row in snapshot["distribution"]}
                leader_change["from_bucket_label"] = labels.get(
                    leader_change["from_bucket_id"], leader_change["from_bucket_id"]
                )
                leader_change["bucket_label"] = labels.get(
                    leader_change["bucket_id"], leader_change["bucket_id"]
                )
                triggers.append(leader_change)

    if previous.get("rules_hash") != snapshot["rules_hash"]:
        triggers.append({"kind": "rules_changed"})
    if previous.get("open") and not snapshot["open"]:
        triggers.append({"kind": "market_closed"})
    return triggers, samples


def trigger_text(trigger: dict) -> str:
    kind = trigger["kind"]
    prefix = f"{trigger['bucket_label']}：" if trigger.get("bucket_label") else ""
    if kind.startswith("change_"):
        window = "1小时" if "1h" in kind else "24小时"
        return f"{prefix}{window}变化 {trigger['delta_pp']:+.1f} 个百分点"
    if kind == "threshold_up":
        return f"{prefix}向上突破 {trigger['threshold_pct']:.0f}%"
    if kind == "threshold_down":
        return f"{prefix}向下跌破 {trigger['threshold_pct']:.0f}%"
    if kind == "leader_changed":
        return f"领先结果由{trigger['from_bucket_label']}变为{trigger['bucket_label']}"
    if kind == "rules_changed":
        return "底层市场裁决规则发生变化"
    if kind == "market_closed":
        return "底层市场已停止接受订单，需检查是否裁决"
    return kind


def cooldown_allows(event: dict, event_state: dict, snapshot: dict, triggers: list[dict]) -> bool:
    if not triggers:
        return False
    if any(row["kind"] in {"rules_changed", "market_closed"} for row in triggers):
        return True
    last_alert_at = event_state.get("last_alert_at")
    if not last_alert_at:
        return True
    elapsed = parse_timestamp(snapshot["timestamp"]) - parse_timestamp(last_alert_at)
    cooldown = timedelta(hours=float(event["triggers"]["cooldown_hours"]))
    if elapsed >= cooldown:
        return True
    if snapshot["mode"] == "distribution":
        previous_probabilities = event_state.get("last_alert_probabilities", {})
        if not previous_probabilities:
            return False
        moved_pp = max(
            abs(probability - as_float(previous_probabilities.get(bucket_id), probability)) * 100
            for bucket_id, probability in snapshot["probabilities"].items()
        )
    else:
        previous_probability = as_float(event_state.get("last_alert_probability"))
        if previous_probability is None:
            return False
        moved_pp = abs(snapshot["probability"] - previous_probability) * 100
    return moved_pp >= float(event["triggers"]["realert_change_pp"])


def format_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def distribution_text(snapshot: dict, separator: str = "｜") -> str:
    return separator.join(
        f"{row['label_zh']} {row['probability_pct']:.1f}%"
        for row in snapshot["distribution"]
    )


def customer_timestamp(timestamp: str) -> str:
    local_time = parse_timestamp(timestamp).astimezone(
        ZoneInfo(CUSTOMER_TIMEZONE_NAME)
    )
    return f"{local_time.strftime('%Y-%m-%d %H:%M')}（{CUSTOMER_TIMEZONE_LABEL}）"


def notification_headline(snapshot: dict) -> str:
    subject = snapshot["notification_title_zh"]
    if snapshot["mode"] == "distribution":
        leader = snapshot["outcome_leader"]
        return (
            f"{subject}：{leader['label_zh']} "
            f"{leader['display_probability_pct']:.1f}%"
        )
    return f"{subject}：{snapshot['probability_pct']:.1f}%"


def build_notification(
    candidates: list[dict], share_url: Optional[str] = None
) -> tuple[str, str]:
    if len(candidates) == 1:
        title = f"{PRODUCT_NAME_ZH}｜{notification_headline(candidates[0]['snapshot'])}"
    else:
        title = f"{PRODUCT_NAME_ZH}｜{len(candidates)}项事件出现重要变化"
    sections = []
    for candidate in candidates:
        snapshot = candidate["snapshot"]
        samples = candidate["samples"]
        changes = []
        if snapshot["mode"] == "distribution":
            for bucket in snapshot["distribution"]:
                delta_1h = current_delta(samples, 1, bucket["id"])
                delta_24h = current_delta(samples, 24, bucket["id"])
                bucket_changes = []
                if delta_1h is not None:
                    bucket_changes.append(f"1小时 {delta_1h:+.1f}pp")
                if delta_24h is not None:
                    bucket_changes.append(f"24小时 {delta_24h:+.1f}pp")
                if bucket_changes:
                    changes.append(f"{bucket['label_zh']} " + "、".join(bucket_changes))
            outcome_lines = "\n".join(
                f"  - {row['label_zh']}：**{row['display_probability_pct']:.1f}%**"
                for row in snapshot["outcomes"]
            )
            probability_line = (
                f"- 当前最可能：**{snapshot['outcome_leader']['label_zh']} "
                f"{snapshot['outcome_leader']['display_probability_pct']:.1f}%**\n"
                f"- 五个互斥结果（合计100%）：\n{outcome_lines}\n"
            )
        else:
            delta_1h = current_delta(samples, 1)
            delta_24h = current_delta(samples, 24)
            if delta_1h is not None:
                changes.append(f"1小时 {delta_1h:+.1f}pp")
            if delta_24h is not None:
                changes.append(f"24小时 {delta_24h:+.1f}pp")
            probability_line = (
                f"- {snapshot['customer_question_zh']}："
                f"**{snapshot['probability_pct']:.1f}%**\n"
            )
        change_text = "；".join(changes) if changes else "历史尚不足以计算窗口变化"
        reasons = "；".join(trigger_text(row) for row in candidate["triggers"])
        share_line = f"\n- [查看并分享公开快照]({share_url})" if share_url else ""
        source_explainer = (
            f"\n\n> {snapshot['source_explainer_zh']}"
            if snapshot.get("source_explainer_zh") else ""
        )
        sections.append(
            f"## {snapshot['label_zh']}\n\n"
            f"**市场现在怎么押**\n\n"
            f"{probability_line}"
            f"- 相比此前：{change_text}\n"
            f"- 为什么提醒：{reasons}\n\n"
            f"**最后怎么判**\n\n"
            f"- 市场概率：[{snapshot['source_name']}]({snapshot['source_url']})\n"
            f"- 判定指标：[{snapshot['resolution_source_name']}]"
            f"({snapshot['resolution_source_url']}) · {snapshot['resolution_metric_zh']}\n"
            f"- 判定规则：{snapshot['resolution_rule_zh']}\n"
            f"- 数据时间：{customer_timestamp(snapshot['timestamp'])}"
            f"{share_line}{source_explainer}"
        )
    sections.append(
        "---\n\n市场概率会变化，仅反映Polymarket参与者当时的预期；"
        "不是事实概率，也不构成交易建议。\n\n"
        "本产品为非官方观测工具，与Polymarket无隶属或合作关系。"
    )
    return title, "\n\n".join(sections)


def daily_digest_due(config: dict, state: dict, now: datetime) -> bool:
    """在设定的本地时间窗口内，每个自然日最多返回一次 True。"""
    settings = config.get("daily_digest", {})
    if not settings.get("enabled"):
        return False
    local_time = now.astimezone(
        ZoneInfo(settings.get("timezone", CUSTOMER_TIMEZONE_NAME))
    )
    start_minute = int(settings.get("hour", 8)) * 60 + int(settings.get("minute", 0))
    window_minutes = max(1, int(settings.get("send_window_minutes", 60)))
    current_minute = local_time.hour * 60 + local_time.minute
    in_window = start_minute <= current_minute < start_minute + window_minutes
    return (
        in_window
        and state.get("last_daily_digest_date") != local_time.date().isoformat()
    )


def build_daily_digest(
    snapshots: list[dict], checked_at: datetime, candidates: Optional[list[dict]] = None,
    share_url: Optional[str] = None, image_url: Optional[str] = None,
) -> tuple[str, str]:
    local_time = checked_at.astimezone(ZoneInfo(CUSTOMER_TIMEZONE_NAME))
    title = f"{PRODUCT_NAME_ZH}早报｜{local_time.month}月{local_time.day}日"
    trigger_map = {
        candidate["snapshot"]["event_id"]: candidate["triggers"]
        for candidate in (candidates or [])
    }
    sections = [
        "# 今日事件概率",
        "> 每天北京时间08:00固定更新；其余时间仅在达到异常阈值时即时提醒。",
    ]
    if image_url:
        separator = "&" if "?" in image_url else "?"
        cache_key = local_time.strftime("%Y%m%d%H%M")
        sections.append(
            f"![{PRODUCT_NAME_ZH}每日快照]({image_url}{separator}v={cache_key})"
        )
    for snapshot in snapshots:
        triggers = trigger_map.get(snapshot["event_id"], [])
        alert_line = ""
        if triggers:
            alert_line = (
                "\n- ⚠️ 本次同时触发异常："
                + "；".join(trigger_text(trigger) for trigger in triggers)
            )
        if snapshot["mode"] == "distribution":
            outcome_lines = "\n".join(
                f"  - {row['label_zh']}：**{row['display_probability_pct']:.1f}%**"
                for row in snapshot["outcomes"]
            )
            probability_text = (
                f"- 当前最可能：**{snapshot['outcome_leader']['label_zh']} "
                f"{snapshot['outcome_leader']['display_probability_pct']:.1f}%**\n"
                f"- 五个互斥结果（合计100%）：\n{outcome_lines}"
            )
        else:
            probability_text = (
                f"- {snapshot['customer_question_zh']}："
                f"**{snapshot['probability_pct']:.1f}%**"
            )
        sections.append(
            f"## {snapshot['label_zh']}\n\n"
            f"{probability_text}{alert_line}\n"
            f"- 市场概率：[{snapshot['source_name']}]({snapshot['source_url']})\n"
            f"- 判定指标：[{snapshot['resolution_source_name']}]"
            f"({snapshot['resolution_source_url']}) · {snapshot['resolution_metric_zh']}"
        )
    if share_url:
        sections.append(f"[查看并分享公开快照]({share_url})")
    sections.append(
        f"数据时间：{customer_timestamp(isoformat_utc(checked_at))}\n\n"
        "---\n\n市场概率会变化，仅反映Polymarket参与者当时的预期；"
        "不是事实概率，也不构成交易建议。\n\n"
        "本产品为非官方观测工具，与Polymarket无隶属或合作关系。"
    )
    return title, "\n\n".join(sections)


def mark_alert_candidates_sent(candidates: list[dict]) -> None:
    for candidate in candidates:
        event_state = candidate["event_state"]
        snapshot = candidate["snapshot"]
        event_state["last_alert_at"] = snapshot["timestamp"]
        if snapshot["mode"] == "distribution":
            event_state["last_alert_probabilities"] = snapshot["probabilities"]
        else:
            event_state["last_alert_probability"] = snapshot["probability"]
        event_state.pop("pending_alert", None)


def share_summary_text(snapshots: list[dict], checked_at: datetime) -> str:
    lines = [f"{PRODUCT_NAME_ZH}｜{customer_timestamp(isoformat_utc(checked_at))}"]
    for snapshot in snapshots:
        if snapshot["mode"] == "distribution":
            outcomes = "；".join(
                f"{row['label_zh']} {row['display_probability_pct']:.1f}%"
                for row in snapshot["outcomes"]
            )
            lines.append(f"美联储9月：{outcomes}")
        else:
            lines.append(
                f"霍尔木兹：{snapshot['customer_question_zh']} "
                f"{snapshot['probability_pct']:.1f}%"
            )
    lines.extend([
        "概率来自Polymarket；最终结果按各事件的独立判定指标确认。",
        "仅反映市场当时预期，不构成交易建议。",
        "非Polymarket官方产品。",
    ])
    return "\n".join(lines)


def public_event_html(snapshot: dict) -> str:
    label = html.escape(snapshot["label_zh"])
    source_name = html.escape(snapshot["source_name"])
    source_url = html.escape(snapshot["source_url"], quote=True)
    resolution_name = html.escape(snapshot["resolution_source_name"])
    resolution_url = html.escape(snapshot["resolution_source_url"], quote=True)
    metric = html.escape(snapshot["resolution_metric_zh"])
    rule = html.escape(snapshot["resolution_rule_zh"])
    explainer = html.escape(snapshot.get("source_explainer_zh", ""))
    if snapshot["mode"] == "distribution":
        leader = snapshot["outcome_leader"]
        rows = "".join(
            "<li>"
            f"<span>{html.escape(row['label_zh'])}</span>"
            "<span class=\"radar-outcome-track\" aria-hidden=\"true\">"
            f"<span style=\"width:{row['display_probability_pct']:.1f}%\"></span></span>"
            f"<strong>{row['display_probability_pct']:.1f}%</strong>"
            "</li>"
            for row in snapshot["outcomes"]
        )
        headline = (
            "<p class=\"radar-question\">当前最可能</p>"
            f"<p class=\"radar-leader\">{html.escape(leader['label_zh'])}</p>"
            f"<p class=\"radar-number\">{leader['display_probability_pct']:.1f}%</p>"
            f"<ol class=\"radar-outcomes\" aria-label=\"五个互斥结果，合计100%\">{rows}</ol>"
        )
    else:
        headline = (
            f"<p class=\"radar-number\">{snapshot['probability_pct']:.1f}%</p>"
            f"<p class=\"radar-question\">{html.escape(snapshot['customer_question_zh'])}</p>"
        )
    return (
        "<article class=\"radar-event\">"
        f"<p class=\"radar-eyebrow\">{html.escape(snapshot['notification_title_zh'])}</p>"
        f"<h2>{label}</h2>{headline}"
        "<dl class=\"radar-method\">"
        f"<div><dt>市场概率</dt><dd><a href=\"{source_url}\" target=\"_blank\" "
        f"rel=\"noopener\">{source_name}</a></dd></div>"
        f"<div><dt>判定指标</dt><dd><a href=\"{resolution_url}\" target=\"_blank\" "
        f"rel=\"noopener\">{resolution_name}</a><br>{metric}</dd></div>"
        f"<div><dt>判定规则</dt><dd>{rule}</dd></div>"
        "</dl>"
        f"<p class=\"radar-explainer\">{explainer}</p>"
        "</article>"
    )


def public_styles(*, card: bool = False) -> str:
    canvas = (
        "body{margin:0;width:1080px;height:1350px;overflow:hidden;}"
        ".radar-shell{min-height:1350px;padding:58px 70px 42px;}"
        ".radar-card .radar-head{margin-bottom:20px;}"
        ".radar-card .radar-grid{grid-template-columns:1fr;gap:18px;}"
        ".radar-card .radar-event{padding:21px 28px 19px;}"
        ".radar-card .radar-event h2{font-size:25px;margin-bottom:11px;}"
        ".radar-card .radar-number{font-size:64px;}"
        ".radar-card .radar-question{margin-bottom:12px;}"
        ".radar-card .radar-outcomes{margin-top:11px;}"
        ".radar-card .radar-method{margin-top:13px;}"
        ".radar-card .radar-method>div{grid-template-columns:90px 1fr;padding:7px 0;}"
        ".radar-card .radar-method dt{font-size:12px;}"
        ".radar-card .radar-method dd,.radar-card .radar-explainer{font-size:13px;}"
        ".radar-card .radar-explainer{margin-top:11px;}"
        if card else
        ".event-radar-page-body .main-inner{max-width:1180px;width:calc(100% - 48px);}.radar-shell{padding:52px 0 78px;}"
    )
    return f"""
<style>
{canvas}
.radar-shell{{box-sizing:border-box;color:#1d1c18;font-family:"Lato","PingFang SC",sans-serif;}}
.radar-head{{align-items:end;border-bottom:1px solid #1d1c18;display:flex;gap:28px;justify-content:space-between;margin-bottom:28px;padding-bottom:18px;}}
.radar-kicker{{color:#2f55d4;font-size:12px;font-weight:900;letter-spacing:.15em;margin:0 0 9px;text-transform:uppercase;}}
.radar-head h1{{font-family:"Libre Caslon Display","Noto Serif SC",serif;font-size:clamp(37px,5vw,58px);font-weight:400;letter-spacing:-.035em;line-height:1;margin:0;}}
.radar-time{{color:#706a60;font-size:13px;margin:0;text-align:right;}}
.radar-grid{{display:grid;gap:22px;grid-template-columns:repeat(2,minmax(0,1fr));}}
.radar-event{{background:rgba(255,255,255,.28);border:1px solid #a8a092;border-top:3px solid #2f55d4;padding:26px 28px 24px;}}
.radar-eyebrow{{color:#2f55d4;font-size:11px;font-weight:900;letter-spacing:.12em;margin:0 0 8px;text-transform:uppercase;}}
.radar-event h2{{font-family:"Libre Caslon Text","Noto Serif SC",serif;font-size:23px;font-weight:600;line-height:1.3;margin:0 0 18px;}}
.radar-number{{font-family:"Libre Caslon Display",Georgia,serif;font-size:68px;letter-spacing:-.05em;line-height:.95;margin:0 0 8px;}}
.radar-leader{{font-family:"Noto Serif SC",serif;font-size:25px;font-weight:600;line-height:1.25;margin:0 0 4px;}}
.radar-question{{color:#45423b;font-family:"Noto Serif SC",serif;font-size:15px;line-height:1.6;margin:0 0 22px;}}
.radar-outcomes{{border-top:1px solid #c3bcaf;list-style:none;margin:18px 0 4px;padding:11px 0 0;}}
.radar-outcomes li{{align-items:center;display:grid;font-size:12px;gap:10px;grid-template-columns:minmax(130px,1.25fr) minmax(70px,.75fr) 45px;padding:5px 0;}}
.radar-outcomes strong{{font-variant-numeric:tabular-nums;text-align:right;}}
.radar-outcome-track{{background:#ded8cd;height:4px;overflow:hidden;}}
.radar-outcome-track span{{background:#2f55d4;display:block;height:100%;}}
.radar-method{{border-top:1px solid #a8a092;margin:22px 0 0;}}
.radar-method>div{{border-bottom:1px solid #d2cbbf;display:grid;gap:15px;grid-template-columns:72px 1fr;padding:10px 0;}}
.radar-method dt{{color:#706a60;font-size:11px;font-weight:900;letter-spacing:.06em;}}
.radar-method dd{{font-family:"Noto Serif SC",serif;font-size:12px;line-height:1.55;margin:0;}}
.radar-method a{{border-bottom:1px solid currentColor;color:#2f55d4;text-decoration:none;}}
.radar-explainer{{color:#45423b;font-family:"Noto Serif SC",serif;font-size:12px;line-height:1.65;margin:16px 0 0;}}
.radar-actions{{align-items:center;border-top:1px solid #1d1c18;display:flex;flex-wrap:wrap;gap:12px;margin-top:28px;padding-top:20px;}}
.radar-action{{background:#2f55d4;border:1px solid #2f55d4;color:#fff!important;cursor:pointer;font:800 12px "Lato",sans-serif;letter-spacing:.04em;padding:12px 17px;text-decoration:none;}}
.radar-action--secondary{{background:transparent;color:#2f55d4!important;}}
.radar-status{{color:#706a60;font-size:12px;}}
.radar-foot{{color:#706a60;font-family:"Noto Serif SC",serif;font-size:12px;line-height:1.65;margin:17px 0 0;}}
@media(max-width:760px){{.radar-head{{align-items:start;flex-direction:column;gap:12px;}}.radar-time{{text-align:left;}}.radar-grid{{grid-template-columns:1fr;}}.radar-event{{padding:23px 20px;}}.radar-number{{font-size:58px;}}.radar-outcomes li{{grid-template-columns:minmax(120px,1fr) 60px 42px;}}}}
</style>"""


def build_public_page(
    snapshots: list[dict], failures: list[dict], checked_at: datetime,
    public_url: str = "",
) -> str:
    timestamp = customer_timestamp(isoformat_utc(checked_at))
    cards = "".join(public_event_html(snapshot) for snapshot in snapshots)
    summary_text = share_summary_text(snapshots, checked_at)
    if public_url:
        summary_text += f"\n{public_url}"
    summary_json = json.dumps(summary_text, ensure_ascii=False)
    title_json = json.dumps(f"{PRODUCT_NAME_ZH}｜霍尔木兹与美联储", ensure_ascii=False)
    failure_note = ""
    if failures:
        failure_note = (
            f"<p class=\"radar-foot\">本次有{len(failures)}项数据未成功更新；"
            "分享前请先检查。</p>"
        )
    return f"""---
layout: page
title: Polymarket观测站
description: 非官方的Polymarket事件概率观测页，跟踪霍尔木兹海峡风险与美联储利率决策。
header: false
comments: false
toc: false
permalink: /event-radar/
---

<script>document.body.classList.add('event-radar-page-body');</script>
{public_styles()}
<main class="radar-shell" aria-labelledby="radar-title">
  <header class="radar-head">
    <div><p class="radar-kicker">Polymarket Observatory · Unofficial</p><h1 id="radar-title">Polymarket观测站</h1></div>
    <p class="radar-time">数据时间<br><strong>{html.escape(timestamp)}</strong></p>
  </header>
  <div class="radar-grid">{cards}</div>
{failure_note}
  <div class="radar-actions" aria-label="分享选项">
    <button class="radar-action" type="button" data-radar-share>分享页面</button>
    <button class="radar-action radar-action--secondary" type="button" data-radar-copy>复制分享摘要</button>
    <a class="radar-action radar-action--secondary" href="/images/event-radar-latest.png" download>下载朋友圈图片</a>
    <span class="radar-status" role="status" aria-live="polite"></span>
  </div>
  <p class="radar-foot">概率来自Polymarket公开市场价格。Polymarket提供市场预期，IMF PortWatch或FOMC会后声明负责确认最终事实。市场概率会变化，不是客观预测，也不构成交易建议。本产品为非官方观测工具，与Polymarket无隶属或合作关系。</p>
</main>
<script>
(() => {{
  const text = {summary_json};
  const title = {title_json};
  const status = document.querySelector('.radar-status');
  const setStatus = value => {{ if (status) status.textContent = value; }};
  const copy = async () => {{
    try {{ await navigator.clipboard.writeText(text); setStatus('摘要已复制'); }}
    catch (_) {{ setStatus('复制失败，请手动选择文字'); }}
  }};
  document.querySelector('[data-radar-copy]')?.addEventListener('click', copy);
  document.querySelector('[data-radar-share]')?.addEventListener('click', async () => {{
    if (navigator.share) {{
      try {{ await navigator.share({{ title, text, url: window.location.href }}); }} catch (_) {{}}
    }} else {{ await copy(); }}
  }});
}})();
</script>
"""


def build_share_card(snapshots: list[dict], checked_at: datetime) -> str:
    timestamp = customer_timestamp(isoformat_utc(checked_at))
    cards = "".join(public_event_html(snapshot) for snapshot in snapshots)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=1080">
<title>Polymarket观测站</title>{public_styles(card=True)}</head>
<body class="radar-card" style="background:#f5f1e8"><main class="radar-shell" aria-labelledby="radar-card-title">
  <header class="radar-head">
    <div><p class="radar-kicker">Polymarket Observatory · Unofficial</p><h1 id="radar-card-title">Polymarket观测站</h1></div>
    <p class="radar-time">数据时间<br><strong>{html.escape(timestamp)}</strong></p>
  </header>
  <div class="radar-grid">{cards}</div>
  <p class="radar-foot">数据来自Polymarket公开市场价格；最终结果按IMF PortWatch或FOMC会后声明确认。市场概率会变化，不是客观预测，也不构成交易建议。非Polymarket官方产品。</p>
</main></body></html>
"""


def build_report(snapshots: list[dict], failures: list[dict], checked_at: datetime) -> str:
    lines = [
        f"# {PRODUCT_NAME_ZH}",
        "",
        f"最后检查：{checked_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "> 数据来自Polymarket公开市场价格，反映市场预期，不是客观预测，也不构成交易建议。",
        "",
        "| 事件 | 当前市场隐含概率 | 价差 | 流动性 | 24h成交 | 状态 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for snapshot in snapshots:
        status = "正常" if snapshot["quality_ok"] else "；".join(snapshot["quality_reasons"])
        probability_text = (
            distribution_text(snapshot, " / ")
            if snapshot["mode"] == "distribution"
            else f"{snapshot['probability_pct']:.1f}%"
        )
        lines.append(
            f"| [{snapshot['label_zh']}]({snapshot['source_url']}) | "
            f"{probability_text} | {snapshot['spread_pp']:.2f}pp | "
            f"{format_money(snapshot['liquidity_usd'])} | "
            f"{format_money(snapshot['volume_24h_usd'])} | {status} |"
        )
    for snapshot in snapshots:
        lines.extend([
            "",
            f"## {snapshot['label_zh']}",
            "",
            snapshot["resolution_summary_zh"],
            "",
        ])
        if snapshot["mode"] == "distribution":
            lines.extend([
                "| 五个互斥结果 | 归一化概率 | 原始中点 |",
                "|---|---:|---:|",
            ])
            lines.extend(
                f"| {row['label_zh']} | {row['display_probability_pct']:.1f}% | "
                f"{row['raw_probability'] * 100:.2f}% |"
                for row in snapshot["outcomes"]
            )
            lines.extend([
                "",
                f"五个互斥结果归一化前合计：{snapshot['normalization_total'] * 100:.2f}%。",
                "归一化并显示后的五项合计为100%。",
                "",
            ])
        lines.extend([
            f"- 数据来源：[{snapshot['source_name']}]({snapshot['source_url']})",
            f"- [最终判定依据]({snapshot['resolution_source_url']})",
        ])
    if failures:
        lines.extend(["", "## 抓取失败", ""])
        lines.extend(f"- {row['event_id']}：{row['error']}" for row in failures)
    return "\n".join(lines) + "\n"


def append_history(payload: dict, now: datetime) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{now.strftime('%Y-%m')}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="监控两个精选事件的市场隐含概率。")
    parser.add_argument("--dry-run", action="store_true", help="只读抓取和预览，不通知、不写状态")
    parser.add_argument(
        "--export-share", action="store_true",
        help="刷新公开快照与朋友圈卡片，不通知、不改监控状态",
    )
    parser.add_argument(
        "--daily-now", action="store_true",
        help="立即发送一次完整晨报，用于人工验收",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="事件配置文件")
    return parser.parse_args(argv)


def run(argv: list[str], *, now: Optional[datetime] = None) -> int:
    args = parse_args(argv)
    current_time = now or utc_now()
    config = load_json(args.config, {})
    if config.get("schema_version") != 2 or not config.get("events"):
        print(f"配置无效：{args.config}", file=sys.stderr)
        return 2

    state = load_json(STATE_PATH, {"version": STATE_VERSION, "events": {}})
    if state.get("version") != STATE_VERSION:
        state = {"version": STATE_VERSION, "events": {}}
    state.setdefault("events", {})
    snapshots = []
    failures = []
    candidates = []

    for event in config["events"]:
        try:
            markets = {
                str(component["market_id"]): fetch_market(str(component["market_id"]))
                for component in event_components(event)
            }
            snapshot = build_snapshot(event, markets, current_time)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError,
                subprocess.SubprocessError) as exc:
            failures.append({"event_id": event["id"], "error": str(exc)[:500]})
            continue

        snapshots.append(snapshot)
        event_state = state["events"].setdefault(event["id"], {})
        triggers, samples = detect_triggers(event, event_state, snapshot)
        event_state["samples"] = samples
        event_state["latest"] = compact_sample(snapshot)

        pending = event_state.get("pending_alert")
        if triggers and cooldown_allows(event, event_state, snapshot, triggers):
            pending = {
                "created_at": snapshot["timestamp"],
                "triggers": triggers,
                "snapshot": snapshot,
            }
            if not args.dry_run:
                event_state["pending_alert"] = pending
        if pending:
            candidates.append({
                "event": event,
                "snapshot": pending["snapshot"],
                "triggers": pending["triggers"],
                "samples": samples,
                "event_state": event_state,
            })

        current_text = (
            distribution_text(snapshot, " | ")
            if snapshot["mode"] == "distribution"
            else f"{snapshot['probability_pct']:.1f}%"
        )
        print(
            f"[OK] {snapshot['label_zh']}：{current_text} "
            f"(spread {snapshot['spread_pp']:.2f}pp, liquidity "
            f"{format_money(snapshot['liquidity_usd'])})"
        )

    public_share_url = (
        str(config.get("public_share_url", "")).strip()
        if config.get("public_share_enabled") else ""
    )
    public_image_url = (
        str(config.get("public_image_url", "")).strip()
        if config.get("public_share_enabled") else ""
    )
    if args.export_share:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUBLIC_REPORT_PATH.write_text(
            build_public_page(
                snapshots, failures, current_time,
                str(config.get("public_share_url", "")).strip()
                or "",
            ),
            encoding="utf-8",
        )
        SHARE_CARD_PATH.write_text(
            build_share_card(snapshots, current_time), encoding="utf-8"
        )
        print(f"[SHARE] 公开快照：{PUBLIC_REPORT_PATH}")
        print(f"[SHARE] 朋友圈卡片：{SHARE_CARD_PATH}")
        return 1 if failures else 0

    notification_result = {"ok": True, "reason": "no alerts", "kind": "none"}
    digest_due = args.daily_now or daily_digest_due(config, state, current_time)
    digest_ready = (
        digest_due
        and not failures
        and len(snapshots) == len(config["events"])
    )
    if digest_ready:
        title, body = build_daily_digest(
            snapshots, current_time, candidates, public_share_url or None,
            public_image_url or None,
        )
        notification_result = notify.push(title, body, dry_run=args.dry_run)
        notification_result["kind"] = "daily_digest"
        if notification_result.get("ok") and not args.dry_run:
            digest_timezone = config.get("daily_digest", {}).get(
                "timezone", CUSTOMER_TIMEZONE_NAME
            )
            state["last_daily_digest_date"] = current_time.astimezone(
                ZoneInfo(digest_timezone)
            ).date().isoformat()
            state["last_daily_digest_at"] = isoformat_utc(current_time)
            mark_alert_candidates_sent(candidates)
    elif candidates:
        title, body = build_notification(candidates, public_share_url or None)
        notification_result = notify.push(title, body, dry_run=args.dry_run)
        notification_result["kind"] = "anomaly_alert"
        if notification_result.get("ok") and not args.dry_run:
            mark_alert_candidates_sent(candidates)

    audit = {
        "checked_at": isoformat_utc(current_time),
        "snapshots": snapshots,
        "failures": failures,
        "notification": notification_result,
    }
    if args.dry_run:
        print("[DRY-RUN] 不写状态、不写历史、不发送通知。")
    else:
        write_json_atomic(STATE_PATH, state)
        append_history(audit, current_time)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            build_report(snapshots, failures, current_time), encoding="utf-8"
        )
        PUBLIC_REPORT_PATH.write_text(
            build_public_page(
                snapshots, failures, current_time,
                str(config.get("public_share_url", "")).strip()
                or "",
            ),
            encoding="utf-8",
        )
        SHARE_CARD_PATH.write_text(
            build_share_card(snapshots, current_time), encoding="utf-8"
        )

    for failure in failures:
        print(f"[FAIL] {failure['event_id']}：{failure['error']}", file=sys.stderr)
    if not notification_result.get("ok"):
        print(f"[FAIL] 通知：{notification_result.get('reason')}", file=sys.stderr)
    return 1 if failures or not notification_result.get("ok") else 0


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
