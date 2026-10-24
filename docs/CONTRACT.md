# super-cu — 接口契约（唯一事实来源）

一个「超级快且准」的 Windows Computer-Use 插件：**UIA 语义优先 + 视觉兜底 + 批量动作 + 稳定元素 id**。
形态：DSH 的 MCP server 插件（一个常驻 Python 进程，stdio JSON-RPC），工具名 `mcp__supercu__cu_show` / `mcp__supercu__cu_do`。

## 0. 硬环境事实（实测，勿再猜）

| 项 | 值 |
|---|---|
| Python | `C:\Users\LENOVO\AppData\Local\Programs\Python\Python313\python.exe`（PATH 里 `python` 即是） |
| 已装 | `uiautomation`、`comtypes`、`Pillow`、`psutil` |
| Shell | PowerShell 5.1（`pwsh` 工具） |
| 屏幕 | 单屏 **2560x1600 @150% 缩放（logpixels=144）**，原点左上 |
| **DPI（硬要求）** | 进程必须 `from dpi import ensure_dpi_aware; ensure_dpi_aware()`（`src/dpi.py`，Lead 所有）在**任何** UIA/win32/PIL 调用之前执行。DPI-unaware 进程：UIA rect/GetWindowRect/GetCursorPos 是逻辑空间 1707x1067，而 ImageGrab 是物理 2560x1600，混用偏 1.5 倍。目标是 UIA rect == 截图 == 点击**同一物理空间**（awareness=2, GetSystemMetrics=[2560,1600]） |
| 控制台 | 中文必须 `sys.stdout.reconfigure(encoding="utf-8")`，JSON 用 `ensure_ascii=False` |
| UIA 实测 | 首次 `GetRootControl()` ~500ms（一次性）；单窗口 428 节点 walk 123ms + 属性 65ms；`RootElement.GetChildren()` 每次 ~420ms（**禁止每次调用**，窗口列表走 win32） |
| 截图实测 | `ImageGrab.grab()` 43ms，resize 22ms，JPEG q70 23ms → 1280 宽约 86KB |
| 基线竞争者 | `windows-mcp` 4.0.3（CursorTouch/Windows-MCP.exe），T3 实测 n=5、桌面 26 窗口：冷启动 6993ms；`Snapshot(use_vision=false)` p50 **801ms** / p95 1174ms，文本 p50 **32491 字符 / 39.8KB / ~10154 tokens**；`Screenshot` p50 534ms / ~400KB。我们的目标 = 延迟 ≤1/3、文本 ≤1/3 |

**禁止**：在每个调用里重新枚举 RootElement 子节点；禁止把整桌面 UIA 树一次性序列化。

## 1. 文件布局与 owner（写作用域互斥）

```
D:\opencode\plugins\super-cu\
  CONTRACT.md          lead  (本文件，只读)
  package.json         lead
  cordis.patch.yml     lead
  src\windows.py       T1  win32 窗口/焦点/输入合成
  src\bridge.py        T1  UIA 快照 / 稳定 id / 元素动作
  src\server.py        T2  MCP stdio JSON-RPC + 工具实现 + 状态
  src\compact.py       T2  快照→紧凑文本（token 预算）
  SKILL.md             T2  给 agent 的使用手册
  README.md            T2
  tests\test_bridge.py T1
  tests\test_server.py T2
  bench\               T3
```

## 2. GUI 互斥锁（三个 agent 共享同一桌面，必须遵守）

任何会**改变屏幕/焦点**的操作（click/type/key/activate/launch/drag/scroll）前，必须先拿锁，做完立即放：

```python
# src/gui_lock.py 由 T1 提供；没有它之前用同名文件锁约定
from gui_lock import gui_locked
with gui_locked():        # 阻塞等待，超时 30s
    do_gui_action()
```

锁文件 `D:\opencode\plugins\super-cu\.gui.lock`（`msvcrt.locking` 或 `O_EXCL` + pid 过期清理）。
纯观察（snapshot/screenshot）**不需要**锁。测试只准用 Notepad / 计算器 / 资源管理器，禁止操作用户正在看的 DSH 窗口与 Edge 标签页，结束前关闭自己打开的应用。

## 3. T1 → T2 的 Python API（bridge.py + windows.py）

所有返回 dict 均为纯 JSON 可序列化（无 UIA 对象外泄）。异常一律 `raise BridgeError(msg)`。

### 3.1 windows.py

```python
list_windows() -> list[dict]
# 用 ctypes EnumWindows（禁止 UIA RootElement.GetChildren），z 序从顶层开始
# [{"hwnd": 8788190, "pid": 1234, "proc": "msedge", "title": "...", "class": "Chrome_WidgetWin_1",
#   "rect": [l,t,r,b], "visible": True, "minimized": False, "focused": True, "z": 0}]
foreground_hwnd() -> int
find_windows(query: str, limit: int = 10) -> list[dict]
# query: "#8788190" 精确 hwnd / 标题子串（忽略大小写与不可见字符）/ 进程名 / 类名
# 排序：完全相等 > 前缀 > 子串；focused、visible、面积 加分
resolve_hwnd(query: str | None) -> int | None   # None => 前台窗口
activate(hwnd: int, wait_ms: int = 120) -> bool
# SetForegroundWindow 失败时 AttachThreadInput + BringWindowToTop + ShowWindow(SW_RESTORE)；成功=<IsWindowVisible 且前台>
get_rect(hwnd) -> [l,t,r,b]
wait_for_window(query: str, timeout_ms: int) -> dict | None
```

### 3.2 bridge.py

```python
class Bridge:
    def snapshot(self, hwnd: int, *, view: str = "controls", max_nodes: int = 220,
                 max_depth: int = 24, name_filter: str | None = None) -> dict
    # view: "controls"(只保留可交互+有意义的文本, 默认) | "text"(只文本/文档内容) | "all"
    # 返回:
    # {"hwnd":..., "title":..., "rect":[l,t,r,b], "focused":bool,
    #  "elapsed_ms": int, "total": int, "shown": int, "truncated": bool,
    #  "nodes": [node, ...]}
    # node: {"id": 123,                     # 稳定 int，见 3.3
    #        "ct": "button",                # 小写短名 button/edit/menuitem/tabitem/listitem/treeitem/
    #                                       #   checkbox/radiobutton/combobox/hyperlink/text/document/
    #                                       #   pane/window/image/menubar/toolbar/tab/group/scrollbar/other
    #        "name": "保存", "val": "abc"|None, "aid": "SaveButton"|None, "cls": "Button",
    #        "rect": [l,t,r,b], "enabled": True, "offscreen": False, "focused": False,
    #        "patterns": ["invoke","setvalue"], "kids": 3, "depth": 4,
    #        "path": "工具栏/编辑"}          # 祖先 name 面包屑，用 "/" 连接，最多 3 段

    def by_id(self, eid: int) -> "element|None"          # 从稳定 id 反查（可能已失效 => None）
    def resolve(self, spec: str, hwnd: int) -> tuple["element", dict]
    # spec: "#123" | "button:保存" | "edit:*搜索*"
    #       | "@n:ux,uy"(归一化 0..1，相对最近一次截图矩形 —— 视觉路径**推荐**)
    #       | "@img:x,y"(我们发出去那张图的像素) | "@x,y"(物理屏幕像素)
    # 返回 (element, {"via":"id|selector|point", "node": node, "candidates":[...]})
    # 歧义时 raise AmbiguousError(候选列表) —— 精度优先，绝不猜
    def act(self, hwnd: int, action: str, *, target: str | None = None,
            text: str | None = None, keys: str | None = None, value: str | None = None,
            amount: int = 0, at: str | None = None, to: str | None = None,
            timeout_ms: int = 3000, **_ignored) -> dict
    # action: invoke|toggle|select|expand|collapse|focus|setvalue|click|dblclick|rightclick|
    #         hover|type|key|scroll|drag|paste|activate|launch|close|minimize|maximize
    # 返回 {"ok":bool, "action":..., "via":"invoke|setvalue|click|keys|win32|uia",
    #        "ms":int, "detail":"人读一行", "changed":bool}
    # 语义要求（精度）：
    #  - invoke/setvalue/toggle/select/expand 优先 UIA Pattern（后台、不抢焦点、不移动鼠标）
    #  - Pattern 不可用才回退真实点击（先 activate，再元素中心点 click；双击按需）
    #  - setvalue 用 ValuePattern，失败回退 focus + Ctrl+A + 粘贴
    #  - type：优先 ValuePattern.SetValue 不行则剪贴板粘贴（长文本）/ SendKeys（短文本）
    #  - key：SendKeys 语义（"^s"、"enter"、"alt+f4"、"ctrl+shift+t" 都接受，内部转换）
    #  - 每次 act 后不要自动重拍快照（由 server 决定），但必须返回 changed 估计（可选 False）

    def screenshot(self, hwnd: int | None = None, *, region: list[int] | None = None,
                   max_width: int = 1280, quality: int = 70) -> dict
    # 返回 {"jpeg_bytes": bytes, "mime": "image/jpeg", "img_w":..., "img_h":...,
    #        "screen_w":..., "screen_h":..., "origin":[l,t], "rect":[l,t,w,h],
    #        "scale": screen_w/img_w,   # 物理空间（DPIAware 后 GetSystemMetrics(0)=img_w*scale）
    #        "space": "physical", "ms": int}
    # 判据：img_w * scale == GetSystemMetrics(0)（±1），origin 是截图左上角的**物理屏幕坐标**
    # hwnd 给定 => 裁该窗口矩形；region 优先；越界自动 clamp 到虚拟屏

    def wait(self, kind: str, *, target: str | None = None, text: str | None = None,
             value: str | None = None, hwnd: int | None = None,
             timeout_ms: int = 3000, interval_ms: int = 80) -> dict
    # kind: "text"(屏幕/元素名包含 text) | "element"(selector 出现) | "gone" | "window"(标题出现)
    #       | "idle"(UIA 树连续两次相同) | "change"(与调用前不同)
    # 返回 {"ok":bool, "ms":int, "detail":...}

    def last_shot_map(self) -> dict | None
    # {"origin":[l,t], "scale": 1.481481, "img_w":1728, "img_h":1080}  用于 @img:x,y
```

### 3.3 稳定 id（关键精度机制）

- `id` 由 UIA `RuntimeId` 映射到进程内自增 int，**同一元素在不同快照间保持同一 id**。
- 动作前若元素已失效（COM 异常 / IsOffscreen 变化 / hwnd 不匹配）→ 自动重扫该窗口一次，按 `runtime_id` 找同一元素再试一次；仍失败返回 `ok:false` 并给 `detail`，**不许盲点坐标**。
- `by_id` 必须容忍已释放元素（返回 None，不抛）。

### 3.4 测试（T1 交付）

`tests/test_bridge.py`，`python tests/test_bridge.py` 自跑，输出每项 PASS/FAIL + 耗时：
1. list_windows 覆盖当前前台窗口，`focused` 恰好一个；
2. snapshot(Notepad) 能拿到 menu/按钮，`total>0`；两次连续 snapshot 中同一元素 id 相同；
3. 打开 Notepad → `act(setvalue)` 写入文本 → 用 UIA 校验内容；`act(key)` 保存对话框出现；
4. `resolve("button:保存")` 歧义时抛 AmbiguousError（构造两个同名按钮的场景或跳过并说明）；
5. 截图：全屏/窗口裁剪尺寸与 scale 自洽（`img_w * scale == screen_w` ±1）；
6. 动作全部在 `gui_locked()` 内执行，测试结束关闭自己开的 Notepad。

## 4. T2：MCP server 契约

`src/server.py`，`python -u src/server.py` 直接跑。stdio 换行分隔 JSON-RPC 2.0（MCP stdio transport）：

- `initialize` → 回声客户端 `protocolVersion`（缺省 `"2024-11-05"`），`capabilities:{"tools":{}}`，`serverInfo:{"name":"supercu","version":"1.0.0"}`
- `notifications/initialized`、`notifications/cancelled` → 不回应
- `ping` → `{}`
- `tools/list` → 两个工具（schema 见 §5）
- `tools/call` → 返回 `{"content":[{"type":"text","text":...}]}`；截图时追加 `{"type":"image","data":<base64>,"mimeType":"image/jpeg"}`
- 错误：`{"code":-32602,"message":...}`（参数）/ `-32603`（内部），并在 text 里给可操作建议
- 任何异常不许打死进程；未知 method → `-32601`
- 每行一次 `sys.stdout.write(json.dumps(...)+"\n"); flush()`；日志写 stderr（`[supercu] ...`）
- **stdout 绝不允许出现任何非 JSON 内容**（包括 print 调试、库的警告）

服务端状态（进程内）：
- 最近一次快照（按 hwnd），元素 id 表由 bridge 管
- 最近一次截图映射（供 `@img:x,y`）
- 无状态化原则：`cu_show` 每次都可独立调用

## 5. 工具定义（面向前台模型，措辞要省 token、把规则写死）

### `cu_show` — 观察（默认纯文本，极快）

```json
{"type":"object","properties":{
 "window":{"type":"string","description":"窗口标题子串/进程名/#hwnd。省略=当前前台窗口"},
 "scope":{"type":"string","enum":["window","desktop"],"default":"window"},
 "view":{"type":"string","enum":["controls","text","all"],"default":"controls"},
 "filter":{"type":"string","description":"只保留名字匹配该子串的元素（忽略大小写）"},
 "max_nodes":{"type":"integer","default":200},
 "screenshot":{"type":"boolean","default":false,"description":"附一张 JPEG；仅在文本不足以判断时用"},
 "region":{"type":"array","items":{"type":"integer"},"description":"[x,y,w,h] 屏幕像素裁剪"},
 "max_image_width":{"type":"integer","default":1280}
}}
```
text 输出格式（T2 定稿，遵守）：
```
win #8788190 "题目收集平台…Edge" 2582x1550 @-11,-11 max [focus]
obs 428n/96 shown 132ms controls
[8712] button "保存" [invoke]
[8713] edit "搜索" val="dsh" focus
[8714] tabitem "对话" sel
```
- 首行窗口信息（desktop 作用域时列出前 12 个窗口，`#hwnd` 可点选）
- 元素行：`[id] ct "name"`，可选 `val=`, 状态 `[invoke,setvalue]`/`focus`/`sel`/`dis`(disabled)/`off`(offscreen)，缩进体现层级（每层 2 空格，最多 4 层），`kids=N` 当子树被裁掉时提示
- 名字里的换行压成 `⏎`，超 80 字符截断加 `…`
- 总文本硬上限 8000 字符，超出时按「交互元素优先、按屏幕位置」裁并写 `…(+N hidden)`

### `cu_do` — 动作（一次调用完成多步，减少往返）

```json
{"type":"object","properties":{
 "steps":{"type":"array","minItems":1,"items":{"type":"object","properties":{
   "op":{"type":"string","enum":["click","dblclick","rightclick","hover","focus","set","type","key","scroll","drag","select","invoke","toggle","expand","collapse","activate","launch","close","minimize","maximize","wait","assert","paste"]},
   "target":{"type":"string","description":"#id | 类型:名字 | @n:ux,uy（归一化 0..1，视觉路径推荐）| @img:x,y | @x,y"},
   "text":{"type":"string"},"keys":{"type":"string"},"value":{"type":"string"},
   "amount":{"type":"integer"},"to":{"type":"string"},
   "window":{"type":"string"},"app":{"type":"string"},
   "expect":{"type":"string","description":"动作后断言：文本/元素出现，如 '保存成功'"},
   "timeout_ms":{"type":"integer","default":3000}
 },"required":["op"]}},
 "observe":{"type":"string","enum":["auto","none","state"],"default":"auto","description":"auto=只在中途失败或末步可能改变界面时附带最新状态"},
 "screenshot":{"type":"boolean","default":false},
 "settle_ms":{"type":"integer","default":120}
},"required":["steps"]}
```
返回文本：
```
1 ok click #8712 invoke 11ms
2 ok set #8713 "dsh" setvalue 8ms
3 ok key enter keys 9ms
obs 96n/41 shown 118ms
[..] ...
```
失败即停：`3 FAIL click #9 detail="元素已失效：窗口 8788190 树已重建"`，并提示下一步建议（重新 `cu_show`）。
`op:"wait"` 的 target/text/expect 语义见 bridge.wait。

### 其他硬性规则（写进工具 description）
- 目标窗口不是前台时，`activate` 会自动做，不用单列一步。
- 聊天/发送/支付/删除等不可逆动作不做二次确认（DSH 层已有权限），但 description 要提醒模型在破坏性操作前用 `expect` 验证。
- 坐标只在 `view=text` 或 canvas/无 UIA 时才用；给了 `@img:x,y` 就绝不自己换算 scale。

## 6. T3：验证与基准

`bench/baseline.py`：直连 `C:\Users\LENOVO\.local\bin\windows-mcp.exe serve`（stdio JSON-RPC），测 `Snapshot`(use_vision=false) 与 `Screenshot`：
- 冷启动、p50/p95 延迟、返回字节数、token 估算（len/3.2）

`bench/supercu.py`：同一场景测 `cu_show` / `cu_do`：
- Notepad、资源管理器、Edge（只读：不点击用户页面）
- 报告：每步延迟 p50/p95、文本字节、动作成功率、id 复用率（两次快照同元素 id 相同比例）
- 端到端 10 个真任务（记事本输入保存、资源管理器导航、计算器算 7*8、Edge 地址栏搜索并读数、窗口最大化/切换等），逐条 PASS/FAIL + 总耗时
- 输出 `bench/report.md`（含与 windows-mcp 的对比表、结论、已知限制）

## 7. 验收门槛（Lead 最终检查）

1. `python -u src/server.py` 手工喂 initialize/tools/list/tools/call 三连全对，stdout 无非 JSON 噪声
2. `cu_show` 前台窗口 p50 < 267ms、p95 < 391ms（基线 Snapshot 801/1174ms 的 1/3 以内），文本 < 10830 字符（目标：Edge 15 标签页场景 < 3KB，比门槛好 3 倍）
3. `cu_do` 三步批量（打开记事本→输入→关闭对话框）单次调用成功，且总耗时 < 单次基线 Snapshot
4. 元素 id 跨快照复用率 > 90%；DPI 一致性：`img_w*scale == GetSystemMetrics(0)`（±1），snapshot 的 `space == "physical"`；`@n` 换算误差 ≤2px
5. 截图 ≤178ms 且 ≤133KB（基线 534ms / ~400KB 的 1/3）
6. 安装为 bundle 后新会话可见 `mcp__supercu__*` 两工具
