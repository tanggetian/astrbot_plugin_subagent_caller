"""core/sessions.py plan B 集成测试——不依赖 AstrBot 框架。

跑：python3 tests/test_sessions.py from plugin root。

plan B 测试覆盖：
1. DB schema（project_sessions + astrbot_session_id 列）
2. UNIQUE 约束 (subagent, project)
3. set_astrbot_session_id 持久化 + 读回
4. delete_by_key / delete_session 行为一致
5. validate_project_name 校验
6. lock 串行（同 key）
7. lock 并行（不同 key）
8. lock 超时 → SessionLockTimeout
9. list_active 排序 + owner 过滤
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import types

# === Stub astrbot.api.logger / astrbot.core.utils.astrbot_path ===


def _stub_astrbot():
    pkg_au = types.ModuleType("astrbot")
    pkg_au_core = types.ModuleType("astrbot.core")
    pkg_au_core_utils = types.ModuleType("astrbot.core.utils")
    mod_ap = types.ModuleType("astrbot.core.utils.astrbot_path")
    mod_ap.get_astrbot_data_path = lambda: os.path.join(TMP_DIR, "fake_data")
    pkg_au_core_utils.astrbot_path = mod_ap
    pkg_au_core.utils = pkg_au_core_utils
    pkg_au_api = types.ModuleType("astrbot.api")
    mod_logger = types.ModuleType("astrbot.api.logger")
    import logging

    real_logger = logging.getLogger("subagent_test")
    real_logger.addHandler(logging.StreamHandler())
    real_logger.setLevel(logging.WARNING)
    mod_logger.info = real_logger.info
    mod_logger.warning = real_logger.warning
    mod_logger.error = real_logger.error
    mod_logger.debug = real_logger.debug
    pkg_au_api.logger = mod_logger
    pkg_au.api = pkg_au_api
    pkg_au.core = pkg_au_core
    sys.modules["astrbot"] = pkg_au
    sys.modules["astrbot.core"] = pkg_au_core
    sys.modules["astrbot.core.utils"] = pkg_au_core_utils
    sys.modules["astrbot.core.utils.astrbot_path"] = mod_ap
    sys.modules["astrbot.api"] = pkg_au_api
    sys.modules["astrbot.api.logger"] = mod_logger


# === 隔离 storage 用的 DB 路径——不污染主控的 subagent_caller.db ===
TMP_DIR = tempfile.mkdtemp(prefix="subagent_test_")
TEST_DB = os.path.join(TMP_DIR, "test.db")

_stub_astrbot()

# 把 plugin 根加到 sys.path——允许「import core.xxx」
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_ROOT)

import core.storage as _storage  # noqa: E402

_orig_get_db_path = _storage.get_db_path


def _patched_get_db_path():
    p = TEST_DB
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


_storage.get_db_path = _patched_get_db_path

# 重新触发 init——会建表
_storage.init_db()


def test_db_schema():
    """plan B schema：三张表 + astrbot_session_id 列。"""
    conn = sqlite3.connect(TEST_DB)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [r[0] for r in cur.fetchall()]
    print("  tables:", tables)
    assert "project_sessions" in tables, "缺 project_sessions 表"
    assert "subagent_tasks" in tables, "缺 subagent_tasks 表"
    assert "subagent_instances" in tables, "缺 subagent_instances 表"
    assert "project_messages" not in tables, "plan B 不应有 project_messages 表"

    cols = [
        r[1] for r in conn.execute("PRAGMA table_info(project_sessions)").fetchall()
    ]
    print("  project_sessions cols:", cols)
    assert "astrbot_session_id" in cols, "缺 astrbot_session_id 列"
    assert "session_id" in cols, "缺 session_id 列"
    assert "subagent" in cols and "project" in cols
    assert "message_count" not in cols, "plan B 不应有 message_count 列"

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='project_sessions'"
    )
    idxs = [r[0] for r in cur.fetchall()]
    print("  project_sessions indexes:", idxs)
    conn.close()
    print("  ✅ DB schema OK (plan B)")


def test_get_or_create_uniqueness():
    """(subagent, project) UNIQUE——同 key 第二次拿的是同一个 session。"""
    store = _storage.ProjectSessionStore()
    s1 = store.get_or_create("test_bot", "weather", "owner1")
    s2 = store.get_or_create("test_bot", "weather", "owner1")
    s3 = store.get_or_create("test_bot", "weather", "owner2")
    s4 = store.get_or_create("test_bot", "research", "owner1")
    assert s1 is not None and s2 is not None and s3 is not None and s4 is not None
    assert s1["session_id"] == s2["session_id"]
    assert s1["session_id"] == s3["session_id"]
    assert s1["session_id"] != s4["session_id"]
    # plan B：新建 session 默认 astrbot_session_id = ""
    assert s1["astrbot_session_id"] == "", (
        f"新建 session 应 astrbot_session_id='', 实际 {s1['astrbot_session_id']!r}"
    )
    print(
        f"  ✅ UNIQUE 约束 + 初始空 astrbot_session_id OK (s1={s1['session_id'][:18]}…)"
    )


def test_set_astrbot_session_id():
    """set_astrbot_session_id 持久化 + 读回。"""
    store = _storage.ProjectSessionStore()
    sess = store.get_or_create("test_bot", "weather_sid", "owner1")
    assert sess["astrbot_session_id"] == ""
    # 模拟子 AstrBot 返回的 UUID
    uuid1 = "8f4a1e2b-3c5d-4e6f-9a8b-7c0d1e2f3a4b"
    ok = store.set_astrbot_session_id("test_bot", "weather_sid", uuid1)
    assert ok, "set 应返回 True"
    sess2 = store.get_session("test_bot", "weather_sid")
    assert sess2 is not None
    assert sess2["astrbot_session_id"] == uuid1, (
        f"应读回 {uuid1}，实际 {sess2['astrbot_session_id']}"
    )
    # 覆盖为新值
    uuid2 = "11111111-2222-3333-4444-555555555555"
    store.set_astrbot_session_id("test_bot", "weather_sid", uuid2)
    sess3 = store.get_session("test_bot", "weather_sid")
    assert sess3["astrbot_session_id"] == uuid2
    # 空字符串 = 清空（但 row 还在）
    store.set_astrbot_session_id("test_bot", "weather_sid", "")
    sess4 = store.get_session("test_bot", "weather_sid")
    assert sess4["astrbot_session_id"] == "", (
        "空字符串应清空 astrbot_session_id 但保留 row"
    )
    print(
        f"  ✅ set_astrbot_session_id OK (uuid1={uuid1[:8]}… → uuid2={uuid2[:8]}… → 清空)"
    )


def test_touch_session_updates_updated_at_without_changing_sid():
    """session_id 不变时也要能刷新 updated_at，供 LRU / 超时回收判断最后活跃时间。"""
    store = _storage.ProjectSessionStore()
    store.get_or_create("test_bot", "touch_test", "owner1")
    uuid1 = "touch-11111111-2222-3333-4444-555555555555"
    assert store.set_astrbot_session_id("test_bot", "touch_test", uuid1)
    before = store.get_session("test_bot", "touch_test")
    assert before is not None
    before_updated_at = before["updated_at"]

    time.sleep(0.01)
    assert store.touch_session("test_bot", "touch_test")
    after = store.get_session("test_bot", "touch_test")
    assert after is not None
    assert after["astrbot_session_id"] == uuid1
    assert after["updated_at"] > before_updated_at, (
        f"touch_session 应刷新 updated_at，before={before_updated_at}, after={after['updated_at']}"
    )
    print("  ✅ touch_session 只刷新 updated_at，不改 astrbot_session_id")


def test_delete_by_key():
    """delete_by_key 删整行（项目 session 兼容；plan B 下等价于「删 session_id 映射」）。"""
    store = _storage.ProjectSessionStore()
    store.get_or_create("test_bot", "del_test", "owner1")
    store.set_astrbot_session_id("test_bot", "del_test", "uuid-del-test")
    ok = store.delete_by_key("test_bot", "del_test")
    assert ok, "delete_by_key 应返回 True"
    sess = store.get_session("test_bot", "del_test")
    assert sess is None, "删后应查不到"
    # 再删一次 → 返回 False（幂等）
    ok2 = store.delete_by_key("test_bot", "del_test")
    assert not ok2, "重复 delete_by_key 应返回 False"
    print("  ✅ delete_by_key OK")


def test_delete_session_by_pk():
    """delete_session 按 plugin 内部 PK 删。"""
    store = _storage.ProjectSessionStore()
    sess = store.get_or_create("test_bot", "del_pk_test", "owner1")
    sid = sess["session_id"]
    ok = store.delete_session(sid)
    assert ok
    assert store.get_session("test_bot", "del_pk_test") is None
    # 不存在的 sid
    assert not store.delete_session("ps-fake-fake-fake")
    print("  ✅ delete_session by PK OK")


def test_project_name_validation():
    """validate_project_name 校验：合法字符 + 长度 1~64。"""
    from core.sessions import ProjectNameError, validate_project_name

    for good in ["weather", "research_v2", "my-topic", "topic.1", "abc", "A" * 64]:
        validate_project_name(good)
    for bad in [
        "",
        " ",
        "a b",
        "你好",
        "../etc",
        "a" * 65,
        "$dollar",
        "with/slash",
        None,
    ]:
        try:
            validate_project_name(bad)
            assert False, f"应拒绝：{bad!r}"
        except (ProjectNameError, TypeError):
            pass
    print("  ✅ validate_project_name OK")


def test_list_active():
    """list_active 顺序 + owner 过滤。"""
    from core.sessions import ProjectSessionManager

    store = _storage.ProjectSessionStore()
    mgr = ProjectSessionManager(store)
    # 单独创建 3 个 fresh session
    store.get_or_create("list_test_sub_a", "p_x", "lt_owner1")
    time.sleep(0.01)
    store.get_or_create("list_test_sub_b", "p_y", "lt_owner1")
    time.sleep(0.01)
    store.get_or_create("list_test_sub_c", "p_z", "lt_owner2")
    # 这 3 个应排在最前
    top3 = mgr.list_active(limit=3)
    keys = [(s["subagent"], s["project"]) for s in top3]
    assert keys[0] == ("list_test_sub_c", "p_z"), f"应 c 排第一，actual={keys}"
    assert keys[1] == ("list_test_sub_b", "p_y")
    assert keys[2] == ("list_test_sub_a", "p_x")
    # owner 过滤
    lt_owner1_only = [
        s
        for s in mgr.list_active(user_id="lt_owner1", limit=100)
        if s["user_id"] == "lt_owner1"
    ]
    assert len(lt_owner1_only) == 2, (
        f"应 2 个 lt_owner1 session，实际 {len(lt_owner1_only)}"
    )
    lt_keys = sorted([(s["subagent"], s["project"]) for s in lt_owner1_only])
    assert lt_keys == [("list_test_sub_a", "p_x"), ("list_test_sub_b", "p_y")]
    # plan B：每行带 astrbot_session_id
    for s in top3:
        assert "astrbot_session_id" in s, f"list 返回应带 astrbot_session_id：{s}"
    print(
        f"  ✅ list_active OK (top3 排序 + lt_owner1 过滤 = {len(lt_owner1_only)} 命中)"
    )


def test_lock_serializes_same_key():
    """同 key 串行——并发 acquire 不会同时跑。"""
    from core.sessions import ProjectSessionManager

    store = _storage.ProjectSessionStore()
    mgr = ProjectSessionManager(store, lock_timeout=5.0)
    order: list[str] = []

    async def call_one(tag: str, hold_ms: int):
        async with mgr.acquire("test_bot", "lock_test", "owner1") as sess:
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold_ms / 1000)
            # plan B：模拟 set_astrbot_session_id（不在 append_history）
            await sess.set_astrbot_session_id(f"uuid-from-{tag}")
            order.append(f"{tag}-exit")

    async def run():
        await asyncio.gather(
            call_one("A", 200),
            call_one("B", 50),
        )

    asyncio.run(run())
    assert order.index("A-enter") < order.index("B-enter"), (
        f"应 A 先 enter，order={order}"
    )
    assert order.index("A-exit") < order.index("B-enter"), (
        f"A 必须先 exit，order={order}"
    )
    # 锁内两个都 set 了，最后一次赢（后者 = B）——验证 last writer wins
    sess_final = store.get_session("test_bot", "lock_test")
    assert sess_final["astrbot_session_id"] == "uuid-from-B", (
        f"应 B 写最终值，实际 {sess_final['astrbot_session_id']}"
    )
    print(f"  ✅ lock serializes OK (order={order})")


def test_lock_parallel_different_keys():
    """不同 key 不互相阻塞。"""
    import time as _time
    from core.sessions import ProjectSessionManager

    store = _storage.ProjectSessionStore()
    mgr = ProjectSessionManager(store, lock_timeout=5.0)

    async def call_one(subagent: str, project: str, hold_ms: int):
        async with mgr.acquire(subagent, project, "owner1") as sess:
            await asyncio.sleep(hold_ms / 1000)
            await sess.set_astrbot_session_id(f"uuid-{subagent}-{project}")

    async def run():
        t0 = _time.time()
        await asyncio.gather(
            call_one("test_bot", "p1", 200),
            call_one("test_bot", "p2", 200),
            call_one("other", "p1", 200),
        )
        elapsed = _time.time() - t0
        assert elapsed < 0.5, f"应并行 (elapsed={elapsed:.2f}s 远超预期)"
        return elapsed

    elapsed = asyncio.run(run())
    print(f"  ✅ parallel different keys OK (3×200ms 并行 elapsed={elapsed:.2f}s)")


def test_lock_timeout():
    """锁等待超时 → SessionLockTimeout。"""
    from core.sessions import ProjectSessionManager, SessionLockTimeout

    store = _storage.ProjectSessionStore()
    mgr = ProjectSessionManager(store, lock_timeout=0.3)

    async def hold_lock():
        async with mgr.acquire("test_bot", "timeout_test", "owner1"):
            await asyncio.sleep(0.5)

    async def try_acquire():
        await asyncio.sleep(0.05)
        async with mgr.acquire("test_bot", "timeout_test", "owner1"):
            pass

    async def run():
        try:
            await asyncio.gather(hold_lock(), try_acquire())
            assert False, "应抛 SessionLockTimeout"
        except SessionLockTimeout as e:
            assert "timeout_test" in str(e)
            print(f"  ✅ lock timeout OK ({e})")
            return

    asyncio.run(run())


def main():
    print("=== 1. DB schema (plan B) ===")
    test_db_schema()
    print("=== 2. UNIQUE 约束 ===")
    test_get_or_create_uniqueness()
    print("=== 3. set_astrbot_session_id ===")
    test_set_astrbot_session_id()
    print("=== 3b. touch_session updates updated_at ===")
    test_touch_session_updates_updated_at_without_changing_sid()
    print("=== 4. delete_by_key ===")
    test_delete_by_key()
    print("=== 5. delete_session by PK ===")
    test_delete_session_by_pk()
    print("=== 6. validate_project_name ===")
    test_project_name_validation()
    print("=== 7. list_active ===")
    test_list_active()
    print("=== 8. lock serializes same key ===")
    test_lock_serializes_same_key()
    print("=== 9. parallel different keys ===")
    test_lock_parallel_different_keys()
    print("=== 10. lock timeout ===")
    test_lock_timeout()
    print()
    print("🎉 全部 10 个测试通过")


if __name__ == "__main__":
    main()
