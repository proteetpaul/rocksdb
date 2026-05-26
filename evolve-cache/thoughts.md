# OpenEvolve + Two-level caching:

### Types of policies/heuristics present in Rocksdb
- Priority of different entries (high/low/bottom), fraction of cache that is dedicated to high/low priority blocks (matters only for lru)
- Eviction policy (lru/clock) within a single tier
- Promotion/demotion policies (TieredAdmissionPolicy): Auto, Placeholder, AllowCacheHits, ThreeQueue, AllowAll
- Accounting: whether cache metadata (handles, indexing) counts toward capacity (kDontChargeCacheMetadata vs kFullChargeCacheMetadata)
- Compression: Compression algorithm to use for the lower tier, selectively compress certain entries
- Misc.: whether l0 filter and index blocks should be pinned to the cache

## What signals to consider?
- Maintain set of most frequently accessed blocks using approximation algorithms? Could use the hash table that is present in the LRU mechanism to store lightweight access history
- Leverage the ghost/dummy cache that stores keys and placeholders. Note that the CacheReservationManagerImpl also has a dummy cache, but it is used to account for entries outside of the block cache (e.g.: write buffer, filter construction, etc.)
- Types of queries, e.g. range queries with low selectivity could lead to lots of cache thrashing. However, rocksdb allows users to specify that the cache should not be filled with blocks read during next/prev. Note that compaction caused by update-heavy workloads doesn't directly cause cache thrashing, because compaction doesn't insert any new entries into the cache. However, it could lead to reduced sstable lifetimes, which can lead to cache thrashing.
- Working set size/space amplification
- Expected lifetime of sstable to which the block belongs: We can record this at a per-level granularity. Explore if we can track this at a finer granularity to capture distributions inside an sstable
- Compression factor: BYTES_DECOMPRESSED_FROM, BYTES_DECOMPRESSED_TO, NUMBER_BLOCK_DECOMPRESSED. Note that only data and optionally index blocks are compressed
- Compression latency: DECOMPRESSION_TIMES_NANOS (histogram)
- Cache miss costs: using statistics
- Cache hit rate: existing stats
- Cache size: Should be available

### Block read latency histograms (`read.block.get.micros` vs `file.read.get.micros`)
- **What evolve-cache uses:** `rocksdb.read.block.get.micros` (`READ_BLOCK_GET_MICROS`), parsed in `evaluate/parse_stats.py` as a proxy for block-cache-miss read cost during sizing/evaluation.
- **`read.block.get.micros` (table / block layer):** Wall-clock time (`StopWatch` + `SystemClock::NowMicros`) for **one SST block fetch on a block-cache miss** during a point lookup (`Get` / `MultiGet`, `for_compaction=false`). Recorded in `BlockBasedTable::MaybeReadBlockAndLoadToCache` / `RetrieveBlock` when the block is not in block cache and I/O is allowed.
  - **Included:** persistent block cache lookup, prefetch-buffer copy, disk read (nested inside `BlockFetcher`), checksum/trailer verify, decompression, buffer copies (direct I/O alignment, etc.), and (on the `fill_cache=true` path) inserting the block into block cache (`PutDataBlockToCache`).
  - **Not included:** block-cache **hit** lookup (happens before the timer), memtable-only reads, compaction reads (`READ_BLOCK_COMPACTION_MICROS` instead).
  - **One sample per block read** (data, index, or filter block can each contribute a sample during a single `Get`).
  - **Enablement:** `--statistics` and `stats_level > kExceptTimers` (db_bench default **3** is sufficient).
- **`file.read.get.micros` (file layer):** Wall-clock time for **`RandomAccessFileReader::Read` / `MultiRead` only**, tagged with `IOOptions.io_activity == kGet` (set automatically inside `DB::Get`). Does **not** include decompression or block-cache insert; nested inside `read.block` when disk is actually touched.
  - **Enablement stricter:** `stats_level > kExceptDetailedTimers` (needs **4+**; db_bench default **3** leaves `COUNT : 0`). Also requires the flag to reach db_bench (`bench/bench.ini` `stats_level=4` is **not** forwarded by `bench/ycsb_bench.sh` today—only `--statistics`).

### Per-entry-type occupancy (deferred for tier controller v1)
- **Primary block cache:** RocksDB can report how many entries of each `CacheEntryRole` are resident (count + bytes + % capacity) via `DB::GetMapProperty("rocksdb.block-cache-entry-stats")` / `kFastBlockCacheEntryStats`. Implementation walks the shared `block_cache` with `ApplyToAllEntries` and groups by `CacheItemHelper::role` (data / index / filter / meta / `kMisc` for dummies, CRM placeholders under their roles, etc.). Scans are expensive and cached (background ~minutes, foreground ~10s min interval)—not suitable for 1 Hz controller sampling.
- **Compressed secondary cache:** No public per-role breakdown today—only aggregate capacity/usage (`GetSecondaryCacheCapacity`, `GetSecondaryCachePinnedUsage`). The compressed tier’s internal LRU still tags entries with roles, but nothing exposes `ApplyToAllEntries` on that cache. Would need new instrumentation to see data vs index vs filter mix in secondary.
- **Tiered wrapper:** Entry-stats scan covers the primary LRU behind `CacheWithSecondaryAdapter` / `TieredCache`, not the compressed secondary’s internal cache.
- **Statistics tickers** (`BLOCK_CACHE_*_ADD`, `*_HIT`, `*_MISS`) Tickers associated with the block cache (e.g. `BLOCK_CACHE_HIT`, `BLOCK_CACHE_MISS`, `SECONDARY_CACHE_HITS`) reflect cache hits/misses due to Get queries only; compaction, iterator, and readahead paths don't increment those globals. On user Gets, `BLOCK_CACHE_HIT` counts primary and secondary successes; `BLOCK_CACHE_MISS` counts disk reads only; `SECONDARY_CACHE_HITS` ⊆ `BLOCK_CACHE_HIT`. Controller/evaluator derive primary hit rate as `(BLOCK_CACHE_HIT − SECONDARY_CACHE_HITS) / (BLOCK_CACHE_HIT + BLOCK_CACHE_MISS)`, secondary hit rate as `SECONDARY_CACHE_HITS / (SECONDARY_CACHE_HITS + BLOCK_CACHE_MISS)`, and disk miss count as `BLOCK_CACHE_MISS`. `COMPRESSED_SECONDARY_CACHE_DUMMY_HITS` tracks ghost/placeholder hits on user Gets.
- **For now:** Ignore entry-type occupancy in the feedback controller; v1 uses per-1s primary hit rate, secondary hit rate, and ghost/admission tickers only. Revisit as a slow policy-interval signal (one `block-cache-entry-stats` sample per adjust) or phase-2 if secondary role breakdown is added.

### NVM promotion tickers (`COMPRESSED_SECONDARY_CACHE_PROMOTIONS` / `PROMOTION_SKIPS`) — not used in evolve-cache v1
- **What they measure:** In a **3-tier** stack (primary DRAM → compressed secondary DRAM → NVM/flash), `TieredSecondaryCache` wraps the bottom tier. On a hit in NVM/flash, `MaybeInsertAndCreate` may warm the block into compressed secondary via `InsertSaved` — that increments **promotions**. If the warm is skipped (`advise_erase` set, or block not compressed), **promotion_skips** increments. See `cache/tiered_secondary_cache.cc`.
- **2-tier DRAM setup (evolve-cache):** Evaluator uses `use_tiered_cache` + `use_compressed_secondary_cache` with **no** `nvm_sec_cache` / stacked flash tier. Those code paths never run, so both tickers stay at **~0** for the whole benchmark.
- **Adaptive sizing flow:** Deliberately **excluded** from `CacheTierMemorySnapshot`, `HitRateGhostPolicy`, and evaluator `parse_stats.py`. They are not signals for sizing the primary vs compressed-secondary split.
- **When they would matter:** Only if we extend evolution to a 3-tier config (flash secondary under compressed DRAM) and want policies that react to NVM→compressed warming rates. Until then, prefer `dummy_hits`, `sec_cache_insert_placeholder` / `sec_cache_insert_real`, and compression/decompress perf counters for ghost/admission and secondary-tier activity.

### On-disk dataset size (tier controller v1)
- **v1 signal:** `active_sst_raw_bytes` in `CacheTierMemorySnapshot` = `VersionStorageInfo::accumulated_raw_key_size + accumulated_raw_value_size` on the cache-tier column family (same CF as `GetBlockCacheShared()`). Sample path only reads those accumulators under the DB mutex—**O(1)**, no extra SST opens on each tick.
- **What it measures:** Uncompressed **user key + value** bytes from SST files that already have `init_stats_from_file` (table properties loaded into `FileMetaData`). **Partial** live set—not all active SSTs, not data-block-only on disk.
- **Rejected for v1 sample path:**
  - `EstimateLiveDataSize()` — non-trivial per-file walk; uses full SST file sizes (index/filter/tail), not data blocks only.
  - `rocksdb.live-sst-files-size` and `GetAggregatedTableProperties()` — can imply SST footer I/O or heavy work at sample time.
- **Future work:** Persist `TableProperties::data_size` on `FileMetaData`, non-overlapping sum of active **data block** bytes on disk, optional dedicated tickers; avoid global decompress tickers for tier control (compaction SST reads).

### Opportunities for OpenEvolve:
We can evolve heuristics for the following:
- Dynamically allocating memory between primary and secondary tiers
- Heuristics for triggering promotion/demotion between tiers (new admission policies)

## Sizing cache tiers:
- **Inputs**: Compression latency, working set size, compression factor, access pattern, total memory size, cache hit rates, cache miss cost
- **Output**:
	- Memory allocated to upper/lower tier. Note that sizing is a value-based problem as the amount of main memory available is fixed
	- Which compression scheme to use? Which blocks to compress?
- **Current state**: Currently, the split between primary and secondary cache is entirely driven by config parameters and not inferred from runtime statistics.

## Promotion/Demotion Heuristics:
- We are mostly focussing on the case where there is a primary uncompressed cache and a secondary compressed cache, both in DRAM
- Promotion heuristics are fixed (First miss in primary tier adds an entry to the dummy/ghost cache, second miss inserts the actual entry)
- Demotion heuristics can be configured via tiered admission policies (kAdmPolicyPlaceholder, kAdmPolicyAllowCacheHits, kAdmPolicyAllowAll), but these seem to be pretty straightforward
- Secondary cache has no notion of block priorities, it has a very simple LRU mechanism
- **Note:** Statistics tickers named `promotions` / `promotion_skips` refer to NVM→compressed warming in a 3-tier stack, not primary↔secondary DRAM promotion above; see **NVM promotion tickers** section (out of scope for v1 sizing).