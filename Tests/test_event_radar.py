from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "Scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import event_radar


NOW = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)


def market(
    market_id: str,
    bid: float,
    ask: float,
    *,
    liquidity: float = 100_000,
    open_market: bool = True,
    description: str = "stable rules",
) -> dict:
    return {
        "id": market_id,
        "question": f"Question {market_id}",
        "slug": f"market-{market_id}",
        "bestBid": bid,
        "bestAsk": ask,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": json.dumps([str((bid + ask) / 2), str(1 - (bid + ask) / 2)]),
        "liquidity": str(liquidity),
        "volume24hr": 50_000,
        "active": True,
        "closed": not open_market,
        "acceptingOrders": open_market,
        "description": description,
    }


def event(components: list[dict]) -> dict:
    return {
        "id": "test-event",
        "label_zh": "测试事件",
        "mode": "scalar",
        "source_url": "https://example.com/market",
        "resolution_source_url": "https://example.com/source",
        "resolution_summary_zh": "测试裁决规则",
        "components": components,
        "quality": {"min_liquidity_usd": 50_000, "max_spread_pp": 5},
        "triggers": {
            "change_1h_pp": 5,
            "change_24h_pp": 10,
            "thresholds_pct": [25, 50, 75],
            "confirmation_samples": 2,
            "cooldown_hours": 6,
            "realert_change_pp": 5,
        },
    }


def distribution_event() -> dict:
    config = event([])
    config.update({
        "id": "fed-test",
        "label_zh": "美联储测试分布",
        "mode": "distribution",
        "buckets": [
            {
                "id": "cut",
                "label_zh": "降息",
                "components": [
                    {"market_id": "1", "outcome": "Yes", "label": "降息25bp"},
                    {"market_id": "2", "outcome": "Yes", "label": "降息50bp以上"},
                ],
            },
            {
                "id": "unchanged",
                "label_zh": "不变",
                "components": [
                    {"market_id": "3", "outcome": "Yes", "label": "不变"},
                ],
            },
            {
                "id": "hike",
                "label_zh": "加息",
                "components": [
                    {"market_id": "4", "outcome": "Yes", "label": "加息25bp"},
                    {"market_id": "5", "outcome": "Yes", "label": "加息50bp以上"},
                ],
            },
        ],
        "quality": {
            "min_liquidity_usd": 50_000,
            "max_spread_pp": 5,
            "min_raw_total_pct": 90,
            "max_raw_total_pct": 110,
        },
    })
    config.pop("components", None)
    return config


def sample(at: datetime, probability: float, **extra) -> dict:
    row = {
        "timestamp": event_radar.isoformat_utc(at),
        "probability": probability,
        "quality_ok": True,
        "open": True,
        "rules_hash": "same",
    }
    row.update(extra)
    return row


def distribution_sample(
    at: datetime, cut: float, unchanged: float, hike: float, **extra
) -> dict:
    probabilities = {"cut": cut, "unchanged": unchanged, "hike": hike}
    row = {
        "timestamp": event_radar.isoformat_utc(at),
        "probabilities": probabilities,
        "leader_id": max(probabilities, key=probabilities.get),
        "quality_ok": True,
        "open": True,
        "rules_hash": "same",
    }
    row.update(extra)
    return row


class EventRadarTests(unittest.TestCase):
    def test_no_outcome_complements_yes_midpoint(self) -> None:
        row = event_radar.component_quote(
            {"market_id": "1", "outcome": "No", "label": "risk"},
            market("1", 0.20, 0.24),
        )
        self.assertAlmostEqual(row["probability"], 0.78)
        self.assertAlmostEqual(row["bid"], 0.76)
        self.assertAlmostEqual(row["ask"], 0.80)

    def test_composite_probability_sums_mutually_exclusive_outcomes(self) -> None:
        config = event([
            {"market_id": "1", "outcome": "Yes", "label": "25bp"},
            {"market_id": "2", "outcome": "Yes", "label": "50bp"},
        ])
        snapshot = event_radar.build_snapshot(
            config,
            {"1": market("1", 0.10, 0.12), "2": market("2", 0.03, 0.05)},
            NOW,
        )
        self.assertAlmostEqual(snapshot["probability"], 0.15)
        self.assertAlmostEqual(snapshot["spread_pp"], 4.0)
        self.assertTrue(snapshot["quality_ok"])

    def test_distribution_normalizes_cut_unchanged_and_hike_to_one(self) -> None:
        snapshot = event_radar.build_snapshot(
            distribution_event(),
            {
                "1": market("1", 0.01, 0.02),
                "2": market("2", 0.00, 0.01),
                "3": market("3", 0.61, 0.63),
                "4": market("4", 0.34, 0.36),
                "5": market("5", 0.00, 0.01),
            },
            NOW,
        )
        self.assertAlmostEqual(sum(snapshot["probabilities"].values()), 1.0)
        self.assertEqual([row["label_zh"] for row in snapshot["distribution"]], ["降息", "不变", "加息"])
        self.assertEqual(snapshot["leader"]["id"], "unchanged")
        self.assertNotIn("probability", snapshot)

    def test_first_sample_only_establishes_baseline(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        snapshot = event_radar.build_snapshot(config, {"1": market("1", 0.4, 0.42)}, NOW)
        triggers, samples = event_radar.detect_triggers(config, {}, snapshot)
        self.assertEqual(triggers, [])
        self.assertEqual(len(samples), 1)

    def test_one_hour_jump_requires_two_confirmations(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        history = [
            sample(NOW - timedelta(minutes=90), 0.30),
            sample(NOW - timedelta(minutes=75), 0.31),
            sample(NOW - timedelta(minutes=15), 0.37),
        ]
        snapshot = event_radar.build_snapshot(config, {"1": market("1", 0.37, 0.39)}, NOW)
        triggers, _ = event_radar.detect_triggers(config, {"samples": history}, snapshot)
        kinds = {row["kind"] for row in triggers}
        self.assertIn("change_1h_up", kinds)

    def test_threshold_crossing_requires_persistence(self) -> None:
        rows = [
            sample(NOW - timedelta(minutes=30), 0.49),
            sample(NOW - timedelta(minutes=15), 0.51),
            sample(NOW, 0.52),
        ]
        triggers = event_radar.confirmed_threshold_crossings(rows, [50], 2)
        self.assertEqual(triggers[0]["kind"], "threshold_up")

    def test_quality_gate_blocks_wide_market(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        snapshot = event_radar.build_snapshot(config, {"1": market("1", 0.20, 0.30)}, NOW)
        self.assertFalse(snapshot["quality_ok"])
        self.assertIn("买卖价差超过门槛", snapshot["quality_reasons"])

    def test_rules_change_is_always_reported(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        history = [sample(NOW - timedelta(minutes=15), 0.30, rules_hash="old")]
        snapshot = event_radar.build_snapshot(
            config, {"1": market("1", 0.29, 0.31, description="new")}, NOW
        )
        triggers, _ = event_radar.detect_triggers(config, {"samples": history}, snapshot)
        self.assertIn("rules_changed", {row["kind"] for row in triggers})

    def test_cooldown_requires_material_additional_move(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        snapshot = {
            "timestamp": event_radar.isoformat_utc(NOW),
            "mode": "scalar",
            "probability": 0.52,
        }
        state = {
            "last_alert_at": event_radar.isoformat_utc(NOW - timedelta(hours=1)),
            "last_alert_probability": 0.50,
        }
        triggers = [{"kind": "change_1h_up", "delta_pp": 6}]
        self.assertFalse(event_radar.cooldown_allows(config, state, snapshot, triggers))
        snapshot["probability"] = 0.56
        self.assertTrue(event_radar.cooldown_allows(config, state, snapshot, triggers))

    def test_distribution_change_trigger_names_the_bucket(self) -> None:
        config = distribution_event()
        history = [
            distribution_sample(NOW - timedelta(minutes=90), 0.10, 0.60, 0.30),
            distribution_sample(NOW - timedelta(minutes=75), 0.10, 0.60, 0.30),
            distribution_sample(NOW - timedelta(minutes=15), 0.17, 0.56, 0.27),
        ]
        snapshot = event_radar.build_snapshot(
            config,
            {
                "1": market("1", 0.08, 0.09),
                "2": market("2", 0.08, 0.09),
                "3": market("3", 0.54, 0.56),
                "4": market("4", 0.25, 0.27),
                "5": market("5", 0.01, 0.02),
            },
            NOW,
        )
        triggers, _ = event_radar.detect_triggers(config, {"samples": history}, snapshot)
        cut_changes = [
            row for row in triggers
            if row["kind"] == "change_1h_up" and row.get("bucket_id") == "cut"
        ]
        self.assertEqual(len(cut_changes), 1)
        self.assertEqual(cut_changes[0]["bucket_label"], "降息")

    def test_distribution_leader_change_requires_persistence(self) -> None:
        rows = [
            distribution_sample(NOW - timedelta(minutes=45), 0.10, 0.55, 0.35),
            distribution_sample(NOW - timedelta(minutes=30), 0.10, 0.40, 0.50),
            distribution_sample(NOW - timedelta(minutes=15), 0.10, 0.38, 0.52),
        ]
        trigger = event_radar.confirmed_leader_change(rows, 2)
        self.assertEqual(trigger["from_bucket_id"], "unchanged")
        self.assertEqual(trigger["bucket_id"], "hike")

    def test_distribution_notification_always_shows_all_outcomes(self) -> None:
        config = distribution_event()
        snapshot = event_radar.build_snapshot(
            config,
            {
                "1": market("1", 0.01, 0.02),
                "2": market("2", 0.00, 0.01),
                "3": market("3", 0.61, 0.63),
                "4": market("4", 0.34, 0.36),
                "5": market("5", 0.00, 0.01),
            },
            NOW,
        )
        _, body = event_radar.build_notification([{
            "snapshot": snapshot,
            "samples": [event_radar.compact_sample(snapshot)],
            "triggers": [{
                "kind": "threshold_up",
                "threshold_pct": 50,
                "bucket_id": "unchanged",
                "bucket_label": "不变",
            }],
        }])
        self.assertIn("降息", body)
        self.assertIn("不变", body)
        self.assertIn("加息", body)
        self.assertIn("归一化前五项合计", body)

    def test_dry_run_does_not_write_state_or_notify(self) -> None:
        config = event([{"market_id": "1", "outcome": "Yes", "label": "Yes"}])
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "events.json"
            config_path.write_text(
                json.dumps({"schema_version": 2, "events": [config]}), encoding="utf-8"
            )
            state_path = Path(temporary_directory) / "state.json"
            with patch.object(event_radar, "STATE_PATH", state_path), patch.object(
                event_radar, "fetch_market", return_value=market("1", 0.2, 0.22)
            ), patch.object(event_radar.notify, "push") as push:
                exit_code = event_radar.run(["--dry-run", "--config", str(config_path)], now=NOW)
        self.assertEqual(exit_code, 0)
        self.assertFalse(state_path.exists())
        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
