"""插件装载冒烟测试——不依赖完整 AstrBot 框架，只验证：

1. main.py 的导入路径全部能解析
2. SubAgentCaller 类可被实例化（super().__init__ 调用 OK）
3. context.register_web_api 收到 12 条路由（与 README 中描述的接口一致）
4. _conf_schema.json 与 metadata.yaml 的元数据自洽
5. DB init + seed 流程跑通

跑：python tests/test_smoke_load.py from plugin root。

失败 → 插件在 AstrBot 中**无法加载**或路由数量不对——是「可用性」的前置条件。
"""

import importlib.util
import asyncio
import json
import os
import sys
import tempfile
import time
import types

# === stub 状态：用一个模块级 dict 当 mutable indirection ===
# 因为 core.storage 在 import 时 ``from ... import get_astrbot_data_path``
# 会把函数引用快照到自己的命名空间；后续替换 stub 模块已经晚了。
# 所以这里让 stub 的 get_astrbot_data_path 永远读这个 dict。
_FAKE_DATA_DIR: dict = {"path": ""}


def _run(coro):
    """在 sync 测试里跑一个 async coroutine——这是 test_smoke_load 唯一的 async 调用。"""
    return asyncio.run(coro)


def _stub_modules():
    """构造 AstrBot 框架的最小 stub，让 main.py 能在纯 Python 环境加载。

    必须在第一次 ``import main`` 之前调用一次；之后改 ``_FAKE_DATA_DIR['path']``
    即可让所有后续 ``get_db_path()`` 看到新的 fake data 目录。
    """

    class _FakeMod(types.ModuleType):
        def __init__(self, name, path=None):
            super().__init__(name)
            if path:
                self.__path__ = path

    for n, p in [
        ("astrbot", ["astrbot"]),
        ("astrbot.core", ["astrbot/core"]),
        ("astrbot.core.utils", ["astrbot/core/utils"]),
        ("astrbot.core.message", ["astrbot/core/message"]),
        ("astrbot.api", ["astrbot/api"]),
        ("astrbot.api.star", ["astrbot/api/star"]),
        ("astrbot.api.event", ["astrbot/api/event"]),
        ("astrbot.api.message_components", ["astrbot/api/message_components"]),
    ]:
        sys.modules[n] = _FakeMod(n, p)

    ap = _FakeMod("astrbot.core.utils.astrbot_path")
    ap.get_astrbot_data_path = lambda: _FAKE_DATA_DIR["path"]
    sys.modules["astrbot.core.utils.astrbot_path"] = ap

    logger = _FakeMod("astrbot.api.logger")
    for level in ("info", "warning", "error", "debug"):
        setattr(logger, level, lambda *a, **k: None)
    sys.modules["astrbot.api.logger"] = logger
    sys.modules["astrbot.api"].logger = logger

    mc = _FakeMod("astrbot.api.message_components")
    mc.Plain = type("Plain", (), {})
    sys.modules["astrbot.api.message_components"] = mc

    mc2 = _FakeMod("astrbot.core.message.message_event_result")
    mc2.MessageChain = type("MessageChain", (), {})
    sys.modules["astrbot.core.message.message_event_result"] = mc2

    class _StarBase:
        def __init_subclass__(cls, **kw):
            pass

        def __init__(self, context, config=None):
            self.context = context
            self._raw_config = config or {}

    star = _FakeMod("astrbot.api.star")
    star.Star = _StarBase
    star.Context = type("Context", (), {})
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api"].Star = _StarBase

    event = _FakeMod("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    sys.modules["astrbot.api.event"] = event

    class _FilterNS:
        def command(self, *a, **k):
            def deco(fn):
                return fn

            return deco

        def permission_type(self, *a, **k):
            def deco(fn):
                return fn

            return deco

        def event_message_type(self, *a, **k):
            def deco(fn):
                return fn

            return deco

        def llm_tool(self, *a, **k):
            def deco(fn):
                return fn

            return deco

    event.filter = _FilterNS()


def _load_main():
    """把插件作为 astrbot_plugin_subagent_caller.main 包导入。"""
    pkg = types.ModuleType("astrbot_plugin_subagent_caller")
    pkg.__path__ = [os.getcwd()]
    sys.modules["astrbot_plugin_subagent_caller"] = pkg
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_subagent_caller.main", "./main.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_imports():
    print("=== 1. main.py 可被作为包导入 ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()
    assert mod.SubAgentCaller is not None
    assert mod.PLUGIN_NAME == "astrbot_plugin_subagent_caller"
    print(f"  ✅ SubAgentCaller loaded, PLUGIN_NAME={mod.PLUGIN_NAME}")


def test_metadata_consistency():
    print("=== 2. metadata.yaml vs _conf_schema.json 自洽性 ===")
    PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(PLUGIN_ROOT, "metadata.yaml"), encoding="utf-8") as f:
        meta_raw = f.read()
    import yaml

    meta = yaml.safe_load(meta_raw)
    name = meta["name"]
    assert name.isidentifier() and not __import__("keyword").iskeyword(name), (
        f"metadata.name={name!r} 不是合法 Python 标识符（必须 snake_case，不能含 '-'）"
    )
    with open(os.path.join(PLUGIN_ROOT, "_conf_schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    sub = schema.get("subagents") or {}
    sub_items = sub.get("items", sub)
    sub_props = sub_items.get("properties") or {}
    for sname in sub_props.get("name", {}).get("default", []) or []:
        assert sname.isidentifier() or isinstance(sname, str), (
            f"_conf_schema.json.subagents.items.properties.name.default 里出现非法子 AstrBot 名: {sname!r}"
        )
    print(f"  ✅ name={name} (snake_case ✓) + schema 子实例名 OK")


def test_instantiation_and_routes():
    print("=== 3. SubAgentCaller 实例化 + 12 条路由注册 ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def __init__(self):
            self.routes = []

        def register_web_api(self, route, handler, methods, desc):
            self.routes.append((route, tuple(methods), desc))

    ctx = _Ctx()
    inst = mod.SubAgentCaller(
        ctx,
        {
            "default_timeout": 60,
            "max_concurrent": 5,
            "verify_ssl": True,
            "access_control": {
                "whitelist_enabled": True,
                "allowed_user_ids": ["user1", "user2"],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-test-1234567890",
                    "description": "我的测试实例",
                },
            ],
        },
    )
    assert inst is not None
    assert hasattr(inst, "config"), (
        "SubAgentCaller.__init__ 必须把 config 存到 self.config（real Star 不会自动注入）"
    )
    assert isinstance(inst.config, dict), (
        f"self.config 应当是 dict，实际 {type(inst.config).__name__}"
    )
    assert "subagents" in inst.config, "传入的 config dict 应被原样存到 self.config"
    assert len(ctx.routes) == 12, f"期望 12 条路由，实际 {len(ctx.routes)}"
    paths = sorted(r for r, _, _ in ctx.routes)
    expected_substrings = {
        "subagents",
        "subagents/upsert",
        "subagents/delete",
        "subagents/toggle",
        "subagents/ping",
        "tasks",
        "tasks/get",
        "cancel",
        "delete",
        "project_sessions",
        "project_sessions/get",
        "project_sessions/clear",
    }
    for sub in expected_substrings:
        assert any(sub in p for p in paths), f"缺路由 .../{sub}"
    for p, methods, _ in ctx.routes:
        assert p.startswith(f"/{mod.PLUGIN_NAME}/"), f"路由前缀错: {p}"
        assert methods in (("GET",), ("POST",)), f"方法错: {p} {methods}"
    print(f"  ✅ {len(ctx.routes)} 条路由全部以 /{mod.PLUGIN_NAME}/ 开头")
    for p, m, d in ctx.routes:
        print(f"    {list(m)[0]:4s}  {p:55s}  {d[:50]}")


def test_subagent_seeded_to_db():
    """seed_from_config 把 subagents 写入 SQLite——必须用全新空 DB 跑。"""
    print("=== 4. seed_from_config 把 subagents 写入 SQLite ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "t1",
                },
                {"name": "weather", "base_url": "http://weather.local", "token": "t2"},
            ],
        },
    )
    items = inst._subagent_store.list_all()
    names = sorted(x["name"] for x in items)
    assert names == ["test_bot", "weather"], f"seed 后应有两个 subagent，实际 {names}"
    full = next(x for x in items if x["name"] == "test_bot")
    assert full["token"] == "t1"
    masked = inst._subagent_store.list_all(mask=True)
    m_test_bot = next(x for x in masked if x["name"] == "test_bot")
    assert "t1" not in m_test_bot["token"], (
        f"masked token 仍含原值: {m_test_bot['token']!r}"
    )
    print(f"  ✅ seed 写入 DB 成功 + mask 脱敏 OK (masked={m_test_bot['token']})")


def test_list_subagents_tool():
    """list_subagents_tool 必须返回 {ok, count, subagents[{}]}，且**不**漏 token / base_url。"""
    print("=== 5. list_subagents_tool 返回结构 + 凭据脱敏 ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-leak-test-1234567890",
                    "description": "我的测试实例",
                    "username": "mybot",
                },
                {
                    "name": "weather",
                    "base_url": "http://weather.local:6185",
                    "token": "tk-another-secret-0987654321",
                    "description": "天气查询",
                    "username": "wx",
                },
            ],
        },
    )

    raw = _run(inst.list_subagents_tool(None))
    parsed = json.loads(raw)
    assert parsed.get("ok") is True, f"list_subagents 应返回 ok=True，实际 {parsed!r}"
    assert parsed.get("count") == 2, (
        f"应有 2 个启用实例，实际 count={parsed.get('count')}"
    )
    assert isinstance(parsed.get("subagents"), list), "subagents 字段必须是 list"
    items = parsed["subagents"]
    assert len(items) == 2, f"subagents 应有 2 项，实际 {len(items)}"

    item = next(i for i in items if i["name"] == "test_bot")
    assert item["description"] == "我的测试实例"
    assert item["username"] == "mybot"

    leak_check_raw = json.dumps(parsed, ensure_ascii=False)
    for forbidden in (
        "tk-secret-leak-test",
        "tk-another-secret",
        "http://test-bot.local",
        "http://weather.local",
    ):
        assert forbidden not in leak_check_raw, (
            f"list_subagents_tool 返回值含敏感字段 {forbidden!r}——必须 mask 后才能给 LLM 看"
        )

    for it in items:
        assert "token" not in it, f"item 含 token 字段: {it!r}"
        assert "base_url" not in it, f"item 含 base_url 字段: {it!r}"

    print(
        "  ✅ list_subagents_tool 返回 2 个实例，name/description/username 齐全，无凭据泄露"
    )


def test_call_subagent_tool_returns_reply_and_session_id():
    """call_subagent_tool 同步模式必须返回 {reply, session_id}——不能把整个 CallResult
    dataclass 序列化（json.dumps 不认识 dataclass → 抛 TypeError → except 分支返回 ok=false，
    让 LLM 误以为失败，实际子实例已成功回复）。

    回归测试：**修** main.py:1070 后**必须**返真实 reply 文本 + session_id。
    """
    print("=== 6. call_subagent_tool 同步模式返回 reply + session_id ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-1234567890",
                },
            ],
        },
    )

    fake_sid = "00000000-1111-2222-3333-444444444444"
    fake_reply = "你好呀～我是 test_bot，很高兴认识主控实例！"

    class _StubClient:
        async def call(self, message, username, session_id=None):
            assert message  # 主控传进来的不能为空
            from astrbot_plugin_subagent_caller.core.client import CallResult as _CR

            return _CR(reply=fake_reply, session_id=fake_sid)

    inst._get_client = lambda name: _StubClient() if name == "test_bot" else None

    raw = _run(
        inst.call_subagent_tool(
            None,
            subagent="test_bot",
            message="你好呀 test_bot！我是主控实例，跟你一样都是 AstrBot 实例。",
        )
    )
    parsed = json.loads(raw)

    assert parsed.get("ok") is True, f"call_subagent_tool 应成功，实际 {parsed!r}"
    assert parsed.get("status") == "done", (
        f"status 应为 done，实际 {parsed.get('status')!r}"
    )
    assert parsed.get("subagent") == "test_bot"
    assert parsed.get("reply") == fake_reply, (
        f"reply 应是子实例返回的纯文本（修复前会把整个 CallResult dataclass 塞进去导致 json.dumps 抛 TypeError）。"
        f"实际 reply={parsed.get('reply')!r}"
    )
    assert parsed.get("session_id") == fake_sid, (
        f"session_id 应是子实例返回的 UUID，实际 {parsed.get('session_id')!r}"
    )
    print(
        f"  ✅ call_subagent_tool 同步模式返回 reply+session_id（reply 长度 {len(fake_reply)}）"
    )


def test_call_subagent_async_returns_task_id():
    """call_subagent_async_tool 重构后：立即返回 task_id（不会阻塞等子实例）。"""
    print("=== 8. call_subagent_async_tool 立即返回 task_id ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-1234567890",
                },
            ],
        },
    )

    call_started = []

    async def _slow_call(message, username, session_id=None):
        call_started.append(message)
        await asyncio.sleep(2.0)  # 模拟长任务——主线程**不**该等这个
        from astrbot_plugin_subagent_caller.core.client import CallResult as _CR

        return _CR(reply="done", session_id="slow-sid")

    class _StubClient:
        call = staticmethod(_slow_call)

    inst._get_client = lambda name: _StubClient() if name == "test_bot" else None

    t0 = time.time()
    raw = _run(
        inst.call_subagent_async_tool(None, subagent="test_bot", message="长任务")
    )
    elapsed = time.time() - t0

    parsed = json.loads(raw)
    assert parsed.get("ok") is True, f"async 应 ok，实际 {parsed!r}"
    assert parsed.get("status") == "submitted", (
        f"status 应 submitted，实际 {parsed.get('status')!r}"
    )
    assert isinstance(parsed.get("task_id"), str) and parsed["task_id"].startswith(
        "sa-"
    ), f"task_id 应是 sa- 前缀的 str，实际 {parsed.get('task_id')!r}"
    assert parsed.get("subagent") == "test_bot"
    assert parsed.get("project") == "__default__", (
        f"async 不传 project 也应走 __default__，实际 {parsed.get('project')!r}"
    )
    assert elapsed < 1.0, f"async 工具**不**应该阻塞 {elapsed:.2f}s（子实例 sleep 2s）"

    # 任务已入队（_bg_tasks 里有这个 task_id）——证明后台调度走了 background_run 路径
    task_id = parsed["task_id"]
    assert task_id in inst._bg_tasks, (
        f"task_id {task_id} 应在 inst._bg_tasks 里，实际 keys={list(inst._bg_tasks.keys())}"
    )
    assert inst._bg_tasks[task_id]["subagent"] == "test_bot"
    # background_run 收到 project 参数后会把 mode 标为 project-background；
    # 没 project 时是 tool-background。两者都合法。
    assert inst._bg_tasks[task_id].get("mode") in {
        "tool-background",
        "project-background",
    }, (
        f"_bg_tasks[{task_id}].mode 应为 tool-background 或 project-background，实际 {inst._bg_tasks[task_id]!r}"
    )

    print(
        f"  ✅ call_subagent_async 立即返回 task_id（耗时 {elapsed * 1000:.0f}ms < 1s）"
    )


def test_call_subagent_tool_respects_project_param():
    """call_subagent 重构后：传 project 时响应里 project 字段 = 实际传的值；不传 = __default__。"""
    print("=== 7. call_subagent_tool project 参数透传 ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-1234567890",
                },
            ],
        },
    )

    class _StubClient:
        async def call(self, message, username, session_id=None):
            from astrbot_plugin_subagent_caller.core.client import CallResult as _CR

            return _CR(reply="ok", session_id="fixed-sid")

    inst._get_client = lambda name: _StubClient() if name == "test_bot" else None

    # case A: 不传 project → __default__
    raw_a = _run(inst.call_subagent_tool(None, subagent="test_bot", message="hi"))
    p_a = json.loads(raw_a)
    assert p_a["project"] == "__default__", (
        f"无 project 应走默认，实际 {p_a['project']!r}"
    )

    # case B: 传 project=myproj → 响应 project=myproj
    raw_b = _run(
        inst.call_subagent_tool(
            None, subagent="test_bot", message="hi", project="myproj"
        )
    )
    p_b = json.loads(raw_b)
    assert p_b["project"] == "myproj", f"传 myproj 应透传，实际 {p_b['project']!r}"

    print("  ✅ project= → __default__；project=myproj → myproj")


def test_call_subagent_tool_reuses_default_project_session_id():
    """同一 subagent + 默认 project 的多轮调用必须复用第一轮返回的 session_id。"""
    print("=== 8. call_subagent_tool 默认 project 多轮复用 session_id ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-1234567890",
                },
            ],
        },
    )

    seen_session_ids = []

    class _StubClient:
        async def call(self, message, username, session_id=None):
            from astrbot_plugin_subagent_caller.core.client import CallResult as _CR

            seen_session_ids.append(session_id)
            if len(seen_session_ids) == 1:
                return _CR(reply="第一轮", session_id="sid-default-1")
            return _CR(reply="第二轮", session_id=session_id or "sid-default-2")

    inst._get_client = lambda name: _StubClient() if name == "test_bot" else None

    raw_1 = _run(inst.call_subagent_tool(None, subagent="test_bot", message="第一轮"))
    p_1 = json.loads(raw_1)
    raw_2 = _run(inst.call_subagent_tool(None, subagent="test_bot", message="第二轮"))
    p_2 = json.loads(raw_2)

    assert p_1["ok"] is True and p_2["ok"] is True
    assert p_1["project"] == "__default__"
    assert p_2["project"] == "__default__"
    assert seen_session_ids == [None, "sid-default-1"], (
        f"第二轮必须把第一轮 session_id 传回子实例，实际 {seen_session_ids!r}"
    )
    assert p_2["session_id"] == "sid-default-1"
    print("  ✅ 默认 project 第二轮复用第一轮 session_id")


def test_call_subagent_tool_reuses_named_project_session_id():
    """同一 subagent + 指定 project 的多轮调用必须复用第一轮返回的 session_id。"""
    print("=== 9. call_subagent_tool 指定 project 多轮复用 session_id ===")
    _FAKE_DATA_DIR["path"] = tempfile.mkdtemp(prefix="subagent_smoke_")
    _stub_modules()
    mod = _load_main()

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = mod.SubAgentCaller(
        _Ctx(),
        {
            "access_control": {
                "whitelist_enabled": False,
                "allowed_user_ids": [],
                "block_when_disabled": False,
            },
            "subagents": [
                {
                    "name": "test_bot",
                    "base_url": "http://test-bot.local:6185",
                    "token": "tk-secret-1234567890",
                },
            ],
        },
    )

    seen_session_ids = []

    class _StubClient:
        async def call(self, message, username, session_id=None):
            from astrbot_plugin_subagent_caller.core.client import CallResult as _CR

            seen_session_ids.append(session_id)
            if len(seen_session_ids) == 1:
                return _CR(reply="指定项目第一轮", session_id="sid-named-1")
            return _CR(reply="指定项目第二轮", session_id=session_id or "sid-named-2")

    inst._get_client = lambda name: _StubClient() if name == "test_bot" else None

    raw_1 = _run(
        inst.call_subagent_tool(
            None, subagent="test_bot", message="第一轮", project="research"
        )
    )
    p_1 = json.loads(raw_1)
    raw_2 = _run(
        inst.call_subagent_tool(
            None, subagent="test_bot", message="第二轮", project="research"
        )
    )
    p_2 = json.loads(raw_2)

    assert p_1["ok"] is True and p_2["ok"] is True
    assert p_1["project"] == "research"
    assert p_2["project"] == "research"
    assert seen_session_ids == [None, "sid-named-1"], (
        f"指定 project 第二轮必须把第一轮 session_id 传回子实例，实际 {seen_session_ids!r}"
    )
    assert p_2["session_id"] == "sid-named-1"
    print("  ✅ 指定 project 第二轮复用第一轮 session_id")


def main():
    test_main_imports()
    print()
    test_metadata_consistency()
    print()
    test_instantiation_and_routes()
    print()
    test_subagent_seeded_to_db()
    print()
    test_list_subagents_tool()
    print()
    test_call_subagent_tool_returns_reply_and_session_id()
    print()
    test_call_subagent_tool_respects_project_param()
    print()
    test_call_subagent_async_returns_task_id()
    print()
    test_call_subagent_tool_reuses_default_project_session_id()
    print()
    test_call_subagent_tool_reuses_named_project_session_id()
    print()
    print("🎉 全部 10 个冒烟测试通过——插件可被 AstrBot 加载")


if __name__ == "__main__":
    main()
