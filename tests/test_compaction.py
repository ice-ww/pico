"""P2：历史的两级压缩（two-level compaction）。

Level 1 = 逐条压缩（合并重复读、tool 输出总结成一行、非 tool 截 60 字符）；
Level 2 = 当 Level 1 在预算内塞不下旧历史、开始丢弃旧条目时，把旧段
确定性聚合成一段摘要，让旧历史体积从 O(n) 降到 O(1)，而不是直接丢信息。
"""

from pico import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from pico.context_manager import ContextManager


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def _record_older_history(agent, count):
    for i in range(count):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"old_file_{i}.txt"},
                "content": f"content of old_file_{i}\n",
                "created_at": f"2026-04-07T08:{i % 60:02d}:00+00:00",
            }
        )


def _record_recent_messages(agent, count):
    for i in range(count):
        agent.record(
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"recent-message-{i}",
                "created_at": f"2026-04-07T09:{i:02d}:00+00:00",
            }
        )


def _small_history_manager(agent):
    # history 预算 500，足够小，让 30 条旧历史塞不下，从而触发 Level 2。
    return ContextManager(
        agent,
        total_budget=2000,
        section_budgets={"prefix": 200, "memory": 200, "relevant_memory": 200, "history": 500},
    )


def test_level2_aggregates_older_history_when_budget_drops_them(tmp_path):
    agent = build_agent(tmp_path)
    _record_older_history(agent, 30)
    _record_recent_messages(agent, 10)

    prompt, metadata = _small_history_manager(agent).build("current request")

    assert metadata["history"]["aggregated_older_summary"] is True
    assert "[Earlier transcript summary]" in prompt
    # 30 个不同文件的 read 都被聚合进摘要统计
    assert metadata["history"]["files_read_count"] == 30
    # 最近窗口的最新内容仍被保留
    assert "recent-message-9" in prompt


def test_level2_preserves_recent_window_and_replaces_older_entries(tmp_path):
    agent = build_agent(tmp_path)
    _record_older_history(agent, 30)
    _record_recent_messages(agent, 10)

    prompt, metadata = _small_history_manager(agent).build("current request")

    assert metadata["history"]["aggregated_older_summary"] is True
    # 整个最近窗口（最早的一条 recent）都保留了，没有被丢弃
    assert "recent-message-0" in prompt
    # 旧历史的逐条形式不再出现：摘要只列文件名，不含逐条 "[tool:...] {args}" 结构
    assert '[tool:read_file] {"path": "old_file_0.txt"}' not in prompt


def test_small_history_does_not_trigger_level2(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.set_file_summary("sample.txt", "alpha | beta")
    agent.memory.remember_file("sample.txt")
    for created_at in ("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00"):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "sample.txt", "start": 1, "end": 2},
                "content": "# sample.txt\nalpha\nbeta\n",
                "created_at": created_at,
            }
        )
    for minute in range(2, 12):
        agent.record(
            {
                "role": "user" if minute % 2 == 0 else "assistant",
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:{minute:02d}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check the file")

    assert metadata["history"]["aggregated_older_summary"] is False
    # Level 1 的逐条压缩字段在默认预算下仍正常工作
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 1
