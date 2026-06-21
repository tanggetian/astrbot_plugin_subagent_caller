# astrbot_plugin_subagent_caller

![logo](logo.png)

> **把其他 AstrBot 实例当成"子 agent"来调用。**
> 当前 AstrBot 通过 OpenAPI 把消息 / 任务转发给一个或多个子 AstrBot 处理，再把结果带回来。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.18.0-blueviolet)](https://docs.astrbot.app)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-orange)](CHANGELOG.md)
[![GitHub](https://img.shields.io/badge/GitHub-tanggetian-black?logo=github)](https://github.com/tanggetian/astrbot_plugin_subagent_caller)

---

## 📑 目录

- [✨ 能做什么](#-能做什么)
- [📦 安装](#-安装)
- [🧪 子 AstrBot 准备](#-子-astrbot-准备)
- [⚙️ 配置](#️-配置)
- [🚀 使用方法](#-使用方法)
- [🛠 WebUI](#-webui)
- [🐛 故障排除](#-故障排除)
- [🔐 安全说明](#-安全说明)
- [🌐 国际化](#-国际化)
- [📄 许可 & 链接](#-许可--链接)

---

## ✨ 能做什么

| 类别 | 功能 |
|---|---|
| **同步调用** | 把消息发给指定子 AstrBot 跑一次 LLM 对话，把结果带回来 |
| **异步任务** | 把任务丢给后台跑，拿到 `task_id` 后随时查状态、取结果、取消 |
| **项目 session 多轮对话** | 在某个主题下连续多轮问同一个子 AstrBot，多轮上下文由子 AstrBot 自己维护，**不消耗主控 token** |
| **同一子实例多 project** | 同一个子 AstrBot 可以开 `weather` / `research` 等多个项目会话——互不串扰 |
| **广播** | 一句话同时打所有启用的子 AstrBot，并行收集结果 |
| **LLM Tool 自主调度** | 让主 AstrBot 的 LLM 自己决定"这件事派给哪个子 agent" |
| **白名单** | 只有白名单里的用户能调 |
| **WebUI 实例 / 任务 / 项目会话管理** | 三 tab 一站式管理 |

适用场景：
- 把不同 persona / 模型 / 知识库 / 工具集的任务分发给多台 AstrBot，并行处理
- 把负载分流到多台 AstrBot
- 在某主题下持续和同一个子 AstrBot 多轮对话

---

## 📦 安装

### 方式 A：AstrBot 插件市场（推荐）

1. 打开 AstrBot WebUI → **设置 → 插件源**
2. 搜索 `astrbot_plugin_subagent_caller` 并安装
3. 重启 AstrBot（或在 WebUI 插件管理点 **重载插件**）

### 方式 B：从源码部署

```bash
# AstrBot 数据目录
cd /path/to/AstrBot/data/plugins

# 拷过去
cp -r /path/to/astrbot_plugin_subagent_caller .

# 重启 AstrBot
```

### 依赖

```text
aiohttp>=3.11
```

已写在 `requirements.txt`，AstrBot 会自动安装。

---

## 🧪 子 AstrBot 准备

子 AstrBot 需要先创建一个 API Key。

1. 子 AstrBot 必须是 **≥ 4.18.0** 版本
2. 打开子 AstrBot 的 **WebUI → 设置**，创建一个 API Key
3. **勾选 `chat` scope**（必须，否则调不通）
4. 复制 `abk_xxx` 形式的 Token，记下子 AstrBot 的根 URL（如 `http://192.168.1.10:6185`）

### 验证子 AstrBot 可达

```bash
curl -H "Authorization: Bearer abk_你的token" \
     http://子AstrBot:6185/api/v1/openapi.json | head
```

返回 JSON（不是 401 / 404）就说明鉴权 + 网络都通了。

---

## ⚙️ 配置

打开 AstrBot WebUI → **插件管理 → astrbot_plugin_subagent_caller → 配置**。

### 顶层配置项

| key | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `default_timeout` | int | ❌ | `60` | 同步 / 异步调用共用超时（秒）。长任务调到 `300~1800`。 |
| `max_concurrent` | int | ❌ | `5` | 全局最大并发调用数。广播时受此信号量限制。 |
| `verify_ssl` | bool | ❌ | `true` | 全局是否校验子 AstrBot TLS 证书。自签证书时关掉。 |
| `session_lock_timeout` | int | ❌ | `30` | 同一 `(subagent, project)` 串行调用的锁等待超时（秒）。避免并发同 key 上下文串扰。 |
| `storage_path` | str | ❌ | `data/subagent_tasks.db` | 保留兼容，实际 SQLite 路径在 `data/plugins/astrbot_plugin_subagent_caller/subagent_caller.db`。 |
| `subagents[]` | list | ❌ | `[]` | 首次启动时 seed 到 DB 的初始实例；之后改这里**无效**，请在 WebUI 实例管理页面改。 |
| `access_control.whitelist_enabled` | bool | ❌ | `true` | 启用白名单。**强烈建议默认开启**。 |
| `access_control.allowed_user_ids` | list[str] | ❌ | `[]` | 白名单用户 ID 列表。 |
| `access_control.block_when_disabled` | bool | ❌ | `false` | 未在白名单时是否给用户明确提示。 |

### 子实例项 `subagents.items` 字段

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | str | `""` | 唯一名称（字母数字下划线即可） |
| `base_url` | str | `""` | 子 AstrBot 根 URL（**不含** `/api/v1/chat`），如 `http://192.168.1.10:6185` |
| `token` | str | `""` | 子 AstrBot API Key（Bearer Token，`abk_xxx`），**至少需要 `chat` scope** |
| `description` | str | `""` | 备注（可选） |
| `username` | str | `""` | 子 AstrBot WebUI chat 流 sender 列显示什么。**留空 = fallback 到主控 sender** |
| `enabled` | bool | `true` | 是否启用 |
| `verify_ssl` | bool/null | `null` | 覆盖全局 `verify_ssl`（`null` = 沿用全局） |

### 怎么获取用户 ID

私聊 AstrBot 发任意消息，然后看 AstrBot 日志中的 `sender_id`。

### 子 AstrBot 实例怎么 seed

启动时如果 `subagent_instances` 表为空，会把 `_conf_schema.json.subagents[]` 数组里的每项写入；
**之后**所有增删改都走 WebUI 实例管理页面。

要重新从配置覆盖数据库：

1. 删掉 `data/plugins/astrbot_plugin_subagent_caller/subagent_caller.db`
2. 改 `_conf_schema.json.subagents` 列表
3. 重启 AstrBot

---

## 🚀 使用方法

### 1. 手动命令

| 命令 | 说明 |
|---|---|
| `/subagent_call <name> <消息>` | 同步调一次子 AstrBot |
| `/subagent_task <name> <任务>` | 提交后台任务，返回 `task_id` |
| `/subagent_status <task_id>` | 查任务状态 |
| `/subagent_cancel <task_id>` | 取消运行中任务 |
| `/subagent_broadcast <消息>` | 广播给所有启用的子 AstrBot |
| `/subagent_list` | 列出已注册的子 AstrBot |
| `/subagent_chat <name> <project> <消息>` | 项目 session 多轮对话（前台同步） |
| `/subagent_chat_task <name> <project> <消息>` | 项目 session 多轮对话（后台异步） |
| `/subagent_chat_list` | 列出活跃项目 session |
| `/subagent_chat_clear <name> <project>` | 删 plugin 端 session 映射 → 下次同 key 调用自动开新对话 |

示例：

```text
# 一次调用
/subagent_call my-bot 帮我写一份 Python 教程

# 异步任务
/subagent_task my-bot 帮我做一次深度调研

# 项目 session 多轮对话
/subagent_chat my-bot weather 杭州今天天气怎么样
/subagent_chat my-bot weather 那明天呢              # ← 自动续上下文
/subagent_chat my-bot research 调研一下 LLM 推理优化
/subagent_chat_list                                # 看活跃项目 session
/subagent_chat_clear my-bot weather                # 删 mapping，下次重新开对话
```

### 2. 让主 LLM 自主调用（LLM Tool）

直接描述需求即可，主控 LLM 会自主选择子 AstrBot。插件注册的 LLM Tool：

| Tool | 说明 |
|---|---|
| `call_subagent(subagent, message, project="")` | 前台同步调一个子 AstrBot；`project` 为空时走默认 `__default__` 连续上下文 |
| `call_subagent_async(subagent, message, project="")` | 后台异步调一个子 AstrBot；项目 session 规则同上，立即返回 `task_id` |
| `broadcast_to_subagents(message)` | 广播给所有启用的子 AstrBot |
| `list_subagents()` | 列出已启用子 AstrBot 的 `name` / `description` / `username`，供主 LLM 选 `subagent` 参数 |
| `get_subagent_task_result(task_id, max_chars)` | 读后台任务结果 |
| `clear_subagent_project(subagent, project)` | 删 plugin 端 session 映射，下次同 key 调用自动开新对话 |

> 想不阻塞当前对话，就用 `call_subagent_async`，任务跑完会主动通知用户。

### 3. 项目 session 多轮对话

**适用场景**：在某个主题下连续 3+ 轮问同一个子 AstrBot。

**核心原理**（了解即可）：

- 以 `(subagent, project)` 为唯一 key
- **多轮上下文由子 AstrBot 自己维护**（plugin 不拼接 history 重发，省 token）
- plugin 端**只**存 `(subagent, project) → 子 AstrBot session_id` 映射
- 同一子实例可开多个 project（`weather` / `research` 等），互不串扰
- 同一 `(subagent, project)` 串行调用，避免上下文串扰

**工作示例**：

```text
# 项目 A：天气（首次调用——plugin 不传 session_id，子 AstrBot 自己建新会话）
/subagent_chat my-bot weather 杭州今天天气怎么样
→ my-bot: 杭州今天晴，25°C。

# 项目 A：续上下文（plugin 把上次拿到的 session_id 透传回去）
/subagent_chat my-bot weather 那明天呢
→ my-bot: 明天多云，22°C，记得带伞。

# 项目 B：调研（同一子实例，但独立 session，互不干扰）
/subagent_chat my-bot research 调研 LLM 推理优化技术
→ my-bot: 当前主流技术有...

# 后台异步（用同一 session 续上下文）
/subagent_chat_task my-bot research 进一步深入模型量化
→ 返回 task_id；跑完会主动通知

# 列出 / 删除项目 session
/subagent_chat_list                       # 列所有 (subagent, project) → session_id 映射
/subagent_chat_clear my-bot weather       # 删 mapping → 下次同 key 调子 AstrBot 自动开新会话
```

**强制开新对话**：

- `/subagent_chat_clear <name> <project>` —— 删 plugin 端映射，下次同 key 调用自动开新会话
- LLM Tool 用 `clear_subagent_project(subagent, project)`

**项目名命名规则**：只允许字母 / 数字 / 下划线 / 点 / 横线，长度 `1~64`。

### 4. 广播

```text
/subagent_broadcast 自我介绍一下
```

会同时打所有启用的子 AstrBot，并行汇总返回。受 `max_concurrent` 信号量限制。

### 5. 异步任务流

```text
1. /subagent_task my-bot 帮我做一次深度调研
   → 返回 task_id：sa-1700000000000-abcdef12

2. /subagent_status sa-1700000000000-abcdef12
   → 查任务当前状态（pending / running / done / failed / cancelled）

3. /subagent_cancel sa-1700000000000-abcdef12
   → 取消还在跑的任务
```

任务也可以在 WebUI **任务列表** tab 里看、取消、删除。

---

## 🛠 WebUI

打开 AstrBot WebUI → **插件管理 → astrbot_plugin_subagent_caller**，里面有三个 tab：

- **实例管理**（默认打开）
  - 增删改查子 AstrBot 实例
  - ping 健康检查、启停、SSL 配置
  - Token 字段留空 = 保留原值（编辑时不会丢 token）
  - 10 秒自动刷新
- **任务列表**
  - 运行中 / 历史任务，可取消 / 删除
  - 项目 session 的后台任务也会在这里列出
  - 5 秒自动刷新
- **项目会话**
  - 列出所有活跃 `(subagent, project) → 子 AstrBot session_id` 映射
  - 「删 mapping」= 清 plugin 端 session_id 记录（下次调子 AstrBot 自动开新 UUID = 强制开新对话）
  - 8 秒自动刷新

---

## 🐛 故障排除

按顺序检查：

1. **检查子 AstrBot 可达**：用上面 [验证子 AstrBot 可达](#验证子-astrbot-可达) 的 curl 命令
2. **检查 token**：确认 `abk_xxx` 与子 AstrBot WebUI 中创建的一致，未过期，含 `chat` scope
3. **检查日志**：在 AstrBot 日志中搜 `[subagent` 关键字
4. **检查白名单**：确认 `access_control.allowed_user_ids` 包含你的 `user_id`
5. **检查 SSL**：自签名证书场景需要把子实例的 `verify_ssl` 关掉

启动时日志会输出：

```text
[subagent_caller] 初始化完成: default_timeout=... subagents_total=N enabled=M
```

可用于确认配置读取是否正确（不会输出 token）。

### 常见问题

**Q：调用返回 `chunks=0 (无返回)`？**
通常是 SSE schema 不匹配。本插件已支持 AstrBot v4.18+ 标准 schema（`type=plain` / `type=complete`），
保留 OpenAI 兼容 / 顶层 message / 顶层 content 字段 fallback。如果还出问题，看日志
`[subagent call] phase=stream first_chunk_s=...` 有没有打出来。

**Q：编辑实例后调用报 401 / "未注册或被禁用"？**
大概率是**编辑时 token 被覆盖了**。本插件编辑实例时，token 字段**留空 = 保留原值**；
要换 token 才填新值。

**Q：项目 session 怎么强制开新对话？**
`/subagent_chat_clear <name> <project>` 或 LLM Tool `clear_subagent_project(subagent, project)`。

**Q：广播结果太长被截断？**
广播结果会拼成一条消息返回——子 AstrBot 数量多 / 回复长时可能触发上游 IM 平台字数限制。
默认单条 ≤ 4000 字符。

---

## 🔐 安全说明

- **白名单**：`access_control.whitelist_enabled=True` 时只有 `allowed_user_ids` 列表里的用户能调
- **Token 脱敏**：WebUI 列表 / 日志中只显示 `abk****xxxx` 形式
- **日志脱敏**：完整 URL / Token / sender_id / 任务文本 / 响应文本**不**进 AstrBot 日志；只记 digest（SHA1 前 8 字符）+ 字符数
- **链路 metadata**：调用时在 message body 顶部注入 4 行 metadata（`link_tag` / `request_id` / `from` / `ts`）+
  `---` 分隔符 + 原 message——被调方 WebUI 的 chat 流 sender 列能识别"这条消息来自主控 subagent_caller 链路"
- **SSL 校验可关闭**：自签名证书场景支持，但会在日志中提示"Token 明文传输"
- **数据库隔离**：每个 AstrBot 实例的 `subagent_tasks` / `subagent_instances` 表彼此独立
  （独立 SQLite 在 `data/plugins/astrbot_plugin_subagent_caller/subagent_caller.db`）

---

## 🌐 国际化

插件自带中英双语 i18n 资源（`.astrbot-plugin/i18n/{zh-CN,en-US}.json`），覆盖：

- 插件名称、卡片短描述、详细描述
- 配置项 `description` / `hint` 文案
- WebUI 标题 / 描述

WebUI 会按当前 WebUI 语言自动切换。修改 i18n 后**重载插件**生效。

---

## 📄 许可 & 链接

- **许可**：[GNU AGPL v3](LICENSE)
- **GitHub**：[tanggetian/astrbot_plugin_subagent_caller](https://github.com/tanggetian/astrbot_plugin_subagent_caller)
- **更新日志**：[CHANGELOG.md](CHANGELOG.md)
- **AstrBot 文档**：<https://docs.astrbot.app>

### 兼容性

- AstrBot **≥ 4.18.0**（依赖 OpenAPI `/api/v1/chat` + API Key + `chat` scope）
- Python **≥ 3.10**
- 依赖：`aiohttp>=3.11`

### 已知约束

- 子 AstrBot 必须 ≥ 4.18.0
- 广播结果拼接在单条消息里——子 AstrBot 数量多 / 回复长时可能触发 IM 平台字数限制
- 主 AstrBot 切换 sender name 不会同步到子 AstrBot 那边（两套独立 user 系统）—— 用每个子实例的 `username` 字段固定
