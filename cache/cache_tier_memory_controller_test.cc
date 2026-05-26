//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "cache/cache_tier_memory_controller.h"

#include "cache/compressed_secondary_cache.h"
#include "cache/hit_rate_ghost_policy.h"
#include "cache/secondary_cache_adapter.h"
#include "rocksdb/statistics.h"
#include "test_util/mock_time_env.h"
#include "test_util/secondary_cache_test_util.h"
#include "test_util/testharness.h"
#include "util/compression.h"
#include "util/cast_util.h"
#include "util/random.h"

namespace ROCKSDB_NAMESPACE {

namespace {

class FixedRatioPolicy : public CacheTierMemoryPolicy {
 public:
  explicit FixedRatioPolicy(double ratio) : ratio_(ratio) {}
  double ComputeSecondaryRatio(const CacheTierMemoryWindow& /*window*/,
                               double /*current_ratio*/) override {
    return ratio_;
  }

 private:
  double ratio_;
};

class CapturingPolicy : public CacheTierMemoryPolicy {
 public:
  double ComputeSecondaryRatio(const CacheTierMemoryWindow& window,
                               double current_ratio) override {
    last_window = window;
    return current_ratio;
  }

  CacheTierMemoryWindow last_window;
};

std::shared_ptr<Cache> NewStackedTestCache(size_t pri_cap, size_t sec_cap) {
  LRUCacheOptions pri_opts;
  pri_opts.capacity = pri_cap;
  auto pri_cache = NewLRUCache(pri_opts);
  CompressedSecondaryCacheOptions sec_opts;
  sec_opts.capacity = sec_cap;
  auto sec_cache = NewCompressedSecondaryCache(sec_opts);
  return std::make_shared<CacheWithSecondaryAdapter>(
      pri_cache, sec_cache, TieredAdmissionPolicy::kAdmPolicyAuto,
      /*distribute_cache_res=*/false);
}

void SetTicker(Statistics* stats, Tickers ticker, uint64_t value) {
  stats->setTickerCount(ticker, value);
}

}  // namespace

TEST(CacheTierMemoryControllerTest, SampleHitRatesAndAdjust) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  opts.warmup_time_sec = 0;
  opts.min_policy_intervals = 0;
  opts.min_cache_lookups = 0;
  opts.max_ratio_delta_per_step = 0.2;
  opts.policy = std::make_shared<FixedRatioPolicy>(0.35);
  CacheTierMemoryController controller(mock_clock.get(), opts);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);
  controller.Sample(stats.get(), cache.get());

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 80);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 20);
  SetTicker(stats.get(), SECONDARY_CACHE_HITS, 10);
  SetTicker(stats.get(), COMPRESSED_SECONDARY_CACHE_HITS, 5);
  controller.Sample(stats.get(), cache.get());
  ASSERT_EQ(1u, controller.TEST_NumSnapshots());
  EXPECT_DOUBLE_EQ(0.5, controller.TEST_SnapshotAt(0).secondary_hit_rate);
  ASSERT_OK(controller.MaybeAdjust(cache, nullptr));
  double ratio = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &ratio));
  ASSERT_GE(ratio, 0.3);
}

TEST(CacheTierMemoryControllerTest, WarmupGateSkipsAdjust) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);
  double before = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &before));

  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.warmup_time_sec = 1000000;
  opts.min_policy_intervals = 100;
  opts.min_cache_lookups = 1000000;
  opts.policy = std::make_shared<FixedRatioPolicy>(0.5);
  CacheTierMemoryController controller(mock_clock.get(), opts);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 100);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 100);
  controller.Sample(stats.get(), cache.get());
  for (uint32_t i = 0; i < opts.min_policy_intervals + 1; ++i) {
    ASSERT_OK(controller.MaybeAdjust(cache, nullptr));
  }

  double after = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &after));
  ASSERT_DOUBLE_EQ(before, after);
}

TEST(CacheTierMemoryControllerTest, TuningDurationSecStopsAdjust) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);
  double before = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &before));

  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.warmup_time_sec = 0;
  opts.min_policy_intervals = 0;
  opts.min_cache_lookups = 0;
  opts.tuning_duration_sec = 60;
  opts.max_ratio_delta_per_step = 0.2;
  opts.policy = std::make_shared<FixedRatioPolicy>(0.5);
  CacheTierMemoryController controller(mock_clock.get(), opts);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 100);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 100);
  controller.Sample(stats.get(), cache.get());
  controller.Sample(stats.get(), cache.get());
  ASSERT_OK(controller.MaybeAdjust(cache, nullptr));
  double after_warmup = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &after_warmup));
  ASSERT_NE(before, after_warmup);

  mock_clock->SleepForMicroseconds(61 * 1000000);
  double before_expired = after_warmup;
  ASSERT_OK(controller.MaybeAdjust(cache, nullptr));
  double after_expired = 0.0;
  ASSERT_TRUE(GetCacheTierSecondaryRatio(cache.get(), &after_expired));
  ASSERT_DOUBLE_EQ(before_expired, after_expired);
}

TEST(CacheTierMemoryControllerTest, HitRateGhostPolicyIncrease) {
  CacheTierMemoryWindow window;
  CacheTierMemorySnapshot snap;
  snap.primary_hit_rate = 0.9;
    snap.secondary_hit_rate = 0.25;
    snap.dummy_hits = 1;
  snap.sec_cache_uncompressed_bytes = 1000;
  snap.sec_cache_compressed_bytes = 300;
  snap.compression_ratio = 0.3;
  snap.mean_decompress_us = 50.0;
  window.samples.push_back(snap);
  window.current_secondary_ratio = 0.2;
  HitRateGhostPolicy policy;
  const double target = policy.ComputeSecondaryRatio(window, 0.2);
  ASSERT_GT(target, 0.2);
}

TEST(CacheTierMemoryControllerTest, HitRateGhostPolicyDecreaseOnExpensiveDecompress) {
  CacheTierMemoryWindow window;
  CacheTierMemorySnapshot snap;
  snap.primary_hit_rate = 0.7;
  snap.secondary_hit_rate = 0.15;
  snap.dummy_hits = 20;
  snap.sec_cache_uncompressed_bytes = 1000;
  snap.sec_cache_compressed_bytes = 900;
  snap.compression_ratio = 0.9;
  snap.mean_decompress_us = 1000.0;
  window.samples.push_back(snap);
  window.current_secondary_ratio = 0.3;
  HitRateGhostPolicy policy;
  const double target = policy.ComputeSecondaryRatio(window, 0.3);
  ASSERT_LT(target, 0.3);
}

class CompressionSnapshotTestContext
    : public secondary_cache_test_util::WithCacheType {
 public:
  const std::string& Type() const override {
    static const std::string kLru = kLRU;
    return kLru;
  }
};

TEST(CacheTierMemoryControllerTest, CompressionSnapshotFromAggregatedPerf) {
  if (!LZ4_Supported()) {
    ROCKSDB_GTEST_BYPASS("LZ4 required for compression snapshot test\n");
    return;
  }
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  CacheTierMemoryController controller(mock_clock.get(), opts);

  CompressedSecondaryCacheOptions sec_opts;
  sec_opts.capacity = 1 << 20;
  auto sec_cache = NewCompressedSecondaryCache(sec_opts);
  auto* csc = static_cast<CompressedSecondaryCache*>(sec_cache.get());
  auto cache = std::make_shared<CacheWithSecondaryAdapter>(
      NewLRUCache(LRUCacheOptions()), sec_cache,
      TieredAdmissionPolicy::kAdmPolicyAuto, false);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  controller.Sample(stats.get(), cache.get());

  CompressionSnapshotTestContext ctx;
  Random rnd(301);
  std::string val = rnd.RandomString(2000);
  CompressionSnapshotTestContext::TestItem item(val.data(), val.size());
  ASSERT_OK(sec_cache->Insert("key1", &item, ctx.GetHelper(), false));

  controller.Sample(stats.get(), cache.get());

  CompressedSecondaryCacheAggregatedPerf perf;
  csc->GetAggregatedPerfCounters(&perf);
  ASSERT_GT(perf.uncompressed_bytes, 0U);
  ASSERT_GT(perf.compressed_bytes, 0U);
  ASSERT_GE(perf.insert_real_count, 1U);
}

TEST(CacheTierMemoryControllerTest, SecondaryMissCountSnapshot) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  CacheTierMemoryController controller(mock_clock.get(), opts);
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  controller.Sample(stats.get(), cache.get());  // baseline only

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 50);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 10);
  SetTicker(stats.get(), SECONDARY_CACHE_HITS, 3);
  SetTicker(stats.get(), COMPRESSED_SECONDARY_CACHE_HITS, 2);
  controller.Sample(stats.get(), cache.get());

  ASSERT_EQ(1u, controller.TEST_NumSnapshots());
  ASSERT_EQ(7U, controller.TEST_SnapshotAt(0).secondary_miss_count);
}

TEST(CacheTierMemoryControllerTest, SecondaryHitRateIgnoresCompressedTicker) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  CacheTierMemoryController controller(mock_clock.get(), opts);
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  controller.Sample(stats.get(), cache.get());  // baseline only

  SetTicker(stats.get(), BLOCK_CACHE_MISS, 100);
  SetTicker(stats.get(), SECONDARY_CACHE_HITS, 15);
  SetTicker(stats.get(), COMPRESSED_SECONDARY_CACHE_HITS, 99);
  controller.Sample(stats.get(), cache.get());

  ASSERT_EQ(1u, controller.TEST_NumSnapshots());
  EXPECT_DOUBLE_EQ(0.15, controller.TEST_SnapshotAt(0).secondary_hit_rate);
  ASSERT_EQ(85U, controller.TEST_SnapshotAt(0).secondary_miss_count);
}

TEST(CacheTierMemoryControllerTest, DiskReadLatencyOnAdjust) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  auto capturing_policy = std::make_shared<CapturingPolicy>();
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  opts.warmup_time_sec = 0;
  opts.min_policy_intervals = 0;
  opts.min_cache_lookups = 0;
  opts.policy = capturing_policy;
  CacheTierMemoryController controller(mock_clock.get(), opts);
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  controller.Sample(stats.get(), cache.get());  // baseline only

  for (uint64_t us = 100; us <= 1000; us += 100) {
    stats->reportTimeToHistogram(READ_BLOCK_GET_MICROS, us);
  }

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 50);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 10);
  controller.Sample(stats.get(), cache.get());

  ASSERT_OK(controller.MaybeAdjust(cache, nullptr));
  EXPECT_GT(capturing_policy->last_window.disk_read_p50_us, 0.0);
  EXPECT_GT(capturing_policy->last_window.disk_read_p99_us, 0.0);
}

TEST(CacheTierMemoryControllerTest, ActiveSstRawBytesInSnapshot) {
  std::shared_ptr<Statistics> stats = CreateDBStatistics();
  auto mock_clock = std::make_shared<MockSystemClock>(SystemClock::Default());
  CacheTierControllerOptions opts;
  opts.enabled = true;
  opts.window_samples = 4;
  CacheTierMemoryController controller(mock_clock.get(), opts);
  auto cache = NewStackedTestCache(8 << 20, 2 << 20);

  SetTicker(stats.get(), BLOCK_CACHE_HIT, 0);
  SetTicker(stats.get(), BLOCK_CACHE_MISS, 0);
  controller.Sample(stats.get(), cache.get());  // baseline only
  controller.Sample(stats.get(), cache.get(), 12345);

  ASSERT_EQ(1u, controller.TEST_NumSnapshots());
  ASSERT_EQ(12345U, controller.TEST_SnapshotAt(0).active_sst_raw_bytes);
}

TEST(CacheTierMemoryControllerTest, UpdateCacheTierSplitStacked) {
  const size_t pri_cap = 8 << 20;
  const size_t sec_cap = 2 << 20;
  auto cache = NewStackedTestCache(pri_cap, sec_cap);
  ASSERT_OK(UpdateCacheTierSplit(cache, 0.4));
  size_t new_sec = 0;
  ASSERT_OK(cache->GetSecondaryCacheCapacity(new_sec));
  auto* adapter =
      static_cast_with_check<CacheWithSecondaryAdapter>(cache.get());
  const size_t new_pri = adapter->TEST_GetCache()->GetCapacity();
  ASSERT_EQ(new_pri + new_sec, pri_cap + sec_cap);
  ASSERT_EQ(new_sec, static_cast<size_t>((pri_cap + sec_cap) * 0.4));
}

TEST(CacheTierMemoryControllerTest, UpdateCacheTierSplitRejectsTiered) {
  TieredCacheOptions opts;
  LRUCacheOptions lru_opts;
  lru_opts.capacity = 10 << 20;
  opts.cache_opts = &lru_opts;
  opts.total_capacity = 10 << 20;
  opts.compressed_secondary_ratio = 0.2;
  auto cache = NewTieredCache(opts);
  ASSERT_TRUE(cache != nullptr);
  ASSERT_NOK(UpdateCacheTierSplit(cache, 0.3));
}

}  // namespace ROCKSDB_NAMESPACE

int main(int argc, char** argv) {
  ROCKSDB_NAMESPACE::port::InstallStackTraceHandler();
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
