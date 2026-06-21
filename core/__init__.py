"""astrbot_plugin_subagent_caller core 子包。

把 main.py 拆出的纯逻辑层（无 AstrBot Star 依赖）：
- util：常量、异常、类型转换、错误脱敏
- access：白名单校验
- storage：SQLite 数据库（任务 + 子 AstrBot 实例）
- client：AstrBotClient（封装 /api/v1/chat HTTP API）
- runner：后台任务 runner
- api：Plugin Page Web API handlers

main.py 仅做薄入口：Star 注册、filter/llm_tool 装饰器、state 初始化。
"""
