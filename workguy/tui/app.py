"""WorkGuy 全屏 TUI 主循环（curses）。

职责：消费 ``render.py`` 的纯文本组装结果，把它们画到 curses 屏幕上，并处理键盘、
布局、滚动、斜杠命令、动画。

关键工程约束（务必遵守，否则会破坏托管解释器的全量测试）：
- **模块顶层 ``import curses`` 用 try/except 包起来**：托管解释器没有 curses，
  本模块仍能 import（``curses = None``），不影响 312 项纯逻辑测试。
- curses 的所有真实调用都发生在 ``run()`` 内部、且仅在 ``curses is not None`` 且
  是 TTY 时才进入。否则打印清晰提示并退回批处理模式，**绝不抛栈**。
- ``kernel.run()`` 是同步阻塞的，**必须放到工作线程**，主循环继续跑动画；
  curses 只能在主线程调用，所以结果通过 ``queue`` 回主线程再写入 ``state``，
  杜绝竞态。

动画（用户明确要求「精美」，但本环境无法目测，只保证逻辑正确、不阻塞输入）：
- 开场点亮 600ms，任意键立即跳过；``should_animate`` 为 False 时完全关。
- 思考态：状态栏旋转指示 + Guy 切 ``thinking``；用 ``curses.timeout`` 让 ``get_wch``
  非阻塞，在等待期间持续重绘。
- 完成/出错：切 ``done`` / ``stuck``，短暂停留后回 ``idle``。
"""

from __future__ import annotations

import queue
import sys
import threading
import time

# —— curses 延迟/受保护导入：没有 curses 的解释器（托管测试解释器）也能 import 本模块 ——
try:
    import curses
except ImportError:  # 托管解释器没有 curses，不影响逻辑测试
    curses = None

from .animation import should_animate, splash_frames
from .commands import SUPPORTED, parse_command
from .layout import clamp_offset, compute_layout, visible_range
from .render import (
    PROMPT,
    RenderedLine,
    compose_hint_bar,
    compose_input,
    compose_splash,
    compose_status_bar,
    _input_scroll,
)
from .state import SessionState
from .textutil import display_width, truncate
from .theme import PAIR_IDS, color_for, init_color_pairs


# 开场动画总时长（ms）；与 animation.splash_frames 的 6×100ms 一致
_SPLASH_TOTAL_MS: int = 600
# 完成/出错表情短暂停留（ms）
_MOOD_FLASH_MS: int = 450
# 思考态重绘间隔（ms）：让 get_wch 非阻塞，期间持续转圈
_THINK_POLL_MS: int = 80


class TuiApp:
    """curses 全屏 TUI 应用。"""

    def __init__(self, kernel, state: SessionState | None = None, animate: bool = True) -> None:
        self._kernel = kernel
        self._state = state or SessionState()
        self._animate = bool(animate)
        # 颜色对（在 _main 里按终端能力初始化）
        self._pairs: dict[str, int] = {}
        self._has_color = False
        # 运行期状态（只在主线程读写）
        self._buffer: str = ""
        self._cursor: int = 0
        self._offset: int = 0  # 对话区滚动偏移（已滚过的行数）
        self._mood: str = "idle"
        self._mood_expire: float = 0.0  # 表情闪留到期时刻（monotonic 秒）
        self._busy: bool = False
        self._anim_frame: int = 0
        self._quit: bool = False
        self._result_q: queue.Queue = queue.Queue()
        self._cols: int = 80
        self._rows: int = 24

    # ------------------------------------------------------------------ 入口
    def run(self) -> int:
        """主入口，返回退出码。"""
        # 降级 1：没有 curses 模块
        if curses is None:
            self._print_fallback("当前 Python 解释器缺少 curses 模块（curses 不可用）")
            return 2
        # 降级 2：非交互终端（管道/重定向）→ 关闭动画并退回批处理提示
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            self._print_fallback("检测到非交互终端（管道/重定向）")
            return 2
        try:
            return curses.wrapper(self._main)
        except Exception as exc:  # noqa: BLE001 — 顶层兜底，终端无论如何要还原
            # 还原终端后再报错，避免把用户终端搞花
            sys.stderr.write(f"\n[TUI] 运行异常：{exc!r}\n")
            return 1

    def _print_fallback(self, reason: str) -> None:
        print(
            f"[WorkGuy TUI] 无法进入全屏模式：{reason}。\n"
            "  已退回批处理用法：\n"
            "    python -m workguy            # 查看可用子命令\n"
            "    python -m workguy version    # 产品名与版本\n"
            "    python -m workguy demo       # 内置精简演示\n"
            "    python -m workguy skills <目录>  # 扫描技能\n"
            "  若要用 TUI 聊天，请在真实交互终端（且安装 windows-curses）下运行：\n"
            "    python -m workguy chat --no-animation\n",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------ 主循环
    def _main(self, stdscr) -> int:
        # 终端初始化
        curses.curs_set(1)  # 显示光标（输入行需要）
        self._has_color = curses.has_colors()
        if self._has_color:
            self._pairs = init_color_pairs(curses, curses.COLORS)
        else:
            self._pairs = {name: 0 for name in PAIR_IDS}
        stdscr.leaveok(False)
        stdscr.keypad(True)

        animate = self._animate and should_animate(True)
        if animate:
            self._play_splash(stdscr)

        # 开场问候语
        from .theme import GREETING

        self._state.add_system(GREETING)

        while not self._quit:
            rows, cols = stdscr.getmaxyx()
            self._rows, self._cols = rows, cols
            layout = compute_layout(rows, cols)

            # 表情闪留到期 → 回 idle
            if not self._busy and self._mood_expire and time.monotonic() > self._mood_expire:
                self._mood = "idle"
                self._mood_expire = 0.0

            self._draw(stdscr, layout)

            # 忙碌时非阻塞轮询（动画），空闲时阻塞等输入
            stdscr.timeout(_THINK_POLL_MS if (self._busy and animate) else -1)
            try:
                ch = stdscr.get_wch()
            except curses.error:
                ch = None
            except KeyboardInterrupt:
                ch = 3  # 当作 Ctrl+C

            if ch is not None:
                self._handle_key(stdscr, ch, layout)

            # 忙碌态：检查工作线程结果（不阻塞）
            if self._busy:
                self._anim_frame += 1
                try:
                    item = self._result_q.get_nowait()
                except queue.Empty:
                    item = None
                if item is not None:
                    self._consume_result(item)

            # 窗口缩放：getmaxyx 已反映新尺寸，下一轮自然重画
        return 0

    # ------------------------------------------------------------------ 键盘
    def _handle_key(self, stdscr, ch, layout) -> None:
        # 任意键跳过开场动画（由 _play_splash 内部的 timeout 处理，这里不重复）
        # Ctrl+C：退出
        if ch == 3:
            self._quit = True
            return

        # 退格：删除光标前字符
        if ch in (curses.KEY_BACKSPACE, 127, 8, "\b"):
            if self._cursor > 0:
                self._buffer = self._buffer[: self._cursor - 1] + self._buffer[self._cursor :]
                self._cursor -= 1
            return

        # 回车：提交
        if ch in (curses.KEY_ENTER, "\n", "\r"):
            self._submit()
            return

        # 方向键
        if ch == curses.KEY_LEFT:
            self._cursor = max(0, self._cursor - 1)
            return
        if ch == curses.KEY_RIGHT:
            self._cursor = min(len(self._buffer), self._cursor + 1)
            return
        if ch == curses.KEY_UP:
            prev = self._state.history_prev()
            if prev is not None:
                self._buffer = prev
                self._cursor = len(prev)
            return
        if ch == curses.KEY_DOWN:
            nxt = self._state.history_next()
            if nxt is not None:
                self._buffer = nxt
                self._cursor = len(nxt)
            else:
                self._buffer = ""
                self._cursor = 0
            return

        # 滚动对话区
        chat_h = max(0, layout.chat_end - layout.chat_start)
        if ch == curses.KEY_PPAGE:
            self._offset = clamp_offset(self._offset - chat_h, self._line_count(), chat_h)
            return
        if ch == curses.KEY_NPAGE:
            self._offset = clamp_offset(self._offset + chat_h, self._line_count(), chat_h)
            return
        if ch == curses.KEY_HOME:
            self._offset = 0
            return
        if ch == curses.KEY_END:
            self._offset = clamp_offset(self._offset, self._line_count(), chat_h)
            return

        # 可打印字符（含中文，get_wch 返回 str）：插入到光标处
        if isinstance(ch, str) and ch.isprintable():
            self._buffer = self._buffer[: self._cursor] + ch + self._buffer[self._cursor :]
            self._cursor += 1

    # ------------------------------------------------------------------ 提交
    def _submit(self) -> None:
        text = self._buffer.strip()
        if not text:
            return
        # 斜杠命令优先处理
        cmd = parse_command(text)
        if cmd is not None:
            self._exec_command(cmd.name.lower(), cmd.args)
            # 命令执行后清空输入（未退出则）
            self._buffer = ""
            self._cursor = 0
            return
        # 忙碌中不允许重复提交
        if self._busy:
            return
        # 普通对话：记录历史 + 回显 + 起工作线程跑 kernel
        self._state.push_input(text)
        self._state.add_user(text)
        self._state.incr_turns()
        self._buffer = ""
        self._cursor = 0
        self._busy = True
        self._mood = "thinking"
        self._result_q = queue.Queue()
        t = threading.Thread(target=self._worker, args=(text,), daemon=True)
        t.start()

    def _worker(self, text: str) -> None:
        """工作线程：同步跑 kernel.run，结果回队列（不在本线程碰 curses/state 显示）。"""
        try:
            result = self._kernel.run(self._state.agent, text, session_id=self._state.session_id or None)
            self._result_q.put(("ok", result))
        except Exception as exc:  # noqa: BLE001
            self._result_q.put(("err", exc))

    def _consume_result(self, item) -> None:
        """主线程消费结果，写入 state（仅主线程碰 state，无竞态）。"""
        self._busy = False
        status, payload = item
        if status == "ok":
            res = payload
            self._state.add_assistant(getattr(res, "content", "") or "")
            self._state.add_credits(float(getattr(res, "credits", 0.0) or 0.0))
            if getattr(res, "model", ""):
                self._state.set_model(res.model, getattr(res, "model_tier", ""))
            for d in getattr(res, "denied", []) or []:
                self._state.add_error(f"被拒：{d}")
            self._mood = "done"
        else:
            self._state.add_error(f"调用失败：{payload!r}")
            self._mood = "stuck"
        self._mood_expire = time.monotonic() + _MOOD_FLASH_MS / 1000.0
        # 新消息自动滚到底
        self._offset = clamp_offset(self._offset, self._line_count(), self._chat_height())

    # ------------------------------------------------------------------ 命令
    def _exec_command(self, name: str, args: str) -> None:
        if name in ("exit", "quit"):
            self._quit = True
            return
        if name == "clear":
            self._state.clear()
            return
        if name == "help":
            self._state.add_system(
                "支持的命令：/exit /quit /clear /skills /audit /agent <name> /help"
            )
            return
        if name == "agent":
            if self._state.set_agent(args):
                self._state.add_system(f"已切换到 agent：{args}")
            else:
                self._state.add_error(f"未知 agent：{args or '(空)'}。用 /skills 查看可用代理")
            return
        if name == "skills":
            self._add_skills()
            return
        if name == "audit":
            self._add_audit()
            return
        # 未知命令（parse_command 放行但不在 SUPPORTED 内）
        self._state.add_error(f"未知命令 /{name}。输入 /help 查看支持的命令")

    def _add_skills(self) -> None:
        skills = getattr(self._kernel, "skills", None)
        if not skills:
            self._state.add_system("未挂载技能注册表。")
            return
        try:
            specs = list(skills.list_specs())
        except Exception as exc:  # noqa: BLE001
            self._state.add_error(f"列出技能失败：{exc!r}")
            return
        if not specs:
            self._state.add_system("未找到任何技能。")
            return
        self._state.add_system(f"可用技能（{len(specs)} 个）：")
        for s in specs:
            flag = " [disabled]" if getattr(s, "disabled", False) else ""
            self._state.add_system(f"  • {s.name}{flag}")

    def _add_audit(self) -> None:
        audit = getattr(self._kernel, "audit", None)
        if audit is None:
            self._state.add_system("未挂载审计链。")
            return
        try:
            ok = audit.verify_chain()
        except Exception as exc:  # noqa: BLE001
            self._state.add_error(f"审计校验失败：{exc!r}")
            return
        self._state.add_system(f"审计链完整：{ok}")

    # ------------------------------------------------------------------ 绘制
    def _line_count(self) -> int:
        return len(self._state.lines())

    def _chat_height(self) -> int:
        layout = compute_layout(self._rows, self._cols)
        return max(0, layout.chat_end - layout.chat_start)

    def _draw(self, stdscr, layout) -> None:
        stdscr.erase()
        # 1) 状态栏
        rl = compose_status_bar(
            self._state,
            layout.total_cols,
            mood=self._mood,
            frame=self._anim_frame,
            context_ratio=self._context_ratio(),
        )
        self._put(stdscr, layout.status_row, 0, rl)

        # 2) 对话区（逻辑行 → 按列折行 → 可见区间）
        display = self._build_chat_display(layout.total_cols)
        start, end = visible_range(len(display), layout.chat_end - layout.chat_start, self._offset)
        row = layout.chat_start
        for i in range(start, end):
            self._put(stdscr, row, 0, display[i])
            row += 1

        # 3) 输入行
        inp = compose_input(self._state, layout.total_cols, self._buffer, self._cursor)
        self._put(stdscr, layout.input_start, 0, inp)
        # 光标定位到输入缓冲中的实际位置
        self._place_input_cursor(stdscr, layout, inp)

        # 4) 提示栏
        hint = compose_hint_bar(layout.total_cols, busy=self._busy)
        self._put(stdscr, layout.hint_row, 0, hint)

        stdscr.refresh()

    def _build_chat_display(self, cols: int) -> list[RenderedLine]:
        """把 state 的逻辑行按终端列宽折行，展开成「显示行」列表。"""
        from .textutil import wrap_text

        out: list[RenderedLine] = []
        for ln in self._state.lines():
            wrapped = wrap_text(ln.text, cols) or [""]
            for w in wrapped:
                out.append(RenderedLine(w, ln.style))
        return out

    def _place_input_cursor(self, stdscr, layout, inp: RenderedLine) -> None:
        """把终端光标放到输入缓冲里光标字符对应的列。"""
        if layout.input_start < 0 or self._cursor < 0:
            return
        # 复算滚动窗口，得到光标在视口内的显示列
        avail = layout.total_cols - display_width(PROMPT)
        a, b, prefix, suffix, view = _input_scroll(self._buffer, self._cursor, avail)
        pw = display_width(PROMPT)
        col = pw + display_width(prefix) + display_width(view[: self._cursor - a])
        try:
            stdscr.move(layout.input_start, col)
        except curses.error:
            pass

    def _put(self, stdscr, row: int, col: int, rl: RenderedLine) -> None:
        """画一行；自动按语义色上色，并夹到可用列、绝不越界。"""
        text = rl.text
        if not text:
            return
        if row < 0 or row >= self._rows or col < 0:
            return
        # 语义名直接是 PAIR_IDS 的键，否则经 color_for 归一
        sem = rl.style if rl.style in PAIR_IDS else color_for(rl.style)
        pair = self._pairs.get(sem, 0)
        attr = curses.color_pair(pair) if (pair and self._has_color) else 0
        max_col = self._cols - 1
        if col > max_col:
            return
        avail = max_col - col + 1
        if display_width(text) > avail:
            text = truncate(text, avail)
        try:
            stdscr.addstr(row, col, text, attr)
        except curses.error:
            pass

    # ------------------------------------------------------------------ 开场动画
    def _play_splash(self, stdscr) -> None:
        frames = splash_frames()
        if not frames:
            return
        start = time.monotonic()
        elapsed = 0.0
        shown_idx = -1
        while elapsed < _SPLASH_TOTAL_MS:
            rows, cols = stdscr.getmaxyx()
            # progress：用已播放比例驱动 compose_splash
            progress = elapsed / _SPLASH_TOTAL_MS
            lines = compose_splash(rows, cols, progress)
            # 重画（仅 splash 区域，简单 erase 整屏）
            stdscr.erase()
            r = 0
            for rl in lines:
                if r >= rows:
                    break
                self._put(stdscr, r, 0, rl)
                r += 1
            stdscr.refresh()

            # 非阻塞等待一帧；任意键立即跳过
            stdscr.timeout(30)
            try:
                ch = stdscr.get_wch()
            except curses.error:
                ch = None
            if ch is not None:
                break
            elapsed = (time.monotonic() - start) * 1000.0
        # 确保最后一帧（全亮）至少闪一下
        try:
            rows, cols = stdscr.getmaxyx()
            lines = compose_splash(rows, cols, 1.0)
            stdscr.erase()
            r = 0
            for rl in lines:
                if r >= rows:
                    break
                self._put(stdscr, r, 0, rl)
                r += 1
            stdscr.refresh()
        except curses.error:
            pass

    # 上下文占用率（占位：暂无精确 token 计数时返回 0，由调用方接入 monitor）
    def _context_ratio(self) -> float:
        return 0.0
