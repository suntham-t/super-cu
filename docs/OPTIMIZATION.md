# super-cu 1.2.0 优化报告（性能轮）

- 日期：2026-10-27
- 范围：`src/bridge.py`、`src/server.py`、`tests/test_bridge.py`、`tests/test_server.py` + 基准/验证工具
- 未改动：`src/compact.py`、`src/windows.py`、`src/mock_bridge.py`、`src/dpi.py`、`src/gui_lock.py`（与 1.1.2 逐字节相同）
- 对外契约：工具 schema、`cu_show` 文本格式、`cu_do` 结果格式、快照字典字段、稳定 id 语义全部不变

## 0. 一句话结论

**冷启动缩短 ~70%，大窗口 `cu_show` 快 8.6 倍、中大窗口快 33-45%，热态端到端快 41%；长会话内存有界；呈现层 token 与 1.1.2 逐字节相同。**
所有数字来自同一台机器上的成对交织 A/B（`bench/opt/bench.py`），原始数据与复现命令见文末。

## 1. 改了什么

### 1.1 UIA 缓存请求快路径（`src/bridge.py`，主要收益）

原来每访问一个节点要付 ~9 次跨进程属性读取 + RawViewWalker 子节点调用（实测 1.5-2.0 ms/节点）。
现在一个窗口用**一次** `IUIAutomationElement.BuildUpdatedCache(RawViewCondition, TreeScope_Subtree)`
把整棵 raw 子树连同属性取回本地，后续 `GetCachedPropertyValue/GetCachedChildren` 都是本进程内存读取。

- 必须用 `RawViewCondition`（legacy walker 就是 RawViewWalker；Control View 会静默换树）。
- `RuntimeId` 无 Cached 属性，用 `GetCachedPropertyValue(RuntimeIdProperty)`；Value/TextPattern 走
  `GetCachedPattern` + `QueryInterface`，不可用时只对少量 edit/document 做实时回退。
- `Element.control` 在快照阶段保持 `None`（避免每节点一次 live `ControlType`），首次动作时才
  materialize 成 uiautomation Control 并做一次 live 探活；失效元素仍按 RuntimeId/语义重定位，绝不盲点。
- 兜底：任何缓存/COM 异常 → stderr 一行 `[supercu]` 日志 + 透明回退 legacy walk；连续失败的窗口 60s 内
  直接用 legacy；`BuildUpdatedCache` 超过 250ms 预算的窗口本窗口暂停快路径 60s。输出永远正确，只影响速度。
- 环境开关：`SUPER_CU_NO_CACHE=1`（0/false/no/off/空 = 开启）；`Bridge(use_cache=False)` 供测试。

### 1.2 冷启动：懒导入 + 握手后预热（`src/server.py`）

`initialize`/`tools/list` 是纯常量响应，不再导入 `uiautomation`（~300-500ms）与 Pillow（~40-200ms）：

- 后端模块在第一次工具调用时导入（错误照旧以可操作的提示返回）；
- 发完 `initialize`/`tools/list` 响应后，用**原始线程** `_thread.start_new_thread` 起预热线程导入模块
  （`threading.Thread().start()` 在负载高的机器上会阻塞主循环 100-150ms；原始线程启动 ~0.1ms）；
- 预热线程只 import、不碰 COM；`Bridge` 仍在请求线程构造。
- 环境开关：`SUPER_CU_NO_PREFETCH=1`（回到首次调用付导入成本的行为）。

**顺带修掉的 ship-blocker**：comtypes 在「首次 import 它的线程」里调用 `CoInitializeEx`。预热线程先导入
bridge 时，请求线程没有 COM apartment，第一次真实 `cu_show` 会稳定报
`OSError: [WinError -2147221008] 尚未调用 CoInitialize`。现在 `Bridge.__init__` 第一步在请求线程
显式 `comtypes.CoInitializeEx()`（每线程、幂等）；新增单元测试 + 真实进程回归测试（禁用修复时 3/3 失败）。

### 1.3 长会话内存上限（`src/bridge.py`）

- 元素 id 表（`_rid2id`/`_id2el`）上限 **20000**，按最近使用淘汰；id 单调递增、绝不复用；
  `by_id()` 永不抛异常，未命中提示改为「可能已被回收，先 cu_show 重新拿 id」。
- 窗口相关缓存（`_win_ctl`/`_win_info`/`_cache`）上限 **32**。
- 合成压测 200 张快照 × 400 个新元素 = 80000 个不同 id：旧版 80000/80000 条、RSS +45.0MB；
  新版封顶 20000/20000 条、RSS +12.4MB，最近 id 仍可解析。

### 1.4 cu_do 批处理：settle 自适应早退（`src/server.py`）

`settle_ms`（默认 120ms）仍是请求等待的上限，但只有同时满足以下条件的「已变化步骤」才可能提前结束：
`op` 是 `set`/`focus`、`via` 是同步 UIA pattern（setvalue/uia）而不是输入合成、该步没有 `expect`、
不是最后一步、两个相隔 12ms 的廉价 win32 探针（窗口矩形/标题/前台/窗口数）一致。
其余（click/key/type/paste/drag/scroll/launch/close…）仍完整睡 `settle_ms`。
`SUPER_CU_SETTLE_MIN_MS=N` 可强制盲等下限（回滚开关）。

### 1.5 测试与工具

- `tests/test_bridge.py`：+430 行（缓存逐字段 parity、开关、回退、内存上限、COM 线程、真实窗口 parity/marks）。
- `tests/test_server.py`：+31 项（懒加载、预热、COM race 真实进程回归、settle）。
- `bench/opt/bench.py`：可复现基准（冷启动/快照/呈现/截图/端到端/内存 + 成对交织 A/B + fixture）；
  `bench/verify/verify.py` + `phase_b.py`：独立 parity/协议/边界/性能门禁。
- 文档：`docs/OPT-BASELINE.md`（1.1.2 基线）、`docs/OPT-AFTER.md`（1.2.0 对照）、`docs/OPT-VERIFY.md`（验收）。

## 2. 证据（成对交织 A/B，n=5，2026-10-27 10:16，CPU 30.1%→22.5%）

| 指标 | 1.1.2 基线 | 1.2.0 | 变化 |
|---|---|---|---|
| 冷启动 spawn→initialize（真实后端，raw p50） | 859.7 ms | **255.8 ms** | **-70.8%** |
| 冷启动 扣除解释器地板（adjusted p50） | 668.4 ms | **33.5 ms** | **-95.4%** |
| 冷启动 mock raw p50 | 382.2 ms | 257.9 ms | -31.4% |
| `initialize`→`tools/list` | 0.2 ms | 0.2 ms | 0（预热不阻塞协议循环） |
| `tools/list`→首次 `cu_show`（预热已完成，空闲 ≥1.5s） | 480.2 ms | **12.2 ms** | -468 ms |
| `cu_show` 快照 Notepad（82 节点，ms/节点） | 1.923 ms（169.2 ms） | **1.043 ms（91.8 ms）** | -44.5% |
| `cu_show` 快照 Explorer System32（128 节点） | 1.702 ms（217.8 ms） | **1.008 ms（129.0 ms）** | -39.5% |
| `cu_show` 快照大 Chromium 窗口（214 节点） | 1.021 ms（218.4 ms） | **0.132 ms（28.3 ms）** | **-86.3%** |
| 同一大窗口 max_nodes=400（376 节点，bridge 侧 n=9） | 397.5 ms | **46.2 ms** | **8.6x** |
| 端到端热调用 `cu_show`（MCP 客户端墙钟） | 144.6 ms | **94.6 ms** | -40.6% |
| 呈现层 11 个 fixture×view 合计 | 30,313 字符 / 11,659 tok | **30,313 / 11,659** | 0（逐字节相同） |
| 截图（单窗口） | 43.3 ms / 181,469 B | 43.1 ms / 179,681 B | -0.4% / -1.0% |
| 200 次快照内存 id 表 | 63 / 63 | 63 / 63 | 0；RSS 持平 |

- 快照 `visited/shown` 在三个目标上两侧完全一致（Notepad 88/63、Explorer 128/90、大窗口 214/136）。
- 端到端输出字符 1868→1867（-0.1%，内容差异为隐藏计数），两侧 0 个 isError、无 `CoInitialize` 文本。
- 缓存回退分支（`SUPER_CU_NO_CACHE=1`）与基线统计等价：Notepad -5.5%、Explorer -8.6%、大窗口 +3.0%。
- 噪声地板（同代码 A/A 自检）：ms/节点 -5.6%..+1.6%、冷启动 adjusted -3.1%..-1.4%，上表收益均显著超出。
- `cu_do` 五步真实 Notepad 批处理（交错 A/B，n=5）：**1230.2 ms → 1035.6 ms（-194.6 ms, -15.8%）**，
  5/5 成功、输出逐步一致；纯输入合成批次（菜单）无变化。
- BuildUpdatedCache 分布：大 Chromium 36.4-41.6ms（376 节点）、Notepad p50 64.3ms、Explorer 137-141ms
  （4-agent 负载下有一次 347ms，由 250ms 预算 + 60s 暂停策略兜底；最终运行未触发）。

## 3. 正确性验收

- `tests/test_bridge.py` 真实套件 **55/55 PASS**（含缓存逐字段 parity、回退、marks、内存上限、动作 9/9）。
- `tests/test_server.py`（mock）**197 passed / 0 failed**，`--mock-only` 约 3s；`py_compile` 干净。
- 独立验证（`bench/verify/`，非实现者）：mock/真实快照 parity、渲染 golden、协议纯净度/错误码、
  API 无删除、COM race、冷启动、内存上限、stale-id/churn 全部 PASS，详见 `docs/OPT-VERIFY.md`。
- 工具 schema / `cu_show` 文本 / `cu_do` 输出格式未变；`compact.py` 与 1.1.2 逐字节相同。

## 4. 环境开关与回滚

| 开关 | 默认 | 作用 |
|---|---|---|
| `SUPER_CU_NO_CACHE=1` | 关 | 强制 legacy 逐节点 walk（等价 1.1.2 快照路径） |
| `SUPER_CU_NO_PREFETCH=1` | 关 | 关闭握手后模块预热（首次工具调用付导入成本） |
| `SUPER_CU_SETTLE_MIN_MS=N` | 未设 | 强制 cu_do 每步盲等下限，恢复旧的 settle 行为 |
| `PREFETCH_BRIDGE_MODULE=False` | True | 仅当 bridge 重新出现 import 期 COM 时改回（代码内常量） |

## 5. 残余风险

1. 缓存路径会构建**整棵 raw 子树**；极端巨大/动态窗口的单次 build 可能比 legacy 的有界 DFS 贵。
   目前最大实测 41.6ms（376 节点），并有 250ms 预算 + 失败窗口 60s 暂停 + 全量回退兜底。
2. 无子节点的窗口在首帧可能被健康检查判为可疑 → 该窗口 60s 走 legacy（输出正确，只是慢）。
   在 Calculator 启动瞬间观察到过一次。
3. 小窗口收益有限（Notepad ~1.7x，build 固定成本 ~64ms）；Chromium/大窗口收益最大（8.6x）。
4. 超过 20000 个不同元素后旧 id 会被回收，错误文案已说明「先 cu_show 重新拿 id」；id 永不复用。
5. 快路径复用 uiautomation 的私有 `_AutomationClient`（有独立 comtypes client 兜底，再不行回 legacy）；
   未来 uiautomation 升级可能让快路径自动失效，但不会破坏正确性。
6. 实时的流式窗口（聊天/计时器）两次快照间有非结构性差异；静态窗口实测 0 差异。

## 6. 复现

```powershell
# 基线（1.1.2 纯净副本）
python bench\opt\bench.py --src-dir D:\opencode\_supercu_opt\orig\src --label baseline --out bench\opt\baseline.json
# 成对交织 A/B（HEADLINE）
python bench\opt\bench.py --src-a D:\opencode\_supercu_opt\orig\src --src-b D:\opencode\plugins\super-cu\src --label after --cmp-rounds 5 --out bench\opt\compare-after.json
# 报告
python bench\opt\bench.py --report bench\opt\baseline.json bench\opt\compare-after.json --report-out docs\OPT-AFTER.md
# 测试
python tests\test_server.py --mock-only
python tests\test_bridge.py --unit-only
python tests\test_bridge.py            # 真实 GUI（Notepad/Calculator/Explorer，需 GUI 空闲）
python bench\verify\verify.py run      # 独立验证
```

## 7. 变更文件（相对 1.1.2）

| 文件 | 1.1.2 sha256_8 | 1.2.0 sha256_8 | 说明 |
|---|---|---|---|
| `src/bridge.py` | 5974FC39 | A9B18446 | 缓存快路径、内存上限、COM 线程修复、by_id 文案 |
| `src/server.py` | 44C0BB02 | E019E244 | 懒导入/预热、settle 自适应、1.2.0 版本号 |
| `tests/test_bridge.py` | — | 07E135F7 | +430 行缓存/内存/COM 测试 |
| `tests/test_server.py` | — | 2FE284F1 | +31 项懒加载/预热/COM/settle 测试 |
| `bench/opt/*`、`bench/verify/*` | 无 | 新增 | 可复现基准 + 独立验收工具 |
| `docs/OPT-*.md` | 无 | 新增 | 基线/对照/验收报告 |
