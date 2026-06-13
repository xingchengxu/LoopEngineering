# LoopEngineering — 最小 Agent Loop

> 语言：**中文** | [English](README.md)

一个最小可运行的 agentic loop 示例：模型在 while 循环中反复「思考 → 调用工具 → 看结果」，直到给出最终答案。使用 OpenAI 兼容 API，可接 OpenAI 官方、vLLM、DeepSeek 等任何兼容 endpoint。

## 结构

```
agent.py    内层循环：可复用的 Agent 类（配置/工具注册/run）+ 函数式入口 run_loop()
tools.py    工具注册表：schema（给模型看）+ Python 函数（真正执行）
loop.sh     外层循环：反复无头调用 claude / codex / 本项目 agent.py，状态持久化在 STATE.md
test_agent.py  内层离线单元测试（假 OpenAI 客户端，无需真实 API）
test_e2e.sh    外层端到端测试（stub 内层 agent，验证状态注入/写回/完成标记）
```

## 可复用的 Agent 类（agent.py）

内层逻辑抽象成 `Agent` 类：配置集中在 `__init__`、工具集每实例独立、`run()` 是经典的
「发请求 → 执行工具 → 回填结果 → 再发请求」循环。

```python
from agent import Agent

agent = Agent(model="gpt-4o")                 # 配置集中在构造函数
agent.register("my_tool", my_func, my_schema) # 注册/覆盖工具（同名覆盖不重复 schema）

answer = agent.run("帮我算 (27*453)+19")       # 普通模式：返回最终答案字符串

for chunk in agent.run("讲讲这个项目", stream=True):  # 流式模式：逐段产出文本增量
    print(chunk, end="", flush=True)
```

- **工具隔离**：实例工具集是全局注册表的拷贝，`register()` 只影响该实例，不污染全局。
- **健壮工具错误处理**：`execute_tool` 分别处理「未知工具 / 参数非法 JSON / 参数不匹配 /
  执行抛异常」，全部返回**可读错误串**而非抛出 —— 模型能看到错误并自我纠正，循环不崩。
- **流式输出**：`stream=True` 时聚合 OpenAI 流式 chunk（content 实时吐出、tool_calls 按
  index 累积）；带工具调用的轮次仍走普通请求以稳健拿到 tool_calls；底层 client 不支持
  stream 时自动回退普通请求。CLI 亦支持 `python agent.py --stream "..."`。
- `run_loop()` 保留为向后兼容的薄包装，内部委托 `Agent`。

## 外层循环（loop.sh）

Boris Cherny 式的 loop engineering：脚本反复调用编码 agent，每轮 agent 读 `STATE.md` 续上进度、干一步、写回状态，直到输出完成标记 `LOOP_TASK_COMPLETE` 或达到轮数上限。

```bash
./loop.sh "把 tools.py 里的每个工具都补上单元测试，全部通过为止"
AGENT=codex MAX_ITERS=20 ./loop.sh "修复所有 lint 报错"
AGENT=agent ./loop.sh "..."        # 用本项目的 agent.py 作为内层（见下）
AGENT_CMD='my-llm --model x' ./loop.sh "..."   # 完全自定义内层调用命令
```

注意：脚本用了 `--dangerously-skip-permissions`（claude）/ `--full-auto`（codex）以便无人值守，只在可信目录里运行。各轮完整输出保存在 `loop_logs/`。

### 内外层共用同一套约定

外层（loop.sh）和内层（agent.py）共享**同一套契约**，所以 `loop.sh` 既能驱动 claude/codex，
也能用 `AGENT=agent` 把本项目的 `agent.py` 当内层：

- **状态文件**：跨轮唯一记忆是 `STATE.md`（`STATE_FILE` 可改）。外层每轮只把它拼进 prompt
  （滚动摘要，而非全部历史），内层用 `read_file`/`write_file` 读写它。
- **完成标记**：内层在最后**独占一行**输出 `LOOP_TASK_COMPLETE`，外层 grep 到（精确整行匹配，
  正文句中提及不会误判）即以 0 退出。
- **体积预算**：`STATE_MAX_BYTES`（默认 12000）给 STATE.md 设上限，超出仅告警，提醒精炼摘要。

| AGENT 值 | 内层命令 |
|----------|----------|
| `claude`（默认） | `claude -p "$prompt" --dangerously-skip-permissions` |
| `codex` | `codex exec --full-auto "$prompt"` |
| `agent` | `python3 agent.py "$prompt"`（本项目内层，走 OpenAI 兼容 API） |

## 安装与运行

```bash
pip install -r requirements.txt

# OpenAI 官方
export OPENAI_API_KEY=sk-...

# 或本地 vLLM / 其他兼容服务
export OPENAI_API_KEY=EMPTY
export OPENAI_BASE_URL=http://localhost:8000/v1
export MODEL=Qwen/Qwen2.5-72B-Instruct

# 单次执行
python agent.py "算一下 (27 * 453) + 19，再列出当前目录的文件"

# 流式输出（逐段打印）
python agent.py --stream "讲讲这个项目"

# 交互模式
python agent.py
```

## 内置工具

| 工具 | 作用 |
|------|------|
| `calculator` | 安全求值算术表达式（不用 `eval`） |
| `read_file` | 读文本文件（>8000 字符截断） |
| `list_dir` | 列目录 |
| `write_file` | 写文件（覆盖，自动建父目录） |
| `bash` | 在 shell 执行命令，返回 stdout/stderr + 退出码，带超时 |
| `http_get` | 联网 HTTP(S) GET 抓取网页/API |

## 上下文窗口处理（context compression）

模型无状态、`messages` 只增不减，多轮工具调用迟早撑爆 context window。`agent.py`：

- 每轮发请求前用 `estimate_tokens()` 粗估 token（约 4 字符/token）；
- 超过 `CONTEXT_LIMIT`（默认 12000，可用环境变量调）就调用 `compact_messages()`：
  用模型自己把**较早的对话**总结成一条「历史摘要」system 消息，只保留 `system + 摘要 + 最近 KEEP_RECENT 条`；
- 裁剪按**完整单元**进行，绝不把 assistant 的 `tool_calls` 与其 `role=tool` 结果拆散（否则 `tool_call_id` 不配对，API 报错）；
- 摘要调用失败时**降级**为纯文本截断拼接，保证压缩永不崩。

## 循环的关键设计点

1. **退出条件**：`msg.tool_calls` 为空 → 模型不再需要工具 → 返回最终答案
2. **配对**：每个 `tool_call` 必须有一条 `role="tool"` 消息，通过 `tool_call_id` 对应，否则 API 报错
3. **历史累积**：模型无状态，assistant 的工具请求和 tool 结果都要追加进 `messages`
4. **安全上限**：`MAX_TURNS` 防止模型陷入无限循环
5. **上下文压缩**：见上节，避免长任务撑爆 context window

## 测试

两套测试都离线、零外部依赖，无需真实 API：

```bash
python test_agent.py   # 内层单元测试：工具/循环/压缩/Agent 类/流式/错误处理（18 个）
bash   test_e2e.sh     # 外层端到端：用 stub 内层 agent 验证 loop.sh 的状态注入/写回/完成标记
```

- `test_agent.py` 用假 OpenAI 客户端验证内层：工具注册表、`run_loop`/`Agent.run`、上下文压缩、
  工具错误处理、流式聚合等。
- `test_e2e.sh` 在临时目录里用一个 stub「内层 agent」（经 `AGENT_CMD` 注入）跑真实的 `loop.sh`，
  断言：上一轮 STATE.md 注入了 prompt、内层写回了 STATE.md、独占一行的完成标记被检测到即退出、
  无标记则跑满轮数、句中标记不会被误判。

## 如何加新工具

在 `tools.py` 中：写一个函数 → 注册到 `TOOL_FUNCTIONS` → 在 `TOOL_SCHEMAS` 加对应 JSON Schema。循环代码无需改动。
