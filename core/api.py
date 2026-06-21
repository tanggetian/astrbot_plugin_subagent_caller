"""Plugin Page Web API handlers——子 AstrBot 实例 CRUD + 任务管理 + 项目 session 映射。

所有 handler 都是纯函数，参数注入（task_log、bg_tasks、task_handles、subagent_store）。
"""

from __future__ import annotations

import time
from typing import Any

from quart import jsonify, request

from astrbot.api import logger

from .util import sanitize_error

# ============================================================
# 子 AstrBot 实例管理 API
# ============================================================


async def api_list_subagents(subagent_store) -> Any:
    """GET /api/plug/astrbot_plugin_subagent_caller/subagents

    Returns:
        ok=true: {"ok": True, "subagents": [...], "total": N}
    """
    try:
        items = subagent_store.list_all(mask=True)
        return jsonify(
            {
                "ok": True,
                "subagents": items,
                "total": len(items),
            }
        )
    except Exception as e:
        logger.error(f"api_list_subagents 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e), "subagents": []}), 500


async def api_upsert_subagent(subagent_store, global_verify_ssl: bool = True) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/subagents/upsert

    Body: {
      "name": "...", "base_url": "...",
      "token": "abk_xxx",      # 必填（非空）= 替换；缺省 / null / 空字符串 = 保留（编辑场景）
      "description": "...", "enabled": true,
      "username": "yysy"       # 可选；空 / 缺省 = fallback 到主控 sender_id
      "verify_ssl": true|false|null   # 可选；null/缺省 = 沿用全局
    }

    token 字段语义（关键——避免 WebUI 编辑实例时误覆盖原 token）：
        - 缺省（key 不在 body）/ null / 空字符串 → 透传 None 给 storage（**保留**原 token）
        - 非空字符串 → 透传给 storage（**替换**为新值）

    Args:
        global_verify_ssl: 当请求 body 没显式传 verify_ssl 时，作为兜底值落库。
            由 main.py 注入 self.verify_ssl（来自 _conf_schema.json.verify_ssl）。
    """
    try:
        data = await request.get_json() or {}
        name = (data.get("name") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        description = (data.get("description") or "").strip()
        enabled = bool(data.get("enabled", True))

        # === token 解析：缺省 / null / 空字符串 → None（保留）===
        if "token" in data:
            raw_token = data.get("token")
            if raw_token is None or (
                isinstance(raw_token, str) and not raw_token.strip()
            ):
                token_val: str | None = None
            else:
                token_val = str(raw_token).strip()
        else:
            token_val = None

        # === username：body 显式传 = 显式设置（'' 也算显式——"fallback 到主控"）；
        #     body 缺省 = 保留已有（None → storage 内部兜底）。===
        if "username" in data:
            username_val: str | None = str(data.get("username") or "").strip()
        else:
            username_val = None

        # === verify_ssl 解析：body 里没传 / 传 null → 用全局；传 true/false → 显式覆盖 ===
        v = data.get("verify_ssl", None)
        if v is None:
            verify_ssl = None
        else:
            verify_ssl = bool(v)

        # 必填校验：name / base_url 必填；token 仅新增实例时必填
        if not name or not base_url:
            return jsonify(
                {
                    "ok": False,
                    "error": "name / base_url 均为必填",
                }
            ), 400
        if verify_ssl is None:
            verify_ssl = global_verify_ssl

        ok = subagent_store.upsert(
            name=name,
            base_url=base_url,
            token=token_val,
            description=description,
            username=username_val,
            enabled=enabled,
            verify_ssl=verify_ssl,
        )
        if not ok:
            # 最常见原因：调用方没传 token 但这是新增实例（token=None 时 INSERT 失败）
            return jsonify(
                {
                    "ok": False,
                    "error": "新增实例必须提供 token（编辑现有实例时 token 留空 = 保留原值）",
                }
            ), 400
        return jsonify(
            {
                "ok": True,
                "name": name,
                "username": username_val if username_val is not None else "",
                "verify_ssl": verify_ssl,
            }
        )
    except Exception as e:
        logger.error(f"api_upsert_subagent 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_delete_subagent(subagent_store) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/subagents/delete

    Body: {"name": "..."}
    """
    try:
        data = await request.get_json() or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        ok = subagent_store.delete(name)
        if not ok:
            return jsonify({"ok": False, "error": "未找到该子 AstrBot"}), 404
        return jsonify({"ok": True, "name": name, "deleted": True})
    except Exception as e:
        logger.error(f"api_delete_subagent 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_toggle_subagent(subagent_store) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/subagents/toggle

    Body: {"name": "...", "enabled": true|false}
    """
    try:
        data = await request.get_json() or {}
        name = (data.get("name") or "").strip()
        enabled = bool(data.get("enabled", True))
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        ok = subagent_store.set_enabled(name, enabled)
        if not ok:
            return jsonify({"ok": False, "error": "未找到该子 AstrBot"}), 404
        return jsonify({"ok": True, "name": name, "enabled": enabled})
    except Exception as e:
        logger.error(f"api_toggle_subagent 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_ping_subagent(subagent_store, global_verify_ssl: bool = True) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/subagents/ping

    Body: {"name": "..."}——返回 ping_ok / ping_msg
    """
    try:
        data = await request.get_json() or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "name required"}), 400
        sa = subagent_store.get(name, mask=False)
        if not sa:
            return jsonify({"ok": False, "error": "未找到该子 AstrBot"}), 404
        # verify_ssl 决策：实例级显式值 > 全局默认
        sa_verify = sa.get("verify_ssl", None)
        if sa_verify is None:
            effective_verify = bool(global_verify_ssl)
        else:
            effective_verify = bool(sa_verify)
        from .client import AstrBotClient

        client = AstrBotClient(
            base_url=sa["base_url"],
            token=sa["token"],
            timeout=10,
            verify_ssl=effective_verify,
        )
        ping_ok, ping_msg = await client.ping()
        return jsonify(
            {
                "ok": True,
                "name": name,
                "ping_ok": ping_ok,
                "ping_msg": ping_msg,
                "verify_ssl": effective_verify,
            }
        )
    except Exception as e:
        logger.error(f"api_ping_subagent 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


# ============================================================
# 任务管理 API
# ============================================================


async def api_list_tasks(task_log) -> Any:
    """GET /api/plug/astrbot_plugin_subagent_caller/tasks

    返回字段已经过脱敏：不返 user_id / result_text / error_text。
    """
    try:
        tasks = task_log.list_recent(limit=200)
        return jsonify(
            {
                "ok": True,
                "tasks": tasks,
                "running_count": sum(1 for t in tasks if t["status"] == "running"),
                "total_count": len(tasks),
            }
        )
    except Exception as e:
        logger.error(f"api_list_tasks 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e), "tasks": []}), 500


async def api_get_task(task_log) -> Any:
    """GET /api/plug/astrbot_plugin_subagent_caller/tasks/get?task_id=..."""
    try:
        task_id = (request.args.get("task_id") or "").strip()
        if not task_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        info = task_log.get(task_id)
        if not info:
            return jsonify({"ok": False, "error": "task not found"}), 404
        return jsonify({"ok": True, "task": info})
    except Exception as e:
        logger.error(f"api_get_task 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_cancel_task(task_log, bg_tasks, task_handles) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/cancel body: {"task_id": "..."}"""
    try:
        data = await request.get_json() or {}
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        info = bg_tasks.get(task_id)
        if not info:
            return jsonify(
                {"ok": False, "error": "task not found or already finished"}
            ), 404
        handle = task_handles.get(task_id)
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        task_log.update(info)
        if handle and not handle.done():
            handle.cancel()
        return jsonify({"ok": True, "task_id": task_id, "status": "cancelled"})
    except Exception as e:
        logger.error(f"api_cancel_task 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_delete_task(task_log, bg_tasks, task_handles) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/delete body: {"task_id": "..."}"""
    try:
        data = await request.get_json() or {}
        task_id = (data.get("task_id") or "").strip()
        if not task_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        info = bg_tasks.pop(task_id, None)
        handle = task_handles.pop(task_id, None)
        if handle and not handle.done():
            handle.cancel()
        deleted = task_log.delete(task_id)
        if not deleted and not info:
            return jsonify({"ok": False, "error": "task not found"}), 404
        return jsonify({"ok": True, "task_id": task_id, "deleted": True})
    except Exception as e:
        logger.error(f"api_delete_task 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


# ============================================================
# 项目 session 管理 API
# ============================================================


async def api_list_project_sessions(session_manager) -> Any:
    """GET /api/plug/astrbot_plugin_subagent_caller/project_sessions

    列出所有活跃的 (subagent, project) session——按 updated_at DESC。
    """
    try:
        items = session_manager.list_active(limit=200)
        return jsonify(
            {
                "ok": True,
                "sessions": items,
                "total": len(items),
            }
        )
    except Exception as e:
        logger.error(f"api_list_project_sessions 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e), "sessions": []}), 500


async def api_get_project_session(session_manager) -> Any:
    """GET /api/plug/astrbot_plugin_subagent_caller/project_sessions/get?subagent=..&project=..

    返回 session 映射头（含 ``astrbot_session_id``）——plugin 不存 history 详情，
    真正的多轮上下文由子 AstrBot 自己的 chat_history 表累积。
    """
    try:
        subagent = (request.args.get("subagent") or "").strip()
        project = (request.args.get("project") or "").strip()
        if not subagent or not project:
            return jsonify({"ok": False, "error": "subagent and project required"}), 400
        header = session_manager.get_header(subagent, project)
        if not header:
            return jsonify({"ok": False, "error": "session not found"}), 404
        return jsonify(
            {
                "ok": True,
                "header": header,
            }
        )
    except Exception as e:
        logger.error(f"api_get_project_session 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500


async def api_clear_project_session(session_manager) -> Any:
    """POST /api/plug/astrbot_plugin_subagent_caller/project_sessions/clear
    body: {"subagent": "...", "project": "..."}

    删 plugin 端的 session_id 映射——下次同 key 调用让子 AstrBot 自动建新 UUID，
    等于强制开新对话。子 AstrBot 自己的 chat_history 不受影响。
    """
    try:
        data = await request.get_json() or {}
        subagent = (data.get("subagent") or "").strip()
        project = (data.get("project") or "").strip()
        if not subagent or not project:
            return jsonify({"ok": False, "error": "subagent and project required"}), 400
        ok = session_manager.delete_session(subagent, project)
        if not ok:
            return jsonify({"ok": False, "error": "session not found"}), 404
        return jsonify(
            {
                "ok": True,
                "subagent": subagent,
                "project": project,
                "cleared_count": 1,  # 删的是映射行
            }
        )
    except Exception as e:
        logger.error(f"api_clear_project_session 异常：{e}", exc_info=True)
        return jsonify({"ok": False, "error": sanitize_error(e)}), 500
