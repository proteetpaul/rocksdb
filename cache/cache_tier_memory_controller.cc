//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "cache/cache_tier_memory_controller.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

#include "cache/compressed_secondary_cache.h"
#include "cache/hit_rate_ghost_policy.h"
#include "cache/secondary_cache_adapter.h"
#include "logging/logging.h"
#include "rocksdb/statistics.h"
#include "util/cast_util.h"

namespace ROCKSDB_NAMESPACE {

// CacheTierMemoryController is not thread-safe. In production, Sample() and
// MaybeAdjust() are called only from DBImpl periodic tasks, which run on the
// global single-threaded Timer (PeriodicTaskScheduler). That serializes all
// periodic callbacks, so Sample and MaybeAdjust never run concurrently.

namespace {

constexpr double kRatioEpsilon = 0.01;
// Must stay > 0: UpdateTieredCache cannot re-enable secondary after ratio 0.
constexpr double kMinSecondaryRatio = 0.05;
constexpr double kMaxSecondaryRatio = 0.80;

double SafeRate(uint64_t num, uint64_t denom) {
  if (denom == 0) {
    return 0.0;
  }
  return static_cast<double>(num) / static_cast<double>(denom);
}

uint64_t Delta(uint64_t current, uint64_t previous) {
  return current >= previous ? current - previous : 0;
}

}  // namespace

CompressedSecondaryCache* GetCompressedSecondaryCache(const Cache* cache) {
  if (cache == nullptr) {
    return nullptr;
  }
  auto* adapter = static_cast_with_check<CacheWithSecondaryAdapter>(
      const_cast<Cache*>(cache));
  if (adapter == nullptr) {
    return nullptr;
  }
  return static_cast_with_check<CompressedSecondaryCache>(
      adapter->TEST_GetSecondaryCache());
}

bool CacheSupportsTierMemoryControl(const Cache* cache) {
  if (cache == nullptr) {
    return false;
  }
  if (strcmp(cache->Name(), "TieredCache") == 0) {
    return true;
  }
  size_t sec_capacity = 0;
  return cache->GetSecondaryCacheCapacity(sec_capacity).ok();
}

bool GetCacheTierSecondaryRatio(const Cache* cache, double* ratio) {
  if (cache == nullptr || ratio == nullptr) {
    return false;
  }
  size_t sec_capacity = 0;
  Status s = cache->GetSecondaryCacheCapacity(sec_capacity);
  if (!s.ok()) {
    return false;
  }
  const size_t pri_capacity = cache->GetCapacity();
  const size_t total_capacity =
      strcmp(cache->Name(), "TieredCache") == 0
          ? pri_capacity
          : pri_capacity + sec_capacity;
  if (total_capacity == 0) {
    *ratio = 0.0;
    return true;
  }
  *ratio =
      static_cast<double>(sec_capacity) / static_cast<double>(total_capacity);
  return true;
}

Status ApplyCacheTierSecondaryRatio(const std::shared_ptr<Cache>& cache,
                                    double secondary_ratio) {
  if (cache == nullptr) {
    return Status::InvalidArgument("cache is null");
  }
  if (secondary_ratio < 0.0 || secondary_ratio > 1.0) {
    return Status::InvalidArgument("secondary_ratio out of range");
  }
  if (strcmp(cache->Name(), "TieredCache") == 0) {
    auto* adapter =
        static_cast_with_check<CacheWithSecondaryAdapter>(cache.get());
    if (adapter == nullptr) {
      return Status::NotSupported("cache is not CacheWithSecondaryAdapter");
    }
    // Ratio-only update: do not call UpdateTieredCache, which may also change
    // admission policy when adm_policy is passed explicitly.
    return adapter->UpdateCacheReservationRatio(secondary_ratio);
  }
  return UpdateCacheTierSplit(cache, secondary_ratio, -1);
}

CacheTierMemoryController::CacheTierMemoryController(
    SystemClock* clock, const CacheTierControllerOptions& options)
    : clock_(clock),
      options_(options),
      policy_(options.policy ? options.policy
                            : std::make_shared<HitRateGhostPolicy>()),
      enabled_(options.enabled),
      permanently_disabled_(false),
      start_time_us_(clock_->NowMicros()),
      policy_module_invocations(0),
      max_window_samples_(options.window_samples > 0
                              ? options.window_samples
                              : std::max(
                                    uint64_t{1},
                                    options.policy_interval_sec /
                                        std::max(options.sample_interval_sec,
                                                 uint64_t{1}))),
      statistics_(nullptr) {}

void CacheTierMemoryController::UpdateBaseline(
    Statistics* statistics, const CompressedSecondaryCache* sec_cache) {
  if (statistics == nullptr) {
    return;
  }
  // Cumulative DB Statistics used to derive per-interval hit rates and
  // ghost/admission signals (deltas vs. previous Sample() call).
  baseline_.block_cache_hit = statistics->getTickerCount(Tickers::BLOCK_CACHE_HIT);
  baseline_.block_cache_miss = statistics->getTickerCount(Tickers::BLOCK_CACHE_MISS);
  baseline_.secondary_cache_hits = statistics->getTickerCount(Tickers::SECONDARY_CACHE_HITS);
  baseline_.dummy_hits = statistics->getTickerCount(Tickers::COMPRESSED_SECONDARY_CACHE_DUMMY_HITS);
  baseline_.initialized = true;

  // Cumulative compressed-secondary perf counters (PerfContext-aligned,
  // aggregated on CompressedSecondaryCache; not global SST decompress stats).
  if (sec_cache != nullptr) {
    CompressedSecondaryCacheAggregatedPerf perf;
    sec_cache->GetAggregatedPerfCounters(&perf);
    sec_cache_baseline_.uncompressed_bytes = perf.uncompressed_bytes;
    sec_cache_baseline_.compressed_bytes = perf.compressed_bytes;
    sec_cache_baseline_.insert_real_count = perf.insert_real_count;
    sec_cache_baseline_.insert_placeholder_count = perf.insert_placeholder_count;
    sec_cache_baseline_.decompress_nanos = perf.decompress_nanos;
    sec_cache_baseline_.decompress_count = perf.decompress_count;
    sec_cache_baseline_.initialized = true;
  }
}

void CacheTierMemoryController::PopulateDiskReadLatency(
    CacheTierMemoryWindow* window) const {
  if (window == nullptr || statistics_ == nullptr ||
      !statistics_->HistEnabledForType(Histograms::READ_BLOCK_GET_MICROS)) {
    return;
  }
  // Cumulative READ_BLOCK_GET_MICROS proxy for block-cache-miss SST read cost.
  // Not secondary-specific; includes non-secondary-compatible blocks and full
  // SST fetch (I/O + on-read decompress), not raw disk latency alone.
  HistogramData data;
  statistics_->histogramData(Histograms::READ_BLOCK_GET_MICROS, &data);
  window->disk_read_p50_us = data.median;
  window->disk_read_p99_us = data.percentile99;
  window->disk_read_average_us = data.average;
}

// Append one sample_interval_sec snapshot: per-interval deltas from cumulative
// Statistics tickers and CompressedSecondaryCache aggregated perf counters.
void CacheTierMemoryController::Sample(Statistics* statistics, const Cache* cache,
                                     uint64_t active_sst_raw_bytes) {
  if (!enabled_ || permanently_disabled_) {
    return;
  }
  if (statistics == nullptr) {
    permanently_disabled_ = true;
    return;
  }
  statistics_ = statistics;

  CompressedSecondaryCache* sec_cache = GetCompressedSecondaryCache(cache);

  if (!baseline_.initialized) {
    UpdateBaseline(statistics, sec_cache);
    return;
  }

  // BLOCK_CACHE_HIT counts primary and secondary successes; BLOCK_CACHE_MISS
  // counts disk reads only (both tiers missed). SECONDARY_CACHE_HITS is a
  // subset of BLOCK_CACHE_HIT on user Get/MultiGet paths.
  const uint64_t cur_hit = statistics->getTickerCount(Tickers::BLOCK_CACHE_HIT);
  const uint64_t cur_miss = statistics->getTickerCount(Tickers::BLOCK_CACHE_MISS);

  // --- Secondary tier hits (non-compressed + compressed secondary cache) ---
  const uint64_t cur_sec = statistics->getTickerCount(Tickers::SECONDARY_CACHE_HITS);

  // --- Ghost / admission (tiered admission on compressed secondary) ---
  const uint64_t cur_dummy = statistics->getTickerCount(Tickers::COMPRESSED_SECONDARY_CACHE_DUMMY_HITS);

  const uint64_t d_hit = Delta(cur_hit, baseline_.block_cache_hit);
  const uint64_t d_miss = Delta(cur_miss, baseline_.block_cache_miss);
  const uint64_t d_sec = Delta(cur_sec, baseline_.secondary_cache_hits);
  const uint64_t d_dummy = Delta(cur_dummy, baseline_.dummy_hits);

  CacheTierMemorySnapshot snap;
  snap.interval_us = clock_->NowMicros();

  // Derived hit rates over this interval.
  const uint64_t d_primary_hit = d_hit >= d_sec ? d_hit - d_sec : 0;
  const uint64_t lookups = d_hit + d_miss;
  snap.primary_lookups = lookups;
  snap.primary_hit_rate = SafeRate(d_primary_hit, lookups);
  const uint64_t primary_misses = d_sec + d_miss;
  snap.secondary_hit_rate = SafeRate(d_sec, primary_misses);
  snap.secondary_miss_count = d_miss;

  // Raw ghost/admission deltas for policy heuristics.
  snap.dummy_hits = d_dummy;

  // Partial logical SST bytes (accumulated raw key+value); not EstimateLiveDataSize.
  snap.active_sst_raw_bytes = active_sst_raw_bytes;

  // --- Compressed-secondary perf (insert/compress/decompress on DRAM tier) ---
  if (sec_cache != nullptr && sec_cache_baseline_.initialized) {
    CompressedSecondaryCacheAggregatedPerf perf;
    sec_cache->GetAggregatedPerfCounters(&perf);
    snap.sec_cache_uncompressed_bytes =
        Delta(perf.uncompressed_bytes, sec_cache_baseline_.uncompressed_bytes);
    snap.sec_cache_compressed_bytes =
        Delta(perf.compressed_bytes, sec_cache_baseline_.compressed_bytes);
    snap.sec_cache_insert_real =
        Delta(perf.insert_real_count, sec_cache_baseline_.insert_real_count);
    snap.sec_cache_insert_placeholder = Delta(
        perf.insert_placeholder_count, sec_cache_baseline_.insert_placeholder_count);
    snap.sec_cache_decompress_nanos =
        Delta(perf.decompress_nanos, sec_cache_baseline_.decompress_nanos);
    // compressed / uncompressed byte ratio when compression ran this interval.
    if (snap.sec_cache_uncompressed_bytes > 0) {
      snap.compression_ratio = static_cast<double>(snap.sec_cache_compressed_bytes) /
                               static_cast<double>(snap.sec_cache_uncompressed_bytes);
    }
    const uint64_t d_decompress_count =
        Delta(perf.decompress_count, sec_cache_baseline_.decompress_count);
    if (d_decompress_count > 0) {
      snap.mean_decompress_us =
          static_cast<double>(snap.sec_cache_decompress_nanos) /
          static_cast<double>(d_decompress_count) / 1000.0;
    }
  }

  ring_buffer_.push_back(snap);
  while (ring_buffer_.size() > max_window_samples_) {
    ring_buffer_.pop_front();
  }

  UpdateBaseline(statistics, sec_cache);
}

CacheTierMemoryWindow CacheTierMemoryController::BuildWindow(
    const Cache* cache) const {
  CacheTierMemoryWindow window;
  window.samples.assign(ring_buffer_.begin(), ring_buffer_.end());
  double ratio = 0.0;
  if (GetCacheTierSecondaryRatio(cache, &ratio)) {
    window.current_secondary_ratio = ratio;
  }
  return window;
}

bool CacheTierMemoryController::IsWarmedUp(
    const CacheTierMemoryWindow& /*window*/) const {
  const uint64_t now_us = clock_->NowMicros();
  if (now_us - start_time_us_ <
      options_.warmup_time_sec * 1000000U) {
    return false;
  }
  if (policy_module_invocations < options_.min_policy_intervals) {
    return false;
  }
  uint64_t total_lookups = 0;
  for (const auto& snap : ring_buffer_) {
    total_lookups += snap.primary_lookups;
  }
  return total_lookups >= options_.min_cache_lookups;
}

Status CacheTierMemoryController::MaybeAdjust(const std::shared_ptr<Cache>& cache,
                                              Logger* info_log) {
  if (!enabled_ || permanently_disabled_ || cache == nullptr) {
    return Status::OK();
  }
  if (!CacheSupportsTierMemoryControl(cache.get())) {
    return Status::OK();
  }

  ++policy_module_invocations;

  CacheTierMemoryWindow window = BuildWindow(cache.get());
  if (!IsWarmedUp(window)) {
    return Status::OK();
  }

  if (options_.tuning_duration_sec > 0) {
    const uint64_t now_us = clock_->NowMicros();
    if (now_us - start_time_us_ >= options_.tuning_duration_sec * 1000000U) {
      return Status::OK();
    }
  }

  PopulateDiskReadLatency(&window);

  double current_ratio = window.current_secondary_ratio;
  double target_ratio = policy_->ComputeSecondaryRatio(window, current_ratio);

  target_ratio = std::max(kMinSecondaryRatio, target_ratio);
  target_ratio = std::min(kMaxSecondaryRatio, target_ratio);

  const double delta = target_ratio - current_ratio;
  if (std::fabs(delta) <= kRatioEpsilon) {
    return Status::OK();
  }
  const double clamped_delta = std::max(-options_.max_ratio_delta_per_step,
    std::min(options_.max_ratio_delta_per_step, delta));
  target_ratio = current_ratio + clamped_delta;
  target_ratio = std::max(kMinSecondaryRatio, target_ratio);
  target_ratio = std::min(kMaxSecondaryRatio, target_ratio);

  if (std::fabs(target_ratio - current_ratio) <= kRatioEpsilon) {
    return Status::OK();
  }

  Status s = ApplyCacheTierSecondaryRatio(cache, target_ratio);
  if (s.ok()) {
    ROCKS_LOG_INFO(info_log,
                   "Cache tier controller: adjusted secondary ratio from "
                   "%.2f to %.2f",
                   current_ratio, target_ratio);
  } else {
    ROCKS_LOG_WARN(info_log,
                   "Cache tier controller: failed to adjust secondary ratio "
                   "from %.2f to %.2f: %s",
                   current_ratio, target_ratio, s.ToString().c_str());
  }
  return s;
}

}  // namespace ROCKSDB_NAMESPACE
