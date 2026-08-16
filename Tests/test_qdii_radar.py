from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "Scripts"))

import qdii_radar


NOW = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)


def fund(code: str, status: str, **extra: object) -> dict:
    row = {
        "code": code,
        "name": f"测试基金 {code}",
        "manager": "测试管理人",
        "share_class": "A",
        "status": status,
        "purchase_limit_rmb": None,
        "regular_investment": "开放",
        "effective_date": "2026-08-12",
        "source_url": "https://example.com/fund",
        "status_source": "manual",
    }
    row.update(extra)
    return row


class QDIIRadarTests(unittest.TestCase):
    def test_normalize_status_prioritizes_suspension(self) -> None:
        self.assertEqual(qdii_radar.normalize_status("", "暂停申购，恢复日期另行公告"), qdii_radar.STATUS_SUSPENDED)
        self.assertEqual(qdii_radar.normalize_status("", "限制大额申购，单日上限5万元"), qdii_radar.STATUS_LIMITED)
        self.assertEqual(qdii_radar.normalize_status(qdii_radar.STATUS_UNKNOWN, "开放申购"), qdii_radar.STATUS_NORMAL)
        self.assertEqual(qdii_radar.normalize_status("开放申购"), qdii_radar.STATUS_NORMAL)
        self.assertEqual(qdii_radar.normalize_status("未知文本"), qdii_radar.STATUS_UNKNOWN)

    def test_parse_public_trade_state_prioritizes_suspension_over_stale_quota(self) -> None:
        text = '交易状态：</span><span class="staticCell">暂停申购  (<span>单日累计购买上限100.00元</span>)</span>'
        parsed = qdii_radar.parse_eastmoney_trade_state(text)
        self.assertEqual(parsed["status"], qdii_radar.STATUS_SUSPENDED)
        self.assertEqual(parsed["purchase_limit_rmb"], 100.0)

    def test_parse_public_trade_state_without_quota_is_suspended(self) -> None:
        text = '交易状态：</span><span class="staticCell">暂停申购</span>'
        parsed = qdii_radar.parse_eastmoney_trade_state(text)
        self.assertEqual(parsed["status"], qdii_radar.STATUS_SUSPENDED)

    def test_parse_direct_page_extracts_explicit_quota(self) -> None:
        text = "<div>交易状态：限制大额申购，单日单个基金账户申购上限100元</div>"
        parsed = qdii_radar.parse_direct_trade_state(text)
        self.assertEqual(parsed["status"], qdii_radar.STATUS_LIMITED)
        self.assertEqual(parsed["purchase_limit_rmb"], 100.0)

    def test_parse_direct_page_recognizes_quota_subscription(self) -> None:
        parsed = qdii_radar.parse_direct_trade_state(
            "<div>基金代码014978</div><div>交易状态：单日单账户限额直销100元，限额申购，开放定投</div>",
            fund_code="014978",
        )
        self.assertEqual(parsed["status"], qdii_radar.STATUS_LIMITED)
        self.assertEqual(parsed["purchase_limit_rmb"], 100.0)

    def test_parse_direct_table_uses_code_specific_row(self) -> None:
        parsed = qdii_radar.parse_direct_trade_state(
            "<div>000001 暂停申购</div><div>016532/016533/021838 嘉实纳斯达克100ETF发起联接 A/C/I 暂停申购 暂停定投</div><div>000002 限制申购</div>",
            fund_code="016532",
        )
        self.assertEqual(parsed["status"], qdii_radar.STATUS_SUSPENDED)

    def test_parse_direct_quota_api_extracts_current_ceiling(self) -> None:
        parsed = qdii_radar.parse_direct_trade_state(
            '{"CUSTTYPE":"1","FUNDCODE":"270042","MAX_ALLOT_BALA":"5","MIN_ALLOT_BALA":"1"}'
        )
        self.assertEqual(parsed["status"], qdii_radar.STATUS_LIMITED)
        self.assertEqual(parsed["purchase_limit_rmb"], 5.0)

    def test_parse_direct_page_recognizes_stop_as_suspended(self) -> None:
        parsed = qdii_radar.parse_direct_trade_state("<div>网上直销：停止申购</div>")
        self.assertEqual(parsed["status"], qdii_radar.STATUS_SUSPENDED)
        self.assertIsNone(parsed["purchase_limit_rmb"])

    def test_parse_direct_page_does_not_let_history_override_current_status(self) -> None:
        parsed = qdii_radar.parse_direct_trade_state(
            "<div>当前交易状态：开放申购</div><div>历史公告：暂停申购</div>"
        )
        self.assertEqual(parsed["status"], qdii_radar.STATUS_NORMAL)

    def test_live_page_without_quota_clears_old_quota(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            source_url="https://www.efunds.com.cn/direct",
            source_mode="official_direct_html",
            purchase_limit_rmb=100,
            channel="manager_direct",
            direct_sales="true",
        )
        with patch.object(qdii_radar, "fetch_text", return_value="<div>当前交易状态：开放申购</div>"):
            collected = qdii_radar.collect_fund(row, live=True, checked_at=NOW)
        self.assertEqual(collected["status"], qdii_radar.STATUS_NORMAL)
        self.assertIsNone(collected["purchase_limit_rmb"])

    def test_snapshot_groups_current_status_and_marks_changes_without_duplicates(self) -> None:
        previous = {
            "funds": {
                "A": {"status": qdii_radar.STATUS_NORMAL, "purchase_limit_rmb": None, "regular_investment": "开放", "effective_date": "2026-08-11"},
                "B": {"status": qdii_radar.STATUS_LIMITED, "purchase_limit_rmb": 50000, "regular_investment": "开放", "effective_date": "2026-08-11"},
            }
        }
        snapshot = qdii_radar.build_snapshot(
            [fund("A", qdii_radar.STATUS_LIMITED), fund("B", qdii_radar.STATUS_LIMITED, purchase_limit_rmb=10000), fund("C", qdii_radar.STATUS_SUSPENDED)],
            previous,
            NOW,
        )
        self.assertEqual(snapshot["counts"][qdii_radar.STATUS_LIMITED], 2)
        self.assertEqual(snapshot["counts"][qdii_radar.STATUS_SUSPENDED], 1)
        self.assertEqual(snapshot["changed_count"], 2)
        self.assertEqual(len(snapshot["records"]), 3)
        previous_by_code = {row["code"]: row["previous_status"] for row in snapshot["records"]}
        self.assertEqual(previous_by_code["A"], qdii_radar.STATUS_NORMAL)

    def test_digest_is_summary_and_mentions_official_source_boundary(self) -> None:
        snapshot = qdii_radar.build_snapshot(
            [fund("A", qdii_radar.STATUS_NORMAL), fund("B", qdii_radar.STATUS_LIMITED)],
            {"funds": {}},
            NOW,
        )
        title, body = qdii_radar.build_daily_digest(snapshot, checked_at=NOW)
        self.assertIn("纳指100 QDII", title)
        self.assertIn("正常开放：1", body)
        self.assertIn("限制申购：1", body)
        self.assertIn("只采用基金管理人直销平台或基金管理人公告", body)
        self.assertIn("不使用天天基金等代销平台额度", body)
        self.assertNotIn("| 测试基金", body)

    def test_load_funds_only_includes_manager_direct_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "universe.csv"
            csv_path.write_text(
                "code,name,direct_sales,channel,status,source_mode,source_url\n"
                "A,官方直销基金,true,manager_direct,暂停申购,official_direct,https://example.com/official\n"
                "B,代销详情页基金,true,agency,限制申购,eastmoney_html,https://example.com/public\n"
                "C,非直销基金,false,manager_direct,正常开放,official_notice,https://example.com/official\n",
                encoding="utf-8",
            )
            rows = qdii_radar.load_funds({"universe_csv": str(csv_path), "funds": []})
        self.assertEqual({row["code"] for row in rows}, {"A"})

    def test_shared_quota_is_labeled_as_combined(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            purchase_limit_rmb=10,
            quota_scope="combined_aci",
        )
        self.assertIn("¥10/日（A/C/I合计）", qdii_radar.html_row(row))

    def test_public_page_contains_all_status_sections_and_change_badge(self) -> None:
        previous = {"funds": {"A": {"status": qdii_radar.STATUS_NORMAL, "purchase_limit_rmb": None, "regular_investment": "开放", "effective_date": "2026-08-11"}}}
        snapshot = qdii_radar.build_snapshot([fund("A", qdii_radar.STATUS_SUSPENDED)], previous, NOW)
        page = qdii_radar.build_public_page(snapshot)
        self.assertIn("暂停申购", page)
        self.assertIn("今日变化", page)
        self.assertEqual(page.count("测试基金 A"), 1)

    def test_non_official_source_is_pending_without_blocking_snapshot(self) -> None:
        snapshot = qdii_radar.build_snapshot(
            [fund("A", qdii_radar.STATUS_LIMITED, source_url="https://www.howbuy.com/fund/1")],
            {"funds": {}},
            NOW,
        )
        self.assertEqual(snapshot["review_pending"], 1)
        self.assertEqual(snapshot["records"][0]["review_status"], "待核验")
        self.assertIn("待核验", qdii_radar.html_row(snapshot["records"][0]))

    def test_official_detail_page_uses_verification_time_without_effective_date_warning(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            source_url="https://e.efunds.com.cn/cart/subscriptions?fundCode=A",
            source_mode="official_direct_html",
            effective_date="2026-08-12核验",
        )
        self.assertEqual(qdii_radar.source_review_flags(row), [])

    def test_designated_disclosure_publication_is_valid_notice_source(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            source_url="https://epaper.stcn.com/att/202604/23/example.pdf",
            source_mode="official_notice_publication",
            effective_date="2026-04-23",
        )
        self.assertEqual(qdii_radar.source_review_flags(row, today=NOW.date()), [])

    def test_limited_rows_sort_by_verified_quota_descending(self) -> None:
        high = fund("HIGH", qdii_radar.STATUS_LIMITED, purchase_limit_rmb=50000, source_url="https://www.efunds.com.cn/direct", source_mode="official_direct_html")
        low = fund("LOW", qdii_radar.STATUS_LIMITED, purchase_limit_rmb=10, source_url="https://www.efunds.com.cn/direct", source_mode="official_direct_html")
        snapshot = qdii_radar.build_snapshot([low, high], {"funds": {}}, NOW)
        self.assertEqual([row["code"] for row in snapshot["records"]], ["HIGH", "LOW"])
        page = qdii_radar.build_public_page(snapshot)
        self.assertLess(page.index("测试基金 HIGH"), page.index("测试基金 LOW"))

    def test_source_review_flags_rejects_non_https_live_source(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            source_url="http://www.efunds.com.cn/direct",
            source_mode="official_direct_html",
        )
        self.assertIn("source_non_https", qdii_radar.source_review_flags(row))

    def test_live_fetch_failure_does_not_create_false_status_change(self) -> None:
        row = fund(
            "A",
            qdii_radar.STATUS_LIMITED,
            source_url="https://example.com/direct",
            source_mode="official_direct_html",
            purchase_limit_rmb=100,
            channel="manager_direct",
            direct_sales="true",
        )
        with patch.object(qdii_radar, "fetch_text", side_effect=RuntimeError("offline")):
            collected = qdii_radar.collect_fund(row, live=True, checked_at=NOW)
        self.assertEqual(collected["status"], qdii_radar.STATUS_LIMITED)
        self.assertEqual(collected["purchase_limit_rmb"], 100.0)
        self.assertEqual(collected["freshness_status"], "stale_fetch_failed")

    def test_legacy_state_version_is_migrated_without_dropping_baseline(self) -> None:
        legacy = {"version": 1, "funds": {"A": {
            "status": qdii_radar.STATUS_LIMITED,
            "purchase_limit_rmb": 100,
            "regular_investment": "开放",
            "effective_date": "2026-08-11",
        }}}
        migrated = qdii_radar.normalize_state(legacy)
        self.assertEqual(migrated["version"], qdii_radar.STATE_VERSION)
        self.assertIn("A", migrated["funds"])
        row = fund("A", qdii_radar.STATUS_LIMITED, purchase_limit_rmb=10)
        self.assertEqual(
            qdii_radar.compare_fields(row, migrated["funds"]["A"]),
            ["purchase_limit_rmb", "effective_date"],
        )

    def test_digest_image_url_uses_checked_timestamp_cache_buster(self) -> None:
        snapshot = qdii_radar.build_snapshot(
            [fund("A", qdii_radar.STATUS_NORMAL)], {"funds": {}}, NOW
        )
        _, body = qdii_radar.build_daily_digest(
            snapshot, image_url="https://example.test/latest.png", checked_at=NOW
        )
        self.assertIn("latest.png?v=202608120000", body)


if __name__ == "__main__":
    unittest.main()
