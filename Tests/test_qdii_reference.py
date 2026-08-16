from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "Scripts"))

import qdii_reference


SAMPLE_HTML = """
<p>最新一期 · 2026-08-14（每交易日自动更新）</p>
<h3>标普500 <span>共 1 只</span></h3><table class="readout"><tr><td>ignore（000001）</td><td>限大额</td><td>10元</td><td>—</td><td><a href="https://example.test/ignore">公告</a></td></tr></table>
<h3>纳斯达克100 <span>共 3 只</span></h3>
<table class="readout"><thead><tr><th>基金</th></tr></thead><tbody>
<tr><td><a href="/fund/000834.html">大成纳斯达克100ETF联接(QDII)A</a>（000834）</td><td>限大额 ✓公告直核</td><td>代销 10元 直销 100元 10×</td><td>代销（第三方）10元；大成直销（官网）100元</td><td><a href="https://example.test/notice-a">2026-06-03 公告</a></td></tr>
<tr><td><a href="/fund/021000.html">南方纳斯达克100指数发起(QDII)I</a>（021000）</td><td>限大额 ✓公告直核</td><td>200元</td><td>仅南方基金直销(APP/官网)，代销无此额度</td><td><a href="https://example.test/notice-i">公告</a></td></tr>
<tr><td><a href="/fund/012870.html">易方达纳斯达克100C</a>（012870）</td><td>暂停申购 ✓公告直核</td><td>—</td><td>—</td><td><a href="https://example.test/notice-c">公告</a></td></tr>
</tbody></table>
"""


class QDIIReferenceTests(unittest.TestCase):
    def test_parse_amounts_and_status(self) -> None:
        self.assertEqual(qdii_reference.parse_amount_rmb("1万元"), 10_000)
        self.assertEqual(qdii_reference.normalize_status("暂停申购 ✓公告直核"), "暂停申购")

    def test_parser_only_reads_nasdaq_table_and_preserves_channels(self) -> None:
        snapshot = qdii_reference.build_snapshot(SAMPLE_HTML, fetched_at="2026-08-16T00:00:00Z")
        self.assertEqual(snapshot["page_date"], "2026-08-14")
        self.assertEqual(snapshot["fetched_at"], "2026-08-16T00:00:00Z")
        self.assertEqual(len(snapshot["content_hash"]), 64)
        self.assertEqual([row["code"] for row in snapshot["records"]], ["000834", "021000", "012870"])
        first, direct_only, suspended = snapshot["records"]
        self.assertEqual(first["agent_limit_rmb"], 10)
        self.assertEqual(first["direct_limit_rmb"], 100)
        self.assertEqual(first["source_tier"], "secondary_announcement_checked")
        self.assertIsNone(direct_only["agent_limit_rmb"])
        self.assertEqual(direct_only["direct_limit_rmb"], 200)
        self.assertEqual(suspended["status"], "暂停申购")


if __name__ == "__main__":
    unittest.main()
