# Pico Review Pack

面向真实代码仓的小型本地 coding agent 的评审材料索引。这份文件把 pico 的
定位、架构、证据与产物串成一条可复核的线索。

## Project pitch

Pico 是一个运行在本地的、小型的 coding agent，面向 Ollama、OpenAI 兼容、
Anthropic 兼容与 DeepSeek 后端。它把「稳定的系统提示 / 分层记忆 / 相关笔记 /
会话历史 / 当前请求」按预算拼装成 prompt，经过工具白名单与安全护栏（参数校验、
版本锚定、重复调用检测、审批、执行）落到本地文件与 shell，并以可复现的
trace / report 记录每一步决策。

## Architecture map

核心模块：

- `runtime.py` —— 控制循环：组 prompt、解析模型输出、校验并执行工具、写 trace、
  更新工作记忆、在合适时停下。
- `context_manager.py` —— 上下文预算与两级压缩（按 section 收缩 + 旧工具输出
  折叠/摘要）。
- `memory.py` —— 分层记忆（会话 / 工作记忆 / 长期记忆），含文件摘要与版本 hash
  （`last_seen_hashes`）锚定。
- `tools.py` —— 能力白名单与工具执行；`locate_old_text` 负责 patch 目标定位。
- `workspace.py` —— 工作区快照与路径逃逸防护。
- `evaluator.py` —— 确定性基准（FakeModelClient 脚本化输出 + verifier）。

## Benchmark evidence

基准回归（`benchmarks/coding_tasks.json`）用脚本化的确定性输出驱动，覆盖
文档编辑、工具边界（非法参数恢复、路径逃逸恢复、重复读取恢复）、上下文压缩
checkpoint、freshness 重锚定恢复、workspace 漂移恢复、长期记忆晋升/拒绝等场景。
每次运行使用全新的 fixture 副本与 run 目录，保证 diff 稳定可复现。

## Sample run artifact list

一次典型运行的产物（位于 workspace 的 `.pico/` 下）：

- `runs/<run_id>/trace.jsonl` —— 每个事件的逐行 JSON trace（prompt 组装、
  工具执行、checkpoint 等）。
- `runs/<run_id>/report.json` —— 脱敏后的结构化报告（task state、prompt metadata、
  长期记忆晋升/拒绝）。
- `sessions/` —— 可恢复的会话状态。
- `memory/topics/` —— 沉淀到长期记忆的主题笔记。
