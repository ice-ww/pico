"""P1：低进度检测 + 本地重新规划（low-progress detection + local re-planning）。

这套测试验证的是主循环里的「软干预」层：当 agent 连续若干步没有推进
（既没改 workspace，工具也没成功）时，不是一路烧到 step_limit 才停，
而是注入一段反思提示，逼它停下来重新审视目标/卡点/下一步。
"""

from pico import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext

# 反思提示里的稳定锚点，用来断言提示是否真正进入了会话历史。
REPLAN_MARKER = "making little progress"


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def _replan_messages(agent):
    return [
        item["content"]
        for item in agent.session["history"]
        if item["role"] == "user" and REPLAN_MARKER in item["content"]
    ]


def test_low_progress_injects_replan_prompt(tmp_path):
    # read_file 不存在的文件 -> tool_status=error 且不改 workspace，算「无推进」。
    # 连续 3 步无推进 -> 触发一次重新规划。
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"missing1.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing2.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing3.txt"}}</tool>',
            "<final>done</final>",
        ],
    )

    result = agent.ask("find the answer")

    assert result == "done"
    assert agent.current_task_state.replan_count == 1
    assert len(_replan_messages(agent)) == 1


def test_replan_stops_after_max_replans(tmp_path):
    # 阈值 N=3，上限 MAX_REPLANS=2。连续 9 步无推进会命中 3 次检测，
    # 但只有前两次真正注入，第三次被上限挡下，交由 step_limit 兜底。
    outputs = [
        f'<tool>{{"name":"read_file","args":{{"path":"missing{i}.txt"}}}}</tool>'
        for i in range(1, 10)
    ]
    outputs.append("<final>done</final>")
    agent = build_agent(tmp_path, outputs)

    result = agent.ask("find the answer")

    assert result == "done"
    assert agent.current_task_state.replan_count == 2
    assert len(_replan_messages(agent)) == 2


def test_progress_resets_and_no_false_replan(tmp_path):
    # 中间夹一步成功写文件，会重置连续无进度计数，因此不会误触发。
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"missing1.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing2.txt"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"out.txt","content":"hello"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing4.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing5.txt"}}</tool>',
            "<final>done</final>",
        ],
    )

    result = agent.ask("do the work")

    assert result == "done"
    assert agent.current_task_state.replan_count == 0
    assert _replan_messages(agent) == []


def test_replan_disabled_by_feature_flag(tmp_path):
    # feature flag 关闭时，检测被整体跳过，行为与未实现该特性时一致。
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"missing1.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing2.txt"}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"missing3.txt"}}</tool>',
            "<final>done</final>",
        ],
        feature_flags={"replan": False},
    )

    result = agent.ask("find the answer")

    assert result == "done"
    assert agent.current_task_state.replan_count == 0
    assert _replan_messages(agent) == []
