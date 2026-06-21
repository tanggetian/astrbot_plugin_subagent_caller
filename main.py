"""astrbot_plugin_subagent_caller — Star 入口（薄）

把 main.py 的所有非-Star 逻辑都拆到 ``core/`` 子包，本文件只保留：
- SubAgentCaller(Star) 子类
- filter 命令（@filter.command）：/subagent_call /subagent_task /subagent_status /subagent_cancel /subagent_broadcast + 项目 session /subagent_chat /subagent_chat_task /subagent_chat_list /subagent_chat_clear（plan B 删了 /subagent_chat_history——plugin 不存 history 了）
- LLM Tool（@filter.llm_tool）：call_subagent、call_subagent_async、broadcast_to_subagents、list_subagents、get_subagent_task_result、clear_subagent_project
- Web API handler 包装（core/api.py 的薄方法包装，给 register_web_api 用）

所有运行时状态（task_handles、bg_tasks、subagent client 缓存、project session manager 等）
**全部收进 self 实例字段**，不再有 module-level 可变字典。

plan B：项目 session **不**在 plugin 端持久化 history 详情，**只**存
``(subagent, project)`` → 子 AstrBot session_id UUID 映射。每次调用 plugin 把当前
消息 + session_id 发给子 AstrBot，由子 AstrBot 自己的 chat history 累积上下文。
"""

from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

from .core.access import check_allowed
from .core.api import (
    api_cancel_task,
    api_clear_project_session,
    api_delete_subagent,
    api_delete_task,
    api_get_project_session,
    api_get_task,
    api_list_project_sessions,
    api_list_subagents,
    api_list_tasks,
    api_ping_subagent,
    api_toggle_subagent,
    api_upsert_subagent,
)
from .core.client import AstrBotClient
from .core.runner import background_run, extract_send_target
from .core.sessions import (
    DEFAULT_LOCK_TIMEOUT,
    ProjectNameError,
    ProjectSessionManager,
    SessionLockTimeout,
    validate_project_name,
)
from .core.storage import ProjectSessionStore, SubagentStore, TaskLog, init_db
from .core.util import PLUGIN_NAME, digest, new_task_id, sanitize_error, to_bool


def _cfg_get(config: dict, key: str, default):
    """从 AstrBot 配置 dict 取值——兼容 ``{value, description}`` 嵌套格式。"""
    v = config.get(key, default)
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


class SubAgentCaller(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config or {})
        self.context = context
        self.config = config or {}

        # === 全局配置 ===
        self.default_timeout = int(_cfg_get(self.config, "default_timeout", 60))
        self.max_concurrent = int(_cfg_get(self.config, "max_concurrent", 5))
        self.verify_ssl: bool = to_bool(_cfg_get(self.config, "verify_ssl", True), True)
        storage_path_cfg = str(
            _cfg_get(self.config, "storage_path", "data/subagent_tasks.db")
        )
        # 实际 SQLite 路径由 core/storage.get_db_path 决定（放在 data/plugins/<PLUGIN>/ 下），
        # 配置项保留仅为 schema 兼容，不参与路径计算。
        self.storage_path = storage_path_cfg
        self.session_lock_timeout: float = float(
            _cfg_get(self.config, "session_lock_timeout", DEFAULT_LOCK_TIMEOUT),
        )

        # === 访问控制 ===
        ac_raw = _cfg_get(self.config, "access_control", {})
        if not isinstance(ac_raw, dict):
            ac_raw = {}
        self.whitelist_enabled: bool = to_bool(
            ac_raw.get("whitelist_enabled", True), True
        )
        self.allowed_user_ids: set[str] = {
            str(x).strip()
            for x in (ac_raw.get("allowed_user_ids") or [])
            if str(x).strip()
        }
        self.block_when_disabled: bool = to_bool(
            ac_raw.get("block_when_disabled", False), False
        )

        # === 运行时状态 ===
        # 子 AstrBot 实例存储（DB-backed）
        self._subagent_store = SubagentStore()
        # 任务审计 SQLite
        self._task_log = TaskLog()
        # 项目 session 存储 + 管理器
        self._project_session_store = ProjectSessionStore()
        self._project_session_manager = ProjectSessionManager(
            self._project_session_store,
            lock_timeout=self.session_lock_timeout,
        )
        # 内存任务跟踪：task_id -> task_info
        self._bg_tasks: dict[str, dict] = {}
        # asyncio Task 句柄
        self._task_handles: dict[str, asyncio.Task] = {}
        # client 缓存：name -> AstrBotClient（配置变化时失效）
        self._client_cache: dict[str, AstrBotClient] = {}
        # 广播并发信号量
        self._broadcast_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            self.max_concurrent
        )

        # === 初始化 DB + 从配置 seed 子 AstrBot 实例 ===
        init_db()
        seed_items = _cfg_get(self.config, "subagents", []) or []
        if isinstance(seed_items, list):
            written = self._subagent_store.seed_from_config(
                seed_items,
                global_verify_ssl=self.verify_ssl,
            )
            if written > 0:
                logger.info(
                    f"[{PLUGIN_NAME}] 从配置 seed 了 {written} 个子 AstrBot 实例"
                )

        # === 注册 Plugin Page 后端 API ===
        try:
            # 子 AstrBot 实例管理
            context.register_web_api(
                f"/{PLUGIN_NAME}/subagents",
                self._api_list_subagents,
                ["GET"],
                "List registered sub-AstrBot instances",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/subagents/upsert",
                self._api_upsert_subagent,
                ["POST"],
                "Create or update a sub-AstrBot instance",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/subagents/delete",
                self._api_delete_subagent,
                ["POST"],
                "Delete a sub-AstrBot instance",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/subagents/toggle",
                self._api_toggle_subagent,
                ["POST"],
                "Enable or disable a sub-AstrBot instance",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/subagents/ping",
                self._api_ping_subagent,
                ["POST"],
                "Ping a sub-AstrBot instance to test connectivity",
            )
            # 任务管理
            context.register_web_api(
                f"/{PLUGIN_NAME}/tasks",
                self._api_list_tasks,
                ["GET"],
                "List subagent tasks (running + history)",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/tasks/get",
                self._api_get_task,
                ["GET"],
                "Get details of a single task",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/cancel",
                self._api_cancel_task,
                ["POST"],
                "Cancel a running background task",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/delete",
                self._api_delete_task,
                ["POST"],
                "Delete a subagent task",
            )
            # 项目 session 管理（plan B：plugin 只存 (subagent, project) → 子 AstrBot session_id 映射）
            context.register_web_api(
                f"/{PLUGIN_NAME}/project_sessions",
                self._api_list_project_sessions,
                ["GET"],
                "List active project sessions (subagent, project pairs + astrbot session_id)",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/project_sessions/get",
                self._api_get_project_session,
                ["GET"],
                "Get project session header (subagent+project → astrbot_session_id); no messages",
            )
            context.register_web_api(
                f"/{PLUGIN_NAME}/project_sessions/clear",
                self._api_clear_project_session,
                ["POST"],
                "Delete the session_id mapping for (subagent, project). Next call to /subagent_chat with the same key starts a fresh dialog.",
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] register_web_api 失败: {e}")

        # === 就绪性日志 ===
        all_subs = self._subagent_store.list_all(mask=True)
        enabled_count = sum(1 for s in all_subs if s.get("enabled"))
        logger.info(
            f"[{PLUGIN_NAME}] 初始化完成: "
            f"default_timeout={self.default_timeout}s "
            f"max_concurrent={self.max_concurrent} "
            f"subagents_total={len(all_subs)} enabled={enabled_count}"
        )

        if not all_subs:
            logger.warning(
                f"\n[{PLUGIN_NAME}] 尚未注册任何子 AstrBot 实例。\n"
                f"请在 WebUI → 插件管理 → {PLUGIN_NAME} → 任务列表 / 实例管理 中添加，"
                f"或在 _conf_schema.json.subagents 数组里填写初始值后重启。"
            )

    # === helper ===

    def _get_client(self, name: str) -> AstrBotClient | None:
        """按 name 取一个缓存的 client（带 cache invalidation）。

        verify_ssl 决策：子实例字段优先（兼容 DB 老行——空值/缺列都回退到全局），
        否则用 self.verify_ssl。
        """
        sa = self._subagent_store.get(name, mask=False)
        if not sa:
            return None
        if not sa.get("enabled", 1):
            return None
        # verify_ssl 字段可能没有—— .get 兜底 None
        sa_verify = sa.get("verify_ssl", None)
        if sa_verify is None:
            effective_verify = self.verify_ssl
        else:
            effective_verify = bool(sa_verify)
        cached = self._client_cache.get(name)
        # 简单失效：base_url/token/verify_ssl 变了就重造
        if (
            cached
            and cached.base_url == sa["base_url"]
            and cached.token == sa["token"]
            and cached.verify_ssl == effective_verify
        ):
            return cached
        client = AstrBotClient(
            base_url=sa["base_url"],
            token=sa["token"],
            timeout=self.default_timeout,
            verify_ssl=effective_verify,
        )
        self._client_cache[name] = client
        return client

    async def _check_allowed(self, event: AstrMessageEvent) -> bool:
        return await check_allowed(
            self.whitelist_enabled,
            self.allowed_user_ids,
            self.block_when_disabled,
            event,
        )

    def _resolve_sender_id(self, event: AstrMessageEvent | None) -> str:
        if event is None:
            return "anonymous"
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _resolve_call_username(self, name: str, sender_id: str) -> str:
        """解析 client.call() 的 username 字段。

        优先级：
        1. 子 AstrBot 实例的 sa.username（用户在 WebUI 实例管理自己填的被调方 user name）
        2. 主控 sender_id（向后兼容旧行为）
        3. "subagent_caller"（兜底）

        空字符串 username 视为"未配置"——会走到下一级 fallback。
        """
        sa = self._subagent_store.get(name, mask=False)
        sa_username = (sa.get("username") or "").strip() if sa else ""
        return sa_username or sender_id or "subagent_caller"

    # === filter.command: 子 AstrBot 同步调用 ===

    @filter.command("subagent_call")
    async def subagent_call_command(self, event: AstrMessageEvent, prompt: str = ""):
        """手动同步调子 AstrBot 跑 LLM 对话：``/subagent_call <name> <消息>``

        示例：
            /subagent_call claude-bot 帮我写一份 Python 教程
        """
        if not await self._check_allowed(event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/subagent_call <子 AstrBot 名称> <消息内容>\n"
                "  示例：/subagent_call claude-bot 帮我写一份 Python 教程\n\n"
                "如需异步任务，用 /subagent_task <name> <任务>；"
                "如需广播给所有子 AstrBot，用 /subagent_broadcast <消息>。"
            )
            return

        # 解析 [name] 前缀
        name, message = self._parse_name_prompt(prompt)
        if not name:
            yield event.plain_result(
                "提示：请先指定子 AstrBot 名称。\n"
                "  示例：/subagent_call my-bot 帮我写代码\n\n"
                "当前已注册的子 AstrBot（用 /subagent_list 查看）："
            )
            return
        if not message.strip():
            yield event.plain_result(
                f"提示：你输入的 `{name}` 被识别为子 AstrBot 名称。\n"
                f"请在名称后补消息内容，例如：\n"
                f"  /subagent_call {name} 帮我写一份调研报告"
            )
            return

        client = self._get_client(name)
        if not client:
            yield event.plain_result(
                f"❌ 未找到已启用的子 AstrBot：`{name}`\n"
                f"请先在 WebUI 实例管理中添加，或检查是否被禁用。"
            )
            return

        sender_id = self._resolve_sender_id(event)
        logger.info(
            f"[subagent cmd] cmd=/subagent_call subagent={name} "
            f"sender={digest(sender_id)} task_chars={len(message)}"
        )

        yield event.plain_result(f"已转发到子 AstrBot `{name}`：{message[:60]}...")

        try:
            call_username = self._resolve_call_username(name, sender_id)
            result = await client.call(message=message, username=call_username)
            yield event.plain_result(f"\n{result}")
        except Exception as e:
            yield event.plain_result(f"\n[子 AstrBot 调用失败] {sanitize_error(e)}")

    @filter.command("subagent_task")
    async def subagent_task_command(self, event: AstrMessageEvent, prompt: str = ""):
        """手动异步调子 AstrBot：``/subagent_task <name> <任务>``——返回 task_id，跑去后台。

        示例：
            /subagent_task claude-bot 帮我做一次代码扫描
        """
        if not await self._check_allowed(event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/subagent_task <子 AstrBot 名称> <任务>\n"
                "  任务会在后台跑，跑完会主动通知主人\n"
                "  示例：/subagent_task claude-bot 帮我做一次代码扫描\n\n"
                "任务查询：/subagent_status <task_id>\n"
                "任务取消：/subagent_cancel <task_id>"
            )
            return

        name, message = self._parse_name_prompt(prompt)
        if not name:
            yield event.plain_result(
                "提示：请先指定子 AstrBot 名称。\n"
                "  示例：/subagent_task my-bot 帮我跑一个长任务"
            )
            return
        if not message.strip():
            yield event.plain_result(
                f"提示：`{name}` 是子 AstrBot 名称，请在后面补任务内容。\n"
                f"  示例：/subagent_task {name} 帮我做一份调研"
            )
            return

        client = self._get_client(name)
        if not client:
            yield event.plain_result(
                f"❌ 未找到已启用的子 AstrBot：`{name}`\n"
                f"请先在 WebUI 实例管理中添加，或检查是否被禁用。"
            )
            return

        sender_id = self._resolve_sender_id(event)
        task_id = new_task_id("sa")

        # 后台 task 入库
        info = {
            "task_id": task_id,
            "user_id": sender_id,
            "subagent": name,
            "task_text": message,
            "status": "running",
            "mode": "background",
            "created_at": time.time(),
            "finished_at": None,
            "result_text": None,
            "error_text": None,
        }
        self._task_log.insert(info)
        self._bg_tasks[task_id] = info

        # 启动后台协程
        call_username = self._resolve_call_username(name, sender_id)
        task_handle = asyncio.create_task(
            background_run(
                task=message,
                subagent_name=name,
                user_id=sender_id,
                task_id=task_id,
                event=event,
                call_subagent=client.call,
                task_log=self._task_log,
                bg_tasks=self._bg_tasks,
                task_handles=self._task_handles,
                platform_meta=extract_send_target(event),
                context=self.context,
                username=call_username,
            )
        )
        self._task_handles[task_id] = task_handle

        logger.info(
            f"[subagent cmd] cmd=/subagent_task subagent={name} task_id={task_id} "
            f"sender={digest(sender_id)} task_chars={len(message)}"
        )

        yield event.plain_result(
            f"后台任务已提交：{task_id}\n"
            f"  子 AstrBot: {name}\n"
            f"  任务: {message[:60]}...\n"
            f"  跑完会主动通知，不阻塞主人继续聊。\n\n"
            f"  查询：/subagent_status {task_id}\n"
            f"  取消：/subagent_cancel {task_id}"
        )

    @filter.command("subagent_status")
    async def subagent_status_command(self, event: AstrMessageEvent, task_id: str = ""):
        """查询后台任务状态：``/subagent_status <task_id>``"""
        if not await self._check_allowed(event):
            return
        task_id = (task_id or "").strip()
        if not task_id:
            yield event.plain_result(
                "用法：/subagent_status <task_id>\n"
                "  task_id 在提交 /subagent_task 后会返回，形如 sa-1700000000000-xxxxxxxx"
            )
            return
        row = self._task_log.get(task_id)
        if not row:
            yield event.plain_result(f"❌ 未找到任务：`{task_id}`")
            return
        created = row.get("created_at") or 0
        finished = row.get("finished_at")
        elapsed = ""
        if finished:
            elapsed = f"\n耗时：{int(finished - created)}s"
        else:
            elapsed = f"\n已运行：{int(time.time() - created)}s（未结束）"
        yield event.plain_result(
            f"任务状态：{row.get('status', 'unknown')}\n"
            f"  ID: {row.get('task_id')}\n"
            f"  子 AstrBot: {row.get('subagent')}\n"
            f"  任务: {(row.get('task_text') or '')[:80]}\n"
            f"  创建: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created))}"
            f"{elapsed}\n"
            f"  错误: {(row.get('error_text') or '-')[:200]}"
        )

    @filter.command("subagent_cancel")
    async def subagent_cancel_command(self, event: AstrMessageEvent, task_id: str = ""):
        """取消后台任务：``/subagent_cancel <task_id>``"""
        if not await self._check_allowed(event):
            return
        task_id = (task_id or "").strip()
        if not task_id:
            yield event.plain_result("用法：/subagent_cancel <task_id>")
            return
        info = self._bg_tasks.get(task_id)
        if not info:
            yield event.plain_result(
                f"❌ 任务 `{task_id}` 不在运行中或不存在。\n"
                f"提示：已完成/已失败/已取消的任务请用 WebUI 任务列表页面查看。"
            )
            return
        handle = self._task_handles.get(task_id)
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        self._task_log.update(info)
        if handle and not handle.done():
            handle.cancel()
        yield event.plain_result(
            f"✅ 任务 {task_id} 已取消（subagent={info.get('subagent')}）"
        )

    @filter.command("subagent_broadcast")
    async def subagent_broadcast_command(
        self, event: AstrMessageEvent, prompt: str = ""
    ):
        """广播给所有已启用的子 AstrBot：``/subagent_broadcast <消息>``

        内部用 asyncio.gather 并行调用，拼接所有回复。
        """
        if not await self._check_allowed(event):
            return
        message = (prompt or "").strip()
        if not message:
            yield event.plain_result(
                "用法：/subagent_broadcast <消息>\n"
                "  会同时调用所有已启用的子 AstrBot（受 max_concurrent 限制），"
                "  并把所有回复拼成汇总返回。\n"
                "  示例：/subagent_broadcast 自我介绍一下"
            )
            return

        subs = self._subagent_store.list_enabled()
        if not subs:
            yield event.plain_result(
                "❌ 没有已启用的子 AstrBot。\n"
                "请先在 WebUI 实例管理中添加并启用至少一个实例。"
            )
            return

        sender_id = self._resolve_sender_id(event)
        logger.info(
            f"[subagent cmd] cmd=/subagent_broadcast sender={digest(sender_id)} "
            f"subagent_count={len(subs)} task_chars={len(message)}"
        )

        yield event.plain_result(
            f"📡 正在广播给 {len(subs)} 个子 AstrBot：{message[:60]}..."
        )

        async def _call_one(sa: dict) -> tuple[str, str]:
            """返回 (name, reply_or_error)。"""
            client = self._get_client(sa["name"])
            if client is None:
                return sa["name"], "❌ client 不可用（实例被禁用或未找到）"
            sa_username = (sa.get("username") or "").strip()
            call_username = sa_username or sender_id or "subagent_caller"
            try:
                async with self._broadcast_semaphore:
                    reply = await client.call(
                        message=message,
                        username=call_username,
                    )
                return sa["name"], reply
            except Exception as e:
                return sa["name"], f"❌ {sanitize_error(e)}"

        results = await asyncio.gather(
            *(_call_one(sa) for sa in subs),
            return_exceptions=False,
        )

        # 拼接汇总
        chunks = ["📨 广播结果汇总：\n"]
        for name, reply in results:
            chunks.append(f"\n—— 【{name}】 ——\n{reply}")
        summary = "\n".join(chunks)
        # 单条消息太长容易触发平台字数限制——这里不做截断，让上层 message chain 处理
        yield event.plain_result(summary)

    @filter.command("subagent_list")
    async def subagent_list_command(self, event: AstrMessageEvent):
        """列出已注册的子 AstrBot：``/subagent_list``"""
        if not await self._check_allowed(event):
            return
        subs = self._subagent_store.list_all(mask=True)
        if not subs:
            yield event.plain_result(
                "尚未注册任何子 AstrBot。\n"
                "请在 WebUI 实例管理页面添加，或在 _conf_schema.json.subagents 数组里填写初始值。"
            )
            return
        lines = [f"已注册的子 AstrBot（共 {len(subs)} 个）：\n"]
        for sa in subs:
            mark = "✅" if sa.get("enabled") else "⛔"
            lines.append(
                f"  {mark} {sa.get('name')}  base_url={sa.get('base_url')}  token={sa.get('token')}"
            )
            if sa.get("description"):
                lines.append(f"      备注：{sa.get('description')}")
        yield event.plain_result("\n".join(lines))

    # === filter.command: 项目 session 多轮对话（前台同步 + 后台异步）===

    @filter.command("subagent_chat")
    async def subagent_chat_command(self, event: AstrMessageEvent, prompt: str = ""):
        """前台项目 session 多轮对话：``/subagent_chat <name> <project> <消息>``

        plan B：plugin **不**在本地维护 history，**只**存 ``(subagent, project)``
        → 子 AstrBot session_id 的映射。每次调用 plugin 把当前消息 + session_id 发给
        子 AstrBot，由子 AstrBot 自己的 ``(username, session_id)`` chat history
        累积上下文——多轮对话「自动」连贯，不靠 plugin 拼接重发。

        命名规范：project 名只允许字母/数字/下划线/点/横线，长度 1~64。
        同一 subagent 可开多个 project session（不同 project 名互不串扰）。
        同一 (subagent, project) 串行排队——并发同 key 调用会等锁。

        示例：
            /subagent_chat <实例名> weather 杭州今天天气怎么样
            /subagent_chat <实例名> weather 那明天呢
        """
        if not await self._check_allowed(event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/subagent_chat <子 AstrBot 名称> <项目名> <消息内容>\n"
                "  示例：/subagent_chat <实例名> weather 杭州今天天气怎么样\n\n"
                "项目名规则：只允许字母/数字/下划线/点/横线，长度 1~64。\n"
                "  同一子 AstrBot 可开多个 project（不同名互不干扰）。\n"
                "  同一 (子 AstrBot, project) 串行排队，并发同 key 会等锁。\n"
                "  history 由子 AstrBot 自己累积——plugin 不拼 context 重发。\n\n"
                "其他命令：\n"
                "  异步版：/subagent_chat_task <name> <project> <消息>\n"
                "  列表：/subagent_chat_list\n"
                "  删 mapping（强制开新对话）：/subagent_chat_clear <name> <project>"
            )
            return

        # 解析 [name] [project] [message...] 三段式
        name, project, message = self._parse_name_project_prompt(prompt)
        if not name:
            yield event.plain_result(
                "提示：请先指定子 AstrBot 名称。\n"
                "  示例：/subagent_chat <实例名> weather 杭州今天天气怎么样"
            )
            return
        if not project:
            yield event.plain_result(
                f"提示：你输入的 `{name}` 被识别为子 AstrBot 名称。\n"
                f"请在后面补项目名 + 消息，例如：\n"
                f"  /subagent_chat {name} my_topic 帮我做一次调研"
            )
            return
        try:
            project = validate_project_name(project)
        except ProjectNameError as e:
            yield event.plain_result(f"❌ 项目名非法：{e}")
            return
        if not message.strip():
            yield event.plain_result(
                f"提示：项目名 `{project}` 已识别，请在后面补消息内容，例如：\n"
                f"  /subagent_chat {name} {project} 帮我做一次调研"
            )
            return

        client = self._get_client(name)
        if not client:
            yield event.plain_result(
                f"❌ 未找到已启用的子 AstrBot：`{name}`\n"
                f"请先在 WebUI 实例管理中添加，或检查是否被禁用。"
            )
            return

        sender_id = self._resolve_sender_id(event)
        logger.info(
            f"[subagent cmd] cmd=/subagent_chat subagent={name} project={project} "
            f"sender={digest(sender_id)} task_chars={len(message)}"
        )

        # === plan B ===
        # 拿 session 锁 → 拿映射里的 session_id（首次 = 空）→ 发新消息 + session_id 给子 AstrBot
        # → 子 AstrBot SSE 第一个事件返回 session_id → plugin 捕获写回映射
        try:
            async with self._project_session_manager.acquire(
                subagent=name,
                project=project,
                user_id=sender_id,
            ) as sess:
                existing_sid = sess.astrbot_session_id
                call_username = self._resolve_call_username(name, sender_id)
                yield event.plain_result(
                    f"📒 项目 session `{name}::{project}` 正在调子 AstrBot "
                    f"（session_id={'<new>' if not existing_sid else existing_sid[:12] + '...'}"
                    f"，history 在子 AstrBot 那边累积）……"
                )
                try:
                    result = await client.call(
                        message=message,
                        username=call_username,
                        session_id=existing_sid or None,
                    )
                except Exception as call_err:
                    yield event.plain_result(
                        f"\n❌ 调用失败：{sanitize_error(call_err)}"
                    )
                    return
                # CallResult：reply + (可选) 新 session_id
                reply = result.reply if hasattr(result, "reply") else str(result)
                returned_sid = getattr(result, "session_id", None)
                if returned_sid and returned_sid != existing_sid:
                    await sess.set_astrbot_session_id(returned_sid)
                yield event.plain_result(
                    f"\n【项目: {name}::{project} | subagent 回复】\n\n{reply}"
                )
        except SessionLockTimeout as e:
            yield event.plain_result(
                f"❌ {e}\n提示：上一次同 project 调用还没结束（默认等 {self.session_lock_timeout}s）。"
                f"如需强制重置：/subagent_chat_clear {name} {project}"
            )
            return

    @filter.command("subagent_chat_task")
    async def subagent_chat_task_command(
        self, event: AstrMessageEvent, prompt: str = ""
    ):
        """后台项目 session 多轮对话：``/subagent_chat_task <name> <project> <消息>``

        plan B：跟 ``/subagent_chat`` 一样用 plugin 存的 session_id 调子 AstrBot，
        但调用放后台跑——返回 task_id，跑完主动通知。任务在 WebUI 任务列表 tab
        看得到、可以 cancel。history 在子 AstrBot 那边累积，plugin 不存。

        示例：
            /subagent_chat_task <实例名> research 调研一下 LLM 推理优化技术
        """
        if not await self._check_allowed(event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/subagent_chat_task <子 AstrBot 名称> <项目名> <消息>\n"
                "  示例：/subagent_chat_task <实例名> research 调研一下 LLM 推理优化\n\n"
                "和 /subagent_chat 的区别：\n"
                "  /subagent_chat → 前台同步等结果\n"
                "  /subagent_chat_task → 后台跑，返回 task_id，跑完主动通知\n"
                "两者用同一个 (subagent, project) session_id 调子 AstrBot（多轮上下文连贯）。"
            )
            return

        name, project, message = self._parse_name_project_prompt(prompt)
        if not name:
            yield event.plain_result(
                "提示：请先指定子 AstrBot 名称 + 项目名。\n"
                "  示例：/subagent_chat_task <实例名> my_topic 帮我跑一个长任务"
            )
            return
        if not project:
            yield event.plain_result(
                f"提示：`{name}` 是子 AstrBot 名称，请在后面补项目名 + 消息，例如：\n"
                f"  /subagent_chat_task {name} my_topic 帮我做一份调研"
            )
            return
        try:
            project = validate_project_name(project)
        except ProjectNameError as e:
            yield event.plain_result(f"❌ 项目名非法：{e}")
            return
        if not message.strip():
            yield event.plain_result(
                f"提示：项目名 `{project}` 已识别，请在后面补消息内容，例如：\n"
                f"  /subagent_chat_task {name} {project} 帮我做一份调研"
            )
            return

        client = self._get_client(name)
        if not client:
            yield event.plain_result(
                f"❌ 未找到已启用的子 AstrBot：`{name}`\n"
                f"请先在 WebUI 实例管理中添加，或检查是否被禁用。"
            )
            return

        sender_id = self._resolve_sender_id(event)
        task_id = new_task_id("ps")  # ps = project session 区别于 sa
        info = {
            "task_id": task_id,
            "user_id": sender_id,
            "subagent": name,
            "task_text": message,
            "status": "running",
            "mode": "project-background",
            "created_at": time.time(),
            "finished_at": None,
            "result_text": None,
            "error_text": None,
        }
        self._task_log.insert(info)
        self._bg_tasks[task_id] = info

        call_username = self._resolve_call_username(name, sender_id)
        task_handle = asyncio.create_task(
            background_run(
                task=message,
                subagent_name=name,
                user_id=sender_id,
                task_id=task_id,
                event=event,
                call_subagent=client.call,
                task_log=self._task_log,
                bg_tasks=self._bg_tasks,
                task_handles=self._task_handles,
                platform_meta=extract_send_target(event),
                context=self.context,
                username=call_username,
                project=project,
                session_manager=self._project_session_manager,
            )
        )
        self._task_handles[task_id] = task_handle

        logger.info(
            f"[subagent cmd] cmd=/subagent_chat_task subagent={name} project={project} "
            f"task_id={task_id} sender={digest(sender_id)} task_chars={len(message)}"
        )

        yield event.plain_result(
            f"后台项目会话任务已提交：{task_id}\n"
            f"  子 AstrBot: {name}\n"
            f"  项目: {project}\n"
            f"  任务: {message[:60]}...\n"
            f"  跑完会用同一 session_id 调子 AstrBot（续上下文）并主动通知。\n\n"
            f"  查询：/subagent_status {task_id}\n"
            f"  取消：/subagent_cancel {task_id}\n"
            f"  删 mapping（强制开新对话）：/subagent_chat_clear {name} {project}"
        )

    @filter.command("subagent_chat_list")
    async def subagent_chat_list_command(self, event: AstrMessageEvent):
        """列出活跃的 (subagent, project) session：``/subagent_chat_list``"""
        if not await self._check_allowed(event):
            return
        items = self._project_session_manager.list_active(limit=200)
        if not items:
            yield event.plain_result(
                "尚无活跃的项目 session。\n"
                "用 /subagent_chat <name> <project> <消息> 创建第一个。"
            )
            return
        lines = [f"活跃项目 session（共 {len(items)} 个）：\n"]
        for it in items:
            updated = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(it.get("updated_at") or 0),
            )
            astrbot_sid = it.get("astrbot_session_id") or ""
            sid_short = (astrbot_sid[:12] + "…") if astrbot_sid else "<未建立>"
            lines.append(
                f"  • {it.get('subagent')}::{it.get('project')}  "
                f"子 AstrBot session {sid_short}  "
                f"最后更新 {updated}"
            )
        lines.append(
            "\n删 mapping（强制开新对话）：/subagent_chat_clear <name> <project>"
        )
        lines.append(
            "注：history 在子 AstrBot 那边累积，plugin 只存 session_id 映射（plan B）。"
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("subagent_chat_clear")
    async def subagent_chat_clear_command(
        self, event: AstrMessageEvent, prompt: str = ""
    ):
        """删项目 session 映射：``/subagent_chat_clear <name> <project>``

        plan B：plugin **不**存 history，所以「清空」=「删 plugin 这边的 session_id
        映射」。下次同 ``(subagent, project)`` 调一次，子 AstrBot 自动建新 UUID，
        等于强制开新对话（旧 history 留在子 AstrBot 那边，不会丢）。

        如想完整断绝旧 history 联系，去子 AstrBot 那边 WebUI 直接删 session。
        """
        if not await self._check_allowed(event):
            return
        if not prompt.strip():
            yield event.plain_result(
                "用法：/subagent_chat_clear <子 AstrBot 名称> <项目名>\n"
                "  示例：/subagent_chat_clear <实例名> weather\n\n"
                "plan B 语义：删 plugin 这边的 session_id 映射，\n"
                "下次同 key 调用 → 子 AstrBot 自动建新 UUID → 等于强制开新对话。"
            )
            return
        parts = prompt.strip().split()
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/subagent_chat_clear <name> <project>\n"
                "  至少需要 name 和 project 两个参数。"
            )
            return
        name = parts[0]
        project = parts[1]
        try:
            project = validate_project_name(project)
        except ProjectNameError as e:
            yield event.plain_result(f"❌ 项目名非法：{e}")
            return
        ok = self._project_session_manager.delete_session(
            subagent=name,
            project=project,
        )
        if not ok:
            yield event.plain_result(
                f"❌ 未找到项目 session `{name}::{project}`。\n"
                f"提示：用 /subagent_chat_list 看所有活跃 session。"
            )
            return
        yield event.plain_result(
            f"✅ 已删除项目 session 映射 `{name}::{project}`。\n"
            f"下次 /subagent_chat 会从子 AstrBot 那边拿一个新 session_id——"
            f"等于强制开新对话（旧 history 留在子 AstrBot 那边）。"
        )

    # === filter.llm_tool: 让主控 LLM 自主调用子 AstrBot ===

    @filter.llm_tool(name="call_subagent")
    async def call_subagent_tool(
        self,
        event: AstrMessageEvent,
        subagent: str = "",
        message: str = "",
        project: str = "",
    ) -> str:
        """前台同步调子 AstrBot 跑 LLM 对话——阻塞等结果后返回 reply。

        当用户请求需要让另一个 AstrBot 实例处理时，调用本工具：
        - 用户明确说"调用子 bot / 转给小 X / 让另一台 AstrBot 处理"
        - 用户希望用不同的 persona / 模型 / 知识库 / 工具集处理
        - 用户希望 fan-out 到多个子 AstrBot 时，调 broadcast_to_subagents
        - **长任务 / 跑批 / 不想阻塞主对话** 时，用 ``call_subagent_async`` 代替

        项目 session：``project`` 可选——为空时用隐式默认 key ``__default__``，
        实现"不指定项目也连续上下文"；非空时用对应 ``(subagent, project)`` 映射。
        调用 ``call_subagent_async`` 时传相同 ``project`` 即可无缝串上下文。

        白名单：access_control.whitelist_enabled=True 时仅 AstrBot 注入的真实 sender_id 在列表内的用户可调。

        返回值：``{ok, status, subagent, reply, session_id, project}``——
        ``reply`` 是子实例返回的纯文本；``session_id`` 是子实例侧 UUID；
        ``project`` 是实际生效的项目 key（"" 走 ``__default__``）。

        Args:
            subagent (string): 必填。子 AstrBot 名称（必须是已注册并启用的实例）。
            message (string): 必填。完整消息内容。
            project (string): 可选。项目名；空 = 隐式 ``__default__``。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled:
            if not real_sender:
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "missing_sender_id",
                        "reply": "任务未执行：无法确认调用者身份，请重新发送任务。",
                    },
                    ensure_ascii=False,
                )
            if real_sender not in self.allowed_user_ids:
                if not self.block_when_disabled:
                    return json.dumps(
                        {
                            "ok": False,
                            "status": "rejected",
                            "error": "not_in_whitelist",
                            "reply": "任务未执行：你不在子 AstrBot 调用白名单中。",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "forbidden",
                        "reply": "任务未执行。",
                    },
                    ensure_ascii=False,
                )
            sender_id = real_sender
        else:
            sender_id = real_sender or "anonymous"

        subagent = (subagent or "").strip()
        message = (message or "").strip()
        project = (project or "").strip() or "__default__"
        if not subagent or not message:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "subagent and message are required",
                    "reply": "请同时提供 subagent（子 AstrBot 名称）和 message（消息内容）。",
                },
                ensure_ascii=False,
            )

        client = self._get_client(subagent)
        if client is None:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "subagent_not_found_or_disabled",
                    "reply": (
                        f"子 AstrBot `{subagent}` 未注册或被禁用。"
                        f"请先在 WebUI 实例管理中添加并启用，或在 _conf_schema.json.subagents 中配置初始值。"
                    ),
                },
                ensure_ascii=False,
            )

        logger.info(
            f"[subagent tool] tool=call_subagent subagent={subagent} "
            f"mode=sync project={project} sender={digest(sender_id)} task_chars={len(message)}"
        )

        # 同步模式：项目 session 路径（与 call_subagent_async 共享项目维度）
        try:
            call_username = self._resolve_call_username(subagent, sender_id)
            async with self._project_session_manager.acquire(
                subagent=subagent,
                project=project,
                user_id=sender_id,
            ) as sess:
                try:
                    result = await client.call(
                        message=message,
                        username=call_username,
                        session_id=sess.astrbot_session_id or None,
                    )
                except Exception as call_err:
                    return json.dumps(
                        {
                            "ok": False,
                            "status": "failed",
                            "subagent": subagent,
                            "project": project,
                            "error": sanitize_error(call_err),
                            "reply": (
                                f"子 AstrBot 调用失败（`{subagent}::{project}`）："
                                f"{sanitize_error(call_err)}"
                            ),
                        },
                        ensure_ascii=False,
                    )
                reply = result.reply if hasattr(result, "reply") else str(result)
                returned_sid = getattr(result, "session_id", None)
                if returned_sid and returned_sid != sess.astrbot_session_id:
                    await sess.set_astrbot_session_id(returned_sid)
                else:
                    await sess.touch()
            return json.dumps(
                {
                    "ok": True,
                    "status": "done",
                    "subagent": subagent,
                    "project": project,
                    "reply": reply,
                    "session_id": returned_sid,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {
                    "ok": False,
                    "status": "failed",
                    "subagent": subagent,
                    "project": project,
                    "error": sanitize_error(e),
                    "reply": "子 AstrBot 调用失败，详情见 AstrBot 日志。",
                },
                ensure_ascii=False,
            )

    @filter.llm_tool(name="call_subagent_async")
    async def call_subagent_async_tool(
        self,
        event: AstrMessageEvent,
        subagent: str = "",
        message: str = "",
        project: str = "",
    ) -> str:
        """后台异步调子 AstrBot——立即返回 task_id，跑完主动通知。

        与 ``call_subagent`` 的区别**只**在调用方式（sync vs async）：
        - ``call_subagent`` = 同步阻塞等结果
        - ``call_subagent_async`` = 立即返回 ``task_id``，后台跑完推送

        项目 session 行为完全一致：``project`` 为空走默认 ``__default__``；
        非空用对应 ``(subagent, project)`` 映射。
        与前台 ``call_subagent`` 传相同 ``project`` 可无缝串上下文。

        用法：
        - 长任务 / 跑批 / 扫描 / 调研 / 用户不想等
        - 调前台 ``call_subagent`` 拿 reply 后还想追问但不想阻塞，调本工具扔后台

        返回值：``{ok, status, subagent, task_id, project}``——``task_id`` 用来查 ``get_subagent_task_result``。

        Args:
            subagent (string): 必填。子 AstrBot 名称（必须是已注册并启用的实例）。
            message (string): 必填。完整消息内容。
            project (string): 可选。项目名；空 = 隐式 ``__default__``。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled:
            if not real_sender:
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "missing_sender_id",
                        "reply": "任务未执行：无法确认调用者身份，请重新发送任务。",
                    },
                    ensure_ascii=False,
                )
            if real_sender not in self.allowed_user_ids:
                if not self.block_when_disabled:
                    return json.dumps(
                        {
                            "ok": False,
                            "status": "rejected",
                            "error": "not_in_whitelist",
                            "reply": "任务未执行：你不在子 AstrBot 调用白名单中。",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "forbidden",
                        "reply": "任务未执行。",
                    },
                    ensure_ascii=False,
                )
            sender_id = real_sender
        else:
            sender_id = real_sender or "anonymous"

        subagent = (subagent or "").strip()
        message = (message or "").strip()
        project = (project or "").strip() or "__default__"
        if not subagent or not message:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "subagent and message are required",
                    "reply": "请同时提供 subagent（子 AstrBot 名称）和 message（消息内容）。",
                },
                ensure_ascii=False,
            )

        client = self._get_client(subagent)
        if client is None:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "subagent_not_found_or_disabled",
                    "reply": (
                        f"子 AstrBot `{subagent}` 未注册或被禁用。"
                        f"请先在 WebUI 实例管理中添加并启用，或在 _conf_schema.json.subagents 中配置初始值。"
                    ),
                },
                ensure_ascii=False,
            )

        logger.info(
            f"[subagent tool] tool=call_subagent_async subagent={subagent} "
            f"mode=background project={project} sender={digest(sender_id)} task_chars={len(message)}"
        )

        task_id = new_task_id("sa")
        info = {
            "task_id": task_id,
            "user_id": sender_id,
            "subagent": subagent,
            "task_text": message,
            "status": "running",
            "mode": "tool-background",
            "created_at": time.time(),
            "finished_at": None,
            "result_text": None,
            "error_text": None,
        }
        self._task_log.insert(info)
        self._bg_tasks[task_id] = info
        platform_meta = extract_send_target(event) if event is not None else None
        call_username = self._resolve_call_username(subagent, sender_id)
        task_handle = asyncio.create_task(
            background_run(
                task=message,
                subagent_name=subagent,
                user_id=sender_id,
                task_id=task_id,
                event=event,
                call_subagent=client.call,
                task_log=self._task_log,
                bg_tasks=self._bg_tasks,
                task_handles=self._task_handles,
                platform_meta=platform_meta,
                context=self.context,
                username=call_username,
                project=project,
                session_manager=self._project_session_manager,
            )
        )
        self._task_handles[task_id] = task_handle
        return json.dumps(
            {
                "ok": True,
                "status": "submitted",
                "task_id": task_id,
                "subagent": subagent,
                "project": project,
                "message": (
                    f"后台任务已提交（subagent={subagent} / project={project}），"
                    f"用 plugin 存的 session_id 续子 AstrBot 上下文，跑完后主动通知主人"
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="broadcast_to_subagents")
    async def broadcast_to_subagents_tool(
        self,
        event: AstrMessageEvent,
        message: str = "",
    ) -> str:
        """把同一消息广播给所有已启用的子 AstrBot，并行处理后返回汇总。

        触发规则：
        - 用户希望 fan-out / 让多台 AstrBot 各自处理同一任务时调用
        - 用户明确说"广播 / 通知所有 / 让所有 bot 都跑一遍"
        - 短查询（<30 秒）适合用本工具；长任务请用 call_subagent_async 逐个提交

        Args:
            message (string): 必填。要广播给所有子 AstrBot 的消息。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled:
            if real_sender not in self.allowed_user_ids:
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "not_in_whitelist",
                        "reply": "你不在子 AstrBot 调用白名单中。",
                    },
                    ensure_ascii=False,
                )
            sender_id = real_sender
        else:
            sender_id = real_sender or "anonymous"

        message = (message or "").strip()
        if not message:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "message is required",
                    "reply": "请提供要广播的 message。",
                },
                ensure_ascii=False,
            )

        subs = self._subagent_store.list_enabled()
        if not subs:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "no_enabled_subagent",
                    "reply": "没有已启用的子 AstrBot。请先在 WebUI 实例管理中添加。",
                },
                ensure_ascii=False,
            )

        async def _call_one(sa: dict) -> dict:
            client = self._get_client(sa["name"])
            if client is None:
                return {
                    "subagent": sa["name"],
                    "ok": False,
                    "reply": "❌ client 不可用（实例被禁用或未找到）",
                }
            sa_username = (sa.get("username") or "").strip()
            call_username = sa_username or sender_id or "subagent_caller"
            try:
                async with self._broadcast_semaphore:
                    call_result = await client.call(
                        message=message,
                        username=call_username,
                    )
                    reply = (
                        call_result.reply
                        if hasattr(call_result, "reply")
                        else str(call_result)
                    )
                return {"subagent": sa["name"], "ok": True, "reply": reply}
            except Exception as e:
                return {
                    "subagent": sa["name"],
                    "ok": False,
                    "reply": f"❌ {sanitize_error(e)}",
                }

        results = await asyncio.gather(*(_call_one(sa) for sa in subs))
        return json.dumps(
            {
                "ok": True,
                "status": "done",
                "subagent_count": len(results),
                "results": results,
                "summary": "\n\n".join(
                    f"—— 【{r['subagent']}】 ——\n{r['reply']}" for r in results
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="list_subagents")
    async def list_subagents_tool(
        self,
        event: AstrMessageEvent,
    ) -> str:
        """列出当前所有已启用的子 AstrBot 实例，供主控 LLM 选择用。

        触发规则：
        - 用户没说具体要调哪个子 AstrBot、且场景不止一个实例时，先调本工具选实例
        - 用户提到了实例名但你拿不准它是否注册 / 是否启用，调本工具确认
        - 用户要求列「可用的子 agent / 有哪些 bot」，调本工具直接回答

        返回的每个实例含 name / description / username：
        - ``name`` = 调 ``call_subagent`` / ``call_subagent_async`` 时的 ``subagent`` 参数
        - ``description`` = 用户添加实例时填的描述（含 persona / 用途 / 能力），按它匹配最合适
        - ``username`` = 子 AstrBot 上报 sender 列展示的身份（如 "yysy"），可原样转述

        本工具**不**返回 token / base_url 等凭据——LLM 不需要也不该看到。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled:
            if not real_sender:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "missing_sender_id",
                        "reply": "无法确认调用者身份，请重新发送任务。",
                    },
                    ensure_ascii=False,
                )
            if real_sender not in self.allowed_user_ids:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "not_in_whitelist",
                        "reply": "你不在子 AstrBot 调用白名单中。",
                    },
                    ensure_ascii=False,
                )

        try:
            rows = self._subagent_store.list_enabled()
        except Exception as e:
            logger.error(f"list_subagents 读 DB 失败：{e}", exc_info=True)
            return json.dumps(
                {
                    "ok": False,
                    "error": "storage_error",
                    "reply": "读取子 AstrBot 列表失败，请稍后重试。",
                },
                ensure_ascii=False,
            )

        items = [
            {
                "name": r.get("name", ""),
                "description": (r.get("description") or "").strip(),
                "username": (r.get("username") or "").strip(),
            }
            for r in rows
            if r.get("name")
        ]

        if not items:
            return json.dumps(
                {
                    "ok": True,
                    "count": 0,
                    "subagents": [],
                    "reply": (
                        "当前没有任何已启用的子 AstrBot。"
                        "请主人先去 WebUI 实例管理页添加并启用至少一个实例，"
                        "或请用户告诉你可用的实例名。"
                    ),
                },
                ensure_ascii=False,
            )

        names = [i["name"] for i in items]
        return json.dumps(
            {
                "ok": True,
                "count": len(items),
                "subagents": items,
                "reply": (
                    f"当前有 {len(items)} 个已启用子 AstrBot：{', '.join(names)}。"
                    "按 description 选最匹配的实例，用 name 作为 subagent 参数调 call_subagent / call_subagent_async。"
                ),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="get_subagent_task_result")
    async def get_subagent_task_result_tool(
        self,
        event: AstrMessageEvent,
        task_id: str = "",
        max_chars: int = 12000,
    ) -> str:
        """读取子 AstrBot 后台任务结果，供主控 LLM 分析、总结或继续调度。

        触发规则：
        - 用户要求分析、总结、解释子 AstrBot 返回结果时调用本工具读取结果
        - 用户提到 task_id 时按 task_id 读取；未提供时读取最近一个任务结果
        - 如果任务还在 running，应告诉用户结果尚未完成，不要编造结果

        Args:
            task_id (string): 可选。子 AstrBot 任务 ID；留空则读取最近一个任务。
            max_chars (number): 可选。最多返回多少字符，默认 12000，最大 50000。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled and real_sender not in self.allowed_user_ids:
            return json.dumps(
                {
                    "ok": False,
                    "error": "forbidden",
                    "reply": "你不在子 AstrBot 调用白名单中，不能读取任务结果。",
                },
                ensure_ascii=False,
            )

        try:
            limit = int(max_chars or 12000)
        except (TypeError, ValueError):
            limit = 12000
        limit = max(1000, min(limit, 50000))
        task_id_q = (task_id or "").strip()
        row = None
        if task_id_q:
            row = self._task_log.get(task_id_q)
        else:
            # 拉最近一个该 user 的任务
            sender = real_sender
            for info in self._task_log.list_recent(limit=50):
                if info.get("user_id") == sender:
                    # list_recent 不返 user_id / result_text，要 get 一下拿完整
                    full = self._task_log.get(info["task_id"])
                    if full:
                        row = full
                        break
        if not row:
            return json.dumps(
                {
                    "ok": False,
                    "error": "task_not_found",
                    "reply": "未找到可读取的子 AstrBot 任务结果。",
                },
                ensure_ascii=False,
            )
        text = row.get("result_text") or row.get("error_text") or ""
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        return json.dumps(
            {
                "ok": True,
                "task_id": row.get("task_id"),
                "subagent": row.get("subagent"),
                "status": row.get("status"),
                "mode": row.get("mode"),
                "task_text": row.get("task_text"),
                "created_at": row.get("created_at"),
                "finished_at": row.get("finished_at"),
                "truncated": truncated,
                "max_chars": limit,
                "result": text,
                "reply": text if text else "任务尚未产生结果。",
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="clear_subagent_project")
    async def clear_subagent_project_tool(
        self,
        event: AstrMessageEvent,
        subagent: str = "",
        project: str = "",
    ) -> str:
        """删 ``(subagent, project)`` 的 session_id 映射——强制开新对话。

        plan B：plugin **不**存 history，所以「清空」=「删 plugin 这边的
        session_id 映射」。下次同 ``(subagent, project)`` 调一次，子 AstrBot 自动
        建新 UUID，等于强制开新对话（旧 history 留在子 AstrBot 那边，不会丢）。

        适用场景：
        - 用户说「换个话题」/「忘了之前聊的」/「重新开始」
        - 锁等待超时想强制重置
        - 误调了错误的 project 想清掉

        不会触发新 LLM 请求——纯本地 DB 写。

        Args:
            subagent (string): 必填。子 AstrBot 名称。
            project (string): 必填。项目名。
        """
        real_sender = self._resolve_sender_id(event)
        if self.whitelist_enabled:
            if real_sender not in self.allowed_user_ids:
                if not self.block_when_disabled:
                    return json.dumps(
                        {
                            "ok": False,
                            "status": "rejected",
                            "error": "not_in_whitelist",
                            "reply": "你不在子 AstrBot 调用白名单中。",
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "ok": False,
                        "status": "rejected",
                        "error": "forbidden",
                        "reply": "任务未执行。",
                    },
                    ensure_ascii=False,
                )

        subagent = (subagent or "").strip()
        project = (project or "").strip()
        if not subagent or not project:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "subagent and project are required",
                    "reply": "请同时提供 subagent 和 project。",
                },
                ensure_ascii=False,
            )
        try:
            project = validate_project_name(project)
        except ProjectNameError as e:
            return json.dumps(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "invalid_project_name",
                    "reply": f"project 名称非法：{e}",
                },
                ensure_ascii=False,
            )

        ok = self._project_session_manager.delete_session(
            subagent=subagent,
            project=project,
        )
        return json.dumps(
            {
                "ok": ok,
                "status": "cleared" if ok else "not_found",
                "subagent": subagent,
                "project": project,
                "reply": (
                    f"已删除项目 session 映射 `{subagent}::{project}`。"
                    f"下次 call_subagent 会让子 AstrBot 自动建新 UUID，"
                    f"等于强制开新对话（旧 history 留在子 AstrBot 那边，不会丢）。"
                    if ok
                    else f"未找到项目 session `{subagent}::{project}`。"
                ),
            },
            ensure_ascii=False,
        )

    # === Plugin Page Web API 包装（core/api.py 的薄方法包装，给 register_web_api 用） ===

    async def _api_list_subagents(self):
        return await api_list_subagents(self._subagent_store)

    async def _api_upsert_subagent(self):
        return await api_upsert_subagent(
            self._subagent_store,
            global_verify_ssl=self.verify_ssl,
        )

    async def _api_delete_subagent(self):
        result = await api_delete_subagent(self._subagent_store)
        self._client_cache.clear()
        return result

    async def _api_toggle_subagent(self):
        result = await api_toggle_subagent(self._subagent_store)
        self._client_cache.clear()
        return result

    async def _api_ping_subagent(self):
        return await api_ping_subagent(
            self._subagent_store,
            global_verify_ssl=self.verify_ssl,
        )

    async def _api_list_tasks(self):
        return await api_list_tasks(self._task_log)

    async def _api_get_task(self):
        return await api_get_task(self._task_log)

    async def _api_cancel_task(self):
        return await api_cancel_task(self._task_log, self._bg_tasks, self._task_handles)

    async def _api_delete_task(self):
        return await api_delete_task(self._task_log, self._bg_tasks, self._task_handles)

    async def _api_list_project_sessions(self):
        return await api_list_project_sessions(self._project_session_manager)

    async def _api_get_project_session(self):
        return await api_get_project_session(self._project_session_manager)

    async def _api_clear_project_session(self):
        return await api_clear_project_session(self._project_session_manager)

    # === 内部 helper ===

    @staticmethod
    def _parse_name_prompt(prompt: str) -> tuple[str, str]:
        """解析 ``<name> <message>`` 格式。

        规则：
        - 第一个空白分隔的 token 是 name（只要非空都当成 name——子 AstrBot 名称允许任意）
        - 剩余部分作为 message
        - 如果 prompt 只有一个 token（没有空白），name 有值但 message 为空
        """
        stripped = (prompt or "").strip()
        if not stripped:
            return "", ""
        if " " not in stripped and "\n" not in stripped and "\t" not in stripped:
            return stripped, ""
        parts = stripped.split(maxsplit=1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    @staticmethod
    def _parse_name_project_prompt(prompt: str) -> tuple[str, str, str]:
        """解析 ``<name> <project> <message>`` 三段式——用于 ``/subagent_chat*`` 命令。

        规则：
        - 第一个 token = name（必填）
        - 第二个 token = project（必填；由 validate_project_name 进一步校验字符）
        - 剩余 = message（可空——空时返回 "" 让 caller 报「请补消息」）
        """
        stripped = (prompt or "").strip()
        if not stripped:
            return "", "", ""
        parts = stripped.split(maxsplit=2)
        if len(parts) < 2:
            return parts[0] if parts else "", "", ""
        if len(parts) == 2:
            return parts[0], parts[1], ""
        return parts[0], parts[1], parts[2]
