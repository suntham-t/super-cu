# 最小化窗口 / 后台操作（1.3.0）

## 结论

**可以，但按动作类型分两类：**

| 动作 | 最小化窗口可用？ | 说明 |
|---|---|---|
| `invoke` `set` `toggle` `select` `expand` `collapse` | ✅ 默认自动后台 | 纯 UIA Pattern，不需要屏幕坐标，不还原窗口 |
| `minimize` `maximize` `restore` `close` `wait` `assert` `clipboard` | ✅ | 本来就不需要前台 |
| `click` `dblclick` `rightclick` `hover` `drag` `scroll` | ❌ | 需要真实前台 + 屏幕坐标；插件会先 `activate` 还原窗口 |
| `type` `key` `paste` | ❌ | 需要真实键盘焦点；窗口最小化时无法接收输入 |

## 用法

1. `cu_show` 的窗口行会带 `min`（最小化）或 `max`（最大化）标记，例如：
   `win #5705486 "微信" 219x30 @-31991,-32000 min`
   最小化窗口的 `rect` 是图标位置，**不要**用它算坐标。
2. 最小化窗口上执行 pattern 动作时**默认自动后台**（不还原窗口），步骤行会带 ` bg`：
   `1 ok invoke button:通讯录 invoke 566ms bg`
3. 也可显式强制：`{"op":"set","target":"edit:搜索","text":"x","background":true}`；
   整个批次可写 `{"background":true,"steps":[...]}`，单步 `background:false` 覆盖。
4. 输入合成类动作在 `background:true` 下会被拒绝（0ms，给出改写建议），
   去掉 `background` 后插件会自动还原窗口再执行。

## 应用差异（实测）

- **微信**：最小化后 UIA 树完整（116 节点），`invoke` / `setvalue` 都成功，窗口保持最小化；
  但微信的 UIA 提供者会**自己把窗口设为前台**（窗口仍最小化，键盘焦点可能被切走）。
  步骤行会追加 ` focus_stolen` 提示。
- **Windows 11 记事本**：最小化后内容子树不暴露（只剩 78 个节点、没有 edit），
  此时 `set` 会报"未找到目标"——先 `activate` 还原窗口再操作。
- **建议**：先 `cu_show` 看最小化窗口是否还有目标元素；没有就 `activate` 还原。

## 实现

- `bridge.snapshot` 增加 `minimized` / `maximized` 字段（呈现层显示 `min`/`max`）。
- `bridge.act(background=True)`：只接受 pattern 白名单；禁止 click 回退与 setvalue 的
  焦点/粘贴回退；动作后对比前后前台窗口，变化则返回 `focus_stolen`。
- `server.cu_do`：新增批次级/步骤级 `background`；最小化窗口上的 pattern 动作自动后台；
  对输入合成类动作给出明确错误与改写建议。
