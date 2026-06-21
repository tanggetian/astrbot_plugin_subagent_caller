# 更新日志

所有对本插件的**用户可见**改动都记录在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更 (Changed)

- **LLM Tool 调用方式重构为两个正交工具**：
  - `call_subagent(subagent, message, project="")`：前台同步调用；`project` 为空时使用默认 `__default__` 连续上下文
  - `call_subagent_async(subagent, message, project="")`：后台异步调用；项目 session 行为与前台一致，立即返回 `task_id`
  - 移除旧 `call_subagent_with_project` 工具；原能力由 `call_subagent(..., project=...)` 覆盖
- **LLM 可发现子实例**：新增 `list_subagents()`，只返回 `name` / `description` / `username`，不暴露 token / base_url。
- **同步调用返回值修复**：`call_subagent` 正确返回 `reply=result.reply`，并带回子实例 `session_id`。

## [1.2.0] - 2026-06-20

### 变更 (Changed)

- **项目 session 改成 plan B**——plugin 端**不**再维护 history，**只**存 `(subagent, project) → 子 AstrBot session_id` 映射；多轮上下文由**子 AstrBot 自己**的 `(username, session_id)` chat history 累积
  - **调研结论**（对照 AstrBot 框架 `dashboard/routes/open_api.py` + `chat.py` 源码）：
    - AstrBot `/api/v1/chat` 接受 `username`（必填）+ `session_id`（可选）参数
    - 不传 `session_id` → AstrBot 框架自动 `uuid4()` 建 UUID
    - SSE 第一个事件 `{"type": "session_id", "data": null, "session_id": "..."}` 返回这个 UUID
    - AstrBot 按 `(username, session_id)` 维护 chat history——plugin 透传同一对 key 就续上下文
  - **解法**：plugin 第一次调用**不**传 `session_id` → SSE 捕获 UUID → 写回 plugin DB；后续调用 plugin 把存的 UUID 传回给子 AstrBot
  - **核心代码改动**：
    - `core/client.py` —— `call()` 新增 SSE `type=session_id` 事件捕获；返回 `CallResult(reply, session_id)` dataclass（替代纯 `str`）
    - `core/storage.py` —— `project_messages` 表彻底 DROP；`project_sessions` 表加 `astrbot_session_id` 列；新增 `set_astrbot_session_id()` 方法
    - `core/sessions.py` —— 删 `HistoryMessage` / `build_session_message` / `_enforce_cap`；`ProjectSession` 只持有 `astrbot_session_id` 指针
    - `core/runner.py` —— `background_run(project=..., session_manager=...)` 走 plan B：拿锁 → 读 UUID → 调子 AstrBot → 捕获返回 UUID → 写回映射
    - `main.py` —— 同步版 `/subagent_chat` / `call_subagent_with_project` 同样简化
  - **DB 迁移（自动）**：`init_db()` 加 ALTER TABLE 兼容老 v1.1 db（加 `astrbot_session_id` 列）+ DROP 老 `project_messages` 表（保留 v1.1 老 session header row，UUID 默认空——下次调自动填）
  - **API endpoint 行为变更**：
    - `GET /project_sessions/get` —— v1.2+ `messages` 字段永远空数组（向后兼容）；`header` 现在带 `astrbot_session_id`
    - `POST /project_sessions/clear` —— 删 plugin 端 `(subagent, project) → 子 AstrBot session_id` 映射行（下次同 key 调用子 AstrBot 自动建新 UUID，强制开新对话）
  - **命令 / LLM Tool 行为变更**：
    - `/subagent_chat_history` 命令**删除**（plugin 端没有 history 可看）
    - `/subagent_chat_clear` 命令**语义变更**：从「清 history（保留 header）」→「删 session_id 映射」（下次同 key 调子 AstrBot自动开新 UUID）
    - `clear_subagent_project` LLM Tool 同步重定义：从「清 messages」→「删映射」
  - **WebUI 变更**：
    - 项目会话 tab 删 history 详情区（点开看 messages 的 modal/table）
    - 主表加列「子 AstrBot session_id」（短 UUID 截断显示，鼠标 hover 看完整）
    - 「详情」按钮删了；「清空」改名「删 mapping」+ 文案解释 plan B 语义
    - 头部加 v1.2+ plan B 说明 + hint 文字
  - **配置项**：
    - `session_max_turns` 配置项**保留兼容**，但**不再生效**（history 在子 AstrBot 那边）
    - `session_lock_timeout` **仍生效**（避免并发同 key UUID 写乱）
  - **测试**：`tests/test_sessions.py` 重写为 v1.2+ plan B 11 项测试——保留 schema / UNIQUE / set_astrbot_session_id / validate_project_name / lock / list_active / delete；**删** build_message / append / truncate / cap / empty / clear_messages；**新加** v1.1→v1.2 老 db 迁移测试（手建 v1.1 schema 跑 init_db 验证自动加列 + DROP 老表 + 老 row 保留）

### 兼容性

- v1.1 老 db 升级 v1.2.0 **无需手工迁移**——`init_db()` 自动：
  1. ALTER TABLE 加 `astrbot_session_id` 列（v1.1 老 row 默认空字符串）
  2. DROP 老 `project_messages` 表（plugin 端 history 已没用——真正的 history 在子 AstrBot 那边）
  3. 老 (subagent, project) 映射行**保留**——下次调一次自动捕获子 AstrBot 返回的 UUID 填上
- v1.1 老 `project_messages` 表里的内容**丢**——但那些是 plugin 自己拼接的副本，真 history 早就在子 AstrBot 自己的 chat_history 表里
- 命令 / Tool API **签名完全不变**：`/subagent_chat` / `/subagent_chat_task` / `/subagent_chat_list` / `/subagent_chat_clear` / `call_subagent_with_project` / `clear_subagent_project` 名字 + 参数一字不改
- 唯一删除的对外接口：`/subagent_chat_history` 命令（plugin 端已无 history 可看；想去子 AstrBot 那边查）

### 性能 / 成本

- **省 token**：plugin 不再拼接 history 重发，每次只发新消息 + session_id——长任务 cost 显著下降
- **省 CPU**：plugin 不维护 `HistoryMessage` 列表、不跑 cap truncate
- **DB 表少 1 张**：`project_messages` DROP——SQLite 文件体积减小

## [1.1.0] - 2026-06-20

### 新增 (Added)

- **项目 session 多轮对话**（v1.1+ 核心新功能）—— 解决主控 LLM 手动拼 history 不可扩展 + 不优雅 + 上下文长会超 token 的痛点
  - **核心概念**：以 `(subagent_name, project_name)` 为 session key（DB UNIQUE 约束），
    每对 key 独立 history buffer，同一 subagent 可开多个 project 互不串扰
  - **前台同步命令** `/subagent_chat <name> <project> <消息>`——plugin 在锁内「拉 history →
    拼 message → 调 subagent → 写 history」一次完成，第 N 轮子 AstrBot 自动收到前 N-1 轮上下文
  - **后台异步命令** `/subagent_chat_task <name> <project> <消息>`——同样累积 history，
    跑后台返回 `task_id`（前缀 `ps-`），任务在 WebUI 任务列表 tab 看得到、可以 cancel
  - **管理命令**：
    - `/subagent_chat_list` 列活跃 (subagent, project) session
    - `/subagent_chat_history <name> <project> [N=10]` 看最近 N 条 history（最大 50）
    - `/subagent_chat_clear <name> <project>` 清 history（保留 session 头）
  - **LLM Tool**（让主控 LLM 自主）：
    - `call_subagent_with_project(subagent, message, project, background)`——
      同步 / 异步两种模式，主控 LLM 不用手动拼 context
    - `clear_subagent_project(subagent, project)`——强制重置 session
  - **持久化**：SQLite 新增两张表（`project_sessions` 头 + `project_messages` 消息明细），
    history 跨进程重启不丢数据
  - **容量上限**：`session_max_turns` 轮（默认 20，每轮 = 1 user + 1 assistant），
    超了自动 truncate 最早消息；不会无限增长
  - **并发安全**：每个 (subagent, project) 一个 `asyncio.Lock`，同 key 多次并发**串行**；
    锁等待超时 `session_lock_timeout` 秒（默认 30）报明确错误
  - **错误处理**：subagent 不存在 / project 名非法（只允许字母/数字/下划线/点/横线）/
    锁等待超时 / 调用失败 全部有明确错误，并按情况写一条 `[错误 ...]` system/assistant 消息到 history
  - **WebUI 第三个 tab「项目会话」**：在原有 `实例管理` + `任务列表` tab 基础上新增，
    列出所有活跃 session，点「详情」看 history，点「清空」/「删除」管理
  - **i18n 同步**：`.astrbot-plugin/i18n/{zh-CN,en-US}.json` 新增 `tab_sessions` /
    `sessions_heading` 等 7 个 key；plugin 描述补项目 session
  - **集成测试**：`tests/test_sessions.py` 14 项测试覆盖 schema / UNIQUE / append /
    truncate / clear / delete / role 校验 / 名称校验 / 拼 message / 空 history /
    容量上限 / 锁串行 / 锁并行 / 列表 / 锁超时——`python3 tests/test_sessions.py` 全绿
  - **配置项**：`_conf_schema.json` 新增 `session_max_turns`（默认 20）+
    `session_lock_timeout`（默认 30）—— WebUI 配置页可调

### 变更 (Changed)

- **WebUI 单页面 + 内部 tabs 切换**：合并原 `pages/subagent-manage/` 和 `pages/subagent-tasks/` 两个 page 为 `pages/subagent-page/` 单页面
  - **根因**：AstrBot WebUI 框架 `dashboard/src/composables/usePluginSidebarItems.ts:35` 在 sidebar 只为每个 plugin 创建一个菜单项（取 `pages[0]`），原「任务列表」page 没有 sidebar 入口（路径是 AstrBot 上游源码，本插件不再 vendored 框架源码）
  - **解法**：合并到单 page + 内部 tabs 切换（`实例管理` / `任务列表` / `项目会话`），sidebar 自动显示这个新 page
  - **保留所有现有功能**：
    - 实例管理 tab：实例列表、新增/编辑/删除、ping 健康检查、启用/禁用、SSL 配置（10 秒自动刷新）
    - 任务列表 tab：任务列表（运行中 + 历史）、cancel、delete（5 秒自动刷新）
    - 项目会话 tab：活跃 session 列表 + history 详情 + 清空 / 删除（8 秒自动刷新）
  - **URL hash 同步**：`#/tasks` 直达任务列表 tab；`#/sessions` 直达项目会话 tab，方便主人收藏 / 分享
  - **i18n 同步**：`.astrbot-plugin/i18n/{zh-CN,en-US}.json` 中 `pages.subagent-manage` + `pages.subagent-tasks` 合并为 `pages.subagent-page`，新增 `tab_manage` / `tab_tasks` / `tab_sessions` / `manage_heading` / `tasks_running_heading` / `tasks_history_heading` / `sessions_heading` 七个 key
  - **后端 `main.py`**：为新 page 增 `register_web_api` 4 个 project session endpoint（`project_sessions` / `get` / `clear` / `delete`），9 + 4 = 13 个 endpoint 总数

### 兼容性 (Compatibility)

- v1.0.0 全部行为**完全保留**：
  - `/subagent_call` / `/subagent_task` / `/subagent_status` / `/subagent_cancel` / `/subagent_broadcast` / `/subagent_list` 命令一字未改
  - `call_subagent` / `broadcast_to_subagents` / `get_subagent_task_result` 三个 LLM Tool 行为完全不变
  - `subagent_tasks` / `subagent_instances` 表 schema 兼容
- SQLite 老 db 升级 v1.1.0 **无需任何手工迁移**——`init_db()` 用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 兼容老列缺列
- WebUI 升级 v1.1.0 **无需重载数据**——`tab_sessions` 自动出现，按钮自动绑定

## [1.0.0] - 2026-06-19

首个发布版本。所有功能已在主控 + 子 AstrBot（被调方）跑通端到端验证。

### 新增 (Added)

- **多子 AstrBot 实例管理**（`pages/subagent-manage/`）
  - 增删改查子 AstrBot 实例，WebUI 实时生效
  - 健康检查（Ping 走 `/api/v1/openapi.json`，不消耗 chat 配额）
  - 启停开关、SSL 校验开关（自签名证书场景）
  - Token 字段留空 = 保留原值（编辑时无需重输）
- **同步调用**：`/subagent_call <name> <消息>` —— 把消息发给指定子 AstrBot 跑一次 LLM 对话并原样回传
- **异步任务**：`/subagent_task <name> <任务>` —— 后台跑、返回 `task_id`、`/subagent_status` / `/subagent_cancel` 查/取
- **广播**：`/subagent_broadcast <消息>` —— 并行 `asyncio.gather` 调所有启用实例，受 `max_concurrent` 信号量限制
- **WebUI 任务列表**（`pages/subagent-tasks/`）—— 运行中 / 历史任务，5 秒自动刷新
- **LLM Tool 集成**：主控 LLM 自主调度子 AstrBot
  - `call_subagent(subagent, message, background)` — 调一个子 AstrBot
  - `broadcast_to_subagents(message)` — 广播
  - `get_subagent_task_result(task_id, max_chars)` — 读后台任务结果
- **白名单**（`access_control`）—— 默认开启，只有 `allowed_user_ids` 列表内用户能调
- **SQLite 任务审计** —— 插件独立 DB（`data/plugins/astrbot_plugin_subagent_caller/subagent_caller.db`），三张表：
  - `subagent_tasks`（任务生命周期）
  - `subagent_instances`（子 AstrBot 实例注册表）
  - 列迁移兼容老 DB（`verify_ssl` / `username` 缺列时自动 ALTER TABLE 补）
- **配置国际化**（`.astrbot-plugin/i18n/{zh-CN,en-US}.json`）
- **Plugin Page 后端 API** 9 个 endpoint（子 AstrBot CRUD + 任务管理）

### 安全 (Security)

- **链路 metadata**：每次 call 在 message body 顶部注入 4 行 metadata（`link_tag` / `request_id` / `from` / `ts`）+
  `---` 分隔符 + 原 message —— 被调方 WebUI 的 chat 流 sender 列能识别"这条消息来自主控 subagent_caller 链路"
- **日志脱敏**：完整 URL / Token / sender_id / 任务文本 / 响应文本**不进** AstrBot 日志；只记 digest（SHA1 前 8 字符）+ 字符数
- **Token 脱敏**：WebUI 列表 / 日志只显示 `abk****xxxx` 形式
- **SSL 校验可关闭**：自签名证书场景支持（但会在日志中显式提示"Token 明文传输"）

### 修复 (Fixed)

- **AstrBot 标准 SSE 格式支持**（v4.18+）：
  - 识别 `type=plain` / `type=complete` 事件返回 `data` 字段（标准 schema 修复前 `chunks=0` 返回 "(无返回)"）
  - `saw_plain_chunk` 去重：plain 增量收过再遇 complete 跳过，避免回复粘两遍
  - 保留 OpenAI 兼容 / 顶层 message / 顶层 content 字段支持（向后兼容老 Gateway）
- **WebUI 编辑实例 token 留空 = 保留原值**：避免主人每次改任意配置都必须重输 token
- **username 字段 WebUI 渲染**：表单 + 列表都展示"子 AstrBot WebUI chat 流 sender 列显示什么"

### 已知约束 (Known Limitations)

- 子 AstrBot 必须 ≥ 4.18.0（依赖 OpenAPI `/api/v1/chat` + API Key + `chat` scope）
- 广播结果拼接在单条消息里返回——子 AstrBot 数量多 / 回复长时可能触发上游 IM 平台字数限制（默认单条 ≤ 4000 字符）
- 主 AstrBot 切换 sender name 不会同步到子 AstrBot 那边（两套独立 user 系统）—— 用每个实例的 `username` 字段固定

### 兼容性 (Compatibility)

- AstrBot ≥ 4.18.0（PEP 440：`>=4.18.0,<5`）
- Python ≥ 3.10（`str | None` 语法需要）
- 依赖：`aiohttp>=3.11`（AstrBot 间接提供 `quart` 用于 Web API handler）

[1.0.0]: https://github.com/tanggetian/astrbot_plugin_subagent_caller/releases/tag/v1.0.0
[1.1.0]: https://github.com/tanggetian/astrbot_plugin_subagent_caller/releases/tag/v1.1.0
[1.2.0]: https://github.com/tanggetian/astrbot_plugin_subagent_caller/releases/tag/v1.2.0
