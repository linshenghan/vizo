# Vizo 系统 CLI 兼容方案

> 目标：在 Claude Code 和 OpenAI Codex CLI 双底座上运行 Vizo，让没有 Claude Code 订阅的用户也能使用系统。
>
> 说明：文内保留的 `Opus` 术语主要用于描述历史兼容层和既有代码耦合点，不再代表当前主线产品名。

---

## 1. 当前对 Claude Code 的依赖点分析

系统与 Claude Code 有 **6 层深度耦合**：

| 层级  | 依赖点                                               | 耦合程度 | 替代难度       |
| --- | ------------------------------------------------- | ---- | ---------- |
| 1   | `claude -p` 子进程启动                                 | 核心   | 中          |
| 2   | `--output-format stream-json` NDJSON 流式协议         | 核心   | 高          |
| 3   | `--model` / `--fallback-model` / `--allowedTools` | 配置   | 低          |
| 4   | `--resume session_id` 会话恢复                        | 功能   | 中          |
| 5   | Hooks 系统（6+ 个 hook 文件）                            | 深度   | **极高**     |
| 6   | Claude Code 内置工具（Read/Write/Edit/Bash/Glob/Grep）  | 间接   | N/A（由后端提供） |

**最关键的耦合是 Hooks**。当前系统通过 Claude Code 的 `PreToolUse`/`PostToolUse`/`Stop` 等 hook 实现了：

- 确认请求检测（`pre_tool_dispatcher.py`）
- 暂停反馈注入（pause_feedback via Redis）
- 会话生命周期路由（`session_start.py`）
- 会话生命周期管理（`stop_guard.py`）
- 权限通知、完成通知等

这些是 Claude Code 特有的扩展机制，其他工具没有等价物。

---

## 2. Claude Code vs Codex CLI 协议对比

| 维度            | Claude Code                                        | Codex CLI                          | 兼容难度             |
| ------------- | -------------------------------------------------- | ---------------------------------- | ---------------- |
| **非交互命令**     | `claude -p - --output-format stream-json`          | `codex exec --json "prompt"`       | 低                |
| **Prompt 输入** | stdin 管道                                           | 命令行参数或 stdin                       | 低                |
| **流式输出**      | NDJSON（stdout）                                     | JSONL（stdout）                      | **中**（schema 不同） |
| **模型选择**      | `--model opus`                                     | `--model gpt-4o`                   | 低                |
| **工具权限**      | `--allowedTools Read,Write,Bash`                   | `--sandbox` + `--ask-for-approval` | 中                |
| **会话恢复**      | `--resume <session_id>`                            | `codex exec resume <session_id>`   | 低                |
| **Hooks 系统**  | 原生支持（`.claude/hooks/`）                             | **无**                              | **高**            |
| **费用追踪**      | `result` 事件含 `usage`/`modelUsage`/`total_cost_usd` | `turn.completed` 含 `usage`（无费用金额）  | 中                |
| **进度标记**      | `system.init` 含 `session_id`                       | `thread.started` 含 `thread_id`     | 低                |
| **MCP 支持**    | 原生支持                                               | 支持（`codex mcp`）                    | 需测试              |

### 事件流 Schema 对比

```
┌─ Claude Code ──────────────────────┐  ┌─ Codex CLI ────────────────────────┐
│ {"type":"system","subtype":"init",  │  │ {"type":"thread.started",           │
│  "session_id":"..."}               │  │  "thread_id":"..."}                │
│                                    │  │                                    │
│ {"type":"assistant",               │  │ {"type":"turn.started"}            │
│  "message":{"content":[            │  │                                    │
│    {"type":"text","text":"..."},    │  │ {"type":"item.started",            │
│    {"type":"tool_use",             │  │  "item":{"type":"command_execution"│
│     "name":"Bash","input":{}}      │  │   ,"command":"bash -lc ls"}}       │
│  ]}}                               │  │                                    │
│                                    │  │ {"type":"item.completed",          │
│ {"type":"result",                  │  │  "item":{"type":"agent_message",   │
│  "result":"...",                   │  │   "text":"..."}}                   │
│  "usage":{                         │  │                                    │
│    "input_tokens": 1234,           │  │ {"type":"turn.completed",          │
│    "output_tokens": 567            │  │  "usage":{"input_tokens":1234,     │
│  },                                │  │   "output_tokens":567}}            │
│  "total_cost_usd": 0.05}          │  │                                    │
└────────────────────────────────────┘  └────────────────────────────────────┘
```

---

## 3. 架构设计：双底座兼容层

核心思路：**在 `AgentRunner` 和 CLI 之间插入一个 Backend 抽象层**，屏蔽两种 CLI 的差异。

```
                        AgentRunner
                            │
                    ┌───────┴───────┐
                    │  AgentBackend │ (抽象接口)
                    │  (Protocol)   │
                    └───────┬───────┘
                   ┌────────┼────────┐
                   │                 │
          ClaudeCodeBackend    CodexBackend
          ┌────────────┐     ┌────────────┐
          │ 命令构建     │     │ 命令构建     │
          │ 事件解析     │     │ 事件解析     │
          │ Hook 集成    │     │ Wrapper 模拟 │
          │ 费用提取     │     │ 费用估算     │
          └──────┬─────┘     └──────┬─────┘
                 │                   │
           claude -p           codex exec
```

### 改动范围评估

| 文件                     | 改动类型     | 说明                                        |
| ---------------------- | -------- | ----------------------------------------- |
| `agent_runner.py`      | **重构**   | 提取 Backend 接口，`run()` 方法委托给 Backend       |
| `config.json`          | **新增字段** | `agent_backend: "claude_code" \| "codex"` |
| `lib/backends/`        | **新建**   | `base.py`、`claude_code.py`、`codex.py`     |
| `lib/event_adapter.py` | **新建**   | 统一事件格式转换器                                 |
| `hooks/`               | **无改动**  | Claude Code 模式下照常工作                       |
| `orchestrator.py`      | **极少改动** | 只需传递 backend 配置                           |

---

## 4. 关键设计点

### 4.1 统一事件格式（UnifiedEvent）

两种 CLI 的 JSONL 事件都转换为统一格式：

```python
@dataclass
class UnifiedEvent:
    kind: str          # "init" | "text" | "tool_use" | "tool_result" | "result" | "heartbeat"
    session_id: str    # claude session_id / codex thread_id
    content: str       # 文本内容
    tool_name: str     # 工具名（tool_use 时）
    tool_input: dict   # 工具参数
    usage: dict        # {"input_tokens": N, "output_tokens": N}
    cost_usd: float    # 费用（Codex 需要按 token 估算）
    raw: dict          # 原始事件（调试用）
```

### 4.2 Hooks 功能在 Codex 下的替代方案

这是最大挑战。Hooks 承担了 4 个关键功能：

| Hook 功能 | Claude Code 实现                       | Codex 替代方案                                             |
| ------- | ------------------------------------ | ------------------------------------------------------ |
| 确认请求检测  | `pre_tool_dispatcher.py`（每次工具调用时检查）  | **轮询 wrapper**：Backend 在事件循环中每 N 秒检查 `.opus/confirms/` |
| 暂停反馈注入  | Redis → hook → `system-reminder`     | **进程信号**：SIGCONT 后 Codex 的 `codex exec resume` 机制      |
| 会话注册/注销 | `session_start.py` / `stop_guard.py` | **Backend 生命周期回调**：在 `execute()` 前后调用                  |

关键洞察：**Hooks 本质上是"Agent 执行期间的周期性副作用"**。Claude Code 的 hook 恰好在每次工具调用时触发，但我们可以在 Backend 的事件循环中模拟：

```python
# CodexBackend 的事件循环中
async for line in process.stdout:
    event = parse_jsonl(line)
    unified = self.adapt_event(event)

    # 模拟 hook 功能：每次收到事件时执行
    if event["type"] in ("item.started", "item.completed"):
        await self._check_confirms()        # 替代 pre_tool_dispatcher
        await self._check_control_signals() # 替代暂停/终止检测

    yield unified
```

### 4.3 工具权限映射

```python
# Claude Code: --allowedTools Read,Write,Bash,Glob,Grep
# Codex:       --sandbox workspace-write --ask-for-approval never

TOOL_PERMISSION_MAP = {
    "readonly": {
        "claude_code": "--allowedTools Read,Glob,Grep",
        "codex": "--sandbox read-only --ask-for-approval never",
    },
    "full": {
        "claude_code": "",  # 默认全部
        "codex": "--full-auto --sandbox workspace-write",
    },
    "dangerous": {
        "claude_code": "",
        "codex": "--dangerously-bypass-approvals-and-sandbox",
    }
}
```

### 4.4 费用追踪

Codex 的 `turn.completed` 只给 token 数，不给费用。需要自己按定价计算：

```python
CODEX_PRICING = {  # 单位：美元/百万 token
    "o3":       {"input": 2.00,  "output": 8.00},
    "o4-mini":  {"input": 0.50,  "output": 1.50},
    "gpt-4o":   {"input": 2.50,  "output": 10.00},
    "gpt-5":    {"input": 5.00,  "output": 15.00},  # 待确认
}

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = CODEX_PRICING.get(model, {"input": 5.0, "output": 15.0})
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
```

---

## 5. 不可兼容的差异（需接受的限制）

| 特性                    | Claude Code                    | Codex         | 影响                            |
| --------------------- | ------------------------------ | ------------- | ----------------------------- |
| 暂停反馈注入                | Redis → hook → system-reminder | 无等价机制         | Codex 下暂停后只能 terminate+resume |
| `--allowedTools` 精细控制 | 按工具名白名单                        | 只有粗粒度 sandbox | Codex 下无法限制"只许读不许写"           |
| Fallback 模型           | `--fallback-model`             | 无             | Codex 下需要在 Backend 层实现重试切模型   |
| 流式渲染细节                | 事件粒度更细（partial messages）       | 事件粒度较粗        | Codex 下直播面板信息略少               |

---

## 6. 实施路线

### 第一阶段：Backend 抽象层（3-4 天）

1. 新建 `lib/backends/base.py` — 定义 `AgentBackend` 抽象接口
2. 新建 `lib/backends/claude_code.py` — 将现有 `_run_subprocess_streaming()` 中的 Claude Code 特定逻辑迁入
3. 新建 `lib/event_adapter.py` — `UnifiedEvent` 数据类 + Claude Code 事件适配器
4. 重构 `agent_runner.py` — `run()` 方法通过 Backend 执行，不直接构建 `claude` 命令

### 第二阶段：Codex Backend 实现（3-4 天）

1. 新建 `lib/backends/codex.py` — `codex exec --json` 命令构建 + 事件解析
2. 实现 Codex 事件 → `UnifiedEvent` 适配器
3. 实现轮询式 hook 替代（确认检测、信号轮询）
4. 费用估算逻辑

### 第三阶段：配置 & 测试（2-3 天）

1. `config.json` 新增 `agent_backend` 字段 + 按角色覆盖
2. 端到端测试：同一任务分别在 Claude Code / Codex 底座上运行
3. StreamRenderer 适配（处理 Codex 事件格式的差异）
4. 文档更新

### 工作量总计

**约 1-2 周**，改动集中在 `agent_runner.py` + 新建 `lib/backends/`，对 orchestrator 层几乎透明。

---

## 7. 配置示例

```json
{
  "agent_backend": "claude_code",
  "backend_config": {
    "claude_code": {
      "command": "claude",
      "default_model": "opus",
      "fallback_model": "haiku"
    },
    "codex": {
      "command": "codex",
      "default_model": "o3",
      "sandbox": "workspace-write",
      "approval_mode": "never"
    }
  },
  "backend_overrides": {
    "requirement_analyst": "codex",
    "pm": "codex",
    "backend_dev": "claude_code",
    "frontend_dev": "codex",
    "code_reviewer": "codex"
  }
}
```

支持混合模式：分析/设计类角色用 Codex，开发/部署类角色用 Claude Code（如果用户两个都有），或全部用同一个底座。

---

## 参考资料

- [Codex CLI 官方文档](https://developers.openai.com/codex/cli/)
- [Codex CLI 命令行参考](https://developers.openai.com/codex/cli/reference/)
- [Codex 非交互模式文档](https://developers.openai.com/codex/noninteractive)
- [Codex 配置参考](https://developers.openai.com/codex/config-reference/)
- [Codex GitHub 仓库](https://github.com/openai/codex)
