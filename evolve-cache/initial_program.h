#pragma once

#include "rocksdb/cache.h"

namespace ROCKSDB_NAMESPACE {

// EVOLVE-BLOCK-START
class HitRateGhostPolicy : public CacheTierMemoryPolicy {
 public:
  double ComputeSecondaryRatio(const CacheTierMemoryWindow& window,
                               double current_ratio) override {
    if (window.samples.empty()) {
      return current_ratio;
    }
    const CacheTierMemorySnapshot& last = window.samples.back();
    double target = current_ratio;

    const bool good_secondary_use =
        last.secondary_hit_rate > kSecondaryHitRateThreshold &&
        last.compression_ratio > 0.0 &&
        last.compression_ratio < kGoodCompressionRatio &&
        last.mean_decompress_us < kHighDecompressUs;
    const bool expensive_decompress =
        last.mean_decompress_us > kHighDecompressUs;
    const bool poor_compression = last.compression_ratio > 0.0 &&
                                  last.compression_ratio >=
                                      kGoodCompressionRatio;

    if (good_secondary_use) {
      target += kRatioStep;
    } else if (expensive_decompress || poor_compression) {
      target -= kRatioStep;
    }
    return target;
  }

 private:
  static constexpr double kGoodCompressionRatio = 0.5;
  static constexpr double kHighDecompressUs = 500.0;
  static constexpr double kSecondaryHitRateThreshold = 0.1;
  static constexpr double kRatioStep = 0.05;
};
// EVOLVE-BLOCK-END

}  // namespace ROCKSDB_NAMESPACE
