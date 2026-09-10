"""Skill / Connector 机制测试。

测试用的 SKILL.md 样例全部生成在临时目录，不污染项目。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from workguy.skills import SkillRegistry, parse_frontmatter
from workguy.connectors import ConnectorRegistry
from workguy.types import SkillSpec


def _write_skill(base: Path, name: str, fm: str, body: str = "# body\n") -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\n" + fm + "\n---\n\n" + body, encoding="utf-8")
    return d


class FrontmatterTest(unittest.TestCase):
    def test_simple_key_value(self):
        fm, body = parse_frontmatter(
            "---\nname: foo\ndescription: bar baz\nversion: 1.2\n---\n\nhello"
        )
        self.assertEqual(fm["name"], "foo")
        self.assertEqual(fm["description"], "bar baz")
        self.assertEqual(fm["version"], 1.2)
        self.assertEqual(body, "hello")

    def test_quoted_value(self):
        fm, _ = parse_frontmatter('---\nname: x\nversion: "0.1.2"\n---\n')
        self.assertEqual(fm["version"], "0.1.2")

    def test_list(self):
        fm, _ = parse_frontmatter(
            "---\nname: x\nallowed-tools:\n  - Read\n  - Write\n  - Bash\n---\n"
        )
        self.assertEqual(fm["allowed-tools"], ("Read", "Write", "Bash"))

    def test_folded_block(self):
        text = "---\ndescription: >-\n  ALWAYS TRIGGER map compliance\n  for any map request\n---\n\nbody"
        fm, body = parse_frontmatter(text)
        # 折叠块：行用空格连接，去除尾部换行
        self.assertEqual(fm["description"], "ALWAYS TRIGGER map compliance for any map request")
        self.assertEqual(body, "body")

    def test_literal_block(self):
        text = "---\nnotes: |-\n  line one\n  line two\n---\n"
        fm, _ = parse_frontmatter(text)
        self.assertEqual(fm["notes"], "line one\nline two")

    def test_missing_frontmatter_returns_original(self):
        original = "just markdown\nno frontmatter here"
        fm, body = parse_frontmatter(original)
        self.assertEqual(fm, {})
        self.assertEqual(body, original)

    def test_boolean_and_disabled(self):
        fm, _ = parse_frontmatter("---\nname: x\ndisabled: true\n---\n")
        self.assertTrue(fm["disabled"])


class ProgressiveLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _write_skill(
            self.tmp,
            "mapper",
            "name: mapper\ndescription: handle map and PDF documents\nallowed-tools:\n  - Read",
            "L1 BODY CONTENT FOR MAPPER\n",
        )
        _write_skill(
            self.tmp,
            "pdf",
            "name: pdf\ndescription: handle PDF files only\n",
            "L1 BODY CONTENT FOR PDF\n",
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_match_does_not_read_body(self):
        reg = SkillRegistry()
        reg.scan(self.tmp)
        self.assertEqual(reg.l1_reads, 0)  # scan 只读了 frontmatter
        hits = reg.match("pdf")
        self.assertGreater(len(hits), 0)
        # 核心断言：match 之后 L1 读取次数仍为 0，证明没有提前读正文
        self.assertEqual(reg.l1_reads, 0)

    def test_get_body_reads_disk(self):
        reg = SkillRegistry()
        reg.scan(self.tmp)
        body = reg.get_body("mapper")
        self.assertEqual(reg.l1_reads, 1)
        self.assertIn("L1 BODY CONTENT FOR MAPPER", body)
        # 再次读盘计数累加
        reg.get_body("pdf")
        self.assertEqual(reg.l1_reads, 2)

    def test_match_ranking(self):
        reg = SkillRegistry()
        reg.scan(self.tmp)
        # 'map PDF' 两个 token 都命中 mapper，只命中 'pdf' 命中 pdf
        hits = reg.match("map PDF")
        self.assertEqual(hits[0].name, "mapper")
        self.assertEqual(hits[1].name, "pdf")

    def test_match_ranking_cjk(self):
        d = self.tmp / "geo"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: geo\ndescription: 处理地图与位置服务的合规校验\n---\n\nbody", encoding="utf-8"
        )
        reg = SkillRegistry()
        reg.scan(self.tmp)
        hits = reg.match("地图")
        self.assertTrue(hits)
        self.assertEqual(hits[0].name, "geo")

    def test_disabled_excluded_from_match(self):
        reg = SkillRegistry()
        reg.scan(self.tmp)
        reg.disable("mapper")
        names = [s.name for s in reg.match("map")]
        self.assertNotIn("mapper", names)
        # 重新启用后可出现
        reg.enable("mapper")
        names = [s.name for s in reg.match("map")]
        self.assertIn("mapper", names)

    def test_register_disabled_spec_excluded(self):
        reg = SkillRegistry()
        reg.register_skill(SkillSpec(name="off", description="secret tool", disabled=True))
        reg.register_skill(SkillSpec(name="on", description="public tool"))
        names = [s.name for s in reg.match("tool")]
        self.assertIn("on", names)
        self.assertNotIn("off", names)

    def test_get_reference_normal_and_traversal(self):
        d = self.tmp / "mapper"
        (d / "references").mkdir(exist_ok=True)
        (d / "references" / "guide.txt").write_text("REFERENCE DATA", encoding="utf-8")
        reg = SkillRegistry()
        reg.scan(self.tmp)
        self.assertEqual(reg.get_reference("mapper", "references/guide.txt"), "REFERENCE DATA")
        self.assertEqual(reg.l2_reads, 1)
        # 目录穿越必须被拦截
        with self.assertRaises(ValueError):
            reg.get_reference("mapper", "../../etc/passwd")
        with self.assertRaises(ValueError):
            reg.get_reference("mapper", "../" + ".." + "/secret")
        # 不存在的文件
        with self.assertRaises(FileNotFoundError):
            reg.get_reference("mapper", "references/missing.txt")

    def test_allowed_tools(self):
        reg = SkillRegistry()
        reg.scan(self.tmp)
        self.assertEqual(reg.allowed_tools("mapper"), ("Read",))


class ConnectorTest(unittest.TestCase):
    def _cfg(self):
        return {
            "mcpServers": {
                "fs": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                    "disabledTools": ["write"],
                },
                "maps": {
                    "type": "streamableHttp",
                    "url": "https://maps.example.com/mcp",
                    "env": {"MAPS_TOKEN": "${MAPS_API_KEY}"},
                    "disabled": True,
                },
                "feeds": {
                    "type": "sse",
                    "url": "https://feeds.example.com/sse",
                },
            }
        }

    def test_load_config_mcpservers(self):
        reg = ConnectorRegistry()
        n = reg.load_config(self._cfg())
        self.assertEqual(n, 3)
        fs = reg.get("fs")
        self.assertEqual(fs.mcp.transport, "stdio")
        self.assertEqual(fs.mcp.args, ("-y", "@modelcontextprotocol/server-filesystem"))
        self.assertEqual(fs.mcp.disabled_tools, ("write",))
        # type 映射
        self.assertEqual(reg.get("maps").mcp.transport, "http")
        self.assertEqual(reg.get("feeds").mcp.transport, "sse")

    def test_credential_security(self):
        os.environ["MAPS_API_KEY"] = "SUPER_SECRET_VALUE"
        try:
            reg = ConnectorRegistry()
            reg.load_config(self._cfg())
            spec = reg.get("maps")
            # 真实值绝不落明文：env 里只保留占位符
            self.assertEqual(spec.mcp.env["MAPS_TOKEN"], "${MAPS_API_KEY}")
            self.assertEqual(spec.credential_env, "MAPS_API_KEY")
            # resolve 只在调用时从环境变量取值
            self.assertEqual(reg.resolve_credential("maps"), "SUPER_SECRET_VALUE")
        finally:
            os.environ.pop("MAPS_API_KEY", None)
        # env 缺失 -> None，且结构里没有任何真实值
        reg = ConnectorRegistry()
        reg.load_config(self._cfg())
        self.assertIsNone(reg.resolve_credential("maps"))
        self.assertNotIn("SUPER_SECRET_VALUE", str(reg.get("maps").mcp.env))

    def test_credential_exception_no_leak(self):
        # 即便出错，KeyError 也不应携带真实值
        reg = ConnectorRegistry()
        reg.load_config(self._cfg())
        with self.assertRaises(KeyError) as ctx:
            reg.resolve_credential("nonexistent")
        self.assertNotIn("SUPER_SECRET_VALUE", str(ctx.exception))

    def test_disabled_connector_not_in_enabled(self):
        reg = ConnectorRegistry()
        reg.load_config(self._cfg())
        reg.bind("fs", "FS_TOKEN")
        reg.bind("maps", "MAPS_API_KEY")  # maps 在配置里 disabled=True
        enabled_names = [c.name for c in reg.enabled()]
        self.assertIn("fs", enabled_names)
        self.assertNotIn("maps", enabled_names)

    def test_bind_unbind_flow(self):
        reg = ConnectorRegistry()
        reg.load_config(self._cfg())
        self.assertEqual(reg.enabled(), [])
        reg.bind("feeds", "FEEDS_TOKEN")
        self.assertEqual(len(reg.enabled()), 1)
        self.assertTrue(reg.get("feeds").bound)
        self.assertTrue(reg.get("feeds").enabled)
        self.assertEqual(reg.get("feeds").credential_env, "FEEDS_TOKEN")
        reg.unbind("feeds")
        self.assertFalse(reg.get("feeds").bound)
        self.assertFalse(reg.get("feeds").enabled)
        self.assertIsNone(reg.get("feeds").credential_env)
        self.assertEqual(reg.enabled(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
