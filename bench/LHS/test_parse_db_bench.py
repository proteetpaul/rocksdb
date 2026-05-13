"""Unit tests for db_bench output and du parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.LHS.parse_db_bench import (
    parse_db_bench_output,
    parse_du_sh_stdout,
    parse_rocksdb_statistics,
    parse_read_write_histograms,
    parse_throughput_readrandomwriterandom,
)


_FIXTURE = """
readrandomwriterandom : 10.000 micros/op 50000 ops/sec 1.000 seconds 1000000 operations

Microseconds per read:
Count: 1000 Average: 5.0000  StdDev: 1.00
Min: 1  Median: 4.9000  Max: 20
Percentiles: P50: 4.50 P75: 6.00 P99: 18.00 P99.9: 19.00 P99.99: 20.00
------------------------------------------------------

Microseconds per write:
Count: 1000 Average: 8.0000  StdDev: 2.00
Min: 2  Median: 7.5000  Max: 30
Percentiles: P50: 7.00 P75: 10.00 P99: 25.00 P99.9: 29.00 P99.99: 30.00
------------------------------------------------------

rocksdb.stall.micros COUNT : 21000
rocksdb.block.cache.hit COUNT : 101
rocksdb.db.write.stall COUNT : 3
rocksdb.db.write.stall P50 : 10.000000 P95 : 17.000000 P99 : 22.000000 P100 : 25.000000 COUNT : 3 SUM : 45
"""


class TestParseDbBench(unittest.TestCase):
    def test_throughput(self) -> None:
        self.assertAlmostEqual(
            parse_throughput_readrandomwriterandom(_FIXTURE),
            50000.0,
        )

    def test_histogram_pairs(self) -> None:
        rp, wp = parse_read_write_histograms(_FIXTURE)
        assert rp is not None and wp is not None
        self.assertAlmostEqual(rp[0], 4.50)
        self.assertAlmostEqual(rp[1], 18.00)
        self.assertAlmostEqual(wp[0], 7.00)
        self.assertAlmostEqual(wp[1], 25.00)

    def test_parse_db_bench_output(self) -> None:
        m = parse_db_bench_output(_FIXTURE)
        self.assertAlmostEqual(m.throughput_qps or 0.0, 50000.0)
        self.assertAlmostEqual(m.read_p50_us or 0.0, 4.50)
        self.assertAlmostEqual(m.write_p99_us or 0.0, 25.00)
        self.assertIn("rocksdb.stall.micros", m.statistics)
        self.assertIn("rocksdb.db.write.stall", m.statistics)
        self.assertEqual(m.statistics["rocksdb.stall.micros"]["count"], 21000)
        self.assertEqual(m.statistics["rocksdb.db.write.stall"]["kind"], "histogram")

    def test_parse_rocksdb_statistics(self) -> None:
        parsed = parse_rocksdb_statistics(_FIXTURE)
        self.assertEqual(parsed["rocksdb.block.cache.hit"]["kind"], "ticker")
        self.assertEqual(parsed["rocksdb.block.cache.hit"]["count"], 101)
        self.assertEqual(parsed["rocksdb.db.write.stall"]["kind"], "histogram")
        self.assertAlmostEqual(parsed["rocksdb.db.write.stall"]["p99"], 22.0)
        self.assertEqual(parsed["rocksdb.db.write.stall"]["count"], 3)
        self.assertEqual(parsed["rocksdb.db.write.stall"]["sum"], 45)

    def test_du_sh(self) -> None:
        self.assertEqual(parse_du_sh_stdout("1.2G\t/tmp/db\n"), "1.2G")
        self.assertEqual(parse_du_sh_stdout("512K\n"), "512K")


if __name__ == "__main__":
    unittest.main()
