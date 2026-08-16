from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "Scripts"))

import notify


class NotifyTests(unittest.TestCase):
    def test_dedupe_uses_configured_customer_timezone(self) -> None:
        with patch.object(notify, "already_sent_today", return_value=True) as already:
            result = notify.push("日报", "正文", timezone_name="Asia/Shanghai")
        self.assertTrue(result["ok"])
        self.assertIn("已推送", result["reason"])
        already.assert_called_once_with("日报\n正文", timezone_name="Asia/Shanghai")

    def test_endpoint_supports_both_serverchan_key_formats(self) -> None:
        self.assertIn("sctapi.ftqq.com", notify.build_endpoint("SCTdemo"))
        self.assertIn("push.ft07.com", notify.build_endpoint("sctp123tsecret"))


if __name__ == "__main__":
    unittest.main()
