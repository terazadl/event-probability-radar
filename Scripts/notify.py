#!/usr/bin/env python3
"""Server酱推送模块

设计原则：
1. 只有值得打断你的事才推送。噪音一多，你就会开始忽略它，这个系统就死了。
2. 同一条提醒当天只推一次（去重），避免 launchd 重复触发时刷屏。
3. 预期的配置、网络和响应错误要降级为结构化结果——数据抓取比通知重要。

SendKey 配置（二选一，env 优先）：
    export SERVERCHAN_SENDKEY="SCT..."
或在 Scripts/.secrets.json 写：
    {"serverchan_sendkey": "SCT..."}

自测：
    python3 "00 投研系统/Scripts/notify.py" --test
    python3 "00 投研系统/Scripts/notify.py" --test --dry-run
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
SECRETS_PATH = SCRIPT_DIR / ".secrets.json"
STATE_PATH = ROOT / "00 投研系统" / "Data" / "notify_state.json"

CURL_TIMEOUT = "20"


# ---------------------------------------------------------------- 配置

def get_sendkey() -> str:
    """env 优先，其次 .secrets.json。都没有就返回空串（调用方降级为只打印）。"""
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if key:
        return key
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("serverchan_sendkey", "")).strip()


def build_endpoint(sendkey: str) -> str:
    """兼容 Server酱 Turbo 和 Server酱³ 两代 key。

    Turbo:    SCTxxxxxxxx        -> https://sctapi.ftqq.com/<key>.send
    Server酱³: sctp<uid>t<token>  -> https://<uid>.push.ft07.com/send/<key>.send
    """
    match = re.match(r"^sctp(\d+)t", sendkey)
    if match:
        uid = match.group(1)
        return f"https://{uid}.push.ft07.com/send/{sendkey}.send"
    return f"https://sctapi.ftqq.com/{sendkey}.send"


# ---------------------------------------------------------------- 去重

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def already_sent_today(text: str) -> bool:
    state = _load_state()
    if state.get("date") != date.today().isoformat():
        return False
    return _fingerprint(text) in set(state.get("sent", []))


def mark_sent(text: str) -> None:
    today = date.today().isoformat()
    state = _load_state()
    if state.get("date") != today:
        state = {"date": today, "sent": []}
    sent = set(state.get("sent", []))
    sent.add(_fingerprint(text))
    state["sent"] = sorted(sent)
    _save_state(state)


# ---------------------------------------------------------------- 推送

def push(title: str, desp: str = "", *, dry_run: bool = False, dedupe: bool = True) -> dict:
    """推一条消息。返回 {"ok": bool, "reason": str}。

    预期的运行时失败会转成结构化结果，不让通知服务拖垮数据流程。
    """
    key_text = f"{title}\n{desp}"

    if dedupe and already_sent_today(key_text):
        return {"ok": True, "reason": "skipped: 今天已推送过相同内容"}

    if dry_run:
        print(f"[DRY-RUN] 标题：{title}")
        if desp:
            print(f"[DRY-RUN] 正文：\n{desp}")
        return {"ok": True, "reason": "dry-run"}

    sendkey = get_sendkey()
    if not sendkey:
        print(f"[通知未配置] {title}", file=sys.stderr)
        if desp:
            print(desp, file=sys.stderr)
        return {"ok": False, "reason": "未找到 SendKey（env SERVERCHAN_SENDKEY 或 Scripts/.secrets.json）"}

    cmd = [
        "curl", "-sS", "-L", "--ipv4", "--max-time", CURL_TIMEOUT,
        "-X", "POST", build_endpoint(sendkey),
        "--data-urlencode", f"title={title}",
        "--data-urlencode", f"desp={desp}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=int(CURL_TIMEOUT) + 5,
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "找不到 curl"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "curl 进程超时"}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "reason": f"curl 失败: {exc.stderr.strip()[:200]}"}
    except OSError as exc:
        return {"ok": False, "reason": f"无法启动 curl: {exc}"}

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "reason": f"响应不是 JSON: {result.stdout[:200]}"}

    if payload.get("code") == 0:
        if dedupe:
            mark_sent(key_text)
        return {"ok": True, "reason": "sent"}

    return {"ok": False, "reason": f"Server酱返回: {result.stdout[:200]}"}


def push_alerts(alerts: list[str], *, source: str = "投研系统", dry_run: bool = False) -> dict:
    """把一组告警合成一条推送。空列表则不推。"""
    if not alerts:
        return {"ok": True, "reason": "no alerts"}
    title = f"⚠️ {source}：{alerts[0][:30]}"
    if len(alerts) > 1:
        title += f" 等 {len(alerts)} 条"
    desp = "\n\n".join(f"- {a}" for a in alerts)
    desp += f"\n\n---\n\n{date.today().isoformat()} · 由 {source} 自动触发"
    return push(title, desp, dry_run=dry_run)


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    if "--test" in argv:
        result = push(
            "投研系统推送自测",
            "如果你在微信里看到这条，说明 Server酱链路已经通了。\n\n"
            "接下来它只会在触发告警规则时说话。",
            dry_run=dry_run,
            dedupe=False,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if "--status" in argv:
        sendkey = get_sendkey()
        print(f"SendKey: {'已配置（' + sendkey[:6] + '...）' if sendkey else '未配置'}")
        if sendkey:
            print(f"Endpoint: {build_endpoint(sendkey)}")
        print(f"State 文件: {STATE_PATH}")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
