# super-cu 使用手册（给 agent）

Windows 桌面操作的首选工具：**先看 UIA 语义文本，再批量动作**。
比视觉快照快约 6-30 倍（大窗口最明显）、文本小约 10 倍，而且返回的 `id` 可以直接点。

工具名：`mcp__supercu__cu_show`（观察）、`mcp__supercu__cu_do`（动作）。

---

## 1. 基本循环：show → do → 验证

```
cu_show                                  # 看当前前台窗口（默认，纯文本，约 25-130ms）
cu_do {"steps":[...]}                    # 一次把 3-10 步做完
cu_show                                  # 只在需要确认新状态时再 show
```

- **文本优先**：只要元素有名字/类型，就用文本定位，不要截图。
- **截图只在文本不够时用**：canvas / 自绘界面 / 纯图片 / 需要看布局细节 / 文本里没有任何可用元素。
- **同一窗口连续 `cu_show` 的 id 不变**，直接复用，不用重新找。

## 2. 文本格式（一眼看懂）

```
win #8788190 "题目收集平台…Edge" 2560x1528 @0,0 [focus]
obs 312n/100 shown 219ms controls cut=budget(+42 hidden)
[8712] tabitem "对话" [s] *
 [8713] edit "搜索" val="dsh" [v] *
  [8714] button "保存" [i]
   ›工具栏 [8715] button "刷新" [i]
 …+9 more (filter: 会话)
 …(+42 hidden)
```

| 片段 | 含义 |
|---|---|
| `win #hwnd "标题" WxH @x,y [focus]` | 窗口与是否前台；`#hwnd` 可直接给 `window` |
| `obs 312n/100 shown 219ms controls` | 树的节点数（过滤前/后）/返回行数/耗时/视图 |
| `cut=tree` | UIA 节点树被有界 DFS 截断（`truncated` 字段，语义不变） |
| `cut=budget(+N hidden)` | **呈现层**因字符/行数预算裁掉了 N 条，正文末尾有 `…(+N hidden)` |
| `cut=tree,budget(+N)` | 两种同时发生：树截断 + 预算裁剪 |
| `[id]` | 稳定 id，`cu_do` 的 `target="#id"` 就用它 |
| `ct` | 元素类型：button/edit/menuitem/tabitem/listitem/treeitem/checkbox/combobox/hyperlink/text/document/pane… |
| `"名字"` | 元素名；换行显示为 `⏎`，超 48 字用 `…` 截断 |
| `val="..."` | 当前值（输入框/文档），已压缩 |
| `[字母]` | 能力，只显示最强的：`e` expand > `t` toggle > `s` selection > `v` value > `i` invoke > `c` focusable |
| `*` `+` `-` `~` | 状态：focus / selected / disabled / offscreen（可叠加，如 `*-`） |
| `kids=N` | 这棵子树被节点上限裁掉了 N 个子元素 |
| 行首空格 | 层级（1 空格/层，最多 3 层）；更深用 `›父名` 面包屑压平 |
| `…+N more (filter: kw)` | 同父同类的 `listitem/menuitem/text` 超过 12 个时只显示前 8 个，其余用这个标记；想展开就 `cu_show filter="kw"`。**标签页/树节点/按钮/复选框永不折叠**，直接列出 |
| `…(+N hidden)` | 超出文本/行数预算被裁掉的元素数 |

**预算裁剪顺序**：可见可交互 → **offscreen 可交互（仍保留、仍可点，只排在可见元素之后）** → disabled 可交互 → 可见 text → 结构容器 → offscreen 非交互。预算不足时先裁 offscreen/结构节点，不会先丢可见可交互目标。
`cut=budget` 就是告诉你"正文不是全部"——需要更多时用 `max_nodes` / `filter` / `view=text|all`。

**噪音过滤**（默认 `view=controls`）：窗口标题栏按钮、滚动条、重复的"影子 text"、滚出视口的 text、空名容器都会被去掉。
需要完整树时用 `view=all`。

> ⚠️ Chromium（Edge/Chrome）的 `view=text` 取不到 TextPattern（返回空）。读浏览器页面内容用默认 `controls`（文本元素的 name 就是内容）或 `screenshot=true`，别指望 `view=text`。

## 3. 常用参数

| 场景 | 调用 |
|---|---|
| 看前台 | `cu_show {}` |
| 指定窗口 | `cu_show {"window":"记事本"}` / `{"window":"#8788190"}` |
| 列窗口 | `cu_show {"scope":"desktop"}` → 用返回的 `#hwnd` |
| 只找某个东西 | `cu_show {"filter":"保存"}` |
| 看页面正文 | `cu_show {"view":"text"}` |
| 元素太多 | `cu_show {"max_nodes":60}`；或先 `filter` |
| 纯像素界面 | `cu_show {"screenshot":true}` |

默认文本预算：`view=controls` 约 3000-3500 字符、最多约 110 行（`max_nodes` 控制行数）；
`view=text/all` 或带 `filter` 时放宽到硬上限 10830 字符。

## 4. 截图与坐标（只在必要时）

`cu_show {"screenshot":true}` 会在文本后附加一张 JPEG，并给一行：

```
img 1728x1080 (screen 2560x1600) 86KB — 用 @n:0.42,0.31 归一化坐标指位置（服务端换算…）
```

**看到图片就用 `@n:ux,uy`（0-1 归一化，相对这张截图）指位置，不要自己算像素/缩放。**
其他 target 写法：

| target | 含义 |
|---|---|
| `#8712` | 稳定 id（最准，首选） |
| `button:保存` / `edit:搜索` | 类型+名字；`*关键词*` 也可 |
| `@n:0.42,0.31` | 归一化坐标（推荐给像素定位） |
| `@img:864,540` | 我们发出去那张图的像素 |
| `@x,y` | 物理屏幕像素（仅在 view=text / canvas 时） |

`region:[x,y,w,h]` 只截一块（物理像素，按请求尺寸 1:1 返回；此时 `max_image_width` 不生效）。

### 编号截图 `marks`（视觉兜底首选）

`cu_show {"screenshot":true,"marks":true}`（或只给 `marks:true`，会自动截图）会在元素上画红框并写 `[id]`，文本末尾提示：
`marks: 图中方框数字 = 元素 id，可直接用 #id 点击（已标 N 个）`。
看到方框里的数字就**直接用 `#id` 操作**，不要换算坐标；只有画不出框的 canvas/自绘区域才退回 `@n:ux,uy`。默认关闭，普通文本观察不受影响。

## 5. 批量动作（cu_do）

```json
{"steps":[
  {"op":"set","target":"#8713","text":"dsh","expect":"dsh"},
  {"op":"key","keys":"enter"},
  {"op":"click","target":"#8714"}
]}
```

- **顺序执行、失败即停**：失败会返回 `n FAIL op target detail="..."` 和一行 `→` 建议，未执行的步骤跳过。
- **目标窗口不在前台会自动激活**，不用单列 `activate`。
- `launch` 成功后，步骤行会带新窗口 `#hwnd`（如 `1 ok launch notepad win32 537ms #2629302`），后续步骤可以直接用 `#hwnd` 或省略 `window`（沿用上次窗口）。**Windows 11 记事本是单进程多标签**：launch 可能命中已有窗口（detail 里会写 pid/proc 匹配方式），这是正常行为，按返回的 `#hwnd` 继续即可。
- `key` 不加 `window` 时发给**当前前台**（例如上一步弹出来的保存对话框），不会把焦点抢回旧窗口。
- 关闭有未保存内容的窗口会弹保存对话框：`close` 失败时 detail 会提示阻塞，用 `key alt+n`（不保存）或 `enter` 处理后再继续。
- `observe`：`auto`（默认，仅在有变化/失败时附最新状态）、`none`（最省）、`state`（总是附）。
- `screenshot:true` 可在动作后附一张图（默认不附）。

### 常用 op

`click dblclick rightclick hover focus set type key paste scroll scroll_to drag select invoke toggle expand collapse activate launch close minimize maximize wait assert clipboard window_move`

- 写入文本用 `set`（优先 UIA ValuePattern，后台不抢焦点）；输入框追加用 `type`；快捷键用 `key`。
- 滚动用 `scroll` + `amount`（可负），别去点滚动条；**把元素滚进视口用 `scroll_to` + target**（ScrollItemPattern，滚不动会显式报错，不会假装成功）。
- `drag` 的 `to` 可直接写元素：`{"op":"drag","target":"#A","to":"#B"}` 或 `"to":"ct:名字"`，服务端取元素中心，不用自己算坐标。

### 剪贴板：`clipboard` op（显式读写）

```json
{"op":"clipboard","mode":"set","text":"要放进剪贴板的内容"}
{"op":"clipboard","mode":"get"}
```

- `set`：写剪贴板并回读校验；**会覆盖用户剪贴板**，只在确实需要搬运数据时用。
- `get`：把剪贴板文本放进结果：`clipboard: "..."`（换行显示 `⏎`，超长截断 800 字符）。
- `set` 缺文本 → `-32602`；读失败（被占用/非文本）→ 明确报错，不静默。
- 往输入框粘文本仍然优先 `paste`（它有"写剪贴板→回读→SendKeys 兜底"的完整链路）。

### 窗口几何：`window_move`

```json
{"op":"window_move","window":"#1234","x":100,"y":80,"w":1200,"h":800}
```

移动到物理像素位置（`w/h` 省略=保持；多屏坐标可为负；maximized 先还原）。比拖标题栏稳定，适合多显示器/固定布局任务。

### `paste`：剪贴板粘贴（必须给文本）

```json
{"op":"paste","text":"要粘贴的多行/长文本…"}
```

- **`text` 或 `value` 至少给一个**（`value` 是别名）；两个都不给会直接返回 `-32602` 参数错，**不会**盲发 Ctrl+V 把用户旧剪贴板贴进去。
- 语义：服务端把文本交给 bridge → **写剪贴板 → 回读校验 → 校验失败自动 SendKeys 逐字兜底**。所以长文本/多行/中文优先用 `paste`，比 `type` 稳。
- 步骤行 `via` 告诉你实际走了哪条路：`paste`/`clipboard` = 剪贴板写入并已回读校验；`sendkeys` = 剪贴板失败后的逐键兜底（更慢，极端长文本可能丢字符，尽量别依赖）。
- 粘贴目标：`paste` 发给**当前前台控件的焦点**；要粘贴到某个输入框，先在同一步或上一步给 `target` + `focus`/`click`。

### 键名清单（`keys` 参数）

| 写法 | 含义 | 别名 |
|---|---|---|
| `^s` `^c` `^v` `^a` `^z` `^y` `^f` `^n` `^o` `^p` `^w` | Ctrl+字母 | 也接受 `ctrl+s` |
| `+a` / `%{F4}` | Shift / Alt 修饰 | 也接受 `shift+a`、`alt+f4` |
| `enter` `tab` `esc` `space` `backspace` `delete` | 基本键 | `return`=enter、`del`=delete、`escape`=esc |
| `up` `down` `left` `right` `home` `end` `pageup` `pagedown` | 导航键 | – |
| `f1`…`f12` | 功能键 | – |
| `alt+f4` `alt+tab` `alt+n` `ctrl+shift+t` `ctrl+shift+s` | 组合键 | 推荐用完整 `ctrl+…` 写法 |
| `{TAB}` `~` | SendKeys 原生转义 | 一般直接用上面的名字即可 |

写不出来时优先拆成 `click`/`set`，别硬猜键名；报 `unknown_key` 时按表改写法。

### 最小化窗口 / 后台模式（1.3.0）

- 最小化窗口上 `invoke / set / toggle / select / expand / collapse` 默认自动**后台执行**（不还原窗口），步骤行带 ` bg`；
  也可显式 `background:true`（步骤或整批），单步 `background:false` 覆盖。
- `click / key / paste / drag / scroll / type` 必须真实前台：给 `background:true` 会直接报错（附改写建议），
  去掉 `background` 插件会先 `activate` 还原窗口再执行。
- `cu_show` 窗口行带 `min` 标记；最小化窗口的 rect 不可用于坐标。应用是否暴露子树不同：
  微信最小化可操作（但会标 `focus_stolen`），Win11 记事本最小化需先 `activate`。

### 视图切换后的状态检查（1.3.1）

- 点开聊天/切换页面这类**换视图的点击**之后，`observe=state` 的内联状态可能比界面晚 ~250ms（应用重绘需要时间）。
  super-cu 1.3.1 会在改动过的步骤后先等 150-250ms 再截状态；更慢的应用（微信实测 ~300ms）仍可能滞后。
- 判断"有没有切换成功"不要只看 `expect` 命中（旧视图里的同名文本/search 框也会命中）：
  关键切换后如果状态看起来没变，**直接再 `cu_show` 一次**确认，不要重复搜索/重新点击。

### 坐标/鼠标动作（CUA 兼容，1.4.0）

- `click` 可指定 `button`: `left/right/middle/back/forward`；指针动作可用 `modifiers:["ctrl","shift"]` 按住执行。
- `scroll` 可指定 `axis:"x"` 横向滚动；`drag` 可给 `path:["@x1,y1","@x2,y2"]` 走任意路径（此时 `to` 可省略）。
- 这些字段只影响坐标/鼠标动作；语义动作（invoke/set/…）与最小化后台模式不受影响。

### 跨窗口拖拽 / 滚动到元素（1.5.0）

- 元素不在可视区（微信/Electron 自绘列表常见）：先 {"op":"scroll_until_visible","target":"#123","window":"#hwnd"}；
  真实滚轮 + rect 判定，不依赖 UIA ScrollPattern。
- 跨窗口拖放：{"op":"drag","target":"#file","to":"#composer","to_window":"#other","auto_scroll":true}；
  to 在 to_window 里解析成元素中心，drop 后返回 drop_window=#.. pid=.. class=..。
- 起点在窗口外 / 落点不在目标窗口会直接失败（不再静默 ok）；落点被别的窗口接走也会报错。
- cu_show 现在按 rect 与窗口交集标 ~（屏幕外）；看到 ~ 不要点/拖，先 scroll_until_visible。
- 内联状态不再固定等 150ms：只有看起来没变且这步是换视图类时才等一次重试。

## 6. 用 expect 验证（重要操作必做）

`expect` = 动作后等待某段文本/元素名出现（最多 `timeout_ms`，默认 3000）：

```json
{"op":"click","target":"#提交","expect":"提交成功"}
```

- 发送消息、删除、支付、保存文件、提交表单**之后**用它确认结果，再决定下一步。
- 断言失败不重试动作，只报 `FAIL` + 建议。
- 想显式等待/确认，可以用 `wait`/`assert`，并用 `wait_kind`（别名 `kind`）指定语义：
  - `text`：窗口标题或元素名包含某段文本（`text`/`expect`）
  - `element`：某个 target 出现（`target`）
  - `window`：某个标题的窗口出现 —— **`window` 和 `text` 二者皆可，`window` 优先**：
    `{"op":"wait","wait_kind":"window","window":"PowerPoint"}`
    或 `{"op":"wait","wait_kind":"window","text":"演示文稿1"}`
  - `idle`：UIA 树稳定；`change`：与调用前不同；`gone`：元素/窗口消失（`target`/`window`）
  - 成功时步骤行会带命中详情，例如
    `1 ok wait window 1ms 窗口已出现 #80090768 '演示文稿1 - PowerPoint'`，不用再 `cu_show` 一次。
  - 不给 `wait_kind` 时沿用推断（有 text/expect → text，有 target → element，否则 idle）。
- `cu_do` 内联状态（observe）与 `cu_show` 同一预算：controls 约 3500 字符 / ≤110 行；
  观察本身失败只回一行 `obs skipped: <原因>`，**不代表动作失败**。

## 7. Office（PowerPoint / Word / Excel）保存与打开对话框（必读）

现代 Office 的「保存此文件」/「打开」是**云对话框，不接受在文件名框里填全路径**——填了之后「保存」按钮会变灰/无效（踩过的坑）。正确路径：

1. **首选：「更多选项…」→「浏览」** 打开经典「另存为」对话框，再在经典对话框里填路径：
   ```
   key ctrl+shift+s                         # 或者 click 文件 → 另存为
   click button:更多选项…
   wait {"wait_kind":"window","text":"另存为"}
   set  #文件名框  "D:\docs\汇报.pptx"
   click button:保存
   ```
2. 已经在**经典另存为**对话框里：`set` 文件名框为**完整路径**再 `click 保存`（经典对话框支持全路径）。
3. 弹「保存此文件」时，文件名框只接受**纯文件名**；要换目录必须点「浏览」或左侧位置列表。

判断自己在哪个对话框：`cu_show` 看标题/按钮——现代对话框有「更多选项…」，经典对话框有地址栏 + 文件名 + 保存类型。保存后用 `expect`/`wait_kind window` 确认新标题，或 `cu_show` 看标题是否还有 `*`。

### 实战规则 A：Office 是 SDI 多窗口，先选窗口再操作

Excel/Word/PowerPoint 每打开一个文档就是一个**独立顶层窗口**，同进程、标题高度相似（多个「工作簿1 - Excel」）。操作前：

1. `cu_show {"scope":"desktop"}`；
2. 找目标行：同 pid 多个窗口会标 `[同进程 xN]`，结尾有「可用 #hwnd 精确指定」提示；
3. 后续所有步骤都带 `window:"#hwnd"`（或第一条 `cu_do` 里先 `activate` 该 `#hwnd`，之后省略 `window` 会沿用）。
不要只靠标题子串选窗口——会把内容写进另一个工作簿。

### 实战规则 B：Excel 定位精确区域用 Ctrl+G，保存用 F12

- **不要**用 `set` 写 Excel 的「名称框（Name Box）」来跳转：它经常不接受 `set` 的 ValuePattern，写进去不生效或错位。正确做法：`key "^g"` 打开「定位」对话框 → `wait {"wait_kind":"window","text":"定位"}` → 在「引用位置」里 `set "B12"` 或区域 `A1:D20` → `enter`。
- 选中区域后写值用 `paste`（多行/多列拆成 `\t`/`\n` 一次贴），别逐格点。
- 保存用 `key "f12"`（另存为）或 `key "^s"`；F12 在部分版本被占用时回到 §7 主流程（更多选项→浏览）。
- 改完用 `expect`/`cu_show` 确认单元格内容（`view=controls` 里 text 元素会显示值）。

## 8. 失败与重试规则

| 现象 | 处理 |
|---|---|
| `FAIL` detail 含"不存在/已失效/过期" | 元素树重建过：**重新 `cu_show` 拿新 id**，用新 id 重试 |
| detail 含"歧义/候选" | target 命中多个：改用 `#id`，或写全名 `button:完整名字` |
| detail 含"窗口"/"没有找到窗口" | 窗口关了/标题变了：`cu_show scope=desktop` 看窗口，用 `#hwnd` |
| 等锁/GUI 占用 | 别的程序正在操作桌面：稍等重试，别并发乱点 |
| detail 含 `disabled` / "禁用" / "不可用" | 控件当前不可用（文本里状态带 `-`）：换一个可用控件，或先做前置动作（选中/勾选/等加载），别反复点 |
| detail 含 `clipboard` / "剪贴板" | 剪贴板被占用或写入失败：改用 `op=type`（分块写入），或稍后重试 `paste` |
| detail 含 `unknown_key` / "未知按键" | 键名不支持：按上面「键名清单」改写，或改用 `click`/`set` |
| detail 含 `pattern` / "不支持该" | 该控件不支持所需 UIA Pattern：换 `click`/`set`/`type`/`key` 等价动作 |
| 断言等不到 | 先 `cu_show` 看界面到底变没变；确认该变再加大 `timeout_ms` |
| 文本里没有可用元素 | 可能是 canvas：`screenshot=true`，改用 `@n:ux,uy` |

**不要盲点坐标**：`@x,y` 只在没有 UIA 的界面用；有 id 就用 id。
不要在一步失败后继续猜着往下点——先观察，再重试。

## 9. 成本意识

- 一次 `cu_show` 的文本约 0.5-3KB，**别在循环里反复 show**；一次 `cu_do` 尽量多做几步。
- 需要反复看同一窗口时，用 `filter` 或 `max_nodes` 缩小范围。
- 截图约 0.5-1.5K token 等价成本且更慢，确认文本里真的没有信息再用。

## 10. 已知限制

- 自绘/canvas/游戏界面没有 UIA 元素 → 只能截图 + `@n` 坐标。
- Chromium（Edge/Chrome）的 `view=text` 为空：读页面用 `controls` 视图。
- 密码框、部分受保护（UIPI/管理员）窗口无法读取或操作。
- UWP/商店应用（计算器、设置）首次枚举会慢一点（约 0.3-0.5s）。
- Windows 11 记事本是单进程多标签，`launch` 可能复用已有窗口：按返回的 `#hwnd`/detail 继续。
- `close` 遇到未保存内容会弹保存对话框，用 `key alt+n`（不保存）或 `enter` 处理。
- 剪贴板被别的程序占用时 `paste` 可能失败 → 自动降级 SendKeys（更慢、极端长文本可能丢字符），失败时按建议重试。
- 现代 Office「保存此文件」对话框不接受全路径：必须走「更多选项…→浏览」。
- 折叠只发生在 `listitem/menuitem/text`；被折叠项可用提示里的 `filter: kw` 或 `view=all` 取回，关键词是尽力而为。
- `@n`/`@img` 永远相对**本进程最近一次成功截图**；换窗口后先重新截图再指坐标。
