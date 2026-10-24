# super-cu 竞品/同类项目研究与采纳清单（B8）

生成时间：2026-10-24 · 作者：T2 · 全部结论都落到 `src/server.py` / `src/compact.py` / `src/mock_bridge.py` 的具体 op 与 schema。

## 0. 一句话结论

同类项目分两条路：**Windows-MCP 走"OS 工具全家桶"**（PowerShell/文件/注册表/剪贴板/App 一起给），**OmniParser/UFO/computer-use 走"截图→结构化标注→坐标动作"**。super-cu 的差异化是"UIA 语义优先 + 稳定 id + 文本极小"，所以本轮只吸收**不引入视觉模型、不引入外部运行时**的交互层能力：编号标注、剪贴板、窗口几何、把坐标动作升级为元素目标、多窗口消歧。OS 工具、视觉模型、规划器、虚拟机全部明确拒绝。

## 1. 能力对照表

| 能力 | Windows-MCP 4.0.3（本机实测基线） | OmniParser / UFO | Anthropic computer-use | browser-use | super-cu（本轮后） |
|---|---|---|---|---|---|
| 观察格式 | `Snapshot`：**整桌面** UIA 树文本，p50 32.5K 字符/39.8KB/~10K tokens；`use_annotation` 可叠加编号框 | 截图→检测可交互区域+图标 caption，输出结构化元素列表 | 只看截图（像素计数点坐标） | DOM/可访问性树 + 截图，元素带 `[index]` | `cu_show`：窗口 UIA 树→紧凑文本 p50 1.8-3.5K 字符；`marks` 可选编号截图（**本轮新增**） |
| 元素标识 | 无稳定 id（按坐标/标签） | 视觉检测框编号（每次重算） | 无 id，纯像素 | DOM index（页面刷新即失效） | **稳定 int id**（RuntimeId 映射，跨快照 100% 复用）；`marks` 把 id 画到图上（**本轮**） |
| 点击/输入 | Click/Type/Shortcut/MultiEdit（坐标或 label） | 坐标 | click/type/key/drag/scroll | DOM 事件 | `cu_do` 批量 op：click/type/key/paste/set…，失败即停 |
| 拖拽 | Move（from_loc→loc，可 label） | 坐标 | left_click_drag | 拖拽 API | `drag`；**to 支持 `#id`/`ct:名字`（本轮）** |
| 滚动 | Scroll（方向/轮次/loc） | 坐标 | scroll（方向+像素） | 滚动 API | `scroll`(amount) + **`scroll_to`(元素滚入视口，本轮)** |
| 等待/断言 | Wait（固定 sleep）、WaitFor（条件/DOM） | — | 每步后模型自己看截图 | 页面状态等待 | `wait`/`assert` + `wait_kind`（text/element/window/idle/change/gone）+ `expect` |
| 剪贴板 | `Clipboard`（get/set） | — | 无（靠键盘） | 无 | **`clipboard` op（get/set，本轮）** |
| 窗口管理 | App（launch/resize/move/switch）、DisplayInventory | — | 无 | 无（浏览器内） | `activate/close/minimize/maximize` + **`window_move`（本轮）** + desktop 多窗口提示（**本轮**） |
| OS 工具 | PowerShell/FileSystem/Registry/Process/Notification/Scrape | — | 无 | 无 | **不做**（DSH 已有 shell/文件工具，见 §3 R2） |
| 视觉兜底 | Screenshot + annotation | 纯视觉模型 | 仅视觉 | 截图 + DOM | 截图 + `@n:ux,uy` 归一化坐标 + **marks 编号** |
| 规划 | 无（工具集） | UFO 多 agent 规划器 | 无 | 内置 agent loop | 不做（DSH 层负责，见 R3） |

数据来源：本机 `bench/baseline.json` 实测（Windows-MCP 4.0.3 的 20 个工具与 Snapshot 尺寸）＋各项目公开资料（见文末引用）。

## 2. 本轮采纳（5 条，全部 opt-in、默认行为不变）

### A1. `cu_show marks`：编号标注截图（← OmniParser / Windows-MCP `use_annotation`）
- 动机：视觉兜底时模型看不到 id，只能算坐标；OmniParser 证明"检测框+编号"能显著提升 grounding，而 Windows-MCP 也已提供 `use_annotation`。
- 落地：`cu_show` 新增 `marks: boolean=false`；开启时强制附截图，并在 JPEG 上用元素 rect 画框+写 `[id]`（服务端 Pillow 标注，桥不支持时降级为无标注但保留文本提示）；文本追加一行"图中方框数字 = 元素 id，可直接用 #id 点击"。
- 边界：默认关闭（标注多花 ~20-40ms 且增加模型看图成本）；`desktop` 作用域无窗口元素时不标注。

### A2. `cu_do clipboard`：剪贴板读写（← Windows-MCP `Clipboard`）
- 动机：读剪贴板（核对复制结果/提取内容）与写剪贴板（跨应用搬数据、再 Ctrl+V）是真实高频缺口；此前只能靠 paste 间接写。
- 落地：新 op `clipboard`，`mode: get|set` + `text`；`get` 把内容放进结果文本（压缩到 800 字符、换行转 `⏎`），`set` 写剪贴板并回读校验；`set` 缺文本 → `-32602`；复用 `windows.get_clipboard_text/set_clipboard_text` 的重试逻辑。
- 语义：`set` 是全局副作用（会覆盖用户剪贴板），因此只在显式 op 中执行；`paste` 仍保留"写剪贴板→回读→SendKeys"的隔离。

### A3. `cu_do window_move`：窗口几何（← Windows-MCP `App resize/move`）
- 动机：多显示器/DPI 场景下把窗口摆到固定位置再操作，比拖标题栏稳定；Windows-MCP 有 `window_loc/window_size`，我们缺。
- 落地：新 op `window_move` + `x,y,w,h`（w/h 可省略=保持）；复用 `windows.move_window`（虚拟屏 clamp、maximized 先 restore）；持有 GUI 锁执行；返回新 rect。

### A4. `drag.to` 接受元素目标 + `scroll_to`（← computer-use drag/scroll + Windows-MCP `Move(label)`）
- 动机：拖到"某个元素上"和"把元素滚进视口"是坐标猜测的重灾区；元素目标能把误差降到 0。
- 落地：`drag.to` 支持 `#id` / `ct:名字`（服务端 resolve 成元素中心 `@x,y` 再交给 bridge；歧义时报错不猜）；新 op `scroll_to` 透传 bridge 的 ScrollItemPattern 实现（父容器 ScrollPattern 兜底失败会显式报错，不假装成功）。
- 召回：都不改默认路径，`to="@x,y"` 行为不变。

### A5. `cu_show scope=desktop` 同进程多窗口提示（← Office SDI 现实 + Windows-MCP 显式 hwnd 选择）
- 动机：Excel/Word 是 SDI，多个窗口同进程同标题；只按标题选窗口会选错。办公场景是核心用例。
- 落地：`desktop` 视图对 `pid` 相同的窗口标 `[同进程 xN]`，结尾提示"该应用有多个窗口，可用 #hwnd 精确指定"；不改 `list_windows` 排序与默认输出行数。
- 配套：SKILL 增加 Office SDI 规程与 Excel `Ctrl+G`/F12 规则。

## 3. 明确拒绝（不是"以后再说"，是本轮不做且说明理由）

### R1. 引入视觉模型/OmniParser 式解析（拒绝）
- 理由：需要 GPU/大模型依赖与每次 200-800ms 推理，直接毁掉"比视觉快照快 4 倍"的定位；且 UIA 元素在 Office/Browser/Win32 上的 name/type 已经比 caption 更准。
- 替代：`marks` 只做"把已有 id 画到图上"，零模型依赖、可选。

### R2. 把 PowerShell / FileSystem / Registry / Process / Notification / Scrape 做成 cu 工具（拒绝）
- 理由：DSH 已有 shell/文件/进程工具；在 CU 插件里重复实现会扩大权限面（注册表/任意 shell），也违背"一个工具做一件事"。
- 替代：需要 OS 级操作时让模型用 DSH 原生工具，CU 只负责界面。

### R3. 内置规划器 / 多 agent（UFO 式 HostAgent+AppAgent）（拒绝）
- 理由：规划属于上层 agent（DSH）职责；插件里再嵌一层会让每步多一次 LLM 往返，token/延迟翻倍，且状态难同步。
- 替代：`cu_do` 批量 + `expect`/`wait_kind` 已把"观察-动作循环"压到最少往返。

### R4. VM/容器沙箱（trycua/cua 的核心形态）（拒绝）
- 理由：用户要操作的是**真实桌面**（Office、微信、真实登录态）；VM 无法满足，还引入镜像/虚拟化依赖。
- 替代：进程级安全边界 + 显式 opt-in 的破坏性动作确认（DSH 权限层）。

### R5. Chromium CDP/DOM 通道（browser-use 式 `use_dom`）（拒绝）
- 理由：需要以 `--remote-debugging-port` 重启浏览器（会打断用户会话），且只覆盖浏览器；UIA Chromium 树已能拿到按钮/文本。
- 替代：`controls` 视图 + `view=text`（对 Chromium 用 controls 的 text 节点）。

## 4. 与基线的取舍（为什么不学 Windows-MCP 的"全家桶"）

- Windows-MCP 的 Snapshot 是**整桌面**树（p50 32.5K 字符），一次观察就吃掉 ~10K tokens；我们窗口级 + 紧凑层把同任务压到 1.8-3.5K 字符，这是核心优势，不能为了"功能多"退回去。
- 它没有稳定 id；我们的 id 复用率实测 99.8-100%，这是连续多步任务的关键。
- 但它有三点值得学且已采纳：`use_annotation`（→marks）、`Clipboard`（→clipboard op）、`App` 的窗口几何/显式 hwnd（→window_move + 同进程提示）。
- 其余 OS 工具/Scrape/Registry 属"任务域工具"，不是 CU 观察/动作原语，保持拒绝。

## 5. 引用

- CursorTouch/Windows-MCP（工具清单与本机基线数据）：https://github.com/CursorTouch/Windows-MCP
- Microsoft OmniParser（截图→结构化元素/图标 caption）：https://arxiv.org/abs/2408.00203
- Anthropic computer use（screenshot / click / type / key / scroll / wait、像素计数与 flipbook 局限）：https://www.anthropic.com/news/developing-computer-use ；工具文档：https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- browser-use（DOM/可访问性树 + 截图双通道、index 元素）：https://github.com/browser-use/browser-use
- trycua/cua（VM/容器化 computer-use 框架）：https://github.com/trycua/cua
- UFO / WindowsAgentArena（UIA+视觉混合、多 agent 规划）：https://github.com/microsoft/UFO ；https://github.com/microsoft/WindowsAgentArena
- 本机实测数据：`bench/baseline.json`（Windows-MCP 4.0.3，20 工具、Snapshot p50 32.5K 字符）、`bench/matrix.json`（super-cu 应用矩阵）。
