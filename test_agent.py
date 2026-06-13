"""离线测试：工具行为 + 内层循环 + 上下文压缩，全部用假客户端，不需要真实 API。

运行: python test_agent.py   （或 python -m pytest test_agent.py -q）
"""

import os
import tempfile
import types

import agent
import tools


# ---------- 假 OpenAI 客户端 ----------

def _fake_tool_call(call_id, name, arguments):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(id=call_id, type="function", function=fn)


def _fake_message(content=None, tool_calls=None):
    return types.SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


class FakeClient:
    """按预设脚本依次返回 message 的假客户端。"""

    def __init__(self, scripted_messages):
        self._script = list(scripted_messages)
        self.calls = []  # 记录每次收到的 messages，便于断言
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, model, messages, tools=None):
        self.calls.append(list(messages))
        msg = self._script.pop(0)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


def _fake_chunk(content=None, tool_calls=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


def _fake_tc_delta(index, call_id=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=call_id, type="function", function=fn)


class FakeStreamClient:
    """支持 stream=True 的假客户端：每次返回一串预设 chunk。"""

    def __init__(self, scripted_chunk_lists):
        self._script = list(scripted_chunk_lists)
        self.calls = []
        self.stream_calls = 0
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, model, messages, tools=None, stream=False):
        self.calls.append(list(messages))
        if not stream:
            raise AssertionError("FakeStreamClient 仅用于 stream=True 路径")
        self.stream_calls += 1
        return iter(self._script.pop(0))


# ---------- 工具测试 ----------

def test_write_then_read_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "note.txt")
        r = tools.write_file(path, "你好 hello")
        assert "已写入" in r
        assert tools.read_file(path) == "你好 hello"
    print("ok test_write_then_read_roundtrip")


def test_bash_runs_and_reports_exit_code():
    assert "[exit=0]" in tools.bash("echo hi")
    assert "hi" in tools.bash("echo hi")
    out = tools.bash("exit 3")
    assert "[exit=3]" in out
    print("ok test_bash_runs_and_reports_exit_code")


def test_bash_timeout():
    out = tools.bash("sleep 5", timeout=1)
    assert "超时" in out
    print("ok test_bash_timeout")


def test_http_get_rejects_non_http():
    assert "仅支持" in tools.http_get("ftp://example.com")
    print("ok test_http_get_rejects_non_http")


def test_new_tools_registered():
    for name in ("write_file", "bash", "http_get"):
        assert name in tools.TOOL_FUNCTIONS
    schema_names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert {"write_file", "bash", "http_get"} <= schema_names
    print("ok test_new_tools_registered")


# ---------- 循环测试 ----------

def test_loop_executes_tool_then_answers():
    # 第一轮：模型请求 calculator；第二轮：给最终答案
    script = [
        _fake_message(tool_calls=[_fake_tool_call("c1", "calculator", '{"expression": "2+3"}')]),
        _fake_message(content="答案是 5"),
    ]
    client = FakeClient(script)
    result = agent.run_loop("算 2+3", verbose=False, client=client)
    assert result == "答案是 5"
    # 第二次请求的历史里应包含 tool 结果 "5"
    second_call_msgs = client.calls[1]
    tool_msgs = [m for m in second_call_msgs if (m.get("role") if isinstance(m, dict) else None) == "tool"]
    assert any(m["content"] == "5" for m in tool_msgs)
    print("ok test_loop_executes_tool_then_answers")


def test_loop_unknown_tool_does_not_crash():
    script = [
        _fake_message(tool_calls=[_fake_tool_call("c1", "nope", "{}")]),
        _fake_message(content="处理完毕"),
    ]
    client = FakeClient(script)
    result = agent.run_loop("test", verbose=False, client=client)
    assert result == "处理完毕"
    print("ok test_loop_unknown_tool_does_not_crash")


# ---------- 上下文压缩测试 ----------

def test_estimate_tokens_grows():
    small = [{"role": "user", "content": "hi"}]
    big = [{"role": "user", "content": "x" * 4000}]
    assert agent.estimate_tokens(big) > agent.estimate_tokens(small)
    print("ok test_estimate_tokens_grows")


def test_compact_preserves_tool_pairing_and_shrinks():
    # 构造一段长历史：system + 多个 (user, assistant+tool_calls, tool) 单元
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"q{i} " + "x" * 500})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"t{i}", "function": {"name": "bash", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "y" * 500})

    summarizer = FakeClient([_fake_message(content="这是历史摘要")])
    compacted = agent.compact_messages(msgs, summarizer, keep_recent=6, verbose=False)

    # 应该变短
    assert len(compacted) < len(msgs)
    # 含有摘要
    assert any("历史对话摘要" in (m.get("content") or "") for m in compacted if m.get("role") == "system")
    # 关键：被保留段里每个 role=tool 都能找到它配对的 assistant tool_calls
    seen_ids = set()
    for m in compacted:
        for call in (m.get("tool_calls") or []):
            seen_ids.add(call["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in seen_ids, "tool 结果失去了配对的 tool_calls！"
    print("ok test_compact_preserves_tool_pairing_and_shrinks")


def test_compact_summarizer_failure_degrades_gracefully():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs.append({"role": "user", "content": f"q{i} " + "x" * 500})

    class BoomClient:
        def __init__(self):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=self._boom))

        def _boom(self, *a, **k):
            raise RuntimeError("API down")

    compacted = agent.compact_messages(msgs, BoomClient(), keep_recent=4, verbose=False)
    assert len(compacted) < len(msgs)  # 仍然压缩成功（降级路径）
    assert any("摘要降级" in (m.get("content") or "") for m in compacted)
    print("ok test_compact_summarizer_failure_degrades_gracefully")


# ---------- Agent 类测试 ----------

def test_agent_class_run_executes_tool_then_answers():
    script = [
        _fake_message(tool_calls=[_fake_tool_call("c1", "calculator", '{"expression": "2+3"}')]),
        _fake_message(content="答案是 5"),
    ]
    client = FakeClient(script)
    a = agent.Agent(client=client, verbose=False)
    assert a.run("算 2+3") == "答案是 5"
    print("ok test_agent_class_run_executes_tool_then_answers")


def test_agent_register_custom_tool():
    a = agent.Agent(client=FakeClient([]), verbose=False)
    n_before = len(a.tool_schemas)
    a.register("shout", lambda text: text.upper(),
               {"type": "function", "function": {"name": "shout",
                "parameters": {"type": "object", "properties": {}}}})
    assert "shout" in a.tool_functions
    assert a.tool_functions["shout"]("hi") == "HI"
    assert len(a.tool_schemas) == n_before + 1
    # 注册不应污染全局注册表（实例隔离）
    assert "shout" not in agent.TOOL_FUNCTIONS
    print("ok test_agent_register_custom_tool")


def test_agent_register_overwrites_schema_without_duplicate():
    a = agent.Agent(client=FakeClient([]), verbose=False)
    n_before = len(a.tool_schemas)
    schema = {"type": "function", "function": {"name": "calculator",
              "parameters": {"type": "object", "properties": {}}}}
    a.register("calculator", lambda **k: "x", schema)
    names = [s["function"]["name"] for s in a.tool_schemas]
    assert names.count("calculator") == 1  # 覆盖而非重复
    assert len(a.tool_schemas) == n_before
    print("ok test_agent_register_overwrites_schema_without_duplicate")


def test_execute_tool_bad_json_arguments():
    out = agent.execute_tool("calculator", "{not json")
    assert "不是合法 JSON" in out
    print("ok test_execute_tool_bad_json_arguments")


def test_execute_tool_arg_mismatch():
    out = agent.execute_tool("calculator", '{"wrong": 1}')
    assert "参数不匹配" in out
    print("ok test_execute_tool_arg_mismatch")


# ---------- 流式输出测试 ----------

def test_agent_streaming_yields_text_increments():
    chunks = [_fake_chunk(content="部分"), _fake_chunk(content="答案"),
              _fake_chunk(content="完成")]
    a = agent.Agent(client=FakeStreamClient([chunks]), verbose=False)
    pieces = list(a.run("问题", stream=True))
    assert pieces == ["部分", "答案", "完成"]
    print("ok test_agent_streaming_yields_text_increments")


def test_agent_streaming_with_tool_call_then_answer():
    # 第一次流式返回一个分片的 tool_call；第二次流式返回最终答案文本
    tool_chunks = [
        _fake_chunk(tool_calls=[_fake_tc_delta(0, call_id="c1", name="calculator")]),
        _fake_chunk(tool_calls=[_fake_tc_delta(0, arguments='{"expr')]),
        _fake_chunk(tool_calls=[_fake_tc_delta(0, arguments='ession": "2+3"}')]),
    ]
    answer_chunks = [_fake_chunk(content="结果 "), _fake_chunk(content="5")]
    client = FakeStreamClient([tool_chunks, answer_chunks])
    a = agent.Agent(client=client, verbose=False)
    pieces = list(a.run("算 2+3", stream=True))
    assert "".join(pieces) == "结果 5"
    assert client.stream_calls == 2
    # 第二次请求历史里应包含工具执行结果 "5"
    second = client.calls[1]
    tool_msgs = [m for m in second if isinstance(m, dict) and m.get("role") == "tool"]
    assert any(m["content"] == "5" for m in tool_msgs)
    print("ok test_agent_streaming_with_tool_call_then_answer")


def test_run_loop_stream_falls_back_on_fake_client():
    # FakeClient 不接受 stream 参数 -> _chat 应回退普通请求，仍拿到答案
    script = [_fake_message(content="直接答案")]
    out = agent.run_loop("hi", verbose=False, client=FakeClient(script), stream=True)
    assert out == "直接答案"
    print("ok test_run_loop_stream_falls_back_on_fake_client")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 个测试通过 ✅")
