//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#pragma once

#include <cstdint>
#include <deque>
#include <memory>

#include "rocksdb/cache.h"
#include "rocksdb/rocksdb_namespace.h"
#include "rocksdb/status.h"
#include "rocksdb/system_clock.h"

namespace ROCKSDB_NAMESPACE {

class CompressedSecondaryCache;
class Logger;
class Statistics;

CompressedSecondaryCache* GetCompressedSecondaryCache(const Cache* cache);

// Returns true if cache is TieredCache or has a compressed secondary tier.
bool CacheSupportsTierMemoryControl(const Cache* cache);

// Current secondary_ratio in [0, 1], or false if unsupported.
bool GetCacheTierSecondaryRatio(const Cache* cache, double* ratio);

// Apply ratio via UpdateCacheReservationRatio (TieredCache) or
// UpdateCacheTierSplit (stacked cache).
Status ApplyCacheTierSecondaryRatio(const std::shared_ptr<Cache>& cache,
                                    double secondary_ratio);

class CacheTierMemoryController {
 public:
  CacheTierMemoryController(SystemClock* clock,
                            const CacheTierControllerOptions& options);

  // Record one sample_interval_sec snapshot. No-op if disabled.
  // active_sst_raw_bytes: accumulated raw key+value on disk (see evolve-cache/thoughts.md).
  void Sample(Statistics* statistics, const Cache* cache,
              uint64_t active_sst_raw_bytes = 0);

#ifndef NDEBUG
  size_t TEST_NumSnapshots() const { return ring_buffer_.size(); }
  const CacheTierMemorySnapshot& TEST_SnapshotAt(size_t i) const {
    return ring_buffer_[i];
  }
#endif

  // Run policy and apply ratio if warmup/cooldown allow. Returns status of
  // apply (OK if skipped).
  Status MaybeAdjust(const std::shared_ptr<Cache>& cache, Logger* info_log);

  bool enabled() const { return enabled_; }

 private:
  struct TickerBaseline {
    uint64_t block_cache_hit = 0;
    uint64_t block_cache_miss = 0;
    uint64_t secondary_cache_hits = 0;
    uint64_t dummy_hits = 0;
    bool initialized = false;
  };

  struct SecondaryCacheBaseline {
    uint64_t uncompressed_bytes = 0;
    uint64_t compressed_bytes = 0;
    uint64_t insert_real_count = 0;
    uint64_t insert_placeholder_count = 0;
    uint64_t decompress_nanos = 0;
    uint64_t decompress_count = 0;
    bool initialized = false;
  };

  bool IsWarmedUp(const CacheTierMemoryWindow& window) const;
  CacheTierMemoryWindow BuildWindow(const Cache* cache) const;
  void UpdateBaseline(Statistics* statistics,
                      const CompressedSecondaryCache* sec_cache);
  void PopulateDiskReadLatency(CacheTierMemoryWindow* window) const;

  SystemClock* clock_;
  CacheTierControllerOptions options_;
  std::shared_ptr<CacheTierMemoryPolicy> policy_;
  bool enabled_;
  bool permanently_disabled_;
  uint64_t start_time_us_;
  uint32_t policy_module_invocations;
  size_t max_window_samples_;
  Statistics* statistics_;
  TickerBaseline baseline_;
  SecondaryCacheBaseline sec_cache_baseline_;
  std::deque<CacheTierMemorySnapshot> ring_buffer_;
};

}  // namespace ROCKSDB_NAMESPACE
