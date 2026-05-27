//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "cache/cache_tier_memory_controller.h"
#include "db/db_test_util.h"
#include "rocksdb/cache.h"
#include "rocksdb/iterator.h"
#include "rocksdb/statistics.h"
#include "rocksdb/table.h"
#include "util/compression.h"

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

}  // namespace

class DBCacheTierControllerTest : public DBTestBase {
 public:
  DBCacheTierControllerTest()
      : DBTestBase("db_cache_tier_controller_test", /*env_do_fsync=*/true) {}

  Options GetTieredOptions(double initial_ratio, double target_ratio) {
    Options options = CurrentOptions();
    options.statistics = CreateDBStatistics();
    options.cache_tier_controller_options.enabled = true;
    options.cache_tier_controller_options.sample_interval_sec = 1;
    options.cache_tier_controller_options.policy_interval_sec = 1;
    options.cache_tier_controller_options.warmup_time_sec = 0;
    options.cache_tier_controller_options.min_policy_intervals = 0;
    options.cache_tier_controller_options.min_cache_lookups = 0;
    options.cache_tier_controller_options.max_ratio_delta_per_step = 0.2;
    options.cache_tier_controller_options.policy =
        std::make_shared<FixedRatioPolicy>(target_ratio);

    TieredCacheOptions tier_opts;
    LRUCacheOptions lru_opts;
    lru_opts.capacity = 16 << 20;
    tier_opts.cache_opts = &lru_opts;
    tier_opts.total_capacity = 16 << 20;
    tier_opts.compressed_secondary_ratio = initial_ratio;
    BlockBasedTableOptions table_opts;
    table_opts.block_cache = NewTieredCache(tier_opts);
    options.table_factory.reset(NewBlockBasedTableFactory(table_opts));
    initial_ratio_ = initial_ratio;
    target_ratio_ = target_ratio;
    return options;
  }

 private:
  double initial_ratio_ = 0.0;
  double target_ratio_ = 0.0;
};

TEST_F(DBCacheTierControllerTest, RegistersPeriodicTasksWithTieredCache) {
  if (!LZ4_Supported()) {
    ROCKSDB_GTEST_BYPASS("LZ4 required for tiered cache test\n");
    return;
  }
  Options options = GetTieredOptions(0.2, 0.4);
  options.env = env_;
  DestroyAndReopen(options);
  ASSERT_TRUE(dbfull()->TEST_GetPeriodicTaskScheduler().TEST_HasTask(
      PeriodicTaskType::kSampleCacheTierStats));
  ASSERT_TRUE(dbfull()->TEST_GetPeriodicTaskScheduler().TEST_HasTask(
      PeriodicTaskType::kAdjustCacheTierMemory));
  Cache* block_cache = options.table_factory->GetOptions<Cache>(
      TableFactory::kBlockCacheOpts());
  ASSERT_NE(block_cache, nullptr);
  ASSERT_STREQ(block_cache->Name(), "TieredCache");
}

TEST_F(DBCacheTierControllerTest, DisabledWithoutStatistics) {
  Options options = CurrentOptions();
  options.statistics = nullptr;
  options.cache_tier_controller_options.enabled = true;
  options.env = env_;
  DestroyAndReopen(options);
  ASSERT_FALSE(dbfull()->TEST_GetPeriodicTaskScheduler().TEST_HasTask(
      PeriodicTaskType::kSampleCacheTierStats));
}

TEST_F(DBCacheTierControllerTest, BlockCacheMissDiskReadAfterColdScan) {
  if (!LZ4_Supported()) {
    ROCKSDB_GTEST_BYPASS("LZ4 required for tiered cache test\n");
    return;
  }
  Options options = GetTieredOptions(0.2, 0.4);
  options.env = env_;
  DestroyAndReopen(options);

  ASSERT_OK(Put("foo", std::string(8000, 'x')));
  ASSERT_OK(Put("baz", std::string(8000, 'y')));
  ASSERT_OK(Flush());

  dbfull()->SampleCacheTierStats();  // baseline

  for (int pass = 0; pass < 2; ++pass) {
    std::string value;
    ASSERT_OK(dbfull()->Get(ReadOptions(), "foo", &value));
    ASSERT_OK(dbfull()->Get(ReadOptions(), "baz", &value));
  }

  dbfull()->SampleCacheTierStats();
  ASSERT_GE(dbfull()->TEST_CacheTierControllerSnapshotCount(), 1u);
  ASSERT_GT(dbfull()->TEST_CacheTierControllerLastSecondaryMissCount(), 0U);
}

TEST_F(DBCacheTierControllerTest, ActiveSstRawBytesAfterWriteFlush) {
  if (!LZ4_Supported()) {
    ROCKSDB_GTEST_BYPASS("LZ4 required for tiered cache test\n");
    return;
  }
  Options options = GetTieredOptions(0.2, 0.4);
  options.env = env_;
  DestroyAndReopen(options);

  ASSERT_OK(Put("foo", "bar"));
  ASSERT_OK(Put("baz", "qux"));
  ASSERT_OK(Flush());

  std::unique_ptr<Iterator> iter(dbfull()->NewIterator(ReadOptions()));
  for (iter->SeekToFirst(); iter->Valid(); iter->Next()) {
  }
  ASSERT_OK(iter->status());

  dbfull()->SampleCacheTierStats();
  ASSERT_GE(dbfull()->TEST_CacheTierControllerSnapshotCount(), 1u);
  ASSERT_GT(dbfull()->TEST_CacheTierControllerLastActiveSstRawBytes(), 0U);
}

}  // namespace ROCKSDB_NAMESPACE

int main(int argc, char** argv) {
  ROCKSDB_NAMESPACE::port::InstallStackTraceHandler();
  ::testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
