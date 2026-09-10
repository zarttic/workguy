"""Agent 执行循环（ReAct）—— 内核与产品化层的接合点。

一次 run 的完整链路：

    技能匹配(L0) → 会话建档 → 上下文体检 → 模型路由 → LLM 推理
        ↑                                                 ↓
        └─── 结果回填 + 权限校验 + 审计 + 计费 + 落盘 ←────┘

**安全边界的核心断言**：纯推理代理（13 个白名单为空的代理）即使 LLM 返回了
tool_call，也会被拦截，工具根本不会被触达。这是运行时强制，不是提示词约束。

**动态工具的额外收紧**：MCP 挂载进来的工具只对 `DYNAMIC_TOOL_AGENTS` 里的
全能/通用代理开放，纯推理代理依旧零工具——防止"挂个 MCP 就把权限金字塔打穿"。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .agents import AgentRegistry
from .audit import AuditLog
from .config import AGENTS, TOOLS
from .context import ContextMonitor
from .llm import LLMClient
from .logging import get_logger
from .providers import Gateway
from .router import ModelRouter
from .tools import ToolRegistry
from .types import (
    AgentSpec,
    ChatRequest,
    ChatResponse,
    ContextAction,
    Message,
    PermissionDeniedError,
    RouterDecision,
    SkillSpec,
    ToolCall,
    ToolExecutionError,
)

_log = get_logger(__name__)

DEFAULT_MAX_TOKENS = 8_000
DEFAULT_MAX_ITERATIONS = 8
DEFAULT_SKILL_TOP_K = 3

# 默认 provider 回退顺序（Gateway 模式下生效）
DEFAULT_PROVIDER_CHAIN: tuple[str, ...] = ("longcat", "openai", "anthropic")

# 允许使用动态挂载工具（MCP）的代理。刻意收紧到最小集合。
DYNAMIC_TOOL_AGENTS: frozenset[str] = frozenset({"cli", "general-purpose"})

# 技能 description 注入上下文时的截断长度（L0 只做提示，不灌全文）
SKILL_DESC_LIMIT = 200


@dataclass
class RunResult:
    content: str = ""
    iterations: int = 0
    credits: float = 0.0
    model: str = ""  # 实际调用的模型（来自响应，如 LongCat-2.0）
    model_tier: str = ""  # 路由选出的档位（如 default），决定计费倍率
    session_id: str = ""
    denied: list[dict] = field(default_factory=list)
    executed: list[dict] = field(default_factory=list)
    context_actions: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    provider: str = ""
    persisted: bool = False
    store_errors: list[str] = field(default_factory=list)
    audit: AuditLog | None = None

    @property
    def denied_count(self) -> int:
        return len(self.denied)


class AgentKernel:
    def __init__(
        self,
        llm: LLMClient | None = None,
        agents: AgentRegistry | None = None,
        tools: ToolRegistry | None = None,
        router: ModelRouter | None = None,
        audit: AuditLog | None = None,
        *,
        gateway: Gateway | None = None,
        provider_chain: tuple[str, ...] | list[str] | None = None,
        store=None,
        skills=None,
        mcp=None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        skill_top_k: int = DEFAULT_SKILL_TOP_K,
    ) -> None:
        if llm is None and gateway is None:
            raise ValueError("必须提供 llm 或 gateway 之一作为 LLM 后端")

        self.llm = llm
        self.gateway = gateway
        self.provider_chain = tuple(provider_chain or DEFAULT_PROVIDER_CHAIN)
        self.agents = agents or AgentRegistry(AGENTS)
        self.tools = tools or ToolRegistry(TOOLS)
        self.router = router or ModelRouter()
        self.audit = audit or AuditLog()
        self.store = store
        self.skills = skills
        self.mcp = mcp
        self.monitor = ContextMonitor(max_tokens=max_tokens)
        self.max_iterations = max_iterations
        self.skill_top_k = skill_top_k

        # MCP 挂载进来的工具名（不进 AgentSpec 白名单，单独管理）
        self._dynamic_tools: set[str] = set()
        if mcp is not None:
            self._mount_mcp(mcp)

    # -- 扩展接线 ----------------------------------------------------------
    def _mount_mcp(self, mcp) -> None:
        """把 MCP 工具挂进工具表，并记录哪些是动态工具。

        用前/后快照求差集，避免依赖 ToolRegistry 的内部结构。
        """
        before = set(self.tools.default_context_tools())
        try:
            mounted = mcp.mount_into(self.tools)
        except Exception as e:  # noqa: BLE001
            _log.warning("MCP 挂载失败：%s", e)
            return
        after = set(self.tools.default_context_tools())
        self._dynamic_tools |= after - before
        if mounted:
            _log.info("已挂载 %d 个 MCP 工具：%s", mounted, sorted(self._dynamic_tools))

    # -- 审计双写 ----------------------------------------------------------
    def _record(
        self,
        agent: str,
        category: str,
        event_type: str,
        decision: str,
        detail: dict,
        result: RunResult | None = None,
    ) -> None:
        """写内存哈希链；若接了 Store 则同步落盘。

        落盘失败**不中断执行**（审计不可用不该让任务崩），但要记进
        result.store_errors 并打日志——静默失败比崩溃更危险。
        """
        rec = self.audit.append(
            agent=agent,
            category=category,
            event_type=event_type,
            decision=decision,
            detail=detail,
        )
        if self.store is not None:
            try:
                self.store.append_audit(rec)
            except Exception as e:  # noqa: BLE001
                _log.warning("审计落盘失败：%s", e)
                if result is not None:
                    result.store_errors.append(f"audit:{e}")

    # -- 技能匹配（渐进加载 L0） --------------------------------------------
    def _match_skills(self, user_input: str) -> list[SkillSpec]:
        """只做 L0 匹配：拿回元数据，**不读正文**。

        正文（L1）要等真正选中该技能时才读盘——这是渐进加载的全部意义。
        """
        if self.skills is None or not user_input.strip():
            return []
        try:
            return list(self.skills.match(user_input))[: self.skill_top_k]
        except Exception as e:  # noqa: BLE001
            _log.warning("技能匹配失败：%s", e)
            return []

    @staticmethod
    def _skills_hint(specs: list[SkillSpec]) -> Message | None:
        """把候选技能以 L0 形式（name + 截断的 description）注入上下文。"""
        if not specs:
            return None
        lines = ["[可用技能] 以下技能与当前任务相关（仅给出元数据，需要时再加载正文）："]
        for s in specs:
            desc = s.description[:SKILL_DESC_LIMIT]
            lines.append(f"- {s.name}: {desc}")
        return Message(role="system", content="\n".join(lines))

    # -- 工具权限（静态白名单 + 动态工具） ----------------------------------
    def _may_use(self, agent: AgentSpec, tool_name: str) -> bool:
        if tool_name in agent.tools:
            return True
        # 动态工具只对全能/通用代理开放；纯推理代理永远拿不到
        return tool_name in self._dynamic_tools and agent.name in DYNAMIC_TOOL_AGENTS

    def _assert_may_use(self, agent: AgentSpec, call: ToolCall) -> None:
        if self._may_use(agent, call.name):
            return
        # 复用 AgentRegistry 的报错文案，保持提示一致
        self.agents.assert_may_call(agent.name, call)

    def _effective_tools(self, agent: AgentSpec) -> list[str]:
        """该代理实际可用的工具：静态白名单 + （有资格的）动态工具，
        再过滤掉尚未装载的懒加载工具。"""
        names = list(agent.tools)
        if agent.name in DYNAMIC_TOOL_AGENTS:
            names.extend(sorted(self._dynamic_tools))
        return [t for t in names if self.tools.is_loaded(t)]

    # -- 上下文压缩 -------------------------------------------------------
    def _apply_context_action(
        self, messages: list[Message], action: ContextAction
    ) -> list[Message]:
        """骨架版压缩策略：保留首条 + 最近两条，中间折叠成一条摘要占位。

        真实实现会调摘要代理生成文本（对应 config 里的 summaryGenerator）。
        """
        if action == ContextAction.NONE or len(messages) <= 3:
            return messages
        head, tail = messages[0], messages[-2:]
        dropped = len(messages) - 3
        placeholder = Message(
            role="system",
            content=f"[上下文已压缩：省略 {dropped} 条历史消息，触发动作={action.value}]",
        )
        return [head, placeholder, *tail]

    # -- 工具装载（懒加载） ------------------------------------------------
    def _ensure_loaded(self, name: str) -> None:
        spec = TOOLS.get(name)
        # 动态工具（MCP）不在静态 TOOLS 表里，由 MCPRegistry 挂载时已装载
        if spec is not None and spec.defer_loading and not self.tools.is_loaded(name):
            self.tools.load(name)

    # -- LLM 调用：统一后端 ------------------------------------------------
    def _invoke_llm(
        self, messages: list[Message], model_id: str, available: list[str]
    ) -> ChatResponse:
        """统一两种后端：Gateway（多 provider + fallback）或单一 llm。"""
        if self.gateway is not None:
            usable = [p for p in self.provider_chain if p in self.gateway.available()]
            if not usable:
                usable = self.gateway.available()
            if not usable:
                raise RuntimeError(
                    "Gateway 没有可用 provider：请检查各 provider 的 api_key 是否已配置"
                )
            return self.gateway.complete_with_fallback(
                usable,
                ChatRequest(messages=messages, tools=available),
            )

        resp = self.llm.chat(messages, model_id, available)
        return ChatResponse(
            content=resp.content,
            tool_calls=resp.tool_calls,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            model=resp.model,
            provider=getattr(self.llm, "name", type(self.llm).__name__),
        )

    # -- 单次工具调用（含权限边界） ----------------------------------------
    def _dispatch(self, agent: AgentSpec, call: ToolCall, result: RunResult) -> str:
        try:
            # 权限校验必须先于任何执行动作：这是安全边界，顺序不能反
            self._assert_may_use(agent, call)
        except PermissionDeniedError as e:
            result.denied.append({"agent": agent.name, "tool": call.name, "reason": str(e)})
            self._record(
                agent.name, "tool_call", "permission_check", "deny",
                {"tool": call.name, "reason": str(e)}, result,
            )
            return f"[拒绝] {e}"

        self._ensure_loaded(call.name)
        try:
            output = self.tools.execute(call, caller=agent.name)
        except ToolExecutionError as e:
            self._record(
                agent.name, "tool_call", "execution", "error",
                {"tool": call.name, "error": str(e)}, result,
            )
            return f"[执行失败] {e}"

        result.executed.append({"agent": agent.name, "tool": call.name})
        self._record(
            agent.name, "tool_call", "execution", "executed",
            {"tool": call.name, "output_preview": output[:200]}, result,
        )
        return output

    # -- 主循环 ------------------------------------------------------------
    def run(
        self, agent_name: str, user_input: str, session_id: str | None = None
    ) -> RunResult:
        agent = self.agents.get(agent_name)
        result = RunResult(audit=self.audit)

        # 1. 会话标识
        sid = session_id or f"wb_{uuid.uuid4().hex[:12]}"
        result.session_id = sid

        # 2. 技能匹配（只读 L0 元数据，不读正文）
        matched = self._match_skills(user_input)
        result.matched_skills = [s.name for s in matched]
        if matched:
            self._record(
                agent_name, "skill", "match", "executed",
                {"skills": result.matched_skills}, result,
            )

        # 3. 会话建档
        if self.store is not None:
            try:
                self.store.create_session(sid, agent=agent_name)
                result.persisted = True
            except Exception as e:  # noqa: BLE001
                _log.warning("会话建档失败：%s", e)
                result.store_errors.append(f"session:{e}")

        self._record(
            agent_name, "run", "start", "executed",
            {"input_preview": user_input[:200], "is_pure_reasoner": agent.is_pure_reasoner},
            result,
        )

        # 4. 组装初始消息：技能提示(L0) + 用户输入
        messages: list[Message] = []
        hint = self._skills_hint(matched)
        if hint is not None:
            messages.append(hint)
        messages.append(Message(role="user", content=user_input))

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration

            action = self.monitor.check_before_message(messages)
            if action != ContextAction.NONE:
                result.context_actions.append(action.value)
                messages = self._apply_context_action(messages, action)

            decision: RouterDecision = self.router.route(
                agent, requires_tools=bool(agent.tools)
            )
            available = self._effective_tools(agent)

            response = self._invoke_llm(messages, decision.model.id, available)
            cost = self.router.charge(
                decision.model.id, response.input_tokens, response.output_tokens
            )
            result.credits += cost
            # model 记实际调用的模型，model_tier 记路由档位（决定倍率）。
            # 两者可能不同：档位是配置概念（default=2.0x），模型是服务端真实标识。
            result.model = response.model or decision.model.id
            result.model_tier = decision.model.id
            result.provider = response.provider

            if self.store is not None:
                try:
                    self.store.record_usage(
                        sid, response.model or decision.model.id,
                        response.input_tokens, response.output_tokens, cost,
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("用量落盘失败：%s", e)
                    result.store_errors.append(f"usage:{e}")

            if not response.tool_calls:
                result.content = response.content
                break

            messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                output = self._dispatch(agent, call, result)
                messages.append(
                    Message(role="tool", content=output, tool_call_id=call.id)
                )
        else:
            result.content = result.content or "[达到最大迭代次数]"

        # 5. 消息落盘
        if self.store is not None:
            for m in messages:
                try:
                    self.store.save_message(sid, m)
                except Exception as e:  # noqa: BLE001
                    _log.warning("消息落盘失败：%s", e)
                    result.store_errors.append(f"message:{e}")
                    break  # 连续失败没必要刷屏

        self._record(
            agent_name, "run", "end", "executed",
            {
                "iterations": result.iterations,
                "credits": round(result.credits, 6),
                "denied": result.denied_count,
                "executed": len(result.executed),
                "skills": result.matched_skills,
            },
            result,
        )
        return result
