"""SQLite 存储层——插件独立 DB，三张表：

- subagent_tasks：任务生命周期（与参考插件的 openclaw_tasks 一致）
- subagent_instances：子 AstrBot 实例注册表（web CRUD 操作）
- project_sessions：项目 session 映射表——(subagent, project) → 子 AstrBot session_id
  （plan B：plugin **不**持久化 history 详情，**只**存 session_id 映射；
  history 真正存在子 AstrBot 自己 AstrBot 框架的 ``platform_sessions`` + ``chat_history`` 表里）

子 AstrBot 实例同时支持「配置文件初始值」+「WebUI 动态增删改」：
- 启动时从 _conf_schema.json.subagents 读初始值，写入 subagent_instances（如果 DB 为空）
- 运行期所有增删改都走 subagent_instances（DB 为单一真相源）
- 配置改了之后，web 端可以选择「从配置重新导入」覆盖 DB

项目 session：
- session key = (subagent, project) UNIQUE 约束
- 存 ``astrbot_session_id``（UUID）——子 AstrBot 那边 chat session 的真身
- 第一次调用 plugin 不传 session_id → 子 AstrBot ``/api/v1/chat`` 自动创建 UUID →
  plugin 从 SSE ``type=session_id`` 事件捕获 → 存到本表
- 后续调用 plugin 传这个 UUID → 子 AstrBot 按 ``(username, session_id)`` 复用同一段对话，
  history **完全在子 AstrBot 自己**累积，plugin 不再重发拼接
- 跨进程重启：plugin 只丢了「映射」，再调一次子 AstrBot会自动获得新 UUID；
  老 history 留在子 AstrBot那里，**不会**丢——只是 plugin 这边指针断了

注意：本插件是**第一版**，不维护从老 schema 升级的迁移路径。
老用户从 0.x 升上来时直接 ``rm -f data/plugins/astrbot_plugin_subagent_caller/subagent_caller.db`` 重置即可。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .util import PLUGIN_NAME, mask_token, to_bool


def get_db_path() -> Path:
    """获取插件独立 SQLite 数据库路径。

    放在 ``<data_dir>/plugins/<PLUGIN_NAME>/subagent_caller.db``，不污染 AstrBot 主库。
    """
    data_dir = Path(get_astrbot_data_path()) / "plugins" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "subagent_caller.db"


@contextmanager
def _connect():
    """打开 SQLite 连接——保证 schema 存在 + 退出时关闭。"""
    init_db()
    conn = sqlite3.connect(str(get_db_path()))
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """确保所有表存在。

    第一版**无**迁移逻辑：schema 调整请直接删 DB 重建。
    """
    db = get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subagent_tasks (
                task_id      TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                subagent     TEXT NOT NULL,
                task_text    TEXT NOT NULL,
                status       TEXT NOT NULL,
                mode         TEXT NOT NULL,
                created_at   REAL NOT NULL,
                finished_at  REAL,
                result_text  TEXT,
                error_text   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subagent_instances (
                name        TEXT PRIMARY KEY,
                base_url    TEXT NOT NULL,
                token       TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                username    TEXT NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                verify_ssl  INTEGER NOT NULL DEFAULT 1,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_sessions (
                session_id         TEXT PRIMARY KEY,
                user_id            TEXT NOT NULL,
                subagent           TEXT NOT NULL,
                project            TEXT NOT NULL,
                astrbot_session_id TEXT NOT NULL DEFAULT '',
                created_at         REAL NOT NULL,
                updated_at         REAL NOT NULL,
                UNIQUE(subagent, project)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_created ON subagent_tasks(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_subagent ON subagent_tasks(subagent, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sess_updated ON project_sessions(updated_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(str(get_db_path()))


# ============================================================
# SubagentStore：子 AstrBot 实例的 CRUD
# ============================================================


class SubagentStore:
    """子 AstrBot 实例的 SQLite 封装。

    设计目标：
    - 把子 AstrBot 实例的 CRUD 收口到一个类
    - WebUI 和指令都走这一个入口
    - Token 字段在 list/get 时可选择脱敏
    """

    def seed_from_config(
        self,
        config_subagents: list[dict],
        global_verify_ssl: bool = True,
    ) -> int:
        """如果 DB 为空，从 _conf_schema.json.subagents 写入初始实例。

        Args:
            config_subagents: 配置里的初始子 AstrBot 列表。
            global_verify_ssl: 顶层 verify_ssl 配置，作为 verify_ssl 缺省值。
                传 None / 留空 / 显式传 false 都会按字段原样落库。

        Returns:
            写入的数量。
        """
        try:
            with _connect() as conn:
                cur = conn.execute("SELECT COUNT(*) FROM subagent_instances")
                count = cur.fetchone()[0]
                if count > 0:
                    return 0
                now = time.time()
                written = 0
                for sa in config_subagents or []:
                    name = str(sa.get("name", "")).strip()
                    base_url = str(sa.get("base_url", "")).strip()
                    token = str(sa.get("token", "")).strip()
                    if not name or not base_url or not token:
                        continue
                    # verify_ssl 字段：null/缺省 = 沿用全局；true/false = 显式覆盖
                    v = sa.get("verify_ssl", None)
                    if v is None:
                        verify_ssl_int = 1 if global_verify_ssl else 0
                    else:
                        verify_ssl_int = 1 if to_bool(v) else 0
                    conn.execute(
                        "INSERT INTO subagent_instances "
                        "(name, base_url, token, description, username, enabled, verify_ssl, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            name,
                            base_url.rstrip("/"),
                            token,
                            str(sa.get("description", "")).strip(),
                            str(sa.get("username", "")).strip(),
                            1 if sa.get("enabled", True) else 0,
                            verify_ssl_int,
                            now,
                            now,
                        ),
                    )
                    written += 1
                conn.commit()
                return written
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.seed_from_config 失败: {e}")
            return 0

    def list_all(self, mask: bool = False) -> list[dict[str, Any]]:
        """列出所有子 AstrBot 实例。"""
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT name, base_url, token, description, username, enabled, verify_ssl, created_at, updated_at "
                    "FROM subagent_instances ORDER BY name"
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d.get("enabled", 1))
                d["verify_ssl"] = bool(d.get("verify_ssl", 1))
                # username 老行可能没有—— .get 兜底 ''
                d.setdefault("username", "")
                if mask:
                    d["token"] = mask_token(d.get("token", ""))
                result.append(d)
            return result
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.list_all 失败: {e}")
            return []

    def list_enabled(self) -> list[dict[str, Any]]:
        """列出所有 enabled 的子 AstrBot 实例（广播用）。"""
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT name, base_url, token, description, username, enabled, verify_ssl, created_at, updated_at "
                    "FROM subagent_instances WHERE enabled=1 ORDER BY name"
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["enabled"] = bool(d.get("enabled", 1))
                d["verify_ssl"] = bool(d.get("verify_ssl", 1))
                d.setdefault("username", "")
                out.append(d)
            return out
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.list_enabled 失败: {e}")
            return []

    def get(self, name: str, mask: bool = False) -> dict[str, Any] | None:
        """按名称取一个子 AstrBot 实例。"""
        if not name:
            return None
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT name, base_url, token, description, username, enabled, verify_ssl, created_at, updated_at "
                    "FROM subagent_instances WHERE name=?",
                    (name,),
                ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["enabled"] = bool(d.get("enabled", 1))
            d["verify_ssl"] = bool(d.get("verify_ssl", 1))
            d.setdefault("username", "")
            if mask:
                d["token"] = mask_token(d.get("token", ""))
            return d
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.get 失败: {e}")
            return None

    def upsert(
        self,
        name: str,
        base_url: str,
        token: str | None = None,
        description: str = "",
        username: str | None = None,
        enabled: bool = True,
        verify_ssl: bool | None = None,
    ) -> bool:
        """插入或更新一个子 AstrBot 实例。

        Args:
            name: 唯一名称（必填非空）
            base_url: 子 AstrBot 根 URL（必填非空）
            token:
                - str（必填非空）：显式设置 API Key
                - None：**保留已有值**（仅对 UPDATE 生效）；INSERT（新建）时返回 False
            description: 备注（UPDATE 时总是写入；默认空字符串 = "清空备注"）
            username:
                - str（可空字符串 ''）：显式设置被调方 user name
                - None：保留已有值（INSERT 落空字符串 ''）
            enabled: 是否启用（UPDATE 时总是写入）
            verify_ssl:
                - True / False：显式覆盖
                - None：保留已有值（INSERT 走默认 1）
        """
        name = (name or "").strip()
        base_url = (base_url or "").strip().rstrip("/")
        description = (description or "").strip()
        if not name or not base_url:
            return False
        # token：None 走"保留"路径；str 必须非空（空 token 拒绝——避免误覆盖）
        if token is not None:
            token = str(token).strip()
            if not token:
                return False
        now = time.time()
        try:
            with _connect() as conn:
                # === 一次性 SELECT 决定 INSERT/UPDATE + 解析所有 None 字段 ===
                cur = conn.execute(
                    "SELECT token, username, verify_ssl FROM subagent_instances WHERE name=?",
                    (name,),
                )
                row = cur.fetchone()
                is_new = row is None

                # === token 解析 ===
                if token is None:
                    if is_new:
                        # INSERT（新建）必须提供 token——返回 False 让 API 层报 400
                        return False
                    effective_token = row[0]  # 保留
                else:
                    effective_token = token

                # === username 解析 ===
                if username is None:
                    if is_new:
                        effective_username = ""
                    else:
                        # ——row[1] None 时兜底 ''
                        effective_username = (
                            (row[1] or "") if row[1] is not None else ""
                        )
                else:
                    effective_username = str(username).strip()

                # === verify_ssl 解析 ===
                if verify_ssl is None:
                    if is_new:
                        # INSERT 默认 1（开）——和 main.py 的 global_verify_ssl 解析对齐
                        effective_verify_ssl = 1
                    else:
                        effective_verify_ssl = int(row[2]) if row[2] is not None else 1
                else:
                    effective_verify_ssl = 1 if to_bool(verify_ssl) else 0

                # === INSERT / UPDATE ===
                if is_new:
                    conn.execute(
                        "INSERT INTO subagent_instances "
                        "(name, base_url, token, description, username, enabled, verify_ssl, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            name,
                            base_url,
                            effective_token,
                            description,
                            effective_username,
                            1 if enabled else 0,
                            effective_verify_ssl,
                            now,
                            now,
                        ),
                    )
                else:
                    # UPDATE：所有字段都明确写入（包括 description / enabled）——WebUI 总是全量发
                    conn.execute(
                        "UPDATE subagent_instances SET "
                        "base_url=?, token=?, description=?, username=?, "
                        "enabled=?, verify_ssl=?, updated_at=? "
                        "WHERE name=?",
                        (
                            base_url,
                            effective_token,
                            description,
                            effective_username,
                            1 if enabled else 0,
                            effective_verify_ssl,
                            now,
                            name,
                        ),
                    )
                conn.commit()
            return True
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.upsert 失败: {e}")
            return False

    def delete(self, name: str) -> bool:
        """删除一个子 AstrBot 实例。"""
        if not name:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "DELETE FROM subagent_instances WHERE name=?", (name,)
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.delete 失败: {e}")
            return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """开关一个子 AstrBot 实例。"""
        if not name:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE subagent_instances SET enabled=?, updated_at=? WHERE name=?",
                    (1 if enabled else 0, time.time(), name),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] SubagentStore.set_enabled 失败: {e}")
            return False


# ============================================================
# TaskLog：任务审计 / 列表
# ============================================================


class TaskLog:
    """任务审计 / 列表的 SQLite 封装。

    字段（与参考插件的 openclaw_tasks 保持一致，方便复用前端代码风格）：
    - task_id (TEXT PK)
    - user_id, subagent, task_text, status, mode, created_at, finished_at, result_text, error_text
    """

    def insert(self, info: dict) -> None:
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO subagent_tasks "
                    "(task_id,user_id,subagent,task_text,status,mode,"
                    "created_at,finished_at,result_text,error_text) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        info["task_id"],
                        info["user_id"],
                        info["subagent"],
                        info["task_text"],
                        info["status"],
                        info["mode"],
                        info["created_at"],
                        info["finished_at"],
                        info.get("result_text"),
                        info.get("error_text"),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] TaskLog.insert 失败: {e}")

    def update(self, info: dict) -> None:
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE subagent_tasks SET status=?, finished_at=?, "
                    "result_text=?, error_text=? WHERE task_id=?",
                    (
                        info["status"],
                        info["finished_at"],
                        info.get("result_text"),
                        info.get("error_text"),
                        info["task_id"],
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] TaskLog.update 失败: {e}")

    def get(self, task_id: str) -> dict[str, Any] | None:
        if not task_id:
            return None
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT task_id,user_id,subagent,task_text,status,mode,"
                    "created_at,finished_at,result_text,error_text "
                    "FROM subagent_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] TaskLog.get 失败: {e}")
            return None

    def list_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT task_id,subagent,task_text,status,mode,"
                    "created_at,finished_at "
                    "FROM subagent_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] TaskLog.list_recent 失败: {e}")
            return []

    def delete(self, task_id: str) -> bool:
        if not task_id:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "DELETE FROM subagent_tasks WHERE task_id=?",
                    (task_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] TaskLog.delete 失败: {e}")
            return False


# ============================================================
# ProjectSessionStore：项目 session（(subagent, project) → astrbot_session_id 映射）持久化
# ============================================================


class ProjectSessionStore:
    """项目 session 映射表——``(subagent, project)`` → ``astrbot_session_id``。

    plan B 简化：
    - **不再**持久化任何消息——history 真身在子 AstrBot 自己
    - **只**存 plugin 自己算的 session_id（PK + audit）+ 子 AstrBot 那边返回的
      astrbot_session_id（多轮对话的关键指针）
    - session key = ``(subagent, project)`` UNIQUE —— 同一个 (subagent, project)
      全程共享同一段子 AstrBot 对话
    - user_id 仅审计 / 取消权限用，不参与 unique
    - 跨进程重启：plugin 只丢了「映射」，老 history 还在子 AstrBot 那边
    """

    @staticmethod
    def _new_session_id() -> str:
        return f"ps-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def get_session(
        self,
        subagent: str,
        project: str,
    ) -> dict[str, Any] | None:
        """按 ``(subagent, project)`` 查 session 映射——找不到返回 None。"""
        subagent = (subagent or "").strip()
        project = (project or "").strip()
        if not subagent or not project:
            return None
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT session_id, user_id, subagent, project, astrbot_session_id, "
                    "created_at, updated_at "
                    "FROM project_sessions WHERE subagent=? AND project=?",
                    (subagent, project),
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] ProjectSessionStore.get_session 失败: {e}")
            return None

    def get_or_create(
        self,
        subagent: str,
        project: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """按 ``(subagent, project)`` 拿 session 映射；没有就创建一条。

        注意：``get_or_create`` **不**主动给 ``astrbot_session_id`` 赋值——那一步
        是在第一次实际调子 AstrBot 时由 SSE 响应返回后才写回的。如果映射已存在
        且 ``astrbot_session_id`` 非空，说明子 AstrBot 那边已经有对话上下文了，
        下次调就把这个 UUID 传回去实现多轮累积。

        Args:
            subagent: 子 AstrBot 名称
            project: 项目名（外层已 validate 过）
            user_id: 主控 sender_id（审计 + cancel 权限）

        Returns:
            session 映射 dict ``{session_id, user_id, subagent, project, astrbot_session_id, created_at, updated_at}``
            出错返回 None（param 为空等）。
        """
        subagent = (subagent or "").strip()
        project = (project or "").strip()
        user_id = (user_id or "").strip() or "anonymous"
        if not subagent or not project:
            return None
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT session_id, user_id, subagent, project, astrbot_session_id, "
                    "created_at, updated_at "
                    "FROM project_sessions WHERE subagent=? AND project=?",
                    (subagent, project),
                ).fetchone()
                if row:
                    # 已存在：若 user_id 不同，更新 user_id（主控 owner 视角变化）
                    if row["user_id"] != user_id:
                        conn.execute(
                            "UPDATE project_sessions SET user_id=?, updated_at=? "
                            "WHERE session_id=?",
                            (user_id, time.time(), row["session_id"]),
                        )
                        conn.commit()
                    return dict(row)
                # 新建
                session_id = self._new_session_id()
                now = time.time()
                conn.execute(
                    "INSERT INTO project_sessions "
                    "(session_id, user_id, subagent, project, astrbot_session_id, "
                    "created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (session_id, user_id, subagent, project, "", now, now),
                )
                conn.commit()
            return {
                "session_id": session_id,
                "user_id": user_id,
                "subagent": subagent,
                "project": project,
                "astrbot_session_id": "",
                "created_at": now,
                "updated_at": now,
            }
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.get_or_create 失败: {e}"
            )
            return None

    def set_astrbot_session_id(
        self,
        subagent: str,
        project: str,
        astrbot_session_id: str,
    ) -> bool:
        """把子 AstrBot 那边返回的 session_id UUID 写回映射表。

        Args:
            subagent: 子 AstrBot 名称
            project: 项目名
            astrbot_session_id: 子 AstrBot SSE ``type=session_id`` 事件返回的 UUID；
                传空字符串表示「清空映射」（等价于 ``delete_by_key``）

        Returns:
            True=更新成功；False=未找到对应 session 行 / DB 异常
        """
        subagent = (subagent or "").strip()
        project = (project or "").strip()
        if not subagent or not project:
            return False
        sid = (astrbot_session_id or "").strip()
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE project_sessions SET astrbot_session_id=?, updated_at=? "
                    "WHERE subagent=? AND project=?",
                    (sid, time.time(), subagent, project),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.set_astrbot_session_id 失败: {e}"
            )
            return False

    def touch_session(
        self,
        subagent: str,
        project: str,
    ) -> bool:
        """刷新 ``(subagent, project)`` 的 updated_at，表示这段项目会话刚活跃过。"""
        subagent = (subagent or "").strip()
        project = (project or "").strip()
        if not subagent or not project:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE project_sessions SET updated_at=? "
                    "WHERE subagent=? AND project=?",
                    (time.time(), subagent, project),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.touch_session 失败: {e}"
            )
            return False

    def list_sessions(
        self,
        user_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """列 session 映射——按 updated_at DESC 排序。

        Args:
            user_id: 可选过滤（主控 owner 视角）
            limit: 最多返回条数
        """
        try:
            with _connect() as conn:
                conn.row_factory = sqlite3.Row
                if user_id:
                    rows = conn.execute(
                        "SELECT session_id, user_id, subagent, project, astrbot_session_id, "
                        "created_at, updated_at "
                        "FROM project_sessions WHERE user_id=? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (user_id, int(limit)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT session_id, user_id, subagent, project, astrbot_session_id, "
                        "created_at, updated_at "
                        "FROM project_sessions "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (int(limit),),
                    ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.list_sessions 失败: {e}"
            )
            return []

    def delete_session(self, session_id: str) -> bool:
        """按 plugin 内部 session_id (PK) 删整行。"""
        if not session_id:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "DELETE FROM project_sessions WHERE session_id=?",
                    (session_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.delete_session 失败: {e}"
            )
            return False

    def delete_by_key(self, subagent: str, project: str) -> bool:
        """按 ``(subagent, project)`` 删 session 映射——WebUI / 命令快捷删除。

        注意：plugin 删映射**不**影响子 AstrBot 那边已存的 chat history——下次
        同 key 调一次，子 AstrBot 拿到新 UUID 开新对话。
        """
        subagent = (subagent or "").strip()
        project = (project or "").strip()
        if not subagent or not project:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "DELETE FROM project_sessions WHERE subagent=? AND project=?",
                    (subagent, project),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] ProjectSessionStore.delete_by_key 失败: {e}"
            )
            return False
