"""AstrBotClient——封装对子 AstrBot 实例的 HTTP API 调用。

AstrBot v4.18+ 提供基于 API Key 的 HTTP API，调用方式：
- POST {base_url}/api/v1/chat
- Authorization: Bearer abk_xxx
- Body: {"username": "...", "session_id": "...", "message": "...", "enable_streaming": true}
- 响应：SSE 流，每个 data: 行是一个 JSON 事件，标准 schema：

    data: {"type": "session_id",          "data": null,    "session_id": "..."}
    data: {"type": "user_message_saved",  "data": {...}}
    data: {"type": "plain",               "data": "流式片段", "streaming": true}
    data: {"type": "agent_stats",         "data": {...}}           ← 元数据，忽略
    data: {"type": "complete",            "data": "完整回复"}     ← 与 plain 重复，去重
    data: {"type": "message_saved",       "data": {...}}           ← 元数据
    data: {"type": "end",                 "data": ""}              ← 流结束

  提取规则：
    - type=plain / type=complete 时取 evt["data"]（str）拼成 assistant 完整回复
    - type=session_id 时取 evt["session_id"]——子 AstrBot 创建 / 复用的会话 UUID
    - 其他 type 一律视为元数据，返回 ""。OpenAI 兼容 / 顶层 message / 顶层 content
      格式也保留（旧版本 / 自定义 Gateway 兼容）。

设计要点：
- 同步调用（/subagent_call）走 SSE 流式解析，攒齐 assistant 完整回复
- 异步任务（/subagent_task）也走这个 call()，只是外层在 background_run 包
- 广播（/subagent_broadcast）走 asyncio.gather 并行
- 一致支持 username + session_id 透传——子 AstrBot 按 (username, session_id)
  维护 chat history；plugin 把同一 (subagent, project) 映射到稳定的 session_id
- 错误分类：HTTP 4xx/5xx、超时、连接错误、SSE 空响应——分别给用户/LLM 不同提示
- AstrBot SSE 去重：plain 增量收到后再遇到 complete 时跳过，避免回复被粘两遍
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger

from .util import SubAgentError, digest, new_request_id


@dataclass
class CallResult:
    """``client.call()`` 的返回值。

    Attributes:
        reply: 子 AstrBot 返回的 assistant 完整文本。
        session_id: 子 AstrBot 端的会话 UUID（plan B 关键字段）。
            - 若调用方传入了 session_id 且被接受：等于传入值
            - 若调用方未传 session_id：等于子 AstrBot 自动创建的 UUID
            - 若 SSE 没收到 session_id 事件：等于 None（极少见——旧版本 / 非 SSE 响应）
    """

    reply: str
    session_id: Optional[str] = None


def _normalize_base_url(base_url: str) -> str:
    """归一化 base_url——去掉末尾 /，自动补 http://。

    接受：
    - 完整 URL：``http://host:port`` / ``https://host:port``
    - 裸 host:port：``192.168.1.10:6185`` / ``example.com:6185``
      → 自动补 ``http://`` 前缀
    - 带尾部斜杠的：自动去掉

    异常检测：``urlparse`` 把 ``localhost:6185`` 解析成 ``scheme=localhost, path=6185``，
    把 ``x:6185`` 解析成 ``scheme=x, path=6185``——这两种都不是真正的 URL。
    判定规则：scheme 不在 ``{http, https}`` 名单中时，认为没写协议，补上 ``http://``。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        url = "http://" + url
    return url.rstrip("/")


class AstrBotClient:
    """对单个子 AstrBot 实例的 HTTP API 客户端封装。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = 60,
        verify_ssl: bool = True,
    ):
        self.base_url = _normalize_base_url(base_url)
        self.token = (token or "").strip()
        self.timeout = int(timeout) if timeout else 60
        self.verify_ssl = bool(verify_ssl)

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/api/v1/chat"

    @property
    def openapi_url(self) -> str:
        return f"{self.base_url}/api/v1/openapi.json"

    async def ping(self) -> tuple[bool, str]:
        """健康检查：访问 openapi.json 验证 base_url + token。

        Returns:
            (ok, message)——ok=True 时 message 是空串；ok=False 时 message 是给用户的错误提示。
        """
        if not self.base_url or not self.token:
            return False, "base_url 或 token 未配置"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            connector = self._build_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    self.openapi_url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        return True, ""
                    if resp.status in (401, 403):
                        return False, f"鉴权失败（HTTP {resp.status}）— token 可能错误"
                    return False, f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            return False, "连接超时（10s）"
        except aiohttp.ClientError as e:
            return False, f"连接错误：{type(e).__name__}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def call(
        self,
        message: str,
        username: str,
        session_id: Optional[str] = None,
    ) -> CallResult:
        """调子 AstrBot /api/v1/chat 跑一次对话，返回 ``CallResult``。

        Args:
            message: 要发给子 AstrBot 的用户消息。
            username: 子 AstrBot 端的「用户名」——本插件用 caller 标识（主控
                AstrBot 里的 sender_id）。子 AstrBot 按 ``(username, session_id)``
                维护 chat history；同一对 key 多次调用 = 同一段对话。
            session_id: 可选，指定一个 session_id 复用历史；不传则让子 AstrBot 自动生成 UUID。
                **plan B 关键参数**：plugin 把 ``(subagent, project)`` 映射到稳定的
                session_id，第一次调用不传（子 AstrBot 自动建），后续调用传入实现多轮累积。

        Returns:
            ``CallResult(reply, session_id)``：
            - ``reply``：子 AstrBot 返回的 assistant 文本
            - ``session_id``：子 AstrBot 那边的会话 UUID——从 SSE 的 ``type=session_id`` 事件
              捕获；不传 session_id 时等于子 AstrBot 自动创建的 UUID，传了等于回显值

        Raises:
            SubAgentError: HTTP 错误 / 超时 / 连接错误 / SSE 空响应。
        """
        request_id = new_request_id()
        url = self.chat_url
        token = self.token
        timeout_seconds = self.timeout

        if not self.base_url or not token:
            raise SubAgentError("子 AstrBot 未配置（base_url 或 token 为空）")

        sender_digest = digest(username)
        logger.info(
            f"[subagent call] phase=start request_id={request_id} "
            f"subagent_url={self.base_url} sender={sender_digest} "
            f"task_chars={len(message)} session_id={session_id or '-'}"
        )

        # === 链路 metadata：让被调子 AstrBot（子 AstrBot等）能从 message body 顶部识别
        #     "这条消息来自主控 subagent_caller 链路"。
        #     4 行 metadata + --- 分隔符 + 原 message；username 字段保持真实 sender name，
        #     不污染子 AstrBot 端的用户名映射。
        link_tag = f"[🔗链路:{sender_digest[:8]}]"
        ts_iso = datetime.now(timezone.utc).isoformat()
        link_message = f"""{link_tag} via subagent_caller
  request_id: {request_id}
  from: {sender_digest}
  ts: {ts_iso}
---
{message}"""

        payload: dict[str, Any] = {
            "username": username,
            "message": link_message,
            "enable_streaming": True,
        }
        if session_id:
            payload["session_id"] = session_id

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        chunks: list[str] = []
        captured_session_id: Optional[str] = None
        first_chunk_time: Optional[float] = None
        # AstrBot SSE 去重：plain 是流式增量，complete 是同一段回复的完整版。
        # 收到任意 plain 之后，complete 事件直接跳过，避免把回复粘两遍。
        saw_plain_chunk: bool = False
        t0 = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            connector = self._build_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        # 不打 body 进日志（可能很长 + 含敏感信息）
                        logger.error(
                            f"[subagent call] phase=end request_id={request_id} "
                            f"status=http_{resp.status} total_s={time.time() - t0:.2f} "
                            f"body_chars={len(body)}"
                        )
                        raise SubAgentError(
                            f"子 AstrBot HTTP {resp.status}（{len(body)} 字节）"
                        )
                    content_type = resp.headers.get("Content-Type", "")
                    if content_type.startswith("text/event-stream"):
                        buf = b""
                        SSE_BUF_MAX = 1_048_576  # 1 MB——防 Gateway bug 把内存撑爆
                        done = False
                        async for raw_line in resp.content:
                            buf += raw_line
                            if len(buf) > SSE_BUF_MAX:
                                logger.warning(
                                    f"[subagent call] SSE buffer 超过 {SSE_BUF_MAX} 字节未换行，截断"
                                )
                                buf = b""
                                continue
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                line = line.decode("utf-8", errors="ignore").strip()
                                if not line or line.startswith(":"):
                                    continue
                                if line.startswith("data:"):
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        done = True
                                        break
                                    try:
                                        evt = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue
                                    evt_type = (
                                        evt.get("type")
                                        if isinstance(evt, dict)
                                        else None
                                    )
                                    # === plan B 关键：捕获子 AstrBot 的 session_id ===
                                    # AstrBot SSE 第一个事件就是 session_id：
                                    #   data: {"type": "session_id", "data": null, "session_id": "..."}
                                    # 不传 session_id 时 == 子 AstrBot 自动创建的 UUID；传了 == 回显
                                    if evt_type == "session_id" and isinstance(
                                        evt, dict
                                    ):
                                        sid = evt.get("session_id")
                                        if (
                                            isinstance(sid, str)
                                            and sid
                                            and not captured_session_id
                                        ):
                                            captured_session_id = sid
                                        continue
                                    # AstrBot SSE 去重：type=complete 跟在 type=plain 之后会发
                                    # 同一段回复的完整文本。若已收到过 plain 增量，跳过 complete，
                                    # 避免把回复粘两遍（最终结果会变成"流式片段"+"完整文本"）。
                                    if evt_type == "complete" and saw_plain_chunk:
                                        continue
                                    content = _extract_content(evt)
                                    if content:
                                        if first_chunk_time is None:
                                            first_chunk_time = time.time() - t0
                                            logger.info(
                                                f"[subagent call] phase=stream "
                                                f"request_id={request_id} "
                                                f"first_chunk_s={first_chunk_time:.2f}"
                                            )
                                        chunks.append(content)
                                        if evt_type == "plain":
                                            saw_plain_chunk = True
                            if done:
                                break
                    else:
                        # 非 SSE——非流式 JSON
                        body = await resp.text()
                        try:
                            obj = json.loads(body)
                            content = _extract_content(obj)
                            if content:
                                chunks.append(content)
                            else:
                                chunks.append(body[:1000])
                        except Exception:
                            chunks.append(body[:1000])
        except asyncio.TimeoutError:
            # 已收到部分内容 → 优雅降级：返回部分响应 + 提示
            partial = "".join(chunks).strip() if chunks else ""
            if partial:
                logger.warning(
                    f"[subagent call] phase=end request_id={request_id} "
                    f"status=partial_response timeout_s={timeout_seconds} "
                    f"chunks={len(chunks)} response_chars={len(partial)} "
                    f"total_s={time.time() - t0:.2f}（超时，但已收到部分内容）"
                )
                return CallResult(
                    reply=(
                        f"{partial}\n\n"
                        f"[⚠️ 子 AstrBot 响应超时（{timeout_seconds}s），以上为已收到的部分内容]"
                    ),
                    session_id=captured_session_id,
                )
            logger.error(
                f"[subagent call] phase=end request_id={request_id} "
                f"status=timeout timeout_s={timeout_seconds} "
                f"total_s={time.time() - t0:.2f}"
            )
            raise SubAgentError(f"子 AstrBot 请求超时（{timeout_seconds}s）")
        except aiohttp.ClientError as e:
            if chunks:
                partial = "".join(chunks).strip()
                logger.warning(
                    f"[subagent call] phase=end request_id={request_id} "
                    f"status=partial_response chunks={len(chunks)} "
                    f"response_chars={len(partial)} error={type(e).__name__} "
                    f"total_s={time.time() - t0:.2f}（连接中断，但已收到部分内容）"
                )
                return CallResult(
                    reply=(
                        f"{partial}\n\n"
                        f"[⚠️ 子 AstrBot 连接中断，响应可能不完整（{type(e).__name__}）]"
                    ),
                    session_id=captured_session_id,
                )
            logger.error(
                f"[subagent call] phase=end request_id={request_id} "
                f"status=connection_error error={type(e).__name__} "
                f"total_s={time.time() - t0:.2f}",
                exc_info=True,
            )
            raise SubAgentError("子 AstrBot 连接错误") from e
        except SubAgentError:
            raise
        except Exception as e:
            logger.error(
                f"[subagent call] phase=end request_id={request_id} "
                f"status=unknown_error error={type(e).__name__} "
                f"total_s={time.time() - t0:.2f}",
                exc_info=True,
            )
            raise SubAgentError("子 AstrBot 调用失败") from e

        if not chunks:
            logger.warning(
                f"[subagent call] phase=end request_id={request_id} "
                f"status=empty_response chunks=0 "
                f"total_s={time.time() - t0:.2f}"
            )
            return CallResult(
                reply="[子 AstrBot] (无返回)", session_id=captured_session_id
            )

        full_reply = "".join(chunks).strip()
        logger.info(
            f"[subagent call] phase=end request_id={request_id} "
            f"status=ok chunks={len(chunks)} "
            f"first_chunk_s={first_chunk_time if first_chunk_time is not None else 0:.2f} "
            f"total_s={time.time() - t0:.2f} "
            f"response_chars={len(full_reply)} "
            f"session_id_captured={'yes' if captured_session_id else 'no'}"
        )
        return CallResult(reply=full_reply, session_id=captured_session_id)

    def _build_connector(self):
        if self.verify_ssl:
            return aiohttp.TCPConnector()
        logger.warning(
            f"[subagent call] SSL 证书校验已关闭——Bearer Token 将以明文在网络中传输，"
            f"仅限本地/自签名证书场景（url={self.base_url}）"
        )
        return aiohttp.TCPConnector(ssl=False)


def _extract_content(evt: Any) -> str:
    """从一个 SSE JSON 事件里挑出 assistant 的 content 增量。

    兼容（按查找顺序，命中即返回）：
    1. **AstrBot 标准 SSE 格式**：
       - ``{"type": "plain", "data": "..."}``——LLM 流式增量（多次）
       - ``{"type": "complete", "data": "..."}``——LLM 完整文本（一次）
       - 其他 type（``agent_stats`` / ``end`` / ``message_saved`` / ``session_id`` /
         ``user_message_saved``）→ 视为元数据，data 字段非文本，返回空串
       - 注意：同一段回复会发两次（plain 是流式片段，complete 是完整版）。
         去重在 ``call()`` 循环里做——见了 plain 就标记 ``saw_plain_chunk``，
         见到 complete 时若 ``saw_plain_chunk`` 为真则跳过。
    2. **OpenAI 兼容 chunk**：``{"choices": [{"delta": {"content": "..."}}]}``
    3. **OpenAI 兼容非流式**：``{"choices": [{"message": {"content": "..."}}]}``
    4. **顶层 message 字段**：``{"message": "..."}`` 或 ``{"message": {"content": "..."}}``
    5. **顶层 content 字段**：``{"content": "..."}``
    """
    if not isinstance(evt, dict):
        return ""

    # 1. AstrBot 标准 SSE 格式：type=plain / type=complete
    evt_type = evt.get("type")
    if evt_type in ("plain", "complete"):
        data = evt.get("data")
        if isinstance(data, str):
            return data
        # data 不是字符串（image/obj 等消息段）——不算文本，跳过
        return ""

    # 2. OpenAI 兼容 chunk（流式 delta）
    choices = evt.get("choices")
    if isinstance(choices, list) and choices:
        c = choices[0]
        if isinstance(c, dict):
            delta = c.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    return content
            # 3. OpenAI 兼容（非流式 message）
            msg = c.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    return content

    # 4. 顶层 message 字段
    msg = evt.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content

    # 5. 顶层 content 字段
    content = evt.get("content")
    if isinstance(content, str):
        return content

    return ""
