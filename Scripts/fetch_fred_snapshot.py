#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "00 投研系统" / "Data" / "fred"
REPORT_DIR = ROOT / "00 投研系统" / "Reports"


@dataclass(frozen=True)
class SeriesConfig:
    series_id: str
    name: str
    category: str
    unit: str
    why: str


SERIES = [
    SeriesConfig("DGS2", "美国2年期国债收益率", "利率", "percent", "反映短端利率和 Fed 路径"),
    SeriesConfig("DGS10", "美国10年期国债收益率", "利率", "percent", "影响全球资产估值、美元和风险偏好"),
    SeriesConfig("DFII10", "美国10年TIPS实际利率", "利率", "percent", "影响黄金、比特币和高估值科技股"),
    SeriesConfig("T10YIE", "10年通胀预期", "通胀", "percent", "帮助拆分名义利率来自实际利率还是通胀预期"),
    SeriesConfig("BAMLH0A0HYM2", "美国高收益债信用利差", "信用", "percent", "观察风险偏好和信用压力"),
    SeriesConfig("WALCL", "美联储总资产", "流动性", "millions_usd", "观察 Fed 资产负债表方向"),
]


def fetch_csv(series_id: str) -> str:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    result = subprocess.run(
        ["curl", "-L", "--ipv4", "-fsSL", "--max-time", "20", url],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def parse_observations(raw_csv: str) -> list[tuple[date, float]]:
    reader = csv.DictReader(raw_csv.splitlines())
    observations: list[tuple[date, float]] = []
    fieldnames = reader.fieldnames or []
    if len(fieldnames) < 2:
        return observations
    date_key = fieldnames[0]
    value_key = fieldnames[1]

    for row in reader:
        raw_value = row.get(value_key, "").strip()
        if raw_value in {"", "."}:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        observations.append((datetime.strptime(row[date_key], "%Y-%m-%d").date(), value))
    return observations


def value_on_or_before(observations: list[tuple[date, float]], target: date) -> tuple[date, float] | None:
    dates = [item[0] for item in observations]
    idx = bisect_right(dates, target) - 1
    if idx < 0:
        return None
    return observations[idx]


def fmt_value(value: float, unit: str) -> str:
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "millions_usd":
        return f"${value / 1_000_000:.2f}T"
    if unit == "billions_usd":
        return f"${value:.0f}B"
    return f"{value:.2f}"


def fmt_change(current: float, previous: float | None, unit: str) -> str:
    if previous is None or math.isnan(previous):
        return "n/a"
    change = current - previous
    if unit == "percent":
        return f"{change * 100:+.0f} bp"
    if unit == "millions_usd":
        return f"${change / 1_000_000:+.2f}T"
    if unit == "billions_usd":
        return f"${change:+.0f}B"
    return f"{change:+.2f}"


def quick_read(rows: list[dict]) -> list[str]:
    by_id = {row["series_id"]: row for row in rows}
    notes: list[str] = []

    dgs10 = by_id.get("DGS10")
    dfii10 = by_id.get("DFII10")
    t10yie = by_id.get("T10YIE")
    hy = by_id.get("BAMLH0A0HYM2")

    if dgs10 and dfii10 and t10yie:
        real_chg = dfii10["change_30d_raw"]
        inf_chg = t10yie["change_30d_raw"]
        if real_chg is not None and inf_chg is not None:
            if abs(real_chg) > abs(inf_chg):
                notes.append("过去约一个月，美债名义利率变化更偏实际利率驱动，通常对黄金、比特币和高估值科技股更不友好。")
            elif abs(inf_chg) > abs(real_chg):
                notes.append("过去约一个月，美债名义利率变化更偏通胀预期驱动，市场更像在交易通胀或名义增长。")

    if hy:
        hy_value = hy["latest_value"]
        if hy_value >= 5.0:
            notes.append("高收益债利差处在偏高区域，信用市场已经出现较明显压力。")
        elif hy_value <= 3.5:
            notes.append("高收益债利差仍然不高，信用市场暂时没有显示系统性压力。")

    if not notes:
        notes.append("本次快照没有给出强信号，先把它作为基准数据留存。")
    return notes


def build_report(rows: list[dict]) -> str:
    today = date.today().isoformat()
    success_rows = [row for row in rows if not row.get("error")]
    missing = [row for row in rows if row.get("error")]
    lines = [
        f"# FRED 利率与通胀快照 - {today}",
        "",
        f"日期：{today}",
        "标签：#FRED #利率 #通胀 #美元流动性 #投研系统",
        "",
        "## 一句话",
        "",
        "这是一份自动生成的 FRED 快照，用来观察美债利率、实际利率、通胀预期、信用利差和美元流动性。",
        "",
        "## 快速解读",
        "",
    ]
    for note in quick_read(success_rows):
        lines.append(f"- {note}")

    lines.extend(
        [
            "",
            "## 指标表",
            "",
            "| 类别 | 指标 | 最新日期 | 最新值 | 7日变化 | 30日变化 | 为什么看 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in success_rows:
        lines.append(
            "| {category} | {name} | {latest_date} | {latest} | {chg_7d} | {chg_30d} | {why} |".format(
                category=row["category"],
                name=row["name"],
                latest_date=row["latest_date"],
                latest=row["latest"],
                chg_7d=row["change_7d"],
                chg_30d=row["change_30d"],
                why=row["why"],
            )
        )

    if missing:
        lines.extend(["", "## 下载失败", ""])
        for row in missing:
            lines.append(f"- {row['series_id']}：{row['error']}")

    lines.extend(
        [
            "",
            "## 资产含义",
            "",
            "- 实际利率上行：通常压制黄金、比特币和高估值科技股。",
            "- 通胀预期上行：更像通胀交易，可能支撑黄金、商品和能源。",
            "- 信用利差扩大：说明市场风险偏好下降，权益和高收益债压力上升。",
            "- Fed 总资产和逆回购变化：用于观察美元流动性环境，但不能单独作为交易信号。",
            "",
            "## 下一步",
            "",
            "- 如果某个指标连续两周朝同一方向移动，把它写入 [[观点与假设库]]。",
            "- 如果利率和风险资产反应不一致，写一张 [[Templates/观点卡片模板]]。",
            "- 如果快照改变了资产判断，更新 [[资产观察清单]]。",
            "",
            "## 数据来源",
            "",
            "- FRED CSV: https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES_ID",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for config in SERIES:
        try:
            raw_csv = fetch_csv(config.series_id)
            (DATA_DIR / f"{config.series_id}.csv").write_text(raw_csv, encoding="utf-8")
            observations = parse_observations(raw_csv)
        except Exception as exc:
            rows.append(
                {
                    "series_id": config.series_id,
                    "name": config.name,
                    "category": config.category,
                    "unit": config.unit,
                    "why": config.why,
                    "error": str(exc),
                }
            )
            continue
        if not observations:
            rows.append(
                {
                    "series_id": config.series_id,
                    "name": config.name,
                    "category": config.category,
                    "unit": config.unit,
                    "why": config.why,
                    "error": "No observations parsed",
                }
            )
            continue

        latest_date, latest_value = observations[-1]
        week_ago = value_on_or_before(observations, latest_date - timedelta(days=7))
        month_ago = value_on_or_before(observations, latest_date - timedelta(days=30))
        week_value = week_ago[1] if week_ago else None
        month_value = month_ago[1] if month_ago else None

        rows.append(
            {
                "series_id": config.series_id,
                "name": config.name,
                "category": config.category,
                "unit": config.unit,
                "why": config.why,
                "latest_date": latest_date.isoformat(),
                "latest_value": latest_value,
                "latest": fmt_value(latest_value, config.unit),
                "change_7d_raw": None if week_value is None else latest_value - week_value,
                "change_30d_raw": None if month_value is None else latest_value - month_value,
                "change_7d": fmt_change(latest_value, week_value, config.unit),
                "change_30d": fmt_change(latest_value, month_value, config.unit),
                "error": "",
            }
        )

    today = date.today().isoformat()
    (DATA_DIR / "latest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if not any(not row.get("error") for row in rows):
        error_path = DATA_DIR / f"errors-{today}.json"
        error_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(f"All downloads failed. Error details written to {error_path}")
    report_path = REPORT_DIR / f"FRED 利率与通胀快照 {today}.md"
    report_path.write_text(build_report(rows), encoding="utf-8")
    print(report_path)

    failed_rows = [row for row in rows if row.get("error")]
    if failed_rows:
        failed_ids = ", ".join(row["series_id"] for row in failed_rows)
        print(
            f"部分 FRED 数据源失败（{len(failed_rows)}/{len(rows)}）：{failed_ids}",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
