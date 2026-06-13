#!/usr/bin/env bash
# loop.sh — 外层循环（loop engineering 的 "loop"）
#
# 反复以无头模式调用编码 agent（Claude Code 或 Codex），每轮：
#   1. 把「任务 + 当前进度状态(STATE.md)」拼成 prompt 喂给 agent
#   2. agent 干一小步活（它内部自己跑工具调用的内层循环）
#   3. agent 把进度写回 STATE.md（持久化状态，跨轮记忆）
#   4. 检测完成标记 -> 退出；否则继续下一轮
#
# 上下文压缩（外层这一层）：每轮 prompt 只携带 STATE.md 这一份滚动摘要，
# 而不是把所有历史塞进去；STATE_MAX_BYTES 给 STATE.md 设大小预算，超了就告警，
# 提醒 agent 把状态写精炼 —— 这就是把"无界历史"压成"有界摘要"的做法。
#
# 用法:
#   ./loop.sh "把 tools.py 里的每个工具都补上单元测试，全部通过为止"
#   AGENT=codex MAX_ITERS=20 ./loop.sh "修复所有 lint 报错"
#   PROXY=http://127.0.0.1:10808 ./loop.sh "..."        # 给 agent CLI 注入代理
#   AGENT_CMD='my-llm --model x' ./loop.sh "..."         # 完全自定义调用命令
#
# 环境变量:
#   AGENT          claude (默认) | codex | agent     选择内置 agent
#                  agent = 本项目的 agent.py（内外层共用 STATE.md + 完成标记约定）
#   AGENT_CMD      自定义命令；prompt 作为最后一个参数追加，覆盖 AGENT
#   MODEL          传给 claude/codex 的模型名（可选）
#   PROXY          注入 https_proxy/http_proxy（解决 CLI 需走代理联网的情况）
#   MAX_ITERS      最大迭代轮数，默认 10
#   ITER_TIMEOUT   每轮超时秒数，0=不限（默认 0；macOS 无 timeout，内置可移植实现）
#   STATE_FILE     状态文件，默认 STATE.md
#   STATE_MAX_BYTES STATE.md 大小预算（字节），默认 12000，超出仅告警

set -euo pipefail

TASK="${1:?用法: ./loop.sh \"任务描述\"}"
AGENT="${AGENT:-claude}"
AGENT_CMD="${AGENT_CMD:-}"
MODEL="${MODEL:-}"
PROXY="${PROXY:-}"
MAX_ITERS="${MAX_ITERS:-10}"
ITER_TIMEOUT="${ITER_TIMEOUT:-0}"
STATE_FILE="${STATE_FILE:-STATE.md}"
STATE_MAX_BYTES="${STATE_MAX_BYTES:-12000}"
DONE_MARKER="LOOP_TASK_COMPLETE"
LOG_DIR="loop_logs"

# 注入代理（子进程继承）：解决 claude/codex 需经代理才能联网的常见情况
if [ -n "$PROXY" ]; then
  export https_proxy="$PROXY" http_proxy="$PROXY" HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
fi

mkdir -p "$LOG_DIR"
[ -f "$STATE_FILE" ] || printf '# 任务进度状态（由 agent 维护，请保持精炼）\n\n尚未开始。\n' > "$STATE_FILE"

# 可移植超时：macOS 无 timeout/gtimeout，用 后台子进程 + 看门狗 实现
run_with_timeout() {
  local secs="$1"; shift
  if [ "$secs" -le 0 ]; then "$@"; return $?; fi
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null ) &
  local watcher=$!
  disown "$watcher" 2>/dev/null || true   # 避免看门狗被 kill 时打印 "Terminated" 噪音
  local rc=0
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watcher" 2>/dev/null || true
  return ${rc}
}

# 每轮喂给 agent 的 prompt：只带 STATE.md 这份滚动摘要（外层上下文压缩的关键）
build_prompt() {
  cat <<EOF
你在一个自动化外层循环中运行，这是第 $1/$MAX_ITERS 轮。没有人在实时看着你，不要提问。

## 总任务
$TASK

## 当前进度状态（来自 ${STATE_FILE}，上一轮的你写的）
$(cat "$STATE_FILE")

## 本轮要求
1. 根据进度状态，完成任务中下一个最有价值的一小步（可以运行命令、改文件、跑测试验证）。
2. 用实际验证结果（测试输出、命令返回）确认这一步真的完成了，不要凭感觉宣称完成。
3. 把最新进度覆盖写入 ${STATE_FILE}，并保持精炼（< ${STATE_MAX_BYTES} 字节）：
   只记「已完成(附验证证据) / 还剩什么 / 下一步」，不要堆砌全部细节 —— 这是循环跨轮的唯一记忆。
4. 只有当整个总任务全部完成并验证通过时，才在回复的最后一行单独输出：$DONE_MARKER
EOF
}

run_agent() {
  if [ -n "$AGENT_CMD" ]; then
    # AGENT_CMD 按空格分词（让其中的 flag 生效），prompt 作为最后一个完整参数
    # shellcheck disable=SC2086
    $AGENT_CMD "$1"
    return $?
  fi
  case "$AGENT" in
    claude)
      # bash 3.2 + set -u 下空数组 "${a[@]}" 会报 unbound，用 ${a[@]+...} 守卫
      local model_arg=(); [ -n "$MODEL" ] && model_arg=(--model "$MODEL")
      # -p 无头模式；--dangerously-skip-permissions 让循环无人值守可跑（请只在可信目录使用）
      claude -p "$1" --dangerously-skip-permissions ${model_arg[@]+"${model_arg[@]}"}
      ;;
    codex)
      local model_arg=(); [ -n "$MODEL" ] && model_arg=(--model "$MODEL")
      codex exec --full-auto ${model_arg[@]+"${model_arg[@]}"} "$1"
      ;;
    agent)
      # 用本项目自带的内层 agent.py 作为内层 agent：
      # 它通过 read_file/write_file/bash 等工具读写 STATE.md，并把最终答案打到 stdout，
      # 外层在日志里 grep 完成标记 —— 内外层共用同一套约定（状态文件 + 完成标记）。
      # MODEL 通过环境变量传给 agent.py（见 agent.py 顶部配置）。
      python3 "$(dirname "$0")/agent.py" "$1"
      ;;
    *)
      echo "未知 AGENT: ${AGENT}（支持 claude / codex / agent，或用 AGENT_CMD 自定义）" >&2; exit 1
      ;;
  esac
}

echo "agent=${AGENT_CMD:-$AGENT}  model=${MODEL:-default}  proxy=${PROXY:-none}  max_iters=$MAX_ITERS  timeout=${ITER_TIMEOUT}s"
echo "任务: $TASK"

for i in $(seq 1 "$MAX_ITERS"); do
  echo
  echo "===== 第 $i 轮 ====="
  LOG="$LOG_DIR/iter_$(printf '%02d' "$i").log"

  # STATE.md 体积预算：超出说明摘要在膨胀，会逐渐吃掉每轮的 context（外层上下文压缩告警）
  state_bytes=$(wc -c < "$STATE_FILE" | tr -d ' ')
  if [ "$state_bytes" -gt "$STATE_MAX_BYTES" ]; then
    echo "[loop] 警告: $STATE_FILE 已 ${state_bytes}B > 预算 ${STATE_MAX_BYTES}B，prompt 在膨胀，已提示 agent 精炼" >&2
  fi

  rc=0
  if run_with_timeout "$ITER_TIMEOUT" run_agent "$(build_prompt "$i")" >"$LOG" 2>&1; then rc=0; else rc=$?; fi
  cat "$LOG"

  if [ "$rc" -ne 0 ]; then
    echo "[loop] 第 $i 轮 agent 退出码 ${rc}（可能超时或出错），继续下一轮让状态自恢复" >&2
    continue
  fi

  # 完成标记需独占一行，避免误匹配正文里提到的标记名
  if grep -qx "$DONE_MARKER" "$LOG" || grep -qE "^${DONE_MARKER}\s*$" "$LOG"; then
    echo
    echo "[loop] 检测到完成标记，任务完成（共 $i 轮）。"
    echo "[loop] 最终状态见 ${STATE_FILE}，各轮日志见 ${LOG_DIR}/"
    exit 0
  fi
done

echo
echo "[loop] 达到最大轮数 $MAX_ITERS 仍未完成。查看 $STATE_FILE 和 $LOG_DIR/ 决定是否继续。" >&2
exit 1
