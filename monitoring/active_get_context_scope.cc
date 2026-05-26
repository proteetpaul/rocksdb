//  Copyright (c) Meta Platforms, Inc. and affiliates.
//  This source code is licensed under both the GPLv2 (found in the
//  COPYING file in the root directory) and Apache 2.0 License
//  (found in the LICENSE.Apache file in the root directory).

#include "monitoring/active_get_context_scope.h"

#include "monitoring/perf_context_imp.h"
#include "monitoring/statistics_impl.h"
#include "table/get_context.h"

namespace ROCKSDB_NAMESPACE {

thread_local GetContext* tls_active_get_context_for_cache_stats = nullptr;

GetContext* ActiveGetContextForCacheStats() {
  return tls_active_get_context_for_cache_stats;
}

ActiveGetContextScope::ActiveGetContextScope(GetContext* get_context)
    : previous_(tls_active_get_context_for_cache_stats) {
  tls_active_get_context_for_cache_stats = get_context;
}

ActiveGetContextScope::~ActiveGetContextScope() {
  tls_active_get_context_for_cache_stats = previous_;
}

void RecordSecondaryCacheHitForUserGet(Statistics* stats,
                                       CacheEntryRole role) {
  GetContext* ctx = ActiveGetContextForCacheStats();
  if (ctx == nullptr) {
    return;
  }
  ++ctx->get_context_stats_.num_secondary_cache_hits;
  if (stats != nullptr) {
    switch (role) {
      case CacheEntryRole::kFilterBlock:
        RecordTick(stats, SECONDARY_CACHE_FILTER_HITS);
        break;
      case CacheEntryRole::kIndexBlock:
        RecordTick(stats, SECONDARY_CACHE_INDEX_HITS);
        break;
      case CacheEntryRole::kDataBlock:
        RecordTick(stats, SECONDARY_CACHE_DATA_HITS);
        break;
      default:
        break;
    }
  }
  PERF_COUNTER_ADD(secondary_cache_hit_count, 1);
}

void RecordCompressedSecondaryDummyHitForUserGet(Statistics* /*stats*/) {
  GetContext* ctx = ActiveGetContextForCacheStats();
  if (ctx == nullptr) {
    return;
  }
  ++ctx->get_context_stats_.num_compressed_secondary_dummy_hits;
}

}  // namespace ROCKSDB_NAMESPACE
