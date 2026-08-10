#!/usr/bin/env python3
"""每日编排：跑数据脚本 → 汇总告警 → 推送微信 → 写运行日志

这一层存在的理由：单个脚本只管抓自己的数据，谁都不知道"今天整体跑没跑成"。
运行日志（Logs/runs/）是这套系统唯一的存活证明，也是以后写复盘的原始素材。

用法：
    .venv/bin/python3 Scripts/run_daily.py               # 正常跑（当天跑过就跳过）
    .venv/bin/python3 Scripts/run_daily.py --force       # 忽略「当天已跑」的守卫
    .venv/bin/python3 Scripts/run_daily.py --dry-run     # 不真的推送，也不提交
    .venv/bin/python3 Scripts/run_daily.py --heartbeat   # 无告警也推一条「我还活着」
    .venv/bin/python3 Scripts/run_daily.py --no-commit   # 跑完不自动 git commit
    .venv/bin/python3 Scripts/run_daily.py --push        # 提交后顺便 push
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import notify

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = SCRIPT_DIR.parent
LOG_DIR = SYSTEM_DIR / "Logs" / "runs"
RUN_STATE = SYSTEM_DIR / "Data" / "run_state.json"

JOBS = [
    ("FRED 利率与通胀", SCRIPT_DIR / "fetch_fred_snapshot.py"),
    ("资金流", SCRIPT_DIR / "fetch_flows_snapshot.py"),
]

JOB_TIMEOUT = 300  # 秒。数据源偶尔会卡住，卡住比失败更难发现


# ---------------------------------------------------------------- 命令行

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取每日投研快照、汇总告警、写运行日志并按需通知。"
    )
    parser.add_argument("--force", action="store_true", help="忽略当天成功守卫，强制运行")
    parser.add_argument("--dry-run", action="store_true", help="不发送通知、不提交，也不更新运行状态")
    parser.add_argument("--heartbeat", action="store_true", help="无告警时也发送成功心跳")
    parser.add_argument("--no-commit", action="store_true", help="不自动创建 git 提交")
    parser.add_argument("--push", action="store_true", help="自动提交后推送当前 git 分支")
    return parser.parse_args(argv)


# ---------------------------------------------------------------- 当天守卫

def ran_today() -> bool:
    try:
        state = json.loads(RUN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return state.get("last_success_date") == date.today().isoformat()


def mark_run(ok: bool) -> None:
    RUN_STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(RUN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    if ok:
        state["last_success_date"] = date.today().isoformat()
    RUN_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 执行

def run_job(name: str, path: Path) -> dict:
    started = time.time()
    if not path.exists():
        return {"name": name, "ok": False, "seconds": 0.0, "error": f"脚本不存在：{path}", "stdout": ""}
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT,
            cwd=str(SYSTEM_DIR),
        )
    except subprocess.TimeoutExpired:
        return {
            "name": name, "ok": False, "seconds": round(time.time() - started, 1),
            "error": f"超时（>{JOB_TIMEOUT}s）", "stdout": "",
        }
    elapsed = round(time.time() - started, 1)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {
            "name": name, "ok": False, "seconds": elapsed,
            "error": tail[-1][:300] if tail else f"退出码 {proc.returncode}",
            "stdout": proc.stdout.strip(),
        }
    return {"name": name, "ok": True, "seconds": elapsed, "error": "", "stdout": proc.stdout.strip()}


def collect_alerts() -> list[str]:
    """从各脚本落盘的 alerts.json 里收集当天告警。"""
    today = date.today().isoformat()
    alerts: list[str] = []
    for alerts_path in sorted(SYSTEM_DIR.glob("Data/*/alerts.json")):
        try:
            payload = json.loads(alerts_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("date") != today:
            continue  # 陈旧文件不算数，否则会天天重推同一条
        alerts.extend(payload.get("alerts", []))
    return alerts


def build_notification(
    alerts: list[str], failures: list[dict], *, all_failed: bool
) -> Optional[tuple[str, str]]:
    """Build one notification so alerts never hide collection failures."""
    if not alerts and not failures:
        return None

    if alerts and failures:
        title = f"⚠️ 投研系统：{len(alerts)} 条告警，{len(failures)} 项任务失败"
    elif failures:
        title = "❌ 投研系统抓取失败" if all_failed else "⚠️ 投研系统部分失败"
    else:
        title = f"⚠️ 投研系统：{alerts[0][:30]}"
        if len(alerts) > 1:
            title += f" 等 {len(alerts)} 条"

    sections = []
    if alerts:
        sections.append("## 市场告警\n\n" + "\n".join(f"- {alert}" for alert in alerts))
    if failures:
        sections.append(
            "## 抓取失败\n\n"
            + "\n".join(f"- **{result['name']}**：{result['error']}" for result in failures)
        )
    sections.append(f"---\n\n{date.today().isoformat()} · 由投研系统自动触发")
    return title, "\n\n".join(sections)


# ---------------------------------------------------------------- 日志

def write_log(results: list[dict], alerts: list[str], push_result: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().strftime('%Y-%m')}.md"
    if not log_path.exists():
        header = [
            f"# 运行日志 {date.today().strftime('%Y-%m')}",
            "",
            "由 `run_daily.py` 自动追加。这个文件是系统「确实在跑」的证据，别手动改。",
            "",
            "| 时间 | 任务 | 结果 | 耗时 | 告警 | 备注 |",
            "|---|---|---|---:|---|---|",
            "",
        ]
        log_path.write_text("\n".join(header), encoding="utf-8")

    now = datetime.now().strftime("%m-%d %H:%M")
    rows = []
    for r in results:
        status = "✅" if r["ok"] else "❌"
        note = r["error"][:80] if r["error"] else ""
        rows.append(f"| {now} | {r['name']} | {status} | {r['seconds']}s | | {note} |")
    failure_count = sum(1 for result in results if not result["ok"])
    if alerts or failure_count:
        summary_parts = []
        if alerts:
            summary_parts.append(f"{len(alerts)} 条告警")
        if failure_count:
            summary_parts.append(f"{failure_count} 项失败")
        detail = "；".join(a[:40] for a in alerts)
        rows.append(
            f"| {now} | 通知 | {'✅' if push_result.get('ok') else '❌'} | | "
            f"{'，'.join(summary_parts)} | {detail} |"
        )

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")
    return log_path


# ---------------------------------------------------------------- 自动提交

# 只提交明确列出的机器生成文件。不能用整个 Data/Reports 目录，否则同目录里的
# 手写笔记仍可能被定时任务暂存。
AUTO_COMMIT_PATHS = [
    "Data/fred/latest.json",
    "Data/flows/latest.json",
    "Data/flows/alerts.json",
]


def auto_commit_paths() -> list[str]:
    today = date.today().isoformat()
    month = date.today().strftime("%Y-%m")
    candidates = [
        *AUTO_COMMIT_PATHS,
        f"Reports/FRED 利率与通胀快照 {today}.md",
        f"Reports/资金流快照 {today}.md",
        f"Logs/runs/{month}.md",
    ]
    return [path for path in candidates if (SYSTEM_DIR / path).exists()]


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(SYSTEM_DIR), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def git_commit(results: list[dict], alerts: list[str], *, push: bool = False) -> dict:
    """把当天的快照提交进版本历史。任何失败都只报告，不影响主流程。"""
    if not (SYSTEM_DIR / ".git").exists():
        return {"ok": False, "reason": "不是 git 仓库，跳过"}

    try:
        existing = auto_commit_paths()
        if not existing:
            return {"ok": True, "reason": "没有可提交的路径"}

        add = _git("add", "--", *existing)
        if add.returncode != 0:
            return {"ok": False, "reason": f"git add 失败: {add.stderr.strip()[:200]}"}

        # 暂存区没东西就别造空提交，否则历史会被噪音淹掉
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return {"ok": True, "reason": "无变化，未提交"}

        ok_count = sum(1 for r in results if r["ok"])
        summary = f"auto: {date.today().isoformat()} 快照（{ok_count}/{len(results)} 成功"
        summary += f"，{len(alerts)} 条告警）" if alerts else "，无告警）"
        body = "\n".join(f"- {a}" for a in alerts)
        args = ["commit", "-m", summary] + (["-m", body] if body else [])

        commit = _git(*args)
        if commit.returncode != 0:
            return {"ok": False, "reason": f"git commit 失败: {commit.stderr.strip()[:200]}"}

        result = {"ok": True, "reason": summary}

        if push:
            pushed = _git("push", timeout=120)
            if pushed.returncode != 0:
                result["reason"] += f"；但 push 失败: {pushed.stderr.strip()[:200]}"
                result["push_ok"] = False
            else:
                result["push_ok"] = True
        return result

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"ok": False, "reason": f"git 异常: {exc}"}


# ---------------------------------------------------------------- 主流程

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dry_run = args.dry_run
    force = args.force
    heartbeat = args.heartbeat
    do_commit = not args.no_commit and not dry_run
    do_push = args.push

    if ran_today() and not force:
        print("今天已经成功跑过了，跳过。用 --force 强制重跑。")
        return 0

    results = [run_job(name, path) for name, path in JOBS]
    for r in results:
        flag = "OK " if r["ok"] else "FAIL"
        print(f"[{flag}] {r['name']} ({r['seconds']}s) {r['error']}")
        if r["stdout"]:
            print(f"       {r['stdout'].splitlines()[-1]}")

    alerts = collect_alerts()
    failures = [r for r in results if not r["ok"]]
    all_failed = len(failures) == len(results)

    push_result = {"ok": True, "reason": "nothing to push"}

    notification = build_notification(alerts, failures, all_failed=all_failed)
    if notification:
        title, desp = notification
        push_result = notify.push(title, desp, dry_run=dry_run)
    elif heartbeat:
        push_result = notify.push(
            "✅ 投研系统正常",
            f"{date.today().isoformat()} 全部数据源抓取成功，没有触发任何告警规则。",
            dry_run=dry_run,
            dedupe=False,
        )

    log_path = write_log(results, alerts, push_result)
    print(f"日志：{log_path}")
    if not push_result.get("ok"):
        print(f"推送未成功：{push_result.get('reason')}", file=sys.stderr)

    if do_commit:
        commit_result = git_commit(results, alerts, push=do_push)
        print(f"git：{commit_result['reason']}")

    if not dry_run:
        mark_run(ok=not failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
