"""Tests for parse_stats helpers."""

from __future__ import annotations

import unittest

from evaluate.parse_stats import (
    LiveSstFilterBytes,
    parse_block_cache_statistics,
    parse_live_sst_filter_bytes,
)


class TestParseLiveSstFilterBytes(unittest.TestCase):
    def test_per_level_and_aggregated_prefers_level_sum(self) -> None:
        text = (
            "Level[0]: rocksdb.filter.size: 100\n"
            "Level[2]: rocksdb.filter.size: 50\n"
            "Level[0]: rocksdb.filter.size: 10\n"
        )
        out = parse_live_sst_filter_bytes(text)
        self.assertIsInstance(out, LiveSstFilterBytes)
        self.assertEqual(out.per_level_bytes, {0: 10.0, 2: 50.0})
        self.assertIsNone(out.global_bytes)
        self.assertEqual(out.aggregated_bytes(), 60.0)

    def test_global_when_no_level_lines(self) -> None:
        text = "rocksdb.filter.size: 42\nother line\nrocksdb.filter.size: 99\n"
        out = parse_live_sst_filter_bytes(text)
        self.assertEqual(out.per_level_bytes, {})
        self.assertEqual(out.global_bytes, 99.0)
        self.assertEqual(out.aggregated_bytes(), 99.0)

    def test_tracks_global_even_when_levels_present(self) -> None:
        text = (
            "rocksdb.filter.size: 7\n"
            "Level[1]: rocksdb.filter.size: 11\n"
            "table rocksdb.filter.size: 13\n"
        )
        out = parse_live_sst_filter_bytes(text)
        self.assertEqual(out.per_level_bytes, {1: 11.0})
        self.assertEqual(out.global_bytes, 13.0)
        self.assertEqual(out.aggregated_bytes(), 11.0)


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


if __name__ == "__main__":
    unittest.main()
