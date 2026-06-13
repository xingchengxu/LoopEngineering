#!/usr/bin/env bash
# test_e2e.sh — loop.sh 外层循环的端到端测试（离线，零依赖）。
#
# 用一个 stub「内层 agent」（通过 AGENT_CMD 注入）替代真实的 claude/codex/agent.py，
# 在临时目录里跑 loop.sh，验证内外层共用的那套约定真的成立：
#   1. 状态注入：上一轮的 STATE.md 内容被拼进喂给内层的 prompt。
#   2. 状态写回：内层写 STATE.md，作为跨轮记忆持久化。
#   3. 完成标记：内层独占一行输出 LOOP_TASK_COMPLETE → 外层检测到并以 0 退出。
#   4. 轮数上限：始终不输出标记 → 跑满 MAX_ITERS 后非零退出。
#   5. 误匹配防护：标记只出现在正文句中（非独占一行）→ 不应判定为完成。
#
# 运行：bash test_e2e.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOOP="$HERE/loop.sh"

PASS=0
FAIL=0
check() {  # check <实际rc为0表示通过> <描述>
  if [ "$1" -eq 0 ]; then
    echo "ok   $2"; PASS=$((PASS + 1))
  else
    echo "FAIL $2"; FAIL=$((FAIL + 1))
  fi
}
contains() { printf '%s' "$1" | grep -q -- "$2"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"   # loop.sh 的 loop_logs/ 等相对路径落在临时目录，不污染项目

# ---------------------------------------------------------------------------
# 测试 1：完成标记 → 1 轮退出；状态被注入 prompt 且被写回
# ---------------------------------------------------------------------------
SEED="SEED_MARKER_$$"
STATE1="$WORK/state1.md"
printf '# state\n%s\n' "$SEED" > "$STATE1"

STUB1="$WORK/stub_done.sh"
cat > "$STUB1" <<STUBEOF
#!/usr/bin/env bash
# 内层 agent stub：第一个参数即外层拼好的 prompt。
prompt="\$1"
# 若 prompt 里带上了上一轮 STATE.md 的内容，说明状态注入成立
if printf '%s' "\$prompt" | grep -q "$SEED"; then
  echo "STATE_INJECTED_OK"
fi
# 模拟内层用 write_file 把进度写回状态文件（跨轮记忆）
printf '# state\nstub 干完了一步\n' > "$STATE1"
echo "已完成本轮工作。"
echo "LOOP_TASK_COMPLETE"
STUBEOF
chmod +x "$STUB1"

set +e
OUT1="$(AGENT_CMD="$STUB1" STATE_FILE="$STATE1" MAX_ITERS=3 "$LOOP" "测试任务" 2>&1)"
RC1=$?
set -e 2>/dev/null || true

check "$RC1" "完成标记 → loop 以 0 退出"
contains "$OUT1" "STATE_INJECTED_OK"; check $? "上一轮 STATE.md 被注入到 prompt"
contains "$OUT1" "检测到完成标记"; check $? "外层报告检测到完成标记"
contains "$OUT1" "共 1 轮"; check $? "检测到标记后第 1 轮即退出"
STATE1_CONTENT="$(cat "$STATE1")"
contains "$STATE1_CONTENT" "stub 干完了一步"; check $? "内层把进度写回了 STATE.md"

# ---------------------------------------------------------------------------
# 测试 2：始终不输出标记 → 跑满 MAX_ITERS 后非零退出
# ---------------------------------------------------------------------------
STATE2="$WORK/state2.md"
STUB2="$WORK/stub_never.sh"
cat > "$STUB2" <<'STUBEOF'
#!/usr/bin/env bash
echo "干了一点活，但任务还没完。"
STUBEOF
chmod +x "$STUB2"

set +e
OUT2="$(AGENT_CMD="$STUB2" STATE_FILE="$STATE2" MAX_ITERS=2 "$LOOP" "永不完成的任务" 2>&1)"
RC2=$?
set -e 2>/dev/null || true

[ "$RC2" -ne 0 ]; check $? "无完成标记 → loop 非零退出"
contains "$OUT2" "达到最大轮数"; check $? "跑满 MAX_ITERS 后报告未完成"
contains "$OUT2" "第 2 轮"; check $? "确实跑满了 2 轮"

# ---------------------------------------------------------------------------
# 测试 3：标记只出现在正文句中（非独占一行）→ 不应判定为完成
# ---------------------------------------------------------------------------
STATE3="$WORK/state3.md"
STUB3="$WORK/stub_inline.sh"
cat > "$STUB3" <<'STUBEOF'
#!/usr/bin/env bash
# 故意把标记嵌在句子里，绝不独占一行——外层不应误判为完成
echo "我还没准备好输出 LOOP_TASK_COMPLETE 这个标记，再等等。"
STUBEOF
chmod +x "$STUB3"

set +e
OUT3="$(AGENT_CMD="$STUB3" STATE_FILE="$STATE3" MAX_ITERS=2 "$LOOP" "句中标记任务" 2>&1)"
RC3=$?
set -e 2>/dev/null || true

[ "$RC3" -ne 0 ]; check $? "句中标记 → 未被误判为完成（loop 非零退出）"
contains "$OUT3" "达到最大轮数"; check $? "句中标记 → 跑满轮数而非提前退出"

# ---------------------------------------------------------------------------
echo
echo "E2E: $PASS 通过, $FAIL 失败"
[ "$FAIL" -eq 0 ]
