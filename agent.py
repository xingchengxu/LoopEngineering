"""最小 agent loop（OpenAI 兼容 API）。

核心就是 run_loop() 里的 while 循环：
    发请求 -> 模型要么给答案、要么请求工具
    -> 执行工具 -> 把结果塞回历史 -> 再发请求 ... 直到没有工具调用。

配置（环境变量）:
    OPENAI_API_KEY    必填（本地 vLLM 可随便填，如 "EMPTY"）
    OPENAI_BASE_URL   选填，OpenAI 兼容 endpoint，如 http://localhost:8000/v1
    MODEL             选填，模型名，默认 gpt-4o
    CONTEXT_LIMIT     选填，触发上下文压缩的 token 估算阈值，默认 12000

上下文窗口处理（context compression）:
    模型无状态，messages 只增不减，迟早撑爆 context window。
    每轮发请求前先估算 token，超过 CONTEXT_LIMIT 就把较早的对话压缩成
    一条摘要消息（用模型自己做摘要），只保留 system + 摘要 + 最近若干消息。
    压缩时按「完整单元」裁剪，绝不拆散 assistant 的 tool_calls 与其 tool 结果，
    否则 OpenAI API 会因 tool_call_id 不配对而报错。
"""

import json
import os

from tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

MODEL = os.environ.get("MODEL", "gpt-4o")
MAX_TURNS = 20  # 防止失控循环的安全上限
CONTEXT_LIMIT = int(os.environ.get("CONTEXT_LIMIT", "12000"))  # token 估算阈值
KEEP_RECENT = 6  # 压缩时至少保留最近的消息条数


def _make_client():
    """延迟创建 client，便于测试时注入假客户端。"""
    from openai import OpenAI

    return OpenAI()  # 自动读取 OPENAI_API_KEY / OPENAI_BASE_URL


SYSTEM_PROMPT = "你是一个会使用工具的助手。需要计算或读文件时调用工具，不要凭空猜测。"


def _msg_text(m) -> str:
    """把一条消息（dict 或 OpenAI message 对象）拍平成字符串，用于估算长度。"""
    if isinstance(m, dict):
        parts = [str(m.get("content") or "")]
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            parts.append(str(fn.get("name", "")) + str(fn.get("arguments", "")))
        return " ".join(parts)
    # OpenAI 的 message 对象
    parts = [str(getattr(m, "content", "") or "")]
    for call in getattr(m, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        if fn is not None:
            parts.append(str(getattr(fn, "name", "")) + str(getattr(fn, "arguments", "")))
    return " ".join(parts)


def estimate_tokens(messages) -> int:
    """粗略 token 估算：约 4 字符 / token（中英文混排的够用近似）。"""
    chars = sum(len(_msg_text(m)) for m in messages)
    return chars // 4 + 1


def _role(m) -> str:
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", "")


def _has_tool_calls(m) -> bool:
    if isinstance(m, dict):
        return bool(m.get("tool_calls"))
    return bool(getattr(m, "tool_calls", None))


def _split_keep_boundary(messages, keep_recent):
    """从尾部保留至少 keep_recent 条，但把分界点往前移到一个安全边界：
    分界处不能让某条 role=tool 成为被保留段的开头（它的 assistant tool_calls 会被压缩掉）。
    返回 (older, recent)。"""
    n = len(messages)
    if n <= keep_recent:
        return [], list(messages)
    cut = n - keep_recent
    # 若被保留段开头是 tool 结果，则把它也归入 older，直到边界落在非 tool 消息上
    while cut < n and _role(messages[cut]) == "tool":
        cut += 1
    return list(messages[:cut]), list(messages[cut:])


def compact_messages(messages, client, model=MODEL, keep_recent=KEEP_RECENT, verbose=False):
    """把较早的对话压缩成一条摘要消息，控制 context window 大小。

    保留：第一条 system 提示 + 一条「历史摘要」+ 最近 keep_recent 条消息。
    摘要由模型生成；若模型调用失败则降级为简单的文本拼接截断。
    """
    if not messages:
        return messages

    system_msgs = [m for m in messages if _role(m) == "system"]
    rest = [m for m in messages if _role(m) != "system"]

    older, recent = _split_keep_boundary(rest, keep_recent)
    if not older:
        return messages  # 没有可压缩的早期消息

    transcript = "\n".join(f"[{_role(m)}] {_msg_text(m)[:1000]}" for m in older)
    summary_prompt = (
        "下面是一段 agent 与工具交互的历史记录。请用简洁中文总结其中的关键事实、"
        "已完成的步骤、工具返回的重要结果和尚未解决的问题，供后续继续任务时参考：\n\n"
        + transcript
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        summary = resp.choices[0].message.content or ""
    except Exception as e:
        # 降级：不调用模型，直接截断拼接
        summary = transcript[:2000] + f"\n...[摘要降级，原始 {len(transcript)} 字符；{e}]"

    if verbose:
        print(f"[compact] 压缩 {len(older)} 条早期消息为摘要（{len(summary)} 字符）")

    summary_msg = {
        "role": "system",
        "content": "【历史对话摘要】\n" + summary,
    }
    return system_msgs[:1] + [summary_msg] + recent


def execute_tool(name: str, arguments: str, tool_functions=None) -> str:
    """根据模型给的工具名和 JSON 参数串执行工具，返回字符串结果。

    健壮的工具错误处理：分别捕获「未知工具 / 参数解析失败 / 执行抛异常」三类错误，
    任何一类都返回一段可读的错误字符串（而不是抛出），让模型能看到错误并自我纠正，
    内层循环不会因为某次工具调用失败而崩溃。
    """
    funcs = TOOL_FUNCTIONS if tool_functions is None else tool_functions
    fn = funcs.get(name)
    if fn is None:
        return f"错误: 未知工具 {name}（可用: {', '.join(sorted(funcs))}）"
    try:
        kwargs = json.loads(arguments or "{}")
    except json.JSONDecodeError as e:
        return f"工具参数不是合法 JSON: {e}（收到: {arguments!r}）"
    if not isinstance(kwargs, dict):
        return f"工具参数必须是 JSON 对象，收到 {type(kwargs).__name__}: {arguments!r}"
    try:
        result = fn(**kwargs)
    except TypeError as e:
        return f"工具 {name} 参数不匹配: {e}"
    except Exception as e:
        return f"工具 {name} 执行出错: {e}"
    return result if isinstance(result, str) else str(result)


class Agent:
    """可复用的内层 agent：封装配置、工具注册、和 run 接口。

    用法:
        agent = Agent(model="gpt-4o")
        agent.register("my_tool", my_func, my_schema)   # 可选：注册自定义工具
        answer = agent.run("帮我算 2+3")                  # 跑一次内层循环拿最终答案
        for chunk in agent.run("...", stream=True): ...  # 流式拿增量输出

    设计要点:
        - 配置集中在 __init__（model / system_prompt / 上下文阈值 / 工具集 等）。
        - 工具默认取全局注册表，但每个实例可独立增删，互不影响。
        - run() 内部就是经典的 "发请求→执行工具→回填结果→再发请求" 循环。
    """

    def __init__(self, client=None, model=None, system_prompt=SYSTEM_PROMPT,
                 tools=None, tool_schemas=None, max_turns=MAX_TURNS,
                 context_limit=CONTEXT_LIMIT, keep_recent=KEEP_RECENT,
                 verbose=True):
        self.client = client
        self.model = model or MODEL
        self.system_prompt = system_prompt
        # 复制一份，避免实例改动污染全局注册表
        self.tool_functions = dict(TOOL_FUNCTIONS if tools is None else tools)
        self.tool_schemas = list(TOOL_SCHEMAS if tool_schemas is None else tool_schemas)
        self.max_turns = max_turns
        self.context_limit = context_limit
        self.keep_recent = keep_recent
        self.verbose = verbose

    def _client(self):
        if self.client is None:
            self.client = _make_client()
        return self.client

    def register(self, name, func, schema):
        """注册（或覆盖）一个工具：函数 + 给模型看的 JSON Schema。"""
        self.tool_functions[name] = func
        self.tool_schemas = [s for s in self.tool_schemas
                             if s.get("function", {}).get("name") != name]
        self.tool_schemas.append(schema)
        return self

    def execute_tool(self, name, arguments):
        return execute_tool(name, arguments, tool_functions=self.tool_functions)

    def run(self, user_input, stream=False):
        """跑一次内层循环。stream=True 时返回一个生成器，逐段产出最终答案文本。"""
        if stream:
            return self._run_stream(user_input)
        return self._run(user_input, on_delta=None)

    def _run(self, user_input, on_delta=None):
        client = self._client()
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        for turn in range(self.max_turns):
            if estimate_tokens(messages) > self.context_limit:
                messages = compact_messages(messages, client, model=self.model,
                                            keep_recent=self.keep_recent,
                                            verbose=self.verbose)

            # 仅在「期望最终答案」且调用方要流式时才开 stream；
            # 带工具调用的轮次仍走普通请求，便于稳健地拿到 tool_calls。
            msg = self._chat(client, messages, on_delta=on_delta)

            if not msg.tool_calls:
                return msg.content or ""

            messages.append(msg)
            for call in msg.tool_calls:
                result = self.execute_tool(call.function.name, call.function.arguments)
                if self.verbose:
                    print(f"[turn {turn}] 工具 {call.function.name}"
                          f"({call.function.arguments}) -> {result[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })

        return "已达到最大轮数限制，任务未完成。"

    def _chat(self, client, messages, on_delta=None):
        """发一次请求，返回 message 对象（含 content 和/或 tool_calls）。

        on_delta 不为空时尝试流式：逐段把文本增量交给 on_delta，并聚合出
        与非流式等价的 message（content + tool_calls）。若底层 client 不支持
        stream（如测试用的假客户端），自动回退到普通请求。
        """
        if on_delta is None:
            response = client.chat.completions.create(
                model=self.model, messages=messages, tools=self.tool_schemas)
            return response.choices[0].message
        try:
            chunks = client.chat.completions.create(
                model=self.model, messages=messages,
                tools=self.tool_schemas, stream=True)
            return _consume_stream(chunks, on_delta)
        except TypeError:
            # 假客户端不接受 stream 参数 -> 回退普通请求
            response = client.chat.completions.create(
                model=self.model, messages=messages, tools=self.tool_schemas)
            msg = response.choices[0].message
            if msg.content and not msg.tool_calls:
                on_delta(msg.content)
            return msg

    def _run_stream(self, user_input):
        """生成器版 run：把最终答案的文本增量 yield 出来。"""
        captured = []

        def on_delta(text):
            captured.append(text)

        # 复用 _run；但需要在产出最终答案时把增量吐出来。
        # 简化实现：用闭包收集增量，循环结束后整体已通过 on_delta 推送，
        # 这里改为真正逐段 yield。
        client = self._client()
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        for turn in range(self.max_turns):
            if estimate_tokens(messages) > self.context_limit:
                messages = compact_messages(messages, client, model=self.model,
                                            keep_recent=self.keep_recent,
                                            verbose=self.verbose)
            buf = []
            msg = self._chat(client, messages, on_delta=buf.append)
            if not msg.tool_calls:
                if buf:
                    for piece in buf:
                        yield piece
                else:
                    yield msg.content or ""
                return
            messages.append(msg)
            for call in msg.tool_calls:
                result = self.execute_tool(call.function.name, call.function.arguments)
                if self.verbose:
                    print(f"[turn {turn}] 工具 {call.function.name}"
                          f"({call.function.arguments}) -> {result[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        yield "已达到最大轮数限制，任务未完成。"


def _consume_stream(chunks, on_delta):
    """聚合 OpenAI 流式 chunk 为一条 message（content + tool_calls）。

    文本增量实时交给 on_delta；tool_calls 按 index 累积 name/arguments。
    返回一个带 .content / .tool_calls 属性的 SimpleNamespace（与非流式等价）。
    """
    import types

    content_parts = []
    tool_acc = {}  # index -> {id, name, arguments}
    for chunk in chunks:
        choice = chunk.choices[0]
        delta = choice.delta
        text = getattr(delta, "content", None)
        if text:
            content_parts.append(text)
            on_delta(text)
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc, "index", 0)
            slot = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"] += fn.arguments

    tool_calls = None
    if tool_acc:
        tool_calls = []
        for idx in sorted(tool_acc):
            s = tool_acc[idx]
            tool_calls.append(types.SimpleNamespace(
                id=s["id"], type="function",
                function=types.SimpleNamespace(name=s["name"], arguments=s["arguments"])))
    content = "".join(content_parts) or None
    return types.SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


def run_loop(user_input: str, verbose: bool = True, client=None,
             context_limit: int = CONTEXT_LIMIT, stream: bool = False) -> str:
    """函数式入口（向后兼容）：内部委托给 Agent。stream=True 时把增量打印到 stdout。"""
    agent = Agent(client=client, verbose=verbose, context_limit=context_limit)
    if stream:
        parts = []
        for piece in agent.run(user_input, stream=True):
            parts.append(piece)
            print(piece, end="", flush=True)
        print()
        return "".join(parts)
    return agent.run(user_input)


def main():
    import sys

    args = sys.argv[1:]
    stream = False
    if "--stream" in args:
        stream = True
        args = [a for a in args if a != "--stream"]

    if args:
        # 单次模式: python agent.py [--stream] "你的问题"
        out = run_loop(" ".join(args), stream=stream)
        if not stream:
            print(out)
    else:
        # 交互模式
        print(f"模型: {MODEL}  (输入 exit 退出{'，流式输出已开启' if stream else ''})")
        while True:
            try:
                q = input("\n>>> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q in ("exit", "quit", ""):
                break
            out = run_loop(q, stream=stream)
            if not stream:
                print(out)


if __name__ == "__main__":
    main()
