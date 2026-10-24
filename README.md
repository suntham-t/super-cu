# dsh-super-cu

**Windows 电脑操作插件（DeepSeek Harness / DSH bundle）** — 让智能体**看得懂界面、点得准、还快**。
用 **UI Automation 语义文本**取代截图循环：默认不截图、不猜坐标、一次调用批量做完多步。

> A Windows computer-use bundle for DeepSeek Harness. Two agent tools — `cu_show` (observe) and
> `cu_do` (act) — built on UI Automation with **stable element ids**, **pattern-first actions**,
> **batched steps** and a **visual fallback**. No dependency on screenshots for the normal path.

|  | 实测（同机对比 `windows-mcp` 4.0.3） |
|---|---|
| 观察延迟 p50 / p95 | **231 / 255 ms** vs 1059 / 1229 ms → **快 4.57×** |
| 文本体积（Edge 15 标签页） | **3 308 字符** vs 31 261 → **小 9.45×**（~1305 vs ~9472 tokens） |
| 元素级稳定 id | **跨快照 100% 复用**（旧方案只有坐标） |
| 交互元素召回率 | **100%（93/93）** |
| 冷启动到 tools/list | **745 ms** vs 6 993 ms |
| 工具数 | **2 个** vs 20 个 |

---

## 为什么快

1. **窗口列表走 win32 `EnumWindows`（~5 ms）**，UIA 只进目标窗口（旧方案每次枚举整桌面 UIA ≈420 ms/次）。
2. **单窗口 DFS + 叶子剪枝 + 属性延迟读取**：Edge 428 节点 walk 123 ms + 属性 65 ms。
3. **默认纯文本，不截图**：截图要多一次抓屏+压缩（~90 ms）和上千 token。
4. **有界 DFS 早停**：controls 视图最终只展示前 `max_nodes` 个交互元素，收集满即停 ——
   重窗口（436 节点、truncated）p50 **265 → 135 ms（-49%）**，输出逐字节相同、召回 100%。
5. **批量动作**：一次 `cu_do` 完成多步（含等待与断言），把 N 个来回压成 1 个。

## 为什么准

1. **稳定元素 id**：UIA `RuntimeId → 进程内自增 int`，同一元素跨快照 id 不变（实测复用率 100%）；
   失效时按 `(类型, 名字) → AutomationId → 祖先路径` **语义重定位**后继续执行原动作。
2. **Pattern 优先**：`invoke / setvalue / select / toggle / expand / focus` 走 UIA Pattern
   （后台生效、不抢焦点、不动鼠标），不可用才回退真实点击。
3. **歧义即拒绝**：`ct:名字` 命中多个候选时报 `AmbiguousError` 并列出候选，**绝不猜**。
4. **DPI 三空间合一**：启动即 `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`，
   使 UIA rect / 抓屏 / 点击落在同一物理像素空间（150% 缩放下旧做法会偏 1.5 倍）。
5. **归一化坐标**：视觉兜底用 `@n:0.42,0.31`（0–1，相对最近截图），换算在服务端完成，模型不做算术。
6. **动作后可断言**：`expect` / `wait` 在同一次调用里确认结果，而不是"做完就走"。
7. **诚实失败**：`disabled` 控件、`pattern_unavailable`、剪贴板被占用、未知按键 —— 都返回
   `ok:false` + 可操作建议，**不静默成功**。

## 工具

### `cu_show` — 观察（默认纯文本，~50–300 ms）

```
win #39719780 "无标题 - 画图" 2030x1271 @264,159 [focus]
obs 226n/145 shown 239ms controls cut=budget(+61 hidden)
[6] document "开发高效精准的ComputerUse插件"
 [7] button "新建会话" [i]
 [15] edit "搜索会话名称" ~
 [57] tabitem "对话"
```
元素行 = `[稳定 id] 类型 "名字"` + 能力字母（`e` expand > `t` toggle > `s` selection > `v` value > `i` invoke > `c` focusable）+ 状态字符（`*` focus / `+` selected / `-` disabled / `~` offscreen）。
`cut=tree` 表示节点树被截断，`cut=budget(+N hidden)` 表示字符预算裁剪 —— 两者可同时出现。

主要参数：`window`（标题/进程名/`#hwnd`）、`view`（controls/text/all）、`filter`、`max_nodes`、
`screenshot`、`region`、`max_image_width`。

### `cu_do` — 动作（一次调用多步）

```json
{"steps": [
  {"op": "set",  "target": "#8713", "text": "关键词", "expect": "关键词"},
  {"op": "key",  "keys": "enter"},
  {"op": "wait", "text": "搜索结果", "timeout_ms": 3000}
]}
```

- 目标写法：`#id`（推荐） / `ct:名字` / `@n:ux,uy`（视觉） / `@img:x,y` / `@x,y`（物理屏幕）
- 失败即停并给一行 `→` 建议；未执行的步骤跳过
- 目标窗口不在前台会自动 `activate`（含最小化还原、托盘隐藏窗口 `SW_SHOW`）

op 一览：

| 类别 | op |
|---|---|
| 鼠标 | `click` `dblclick` `rightclick` `hover` `drag`（可 `hold_ms`）`scroll` |
| 键盘 | `key` `type` `paste` `hold`（按住 N ms） |
| 鼠标按住 | `hold_mouse`（`left/right/middle`，按住期间 100 ms 心跳） |
| UIA | `invoke` `toggle` `select` `expand` `collapse` `focus` `set` |
| 窗口 | `activate` `launch` `close` `minimize` `maximize` |
| 控制 | `wait`（`wait_kind`: text/element/window/idle/change/gone） `assert` |

## 安装

**要求**：Windows 10/11、Python 3.11+、DSH `>= 0.2`。

```powershell
git clone https://github.com/suntham-t/super-cu.git
cd super-cu
./setup.ps1          # 自动探测 python、安装 Pillow + uiautomation、把绝对路径写进 cordis.patch.yml
```

然后在 DSH 里：**插件 / Plugins → 安装本地插件（install from folder）→ 选这个目录**。
重新开一个会话，工具 `mcp__supercu__cu_show` / `mcp__supercu__cu_do` 就出现了。

> 本插件以 **DSH bundle** 形式分发：`package.json` 声明 `dsh.bundle.patch`，`cordis.patch.yml`
> 通过官方 `@deepseek-ai/dsh-mcp-client` 行拉起一个常驻 Python MCP server（stdio）。
> 目录结构可直接被 `plugin_manager(install_bundle)` 或插件面板安装。

### 插件市场 / Plugin market

本仓库按 [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 的收录要求准备：
`package.json` 声明 `dsh.bundle`、根目录有 `cordis.patch.yml`、仓库带 `dsh-plugin` topic。
市场条目位于 [`.market/suntham-t__super-cu.yml`](.market/suntham-t__super-cu.yml)，
用 `./submit-to-market.ps1` 一键投稿（fork → 上传条目 → 开 PR，无需克隆 200MB 索引仓库）。


## 文件

```
package.json           DSH bundle 清单（dsh.bundle.patch）
cordis.patch.yml       MCP client 行（setup.ps1 写入本机路径）
setup.ps1              一次性安装：python 检测 + 依赖 + 路径替换
src/server.py          MCP stdio JSON-RPC、两个工具、坐标/断言/状态
src/bridge.py          UIA 快照 / 稳定 id / 解析 / 动作 / 截图 / wait
src/windows.py         win32 窗口枚举、激活升级链、剪贴板、DPI
src/compact.py         紧凑格式化（去噪 / 折叠 / 预算 / cut 标记）
src/dpi.py             DPI 三空间合一
src/gui_lock.py        多智能体共享桌面的互斥锁
src/mock_bridge.py     离线 mock（协议层测试用）
tests/                 单测 + 真机集成测试（GUI 部分）
SKILL.md               给智能体的操作手册
docs/CONTRACT.md       完整接口契约（实现细节）
docs/images/           真实任务截图
```

## 真实验证（不只是单测）

- **基准**：26 个应用/交互场景矩阵（16 PASS / 1 FAIL→已修 / 9 SKIP）、7 组压力与鲁棒性
  （20 步压力 RSS +0.05 MB / 句柄 +0；并发锁 ratio 0.66；`@n` 误差 worst 0.71 px；50 次 id 解析 0 失败）、
  输出质量审计（影子 text 残留 0）。
- **端到端**：记事本输入并保存到磁盘（内容逐字比对）、计算器 7×8=56、PowerPoint 6 页 PPT 并发送到微信
  （无 API 客户端）、微信发消息、画图里画笑脸、Blender 建一棵树、**在真实 Minecraft 里用 `/fill`
  盖了一栋 7×7 带门/窗/屋顶的房子**。
- **回归**：`tests/test_bridge.py` 29 项（含剪贴板、拖拽、UWP 重定位、修饰键、hold）+
  `tests/test_server.py` 141 项 mock 协议测试。

| | |
|---|---|
| ![notepad](docs/images/proof-notepad.jpg) | ![ppt](docs/images/proof-ppt-6slides.jpg) |
| ![minecraft](docs/images/proof-mc-house.jpg) | ![paint](docs/images/proof-smiley.jpg) |

## 已知限制

- **无 UIA 的界面**（canvas / 游戏 / 部分自绘 UI）：只能用 `screenshot=true` + `@n` 坐标，慢且依赖视觉。
- **Chromium 的 `view=text`** 取不到正文（TextPattern 为空）→ 用 `controls`（TextControl 的 name 就是文本）。
- **控制台窗口**（conhost）UIA 只有十几个节点、读不到输出文本 → 退路：截图或用非 UI 手段。
- **UIPI**：管理员权限窗口可能拒绝输入/读取。
- **指针锁定 + raw input 的游戏**（如 Minecraft）：`SetCursorPos` 无法转视角；要用游戏内命令或键位。
- **Office 现代「保存此文件」对话框**不接受全路径 → 先「更多选项…」→「浏览」进经典对话框。
- 一次 `cu_do` 建议 ≤10 步（批量期间持有 GUI 锁，其它智能体会等待）。

## License

MIT © 2026 suntham-t

---

### English TL;DR

`dsh-super-cu` is a DeepSeek Harness bundle that gives an agent two tools — `cu_show` (a compact,
token-cheap accessibility snapshot of one window with **stable element ids**) and `cu_do` (a batched
action runner with waits/assertions). Compared with screenshot-based computer use it is **4.6× faster**,
returns **9.5× less text** and reuses element ids across snapshots (100%). Accuracy comes from
UI-Automation patterns, refusing ambiguous targets, DPI-space unification, normalized fallback
coordinates and loud failures. Works on Windows 10/11 with Python 3.11+; install with `setup.ps1`
then add the folder in DSH's plugin manager.
