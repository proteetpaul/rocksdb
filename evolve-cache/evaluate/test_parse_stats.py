"""Tests for cache tier parse_stats helpers."""

from __future__ import annotations

import unittest

from parse_stats import (
    controller_log_artifacts,
    derive_cache_hit_rate_metrics,
    parse_block_cache_statistics,
    parse_cache_tier_controller_logs,
    parse_cache_tier_statistics,
    parse_read_block_get_histogram,
)


class TestParseBlockCacheStatistics(unittest.TestCase):
    def test_hit_and_miss_counts_from_statistics_block(self) -> None:
        text = (
            "STATISTICS:\n"
            "rocksdb.block.cache.hit COUNT : 90000\n"
            "rocksdb.block.cache.miss COUNT : 1000\n"
        )
        out = parse_block_cache_statistics(text)
        self.assertEqual(out["rocksdb.block.cache.hit.count"], 90000.0)
        self.assertEqual(out["rocksdb.block.cache.miss.count"], 1000.0)


class TestParseCacheTierStatistics(unittest.TestCase):
    def test_secondary_and_compressed_tickers(self) -> None:
        text = (
            "STATISTICS:\n"
            "rocksdb.secondary.cache.hits COUNT : 500\n"
            "rocksdb.compressed.secondary.cache.hits COUNT : 200\n"
            "rocksdb.compressed.secondary.cache.dummy.hits COUNT : 50\n"
        )
        out = parse_cache_tier_statistics(text)
        self.assertEqual(out["rocksdb.secondary.cache.hits.count"], 500.0)
        self.assertEqual(out["rocksdb.compressed.secondary.cache.hits.count"], 200.0)
        self.assertEqual(
            out["rocksdb.compressed.secondary.cache.dummy.hits.count"], 50.0
        )


class TestDeriveCacheHitRateMetrics(unittest.TestCase):
    def test_hit_rates(self) -> None:
        block = {
            "rocksdb.block.cache.hit.count": 9000.0,
            "rocksdb.block.cache.miss.count": 1000.0,
        }
        tier = {
            "rocksdb.secondary.cache.hits.count": 300.0,
        }
        out = derive_cache_hit_rate_metrics(block, tier)
        self.assertAlmostEqual(out["primary_cache_hit_rate"], 0.87)
        self.assertAlmostEqual(out["secondary_cache_hit_rate"], 300.0 / 1300.0)


class TestParseReadBlockGetHistogram(unittest.TestCase):
    def test_read_block_get_micros_histogram(self) -> None:
        text = (
            "STATISTICS:\n"
            "rocksdb.read.block.get.micros P50 : 120.0 P95 : 400.0 "
            "P99 : 800.0 P100 : 1000.0 COUNT : 50 SUM : 15000\n"
        )
        out = parse_read_block_get_histogram(text)
        self.assertEqual(out["rocksdb.read.block.get.micros.p50"], 120.0)
        self.assertEqual(out["rocksdb.read.block.get.micros.p99"], 800.0)
        self.assertNotIn("rocksdb.read.block.get.micros.count", out)
        self.assertNotIn("rocksdb.read.block.get.micros.sum", out)


class TestParseCacheTierControllerLogs(unittest.TestCase):
    def test_adjustments_and_failures(self) -> None:
        text = (
            "2026/05/26-12:00:00.123456 1abc Cache tier controller: adjusted "
            "secondary ratio from 0.50 to 0.55\n"
            "2026/05/26-12:00:10.123456 1abc Cache tier controller: adjusted "
            "secondary ratio from 0.55 to 0.60\n"
            "2026/05/26-12:00:20.123456 1abc Cache tier controller: failed to "
            "adjust secondary ratio from 0.60 to 0.65: Not supported\n"
        )
        entries = parse_cache_tier_controller_logs(text)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].level, "info")
        self.assertAlmostEqual(entries[0].from_ratio, 0.50)
        self.assertAlmostEqual(entries[0].to_ratio, 0.55)
        self.assertEqual(entries[2].level, "warn")
        self.assertEqual(entries[2].error, "Not supported")

        artifacts = controller_log_artifacts(entries)
        self.assertEqual(artifacts["controller_adjustment_count"], "2")
        self.assertEqual(artifacts["controller_adjustment_failures"], "1")
        self.assertEqual(artifacts["controller_final_secondary_ratio"], "0.6")
        self.assertIn("adjusted secondary ratio from 0.55 to 0.60", artifacts["controller_log"])


if __name__ == "__main__":
    unittest.main()
