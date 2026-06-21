"""core/storage.py SubagentStore + TaskLog 集成测试——不依赖 AstrBot 框架。

跑：python tests/test_storage.py from plugin root。

覆盖范围：
1. SubagentStore.upsert：INSERT（必须 token）+ UPDATE（token=None 保留）
2. SubagentStore.list_all / list_enabled / get / delete / set_enabled
3. SubagentStore.seed_from_config：DB 空时导入；非空时跳过
4. TaskLog.insert / get / list_recent / update / delete
"""

import os
import sqlite3
import sys
import tempfile
import time
import types


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

    real_logger = logging.getLogger("subagent_storage_test")
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


TMP_DIR = tempfile.mkdtemp(prefix="subagent_storage_test_")
TEST_DB = os.path.join(TMP_DIR, "test_storage.db")

_stub_astrbot()

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_ROOT)

import core.storage as _storage  # noqa: E402


def _patched_get_db_path():
    p = TEST_DB
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


_storage.get_db_path = _patched_get_db_path
_storage.init_db()


def _reset_db():
    """清空所有表——不删文件，保留 schema。"""
    conn = sqlite3.connect(TEST_DB)
    conn.execute("DELETE FROM subagent_instances")
    conn.execute("DELETE FROM subagent_tasks")
    conn.execute("DELETE FROM project_sessions")
    conn.commit()
    conn.close()


def test_subagent_upsert_insert_requires_token():
    """INSERT 路径：name/base_url 必填；token=None 时返回 False。"""
    store = _storage.SubagentStore()
    assert store.upsert("test_bot", "http://test-bot.local", token=None) is False, (
        "INSERT + token=None 应返回 False（不落库）"
    )
    assert store.get("test_bot") is None, "失败 upsert 不应落行"
    print("  ✅ INSERT + token=None 被拒")


def test_subagent_upsert_insert_full():
    """INSERT 全字段：description / username / verify_ssl 都落库。"""
    store = _storage.SubagentStore()
    ok = store.upsert(
        "test_bot",
        "http://test-bot.local:6185",
        token="sk-test-token-1234567890",
        description="我的测试实例",
        username="weather-bot",
        enabled=True,
        verify_ssl=False,
    )
    assert ok
    row = store.get("test_bot", mask=False)
    assert row is not None
    assert row["base_url"] == "http://test-bot.local:6185"
    assert row["token"] == "sk-test-token-1234567890"
    assert row["description"] == "我的测试实例"
    assert row["username"] == "weather-bot"
    assert row["enabled"] is True
    assert row["verify_ssl"] is False
    # token 脱敏
    masked = store.get("test_bot", mask=True)
    assert masked["token"].startswith("sk-t") and "*" in masked["token"]
    print("  ✅ INSERT 全字段 OK (masked={})".format(masked["token"]))


def test_subagent_upsert_update_preserves_token_when_none():
    """UPDATE 路径：token=None 时**保留**原值，不应被清空。"""
    store = _storage.SubagentStore()
    store.upsert("test_bot", "http://test-bot.local", token="original-token-aaaaaaaaaa")
    original = store.get("test_bot")
    assert original["token"] == "original-token-aaaaaaaaaa"

    ok = store.upsert(
        "test_bot", "http://test-bot.local:6185", token=None, description="update desc"
    )
    assert ok
    row = store.get("test_bot")
    assert row["token"] == "original-token-aaaaaaaaaa", (
        f"UPDATE token=None 应保留原值, got {row['token']!r}"
    )
    assert row["base_url"] == "http://test-bot.local:6185", "UPDATE 应改 base_url"
    assert row["description"] == "update desc", "UPDATE 应改 description"
    print("  ✅ UPDATE + token=None 保留原值")


def test_subagent_upsert_rejects_empty_inputs():
    """name / base_url 空字符串 → 返回 False。"""
    store = _storage.SubagentStore()
    assert store.upsert("", "http://x", token="t") is False
    assert store.upsert("  ", "http://x", token="t") is False
    assert store.upsert("name1", "", token="t") is False
    assert store.upsert("name1", "  ", token="t") is False
    assert store.upsert("name1", "http://x", token="") is False, "空 token 应被拒"
    assert store.upsert("name1", "http://x", token="   ") is False, (
        "纯空格 token 应被拒"
    )
    assert store.get("name1") is None
    print("  ✅ 空 name / base_url / token 全部被拒")


def test_subagent_list_and_enabled():
    """list_all / list_enabled 顺序 + 过滤。"""
    _reset_db()
    store = _storage.SubagentStore()
    store.upsert("alpha", "http://a", token="tk-a", enabled=True)
    store.upsert("bravo", "http://b", token="tk-b", enabled=False)
    store.upsert("charlie", "http://c", token="tk-c", enabled=True)

    all_items = store.list_all()
    assert [x["name"] for x in all_items] == ["alpha", "bravo", "charlie"], (
        f"应按 name 排序: {[x['name'] for x in all_items]}"
    )
    enabled_items = store.list_enabled()
    assert [x["name"] for x in enabled_items] == ["alpha", "charlie"], (
        f"enabled 过滤: {[x['name'] for x in enabled_items]}"
    )
    # bool 字段被转回 Python bool
    for x in all_items:
        assert isinstance(x["enabled"], bool)
        assert isinstance(x["verify_ssl"], bool)
    print(f"  ✅ list_all={len(all_items)} list_enabled={len(enabled_items)}")


def test_subagent_toggle_and_delete():
    """set_enabled + delete。"""
    _reset_db()
    store = _storage.SubagentStore()
    store.upsert("test_bot", "http://test-bot.local", token="tk-h")
    assert store.set_enabled("test_bot", False) is True
    assert store.get("test_bot")["enabled"] is False
    assert store.set_enabled("test_bot", True) is True
    assert store.get("test_bot")["enabled"] is True
    # 不存在
    assert store.set_enabled("ghost", True) is False
    # delete
    assert store.delete("test_bot") is True
    assert store.get("test_bot") is None
    assert store.delete("test_bot") is False, "重复 delete 应返回 False"
    print("  ✅ set_enabled + delete OK")


def test_subagent_seed_from_config():
    """seed_from_config：DB 空时按配置写入；非空时跳过。"""
    # 1) DB 空 → 全部写入
    _reset_db()
    store = _storage.SubagentStore()
    seed_cfg = [
        {"name": "test_bot", "base_url": "http://test-bot.local:6185", "token": "t1"},
        {
            "name": "weather",
            "base_url": "http://weather.local",
            "token": "t2",
            "description": "天气专家",
        },
        {"name": "broken", "base_url": "", "token": "t3"},  # base_url 空 → 跳过
    ]
    n = store.seed_from_config(seed_cfg, global_verify_ssl=False)
    assert n == 2, f"应只写 2 条（broken 被跳），实际 {n}"
    items = {x["name"]: x for x in store.list_all()}
    assert "test_bot" in items and "weather" in items and "broken" not in items
    # global_verify_ssl=False → 新行 verify_ssl=0
    assert items["test_bot"]["verify_ssl"] is False
    # 2) DB 非空 → 第二次 seed 应跳过
    n_skip = store.seed_from_config(
        [{"name": "another", "base_url": "http://a.local", "token": "t"}],
        global_verify_ssl=True,
    )
    assert n_skip == 0, f"非空 DB 应跳过 seed, got {n_skip}"
    assert store.get("another") is None
    # 3) 验证「单 subagent 显式 verify_ssl=True 覆盖 global」需要 DB 空时跑——独立子用例
    _reset_db()
    store2 = _storage.SubagentStore()
    n2 = store2.seed_from_config(
        [
            {
                "name": "with_ssl",
                "base_url": "http://s.local",
                "token": "t",
                "verify_ssl": True,
            },
        ],
        global_verify_ssl=False,
    )
    assert n2 == 1
    with_ssl = store2.get("with_ssl")
    assert with_ssl["verify_ssl"] is True
    print("  ✅ seed_from_config: 空 DB 写入 + 非空跳过 + verify_ssl 覆盖 OK")


def test_task_log_lifecycle():
    """TaskLog.insert → get → update → list_recent → delete。"""
    log = _storage.TaskLog()
    task = {
        "task_id": "sa-test-1",
        "user_id": "user1",
        "subagent": "test_bot",
        "task_text": "查询天气",
        "status": "running",
        "mode": "background",
        "created_at": time.time(),
        "finished_at": None,
        "result_text": None,
        "error_text": None,
    }
    log.insert(task)
    row = log.get("sa-test-1")
    assert row is not None
    assert row["task_text"] == "查询天气"
    assert row["status"] == "running"
    assert row["result_text"] is None

    # update
    log.update(
        {
            "task_id": "sa-test-1",
            "status": "success",
            "finished_at": time.time(),
            "result_text": "晴 25℃",
            "error_text": None,
        }
    )
    row2 = log.get("sa-test-1")
    assert row2["status"] == "success"
    assert row2["result_text"] == "晴 25℃"
    assert row2["finished_at"] is not None

    # list_recent 按 created_at DESC
    log.insert(
        {
            **task,
            "task_id": "sa-test-2",
            "task_text": "2号任务",
            "created_at": time.time() + 0.01,
            "status": "queued",
        }
    )
    rows = log.list_recent(limit=10)
    assert len(rows) == 2
    # 列表只查部分字段（无 result_text）
    keys = set(rows[0].keys())
    assert "task_id" in keys
    assert "result_text" not in keys, "list_recent 不应返回 result_text 大字段"

    # delete
    assert log.delete("sa-test-1") is True
    assert log.get("sa-test-1") is None
    assert log.delete("sa-test-1") is False
    # 不存在的 id
    assert log.get("") is None
    assert log.delete("") is False
    print("  ✅ TaskLog insert/get/update/list/delete OK")


def main():
    print("=== 1. upsert INSERT token=None 被拒 ===")
    test_subagent_upsert_insert_requires_token()
    print("=== 2. upsert INSERT 全字段 ===")
    test_subagent_upsert_insert_full()
    print("=== 3. upsert UPDATE 保留 token ===")
    test_subagent_upsert_update_preserves_token_when_none()
    print("=== 4. upsert 拒绝空输入 ===")
    test_subagent_upsert_rejects_empty_inputs()
    print("=== 5. list / list_enabled 排序 ===")
    test_subagent_list_and_enabled()
    print("=== 6. set_enabled + delete ===")
    test_subagent_toggle_and_delete()
    print("=== 7. seed_from_config ===")
    test_subagent_seed_from_config()
    print("=== 8. TaskLog 生命周期 ===")
    test_task_log_lifecycle()
    print()
    print("🎉 全部 8 个 storage 测试通过")


if __name__ == "__main__":
    main()
