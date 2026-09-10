"""Connector（连接器）机制：MCP server 配置解析 + 生命周期 + 凭据安全。

安全核心：
- 凭据绝不落明文：配置里写 ${ENV_NAME} 占位符，真实值只从环境变量读。
- resolve_credential 在调用时才读环境变量；缺失返回 None，异常信息不含真实值。
"""

from __future__ import annotations

import os
from dataclasses import replace

from workguy.types import ConnectorSpec, MCPServerConfig

# 业界通行的 mcpServers.type 字段 -> 内部 transport 映射
_TRANSPORT_MAP = {
    "stdio": "stdio",
    "stdin": "stdio",
    "streamablehttp": "http",
    "streamable": "http",
    "http": "http",
    "sse": "sse",
}


class ConnectorRegistry:
    """Connector 注册表：配置解析 + 状态流转 + 凭据引用。"""

    def __init__(self) -> None:
        self._connectors: dict[str, ConnectorSpec] = {}

    # -- 配置解析 --------------------------------------------------------
    def load_config(self, data: dict) -> int:
        """解析 {"mcpServers": {...}} 格式。返回加载的连接器数。"""
        servers = (data or {}).get("mcpServers", {})
        count = 0
        for name, entry in servers.items():
            self._connectors[name] = self._build(name, entry or {})
            count += 1
        return count

    def _build(self, name: str, entry: dict) -> ConnectorSpec:
        t = str(entry.get("type", "stdio")).lower()
        transport = _TRANSPORT_MAP.get(t, "stdio")
        env: dict[str, str] = {}
        cred_env: str | None = None
        for k, v in (entry.get("env") or {}).items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                ref = v[2:-1]
                # 只保留占位符，绝不保存明文真实值
                env[k] = v
                if cred_env is None:
                    cred_env = ref
            else:
                env[k] = v
        mcp = MCPServerConfig(
            name=name,
            transport=transport,
            command=entry.get("command"),
            args=tuple(entry.get("args") or ()),
            env=env,
            url=entry.get("url"),
            headers=dict(entry.get("headers") or {}),
            disabled=bool(entry.get("disabled", False)),
            disabled_tools=tuple(entry.get("disabledTools") or ()),
        )
        return ConnectorSpec(
            name=name,
            mcp=mcp,
            bound=False,
            enabled=False,
            credential_env=cred_env,
        )

    # -- 查询 ------------------------------------------------------------
    def get(self, name: str) -> ConnectorSpec:
        if name not in self._connectors:
            raise KeyError(name)
        return self._connectors[name]

    def list_all(self) -> list[ConnectorSpec]:
        return list(self._connectors.values())

    def enabled(self) -> list[ConnectorSpec]:
        """bound 且 enabled 且未 disabled 的连接器。"""
        return [
            c
            for c in self._connectors.values()
            if c.bound and c.enabled and not c.mcp.disabled
        ]

    # -- 生命周期 --------------------------------------------------------
    def bind(self, name: str, credential_env: str) -> None:
        c = self.get(name)
        self._connectors[name] = replace(
            c, bound=True, enabled=True, credential_env=credential_env
        )

    def unbind(self, name: str) -> None:
        c = self.get(name)
        self._connectors[name] = replace(
            c, bound=False, enabled=False, credential_env=None
        )

    # -- 凭据安全 --------------------------------------------------------
    def resolve_credential(self, name: str) -> str | None:
        """从环境变量读取凭据真实值；缺失返回 None。

        真实值从不进入任何结构，异常信息也不包含真实值。
        """
        c = self.get(name)
        if c.credential_env is None:
            return None
        return os.environ.get(c.credential_env)
