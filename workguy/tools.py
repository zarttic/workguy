"""工具注册表。

职责：
- 持有所有工具规格（ToolSpec）与实现（ToolHandler）
- 实现懒加载：defer_loading=True 的工具不进默认上下文，需先 search 再 load
- execute 是运行时出口：未知工具、未装载的懒加载工具一律抛 ToolExecutionError
"""

from __future__ import annotations

from .types import ToolCall, ToolExecutionError, ToolHandler, ToolSpec


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec] | None = None) -> None:
        # 所有已知工具规格；非 defer 的默认视为已装载
        self._specs: dict[str, ToolSpec] = {}
        # 工具实现：(arguments, caller) -> str
        self._handlers: dict[str, ToolHandler] = {}
        # 已装载（可立即 execute）的工具名集合
        self._loaded: set[str] = set()

        if specs:
            for name, spec in specs.items():
                self._specs[name] = spec
                if not spec.defer_loading:
                    self._loaded.add(name)

    def register(
        self, name: str, handler: ToolHandler, defer_loading: bool = False
    ) -> None:
        """登记一个工具实现。defer_loading 的工具不会自动装载。"""
        self._specs[name] = ToolSpec(
            name=name, description=f"{name} tool", defer_loading=defer_loading
        )
        self._handlers[name] = handler
        if not defer_loading:
            self._loaded.add(name)

    def search(self, query: str) -> list[ToolSpec]:
        """按名字/描述模糊匹配（大小写不敏感）。搜索不要求工具已装载。"""
        q = query.strip().lower()
        if not q:
            return []
        hits: list[ToolSpec] = []
        for spec in self._specs.values():
            haystack = f"{spec.name} {spec.description}".lower()
            if q in haystack:
                hits.append(spec)
        return hits

    def is_loaded(self, name: str) -> bool:
        """懒加载工具是否已装载。未知工具视为未装载。"""
        return name in self._loaded

    def load(self, name: str) -> None:
        """显式装载一个懒加载工具。未知工具抛 ToolExecutionError。"""
        if name not in self._specs:
            raise ToolExecutionError(f"unknown tool: {name}")
        self._loaded.add(name)

    def default_context_tools(self) -> list[str]:
        """所有非 defer 的工具名 —— 这些是进默认上下文的。"""
        return [n for n, s in self._specs.items() if not s.defer_loading]

    def execute(self, call: ToolCall, caller: str) -> str:
        """执行工具调用。

        - 未知工具 → ToolExecutionError
        - defer_loading 且未 load() → ToolExecutionError（懒加载拦截）
        - 否则调用底层 handler
        """
        name = call.name
        spec = self._specs.get(name)
        if spec is None:
            raise ToolExecutionError(f"unknown tool: {name}")
        if spec.defer_loading and name not in self._loaded:
            raise ToolExecutionError(f"lazy tool not loaded: {name} (call load first)")
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolExecutionError(f"tool has no handler: {name}")
        return handler(call.arguments, caller)
