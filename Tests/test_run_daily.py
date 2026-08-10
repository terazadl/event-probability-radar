from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "Scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import notify
import run_daily


class RunDailyTests(unittest.TestCase):
    def test_alerts_and_failures_share_one_notification(self) -> None:
        failures = [
            {"name": "资金流", "error": "FINRA unavailable", "ok": False}
        ]
        payload = run_daily.build_notification(
            ["【双收缩】测试告警"], failures, all_failed=False
        )
        self.assertIsNotNone(payload)
        title, body = payload
        self.assertIn("1 条告警", title)
        self.assertIn("1 项任务失败", title)
        self.assertIn("市场告警", body)
        self.assertIn("抓取失败", body)
        self.assertIn("FINRA unavailable", body)

    def test_no_notification_when_there_is_nothing_to_report(self) -> None:
        self.assertIsNone(
            run_daily.build_notification([], [], all_failed=False)
        )

    def test_failed_run_does_not_mark_today_as_successful(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "run_state.json"
            with patch.object(run_daily, "RUN_STATE", state_path):
                run_daily.mark_run(ok=False)
                state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("last_run_at", state)
        self.assertNotIn("last_success_date", state)

    def test_successful_run_marks_today(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "run_state.json"
            with patch.object(run_daily, "RUN_STATE", state_path):
                run_daily.mark_run(ok=True)
                state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_success_date"], date.today().isoformat())

    def test_auto_commit_allowlist_excludes_handwritten_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            system_dir = Path(temporary_directory)
            generated = [
                system_dir / "Data/fred/latest.json",
                system_dir
                / f"Reports/FRED 利率与通胀快照 {date.today().isoformat()}.md",
                system_dir
                / f"Logs/runs/{date.today().strftime('%Y-%m')}.md",
            ]
            for path in generated:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated", encoding="utf-8")
            handwritten = system_dir / "Data/核心指标清单.md"
            handwritten.write_text("handwritten", encoding="utf-8")

            with patch.object(run_daily, "SYSTEM_DIR", system_dir):
                paths = run_daily.auto_commit_paths()

        self.assertIn("Data/fred/latest.json", paths)
        self.assertNotIn("Data/核心指标清单.md", paths)
        self.assertTrue(all(not path.endswith("核心指标清单.md") for path in paths))

    def test_serverchan_endpoint_detection(self) -> None:
        self.assertEqual(
            notify.build_endpoint("sctp123tTOKEN"),
            "https://123.push.ft07.com/send/sctp123tTOKEN.send",
        )
        self.assertEqual(
            notify.build_endpoint("SCT_TOKEN"),
            "https://sctapi.ftqq.com/SCT_TOKEN.send",
        )

    def test_partial_failure_returns_nonzero_and_is_not_marked_successful(self) -> None:
        results = [
            {
                "name": "FRED 利率与通胀",
                "ok": True,
                "seconds": 0.1,
                "error": "",
                "stdout": "snapshot",
            },
            {
                "name": "资金流",
                "ok": False,
                "seconds": 0.2,
                "error": "FINRA unavailable",
                "stdout": "partial snapshot",
            },
        ]
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(run_daily, "ran_today", return_value=False)
            )
            stack.enter_context(
                patch.object(run_daily, "run_job", side_effect=results)
            )
            stack.enter_context(
                patch.object(
                    run_daily, "collect_alerts", return_value=["测试告警"]
                )
            )
            push_mock = stack.enter_context(
                patch.object(run_daily.notify, "push", return_value={"ok": True})
            )
            stack.enter_context(
                patch.object(
                    run_daily,
                    "write_log",
                    return_value=Path("Logs/runs/test.md"),
                )
            )
            mark_run = stack.enter_context(patch.object(run_daily, "mark_run"))
            exit_code = run_daily.main(["--dry-run"])

        self.assertEqual(exit_code, 1)
        mark_run.assert_called_once_with(ok=False)
        pushed_body = push_mock.call_args.args[1]
        self.assertIn("测试告警", pushed_body)
        self.assertIn("FINRA unavailable", pushed_body)


if __name__ == "__main__":
    unittest.main()
