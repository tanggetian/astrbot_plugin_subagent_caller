"""项目 session 多轮对话——以 ``(subagent, project)`` 为 key，**复用**子 AstrBot 自己的 session。

设计：
- plugin **不**持久化 history 详情（不存 user/assistant 消息内容）
- plugin **只**存 ``(subagent, project) → 子 AstrBot session_id`` 的映射
- 第一次调用：plugin 不传 session_id → 子 AstrBot ``/api/v1/chat`` 自动创建 UUID →
  SSE ``type=session_id`` 事件返回 → plugin 捕获并写回映射
- 后续调用：plugin 把存的 UUID 作为 ``session_id`` 传给子 AstrBot → 子 AstrBot 按
  ``(username, session_id)`` 复用同一段对话，history **完全在子 AstrBot 自己**累积

并发安全：每个 ``(subagent, project)`` 一个 ``asyncio.Lock``，懒创建
- 锁内做「读映射 → 调 subagent → 写回 session_id」原子序列
- 同 key 多次并发调用排队；不同 key 互不干扰
- ``_locks`` 字典只在 asyncio 事件循环里被改，单协程模型下不需要元锁保护字典本身

API：
- ``ProjectSessionManager.acquire(subagent, project, user_id)`` → ``ProjectSession``
- ``ProjectSession.session_id`` → 子 AstrBot 端的 UUID（空字符串表示「还没建过」）
- ``ProjectSession.set_astrbot_session_id(astrbot_session_id)`` → 第一次拿到 UUID 后写回
- ``ProjectSessionManager.list_active()`` / ``get_header()``（无锁展示用）/ ``delete_session()``

错误：``ProjectNameError``（project 非法）、``SessionLockTimeout``（锁等待超时）

为什么单独建一个 ``core/sessions.py`` 而不是塞 ``storage.py``：
- storage.py 是「纯 DB CRUD 适配层」风格，零业务逻辑
- sessions.py 放「业务逻辑」：lock 协议、validate、session 缓存
- main.py 调 sessions.py；sessions.py 调 storage.py——分层清晰
"""

from __future__ import annotations

import asyncio
import re
from typing import Any


from .storage import ProjectSessionStore

# === 错误类型 ===


class ProjectNameError(ValueError):
    """project 名称非法——只允许字母数字下划线点横线，长度 1~64。"""


class SessionLockTimeout(RuntimeError):
    """同 ``(subagent, project)`` 的另一个调用还在跑，等锁超时。"""


# === 常量 ===

# 锁等待超时：默认 30 秒（同步调用场景——慢的 LLM 也能 cover；超时说明有死锁或调用太慢）
DEFAULT_LOCK_TIMEOUT = 30.0

# project 名合法字符：字母 / 数字 / 下划线 / 点 / 横线
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def validate_project_name(project: str) -> str:
    """校验 project 名合法性。返回 stripped 值；非法抛 ``ProjectNameError``。"""
    if project is None:
        raise ProjectNameError("project 必填")
    p = str(project).strip()
    if not _PROJECT_NAME_RE.match(p):
        raise ProjectNameError(
            "project 名称非法——只允许字母 / 数字 / 下划线 / 点 / 横线，长度 1~64"
        )
    return p


# === ProjectSession：一次「拿锁 + 读映射 + 调 subagent + 写回 session_id」的事务对象 ===


class ProjectSession:
    """一个 ``(subagent, project)`` session 的「事务对象」。

    用法（由 ``ProjectSessionManager.acquire`` 创建，async with 协议）：
        async with manager.acquire(subagent, project, user_id) as sess:
            result = await client.call(
                message="...",
                username="...",
                session_id=sess.astrbot_session_id or None,
            )
            if result.session_id and result.session_id != sess.astrbot_session_id:
                await sess.set_astrbot_session_id(result.session_id)

    实现细节：
    - 拿锁时机：``__aenter__``；放锁时机：``__aexit__``
    - ``astrbot_session_id`` 在拿锁时从 DB 读一次（snapshot）
    - 调子 AstrBot 拿到返回值后，**新**的 session_id 写回 DB（异步 + to_thread）
    """

    def __init__(
        self,
        *,
        subagent: str,
        project: str,
        user_id: str,
        session_header: dict[str, Any],
        store: ProjectSessionStore,
    ):
        self._subagent = subagent
        self._project = project
        self._user_id = user_id
        self._header = session_header
        self._store = store
        # 拿锁时的快照——本次 session 期间可能因为 set_astrbot_session_id 变化
        self._astrbot_session_id_at_acquire: str = str(
            session_header.get("astrbot_session_id") or ""
        )

    # --- 读 ---

    @property
    def session_id(self) -> str:
        """plugin 内部 PK（``ps-...``）——主要用于 audit / 日志。"""
        return self._header["session_id"]

    @property
    def subagent(self) -> str:
        return self._subagent

    @property
    def project(self) -> str:
        return self._project

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def astrbot_session_id(self) -> str:
        """拿锁时快照的子 AstrBot session_id——空字符串表示「首次调用，子 AstrBot 会自动建 UUID」。"""
        return self._astrbot_session_id_at_acquire

    @property
    def is_first_call(self) -> bool:
        """是否是第一次调（plugin 这边还没存过子 AstrBot session_id）"""
        return not self._astrbot_session_id_at_acquire

    # --- 写 ---

    async def set_astrbot_session_id(self, astrbot_session_id: str) -> bool:
        """把子 AstrBot 那边返回的 session_id UUID 写回映射表。

        通常在第一次调用拿到 SSE ``type=session_id`` 事件后调用。
        同 key 后续调用会拿到同一个 UUID（子 AstrBot ``(username, session_id)`` 唯一），所以
        这个方法只在第一次调用时**实际**写 DB，后续调用 noop。

        Args:
            astrbot_session_id: 子 AstrBot SSE 事件返回的 UUID；
                空字符串表示「清空映射」（等价于 ``delete_by_key``）

        Returns:
            True=写回成功；False=DB 异常
        """
        sid = (astrbot_session_id or "").strip()
        if not sid:
            # 空字符串 —— 删除映射（让下次同 key 重新建）
            ok = await asyncio.to_thread(
                self._store.delete_by_key,
                self._subagent,
                self._project,
            )
            return ok
        ok = await asyncio.to_thread(
            self._store.set_astrbot_session_id,
            self._subagent,
            self._project,
            sid,
        )
        return ok

    async def touch(self) -> bool:
        """刷新本项目 session 的 updated_at，表示刚完成一次有效调用。"""
        return await asyncio.to_thread(
            self._store.touch_session,
            self._subagent,
            self._project,
        )


# === ProjectSessionManager：拿锁 + 维护 session 缓存 ===


class ProjectSessionManager:
    """项目 session 管理器。

    责任：
    - 维护「``(subagent, project)`` → ``asyncio.Lock``」缓存（懒创建）
    - ``acquire()`` async with 协议：拿锁 + 拉映射 + 返回 ``ProjectSession``
    - 提供 list / get / delete 给 WebUI 和命令
    """

    def __init__(
        self,
        store: ProjectSessionStore,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ):
        self._store = store
        self._lock_timeout = (
            float(lock_timeout) if lock_timeout else DEFAULT_LOCK_TIMEOUT
        )
        # 锁缓存：key = f"{subagent}\x00{project}" → asyncio.Lock
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_key(self, subagent: str, project: str) -> str:
        return f"{subagent}\x00{project}"

    # --- 拿 session（主入口） ---

    class _AcquireCtx:
        """async with 协议实现——``manager.acquire(...) as sess``。"""

        def __init__(
            self,
            mgr: "ProjectSessionManager",
            lock: asyncio.Lock,
            subagent: str,
            project: str,
            user_id: str,
        ):
            self._mgr = mgr
            self._lock = lock
            self._subagent = subagent
            self._project = project
            self._user_id = user_id
            self._session: ProjectSession | None = None
            self._acquired = False

        async def __aenter__(self) -> ProjectSession:
            try:
                await asyncio.wait_for(
                    self._lock.acquire(),
                    timeout=self._mgr._lock_timeout,
                )
            except asyncio.TimeoutError as e:
                raise SessionLockTimeout(
                    f"项目 session `{self._subagent}::{self._project}` 锁等待超时（{self._mgr._lock_timeout}s）——"
                    f"上一次调用还在跑或被卡住，请稍后重试或先 /subagent_chat_clear 清除"
                ) from e
            self._acquired = True
            # 在锁内 get_or_create（拿到 header snapshot）
            header = await asyncio.to_thread(
                self._mgr._store.get_or_create,
                self._subagent,
                self._project,
                self._user_id,
            )
            if header is None:
                # 释放锁 + 报错
                self._lock.release()
                self._acquired = False
                raise RuntimeError("project session get_or_create 失败")
            self._session = ProjectSession(
                subagent=self._subagent,
                project=self._project,
                user_id=self._user_id,
                session_header=header,
                store=self._mgr._store,
            )
            return self._session

        async def __aexit__(self, exc_type, exc, tb):
            if self._acquired:
                try:
                    self._lock.release()
                except Exception:
                    pass
                self._acquired = False
            return False  # 不吞异常

    def acquire(
        self,
        subagent: str,
        project: str,
        user_id: str,
    ) -> "_AcquireCtx":
        """拿一个 session 锁 + 返回 ``ProjectSession``。

        必须在 async with 内用。锁内会自动 get_or_create session（不查/写 history）。
        """
        key = self._lock_key(subagent, project)
        # _locks 字典只在 asyncio 协程里被改，setdefault 是原子的
        lock = self._locks.setdefault(key, asyncio.Lock())
        return self._AcquireCtx(
            mgr=self,
            lock=lock,
            subagent=subagent,
            project=project,
            user_id=user_id,
        )

    # --- 读（无锁） ---

    def get_header(self, subagent: str, project: str) -> dict[str, Any] | None:
        return self._store.get_session(subagent, project)

    def list_active(
        self,
        user_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self._store.list_sessions(user_id=user_id, limit=limit)

    # --- 写（破坏性） ---

    def delete_session(self, subagent: str, project: str) -> bool:
        """按 ``(subagent, project)`` 删映射——下次同 key 调用会创建新的 session。

        plugin 端删映射**不**影响子 AstrBot 那边已存的 chat history（如果还有，
        等下次新 session_id 拿过来就开新对话了）。子 AstrBot 那边的 chat_history 表里
        老 session 还活着，只是不再被 plugin 引用。
        """
        return self._store.delete_by_key(subagent, project)
