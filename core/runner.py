"""后台任务 runner——``/subagent_task``、``/subagent_chat_task`` 和 ``call_subagent_async`` 都走它。

行为参考 openclaw_caller 的 runner：
- 把任务生命周期写入 SQLite（subagent_tasks）和调用方传入的内存字典
- 异常分类处理：CancelledError / Exception / send 失败
- 1 小时内存 GC
- 推送路径：先 event.send() 走原路径；失败 / 无 event 时自动 fallback 到
  context.get_platform().send_message()——绕开 event 生命周期，是 AstrBot 延迟推送的标准姿势

状态全部从参数传入（不读模块级），方便单测和 reload。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .util import digest


async def _try_send_result(
    event,
    msg: str,
    *,
    task_id: str,
    platform_meta: dict | None,
    context,
    kind: str,  # "done" | "failed" — 仅用于日志区分
) -> bool:
    """按顺序尝试：``event.send()`` → ``context.get_platform().send_message()`` 平台 fallback。"""
    # 路径 1（主）：event.send()——对真 event 仍在生命周期内的快任务最直接
    if event is not None and not getattr(event, "_is_lite", False):
        try:
            await event.send(MessageChain([Plain(msg)]))
            logger.info(
                f"[subagent bg] phase=end task_id={task_id} status={kind} push_via=event_send"
            )
            return True
        except Exception as send_err:
            logger.warning(
                f"[subagent bg] phase=end task_id={task_id} status={kind} "
                f"push_via=event_send_failed error={type(send_err).__name__} "
                "（尝试 platform fallback）"
            )

    # 路径 2（fallback）：context.get_platform().send_message()——不绑 event 生命周期
    if not platform_meta or not context:
        logger.error(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            "push_via=all_failed（event.send 失败且无 platform fallback）"
        )
        return False
    platform_name = platform_meta.get("platform_name", "")
    session_id = platform_meta.get("session_id", "")
    if not platform_name or not session_id:
        logger.error(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            "push_via=all_failed（event.send 失败且 platform_meta 不完整）"
        )
        return False
    try:
        platform = context.get_platform(platform_name)
    except Exception as lookup_err:
        logger.error(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed error={type(lookup_err).__name__} "
            f"platform={platform_name}",
            exc_info=True,
        )
        return False
    if platform is None:
        logger.warning(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed reason=platform_not_found "
            f"platform={platform_name}"
        )
        return False
    try:
        await platform.send_message(session_id, MessageChain([Plain(msg)]))
        logger.info(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback platform={platform_name}"
        )
        return True
    except Exception as fallback_err:
        logger.error(
            f"[subagent bg] phase=end task_id={task_id} status={kind} "
            f"push_via=platform_fallback_failed error={type(fallback_err).__name__} "
            f"platform={platform_name}",
            exc_info=True,
        )
        return False


def _extract_send_target(event) -> dict:
    """从 event 抓出 (platform_name, session_id) — 后台任务完成时推送用。"""
    if event is None:
        return {}
    platform_name = ""
    for method in ("get_platform_id", "get_platform_name"):
        try:
            value = getattr(event, method, lambda: "")()
            if value:
                platform_name = str(value)
                break
        except Exception:
            pass
    session_id = ""
    try:
        value = getattr(event, "get_session_id", lambda: "")()
        if value:
            session_id = str(value)
    except Exception:
        pass
    import re

    if not platform_name or not session_id:
        return {}
    return {
        "platform_name": re.sub(r"[^a-zA-Z0-9_-]+", "-", platform_name)
        .strip("-")
        .lower(),
        "session_id": session_id,
    }


async def background_run(
    *,
    task: str,
    subagent_name: str,
    user_id: str,
    task_id: str,
    event,  # 真 AstrMessageEvent
    call_subagent,  # 注入的 AstrBotClient.call（或兼容函数）——返回 CallResult（reply, session_id）
    task_log,  # 注入的 TaskLog 实例
    bg_tasks: dict[str, dict[str, Any]],
    task_handles: dict[str, asyncio.Task],
    platform_meta: dict
    | None = None,  # {platform_name, session_id}——延迟推送 fallback 用
    context=None,  # AstrBot Context——context.get_platform() 拿平台适配器
    username: str = "subagent_caller",
    # === 项目 session 模式（可选）===
    project: str | None = None,  # 项目名；为 None 时走老路径
    session_manager=None,  # ProjectSessionManager 实例；project 模式必传
) -> None:
    """后台跑 subagent 任务。

    **event 必须由 caller 显式传进来**——不接受全局 event 缓存，避免跨用户竞态。
    **platform_meta + context 必须由 caller 传进来**——推送失败时的平台适配器 fallback。

    project=None（默认）：单次 LLM 调用，行为与 v1.0 完全一致。
    project=非空 + session_manager=实例：项目 session 多轮模式（plan B）——
        1. 拿 session 锁
        2. 从映射拿子 AstrBot session_id（首次调 = 空字符串 → 子 AstrBot 自动建 UUID）
        3. 发新消息 + session_id 给子 AstrBot，**不**拼 history 重发
        4. 从 SSE ``type=session_id`` 事件捕获子 AstrBot 那边返回的 UUID，写回映射
        5. history 真身在子 AstrBot 那边累积，plugin 端只维护这个 UUID 指针
    """
    created_at = time.time()
    is_project_mode = bool(project) and session_manager is not None
    mode_value = "project-background" if is_project_mode else "background"
    info = {
        "task_id": task_id,
        "user_id": user_id,
        "subagent": subagent_name,
        "task_text": task,
        "status": "running",
        "mode": mode_value,
        "created_at": created_at,
        "finished_at": None,
        "result_text": None,
        "error_text": None,
    }
    bg_tasks[task_id] = info
    task_log.insert(info)
    sender_digest = digest(user_id)
    logger.info(
        f"[subagent bg] phase=start task_id={task_id} subagent={subagent_name} "
        f"sender={sender_digest} task_chars={len(task)} "
        f"project={project or '-'} "
        f"has_platform_fallback={bool(platform_meta and context)}"
    )

    has_recipient = event is not None and not getattr(event, "_is_lite", False)
    if not has_recipient and not (platform_meta and context):
        logger.warning(
            f"[subagent bg] phase=start task_id={task_id} "
            "event_is_lite=true 且无 platform fallback——结果仅写 SQLite，Plugin Page 标 no_recipient。"
        )

    from .util import sanitize_error
    from .sessions import SessionLockTimeout

    t0 = time.time()

    # === 项目 session 模式（plan B）：拿锁 → 取/存子 AstrBot session_id ===
    if is_project_mode:
        try:
            async with session_manager.acquire(
                subagent=subagent_name,
                project=project,
                user_id=user_id,
            ) as sess:
                # 拿锁时 snapshot 的子 AstrBot session_id——首次调用是空字符串
                existing_astrbot_sid = sess.astrbot_session_id
                # 检查 cancel（在锁内 + 调 subagent 之前）
                if info.get("status") == "cancelled":
                    logger.info(
                        f"[subagent bg] phase=end task_id={task_id} status=cancelled "
                        f"project={project}（拿锁后被 cancel）"
                    )
                    info["finished_at"] = time.time()
                    info["status"] = "cancelled"
                    info["error_text"] = "cancelled"
                    task_log.update(info)
                    return
                # 真发 subagent——session_id = plugin 存的 UUID（空 = 让子 AstrBot 自动建）
                call_result = await call_subagent(
                    message=task,
                    username=username,
                    session_id=existing_astrbot_sid or None,
                )
                # CallResult：reply + session_id
                reply = (
                    call_result.reply
                    if hasattr(call_result, "reply")
                    else str(call_result)
                )
                returned_sid = (
                    getattr(call_result, "session_id", None)
                    if not isinstance(call_result, str)
                    else None
                )
                # 把子 AstrBot 返回的 session_id 写回映射（首次 / 或子 AstrBot 重新分配时）
                if returned_sid and returned_sid != existing_astrbot_sid:
                    await sess.set_astrbot_session_id(returned_sid)
                    logger.info(
                        f"[subagent bg] session_id updated project={project} "
                        f"old={'<empty>' if not existing_astrbot_sid else existing_astrbot_sid[:12]} "
                        f"new={returned_sid[:12]}"
                    )
                else:
                    await sess.touch()
                # 再检查 cancel（返回后）
                if info.get("status") == "cancelled":
                    logger.info(
                        f"[subagent bg] phase=end task_id={task_id} status=cancelled "
                        f"project={project} total_s={time.time() - t0:.2f}（subagent 返回后被 cancel）"
                    )
                    info["finished_at"] = time.time()
                    info["status"] = "cancelled"
                    info["error_text"] = "cancelled"
                    task_log.update(info)
                    return
                info["finished_at"] = time.time()
                info["result_text"] = reply
                msg = (
                    f"✅ 项目会话后台任务 {task_id} 完成（subagent={subagent_name} / project={project}）\n\n"
                    f"{reply}"
                )
                await _finish_task(
                    info,
                    task_log,
                    has_recipient,
                    event,
                    platform_meta,
                    context,
                    msg,
                    task_id,
                    kind="done",
                    t0=t0,
                )
        except SessionLockTimeout as e:
            # 锁等不到——一般说明同 project 另一个调用还在跑
            info["finished_at"] = time.time()
            info["status"] = "failed"
            info["error_text"] = f"session_lock_timeout: {e}"
            task_log.update(info)
            err = f"❌ 项目 session `{subagent_name}::{project}` 锁等待超时，请稍后重试"
            logger.warning(
                f"[subagent bg] phase=end task_id={task_id} status=failed "
                f"reason=session_lock_timeout project={project}"
            )
            if has_recipient or (platform_meta and context):
                sent = await _try_send_result(
                    event if has_recipient else None,
                    err,
                    task_id=task_id,
                    platform_meta=platform_meta,
                    context=context,
                    kind="failed",
                )
                if not sent:
                    logger.error(
                        f"[subagent bg] phase=end task_id={task_id} status=failed_no_push "
                        "reason=all_paths_failed（event.send 与 platform fallback 都失败）"
                    )
        except asyncio.CancelledError:
            # 异步取消——不再写 history 标记，只更新 task 状态
            info["status"] = "cancelled"
            info["finished_at"] = time.time()
            info["error_text"] = "cancelled"
            task_log.update(info)
            logger.info(
                f"[subagent bg] phase=end task_id={task_id} status=cancelled "
                f"project={project} total_s={time.time() - t0:.2f}（asyncio 取消）"
            )
        except Exception as e:
            # subagent 调用失败 / 其他——推送失败消息
            info["finished_at"] = time.time()
            info["error_text"] = str(e)
            info["status"] = "failed"
            task_log.update(info)
            err = f"❌ 项目会话后台任务 {task_id} 失败：{sanitize_error(e)}"
            logger.error(
                f"[subagent bg] phase=end task_id={task_id} status=failed "
                f"error={type(e).__name__} project={project} total_s={time.time() - t0:.2f}",
                exc_info=True,
            )
            if has_recipient or (platform_meta and context):
                sent = await _try_send_result(
                    event if has_recipient else None,
                    err,
                    task_id=task_id,
                    platform_meta=platform_meta,
                    context=context,
                    kind="failed",
                )
                if not sent:
                    logger.error(
                        f"[subagent bg] phase=end task_id={task_id} status=failed_no_push "
                        "reason=all_paths_failed（event.send 与 platform fallback 都失败）"
                    )
        finally:

            async def _gc():
                await asyncio.sleep(3600)
                bg_tasks.pop(task_id, None)
                task_handles.pop(task_id, None)

            asyncio.create_task(_gc())
        return

    # === 老路径：单次 LLM 调用（无 session）—— v1.0 行为完全不变 ===
    try:
        call_result = await call_subagent(
            message=task,
            username=username,
            session_id=None,
        )
        # CallResult：取 reply 字段
        reply = call_result.reply if hasattr(call_result, "reply") else str(call_result)
        if info.get("status") == "cancelled":
            logger.info(
                f"[subagent bg] phase=end task_id={task_id} status=cancelled "
                f"total_s={time.time() - t0:.2f}（子 AstrBot 返回后被 cancel）"
            )
            return
        info["finished_at"] = time.time()
        info["result_text"] = reply
        msg = f"✅ 后台任务 {task_id} 完成（subagent={subagent_name}）\n\n{reply}"
        await _finish_task(
            info,
            task_log,
            has_recipient,
            event,
            platform_meta,
            context,
            msg,
            task_id,
            kind="done",
            t0=t0,
        )
    except asyncio.CancelledError:
        info["status"] = "cancelled"
        info["finished_at"] = time.time()
        task_log.update(info)
        logger.info(
            f"[subagent bg] phase=end task_id={task_id} status=cancelled "
            f"total_s={time.time() - t0:.2f}（asyncio 取消）"
        )
    except Exception as e:
        info["finished_at"] = time.time()
        info["error_text"] = str(e)
        info["status"] = "failed"
        task_log.update(info)
        err = f"❌ 后台任务 {task_id} 失败：{sanitize_error(e)}"
        logger.error(
            f"[subagent bg] phase=end task_id={task_id} status=failed "
            f"error={type(e).__name__} total_s={time.time() - t0:.2f}",
            exc_info=True,
        )
        if has_recipient or (platform_meta and context):
            sent = await _try_send_result(
                event if has_recipient else None,
                err,
                task_id=task_id,
                platform_meta=platform_meta,
                context=context,
                kind="failed",
            )
            if not sent:
                logger.error(
                    f"[subagent bg] phase=end task_id={task_id} status=failed_no_push "
                    "reason=all_paths_failed（event.send 与 platform fallback 都失败）"
                )
    finally:

        async def _gc():
            await asyncio.sleep(3600)
            bg_tasks.pop(task_id, None)
            task_handles.pop(task_id, None)

        asyncio.create_task(_gc())


async def _finish_task(
    info,
    task_log,
    has_recipient,
    event,
    platform_meta,
    context,
    msg,
    task_id,
    kind,
    t0,
):
    """老路径下：把 task 标 done / no_recipient + 推送结果。

    从 background_run 拆出来——和 v1.0 行为完全一致。
    """
    if has_recipient or (platform_meta and context):
        info["status"] = "done"
        task_log.update(info)
        sent = await _try_send_result(
            event if has_recipient else None,
            msg,
            task_id=task_id,
            platform_meta=platform_meta,
            context=context,
            kind=kind,
        )
        if not sent:
            info["status"] = "no_recipient"
            task_log.update(info)
            logger.warning(
                f"[subagent bg] phase=end task_id={task_id} status=no_recipient "
                f"total_s={time.time() - t0:.2f} "
                "（event.send 与 platform fallback 都失败，详见上面 push_via 日志）"
            )
    else:
        info["status"] = "no_recipient"
        task_log.update(info)
        logger.warning(
            f"[subagent bg] phase=end task_id={task_id} status=no_recipient "
            f"reason=event_is_lite_no_fallback total_s={time.time() - t0:.2f} "
            "（任务完成但无任何推送通道）"
        )


def extract_send_target(event) -> dict:
    """公开的 helper——main.py 用：把 event 抓出 (platform_name, session_id)。"""
    return _extract_send_target(event)
