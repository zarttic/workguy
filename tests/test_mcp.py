"""MCP 客户端的测试套件（真实子进程，非 mock）。

通过 tests/fixtures/fake_mcp_server.py 启动真实 MCP server 子进程，
验证 stdio 传输下的 JSON-RPC 握手、工具列举、工具调用、超时、进程清理、
disabled 过滤、挂载与同名工具冲突策略。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让 fixture 脚本路径稳定可寻（无论 cwd 在哪）
FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"

from unittest import mock

from workguy.mcp import MCPClient, MCPError, MCPRegistry
from workguy.sandbox import CommandNotAllowedError
from workguy.tools import ToolRegistry
from workguy.types import MCPServerConfig, ToolCall

# 测试默认 timeout，给真实子进程留足余量（秒级）
NORMAL_TIMEOUT_MS = 10_000


def make_config(name: str, **overrides) -> MCPServerConfig:
    """构造指向 fake_mcp_server.py 的 MCP server 配置。"""
    base = dict(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(FIXTURE),),
        timeout_ms=NORMAL_TIMEOUT_MS,
    )
    base.update(overrides)
    return MCPServerConfig(**base)


class TestMCPRealSubprocess(unittest.TestCase):
    """以下测试全部跑真实子进程。"""

    def test_01_single_client_connect_initialize(self):
        client = MCPClient(make_config("s1"))
        client.connect()
        try:
            result = client.initialize()
            self.assertIn("serverInfo", result)
            self.assertEqual(result["serverInfo"]["name"], "fake-mcp-server")
        finally:
            client.close()

    def test_02_list_tools_names(self):
        client = MCPClient(make_config("s1"))
        client.connect()
        try:
            client.initialize()
            tools = client.list_tools()
            names = {t["name"] for t in tools}
            self.assertIn("echo", names)
            self.assertIn("add", names)
            self.assertIn("whoami", names)
            self.assertIn("echo", client.tool_names)
        finally:
            client.close()

    def test_03_call_tool_echo_and_add(self):
        client = MCPClient(make_config("s1"))
        client.connect()
        try:
            client.initialize()
            client.list_tools()
            self.assertEqual(client.call_tool("echo", {"text": "hello world"}), "hello world")
            self.assertEqual(client.call_tool("add", {"a": 2, "b": 3}), "5")
            # 字符串参数同样支持
            self.assertEqual(client.call_tool("add", {"a": "7", "b": "8"}), "78")
        finally:
            client.close()

    def test_04_full_roundtrip_no_error(self):
        client = MCPClient(make_config("s1"))
        client.connect()
        client.initialize()
        tools = client.list_tools()
        out = client.call_tool("echo", {"text": "roundtrip"})
        client.close()
        self.assertIsInstance(tools, list)
        self.assertEqual(out, "roundtrip")
        # close 后子进程应已退出
        self.assertIsNone(client._proc)

    def test_05_disabled_server_not_connected(self):
        reg = MCPRegistry()
        reg.add(make_config("off", disabled=True))
        counts = reg.connect_all()
        self.assertEqual(counts, {})  # 没连任何 server
        self.assertEqual(reg._clients, {})
        # 调用任何工具都应失败（没有可用 server）
        with self.assertRaises(MCPError):
            reg.call_tool("echo", {"text": "x"})
        reg.close_all()

    def test_06_disabled_tools_not_mounted(self):
        reg = MCPRegistry()
        reg.add(make_config("s1", disabled_tools=("echo",)))
        counts = reg.connect_all()
        # echo 被禁用，只剩 add / whoami / hang 三个工具
        self.assertEqual(counts["s1"], 3)
        # echo 不应进入路由表
        with self.assertRaises(MCPError):
            reg.call_tool("echo", {"text": "x"})
        # 未被禁用的工具仍可用
        self.assertEqual(reg.call_tool("add", {"a": 1, "b": 1}), "2")
        reg.close_all()

    def test_07_mount_into_registry(self):
        reg = MCPRegistry()
        reg.add(make_config("s1"))
        reg.connect_all()

        tool_reg = ToolRegistry()
        mounted = reg.mount_into(tool_reg)
        self.assertEqual(mounted, 4)  # echo, add, whoami, hang
        # 挂载后可通过既有注册表执行 MCP 工具
        out = tool_reg.execute(ToolCall(id="1", name="echo", arguments={"text": "mounted"}), "tester")
        self.assertEqual(out, "mounted")
        reg.close_all()

    def test_08_process_cleanup_after_close(self):
        client = MCPClient(make_config("s1"))
        client.connect()
        client.initialize()
        proc = client._proc
        self.assertIsNotNone(proc)
        self.assertIsNone(proc.poll())  # 运行中
        client.close()
        # close 后进程确实退出（poll 返回退出码，非 None）
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(client._proc)

    def test_09_timeout_raises_mcp_error(self):
        # 极小 timeout + 一个会睡眠 30s 的 hang 工具 -> 应超时抛 MCPError
        # 极小 timeout（握手足够，但 hang 工具睡眠 30s 必然触发超时）
        client = MCPClient(make_config("slow", timeout_ms=800))
        client.connect()
        try:
            client.initialize()
            client.list_tools()
            with self.assertRaises(MCPError):
                client.call_tool("hang", {})
        finally:
            client.close()

    def test_10_name_conflict_first_registered_wins(self):
        reg = MCPRegistry()
        # 两个 server 都提供 whoami / echo / add，但 whoami 会返回各自名字
        reg.add(make_config("A", env={"FAKE_MCP_NAME": "A"}))
        reg.add(make_config("B", env={"FAKE_MCP_NAME": "B"}))
        counts = reg.connect_all()
        # A 先注册，挂满 4 个工具；B 的工具全部因同名被忽略 -> 0
        self.assertEqual(counts["A"], 4)
        self.assertEqual(counts["B"], 0)
        # 路由表每个工具名只有一条（先注册者优先）
        self.assertEqual(len(reg._routing), 4)
        # whoami 命中 A，返回 "A" 而非 "B"
        self.assertEqual(reg.call_tool("whoami", {}), "A")
        self.assertEqual(reg.call_tool("echo", {"text": "z"}), "z")
        reg.close_all()


class TestMCPSecurity(unittest.TestCase):
    """命令准入校验的安全集成用例（不破坏上面的 10 项既有测试）。"""

    def test_08_calc_command_rejected_and_no_process(self):
        # command="calc.exe" 的配置，connect() 必须抛 CommandNotAllowedError，
        # 并且绝不能真的启动进程。
        cfg = make_config("evil", command="calc.exe", args=())
        client = MCPClient(cfg)
        with self.assertRaises(CommandNotAllowedError):
            client.connect()
        # 校验在 Popen 之前，进程绝不应当被创建。
        self.assertIsNone(client._proc)

    def test_08b_dangerous_args_rejected_on_real_python(self):
        # 即便命令在白名单内，带内联代码执行 / 注入字符的参数也被拦。
        cfg = make_config("evil2", command=sys.executable,
                          args=["-c", "import os; os.system('evil')"])
        client = MCPClient(cfg)
        with self.assertRaises(CommandNotAllowedError):
            client.connect()

    def test_09_allow_unvalidated_requires_reason(self):
        cfg = make_config("escape", command="calc.exe", args=())
        with self.assertRaises(ValueError):
            MCPClient(cfg, allow_unvalidated=True)  # 缺 unsafe_reason

    def test_09b_allow_unvalidated_skips_validation(self):
        # 逃生口：allow_unvalidated=True 时跳过校验（验证 validate_command 未被调用）。
        # 测试中不真的启动危险进程：mock 掉 Popen，使 connect 不会真正拉起 calc.exe。
        cfg = make_config("escape2", command="calc.exe", args=())
        client = MCPClient(cfg, allow_unvalidated=True, unsafe_reason="受信本地签名包")

        with mock.patch("workguy.mcp.validate_command") as vmock, \
             mock.patch("workguy.mcp.subprocess.Popen") as popen_mock:
            popen_mock.return_value.stdout = None
            try:
                client.connect()
            except Exception:
                pass
            # 关键断言：逃生口确实绕过了准入校验。
            vmock.assert_not_called()

    def test_09c_default_always_validates(self):
        # 默认（allow_unvalidated=False）时一定会调用校验。
        cfg = make_config("normal", command=sys.executable, args=(str(FIXTURE),))
        client = MCPClient(cfg)
        with mock.patch("workguy.mcp.validate_command") as vmock, \
             mock.patch("workguy.mcp.subprocess.Popen") as popen_mock:
            popen_mock.return_value.stdout = None
            try:
                client.connect()
            except Exception:
                pass
            vmock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
