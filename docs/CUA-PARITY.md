# OpenAI CUA 对标（1.4.0）

> 阅读时间：2026-10-27。来源：OpenAI API 文档 *Computer use*（developers.openai.com，fetched markdown）、官方 openai-python 的
> `ComputerAction` 类型定义、以及 2025-01 的 CUA/Operator 发布页。结论落到了本插件的动作面、测试与文档。

## 0. 先回答：之前对标了吗？

**没有直接对标。** 1.2.0 之前的 `docs/RESEARCH.md` 只比较了 Windows-MCP、OmniParser/UFO、Anthropic computer-use、
browser-use、trycua/cua；OpenAI 的 CUA/Operator 与 2026 版 “Computer use” 工具没有进入研究清单。
本轮补齐了**文档层面对标**，并把动作面差异落成 **1.4.0 的代码**（见 §2/§3）。

## 1. OpenAI 现在怎么做 CU（2026 文档事实）

OpenAI 当前文档提供两条集成路径：

| 路径 | 形态 | 说明 |
|---|---|---|
| **Code execution harness** | 模型在持久浏览器/桌面里写并执行代码（PyAutoGUI / Playwright），用 `display(screenshot())` 看屏幕 | 示例模型名 `gpt-6-astra`；上下文视口 1440x900；PyAutoGUI fail-safe 保留 |
| **Responses computer tool + 应用自带 driver** | 模型输出结构化动作，应用实现 driver 执行并回传 `computer_screenshot` | 示例模型名 `gpt-6.1-sol`；driver 由应用负责会话/权限/清理；可 `beforeAction` 审批、`maxSteps` 限制 |

**driver 动作面（9 个）**，来自官方类型定义：

| OpenAI action | 字段 |
|---|---|
| `screenshot` | – |
| `click` | `x,y` + `button` ∈ left/right/wheel/back/forward + `keys`（按住） |
| `double_click` | `x,y` + `keys` |
| `drag` | `path`（任意多个坐标点） + `keys` |
| `keypress` | `keys`（组合键） |
| `move` | `x,y` + `keys` |
| `scroll` | `x,y` + `scroll_x/scroll_y` + `keys` |
| `type` | `text` |
| `wait` | – |

**安全形态**：driver 白名单/`refuse` 未授权动作、动作前 `beforeAction` 人工批准、步数上限、隔离环境、确认与同意规范。
（我们对齐的是 driver 这一层；模型侧规划/代码执行/VM 不在本插件职责内。）

## 2. 动作面映射（OpenAI → super-cu）

| OpenAI | super-cu（2.0 现状） | 备注 |
|---|---|---|
| `screenshot` | `cu_show {screenshot:true}` / `cu_do {screenshot:true}` | 截图默认只在需要时附；文本优先 |
| `click`（button/keys） | `{"op":"click","target":"@x,y","button":"back","modifiers":["ctrl"]}` | 1.4.0 新增 button 与 modifiers |
| `double_click` | `{"op":"dblclick","target":"@x,y"}` | 可选 modifiers |
| `drag`（path/keys） | `{"op":"drag","target":"@x,y","path":["@x2,y2","@x3,y3"],"modifiers":[...]}` | 1.4.0 新增 `path`（任意路径点）与 modifiers |
| `keypress` | `{"op":"key","keys":"ctrl+shift+t"}` | 组合键语法一致 |
| `move` | `{"op":"hover","target":"@x,y"}` | hover = 只移动不点击 |
| `scroll`（x/y + keys） | `{"op":"scroll","target":"@x,y","amount":3,"axis":"x"}` | 1.4.0 新增 `axis`；按键按住用 modifiers |
| `type` | `{"op":"type","text":...}` / `paste` / `set` | set 走 UIA ValuePattern，无焦点 |
| `wait` | `{"op":"wait","wait_kind":"text/element/window/idle/change/gone"}` / `assert` / `expect` | 语义更丰富 |
| （无） | `invoke/toggle/select/expand/collapse/focus/clipboard/activate/launch/close/minimize/maximize/window_move/scroll_to` | 语义层增值：pattern 优先、不抢焦点、最小化后台 |

**结论：9/9 动作面全部可表达。** 且额外具备 OpenAI driver 没有的语义目标（`#id` / `ct:名字`）、稳定 id、批量 fail-fast、
`expect`/断言、后台最小化操作。

## 3. 1.4.0 落地内容

- `src/bridge.py`
  - 鼠标键扩展：`_button_event()` 支持 left/right/middle（滚轮键）/back/forward（XBUTTON1/2），
    `MOUSEEVENTF_XDOWN/XUP` + mouseData 正确填充；
  - `_normalize_mods()` / `_mods_press()` / `_mods_release()`：指针动作期间按住 ctrl/shift/alt/win，
    顺序为 keydown → 动作 → keyup（单测校验顺序）；
  - `_scroll(axis="x"|"y")`：横向滚轮 `MOUSEEVENTF_HWHEEL` 或 ScrollPattern 横向；
  - `_drag(path=[...])`：任意路径点（CUA drag.path），保留原有标题栏严格校验与内容拖动心跳；
  - 所有新参数默认关闭/不影响旧调用。
- `src/server.py`：`cu_do` 步骤 schema 新增 `button/axis/path/modifiers`，预检阶段返回 `-32602`；
  坐标字符串支持 ` @x,y / @n:ux,uy / @img:x,y`。
- 测试：bridge 单元 14.6（按钮映射、修饰键规范化与按下/释放顺序、横向滚轮路由、路径解析）、
  mock 协议 CUA 段（schema/合法组合/非法值），真实 GUI 手测（见 §4）。
- 文档：本节 `docs/CUA-PARITY.md`，并在 `docs/RESEARCH.md` 增补 §6/引用。

## 4. 真机验证（本机，1.4.0）

| 场景 | 结果 |
|---|---|
| Explorer 双文件 ctrl+click 多选 | 动作 ok，选中状态由资源管理器维护（UIA 不暴露 selected 字段时无法回读，见残余差异） |
| Explorer 进入子目录后 **鼠标 back 键**（XBUTTON1） | 标题 `subdir - 文件资源管理器` → 回到父目录 ✅ |
| 横向滚轮 `axis=x` | 滚轮 x 3 格，动作 ok ✅ |
| Notepad 内容拖拽 `path` 4 个路径点 | 拖动 (277,304) → (720,322) path=4 pts ✅ |
| 中键点击 / ctrl+shift+点击 | 动作 ok，held=ctrl+shift ✅ |
| 回归 | 真实 bridge 套件 **57/57 PASS**、mock 216/216、bridge 单元 26/26、py_compile 干净 |

## 5. 基准/能力对标的现状（别把模型级和工具级混在一起）

- OpenAI 公布的 CUA 数字是**模型级 web 评测**：CUA（Operator，2025-01）OSWorld **38.1%**、WebArena **58.1%**、
  WebVoyager **87%**；2026 文档不再给该表，改为集成形态说明。这些数字衡量“模型自己看图决策”的能力，
  与一个**工具层插件**不可直接比较。
- super-cu 的可比指标是**工具层效率**：每步 token、观察/动作延迟、往返次数、id 复用率。
  现有证据：`docs/OPT-AFTER.md`（大 Chromium 窗口 `cu_show` p50 **28.3ms**、cold start **255.8ms**、
  `cu_show` 文本 30,313 字符与 1.2.0 逐字节一致）、100% id 复用、批量 fail-fast 把 N 个往返压成 1 个。
- **要真正做“GPT CUA vs DSH 模型 + super-cu”的端到端对标**，需要：OpenAI 模型访问、隔离桌面/浏览器、同一任务集、
  统计成功率/步数/token/人工干预率。本机当前没有 OpenAI CU 模型访问，因此本轮只做动作面与工具层指标；
  任务集与 harness 设计已列入下一轮候选（Notepad 保存、Calculator、Explorer 多选/导航、Office 保存、微信发送、多步混合）。

## 6. 刻意不照搬

1. **不内置规划器/视觉模型**：模型能力属于宿主（DSH）；插件只做高可靠驱动层。
2. **不做 VM/容器**：用户要操作真实桌面（Office/微信/登录态）；隔离环境是 OpenAI 的托管形态，不适合本场景。
3. **不做代码执行 harness**：DSH 已有 shell/Python 工具；在 CU 插件里再嵌 PyAutoGUI 运行时会扩大权限面且绕开
   `#id`/expect 的可验证性。
4. **不默认截图**：截图是 token/延迟大头；文本优先，视觉仅在 canvas/自绘界面兜底。

## 7. 引用

- OpenAI API 文档 *Computer use*：https://developers.openai.com/api/docs/guides/tools-computer-use
- openai-python `ComputerAction` 类型：https://github.com/openai/openai-python/blob/main/src/openai/types/responses/computer_action.py
- CUA / Operator 发布页（含基准数字）：https://openai.com/index/computer-using-agent/
- 本仓库对照证据：`docs/OPT-AFTER.md`、`docs/OPT-VERIFY.md`、`bench/opt/bench.py`
