# Agent Harness v1

Pico 的 harness v1 概览：一个把模型输出约束到可审计动作集合里的控制循环。

## 核心循环

每个请求走一次 ReAct 式循环：组 prompt → 调模型 → 解析工具调用 → 校验并执行 →
把结果写回历史与工作记忆 → 决定继续还是停下。

## 关键约束

- **能力白名单**：工具是显式注册的，模型只能申请清单内的动作。
- **安全护栏管线**：validate（参数形状）→ version-guard（编辑版本锚定，拒绝
  过期文件 / 缺失上下文 / 多处匹配）→ repeated-detection（重复调用检测）→
  approve（审批策略）→ execute。
- **secret 脱敏**：trace/report 里的敏感环境变量值一律替换为 `<redacted>`。

## task state 与恢复

每次运行落一个 `TaskState`（run_id、task_id、step 计数、停止原因、attempts）。
通过 checkpoint 与 `runtime_identity`（workspace fingerprint）支持 freshness
不匹配 / workspace 漂移后的局部恢复，而不是从头重跑。

## 上下文预算

`ContextManager` 按 section（prefix / memory / relevant_memory / history /
current_request）分配预算，超预算时按固定顺序收缩，并始终保留最新用户请求。
旧工具输出会被折叠成一行摘要，重复读取复用文件摘要，以减少 prompt 预算。
