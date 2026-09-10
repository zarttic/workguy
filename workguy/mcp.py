"""MCP（Model Context Protocol）客户端，stdio 传输，零第三方依赖。

只使用标准库：subprocess / json / threading / queue。对标 JSON-RPC 2.0。

设计要点：
- 每个 MCPClient 管理一个 MCP server 子进程（stdio）。
- 读 stdout 由独立 reader 线程完成，消息入队；请求方按 timeout 从队列取响应，
  从而把「阻塞读」转成「带超时的取队列」，避免永久卡死。
- close() 会 terminate 子进程并 join reader 线程，配合 __del__ / 上下文管理器
  保证不泄漏僵尸进程。
- 仅支持 transport="stdio"；其他传输方式抛 MCPError。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

from .sandbox import CommandNotAllowedError, validate_command
from .types import MCPServerConfig

__all__ = ["MCPError", "MCPClient", "MCPRegistry", "CommandNotAllowedError"]

# Windows 下用 CREATE_NO_WINDOW 启动子进程，避免弹出控制台窗口。
# 同时 CREATE_NEW_PROCESS_GROUP 让 terminate 更稳妥（TerminateProcess 本身即可）。
_EXTRA_FLAGS = 0
if sys.platform == "win32":
    _EXTRA_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )


class MCPError(Exception):
    """MCP 协议层的错误：连接失败、超时、响应异常、未知工具等。"""


class MCPClient:
    """单个 MCP server 的客户端（stdio 传输）。"""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        allow_unvalidated: bool = False,
        unsafe_reason: str | None = None,
    ) -> None:
        """构造 MCP server 客户端。

        参数：
            config: MCP server 配置。
            allow_unvalidated: 是否跳过命令准入校验。**默认 False（安全优先）**。
                设为 True 即绕过 ``workguy/sandbox.validate_command`` 的防护，
                意味着配置里的 ``command``/``args`` 将**不经检查直接交给
                subprocess.Popen**，存在任意代码执行风险。仅在完全信任配置来源
                （如本地锁定、签名校验通过的技能包）且确无他法时才可开启。
            unsafe_reason: 当 ``allow_unvalidated=True`` 时**必须**提供，记录绕过
                校验的原因（用于审计）。缺失则抛 ``ValueError``。
        """
        if config.transport != "stdio":
            raise MCPError(f"unsupported transport: {config.transport!r} (only 'stdio')")
        if allow_unvalidated and not unsafe_reason:
            raise ValueError(
                "allow_unvalidated=True 时必须提供 unsafe_reason 说明绕过校验的原因"
            )
        self.config = config
        self._allow_unvalidated = allow_unvalidated
        self._unsafe_reason = unsafe_reason
        self._proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_id = 1
        self._initialized = False
        self._tools: list[dict[str, Any]] = []
        self._tool_names: list[str] = []

    # -- 进程生命周期 -------------------------------------------------------

    def connect(self) -> None:
        """启动子进程并启动 reader 线程。"""
        if self._proc is not None:
            raise MCPError("already connected")

        # 安全：在启动进程前做命令准入校验，阻断任意代码执行。
        # allow_unvalidated=True 时跳过（逃生口，需配合 unsafe_reason）。
        if not self._allow_unvalidated:
            validate_command(self.config.command, self.config.args)

        cmd: list[str] = []
        if self.config.command:
            cmd.append(self.config.command)
        cmd.extend(self.config.args)
        if not cmd:
            raise MCPError("MCP config has neither command nor args")

        env = dict(os.environ)
        env.update(self.config.env)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # 行缓冲，配合显式 flush
                creationflags=_EXTRA_FLAGS,
            )
        except OSError as exc:
            raise MCPError(f"failed to start MCP server {self.config.name!r}: {exc}") from exc

        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        """终止子进程并结束 reader 线程，保证无僵尸进程泄漏。"""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
        self._proc = None
        self._reader_thread = None

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # -- 底层收发 -----------------------------------------------------------

    def _reader(self) -> None:
        """持续读 stdout，把每条 JSON 消息放入响应队列。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._response_queue.put(msg)
        except (ValueError, OSError):
            # 进程被关闭/终止时这里会抛异常，静默退出
            pass

    def _send(self, obj: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError("not connected")
        try:
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"failed to write to MCP server: {exc}") from exc

    def _request(self, method: str, params: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        """发送请求并按 timeout 等待响应。"""
        if timeout_ms is None:
            timeout_ms = self.config.timeout_ms
        msg_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            resp = self._response_queue.get(timeout=timeout_ms / 1000.0)
        except queue.Empty as exc:
            raise MCPError(
                f"timeout after {timeout_ms}ms waiting for response to {method!r}"
            ) from exc
        if "error" in resp:
            raise MCPError(f"{method} failed: {resp['error']}")
        return resp.get("result", {})

    # -- 协议步骤 -----------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        """完成握手：initialize -> 收到响应 -> 发 notifications/initialized。"""
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError("not connected")
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "workguy", "version": "0.1.0"},
            },
        )
        # 初始化完成通知：无 id，对端不回包，因此不等待
        self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """调用 tools/list，缓存并返回工具清单。"""
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        self._tools = tools
        self._tool_names = [t.get("name", "") for t in tools]
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用 tools/call，返回拼接后的文本。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)

    @property
    def tool_names(self) -> list[str]:
        """list_tools() 之后可用的工具名列表（已缓存）。"""
        return list(self._tool_names)


class MCPRegistry:
    """管理多个 MCP server。"""

    def __init__(self) -> None:
        self._configs: list[MCPServerConfig] = []
        self._clients: dict[str, MCPClient] = {}
        # 工具名 -> 负责该工具的 MCPClient（先注册者优先）
        self._routing: dict[str, MCPClient] = {}

    def add(self, config: MCPServerConfig) -> None:
        """登记一个 MCP server 配置（暂不连接）。"""
        self._configs.append(config)

    def connect_all(self) -> dict[str, int]:
        """连接所有未 disabled 的 server，返回 {server 名: 本次挂载的工具数}。

        - config.disabled 的 server 直接跳过，不启动进程。
        - config.disabled_tools 中的工具不进入路由表、不被挂载。
        - 工具名冲突策略：**先注册者优先**。多个 server 提供同名工具时，
          保留最先 connect 成功的那个；后续同名条目被忽略。
        """
        counts: dict[str, int] = {}
        for config in self._configs:
            if config.disabled:
                continue
            client = MCPClient(config)
            client.connect()
            try:
                client.initialize()
                tools = client.list_tools()
            except Exception:
                client.close()
                raise

            self._clients[config.name] = client
            disabled = set(config.disabled_tools)
            mounted = 0
            for tool in tools:
                tname = tool.get("name", "")
                if not tname or tname in disabled:
                    continue
                if tname in self._routing:
                    # 同名：先注册者优先，跳过
                    continue
                self._routing[tname] = client
                mounted += 1
            counts[config.name] = mounted
        return counts

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """按工具名路由到对应 server 并调用。

        路由按 connect_all 时建立的路由表解析；冲突工具只会命中「先注册者」。
        未知工具抛 MCPError。
        """
        client = self._routing.get(name)
        if client is None:
            raise MCPError(f"no MCP server provides tool: {name!r}")
        return client.call_tool(name, arguments)

    def mount_into(self, registry: Any) -> int:
        """把 MCP 工具挂进既有 ToolRegistry，返回挂载数。

        每个进入路由表的工具（已排除 disabled_tools 与冲突同名项）注册一个
        handler，调用时经 call_tool 路由回对应 server。返回注册成功的工具数。
        """
        count = 0
        for tool_name in self._routing:
            def _handler(arguments: dict[str, Any], caller: str, _n: str = tool_name) -> str:
                return self.call_tool(_n, arguments)
            registry.register(tool_name, _handler)
            count += 1
        return count

    @property
    def tool_names(self) -> list[str]:
        """已建立路由的工具名。

        已排除 disabled_tools 与被冲突解析丢弃的同名项。调用方（如 kernel
        的动态工具管理）需要它来判断"这台 server 到底贡献了哪些工具"，
        不该去翻私有结构。
        """
        return sorted(self._routing)

    def close_all(self) -> None:
        """关闭所有已连接的 server 子进程。"""
        for client in self._clients.values():
            client.close()
        self._clients.clear()
