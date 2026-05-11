//  Copyright (c) 2011-present, Facebook, Inc.  All rights reserved.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <unordered_map>

#include "rocksdb/filter_policy.h"

namespace ROCKSDB_NAMESPACE {

class EvolveDummyFilterPolicy : public FilterPolicy {
 public:
  explicit EvolveDummyFilterPolicy(std::shared_ptr<Statistics> statistics);

  const char* Name() const override;
  static const char* kClassName();

  const char* CompatibilityName() const override;

  FilterBitsBuilder* GetBuilderWithContext(
      const FilterBuildingContext& context) const override;

  FilterBitsReader* GetFilterBitsReader(const Slice& contents) const override;

 private:
  static double ClampBitsPerKey(double bits_per_key);
  static int64_t ToMillibits(double bits_per_key);

  double ComputeBitsPerKey(const FilterBuildingContext& context) const;

  const FilterPolicy* GetOrCreateDelegate(double bits_per_key) const;

  std::shared_ptr<Statistics> statistics_;
  std::unique_ptr<const FilterPolicy> reader_delegate_;

  mutable std::mutex delegate_mu_;
  mutable std::unordered_map<int64_t, std::unique_ptr<const FilterPolicy>>
      delegate_by_millibits_;
};

}  // namespace ROCKSDB_NAMESPACE
