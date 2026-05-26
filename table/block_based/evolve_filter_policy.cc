//  Copyright (c) 2011-present, Facebook, Inc.  All rights reserved.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "table/block_based/evolve_filter_policy.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <memory>
#include <mutex>
#include <utility>

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
    const FilterBuildingContext& /*context*/) const {
  double stats_based_bits = 9.9;

  if (statistics_ != nullptr) {
    const double useful =
        static_cast<double>(statistics_->getTickerCount(BLOOM_FILTER_USEFUL));
    const double positive = static_cast<double>(
        statistics_->getTickerCount(BLOOM_FILTER_FULL_POSITIVE));
    const double true_positive = static_cast<double>(
        statistics_->getTickerCount(BLOOM_FILTER_FULL_TRUE_POSITIVE));

    const double false_positive = std::max(0.0, positive - true_positive);
    const double negative_queries = useful + false_positive;
    if (negative_queries > 0.0) {
      // Estimate bits/key from observed full-filter FP rate:
      // fp ~= exp(-bits * ln(2)^2) => bits ~= -ln(fp)/ln(2)^2.
      const double observed_fp = false_positive / negative_queries;
      const double bounded_fp = std::min(0.99, std::max(1e-9, observed_fp));
      const double ln2 = std::log(2.0);
      stats_based_bits = -std::log(bounded_fp) / (ln2 * ln2);
    }
  }

  return stats_based_bits;
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
