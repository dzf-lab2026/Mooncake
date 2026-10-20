#!/usr/bin/env python3
"""Apply the Phase-1 intrusive FIFO changes to a clean Mooncake v0.3.13 checkout.

This is an exact-anchor patch applicator: it refuses to edit files if the source
shape does not match the pinned v0.3.13 baseline. Run from the repository root.
"""
from pathlib import Path
import difflib
import re
import subprocess
import sys

ROOT = Path.cwd()
EXPECTED_COMMIT = "e5598b0992cc258b06d22f24d875480e8931a28e"
try:
    actual_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
except (OSError, subprocess.CalledProcessError) as exc:
    sys.exit(f"ERROR: run from a Git checkout of Mooncake v0.3.13 ({exc})")
if actual_commit != EXPECTED_COMMIT:
    sys.exit(
        "ERROR: wrong source baseline; expected " + EXPECTED_COMMIT +
        ", got " + actual_commit + ". Check out v0.3.13 first."
    )
if subprocess.call(["git", "diff", "--quiet"], cwd=ROOT) != 0 or \
   subprocess.call(["git", "diff", "--cached", "--quiet"], cwd=ROOT) != 0:
    sys.exit("ERROR: working tree is not clean; commit or stash existing changes first")

HEADER = ROOT / "mooncake-store/include/master_service.h"
SOURCE = ROOT / "mooncake-store/src/master_service.cpp"
TESTS_CMAKE = ROOT / "mooncake-store/tests/CMakeLists.txt"
TEST_SOURCE = ROOT / "mooncake-store/tests/master_service_fifo_test.cpp"
MASTER_TEST_SOURCE = ROOT / "mooncake-store/tests/master_service_test.cpp"

for p in (HEADER, SOURCE, TESTS_CMAKE, MASTER_TEST_SOURCE):
    if not p.is_file():
        sys.exit(f"ERROR: {p.relative_to(ROOT)} not found; run from Mooncake v0.3.13 repo root")

originals = {
    p: p.read_text()
    for p in (HEADER, SOURCE, TESTS_CMAKE, MASTER_TEST_SOURCE)
}
changed = dict(originals)

def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"anchor {label!r}: expected exactly 1 match, found {n}")
    return text.replace(old, new, 1)

# Header: declare the nested shard type before ObjectMetadata so metadata can
# remember its owning shard; intrusive links live in ObjectMetadata itself.
h = changed[HEADER]
h = replace_once(h, "#include <array>\n", "#include <array>\n#include <bitset>\n", "bitset include")
h = replace_once(h, "namespace test {\nclass MasterServiceTest;", "namespace test {\nclass MasterServiceTest;\nclass FifoMetadataTest;", "test forward declaration")
h = replace_once(h, "class MasterService {\n", "class MasterService {\n    friend class test::FifoMetadataTest;\n", "fifo test friend")
h = replace_once(h, "    struct ObjectMetadata {\n", "    struct MetadataShard;  // forward declaration for intrusive FIFO owner\n\n    struct ObjectMetadata {\n", "MetadataShard forward declaration")
h = replace_once(h, "        const std::string user_key;\n\n        mutable SpinLock lock;", "        const std::string user_key;\n\n        // Phase 1 FIFO: intrusive links; protected by fifo_owner_shard->mutex.\n        uint64_t seq{0};\n        ObjectMetadata* pre_meta{nullptr};\n        ObjectMetadata* next_meta{nullptr};\n        MetadataShard* fifo_owner_shard{nullptr};\n\n        mutable SpinLock lock;", "ObjectMetadata FIFO fields")
h = replace_once(h, "    struct MetadataShard {\n        mutable SharedMutex mutex;", "    struct MetadataShard {\n        mutable SharedMutex mutex;\n        ObjectMetadata* fifo_head GUARDED_BY(mutex) = nullptr;\n        ObjectMetadata* fifo_tail GUARDED_BY(mutex) = nullptr;\n\n        // Caller must hold this shard's mutex exclusively.\n        void LinkMetadata(ObjectMetadata& metadata, uint64_t new_seq) {\n            if (metadata.fifo_owner_shard == this) return;\n            if (metadata.fifo_owner_shard != nullptr) {\n                metadata.fifo_owner_shard->UnlinkMetadata(metadata);\n            }\n            metadata.seq = new_seq;\n            metadata.pre_meta = fifo_tail;\n            metadata.next_meta = nullptr;\n            metadata.fifo_owner_shard = this;\n            if (fifo_tail != nullptr) {\n                fifo_tail->next_meta = &metadata;\n            } else {\n                fifo_head = &metadata;\n            }\n            fifo_tail = &metadata;\n        }\n\n        // Caller must hold this shard's mutex exclusively. Idempotent.\n        void UnlinkMetadata(ObjectMetadata& metadata) {\n            if (metadata.fifo_owner_shard != this) return;\n            if (metadata.pre_meta != nullptr) {\n                metadata.pre_meta->next_meta = metadata.next_meta;\n            } else {\n                fifo_head = metadata.next_meta;\n            }\n            if (metadata.next_meta != nullptr) {\n                metadata.next_meta->pre_meta = metadata.pre_meta;\n            } else {\n                fifo_tail = metadata.pre_meta;\n            }\n            metadata.pre_meta = nullptr;\n            metadata.next_meta = nullptr;\n            metadata.fifo_owner_shard = nullptr;\n        }", "MetadataShard FIFO methods")
h = replace_once(h, "    std::array<MetadataShard, kNumShards> metadata_shards_;", "    std::array<MetadataShard, kNumShards> metadata_shards_;\n    // Starts at 1; all insertions in this Master share one sequence space.\n    std::atomic<uint64_t> next_metadata_seq_{1};", "global FIFO sequence")
create_anchor = '''            it_ = result.first;
        }

       private:'''

create_replacement = '''            it_ = result.first;
            if (result.second) {
                shard_guard_->LinkMetadata(
                    it_->second,
                    service_->next_metadata_seq_.fetch_add(
                        1, std::memory_order_relaxed));
            }
        }

       private:'''

h = replace_once(
    h, create_anchor, create_replacement,
    "MetadataAccessorRW::Create FIFO insertion"
)
changed[HEADER] = h

# Source: make every metadata creation path link into its owning shard list.
c = changed[SOURCE]
standby_old = '''            tenant_state.metadata.emplace(\n                std::piecewise_construct,\n                std::forward_as_tuple(object.user_key),\n                std::forward_as_tuple(\n                    standby_meta.client_id, now, standby_meta.size,\n                    std::move(object.replicas), std::nullopt,\n                    standby_meta.hard_pinned.value_or(false),\n                    standby_meta.data_type, standby_meta.group_id,\n                    object.tenant_id, object.user_key));\n            if (!standby_meta.group_id.empty()) {'''
standby_new = '''            auto [restored_it, restored_inserted] = tenant_state.metadata.emplace(\n                std::piecewise_construct,\n                std::forward_as_tuple(object.user_key),\n                std::forward_as_tuple(\n                    standby_meta.client_id, now, standby_meta.size,\n                    std::move(object.replicas), std::nullopt,\n                    standby_meta.hard_pinned.value_or(false),\n                    standby_meta.data_type, standby_meta.group_id,\n                    object.tenant_id, object.user_key));\n            if (restored_inserted) {\n                shard->LinkMetadata(restored_it->second,\n                    next_metadata_seq_.fetch_add(1, std::memory_order_relaxed));\n            }\n            if (!standby_meta.group_id.empty()) {'''
# The accessor in this restore path is MetadataShardAccessorRW, hence shard->.
c = replace_once(c, standby_old, standby_new, "standby metadata restore insertion")

put_anchor = '''        return tl::make_unexpected(ErrorCode::OBJECT_ALREADY_EXISTS);\n    }\n    if (enable_multi_tenants_) {\n        auto adopt_result = it->second.quota_ledger.AdoptPendingCharge('''
put_replacement = '''        return tl::make_unexpected(ErrorCode::OBJECT_ALREADY_EXISTS);\n    }\n    if (enable_multi_tenants_) {\n        auto adopt_result = it->second.quota_ledger.AdoptPendingCharge('''
c = replace_once(c, put_anchor, put_replacement, "normal PutStart insertion")

restore_anchor = '''        auto [it, inserted] = tenant_state.metadata.emplace(\n            std::piecewise_construct, std::forward_as_tuple(std::move(key)),\n            std::forward_as_tuple(\n                metadata_ptr->client_id, metadata_ptr->put_start_time,\n                metadata_ptr->size, metadata_ptr->PopReplicas(), std::nullopt,\n                metadata_ptr->IsHardPinned(), metadata_ptr->data_type,\n                metadata_ptr->group_id, tenant_id, user_key));\n\n        it->second.lease_timeout = metadata_ptr->lease_timeout;'''
restore_replacement = '''        auto [it, inserted] = tenant_state.metadata.emplace(\n            std::piecewise_construct, std::forward_as_tuple(std::move(key)),\n            std::forward_as_tuple(\n                metadata_ptr->client_id, metadata_ptr->put_start_time,\n                metadata_ptr->size, metadata_ptr->PopReplicas(), std::nullopt,\n                metadata_ptr->IsHardPinned(), metadata_ptr->data_type,\n                metadata_ptr->group_id, tenant_id, user_key));\n        if (inserted) {\n            shard.LinkMetadata(it->second,\n                service_->next_metadata_seq_.fetch_add(1, std::memory_order_relaxed));\n        }\n\n        it->second.lease_timeout = metadata_ptr->lease_timeout;'''
c = replace_once(c, restore_anchor, restore_replacement, "snapshot metadata restore insertion")
put_end_anchor = '''    // If the object is completed, remove it from the processing set.
    if (metadata.AllReplicas(&Replica::fn_is_completed) &&
        accessor.InProcessing()) {
        accessor.EraseFromProcessing();
    }

    SyncCacheTotalAccounting(metadata);'''

put_end_replacement = '''    // Only fully completed objects enter FIFO. A partial PutEnd must not
    // make the object eligible for eviction.
    if (metadata.AllReplicas(&Replica::fn_is_completed)) {
        if (accessor.InProcessing()) {
            accessor.EraseFromProcessing();
        }
        accessor.GetShard()->LinkMetadata(
            metadata, next_metadata_seq_.fetch_add(1, std::memory_order_relaxed));
    }

    SyncCacheTotalAccounting(metadata);'''

c = replace_once(
    c, put_end_anchor, put_end_replacement, "PutEnd FIFO insertion"
)
changed[SOURCE] = c
# Central deletion path plus the one insertion rollback path that erases directly.
erase_anchor = "    auto next = tenant_state.metadata.erase(it);\n    if (had_completed_disk && shard) {"
erase_replacement = "    if (metadata.fifo_owner_shard != nullptr) {\n        metadata.fifo_owner_shard->UnlinkMetadata(metadata);\n    }\n    auto next = tenant_state.metadata.erase(it);\n    if (had_completed_disk && shard) {"
c = replace_once(c, erase_anchor, erase_replacement, "central EraseMetadata unlink")
rollback_anchor = '''            refund_pending_quota();\n            tenant_state.metadata.erase(it);\n            return tl::make_unexpected(ErrorCode::INTERNAL_ERROR);'''
rollback_replacement = '''            refund_pending_quota();\n            if (it->second.fifo_owner_shard != nullptr) {\n                it->second.fifo_owner_shard->UnlinkMetadata(it->second);\n            }\n            tenant_state.metadata.erase(it);\n            return tl::make_unexpected(ErrorCode::INTERNAL_ERROR);'''
c = replace_once(c, rollback_anchor, rollback_replacement, "PutStart rollback unlink")

# Reset intrusive roots whenever snapshot restore discards all shard metadata.
def reset_shard_roots(match):
    indent = match.group(1)
    return (f"{indent}shard.tenants.clear();\n"
            f"{indent}shard.fifo_head = nullptr;\n"
            f"{indent}shard.fifo_tail = nullptr;")
c, clear_count = re.subn(r"^(\s*)shard\.tenants\.clear\(\);$",
                          reset_shard_roots, c, flags=re.MULTILINE)
if clear_count != 2:
    raise RuntimeError(f"snapshot tenant clear anchors: expected 2, found {clear_count}")
changed[SOURCE] = c

# Phase 1: preserve the existing safe replica/offload/OpLog/group deletion
# machinery, but replace candidate selection with a FIFO-head walk. The branch
# runs while holding each shard's exclusive lock and uses DeleteBudget only.
c = changed[SOURCE]
phase1_anchor = "    // Candidate carries key for safe lookup after releasing shard lock."
phase1_end_anchor = "\nvoid MasterService::NoFBatchEvict"
phase1 = r'''    // ===== Phase 1: FIFO eviction (v7) =====
    // DeleteBudget counts successfully evicted metadata objects, not scanned
    // entries. Phase 1 deliberately has no ScanBudget or scheduler rounds.
    // Preserve the existing lock order: snapshot before any shard lock.
    std::shared_lock<std::shared_mutex> snapshot_lock(snapshot_mutex_);

    long total_eviction_base = 0;
    for (size_t shard_idx = 0; shard_idx < kNumShards; ++shard_idx) {
        MetadataShardAccessorRW shard(this, shard_idx);
        DiscardExpiredProcessingReplicas(shard, now);
        for (const auto& [tenant_id, tenant_state] : shard->tenants) {
            for (const auto& [key, metadata] : tenant_state.metadata) {
                if (!metadata.IsHardPinned() && can_evict_replicas(metadata)) {
                    ++total_eviction_base;
                }
            }
        }
    }

    const long delete_budget = static_cast<long>(
        std::ceil(total_eviction_base * evict_ratio_target));
    long evicted_count = 0;
    uint64_t total_freed_size = 0;
    std::bitset<kNumShards> fifo_evicted_shards;

    if (delete_budget > 0) {
        for (int soft_pin_pass = 0;
            soft_pin_pass <
                (allow_evict_soft_pinned_objects_ ? 2 : 1) &&
                evicted_count < delete_budget;
            ++soft_pin_pass) {
            const bool allow_soft_pinned = (soft_pin_pass == 1);
            const size_t start_idx = randomIndex(kNumShards);
            for (size_t shard_offset = 0;
                 shard_offset < kNumShards && evicted_count < delete_budget;
                 ++shard_offset) {
                const size_t shard_idx = (start_idx + shard_offset) % kNumShards;
                std::vector<std::vector<Replica>> deferred_replicas;
                {
                    MetadataShardAccessorRW shard(this, shard_idx);
                    auto* cursor = shard->fifo_head;
                    uint64_t last_seq = 0;
                    while (cursor != nullptr && evicted_count < delete_budget) {
                        // Group eviction can unlink several nodes. In that case,
                        // resume at the first remaining seq after the current one.
                        // For an ordinary key, next_meta remains stable.
                        auto* next = cursor->next_meta;
                        const uint64_t current_seq = cursor->seq;
                        const bool grouped = cursor->IsGrouped();
                        auto tenant_it = shard->tenants.find(cursor->tenant_id);
                        if (tenant_it == shard->tenants.end()) {
                            last_seq = current_seq;
                            cursor = next;
                            continue;
                        }
                        auto& tenant_state = tenant_it->second;
                        auto it = tenant_state.metadata.find(cursor->user_key);
                        if (it == tenant_state.metadata.end()) {
                            last_seq = current_seq;
                            cursor = next;
                            continue;
                        }
                        auto& metadata = it->second;
                        if (metadata.IsHardPinned() ||
                            !metadata.IsLeaseExpired(now) ||
                            (allow_soft_pinned
                                ? !IsSoftPinActive(metadata, now)
                                : IsSoftPinActive(metadata, now)) ||
                            !can_evict_replicas(metadata)) {
                            last_seq = current_seq;
                            cursor = next;
                            continue;
                        }
                        auto evict_result = try_evict_group_or_object(
                            cursor->tenant_id, cursor->user_key, metadata, shard,
                            tenant_state, deferred_replicas,
                            /*allow_soft_pinned=*/allow_soft_pinned);
                        total_freed_size += evict_result.freed_bytes;
                        // The group helper may have removed other members, but it
                        // keeps the current key alive until this caller erases it.
                        if (!enable_oplog_ && !metadata.IsGrouped()) {
                            PublishKvRemovedAfterEvict(
                                cursor->user_key, evict_result.freed_bytes, "cpu",
                                metadata, cursor->tenant_id);
                        }
                        if (!enable_oplog_ && !metadata.IsValid()) {
                            EraseMetadata(tenant_state, it, cursor->tenant_id,
                                          QuotaEraseMode::kFull, &shard);
                        }
                        if (evict_result.evicted_objects > 0) {
                            evicted_count += evict_result.evicted_objects;
                            fifo_evicted_shards.set(shard_idx);
                        }
                        last_seq = current_seq;
                        if (grouped) {
                            cursor = shard->fifo_head;
                            while (cursor != nullptr && cursor->seq <= last_seq) {
                                cursor = cursor->next_meta;
                            }
                        } else {
                            cursor = next;
                        }
                    }
                    for (auto tenant_it = shard->tenants.begin();
                         tenant_it != shard->tenants.end();) {
                        if (tenant_it->second.Empty()) {
                            tenant_it = shard->tenants.erase(tenant_it);
                        } else {
                            ++tenant_it;
                        }
                    }
                }
                deferred_replicas.clear();
            }
        }
    }

    for (size_t i = 0; i < kNumShards; ++i) {
        if (!fifo_evicted_shards.test(i)) continue;
        MetadataShardAccessorRW shard(this, i);
        for (auto& tenant : shard->tenants) {
            ShrinkBucketsIfSparse(tenant.second.metadata);
        }
    }

    const uint64_t released_discarded_cnt = ReleaseExpiredDiscardedReplicas(now);
    if (evicted_count > 0 || released_discarded_cnt > 0 ||
        offload_deferred_count > 0) {
        need_mem_eviction_ = false;
        MasterMetricManager::instance().inc_eviction_success(evicted_count,
                                                             total_freed_size);
        MasterMetricManager::instance().inc_mem_eviction_success(
            evicted_count, total_freed_size);
    } else {
        if (total_eviction_base == 0) need_mem_eviction_ = false;
        MasterMetricManager::instance().inc_eviction_fail();
        MasterMetricManager::instance().inc_mem_eviction_fail();
    }
    VLOG(1) << "[FIFO-EVICT-RESULT] deleted=" << evicted_count
            << ", delete_budget=" << delete_budget
            << ", eviction_base=" << total_eviction_base
            << ", total_freed_size=" << total_freed_size;
    return;
}

'''
# Replace the old candidate-census / two-pass tail of BatchEvict rather than
# inserting an early-return branch in front of it. This avoids duplicate local
# declarations and ensures Phase 1 is the sole BatchEvict implementation.
if c.count(phase1_anchor) != 1:
    raise RuntimeError(f"anchor 'Phase 1 replacement start': expected exactly 1 match, found {c.count(phase1_anchor)}")
if c.count(phase1_end_anchor) != 1:
    raise RuntimeError(f"anchor 'BatchEvict end': expected exactly 1 match, found {c.count(phase1_end_anchor)}")
start = c.index(phase1_anchor)
end = c.index(phase1_end_anchor, start)
if end <= start:
    raise RuntimeError("BatchEvict replacement anchors are out of order")
c = c[:start] + phase1 + c[end + 1:]
changed[SOURCE] = c

# Register focused unit tests. The test exercises list boundary invariants with
# actual ObjectMetadata objects and does not require a running MasterService.
cm = changed[TESTS_CMAKE]
cm_anchor = "add_store_test(master_service_test master_service_test.cpp)"
cm = replace_once(cm, cm_anchor, cm_anchor + "\nadd_store_test(master_service_fifo_test master_service_fifo_test.cpp)", "CMake FIFO test target")
changed[TESTS_CMAKE] = cm

master_test = changed[MASTER_TEST_SOURCE]

fifo_order_test = r'''TEST_F(MasterServiceTest, BatchEvictUsesFifoOrderWithinShard) {
    auto service_config =
        MasterServiceConfig::builder().set_default_kv_lease_ttl(0).build();
    std::unique_ptr<MasterService> service_(new MasterService(service_config));

    [[maybe_unused]] const auto context = PrepareSimpleSegment(
        *service_, "fifo_order_segment", kDefaultSegmentBase,
        16 * 1024 * 1024);

    const UUID client_id = generate_uuid();
    const std::string oldest_key = "fifo_order_key_0";
    const size_t target_shard =
        MetadataShardIndex(*service_, oldest_key);

    std::string newer_key;
    for (size_t i = 1; newer_key.empty(); ++i) {
        const std::string candidate =
            "fifo_order_key_" + std::to_string(i);
        if (MetadataShardIndex(*service_, candidate) == target_shard) {
            newer_key = candidate;
        }
    }

    ReplicateConfig config;
    config.replica_num = 1;
    PutCompletedObject(*service_, client_id, oldest_key, config);
    PutCompletedObject(*service_, client_id, newer_key, config);

    service_->RunBatchEvictForTesting(0.5, 0.0);

    EXPECT_FALSE(
        service_->ExistKey(oldest_key, TenantId::Default()).value_or(true));
    EXPECT_TRUE(
        service_->ExistKey(newer_key, TenantId::Default()).value_or(false));
}

TEST_F(MasterServiceTest, BatchEvictHonorsSingleObjectBudget) {
    auto service_config =
        MasterServiceConfig::builder().set_default_kv_lease_ttl(0).build();
    std::unique_ptr<MasterService> service_(new MasterService(service_config));

    [[maybe_unused]] const auto context = PrepareSimpleSegment(
        *service_, "fifo_budget_segment", kDefaultSegmentBase,
        16 * 1024 * 1024);

    const UUID client_id = generate_uuid();
    const size_t target_shard =
        MetadataShardIndex(*service_, "fifo_budget_key_0");

    std::vector<std::string> keys{"fifo_budget_key_0"};
    for (size_t i = 1; keys.size() < 3; ++i) {
        const std::string candidate =
            "fifo_budget_key_" + std::to_string(i);
        if (MetadataShardIndex(*service_, candidate) == target_shard) {
            keys.push_back(candidate);
        }
    }

    ReplicateConfig config;
    config.replica_num = 1;
    for (const auto& key : keys) {
        PutCompletedObject(*service_, client_id, key, config);
    }

    // Three eligible objects with a target ratio of 1/3 should yield
    // a one-object target budget (ceil(3 * 1/3) == 1).
    service_->RunBatchEvictForTesting(1.0 / 3.0, 0.0);

    EXPECT_FALSE(
        service_->ExistKey(keys[0], TenantId::Default()).value_or(true));
    EXPECT_TRUE(
        service_->ExistKey(keys[1], TenantId::Default()).value_or(false));
    EXPECT_TRUE(
        service_->ExistKey(keys[2], TenantId::Default()).value_or(false));
}

TEST_F(MasterServiceTest, GroupedBatchEvictUnlinksAllFifoMembers) {
    auto service_config =
        MasterServiceConfig::builder().set_default_kv_lease_ttl(0).build();
    std::unique_ptr<MasterService> service_(new MasterService(service_config));

    [[maybe_unused]] const auto context = PrepareSimpleSegment(
        *service_, "fifo_group_cleanup_segment", kDefaultSegmentBase,
        16 * 1024 * 1024);

    const UUID client_id = generate_uuid();
    const std::string key_a = "fifo_group_cleanup_a";
    const std::string key_b = "fifo_group_cleanup_b";

    ReplicateConfig config;
    config.replica_num = 1;
    config.group_ids =
        std::vector<std::string>{FindGroupIdOnDifferentShard(key_a)};

    PutCompletedObject(*service_, client_id, key_a, config);
    PutCompletedObject(*service_, client_id, key_b, config);

    ASSERT_TRUE(
        service_->ExistKey(key_a, TenantId::Default()).value_or(false));
    ASSERT_TRUE(
        service_->ExistKey(key_b, TenantId::Default()).value_or(false));

    // A budget of two objects allows the complete two-member group to evict.
    service_->RunBatchEvictForTesting(1.0, 0.0);

    EXPECT_FALSE(
        service_->ExistKey(key_a, TenantId::Default()).value_or(true));
    EXPECT_FALSE(
        service_->ExistKey(key_b, TenantId::Default()).value_or(true));

    // A second pass must safely handle the now-empty eviction candidate set.
    EXPECT_NO_THROW(service_->RunBatchEvictForTesting(1.0, 0.0));
}
TEST_F(MasterServiceTest, GroupedEvictionMayExceedDeleteBudget) {
    auto service_config =
        MasterServiceConfig::builder().set_default_kv_lease_ttl(0).build();
    std::unique_ptr<MasterService> service_(new MasterService(service_config));

    [[maybe_unused]] const auto context = PrepareSimpleSegment(
        *service_, "fifo_group_budget_segment", kDefaultSegmentBase,
        16 * 1024 * 1024);

    const UUID client_id = generate_uuid();
    const std::string key_a = "fifo_group_budget_a";
    const std::string key_b = "fifo_group_budget_b";

    ReplicateConfig config;
    config.replica_num = 1;
    config.group_ids =
        std::vector<std::string>{FindGroupIdOnDifferentShard(key_a)};

    PutCompletedObject(*service_, client_id, key_a, config);
    PutCompletedObject(*service_, client_id, key_b, config);

    ASSERT_TRUE(
        service_->ExistKey(key_a, TenantId::Default()).value_or(false));
    ASSERT_TRUE(
        service_->ExistKey(key_b, TenantId::Default()).value_or(false));

    // Two eligible objects with ratio 0.5 yield DeleteBudget == 1.
    // Group eviction may remove both members in one operation.
    service_->RunBatchEvictForTesting(0.5, 0.0);

    EXPECT_FALSE(
        service_->ExistKey(key_a, TenantId::Default()).value_or(true));
    EXPECT_FALSE(
        service_->ExistKey(key_b, TenantId::Default()).value_or(true));

    // The FIFO list must remain valid after the group overshoots the budget.
    EXPECT_NO_THROW(service_->RunBatchEvictForTesting(1.0, 0.0));
}

'''

anchor = "TEST_F(MasterServiceTest, TryEvictLeasedObject) {"
master_test = replace_once(
    master_test, anchor, fifo_order_test + anchor,
    "BatchEvict FIFO order integration test"
)
changed[MASTER_TEST_SOURCE] = master_test

test_content = r'''#include <gtest/gtest.h>

#include <chrono>
#include <optional>
#include <vector>

#include "master_service.h"

namespace mooncake::test {

class FifoMetadataTest : public ::testing::Test {
 public:
    // This member is a friend of MasterService so private nested metadata types
    // can be exercised without widening the production API.
    void RunIntrusiveListBoundaryTest() {
        using Clock = std::chrono::system_clock;
        const auto now = Clock::now();
        MasterService::MetadataShard shard;
        MasterService::MetadataShard other_shard;
        MasterService::ObjectMetadata a(UUID{}, now, 1, {});
        MasterService::ObjectMetadata b(UUID{}, now, 1, {});
        MasterService::ObjectMetadata c(UUID{}, now, 1, {});
        MasterService::ObjectMetadata d(UUID{}, now, 1, {});

        shard.LinkMetadata(a, 1);
        shard.LinkMetadata(b, 2);
        shard.LinkMetadata(c, 3);
        ASSERT_EQ(shard.fifo_head, &a);
        ASSERT_EQ(shard.fifo_tail, &c);
        ASSERT_EQ(a.next_meta, &b);
        ASSERT_EQ(b.pre_meta, &a);
        ASSERT_EQ(b.next_meta, &c);
        ASSERT_EQ(c.pre_meta, &b);

        // Middle removal reconnects both neighbours.
        shard.UnlinkMetadata(b);
        EXPECT_EQ(a.next_meta, &c);
        EXPECT_EQ(c.pre_meta, &a);
        EXPECT_EQ(b.pre_meta, nullptr);
        EXPECT_EQ(b.next_meta, nullptr);

        // Head and tail removal update the endpoints.
        shard.UnlinkMetadata(a);
        EXPECT_EQ(shard.fifo_head, &c);
        EXPECT_EQ(shard.fifo_tail, &c);
        shard.UnlinkMetadata(c);
        EXPECT_EQ(shard.fifo_head, nullptr);
        EXPECT_EQ(shard.fifo_tail, nullptr);

        // Single-node removal and per-shard isolation.
        shard.LinkMetadata(d, 4);
        other_shard.LinkMetadata(b, 5);
        EXPECT_EQ(shard.fifo_head, &d);
        EXPECT_EQ(shard.fifo_tail, &d);
        EXPECT_EQ(other_shard.fifo_head, &b);
        EXPECT_EQ(other_shard.fifo_tail, &b);
        shard.UnlinkMetadata(d);
        other_shard.UnlinkMetadata(b);
        EXPECT_EQ(shard.fifo_head, nullptr);
        EXPECT_EQ(other_shard.fifo_head, nullptr);

        // Unlink is idempotent; it must not corrupt an empty list.
        shard.UnlinkMetadata(d);
        EXPECT_EQ(shard.fifo_head, nullptr);
        EXPECT_EQ(shard.fifo_tail, nullptr);
    }
    void RunRepeatedLinkTest() {
       using Clock = std::chrono::system_clock;
       const auto now = Clock::now();
       MasterService::MetadataShard shard;
       MasterService::ObjectMetadata a(UUID{}, now, 1, {});
       MasterService::ObjectMetadata b(UUID{}, now, 1, {});
       MasterService::ObjectMetadata c(UUID{}, now, 1, {});

       shard.LinkMetadata(a, 1);
       shard.LinkMetadata(b, 2);
       shard.LinkMetadata(c, 3);

       shard.LinkMetadata(b, 4);

       EXPECT_EQ(shard.fifo_head, &a);
       EXPECT_EQ(shard.fifo_tail, &c);
       EXPECT_EQ(a.next_meta, &b);
       EXPECT_EQ(b.pre_meta, &a);
       EXPECT_EQ(b.next_meta, &c);
       EXPECT_EQ(c.pre_meta, &b);
       EXPECT_EQ(b.seq, 2);

       shard.UnlinkMetadata(b);
       EXPECT_EQ(shard.fifo_head, &a);
       EXPECT_EQ(shard.fifo_tail, &c);
       EXPECT_EQ(a.next_meta, &c);
       EXPECT_EQ(c.pre_meta, &a);
       EXPECT_EQ(b.fifo_owner_shard, nullptr);
   }
};

TEST_F(FifoMetadataTest, IntrusiveListInsertionAndUnlinkBoundaries) {
    RunIntrusiveListBoundaryTest();
}

TEST_F(FifoMetadataTest, RepeatedLinkDoesNotDuplicateOrReorderNode) {
    RunRepeatedLinkTest();
}

}  // namespace mooncake::test
'''

# Check all transformations before writing anything.
for p, old in originals.items():
    if changed[p] == old:
        raise RuntimeError(f"no edits produced for {p.relative_to(ROOT)}")
if TEST_SOURCE.exists():
    raise RuntimeError(f"refusing to overwrite existing {TEST_SOURCE.relative_to(ROOT)}")

# Persist edits, then emit a unified diff against the exact original contents.
for p, content in changed.items():
    p.write_text(content)
TEST_SOURCE.write_text(test_content)

patch_path = ROOT / "mooncake_fifo_phase1.patch"
with patch_path.open("w") as out:
    for p in (HEADER, SOURCE, TESTS_CMAKE, MASTER_TEST_SOURCE):
        out.writelines(difflib.unified_diff(
            originals[p].splitlines(keepends=True), changed[p].splitlines(keepends=True),
            fromfile="a/" + str(p.relative_to(ROOT)),
            tofile="b/" + str(p.relative_to(ROOT))))
    out.writelines(difflib.unified_diff(
        [], test_content.splitlines(keepends=True),
        fromfile="/dev/null", tofile="b/" + str(TEST_SOURCE.relative_to(ROOT))))

print(f"Applied Phase 1 changes to {len(changed)} existing files and added {TEST_SOURCE.relative_to(ROOT)}")
print(f"Unified diff written to {patch_path.relative_to(ROOT)}")
print("Next: inspect `git diff --check`, build the master_service_fifo_test target, and run it.")
