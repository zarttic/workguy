"""WorkGuy 视觉主题 —— 颜色、语义、文案（IP 规范 v1 的代码化）。

纯数据 + 纯逻辑，**不 import curses**。渲染层由另一个 agent 负责；
本模块只提供「能被渲染层消费的语义色名、颜色值、文案」以及
「按终端能力初始化 curses 颜色对」的降级逻辑。

颜色降级链（对齐 IP.md 技术约束）：
    真彩 / 256 色  → 直接用 256 色值
    8 色           → 退到 0-7 八基色（按语义映射到最接近的基础色）
    无色（0）      → 全部返回 0，且不调用任何 curses 初始化

设计要点：
- ``init_color_pairs`` 把 ``curses_module`` 作为**参数**传入，便于测试注入 mock，
  模块顶层绝不 import curses（否则会破坏「非 TTY / 无终端」环境下的纯逻辑单测）。
- ``color_for`` / ``message_for`` 只做「语义归一」，未知输入安全回落，绝不抛错。
"""

from __future__ import annotations


# —— 语义色名 -> 256 色值（见 IP.md 色彩系统）——
# normal = -1 表示「用终端默认前景色」（curses 的 -1 语义）。
COLORS: dict[str, int] = {
    "brand": 51,     # Logo、Guy 本体、状态栏边框（青蓝）
    "accent": 208,   # 用户输入回显、当前焦点（琥珀）
    "dim": 240,      # 次要信息、分隔线、提示栏（灰）
    "success": 46,   # 完成、审计链完好（绿）
    "warning": 214,  # 成本告警、上下文压力（黄）
    "error": 196,    # 拒绝、失败（红）
    "normal": -1,    # 默认前景
}

# 语义名出现的顺序即颜色对的分配顺序（pair_id 从 1 起，避开 curses 保留的 0 号）。
SEMANTIC_NAMES: tuple[str, ...] = (
    "brand", "accent", "dim", "success", "warning", "error", "normal",
)

# 语义色名 -> curses 颜色对 ID（1..N，与上面顺序对应）。
PAIR_IDS: dict[str, int] = {name: idx for idx, name in enumerate(SEMANTIC_NAMES, start=1)}

# 8 色降级映射：把 256 色值按「语义」映射到最接近的 0-7 基础色。
#   0 黑 1 红 2 绿 3 黄 4 蓝 5 品红 6 青 7 白
_BASE_COLORS: dict[str, int] = {
    "brand": 6,    # 青蓝 -> 青
    "accent": 3,   # 琥珀 -> 黄（暖色系里最贴近）
    "dim": 7,      # 灰 -> 白（暗环境下可读）
    "success": 2,  # 绿 -> 绿
    "warning": 3,  # 黄 -> 黄
    "error": 1,    # 红 -> 红
    "normal": 0,   # 默认 -> 黑（渲染层对 normal 会退化为默认前景）
}


def init_color_pairs(curses_module, color_count: int) -> dict[str, int]:
    """按终端色深能力初始化颜色对，返回 ``{语义名: pair_id}``。

    降级链（IP.md 技术约束）：
    - ``color_count >= 256`` → 用 256 色值 ``COLORS`` 初始化每对（fg=语义色, bg=0）。
    - ``8 <= color_count < 256`` → 退到 8 基色 ``_BASE_COLORS``。
    - ``color_count == 0`` → 无色模式：全部返回 ``0``，**且不调用** ``init_pair``。

    参数 ``curses_module`` 注入式传入（mock 或真实 curses），模块本身不依赖它。
    """
    # 无色模式：不初始化任何颜色对，直接全部返回 0（渲染层用 pair 0 = 默认色）。
    if color_count == 0:
        return {name: 0 for name in SEMANTIC_NAMES}

    # 8 色模式与 256 色模式共用循环，仅 fg 取值来源不同。
    if color_count >= 256:
        fg_for = COLORS
    else:
        fg_for = _BASE_COLORS

    # 颜色对数量上限。部分终端（尤其 Windows）只有 64 对，
    # 超出范围时 init_pair 会返回 ERR，所以要逐项防御。
    try:
        max_pairs = curses_module.COLOR_PAIRS
    except Exception:  # pragma: no cover - mock 未必提供
        max_pairs = 64

    pairs: dict[str, int] = {}
    for name in SEMANTIC_NAMES:
        pair_id = PAIR_IDS[name]
        fg = fg_for[name]
        # 单项失败只让该项退化为默认色，不能连累整组
        if pair_id >= max_pairs:
            pairs[name] = 0
            continue
        try:
            curses_module.init_pair(pair_id, fg, 0)
            pairs[name] = pair_id
        except Exception:
            pairs[name] = 0
    return pairs


# Line.style（normal/user/assistant/tool/error/dim/accent/system）-> 语义色名。
# 未知 style 一律回落到 ``normal``，保证渲染层永不拿到不认识的色名。
_STYLE_TO_SEMANTIC: dict[str, str] = {
    "normal": "normal",
    "user": "accent",
    "assistant": "brand",
    "tool": "dim",
    "error": "error",
    "dim": "dim",
    "accent": "accent",
    "system": "dim",
}


def color_for(style: str) -> str:
    """把 ``Line.style`` 映射到语义色名；未知 style 回落到 ``normal``。"""
    return _STYLE_TO_SEMANTIC.get(style, "normal")


# —— 语言风格（IP.md 第五节，直接照抄那些文案）——
# 核心：短、具体、不道歉、不卖萌、不说废话，像个真正的同事。
GREETING: str = "在。说吧。"
THINKING: str = "思考中"
DONE: str = "好了。"
FAREWELL: str = "走了。"
TAGLINE: str = "WorkGuy —— 你的工作搭子"

# kind -> 文案。denied / error 用「同事口吻」：指出问题、给下一步，不道歉不卖萌。
_MESSAGES: dict[str, str] = {
    "greeting": GREETING,
    "thinking": THINKING,
    "done": DONE,
    "farewell": FAREWELL,
    # 被拒：直接说不行 + 原因，像同事挡活
    "denied": "不行，compact 没有工具权限",
    # 出错：陈述事实 + 抛回一个可执行的下一步
    "error": "这个挂住了。要重试吗？",
}


def message_for(kind: str) -> str:
    """返回 ``kind`` 对应的文案。

    ``kind ∈ {greeting, thinking, done, farewell, denied, error}``；
    未知 kind 回落到 ``DONE``（绝不抛错，渲染层永远有字可画）。
    """
    return _MESSAGES.get(kind, DONE)
