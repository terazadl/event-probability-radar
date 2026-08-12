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
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import notify


SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
CONFIG_PATH = SYSTEM_DIR / "Config" / "event_watchlist.json"
DATA_DIR = SYSTEM_DIR / "Data" / "events"
STATE_PATH = DATA_DIR / "state.json"
LOCK_PATH = DATA_DIR / "radar.lock"
HISTORY_DIR = DATA_DIR / "history"
REPORT_PATH = SYSTEM_DIR / "Reports" / "事件概率雷达.md"

GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/{market_id}"
CURL_TIMEOUT_SECONDS = 30
STATE_VERSION = 1


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


def build_snapshot(event: dict, markets: dict[str, dict], now: datetime) -> dict:
    components = [
        component_quote(component, markets[str(component["market_id"])])
        for component in event["components"]
    ]
    probability = min(1.0, max(0.0, sum(row["probability"] for row in components)))
    spread = sum(row["spread"] for row in components)
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

    rules_text = "\n".join(row["description"] for row in components)
    rules_hash = hashlib.sha256(rules_text.encode("utf-8")).hexdigest()[:16]
    return {
        "event_id": event["id"],
        "label_zh": event["label_zh"],
        "timestamp": isoformat_utc(now),
        "probability": probability,
        "probability_pct": probability * 100,
        "spread_pp": spread * 100,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume_24h,
        "open": is_open,
        "quality_ok": not quality_reasons,
        "quality_reasons": quality_reasons,
        "rules_hash": rules_hash,
        "components": components,
        "source_url": event["source_url"],
        "resolution_source_url": event["resolution_source_url"],
        "resolution_summary_zh": event["resolution_summary_zh"],
    }


def compact_sample(snapshot: dict) -> dict:
    return {
        "timestamp": snapshot["timestamp"],
        "probability": snapshot["probability"],
        "quality_ok": snapshot["quality_ok"],
        "open": snapshot["open"],
        "rules_hash": snapshot["rules_hash"],
    }


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
    samples: list[dict], *, hours: int, threshold_pp: float, confirmations: int
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
        deltas.append((sample["probability"] - reference["probability"]) * 100)
    if all(delta >= threshold_pp for delta in deltas):
        return {"kind": f"change_{hours}h_up", "delta_pp": deltas[-1]}
    if all(delta <= -threshold_pp for delta in deltas):
        return {"kind": f"change_{hours}h_down", "delta_pp": deltas[-1]}
    return None


def confirmed_threshold_crossings(
    samples: list[dict], thresholds_pct: list[float], confirmations: int
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
        if before["probability"] < boundary and all(row["probability"] >= boundary for row in recent):
            triggers.append({"kind": "threshold_up", "threshold_pct": float(threshold)})
        elif before["probability"] > boundary and all(row["probability"] <= boundary for row in recent):
            triggers.append({"kind": "threshold_down", "threshold_pct": float(threshold)})
    return triggers


def current_delta(samples: list[dict], hours: int) -> Optional[float]:
    if not samples:
        return None
    current = samples[-1]
    reference = reference_sample(samples, parse_timestamp(current["timestamp"]), hours)
    if reference is None:
        return None
    return (current["probability"] - reference["probability"]) * 100


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
        for hours, key in ((1, "change_1h_pp"), (24, "change_24h_pp")):
            change = confirmed_change(
                samples,
                hours=hours,
                threshold_pp=float(settings[key]),
                confirmations=confirmations,
            )
            if change:
                triggers.append(change)
        triggers.extend(
            confirmed_threshold_crossings(
                samples,
                settings["thresholds_pct"],
                confirmations,
            )
        )

    if previous.get("rules_hash") != snapshot["rules_hash"]:
        triggers.append({"kind": "rules_changed"})
    if previous.get("open") and not snapshot["open"]:
        triggers.append({"kind": "market_closed"})
    return triggers, samples


def trigger_text(trigger: dict) -> str:
    kind = trigger["kind"]
    if kind.startswith("change_"):
        window = "1小时" if "1h" in kind else "24小时"
        return f"{window}变化 {trigger['delta_pp']:+.1f} 个百分点"
    if kind == "threshold_up":
        return f"向上突破 {trigger['threshold_pct']:.0f}%"
    if kind == "threshold_down":
        return f"向下跌破 {trigger['threshold_pct']:.0f}%"
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


def build_notification(candidates: list[dict]) -> tuple[str, str]:
    first = candidates[0]["snapshot"]["label_zh"]
    title = f"📡 事件概率雷达：{first[:25]}"
    if len(candidates) > 1:
        title += f" 等 {len(candidates)} 项"
    sections = []
    for candidate in candidates:
        snapshot = candidate["snapshot"]
        samples = candidate["samples"]
        delta_1h = current_delta(samples, 1)
        delta_24h = current_delta(samples, 24)
        changes = []
        if delta_1h is not None:
            changes.append(f"1小时 {delta_1h:+.1f}pp")
        if delta_24h is not None:
            changes.append(f"24小时 {delta_24h:+.1f}pp")
        change_text = "；".join(changes) if changes else "历史尚不足以计算窗口变化"
        reasons = "；".join(trigger_text(row) for row in candidate["triggers"])
        sections.append(
            f"## {snapshot['label_zh']}\n\n"
            f"- 当前市场隐含概率：**{snapshot['probability_pct']:.1f}%**\n"
            f"- 窗口变化：{change_text}\n"
            f"- 触发原因：{reasons}\n"
            f"- 市场质量：价差 {snapshot['spread_pp']:.2f}pp；"
            f"流动性 {format_money(snapshot['liquidity_usd'])}；"
            f"24小时成交 {format_money(snapshot['volume_24h_usd'])}\n"
            f"- 裁决口径：{snapshot['resolution_summary_zh']}\n"
            f"- [原始市场]({snapshot['source_url']}) · "
            f"[裁决来源]({snapshot['resolution_source_url']})"
        )
    sections.append(
        "---\n\n这是公开市场价格形成的隐含概率，不是客观预测，也不构成交易建议。"
    )
    return title, "\n\n".join(sections)


def build_report(snapshots: list[dict], failures: list[dict], checked_at: datetime) -> str:
    lines = [
        "# 事件概率雷达",
        "",
        f"最后检查：{checked_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "> 这是公开市场价格形成的隐含概率，不是客观预测，也不构成交易建议。",
        "",
        "| 事件 | 当前概率 | 价差 | 流动性 | 24h成交 | 状态 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for snapshot in snapshots:
        status = "正常" if snapshot["quality_ok"] else "；".join(snapshot["quality_reasons"])
        lines.append(
            f"| [{snapshot['label_zh']}]({snapshot['source_url']}) | "
            f"{snapshot['probability_pct']:.1f}% | {snapshot['spread_pp']:.2f}pp | "
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
            f"- [原始市场]({snapshot['source_url']})",
            f"- [裁决来源]({snapshot['resolution_source_url']})",
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
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="事件配置文件")
    return parser.parse_args(argv)


def run(argv: list[str], *, now: Optional[datetime] = None) -> int:
    args = parse_args(argv)
    current_time = now or utc_now()
    config = load_json(args.config, {})
    if config.get("schema_version") != 1 or not config.get("events"):
        print(f"配置无效：{args.config}", file=sys.stderr)
        return 2

    state = load_json(STATE_PATH, {"version": STATE_VERSION, "events": {}})
    state.setdefault("events", {})
    snapshots = []
    failures = []
    candidates = []

    for event in config["events"]:
        try:
            markets = {
                str(component["market_id"]): fetch_market(str(component["market_id"]))
                for component in event["components"]
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

        print(
            f"[OK] {snapshot['label_zh']}：{snapshot['probability_pct']:.1f}% "
            f"(spread {snapshot['spread_pp']:.2f}pp, liquidity "
            f"{format_money(snapshot['liquidity_usd'])})"
        )

    notification_result = {"ok": True, "reason": "no alerts"}
    if candidates:
        title, body = build_notification(candidates)
        notification_result = notify.push(title, body, dry_run=args.dry_run)
        if notification_result.get("ok") and not args.dry_run:
            for candidate in candidates:
                event_state = candidate["event_state"]
                event_state["last_alert_at"] = candidate["snapshot"]["timestamp"]
                event_state["last_alert_probability"] = candidate["snapshot"]["probability"]
                event_state.pop("pending_alert", None)

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
            print("事件雷达已有实例在运行，本次跳过。")
            return 0
        return run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
