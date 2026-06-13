# 任务进度状态（由 agent 维护，请保持精炼）

> 总任务的全部**必做**项已完成并用实际测试输出验证通过。

## 已完成（全部验证通过）
- **工具层** tools.py：calculator/read_file/list_dir/write_file/bash/http_get 共 6 个。
- **可复用 Agent 类** agent.py：配置集中 __init__、工具集每实例独立、register() 同名覆盖去重、
  run(stream=False/True)。run_loop() 为向后兼容薄包装。
- **流式输出**：run(stream=True) 逐段产出；_consume_stream 聚合 chunk；假客户端自动回退。
  CLI 支持 `agent.py --stream`。
- **健壮工具错误处理** execute_tool：未知工具/坏JSON/参数不匹配/执行异常均返回可读错误串不抛出。
- **上下文压缩**：estimate_tokens + compact_messages（超阈值摘要早期对话，裁剪不拆 tool 配对，
  摘要失败降级截断）。
- **内外层共用约定**：STATE.md 状态文件 + 独占一行完成标记。loop.sh 支持
  AGENT=claude/codex/agent + AGENT_CMD 自定义；AGENT=agent 走 `python3 agent.py "$prompt"`。
- **【本轮】持久化 e2e 测试** test_e2e.sh：临时目录 + stub 内层 agent(经 AGENT_CMD)跑真实 loop.sh，
  断言 状态注入prompt / 写回STATE.md / 完成标记检测即退出 / 无标记跑满轮数 / 句中标记不误判。
- **【本轮】README 补齐**：新增 Agent 类用法、流式、内外层共用约定表(AGENT 三种值)、e2e 测试说明，
  修正测试数 10→18、删除过时引用。

## 验证证据（本轮实跑）
- `python3 -m py_compile agent.py tools.py test_agent.py` → OK
- `python3 test_agent.py` → 18/18 通过 ✅
- `bash -n loop.sh && bash -n test_e2e.sh` → syntax OK
- `bash test_e2e.sh` → E2E 10/10 通过 ✅
- `grep "10 个测试" README.md` → 无（过时引用已清除）

## 还剩什么
- 仅剩 1 个**可选**项：用真实/本地模型实跑一次 `AGENT=agent ./loop.sh`。需在线 API/endpoint，
  本离线环境无法执行；其内外层契约已由 test_e2e.sh(AGENT_CMD 路径) + agent.py 单测充分覆盖，
  不阻塞总任务完成。

## 下一步
- 无（必做项全部完成）。若有 API key/本地 vLLM，可补做上面那个可选实跑。
