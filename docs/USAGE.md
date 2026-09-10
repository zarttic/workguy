# WorkGuy 使用指南

## 一、安装

WorkGuy 零第三方依赖，只需要 **Python 3.11+**。

```bash
git clone https://github.com/zarttic/workguy.git
cd workguy
```

### 如果要用全屏 TUI，还需装一个包

Python 标准库在 **Windows 上没有 `curses`**（Linux / macOS 自带），
而全屏 TUI 依赖它，所以 Windows 用户需要额外装：

```bash
pip install windows-curses
```

Linux / macOS 用户跳过这一步，标准库自带。

---

## 二、最快验证：跑测试（不需要任何 key）

```bash
python -m unittest discover -s tests

# 预期输出
# Ran 333 tests in ~6s
# OK
```

333 项全绿说明环境没问题。

---

## 三、看它怎么工作：离线演示

```bash
python -m workguy demo
```

依次演示五个机制：最小权限边界、代理权限金字塔、上下文阈值压缩、哈希链审计、成本路由。

> 第一项最值得看：它让 Mock LLM 请求执行 `Bash(rm -rf /)`，
> 展示零权限代理是怎么拦下的——**底层 handler 一次都没被触达**。

---

## 四、扫描技能目录

```bash
python -m workguy skills /path/to/skills
```

列出目录下所有 `SKILL.md` 的技能名与描述（渐进加载的 L0 层）。

---

## 五、全屏 TUI 聊天（需要 LLM）

### 1. 设置 API key

推荐用**用户级环境变量**，设一次永久生效：

**Windows（PowerShell，永久）：**
```powershell
setx LONGCAT_API_KEY "ak_你的key"
# 重开终端后生效
```

**Git Bash / Linux / macOS：**
```bash
# 临时（当前会话）
export LONGCAT_API_KEY=ak_你的key

# 永久：写进 ~/.bashrc 或 ~/.zshrc
echo 'export LONGCAT_API_KEY=ak_你的key' >> ~/.bashrc
```

> key 只从环境变量读，**不写代码、不落盘、不入库**。缺 key 时会明确报错，不会静默降级。

### 2. 启动

```bash
python -m workguy chat
```

参数：

| 参数 | 说明 |
|---|---|
| `--agent cli` | 起始代理（默认 `cli`，全能代理） |
| `--no-animation` | 关掉开场/思考动画（脚本化场景用） |

### 3. 界面操作

| 按键 | 作用 |
|---|---|
| 直接输入 + Enter | 发消息 |
| `↑` `↓` | 翻历史输入 |
| `PageUp` / `PageDown` | 滚动对话 |
| `/exit` `/quit` | 退出 |
| `/clear` | 清屏 |
| `/skills` | 列出可用技能 |
| `/audit` | 查看审计链 |
| `/agent <名字>` | 切换代理 |
| `/help` | 帮助 |
| `Ctrl+C` | 强制退出 |

---

## 六、当成 Python 库用

```python
from workguy import AgentKernel, Gateway, LongCatProvider, Store, SkillRegistry

gateway = Gateway({"longcat": LongCatProvider(api_key="ak_xxx")})
kernel = AgentKernel(
    gateway=gateway,
    provider_chain=["longcat"],
    store=Store("wb.db"),        # 会话/审计/用量落盘
    skills=SkillRegistry(),       # 技能匹配
)

result = kernel.run("cli", "用一句话说明哈希链审计")
print(result.content)
print(f"credits={result.credits}  模型={result.model}  档位={result.model_tier}")
```

开箱可跑的完整例子见 `examples/real_llm.py`（key 从环境变量读）。

---

## 七、常见问题

**Q：报 `No module named '_curses'`**
A：Windows 上没装 `windows-curses`。执行 `pip install windows-curses`。
（其余功能不需要它，所以只有 TUI 会受影响。）

**Q：报"未设置 LONGCAT_API_KEY"**
A：环境变量没设或没生效。用 `setx` 的话需要**重开终端**。

**Q：TUI 里中文显示错位**
A：换等宽字体（Cascadia Code / JetBrains Mono），并确认终端按 UTF-8 解码。

**Q：动画太吵**
A：加 `--no-animation`。非 TTY 环境（管道、重定向）会自动关闭动画。

**Q：不想用 LongCat，想换别的模型**
A：`providers.py` 里已内置 OpenAI / Anthropic / LongCat 三家的方言互译，
用 `Gateway` 注册即可；`Gateway.complete_with_fallback` 还支持自动切换。

---

## 八、项目结构速查

```
workguy/
  # 内核
  kernel.py       执行循环（ReAct）
  agents.py       代理注册表 + 权限校验
  tools.py        工具注册表 + 懒加载
  context.py      上下文阈值监控
  audit.py        哈希链审计
  router.py       模型路由 + 计费
  config.py       阈值/模型/代理/工具配置
  # 产品化
  providers.py    多模型网关（OpenAI/Anthropic/LongCat）
  store.py        SQLite 持久化
  skills.py       技能渐进加载
  connectors.py   连接器 + 凭据占位
  mcp.py          MCP 客户端
  sandbox.py      命令准入校验
  # TUI
  tui/            界面层（art/theme/animation/render/app）
tests/            333 项测试
examples/         可运行示例
design/IP.md      形象与品牌规范
```
