# Pico 架构设计文档

> 本文档面向希望理解 pico 内部设计的读者，逐个拆解 **上下文管理**、**记忆系统**、**工具系统**、**断点与恢复系统**、**评测系统** 五大子系统。所有结论都对照源码，关键常量与算法均给出精确数值与出处。

---

## 0. 总览

`pico` 是一个跑在终端里的轻量本地 coding agent。它的核心可以用一句话概括：

> 把「用户的一句话请求」扩展成一条「感知 → 决策 → 行动 → 记录」的控制循环，期间每一步都受预算、安全、记忆、持久化的约束。

### 0.1 模块地图

| 文件 | 职责 | 对应子系统 |
| --- | --- | --- |
| [pico/cli.py](pico/cli.py) | 命令行入口：解析参数、装配 agent、one-shot / REPL 循环 | 装配层 |
| [pico/models.py](pico/models.py) | 模型后端适配：把不同 provider 抹平成统一的 `complete()` | 模型层 |
| [pico/workspace.py](pico/workspace.py) | 工作区快照：Git 事实 + 白名单项目文档 | 上下文（prefix 的底座） |
| [pico/context_manager.py](pico/context_manager.py) | Prompt 组装与上下文预算控制 | **上下文管理** |
| [pico/memory.py](pico/memory.py) | 分层工作记忆 + 持久记忆 | **记忆系统** |
| [pico/tools.py](pico/tools.py) | 工具定义、校验、执行 | **工具系统** |
| [pico/runtime.py](pico/runtime.py) | 控制循环、工具护栏、checkpoint、脱敏 | 运行时（贯穿多个子系统） |
| [pico/task_state.py](pico/task_state.py) | 单次 `ask()` 的状态机快照 | **断点与恢复** |
| [pico/run_store.py](pico/run_store.py) | 运行工件落盘（task_state / trace / report） | **断点与恢复** |
| [pico/evaluator.py](pico/evaluator.py) | 固定 benchmark 评估器 | **评测系统** |
| [pico/metrics.py](pico/metrics.py) | 消融实验、指标聚合、报告生成 | **评测系统** |
| [pico/config.py](pico/config.py) | 项目级 `.env` 配置加载 | 配置层 |

### 0.2 一次 `ask()` 的完整生命周期

`Pico.ask()`（[runtime.py:756](pico/runtime.py#L756)）是整个系统唯一的总调度器。CLI 收到用户输入后，本质上只做一件事——调用 `agent.ask()`。`ask()` 内部按下面顺序推进：

1. **记忆初始化**：把用户请求写进 working memory 的 `task_summary`。
2. **状态机创建**：`TaskState.create(...)`，并 `run_store.start_run(...)` 建立本次运行的目录。
3. **主循环**（`while tool_steps < max_steps and attempts < max_attempts`）：
   - **感知**：`_build_prompt_and_metadata()` → 刷新 prefix、评估恢复状态、`ContextManager.build()` 组装 prompt。
   - **决策**：`model_client.complete(prompt, ...)` → `parse(raw)` 解析成 `tool` / `final` / `retry` 三类。
   - **行动**：若是 `tool`，走 `run_tool()` 的完整护栏流水线。
   - **记录**：写 history、task_state、trace、checkpoint、memory。
4. **停机**：模型给出 `<final>`，或命中步数上限 / 重试上限，或模型错误。

```
                    ┌───────────────────────────────────────────────────┐
                    │                    Pico.ask()                      │
                    │  (控制循环 · 总调度器 · 状态机推进)                    │
                    └───────┬───────────────────────────┬────────────────┘
                            │                           │
              组 prompt（感知）                         执行工具（行动）
                            │                           │
              ┌─────────────▼───────────┐   ┌───────────▼─────────────┐
              │  ContextManager.build() │   │      run_tool()          │
              │  (prefix+memory+notes+  │   │  (校验→审批→执行→回写)      │
              │   history+request)      │   │                          │
              └─────────────┬───────────┘   └───────────┬─────────────┘
                            │                           │
                    ┌───────▼────────┐          ┌───────▼────────┐
                    │ model.complete │          │  tools.py 白名单 │
                    │  → parse(raw)  │          │  + memory 沉淀   │
                    └───────┬────────┘          └────────────────┘
                            │
                 tool / final / retry
                            │
              ┌─────────────▼──────────────────────────────┐
              │  RunStore: task_state.json / trace.jsonl   │
              │  SessionStore: session.json (checkpoints)  │
              └────────────────────────────────────────────┘
```

---

## 1. 上下文管理系统

**核心文件**：[pico/context_manager.py](pico/context_manager.py)（配合 [pico/workspace.py](pico/workspace.py)）

### 1.1 要解决的问题

仅靠用户这一轮输入，模型并不知道：当前仓库是什么状态、会话里已经读过什么、哪些旧信息还值得继续参考。上下文管理系统的职责是：**把「稳定基线 + 工作记忆 + 相关笔记 + 历史 + 当前请求」拼成一份大小可控、可解释的 prompt**。

它位于 `Pico.ask()` 每轮模型调用之前，是「真正发请求给模型」的最后一道组装工序。

### 1.2 Prompt 的五个 section

最终 prompt 由五个部分按固定顺序拼接（`_assemble_prompt`，[context_manager.py:444](pico/context_manager.py#L444)）：

| 顺序 | section | 来源 | 说明 |
| --- | --- | --- | --- |
| 1 | `prefix` | `WorkspaceContext.text()` + 工具手册 | 稳定规则：agent 是谁、工具怎么调、仓库状态 |
| 2 | `memory` | `LayeredMemory.render_memory_text()` | 工作记忆仪表盘 |
| 3 | `relevant_memory` | 按查询召回的笔记 | 按需展开的相关记忆 |
| 4 | `history` | `session["history"]` | 历史对话/工具结果的压缩视图 |
| 5 | `current_request` | 用户本轮输入 | 永远不裁剪 |

顺序是刻意设计的：**稳定规则放前面（利于 prompt cache），最新请求放最后（保证模型最先看到的是当前最重要的输入）**。`SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")`。

### 1.3 预算模型

`ContextManager` 维护三类预算参数（[context_manager.py:13](pico/context_manager.py#L13)）：

```python
DEFAULT_TOTAL_BUDGET = 32000          # 整份 prompt 的字符上限
DEFAULT_SECTION_BUDGETS = {
    "prefix": 9600, "memory": 4300,
    "relevant_memory": 3200, "history": 13900,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 3200, "memory": 1100,
    "relevant_memory": 800, "history": 4000,
}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
```

- **`total_budget`**：整份 prompt 的硬上限。若超过，触发预算收缩。
- **`section_budgets`**：每个 section 的默认预算。
- **`section_floors`**：每个 section 的「保底」下限，压缩时不能低于它。默认是 `max(20, budget // 4)`，可被 `section_floors` 参数覆盖（[context_manager.py:218](pico/context_manager.py#L218)）。
- **`reduction_order`**：超预算时按此顺序依次压缩。顺序体现了平台偏好：**先牺牲 relevant_memory，再牺牲 history，最后才动 memory 和 prefix**。当前请求永远不在压缩列表里。

### 1.4 预算收缩算法

`build()`（[context_manager.py:78](pico/context_manager.py#L78)）的核心是一个 `while len(prompt) > total_budget` 循环：

```python
while len(prompt) > self.total_budget:
    overflow = len(prompt) - self.total_budget
    for section in self.reduction_order:      # 按固定顺序
        if current_budget <= floor: continue   # 已到保底，跳过
        new_budget = max(floor, current_budget - overflow)
        budgets[section] = new_budget          # 压缩该 section
        rendered = self._render_sections(...)
        prompt = self._assemble_prompt(rendered)
        break                                  # 一次只压一节，再重新评估
    # 若无任何 section 可再压缩（都到 floor），跳出
```

设计要点：

- **一次只压一节**，然后重新渲染、重新计算是否仍超预算——避免一次压过头。
- **每次按 `overflow` 全量扣除**，但被 `floor` 兜底。
- 每步压缩都记录进 `reduction_log`（`section` / `before_chars` / `after_chars` / `overflow_chars`），后续进入 trace / report，用于解释「这轮 prompt 是怎么被拼出来的」。

### 1.5 每个 section 的渲染细节

#### 1.5.1 `prefix` 与 `memory`

直接按预算做**尾部裁剪**（`_tail_clip`，保留前 N 字符 + `"..."`）。`prefix` 前会额外拼接 `render_checkpoint_text()`（断点恢复文本，见 §4），`memory` 若被 feature flag 关闭则显示 `Memory:\n- disabled`。

#### 1.5.2 `relevant_memory`（按需召回）

先调用 `agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)`（`RELEVANT_MEMORY_LIMIT = 6`）选出最多 6 条相关笔记，再按预算渲染：

- **每条 note 平分预算**（`_per_note_budget`）：`usable = budget - header - 3*note_count`，`per_note = usable // note_count`。避免一条超长笔记挤掉其它笔记。
- 用 `_tail_clip` 逐条裁剪，若仍超预算则逐步降低 `per_note_budget`。

#### 1.5.3 `history`（历史压缩，最复杂的部分）

`_render_history_section`（[context_manager.py:297](pico/context_manager.py#L297)）+ `_compressed_history_entries`（[context_manager.py:361](pico/context_manager.py#L361)）：

- **优先保留最近的历史**：`recent_window = 10`，最近 10 条为「recent」，完整保留（行上限 `900` 字符）；更早的为「older」。
- **对 older 条目做四类压缩**：
  1. **合并重复 read_file**：同一个 path 在 older 区已读过，后续重复的 `read_file` 直接跳过（`collapsed_duplicate_reads`）。
  2. **复用文件摘要**：对首次出现的 older `read_file`，若 memory 里有该文件的 `file_summaries`，用一行 `{path} -> {summary}` 替代完整内容（`reused_file_summary_count`）。
  3. **工具调用摘要化**：其它 older 工具调用压成一行（`run_shell` 取 `command` + 结果前三行；`summarized_tool_count`）。
  4. **普通消息裁剪**：非工具消息按 `60` 字符截断。
- 渲染时**从最新往旧倒序贪心填充**，直到达到预算；recent 条目可临时裁到更短，older 条目裁到 `20` 字符兜底。

### 1.6 可解释性：metadata

每次 `build()` 都会产出一份详尽的 `metadata`（[context_manager.py:456](pico/context_manager.py#L456)），记录每个 section 的 `raw_chars` / `budget_chars` / `rendered_chars`、`reduction_log`、`relevant_memory` 的选中笔记明细、`history` 的压缩统计。这些数据随后由 `_build_prompt_and_metadata` 追加缓存、恢复状态、脱敏统计等信息，最终进入 trace 与 report，回答「为什么这一轮 prompt 长这样」。

### 1.7 工作区快照（prefix 的底座）

[workspace.py](pico/workspace.py) 提供一份**小而稳定**的「仓库第一印象」，避免预加载整个仓库：

- **Git 事实**：`cwd`、`repo_root`、`branch`、`default_branch`、`status --short`（裁到 1500 字符）、最近 5 条 `git log --oneline`。
- **白名单项目文档**：`DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")`，每个裁到 1200 字符。
- **指纹** `fingerprint()`：对上述字段做 SHA256，用来判断仓库状态是否变化，从而决定是否重建 prompt prefix（配合 prompt cache）。

---

## 2. 记忆系统

**核心文件**：[pico/memory.py](pico/memory.py)（配合 [pico/runtime.py](pico/runtime.py) 中的沉淀与提取逻辑）

### 2.1 分层设计

记忆系统刻意分成三层，各司其职，避免「整段历史塞满下一轮 prompt」：

| 层 | 存储位置 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| **session history** | `session.json` 的 `history` | 完整事件流（user / assistant / tool） | 整个会话 |
| **working memory** | `session.json` 的 `memory` | 轻量工作集：任务摘要、最近文件、文件摘要、跨轮笔记 | 整个会话，但容量受限 |
| **durable memory** | `.pico/memory/` 目录 | 长期稳定事实，跨会话沉淀 | 跨会话 |

设计理念：session history 负责「完整可回放」，working memory 负责「下一轮接得上」，durable memory 负责「跨会话记得住」。

### 2.2 Working memory 的结构

`default_memory_state()`（[memory.py:43](pico/memory.py#L43)）给出一个小而结构化的状态，而不是一大段自由文本摘要：

```python
{
    "working": {"task_summary": "", "recent_files": []},
    "episodic_notes": [],
    "file_summaries": {},
    "task": "", "files": [], "notes": [], "next_note_index": 0,
}
```

容量上限是硬约束（[memory.py:15](pico/memory.py#L15)）：

```python
WORKING_FILE_LIMIT = 8      # recent_files 最多 8 个
EPISODIC_NOTE_LIMIT = 12    # episodic_notes 最多 12 条
FILE_SUMMARY_LIMIT = 6      # 渲染到 memory 仪表盘的文件摘要最多 6 条
```

各字段语义：

- **`task_summary`**：当前任务的一句话摘要（裁到 300 字符），每一轮 `ask()` 开始都会被用户请求覆盖。
- **`recent_files`**：最近接触的文件（按相对路径 canonicalize，去重、保留最近 8 个）。
- **`episodic_notes`**：跨轮笔记，带 `text` / `tags` / `source` / `created_at` / `note_index` / `kind`。`kind` 有 `episodic`、`durable`、`process` 三种。
- **`file_summaries`**：`{path: {summary, created_at, freshness}}`，记录文件读出来的短摘要，配合 freshness 做失效检测。

> 注意 `task` / `files` / `notes` 是 `working` 的**镜像字段**，用于兼容磁盘上可能长成旧格式的状态；规范化层会把它们对齐到 `working`。

### 2.3 工具结果如何沉淀进记忆

`update_memory_after_tool()`（[runtime.py:642](pico/runtime.py#L642)）在**工具执行完之后、下一轮 prompt 组装之前**被调用，只把「下一轮大概率还会用到」的事实提纯：

- `read_file` / `write_file` / `patch_file` → `remember_file(path)`，把该文件移入 `recent_files`。
- `read_file` → 生成短摘要（`summarize_read_result`：去掉首行 `# ` 标题，取正文前三行拼成 `" | "` 连接，裁到 180 字符），写进 `file_summaries` 并 append 一条 `episodic` 笔记。
- `write_file` / `patch_file` → **让旧摘要失效**（`invalidate_file_summary`），因为文件内容已经变了。

### 2.4 文件摘要的 freshness 失效机制

这是记忆正确性的关键防线：

- `set_file_summary` 写入时会计算文件当前的 SHA256（`file_freshness`）并存为 `freshness` 字段。
- `invalidate_stale_file_summaries`（[memory.py:493](pico/memory.py#L493)）逐文件重算 SHA256，凡是不一致的都从 `file_summaries` 删除并返回失效路径列表。
- 在 `evaluate_resume_state()` 时调用，保证**恢复会话时不会拿着过期的文件摘要继续推理**。这也是「partial-stale」恢复状态的来源之一（见 §4）。

### 2.5 召回算法（无 embedding）

`retrieval_candidates()`（[memory.py:519](pico/memory.py#L519)）**故意保持简单透明**，不引入 embedding：

1. 对查询和每条笔记做 token 化（`re.findall(r"[A-Za-z0-9_]+")` + lower）。
2. 打分三元组 `(exact_tag_match, keyword_overlap, recency[, note_index])`：
   - `exact_tag_match`：查询 token 与笔记 tag 精确命中（0 或 1）。
   - `keyword_overlap`：查询 token 与笔记 token（含 text/source/tags）的重叠数。
   - `recency`：`created_at` 时间戳，越新越靠前。
3. 排序取前 `limit` 条。同时会去 `.pico/memory/` 拉 durable notes 一并参与召回（durable 用 `note_index = -1` 表示更早）。

### 2.6 规范化层

`normalize_memory_state()`（[memory.py:334](pico/memory.py#L334)）负责把「磁盘里可能长得不太一样的旧状态」统一整理成当前 runtime 可直接使用的紧凑结构：类型校验、字段补全、路径 canonicalize、容量裁剪、镜像字段同步。它是所有记忆操作的前置闸口。

### 2.7 Durable memory（持久记忆）

`DurableMemoryStore`（[memory.py:59](pico/memory.py#L59)）把跨会话稳定事实写进 `.pico/memory/`：

- **`MEMORY.md`**：索引文件，每条 topic 一行 `- [slug](topics/slug.md): title`，下跟 `summary` 与 `tags`。
- **`topics/<slug>.md`**：每个主题的正文笔记（`## Notes` 下的 `- ` 列表）。

内置四个主题（`DURABLE_TOPIC_DEFAULTS`，[memory.py:19](pico/memory.py#L19)）：

| slug | 标题 | tags |
| --- | --- | --- |
| `project-conventions` | Project Conventions | convention |
| `key-decisions` | Key Decisions | decision |
| `dependency-facts` | Dependency Facts | dependency |
| `user-preferences` | User Preferences | preference |

`promote()`（[memory.py:188](pico/memory.py#L188)）负责沉淀：对每条候选 note，通过 `_subject_key`（正则提取「主语 is/are/uses/should / 主语 是/使用」的主语部分）判断是否已有同一主语，若有则**替换旧条目并记录 superseded**，否则追加。

### 2.8 持久记忆的提取与拒绝

提取入口 `extract_durable_promotions()`（[runtime.py:723](pico/runtime.py#L723)）：

1. **意图识别**：用户请求必须命中 `DURABLE_MEMORY_INTENT_PATTERN`（`capture/remember/save/store/persist/note`）或中文 `DURABLE_MEMORY_INTENT_ZH_PATTERN`（`记住/保存/记录/沉淀/长期记忆/持久记忆`），否则不做任何沉淀。
2. **行模式匹配**：对最终答案逐行匹配 `DURABLE_MEMORY_LINE_PATTERNS`（[runtime.py:42](pico/runtime.py#L42)），支持英文 `Project convention:` / `Decision:` / `Dependency:` / `Preference:` 与中文 `项目约定：` / `决策：` / `依赖：` / `偏好：` 前缀。
3. **拒绝规则** `reject_durable_reason()`（[runtime.py:695](pico/runtime.py#L695)）过滤四类噪音：
   - `empty`：空文本
   - `secret_shaped`：命中密钥形状正则（`api_key` / `token` / `secret` / `password` / `sk-...`）
   - `transient_task_state`：以 checkpoint 字段名开头（`current goal` / `当前目标` 等）
   - `noisy_output`：含 `stdout/stderr/traceback/exit_code` 或长度 > 220

---

## 3. 工具系统

**核心文件**：[pico/tools.py](pico/tools.py)（定义与执行）+ [pico/runtime.py](pico/runtime.py) 的 `run_tool()`（护栏流水线）

### 3.1 设计定位

工具系统是 agent 的**能力白名单**：模型能申请哪些动作、这些动作如何做参数校验、最终如何执行，都在这里显式定义。工具**不是动态发现，而是显式注册**——模型看到的是一个有边界、可审计的动作集合。

### 3.2 工具清单

`BASE_TOOL_SPECS`（[tools.py:14](pico/tools.py#L14)）定义 6 个基础工具，`DELEGATE_TOOL_SPEC` 定义 1 个子 agent 工具（仅当 `depth < max_depth` 时暴露）：

| 工具 | schema | risky | 说明 |
| --- | --- | --- | --- |
| `list_files` | `path="."` | 否 | 列出目录（最多 200 项，忽略 `IGNORED_PATH_NAMES`） |
| `read_file` | `path`, `start=1`, `end=400` | 否 | 按行号范围读 UTF-8 文件，带行号回显 |
| `search` | `pattern`, `path="."` | 否 | 优先 `rg -n --smart-case --max-count 200`，否则 Python 回退 |
| `run_shell` | `command`, `timeout=20` | **是** | 在仓库根跑 shell，超时范围 [1,120]，输出裁剪 |
| `write_file` | `path`, `content` | **是** | 写文本文件（自动建父目录） |
| `patch_file` | `path`, `old_text`, `new_text` | **是** | 精确替换一处文本块 |
| `delegate` | `task`, `max_steps=6` | 否 | 派一个**只读**子 agent 调查 |

### 3.3 工具执行流水线

`run_tool()`（[runtime.py:1000](pico/runtime.py#L1000)）是工具层的总闸口，**所有工具调用必须先经过它，不能让模型直接碰到底层函数**。流水线顺序：

```
工具是否存在 → 参数是否合法 → 是否重复调用 → 是否通过审批 → 真正执行 → 更新记忆
```

每一步的细节：

1. **存在性**：`self.tools.get(name)` 为 `None` 则返回 `error: unknown tool`。
2. **参数校验** `validate_tool`（[tools.py:82](pico/tools.py#L82)）：每个工具都有严格的参数校验，失败返回错误 + 示例调用。
3. **重复检测** `repeated_tool_call`（[runtime.py:1129](pico/runtime.py#L1129)）：检查最近 `window = clamp(2..6, len(tool_events))` 次工具调用中，相同 `(name, args)` 是否已出现 ≥2 次。这样既能抓 `A→B→A→B` 交替循环，又不会误伤隔了很多步的回读。
4. **审批** `approve`（[runtime.py:1201](pico/runtime.py#L1201)）：仅对 `risky` 工具触发，受 `approval_policy`（`ask`/`auto`/`never`）与 `read_only` 约束。
5. **执行**：对 risky 工具先拍 `capture_workspace_snapshot()`，执行后 `diff_workspace_snapshots()` 算出受影响路径与 diff 摘要；`run_shell` 还会解析 `exit_code` 判定 `partial_success` / `error`。
6. **记忆更新**：`update_memory_after_tool()`（§2.3）+ `record_process_note_for_tool()`（对 `partial_success` / `error` / `rejected` 写 `process` 类型笔记，供下一轮决策参考）。

### 3.4 安全设计

安全是工具系统最核心的关切，集中在三处：

#### 3.4.1 路径逃逸防护

`Pico.path()`（[runtime.py:1341](pico/runtime.py#L1341)）把所有文件类工具锚定在 workspace root 之下：

```python
resolved = path.resolve()
if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
    raise ValueError(f"path escapes workspace: {raw_path}")
```

`resolve()` 展开符号链接，因此**既能防住 `../` 逃逸，也能防住符号链接解析后跳出仓库**。

#### 3.4.2 只读与审批

- `read_only=True` 时，`approve()` 直接返回 `False`，任何 risky 工具都被拒绝（子 agent 即用此模式）。
- `approval_policy="never"` 时拒绝所有 risky 工具；`"ask"` 时交互式询问 `[y/N]`；`"auto"` 时放行。
- `patch_file` 要求 `old_text` 精确命中且**只能出现一次**，保证修改行为确定、失败原因易解释。

#### 3.4.3 敏感信息脱敏

- `SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")`，配合 `secret_env_names` 白名单识别敏感环境变量。
- `shell_env()`（[runtime.py:512](pico/runtime.py#L512)）给 `run_shell` 传入的是**过滤后的环境变量**（`DEFAULT_SHELL_ENV_ALLOWLIST`），而不是继承整个父 shell，减少敏感信息被意外带入命令执行环境。
- `redact_artifact()`（[runtime.py:495](pico/runtime.py#L495)）递归地把 trace / report 里的敏感值替换为 `<redacted>`；`SECRET_SHAPED_TEXT_PATTERN` 还能识别 `sk-` 前缀的密钥形状文本。

#### 3.4.4 工具结果元数据

每次工具调用（无论成败）都产出 `_last_tool_result_metadata`，包含 `tool_status`（`ok`/`partial_success`/`error`/`rejected`）、`tool_error_code`、`security_event_type`（`path_escape`/`read_only_block`/`approval_denied` 等）、`risk_level`、`read_only`、`affected_paths`、`workspace_changed`、`diff_summary`。这些既进入 trace 供审计，也被评测系统用来做安全场景断言（§5）。

### 3.5 delegate 子 agent

`tool_delegate()`（[tools.py:265](pico/tools.py#L265)）派一个**受限**的 `Pico` 子实例：

- `approval_policy="never"`、`read_only=True`、`max_steps` 默认 6、`depth = parent.depth + 1`。
- **一旦 `depth >= max_depth`，就连 `delegate` 这个工具都不再暴露**（`build_tool_registry` 里的条件注册），防止无限递归。
- 委派目标是「调查」而非「放权执行」：子 agent 只把结论文本返回给父 agent。

---

## 4. 断点与恢复系统

**核心文件**：[pico/runtime.py](pico/runtime.py)（checkpoint 与恢复评估）、[pico/task_state.py](pico/task_state.py)（状态机）、[pico/run_store.py](pico/run_store.py)（工件落盘）、[pico/memory.py](pico/memory.py)（freshness）

### 4.1 持久化分层

| 层 | 类 | 路径 | 内容 |
| --- | --- | --- | --- |
| 会话 | `SessionStore` | `.pico/sessions/<session_id>.json` | 可恢复的会话状态：history、memory、checkpoints、runtime_identity、resume_state |
| 运行工件 | `RunStore` | `.pico/runs/<run_id>/` | 单次 `ask()` 的审计工件 |

`SessionStore` 负责「恢复现场」，`RunStore` 负责「复盘证据」，两者分开，恢复现场与复盘证据不会混在一起（[run_store.py](pico/run_store.py) 模块注释）。

### 4.2 Run 工件

每次 `ask()` 生成一个 `run_<timestamp>-<rand>` 目录，写三类文件（[run_store.py](pico/run_store.py)）：

| 文件 | 写入方式 | 内容 |
| --- | --- | --- |
| `task_state.json` | 原子写（临时文件 + `replace`） | 状态机快照，运行中持续覆盖更新 |
| `trace.jsonl` | 追加写 | 逐事件时间线（`run_started` / `prompt_built` / `model_requested` / `model_parsed` / `tool_executed` / `checkpoint_created` / `run_finished`） |
| `report.json` | 原子写 | 运行结束的最终摘要 + 关键指标 |

**原子写**（`_write_json_atomic`，[run_store.py:71](pico/run_store.py#L71)）避免中途异常留下半截 JSON；**jsonl 追加写**比「最后一次性写整份 trace」更稳，更适合流式事件序列的调试。

### 4.3 TaskState 状态机

[task_state.py](pico/task_state.py) 用一个小状态机回答「这次请求进行到哪了、调了多少次工具、最后为什么停下」：

- **状态**：`running` → `completed` / `stopped` / `failed`。
- **关键区分**：`stop_reason`（怎么停的）与 `status`（停下时是什么状态）分开存。
- **两个计数器**：`attempts`（模型被调用了几轮）≠ `tool_steps`（真正进入执行的工具调用次数）。

`stop_reason` 枚举：`final_answer_returned` / `step_limit_reached` / `retry_limit_reached` / `model_error` / `tool_timeout` / `approval_denied` / `delegate_failed` / `persistence_error` / `resume_load_error`。

### 4.4 Checkpoint 结构

`create_checkpoint()`（[runtime.py:601](pico/runtime.py#L601)）生成一份结构化的现场快照：

```python
{
    "checkpoint_id": "ckpt_<hex8>",
    "parent_checkpoint_id": "...",       # 形成 checkpoint 链
    "schema_version": "phase1-v1",
    "created_at": "...",
    "current_goal": "<本轮用户请求>",
    "completed": [...], "excluded": [...],
    "current_blocker": "<stop_reason>",
    "next_step": "<推断的下一步>",
    "key_files": [{"path", "freshness"}, ...],   # 从 recent_files 计算
    "freshness": {path: sha256},
    "summary": "<trigger>: <请求前120字符>",
    "runtime_identity": {...},           # 运行时指纹
}
```

Checkpoint 存于 `session["checkpoints"]`，结构为 `{current_id, items: {id: checkpoint}}`，`current_id` 指向最新一个。

### 4.5 Checkpoint 的创建触发点

`ask()` 主循环在多个时刻创建 checkpoint（每次创建后立即 `write_task_state` + 发 `checkpoint_created` trace 事件）：

| 触发 trigger | 时机 |
| --- | --- |
| `tool_executed` | 每个工具执行完之后 |
| `run_finished` | 正常产出最终答案时 |
| `context_reduction` | prompt 触发预算收缩时 |
| `freshness_mismatch` | 检测到 stale 文件摘要时 |
| `workspace_mismatch` | 检测到 runtime identity 不匹配时 |
| `step_limit_reached` / `retry_limit_reached` | 异常停机时 |

### 4.6 恢复状态评估

`evaluate_resume_state()`（[runtime.py:211](pico/runtime.py#L211)）在每次 `_build_prompt_and_metadata` 时运行，产出五种状态：

| status | 含义 | 判定条件 |
| --- | --- | --- |
| `no-checkpoint` | 无断点 | `current_id` 为空 |
| `schema-mismatch` | schema 版本不符 | `schema_version != "phase1-v1"` |
| `partial-stale` | 关键文件摘要过期 | 某个 `key_files` 的 freshness 与当前 SHA256 不一致 |
| `workspace-mismatch` | 运行时身份漂移 | `runtime_identity` 各字段（cwd/model/approval/feature_flags/tool_signature/workspace_fingerprint…）不一致 |
| `full-valid` | 完全有效 | 上述都通过 |

**runtime_identity**（[runtime.py:179](pico/runtime.py#L179)）是判断「这次恢复是否还是同一个环境」的指纹，涵盖 `cwd`、`model`、`model_client`、`approval_policy`、`read_only`、`max_steps`、`max_new_tokens`、`feature_flags`、`shell_env_allowlist`、`workspace_fingerprint`、`tool_signature` 十一个维度。任意一项漂移都会让 `workspace-mismatch` 状态浮现。

### 4.7 恢复文本如何进入 prompt

`render_checkpoint_text()`（[runtime.py:273](pico/runtime.py#L273)）把当前 checkpoint 渲染成一段「Task checkpoint」文本，内容包括 `Resume status`、`Current goal`、`Current blocker`、`Next step`、`Key files`、`Completed/Excluded`、`Stale paths`、`Summary`。这段文本被拼进 prompt 的 `prefix` section（§1.5.1），让模型**显式感知自己正在从哪个断点恢复**。

### 4.8 恢复流程

- `Pico.from_session()`（[runtime.py:153](pico/runtime.py#L153)）从 `session_store.load(session_id)` 恢复。
- CLI 的 `--resume <id|latest>`（[cli.py:231](pico/cli.py#L231)）触发恢复；`latest` 取 `store.latest()`（按 mtime 最新的 session）。
- 恢复时 `invalidate_stale_memory()` 会先把过期文件摘要清掉，再评估 checkpoint 状态，把 `resume_status` 写进新 TaskState 与 report。

---

## 5. 评测系统

**核心文件**：[pico/evaluator.py](pico/evaluator.py)（固定 benchmark 评估器）、[pico/metrics.py](pico/metrics.py)（消融实验、指标聚合、报告生成）、[benchmarks/coding_tasks.json](benchmarks/coding_tasks.json)（任务定义）

### 5.1 设计目标

评测系统要回答两个问题：**runtime 合同稳不稳定**（固定 harness regression），以及**各模块到底有没有收益**（context / memory / recovery 消融）。核心方法论是**确定性**——用脚本化模型输出替代真实模型，排除 LLM 的随机性，只测平台自身的控制逻辑。

### 5.2 Benchmark schema

`benchmarks/coding_tasks.json`（`BENCHMARK_SCHEMA_VERSION = 1`）定义 12 个任务，每个任务必需字段：

```python
REQUIRED_TASK_KEYS = (
    "id", "prompt", "fixture_repo", "allowed_tools",
    "step_budget", "expected_artifact", "verifier", "category",
)
```

任务按 `category` 分五类：`documentation`、`text-edit`、`tool-boundary`、`recovery`、`durable-contract`。其中 `recovery` 类任务带 `setup` 字段，用于在运行前构造特定的断点/脏状态场景。

### 5.3 BenchmarkEvaluator 的执行流程

`run_task()`（[evaluator.py:441](pico/evaluator.py#L441)）：

1. **fixture 隔离**：把 `fixture_repo` 拷到临时目录 `workspace_root/<task_id>/<repo>`，每次运行都用全新副本，杜绝跨任务污染。
2. **脚本化模型**：`FakeModelClient(_scripted_outputs_for_task(task))` 按预写序列吐工具调用（`SCRIPTED_MODEL_OUTPUTS`，[evaluator.py:46](pico/evaluator.py#L46)）。
3. **任务 setup**：`_apply_task_setup()`（[evaluator.py:299](pico/evaluator.py#L299)）构造 `context_reduction` / `freshness_mismatch` / `workspace_mismatch` 等前置状态。
4. **执行**：`agent.ask(task["prompt"])`，跑完整控制循环。
5. **verifier 校验**：在 fixture 副本里跑 `task["verifier"]`（一段 shell/python 断言）。
6. **通过判定**（四条件同时满足）：

```python
passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
```

- `within_budget`：`tool_steps <= step_budget`
- `verifier_passed`：verifier 子进程 exit code == 0
- `expected_artifact_exists`：产物文件存在
- `non_failure_stop_reason`：`stop_reason == "final_answer_returned"`

失败分类 `_failure_category()`：`missing_artifact` → `budget_exceeded` → `verifier_failed` → `failure_stop_reason` → `unknown`。

### 5.4 可复现性设计

`run()`（[evaluator.py:405](pico/evaluator.py#L405)）产出的 artifact 显式记录可复现要素：

- `runtime.commit_sha` / `runtime.branch`
- `reproducibility.fixture_snapshot_id`：对全部 fixture 文件内容做 SHA256 排序拼接，锁住 fixture 版本
- `reproducibility.model_name/version` + `decoding`（temperature / top_p / max_new_tokens）
- `timezone`（默认 `Asia/Shanghai`）与 `locale`——保证时间戳与文本比较跨机器稳定

### 5.5 消融实验（metrics.py）

消融实验是评测系统的第二层，回答「关掉某特性会怎样」：

#### 5.5.1 Context ablation（上下文消融）

`run_context_stress_matrix()`（[metrics.py:438](pico/metrics.py#L438)）遍历 `history_levels × note_levels × request_levels`（3×2×2 = 12 配置），对每个配置比较 `full`（开 context_reduction）与 `no_context_reduction`（关）下的 prompt 字符数，计算压缩率 `(raw - full) / raw`，并校验 `current_request` 是否被保留。

#### 5.5.2 Memory ablation（记忆消融）

`run_large_scale_memory_experiment()`（[metrics.py:403](pico/metrics.py#L403)）用 12 个记忆任务（fact_lookup / edit_dependency / history_reference 三类）对比 `memory_on` / `memory_off` / `memory_irrelevant` 三个变体，核心指标是**重复读文件次数 `repeated_reads`**——记忆生效时应显著下降（后续轮不用重读文件就能回答）。`_MemoryExperimentModelClient`（[metrics.py:219](pico/metrics.py#L219)）模拟「先读文件记事实，再问后续问题」的交互，并在 prompt 里探测记忆是否命中。

#### 5.5.3 Recovery ablation（恢复消融）

`run_recovery_ablation_v2()`（[metrics.py:1592](pico/metrics.py#L1592)）用 `RECOVERY_ABLATION_TASKS`（10 个场景）对比 `resume_enabled` / `resume_disabled`，用 `_RecoveryScenarioModelClient`（[metrics.py:1271](pico/metrics.py#L1271)）校验恢复文本是否包含必需片段。产出四个指标：

- `resume_success_rate`：恢复成功的比例
- `stale_reanchor_rate`：partial-stale 场景能重锚定的比例
- `workspace_drift_detection_rate`：workspace-mismatch 能被检测出的比例
- `resume_false_accept_rate`：本应拒绝恢复却误判为 `full-valid` 的比例（越低越好）

#### 5.5.4 Security scenarios（安全场景）

`run_security_experiment_suite()`（[metrics.py:626](pico/metrics.py#L626)）用 10 个场景验证工具系统的安全护栏：`path_escape_read`、`symlink_escape`、`search_escape`、`approval_denied_shell`、`read_only_write`、`repeated_identical_call`、`patch_nonunique`、`patch_missing_new_text`、`timeout_out_of_range`、`empty_delegate_task`。每个场景断言 `security_event_type` / `tool_error_code` 是否符合预期。

### 5.6 指标聚合与报告

- `aggregate_benchmark_artifact()`（[metrics.py:43](pico/metrics.py#L43)）：聚合 benchmark 的 pass_rate、within_budget、verifier_passes、failure 分类、平均 tool_steps / attempts。
- `aggregate_run_artifacts()`（[metrics.py:85](pico/metrics.py#L85)）：聚合 `.pico/runs/` 下的 report/trace，产出 cache 命中率、cached token 比例、prefix 复用率、工具状态分布、安全事件分布、时长统计等。
- `render_resume_metrics_markdown()` / `write_benchmark_core_report()`：把聚合结果渲染成 Markdown 报告（后者输出到 `docs/metrics/pico-benchmark-core-report.md`）。

### 5.7 合成实验 vs 真实 provider 实验

`collect_resume_metrics()`（[metrics.py:1079](pico/metrics.py#L1079)）统一编排两种模式：

- **`synthetic`**：全部用 `FakeModelClient`，确定、快速、可复现，用于模块级回归。
- **`real`**：用真实 provider（`run_real_memory_experiment` / `run_real_context_experiment` / `run_real_security_experiment_suite`），测真实模型的记忆依赖、长上下文压缩与安全行为。

两者在 `write_benchmark_core_report()` 里被刻意**分开陈述**（「Harness regression 只证明 runtime 合同稳定，不证明 provider 上限」），避免把「模块收益」与「provider benchmark」混为一谈。

---

## 附：关键常量速查

| 常量 | 值 | 出处 |
| --- | --- | --- |
| `DEFAULT_TOTAL_BUDGET` | 32000 | context_manager.py |
| `DEFAULT_SECTION_BUDGETS` | prefix 9600 / memory 4300 / relevant 3200 / history 13900 | context_manager.py |
| `DEFAULT_SECTION_FLOORS` | prefix 3200 / memory 1100 / relevant 800 / history 4000 | context_manager.py |
| `RELEVANT_MEMORY_LIMIT` | 6 | context_manager.py |
| `history recent_window` | 10 | context_manager.py |
| `WORKING_FILE_LIMIT` | 8 | memory.py |
| `EPISODIC_NOTE_LIMIT` | 12 | memory.py |
| `FILE_SUMMARY_LIMIT` | 6 | memory.py |
| `MAX_TOOL_OUTPUT` | 12000 | workspace.py |
| `MAX_HISTORY` | 32000 | workspace.py |
| `DOC_NAMES` | AGENTS.md / README.md / pyproject.toml / package.json | workspace.py |
| `IGNORED_PATH_NAMES` | .git / .pico / __pycache__ / .pytest_cache / .ruff_cache / .venv / venv | workspace.py |
| `CHECKPOINT_SCHEMA_VERSION` | phase1-v1 | runtime.py |
| `DEFAULT_FEATURE_FLAGS` | memory / relevant_memory / context_reduction / prompt_cache 全开 | runtime.py |
| `SENSITIVE_ENV_NAME_MARKERS` | API_KEY / TOKEN / SECRET / PASSWORD | runtime.py |
| `run_shell timeout` | [1, 120]，默认 20 | tools.py |
| `read_file 默认范围` | start=1, end=400 | tools.py |
| `delegate max_steps` | 6 | tools.py |
| 工具总数 | 6 基础 + 1 delegate = 7 | tools.py |
| `BENCHMARK_SCHEMA_VERSION` | 1 | evaluator.py |
| benchmark 任务数 | 12 | benchmarks/coding_tasks.json |
| durable 主题数 | 4 | memory.py |
