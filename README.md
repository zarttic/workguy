# WorkGuy —— 可运行的 Agent 内核骨架

**WorkGuy** 是一个从零实现的 Agent 内核，Python 3.13 + 标准库，零第三方依赖。

- 包名：`workguy`（Python 包规范，小写）
- 产品名：`WorkGuy`
- 当前版本：**0.4.0**（176 项测试，零第三方依赖）—— 已用真实 LongCat API 端到端验证
- 命名可用性：PyPI 上 `workguy` 与 `work-guy` 均未被占用（2026-09-10 查询）

## 这是什么

它的**设计思想**来自对 WorkBuddy Desktop 5.5.4 的逆向分析——但**不含任何原实现代码**，
每一行都是照着机制重写出来的。取的是设计意图，不是代码。

原始分析见同目录的 `workbuddy-项目拆解.md` 与 `workbuddy-核心机制解析.md`。

## 跑起来

```bash
# 全量测试（59 项）
python -m unittest discover -s tests -v

# 演示五个机制
python demo.py
```

## 五大机制

| 机制 | 文件 | 原型出处 |
|---|---|---|
| 六级阈值上下文压缩 | `workguy/context.py` | `tokenUsageThresholds` |
| 代理权限金字塔 | `workguy/agents.py` | 19 个代理的 `tools` 白名单 |
| 最小权限边界 | `workguy/agents.py` | 13 个代理白名单为空 |
| 哈希链审计 | `workguy/audit.py` | `prevHash → hash` |
| lite 模型成本路由 | `workguy/router.py` | `models: ['lite']` |

### 1. 上下文压缩：用最便宜的手段，在最早的时刻动手

```
ratio 0.15 → SUMMARIZE   摘要最便宜，所以最早介入
ratio 0.40 → COMPACT
ratio 0.60 → COMPACT     warning 只是告警标记，动作等级不回落
ratio 0.70 → COMPACT
ratio 0.90 → EMERGENCY
```

不是等上下文爆了再救火。deepseek 系列放宽到 0.50（实测值）。

### 2. 权限金字塔：19 个代理，13 个没有手

```
cli                34 个工具   ← 金字塔顶
general-purpose    24 个
Plan               18 个
Explore            16 个   (lite)
statusline-setup    5 个
pulse               2 个
─────────────────────────────
compact 等 13 个     0 个   ← 纯推理，白名单为空
```

### 3. 最小权限边界（最重要）

**纯推理代理即便被 LLM 要求调用危险工具，也会被运行时拦截，底层 handler 一次都不会被触达。**

这是运行时强制，不是靠提示词约束 LLM 别乱调。测试 `test_pure_reasoner_cannot_escape_sandbox`
里让 LLM 明确请求 `Bash(rm -rf /)`，结果：

```
denied_count = 1
executed     = []
bash_called  = 0     ← 关键
```

### 4. 哈希链审计

每条记录携带前一条的哈希，任何一条被篡改都会导致链断裂。
`state` 里有 `sequence` / `lastHash`，可导出 JSONL 并回读校验。

### 5. 成本路由

实测倍率区间 0.06x ~ 5.00x，差 80 倍。高频低难度任务走 lite：
测试里 13 个纯推理代理全走 lite 的花费，只有全走 default 的 **2.5%**。

## 接真实 LLM

用网关挂 Provider（比裸用 `LLMClient` 多了方言翻译与 fallback）：

```python
from workguy import AgentKernel, Gateway, LongCatProvider, Store, SkillRegistry

gateway = Gateway({"longcat": LongCatProvider(api_key="你的 key")})
kernel = AgentKernel(gateway=gateway, provider_chain=["longcat"], store=Store("wb.db"))
result = kernel.run("cli", "用一句话说明哈希链审计")
```

开箱可运行的完整示例见 `examples/real_llm.py`（**key 只从环境变量读**）：

```bash
export LONGCAT_API_KEY=ak_xxxxxxxx
python examples/real_llm.py "你的问题"
```

实测输出：

```
回复    : 哈希链审计通过将每个数据块的哈希值嵌入下一个数据块形成链式结构...
provider: longcat  |  模型: LongCat-2.0  档位: default
credits : 1.44     命中技能: ['hash-audit']
会话落盘: True     审计条数: 3  链完好: True
```

### LongCat 实测要点（2026-09-10 真机验证）

| 项 | 结论 |
|---|---|
| OpenAI 端点 | `https://api.longcat.chat/openai/v1` |
| Anthropic 端点 | `https://api.longcat.chat/anthropic/v1/messages` |
| 鉴权 | **两种端点都必须用 `Authorization: Bearer`**——Anthropic 端不接受标准 `x-api-key`，这是它偏离规范之处 |
| 模型名 | `LongCat-2.0`，**大小写敏感**（小写会报 Unsupported model） |
| 上下文 | 1M 输入 / 128K 输出 |
| 限流 | 429 + 响应体 `retry_after`，Provider 已实现指数退避重试 |

### 两个容易踩的坑

**1. `/v1/models` 不校验凭据真实性。**
它只检查 `ak_` 前缀和长度——**随机生成的 key 也能返回 200**，截短一位才会 401。
不要用这个端点判断 key 是否有效，必须打 `chat/completions` 或 `messages`。

**2. LongCat-2.0 是推理模型，会先产出思维链。**
响应带 `reasoning_content`（OpenAI 格式）或 `thinking` 块（Anthropic 格式），已解析进 `ChatResponse.reasoning`。
**但 `max_tokens` 给小了会被思维链吃满，`content` 返回空字符串**——给推理模型留足预算。

## 产品化三件套

内核之上补了三块，让它从"能跑"走向"能用"。设计前先取证了本机 WorkBuddy 5.5.4 的真实实现，
也对照了 MCP / LiteLLM 等业界标准。

### 多模型网关 `providers.py`

问题不是"多接几家"，而是**各家协议方言不同**：

| | OpenAI | Anthropic |
|---|---|---|
| system | 放进 messages 数组 | **顶层独立字段** |
| 工具调用 | `tool_calls[].function.arguments`（**JSON 字符串**） | `content[].tool_use.input`（**对象**） |
| 用量 | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` |
| 鉴权头 | `Authorization: Bearer` | `anthropic-version` + Bearer |

方言翻译集中在 `_translate_request` / `_translate_response`，可单独测试。

```python
gw = Gateway()
gw.register("openai", OpenAIProvider(api_key="..."))
gw.register("longcat", LongCatProvider(api_key="..."))
resp = gw.complete_with_fallback(["openai", "longcat"], req)  # 前一个挂了自动切下一个
```

**LongCat 的坑（实测）**：它的 Anthropic 端点**不接受标准 `x-api-key`**，必须用 Bearer。

### 持久化 `store.py`

6 张表：sessions / messages / audit_chain / usage / extension_state / schema_meta。

- WAL 模式 + `PRAGMA user_version` 幂等迁移（零依赖替代 Alembic）
- **审计链落盘有防断链校验**：`append_audit` 在单事务里校验新记录 `prev_hash` 必须等于库尾 `hash`，不符即回滚——并发写入产生断链会被直接拒掉
- 与内存版 `AuditLog` 可通过 JSONL 互操作

### 扩展机制 `skills.py` / `connectors.py` / `mcp.py`

**Skill 用渐进式加载，这是整套机制的核心：**

| 层 | 内容 | 何时进内存 |
|---|---|---|
| L0 | `{name, description, path}` | 注册时常驻（几 KB/技能） |
| L1 | SKILL.md 正文 | **命中后才读盘** |
| L2 | references/ scripts/ assets/ | 按需单独读 |

理由：description 是模型判断"要不要用"的**唯一匹配面**，所以必须常驻；但正文资源全塞进上下文会爆。
测试里有专门的 spy 计数证明 `match()` 之后 L1 读取次数为 **0**。

**Connector 的凭据安全**（取证自 WorkBuddy 的真实做法）：

配置文件里只写 `${ENV_NAME}` 占位符，真实值运行时才从环境变量读。异常信息里不含真实值。

**MCP 客户端**零依赖实现 JSON-RPC 2.0 over stdio：reader 线程 + queue 实现超时保护，
`close()` 三级兜底（terminate → wait → kill）防僵尸进程。测试跑的是**真实子进程**，
10 项用例在 Windows 上实测通过。

### 内核 vs 产品化：取证对照

| 我们怎么做 | WorkBuddy 怎么做 | 结论 |
|---|---|---|
| 环境变量 `${ENV}` 占位 |连接器密钥同为 `${ENV}` 占位，静态落盘加密 | 做法一致 |
| L0/L1/L2 渐进加载 | `wb-finance-skill` 正文要求"先读 reference" | 做法一致 |
| MCPServerConfig 对齐 `mcpServers` | 连接器配置即 `mcpServers` 字典 | 对齐业界标准 |

## 审视与加固（v0.3.0）

v0.2.0 之后做了一轮**对抗性审视**：派三路独立审查（安全 / 架构 / 测试质量），
明确要求它们挑刺而不是确认。发现的问题与修法：

| 问题 | 严重度 | 修法 |
|---|---|---|
| **MCP 子进程可执行任意命令**——配置里的 `command` 直接进 `Popen`，无校验 | 严重 | 新增 `sandbox.py`：basename 白名单 + LOLBin 拒绝 + 危险参数模式拦截，`connect()` 默认强制校验 |
| **内核与产品化层完全脱钩**——Store / Gateway / Skill / MCP 一个都没接 | 严重 | 重构 `kernel.py`，五条接线全部打通 |
| **审计链可被整体重写或尾部截断**——genesis 硬编码，无外部锚点 | 中等 | 新增 `AuditAnchor`（条数 + 末 hash），`verify_chain(anchor)` 可检出截断与重写 |
| **`MCPServerConfig` 的 repr 泄漏凭据**——dataclass 自动 repr 会打印 `env` | 中等 | 重写 `__repr__`，遮蔽 `env` / `headers` |
| **两套 LLM 抽象互不相通**——`LongCatLLM` 与 `LongCatProvider` 近乎重复 | 中等 | 给 `Provider` 加 `chat()` 适配，使其满足 `LLMClient` 协议 |
| **哈希分隔符歧义**——`\|` 拼接可构造哈希碰撞 | 轻微 | 改长度前缀编码，`None` 与 `""` 区分 |

### 一个被明确记录的边界

`ToolRegistry.execute()` **本身不做权限校验**——权限由 kernel 前置把关。
这是职责分离的设计权衡，不是漏洞，但**必须写测试锁住**，
否则将来有人会误以为"直接调 execute 也安全"。

- `test_tool_registry_execute_does_not_enforce_permission` 记录这个事实
- `test_every_kernel_path_passes_permission_check` 确保经 kernel 的每条路径都过校验

### 整合后新增的安全约束

MCP 挂载的工具**只对 `DYNAMIC_TOOL_AGENTS`（cli / general-purpose）开放**。
这是刻意收紧——否则"挂个 MCP server"就能把整座权限金字塔打穿，
13 个零工具代理会突然获得外部能力。

```python
DYNAMIC_TOOL_AGENTS = frozenset({"cli", "general-purpose"})
```

`test_pure_reasoner_never_gets_dynamic_tools` 遍历全部 13 个纯推理代理验证这一点。

### 接线后的效果

```python
kernel = AgentKernel(
    gateway=Gateway({"longcat": LongCatProvider()}),  # 多模型 + fallback
    store=Store("wb.db"),                            # 会话/审计/用量落盘
    skills=SkillRegistry(),                          # L0 技能匹配
    mcp=mcp_registry,                                # 外部工具挂载
)
r = kernel.run("cli", "帮我分析这份财报")
# → 已完成技能匹配、模型路由、权限校验、工具执行、审计落盘、用量记账
```

## 目录

```
workguy/                        176 项测试，零第三方依赖
  内核
  types.py       数据类型（契约）
  config.py      阈值 / 模型 / 代理 / 工具（对标 product.json）
  context.py     上下文阈值监控
  agents.py      代理注册表 + 权限校验
  tools.py       工具注册表 + 懒加载
  audit.py       哈希链审计（内存版）
  router.py      模型路由 + 计费
  llm.py         MockLLM / LongCatLLM
  kernel.py      ReAct 执行循环
  产品化
  providers.py   多模型网关 + 方言互译 + fallback
  store.py       SQLite 持久化 + 审计链落盘
  skills.py      Skill 渐进式加载（L0/L1/L2）
  connectors.py  连接器 + ${ENV} 凭据占位
  mcp.py         MCP 客户端（JSON-RPC 2.0 over stdio）
  sandbox.py     命令准入校验（白名单 + LOLBin 拒绝 + 注入模式拦截）
tests/           含 fixtures/fake_mcp_server.py 与 test_integration.py（接线验收）
examples/        real_llm.py —— 真实 LLM 端到端示例（key 走环境变量）
demo.py          离线演示脚本
```
