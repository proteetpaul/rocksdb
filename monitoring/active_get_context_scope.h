//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#pragma once

#include "rocksdb/cache.h"
#include "rocksdb/rocksdb_namespace.h"

// Thread-local active GetContext for cache-layer statistics during user
// Get/MultiGet. Primary block-cache tickers gate on get_context passed through
// the table reader; secondary hits are recorded inside the generic cache API
// (Promote, CompressedSecondaryCache::Lookup) which does not receive
// GetContext*, so we set ActiveGetContextScope at the lookup boundary instead.

namespace ROCKSDB_NAMESPACE {

class GetContext;
class Statistics;

// Active GetContext during user Get/MultiGet, or nullptr on other paths.
GetContext* ActiveGetContextForCacheStats();

// RAII: set thread-local active GetContext for cache-layer statistics.
class ActiveGetContextScope {
 public:
  explicit ActiveGetContextScope(GetContext* get_context);
  ~ActiveGetContextScope();

  ActiveGetContextScope(const ActiveGetContextScope&) = delete;
  ActiveGetContextScope& operator=(const ActiveGetContextScope&) = delete;

 private:
  GetContext* previous_;
};

// Count one secondary hit for user Get/MultiGet only. Aggregate
// SECONDARY_CACHE_HITS is batched via GetContext::ReportCounters; per-role
// SECONDARY_CACHE_{FILTER,INDEX,DATA}_HITS are RecordTick'd immediately.
// CacheTierMemoryController uses SECONDARY_CACHE_HITS only; do not sum with
// COMPRESSED_SECONDARY_CACHE_HITS.
void RecordSecondaryCacheHitForUserGet(Statistics* stats, CacheEntryRole role);

// Batch compressed-secondary dummy hit to active GetContext; no-op otherwise.
void RecordCompressedSecondaryDummyHitForUserGet(Statistics* stats);

}  // namespace ROCKSDB_NAMESPACE
