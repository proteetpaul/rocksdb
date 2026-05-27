//  Copyright (c) 2011-present, Facebook, Inc.  All rights reserved.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "table/block_based/evolve_filter_policy.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <mutex>
#include <utility>
#include <vector>

#include "rocksdb/statistics.h"

namespace {

// Delegate cache keys must match BloomLikeFilterPolicy's ctor logic in
// filter_policy.cc so equivalent raw bits-per-key values share one delegate.
double SanitizeBloomBitsPerKey(double bits_per_key) {
  if (bits_per_key < 0.5) {
    return 0;
  } else if (bits_per_key < 1.0) {
    return 1.0;
  } else if (!(bits_per_key < 100.0)) {
    return 100.0;
  }
  return bits_per_key;
}

int64_t BloomMillibitsCacheKey(double bits_per_key) {
  const double sanitized = SanitizeBloomBitsPerKey(bits_per_key);
  return static_cast<int64_t>(static_cast<int>(
      sanitized * 1000.0 + 0.500001));
}

constexpr int kFallbackNumLevels = 7;
constexpr uint64_t kFallbackNumKeys = 516666666;
constexpr uint64_t kFallbackKeySize = 16;
constexpr uint64_t kFallbackValueSize = 64;
constexpr uint64_t kFallbackWriteBufferSize = 62839682;
constexpr uint64_t kFallbackLevel0FileNumCompactionTrigger = 5;
constexpr uint64_t kFallbackMaxBytesForLevelBase = 227208189;
constexpr double kFallbackMaxBytesForLevelMultiplier = 11.914889790996936;
constexpr double kFilterMemoryBudgetMb = 400.0;
constexpr double kBytesPerMb = 1024.0 * 1024.0;
constexpr double kBitsPerByte = 8.0;
constexpr double kEmptyLevelBitsPerKey = 16.0;

double Ln2Squared() {
  const double ln2 = std::log(2.0);
  return ln2 * ln2;
}

std::vector<uint64_t> EstimateFallbackLevelKeys(int num_levels) {
  const int level_count = std::max(1, num_levels);
  const uint64_t bytes_per_entry = kFallbackKeySize + kFallbackValueSize;
  std::vector<uint64_t> capacities(static_cast<size_t>(level_count), 0);

  capacities[0] =
      std::max<uint64_t>(1, kFallbackWriteBufferSize *
                                kFallbackLevel0FileNumCompactionTrigger /
                                bytes_per_entry);

  double level_bytes = static_cast<double>(kFallbackMaxBytesForLevelBase);
  for (int level = 1; level < level_count; ++level) {
    capacities[static_cast<size_t>(level)] =
        std::max<uint64_t>(1, static_cast<uint64_t>(
                                  level_bytes / bytes_per_entry));
    level_bytes *= kFallbackMaxBytesForLevelMultiplier;
  }

  uint64_t remaining_keys = kFallbackNumKeys;
  std::vector<uint64_t> level_keys(static_cast<size_t>(level_count), 0);
  for (int level = level_count - 1; level >= 0; --level) {
    const uint64_t keys =
        std::min(capacities[static_cast<size_t>(level)], remaining_keys);
    level_keys[static_cast<size_t>(level)] = keys;
    remaining_keys -= keys;
    if (remaining_keys == 0) {
      break;
    }
  }

  if (remaining_keys > 0) {
    level_keys.back() += remaining_keys;
  }

  return level_keys;
}

std::vector<uint64_t> EstimateLevelKeys(
    const ROCKSDB_NAMESPACE::FilterBuildingContext& context) {
  const uint64_t bytes_per_entry = kFallbackKeySize + kFallbackValueSize;
  const int context_num_levels =
      context.num_levels > 0 ? context.num_levels : kFallbackNumLevels;
  const int level_count = std::max<int>(
      1, std::max<int>(context_num_levels,
                       static_cast<int>(context.metrics.level_bytes.size())));

  if (!context.metrics.valid || context.metrics.level_bytes.empty()) {
    return EstimateFallbackLevelKeys(level_count);
  }

  std::vector<uint64_t> level_keys(static_cast<size_t>(level_count), 0);
  uint64_t estimated_keys_from_bytes = 0;
  for (size_t i = 0; i < context.metrics.level_bytes.size(); ++i) {
    const uint64_t keys = context.metrics.level_bytes[i] / bytes_per_entry;
    level_keys[i] = keys;
    estimated_keys_from_bytes += keys;
  }

  if (estimated_keys_from_bytes == 0) {
    return EstimateFallbackLevelKeys(level_count);
  }

  if (context.metrics.estimated_total_keys > 0) {
    const double scale = static_cast<double>(context.metrics.estimated_total_keys) /
                         static_cast<double>(estimated_keys_from_bytes);
    for (uint64_t& keys : level_keys) {
      if (keys > 0) {
        keys = std::max<uint64_t>(
            1, static_cast<uint64_t>(static_cast<double>(keys) * scale));
      }
    }
  }

  return level_keys;
}

double TotalFilterBits(const std::vector<uint64_t>& level_keys,
                       double lambda_value) {
  const double ln2_squared = Ln2Squared();
  double total_bits = 0.0;
  for (uint64_t keys : level_keys) {
    if (keys == 0) {
      continue;
    }
    const double fpr = std::min(
        1.0, std::max(std::numeric_limits<double>::min(),
                      lambda_value * static_cast<double>(keys)));
    total_bits += static_cast<double>(keys) * (-std::log(fpr) / ln2_squared);
  }
  return total_bits;
}

std::vector<double> OptimizeMonkeyBitsPerKey(
    const std::vector<uint64_t>& level_keys) {
  std::vector<double> bits_per_key(level_keys.size(), 0.0);
  uint64_t max_level_keys = 0;
  for (uint64_t keys : level_keys) {
    max_level_keys = std::max(max_level_keys, keys);
  }
  if (max_level_keys == 0) {
    return bits_per_key;
  }

  const double budget_bits =
      kFilterMemoryBudgetMb * kBytesPerMb * kBitsPerByte;
  double low = std::numeric_limits<double>::min();
  double high = 1.0 / static_cast<double>(max_level_keys);
  for (int i = 0; i < 200; ++i) {
    const double mid = (low + high) / 2.0;
    if (TotalFilterBits(level_keys, mid) > budget_bits) {
      low = mid;
    } else {
      high = mid;
    }
  }

  const double ln2_squared = Ln2Squared();
  for (size_t i = 0; i < level_keys.size(); ++i) {
    if (level_keys[i] == 0) {
      continue;
    }
    const double fpr = std::min(
        1.0, std::max(std::numeric_limits<double>::min(),
                      high * static_cast<double>(level_keys[i])));
    bits_per_key[i] = -std::log(fpr) / ln2_squared;
  }

  return bits_per_key;
}

}  // namespace

namespace ROCKSDB_NAMESPACE {

EvolveDummyFilterPolicy::EvolveDummyFilterPolicy(
    std::shared_ptr<Statistics> statistics)
    : statistics_(std::move(statistics)),
      reader_delegate_(NewBloomFilterPolicy(/*bits_per_key=*/10.0)) {}

const char* EvolveDummyFilterPolicy::Name() const { return kClassName(); }

const char* EvolveDummyFilterPolicy::kClassName() {
  return "rocksdb.EvolveDummyFilter";
}

const char* EvolveDummyFilterPolicy::CompatibilityName() const {
  return reader_delegate_->CompatibilityName();
}

FilterBitsBuilder* EvolveDummyFilterPolicy::GetBuilderWithContext(
    const FilterBuildingContext& context) const {
  const auto start = std::chrono::steady_clock::now();
  const double dynamic_bpk = ComputeBitsPerKey(context);
  if (statistics_ != nullptr) {
    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - start);
    statistics_->reportTimeToHistogram(
        COMPUTE_BITS_PER_KEY_MICROS,
        static_cast<uint64_t>(std::max<int64_t>(0, elapsed.count())));
  }
  const FilterPolicy* delegate = GetOrCreateDelegate(dynamic_bpk);
  return delegate->GetBuilderWithContext(context);
}

FilterBitsReader* EvolveDummyFilterPolicy::GetFilterBitsReader(
    const Slice& contents) const {
  return reader_delegate_->GetFilterBitsReader(contents);
}

// EVOLVE-BLOCK-START
double EvolveDummyFilterPolicy::ComputeBitsPerKey(
    const FilterBuildingContext& context) const {
  if (context.level_at_creation < 0) {
    return 9.9;
  }

  const std::vector<uint64_t> level_keys = EstimateLevelKeys(context);
  const size_t level = static_cast<size_t>(context.level_at_creation);
  if (level >= level_keys.size()) {
    return 9.9;
  }
  if (level_keys[level] == 0) {
    return kEmptyLevelBitsPerKey;
  }

  const std::vector<double> bits_per_key = OptimizeMonkeyBitsPerKey(level_keys);
  return bits_per_key[level];
}
// EVOLVE-BLOCK-END

const FilterPolicy* EvolveDummyFilterPolicy::GetOrCreateDelegate(
    double bits_per_key) const {
  const int64_t millibits = BloomMillibitsCacheKey(bits_per_key);

  std::lock_guard<std::mutex> lock(delegate_mu_);
  auto it = delegate_by_millibits_.find(millibits);
  if (it != delegate_by_millibits_.end()) {
    return it->second.get();
  }

  std::unique_ptr<const FilterPolicy> created(NewBloomFilterPolicy(bits_per_key));
  const FilterPolicy* raw = created.get();
  delegate_by_millibits_.emplace(millibits, std::move(created));
  return raw;
}

const FilterPolicy* NewEvolveDummyFilterPolicy(
    std::shared_ptr<Statistics> statistics) {
  return new EvolveDummyFilterPolicy(std::move(statistics));
}

}  // namespace ROCKSDB_NAMESPACE
