# LoopEngineering — Minimal Agent Loop

> Language: **English** | [中文](README_cn.md)

A minimal, runnable agentic-loop example: the model repeats "think → call tool → see result" inside a while loop until it produces a final answer. It uses an OpenAI-compatible API, so it works with OpenAI itself, vLLM, DeepSeek, or any compatible endpoint.

## Layout

```
agent.py       Inner loop: a reusable Agent class (config/tool registration/run) + functional entry run_loop()
tools.py       Tool registry: schemas (for the model) + Python functions (the real execution)
loop.sh        Outer loop: repeatedly invokes claude / codex / this project's agent.py headless; state persisted in STATE.md
test_agent.py  Inner offline unit tests (fake OpenAI client, no real API needed)
test_e2e.sh    Outer end-to-end tests (stub inner agent; verifies state injection / write-back / done marker)
```

## The reusable Agent class (agent.py)

The inner logic is abstracted into an `Agent` class: configuration is centralized in `__init__`, the tool set is per-instance, and `run()` is the classic "send request → execute tools → feed results back → send again" loop.

```python
from agent import Agent

agent = Agent(model="gpt-4o")                  # config centralized in the constructor
agent.register("my_tool", my_func, my_schema)  # register/override a tool (same name overrides, no duplicate schema)

answer = agent.run("compute (27*453)+19")      # normal mode: returns the final answer string

for chunk in agent.run("tell me about this project", stream=True):  # streaming mode: yields text deltas
    print(chunk, end="", flush=True)
```

- **Tool isolation**: an instance's tool set is a copy of the global registry; `register()` affects only that instance and never pollutes the global registry.
- **Robust tool error handling**: `execute_tool` separately handles "unknown tool / invalid-JSON arguments / argument mismatch / execution raised", and returns a **readable error string** instead of throwing — the model can see the error and self-correct, and the loop doesn't crash.
- **Streaming output**: with `stream=True` it aggregates OpenAI streaming chunks (content is emitted live, tool_calls are accumulated by index); turns that involve tool calls still use a normal request to robustly obtain the tool_calls; if the underlying client doesn't support streaming it automatically falls back to a normal request. The CLI also supports `python agent.py --stream "..."`.
- `run_loop()` is kept as a thin backward-compatible wrapper that delegates to `Agent`.

## The outer loop (loop.sh)

Boris Cherny-style loop engineering: the script repeatedly invokes a coding agent; each round the agent reads `STATE.md` to resume progress, does one step, and writes the state back — until it outputs the done marker `LOOP_TASK_COMPLETE` or the iteration cap is reached.

```bash
./loop.sh "add unit tests for every tool in tools.py until they all pass"
AGENT=codex MAX_ITERS=20 ./loop.sh "fix all lint errors"
AGENT=agent ./loop.sh "..."        # use this project's agent.py as the inner agent (see below)
AGENT_CMD='my-llm --model x' ./loop.sh "..."   # fully custom inner-agent command
```

Note: the script uses `--dangerously-skip-permissions` (claude) / `--full-auto` (codex) so it can run unattended — only run it in a trusted directory. Each round's full output is saved under `loop_logs/`.

### Inner and outer share one contract

The outer loop (loop.sh) and the inner loop (agent.py) share the **same contract**, so `loop.sh` can drive claude/codex, and with `AGENT=agent` it can also use this project's `agent.py` as the inner agent:

- **State file**: the only cross-round memory is `STATE.md` (`STATE_FILE` is configurable). Each round the outer loop splices only this into the prompt (a rolling summary, not the full history); the inner agent reads/writes it via `read_file`/`write_file`.
- **Done marker**: the inner agent outputs `LOOP_TASK_COMPLETE` on its **own line** at the very end; the outer loop greps for it (exact whole-line match, so a mid-sentence mention won't trigger a false positive) and exits 0.
- **Size budget**: `STATE_MAX_BYTES` (default 12000) caps STATE.md; exceeding it only warns, nudging the agent to keep the summary concise.

| AGENT value | Inner command |
|----------|----------|
| `claude` (default) | `claude -p "$prompt" --dangerously-skip-permissions` |
| `codex` | `codex exec --full-auto "$prompt"` |
| `agent` | `python3 agent.py "$prompt"` (this project's inner loop, via an OpenAI-compatible API) |

#### Useful loop.sh environment variables

| Variable | Purpose |
|---|---|
| `MAX_ITERS` | Maximum number of rounds (default 10) |
| `ITER_TIMEOUT` | Per-round timeout in seconds, 0 = unlimited (portable impl; macOS has no `timeout`) |
| `MODEL` | Model name passed to claude/codex |
| `PROXY` | Inject `https_proxy`/`http_proxy` (when the CLI must reach the network via a proxy) |
| `STATE_FILE` | Cross-round memory file (default STATE.md) |
| `STATE_MAX_BYTES` | STATE.md size budget; exceeding it only warns |

## Install and run

```bash
pip install -r requirements.txt

# OpenAI official
export OPENAI_API_KEY=sk-...

# or local vLLM / other compatible service
export OPENAI_API_KEY=EMPTY
export OPENAI_BASE_URL=http://localhost:8000/v1
export MODEL=Qwen/Qwen2.5-72B-Instruct

# single run
python agent.py "compute (27 * 453) + 19, then list the files in the current directory"

# streaming output (printed incrementally)
python agent.py --stream "tell me about this project"

# interactive mode
python agent.py
```

## Built-in tools

| Tool | Purpose |
|------|------|
| `calculator` | Safely evaluate arithmetic expressions (no `eval`) |
| `read_file` | Read a text file (truncated past 8000 chars) |
| `list_dir` | List a directory |
| `write_file` | Write a file (overwrite, auto-create parent dirs) |
| `bash` | Run a shell command, returns stdout/stderr + exit code, with timeout |
| `http_get` | Networked HTTP(S) GET to fetch a web page / API |

## Context-window handling (context compression)

The model is stateless and `messages` only grows, so multi-round tool calls will eventually blow past the context window. `agent.py`:

- Before each request, `estimate_tokens()` roughly estimates tokens (~4 chars/token);
- Once it exceeds `CONTEXT_LIMIT` (default 12000, tunable via env var), `compact_messages()` runs: it asks the model to summarize the **earlier conversation** into a single "history summary" system message, keeping only `system + summary + the most recent KEEP_RECENT messages`;
- Trimming happens by **complete unit** — it never splits an assistant's `tool_calls` from its `role=tool` results (otherwise `tool_call_id` mismatches and the API errors);
- If the summary call fails, it **degrades** to plain text truncation so compaction never crashes.

## Key loop design points

1. **Exit condition**: `msg.tool_calls` is empty → the model no longer needs tools → return the final answer
2. **Pairing**: every `tool_call` must have a matching `role="tool"` message tied by `tool_call_id`, or the API errors
3. **History accumulation**: the model is stateless, so the assistant's tool requests and the tool results must both be appended to `messages`
4. **Safety cap**: `MAX_TURNS` prevents the model from looping forever
5. **Context compaction**: see the section above; avoids long tasks blowing past the context window

## Tests

Both test suites are offline with zero external dependencies — no real API needed:

```bash
python test_agent.py   # inner unit tests: tools/loop/compaction/Agent class/streaming/error handling (18 tests)
bash   test_e2e.sh     # outer end-to-end: a stub inner agent verifies loop.sh's state injection/write-back/done marker
```

- `test_agent.py` uses a fake OpenAI client to verify the inner loop: the tool registry, `run_loop`/`Agent.run`, context compaction, tool error handling, streaming aggregation, etc.
- `test_e2e.sh` runs the real `loop.sh` in a temp directory with a stub "inner agent" (injected via `AGENT_CMD`), asserting: the previous round's STATE.md was injected into the prompt, the inner agent wrote STATE.md back, a done marker on its own line is detected and exits, no marker runs to the iteration cap, and a mid-sentence marker isn't misdetected.

## How to add a new tool

In `tools.py`: write a function → register it in `TOOL_FUNCTIONS` → add the matching JSON Schema to `TOOL_SCHEMAS`. The loop code needs no changes.
