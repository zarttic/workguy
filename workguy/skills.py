"""Skill（技能）机制：零依赖的 SKILL.md 解析 + 渐进式加载（Progressive Disclosure）。

设计要点：
- L0：frontmatter 元数据（name/description/...），注册时常驻内存，是模型判断「要不要用」的唯一匹配面。
- L1：SKILL.md 正文（指令/workflow），命中后才读盘。
- L2：references/ scripts/ assets/ 附属资源，按需单独读。

零第三方依赖：自己写 frontmatter 解析（简易 YAML 子集），不引入 PyYAML。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from workguy.types import SkillSpec

# ---------------------------------------------------------------------------
# frontmatter 解析（简易 YAML 子集，零依赖）
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[a-z0-9]+")
_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _parse_scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", "none"):
        return None
    if _INT_RE.fullmatch(v):
        try:
            return int(v)
        except ValueError:
            pass
    if _FLOAT_RE.fullmatch(v):
        try:
            return float(v)
        except ValueError:
            pass
    return v


def _is_block_sigil(val: str) -> bool:
    """folded(>) / literal(|) 块标：`>-` `|-` `>+` `|+` 等。"""
    return val in (">", ">-", ">+", "|", "|-", "|+")


def _parse_fm_lines(fm_lines: list[str]) -> dict:
    """解析 frontmatter 行列表为 dict。"""
    data: dict = {}
    i = 0
    n = len(fm_lines)
    while i < n:
        line = fm_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # 顶层列表项（无父键）跳過，避免被误判为 key
        if stripped.startswith("-"):
            i += 1
            continue
        m = re.match(r"^([^\s:][^:]*):\s?(.*)$", line)
        if not m:
            i += 1
            continue
        key = m.group(1).strip()
        val = m.group(2)
        if _is_block_sigil(val):
            # 折叠/字面块：收集后续缩进行
            i += 1
            block: list[str] = []
            while i < n:
                bl = fm_lines[i]
                if not bl.strip():
                    block.append("")
                    i += 1
                    continue
                if not (bl.startswith(" ") or bl.startswith("\t")):
                    break
                block.append(bl.strip())
                i += 1
            while block and block[-1] == "":
                block.pop()
            if val.startswith(">"):
                # folded：行用空格连接，去除尾部空白
                joined = " ".join(block).rstrip()
            else:
                # literal：保留换行
                joined = "\n".join(block)
            data[key] = joined
            continue
        if val == "":
            # 可能是列表，也可能就是空值
            if i + 1 < n and fm_lines[i + 1].lstrip().startswith("- "):
                i += 1
                lst: list[str] = []
                while i < n and fm_lines[i].lstrip().startswith("-"):
                    item = fm_lines[i].lstrip()[1:].strip()
                    lst.append(_unquote(item))
                    i += 1
                data[key] = tuple(lst)
                continue
            data[key] = None
            i += 1
            continue
        data[key] = _parse_scalar(val)
        i += 1
    return data


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md：返回 (frontmatter 字典, 正文)。

    零依赖简易 YAML 子集，支持：key:value、列表（- item）、折叠文本
    （key: >- / key: |- 后的缩进块）、引号包裹的值。缺失 frontmatter
    时返回 ({}, 原文)。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_lines = lines[1:end]
    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    data = _parse_fm_lines(fm_lines)
    return data, body


# ---------------------------------------------------------------------------
# 渐进式加载
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """中英文混合分词：拉丁词 + 每个 CJK 字符作为独立 token。"""
    text = text.lower()
    tokens: set[str] = set()
    for m in _LATIN_RE.findall(text):
        tokens.add(m)
    for ch in text:
        if _CJK_RE.match(ch):
            tokens.add(ch)
    return tokens


def _build_spec(fm: dict, path: str) -> SkillSpec:
    norm: dict = {}
    for k, v in fm.items():
        norm[k.replace("-", "_")] = v
    name = norm.get("name") or Path(path).parent.name
    desc = norm.get("description") or ""
    allowed = norm.get("allowed_tools") or ()
    if isinstance(allowed, str):
        allowed = (allowed,)
    disabled = bool(norm.get("disabled", False)) or bool(norm.get("disable", False))
    return SkillSpec(
        name=name,
        description=desc,
        path=path,
        description_en=norm.get("description_en"),
        when_to_use=norm.get("when_to_use"),
        allowed_tools=tuple(allowed),
        license=norm.get("license"),
        version=norm.get("version"),
        author=norm.get("author"),
        disabled=disabled,
    )


class SkillRegistry:
    """Skill 注册表。L0 常驻，L1/L2 按需读盘。"""

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        self._specs: dict[str, SkillSpec] = {}
        self._disabled: set[str] = set()
        # spy 计数器：证明 L1/L2 真的按需读盘
        self.l1_reads: int = 0
        self.l2_reads: int = 0
        if roots:
            for r in roots:
                self.scan(r)

    # -- L0：注册 / 列举 --------------------------------------------------
    def scan(self, root) -> int:
        """扫目录，仅读 frontmatter（L0，不把正文留在内存）。返回扫描到的技能数。"""
        root = Path(root)
        count = 0
        for p in root.rglob("SKILL.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = text.split("\n")
            if not lines or lines[0].strip() != "---":
                continue
            end = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end = i
                    break
            if end is None:
                continue
            fm = _parse_fm_lines(lines[1:end])  # 只解析 frontmatter，正文从不进入内存
            spec = _build_spec(fm, str(p))
            self.register_skill(spec)
            count += 1
        return count

    def register_skill(self, spec: SkillSpec) -> None:
        self._specs[spec.name] = spec
        if spec.disabled:
            self._disabled.add(spec.name)

    def list_specs(self) -> list[SkillSpec]:
        """L0 全量（含 disabled）。"""
        return list(self._specs.values())

    # -- match：L0 关键词打分 --------------------------------------------
    def match(self, query: str) -> list[SkillSpec]:
        """对 name + description 做分词匹配，按命中数（score）降序返回候选。

        disabled 技能不参与。只扫 L0 元数据，**不读正文**。
        """
        qtokens = _tokenize(query)
        if not qtokens:
            return []
        results: list[tuple[int, str, SkillSpec]] = []
        for spec in self._specs.values():
            if spec.name in self._disabled:
                continue
            text = (
                spec.name + " " + spec.description + " " + (spec.when_to_use or "")
            ).lower()
            score = sum(1 for t in qtokens if t in text)
            if score > 0:
                results.append((score, spec.name, spec))
        results.sort(key=lambda x: (-x[0], x[1]))
        return [r[2] for r in results]

    # -- L1：正文 --------------------------------------------------------
    def get_body(self, name: str) -> str:
        """L1：命中后才读 SKILL.md 正文。"""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        self.l1_reads += 1
        text = Path(spec.path).read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        return body

    # -- L2：附属资源 ----------------------------------------------------
    def get_reference(self, name: str, rel: str) -> str:
        """L2：读附属文件。防目录穿越（../ 攻击），越界抛 ValueError。"""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        base = Path(spec.path).parent.resolve()
        target = (base / rel).resolve()
        # base 自身或 base 的子路径才合法；否则视为穿越
        if target != base and base not in target.parents:
            raise ValueError(f"path traversal denied: {rel!r}")
        if not target.is_file():
            raise FileNotFoundError(rel)
        self.l2_reads += 1
        return target.read_text(encoding="utf-8")

    def allowed_tools(self, name: str) -> tuple[str, ...]:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        return spec.allowed_tools

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def disable(self, name: str) -> None:
        self._disabled.add(name)
