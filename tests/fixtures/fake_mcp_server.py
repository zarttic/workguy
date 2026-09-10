#!/usr/bin/env python3
"""最小化 MCP server（仅用于测试，stdio 传输，JSON-RPC 2.0）。

从 stdin 逐行读取 JSON-RPC 请求，按 method 处理并写回一行 JSON 到 stdout。
支持：
- initialize      -> 返回协议/能力/服务端信息
- tools/list      -> 返回工具清单（echo / add / whoami / hang）
- tools/call      -> 调用具体工具
- 通知（无 id）   -> 不回包

工具：
- echo    : 原样返回 arguments["text"]
- add     : 返回 a + b
- whoami  : 返回环境变量 FAKE_MCP_NAME（用于测试同名工具冲突路由）
- hang    : 睡眠 30s 再回包（用于测试客户端超时保护）

注意：每条响应都显式 flush，避免块缓冲导致对端阻塞。
"""

import json
import os
import sys
import time


def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(msg):
    """处理单条 JSON-RPC 消息。通知（无 id）返回 None 表示不回包。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _ok(msg_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
        })

    if method == "tools/list":
        tools = [
            {
                "name": "echo",
                "description": "Echo the input text back.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            {
                "name": "add",
                "description": "Add two numbers and return the sum as text.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    "required": ["a", "b"],
                },
            },
            {
                "name": "whoami",
                "description": "Return this server's FAKE_MCP_NAME (for routing tests).",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "hang",
                "description": "Sleep 30s before responding (for timeout tests).",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
        return _ok(msg_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}

        if name == "echo":
            content = [{"type": "text", "text": str(arguments.get("text", ""))}]
        elif name == "add":
            a = arguments.get("a", 0)
            b = arguments.get("b", 0)
            content = [{"type": "text", "text": str(a + b)}]
        elif name == "whoami":
            content = [{"type": "text", "text": os.environ.get("FAKE_MCP_NAME", "?")}]
        elif name == "hang":
            # 故意长时间不回包，用于验证客户端超时保护
            time.sleep(30)
            content = [{"type": "text", "text": "done"}]
        else:
            return _err(msg_id, -32601, f"unknown tool: {name}")

        return _ok(msg_id, {"content": content})

    # 未知方法：若是请求则报错，若是通知则静默
    if msg_id is not None:
        return _err(msg_id, -32601, f"unknown method: {method}")
    return None


def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
