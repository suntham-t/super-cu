# super-cu 优化轮 - 独立验证报告（verifier / 最终版）

> **状态：FINAL。结论：SHIP（可以放行 1.2.0）。**
> 本报告由独立验证者 verifier 产出；验证者只读 src/ 与 tests/，只写
> bench/verify/** 与本文档。所有数字均来自磁盘上的证据文件（见 §10）。

- 基线（A）：D:/opencode/_supercu_opt/orig/src（2026-10-27 00:11 抓取）
- 候选（B）：D:/opencode/plugins/super-cu/src（1.2.0，最终冻结 hash）
- 最终门禁：bench/verify/out/phase_b_phaseB3.json -> **11 PASS / 0 FAIL / 0 SKIP**

## 1. 最终冻结指纹

| 文件 | sha256(前16) | 说明 |
|---|---|---|
| src/bridge.py | **a9b18446** | 缓存快路径 + MAX_IDS/MAX_WINDOWS + _ensure_com |
| src/server.py | **e019e244** | 懒加载 + prefetch + 自适应 settle + SERVER_VERSION 1.2.0 |
| src/compact.py | **8d0e328c** | 与 pristine 完全一致（本轮未改） |
| src/windows.py | c07bb57a | 未改 |
| src/mock_bridge.py | 4ec91bbd | 未改 |
| src/dpi.py / src/gui_lock.py | 64d1e223 / 479e6a70 | 未改 |
| package.json | 1.2.0（f676cd36） | 与 SERVER_VERSION 一致 |
| cordis.patch.yml | marker 3（ea2ad153） | 构建标记 |

## 2. 验证方法（可复现）

- **物理隔离**：两棵树从不在同一解释器导入。每一侧由 bench/verify/_vcap.py
  子进程加载（该侧 src 唯一在 sys.path 首位，并断言 module.__file__ 位于该
  src 下），结果为纯数据后比较。
- **id 归一化**：进程内自增 id 先按"首次出现的 RuntimeId"映射再比较；对
  by_id/动作解析另有同进程 id 稳定性检查。elapsed_ms 在渲染 golden 中清零。
- **负载协变量**：机器由 4 个 agent 共享；冷启动使用配对交错采样 + 中位数，
  并同时测 python -c pass 解释器基线；快照以 **ms/visited-node** 作为负载
  鲁棒指标；AFTER 由 profiler 的配对 A/B（n=5）提供并与验证器独立数据交叉验证。
- **真实 GUI 规则**：只用记事本/计算器/资源管理器；由插件 bridge 打开/关闭
  （bridge.act 自持 gui_lock），快照只读；不触碰用户 Edge/微信/DSH 窗口；
  收尾验证无残留窗口（见 §7）。
- **对抗性**：畸形输入、空树、非 ASCII/引号/换行标题、超长文档、关闭/复用窗口、
  陈旧 #id、缓存注入失败、SUPER_CU_NO_CACHE=1 / NO_PREFETCH 开关、内存上限、
  DPI/截图 region/clamp、协议错误码与 stdout 纯净性。

## 3. 最终门禁（B1-B11，全部 PASS）

| # | 门禁 | 结果 | 证据 |
|---|---|---|---|
| B1 | python -m py_compile 全部 src | PASS | phase_b_phaseB3.json |
| B2 | tests/test_server.py --mock-only | PASS（**197 passed / 0 failed**, 2.9s） | 同上 runs[1] |
| B3 | verify.py run：mock 快照 A/B + 渲染 golden + 协议/API | PASS（18 PASS / 1 SKIP / 0 FAIL；SKIP=静态 schema 改由运行时校验） | verify_phaseB3.json |
| B4 | src vs pristine 静态差异审查 | PASS | src_diff_review.md |
| B5 | COM/prefetch 竞态（tools/list 后等 0/150/400/800ms 再首个真实 cu_show） | PASS（5/5） | com_race_phaseB3.json |
| B6 | 冷启动配对 A/B（n=7 x prefetch/noprefetch） | PASS（见 §4） | coldstart_phaseB3.json |
| B7 | 真实 GUI：缓存 vs 旧路径 vs pristine 一致性 + 内存 + churn + 陈旧 id | PASS（37/37） | realgui_phaseB3.json |
| B8 | verify.py run --real：真实快照/回退/截图/真实协议 | PASS（44 PASS / 5 INFO / 0 FAIL） | verify_phaseB3-real.json |
| B9 | 真实 tests/test_bridge.py（owner 运行） | PASS（**55/55 PASS, 0 FAIL**） | _supercu_opt/bridge/real_suite_run2.log |
| B10 | profiler AFTER 配对基准 + 指纹校验 | PASS（27 delta rows；baseline==pristine，after==最终当前，after.src_a==pristine） | bench/opt/compare-after.json, bench_delta.md |
| B11 | 发布版本一致性 | PASS（package.json 1.2.0 == SERVER_VERSION 1.2.0，cordis marker 3） | phase_b_phaseB3.json |

## 4. 性能前后对比（配对、同轮次）

### 4.1 profiler AFTER（n=5 交错配对；A=pristine，B=1.2.0；p50）

| 指标 | A pristine | B 1.2.0 | delta |
|---|---|---|---|
| 冷启动 real spawn->initialize | 859.7 ms | 255.8 ms | **-70.8%** |
| 冷启动 real adjusted（减去解释器启动） | 668.4 ms | 33.5 ms | **-95.4%** |
| 冷启动 mock spawn->initialize | 382.2 ms | 257.9 ms | -31.4% |
| spawn->首个 cu_show（零间隔客户端） | 871.4 ms | 844.1 ms | -10.5%（懒加载首调代价，见 §8） |
| snapshot 记事本 ms/visited（wall） | 1.92（169.2ms） | 1.04（91.8ms） | **-44.5%** |
| snapshot Explorer/System32 ms/visited | 1.70（217.8ms） | 1.01（129.0ms） | **-39.5%** |
| snapshot large Chromium/DSH ms/visited | 1.02（218.4ms） | 0.13（28.3ms） | **-86.3%** |
| e2e 热身 cu_show 中位调用 | 144.6 ms | 94.6 ms | **-40.6%** |
| render 文本量（11 fixture x view 合计） | 30,313 chars / 11,658.8 tok | 30,313 / 11,658.8 | **0.0%（11/11 输出完全一致）** |
| screenshot window | 43.3 ms / 181,469 B | 43.1 ms / 179,681 B | -0.4% / -1.0% |
| 内存 100 快照 id 映射 | 63/63 | 63/63 | 0（RSS +0.12MB vs 0.00MB） |

Profiler 同代码 A/A 噪声底：snapshot ms/visited -5.6%..+1.6%，冷启动 adjusted
-3.1%..-1.4%；上表所有收益均超出噪声底。文本量 0.0% 变化、无 >-10% 回归项。

### 4.2 验证器独立复测（n=7 配对交错，最终 hash）

| 指标 | pristine | 1.2.0 | 配对中位差 |
|---|---|---|---|
| spawn->initialize（prefetch 默认） | 845.5 ms | 244.1 ms | **-608.0 ms** |
| 首个真实 cu_show（prefetch 默认） | 184.5 ms | 151.0 ms | **-45.5 ms** |
| spawn->initialize（SUPER_CU_NO_PREFETCH=1） | 861.0 ms | 256.8 ms | -603.4 ms |
| 首个真实 cu_show（SUPER_CU_NO_PREFETCH=1） | 186.7 ms | 634.2 ms | +449.7 ms（预期权衡，见 §8） |

COM 竞态复测（最终 hash，5 种等待）：首个真实 cu_show 均 OK；首个调用耗时
155.9ms（等 800ms 让 prefetch 完成后）～647ms（零等待）；无 CoInitialize 错误文本。

## 5. 语义与契约一致性（对抗性检查）

| 不变量 | 结果 | 证据 |
|---|---|---|
| mock 快照 A/B（26 场景，节点逐字段 + 顺序 + total/shown/truncated） | 全一致 | verify_phaseB3.json ab.mock_snapshot_parity |
| 渲染 golden（18 fixture x 10 用例 = 180 次） | 全一致 | 同上 ab.render_golden；compact.py 未改 |
| 真实窗口缓存路径 vs 旧路径 vs pristine（记事本小内容 / 1500 行文档 / 计算器；controls/all/text/filter；RuntimeId 归一化） | 32 项 parity 检查全 PASS | realgui_phaseB3.json |
| 稳定 id：同窗口两次快照 / 跨视图 / 三连快照 | 57 个 RuntimeId 全部稳定 | verify_phaseB3-real.json |
| 陈旧 #id / 窗口复用 | 18/18 PASS：关闭窗口报错可操作；同应用重开按契约做语义重定位到同一元素；动作不盲点坐标 | scene_stale_phaseA.json（B7 内复跑） |
| 缓存失败回退：SUPER_CU_NO_CACHE=1 与注入 cache 入口异常 | 回退快照与正常路径一致（忽略易变 focused 标志） | verify_phaseB3-real.json fallback.* |
| 内存上限 MAX_IDS=20000 / MAX_WINDOWS=32 | 300 快照+14 次文本编辑：rid2id=57/id2el=57/cache=1/RSS 25.2MB；10 次 churn：cache=10、win_ctl=10、id2el=570（有界）；最新快照 id 全部可解析 | realgui_phaseB3.json runs.memory / runs.churn |
| DPI/截图：img_w*scale==screen_w、region、越界 clamp、marks | base/current 双侧 PASS（物理空间 2560x1600） | verify_phaseB3-real.json real.screenshot_invariants |
| 协议：tools/list schema 与 pristine 完全一致；错误码 -32602/-32601/-32700；stdout 纯 JSON；退出码 0；工具级 isError 语义 | 全 PASS | verify_phaseB3.json + B8 的 protocol_real |
| 工具 schema / 常量 / 公共 API | 无删除；新增仅为新能力（caps、prefetch、settle、gui_locked） | src_diff_review.md |
| 畸形输入/空树/非 ASCII/引号换行标题/超长文档 | 不崩溃、预算不变、无控制字符泄漏 | verify_phaseB3.json edge.* |

## 6. 静态差异审查（src vs pristine）

- 仅 **bridge.py（+608/-35）** 与 **server.py（+342/-68）** 变化；
  compact/windows/mock_bridge/dpi/gui_lock 逐字节相同。
- 无公共函数/类/方法删除；bridge 新增 MAX_IDS=20000、MAX_WINDOWS=32、
  _CACHE_*、_UIA_GLOBALS_READY；server 新增 gui_locked 导出、
  Backend.ensure_modules/prefetched/note_prefetched、maybe_prefetch、
  SETTLE_*、PREFETCH_BRIDGE_MODULE。
- 环境开关（均为显式、默认不影响输出）：SUPER_CU_NO_CACHE、
  SUPER_CU_NO_PREFETCH、SUPER_CU_SETTLE_MIN_MS。
- 唯一对外文本差异：SERVER_VERSION 1.0.0 -> 1.2.0（发布版本号，预期）。
- 详细清单：bench/verify/out/src_diff_review.md。

## 7. 测试与清理

- python tests/test_server.py --mock-only：**197 passed / 0 failed in 2.9s**（含
  server-opt 新增的 settle 自适应、prefetch/COM 回归用例）。
- 真实 tests/test_bridge.py：owner bridge-opt 运行 **55/55 PASS, 0 FAIL**
  （_supercu_opt/bridge/real_suite_run2.log）。
- 验证器真实 GUI 采集：B8 44 PASS/5 INFO/0 FAIL；B7 37/37 PASS。
- 清理：验证器打开的全部记事本/计算器窗口已关闭；门禁后窗口列表无残留
  Notepad/Calculator（_gui.py list 检查为空）。用户 Edge/微信/DSH 窗口未被触碰。

## 8. 残余风险与说明（不阻塞放行）

1. **Win11 记事本自动隐藏滚动条会让"严格滚动条节点 parity"偶发抖动**：
   同一 pristine/legacy 路径在不同进程采样到
   offscreen=True/rect=[0,0,0,0] 或 offscreen=False/rect=[1209,173,1227,882]
   两种状态；缓存路径自身确定一致。验证器用 A/A 对照（legacy-vs-legacy 也出现
   完全相同的两字段差异）确认是 UIA/UI 过渡噪声，并在 realgui 中仅对
   **scrollbar 的 offscreen/rect** 做容忍；任一其他字段/元素不同仍会 FAIL。
   证据：bench/verify/out/scrollbar_flake_evidence.md、
   realgui_phaseB2_*.json vs realgui_phaseB3_*.json。非滚动条节点 parity
   在全部运行中都是确定的。
2. **冷启动语义**：候选把 uiautomation 等重导入推迟到首个工具调用，并在
   initialize 后后台 prefetch。零间隔客户端下首个 cu_show 仍要付 ~588ms 导入
   （spawn->首 show 仅 -10.5%）；真实使用中间隔（>=0.6s）prefetch 可完全隐藏
   （验证器实测首 show 184.5->151.0ms）。SUPER_CU_NO_PREFETCH=1 时首调
   付全价（+449.7ms），属显式逃生门而非默认路径。发布说明应保留该权衡描述。
3. **MAX_IDS 淘汰路径未端到端覆盖**：记事本 57 节点 x 10 窗口 churn 只到 570
   个 id，远小于 20000；淘汰逻辑由 bridge-opt 单测覆盖，本报告未端到端压测。
4. **自适应 settle 是默认开启的行为变化**：仅对 set/focus 且同步 UIA pattern、
   无 expect、后续还有步骤时提前结束等待，最大等待仍是 settle_ms；click/key 等
   输入合成路径保持原全量等待；SUPER_CU_SETTLE_MIN_MS 提供盲等下限逃生门。
   模型可见输出格式/文本与 pristine 一致（仅毫秒数字变化），批次延迟由 owner
   实测改善；结论按任务范围接受。
5. **共享机器负载**：所有延迟数字来自配对/交错与中位数，绝对值受其它 agent 影响；
   以 profiler AFTER 为主、验证器数据为交叉验证。
6. **环境覆盖**：仅在本机（2560x1600 @150%、单屏、Win11 26200）验证；其它 DPI/
   多屏/深色主题下的截图与元素可见性未覆盖。
7. **发布镜像**：验证针对工作副本 plugins/super-cu；_release/super-cu 的同步
   与 git 提交由 Lead 执行，验证器未核对镜像内容（Lead 已声明另行核对）。
8. **运行中的旧 server**：用户当前 DSH 会话仍由旧代码进程（PID 180508）服务；
   新代码只影响新进程（这是设计如此，不是缺陷）。

## 9. 放行判定（SHIP）

**SHIP - 建议放行 1.2.0。** 依据：

1. 最终冻结 hash 上 11 项门禁全 PASS（0 FAIL / 0 SKIP）。
2. 关键性能目标显著达成且超过同代码噪声底：冷启动 adjusted -95.4%、
   snapshot ms/visited 记事本 -44.5% / Explorer -39.5% / Chromium -86.3%、
   e2e 热身 -40.6%；模型可见文本量 0.0% 变化（无 token 回归）。
3. 语义零漂移：mock 26 场景、渲染 180 次、真实窗口 controls/all/text/filter、
   id 稳定性、协议 schema/错误码/stdout 纯净性、渲染预算/召回不变量全部一致；
   compact/windows/mock_bridge 逐字节未改。
4. 稳定性加固有效：COM/prefetch 竞态在最终代码上 5/5 复测 PASS；缓存/旧路径
   回退、内存上限（20000/32）、陈旧 id 语义重定位、窗口 churn 均 PASS。
5. 版本与发布面一致：py_compile、mock 套件 197/0、真实 bridge 套件 55/55、
   package.json=SERVER_VERSION=1.2.0、cordis marker 3。

**放行条件已满足；唯一建议（非阻塞）**：发布说明中保留 §8.2 冷启动权衡与 §8.3
MAX_IDS 压测缺口说明。

## 10. 证据索引

| 文件 | 内容 |
|---|---|
| bench/verify/out/phase_b_phaseB3.json / .md | **最终门禁汇总（11/0/0）** |
| bench/verify/out/verify_phaseB3.json | mock 快照/渲染 golden/协议/API A/B |
| bench/verify/out/verify_phaseB3-real.json | 真实快照/回退/截图/真实协议（44/5/0） |
| bench/verify/out/realgui_phaseB3.json | 真实 GUI parity/memory/churn/stale（37/37） |
| bench/verify/out/coldstart_phaseB3.json | 冷启动配对 A/B（n=7，两模式） |
| bench/verify/out/com_race_phaseB3.json | COM/prefetch 竞态（5/5） |
| bench/verify/out/src_diff_review.md | 静态差异/契约面清单 |
| bench/verify/out/bench_delta.md / .json | profiler AFTER 27 项 delta + 指纹校验 |
| bench/verify/out/scrollbar_flake_evidence.md | 滚动条自动隐藏 A/A 对照证据 |
| bench/verify/out/phaseA_additions.md | Phase-A 附加证据（COM/冷启动/内存/陈旧 id） |
| bench/opt/compare-after.json, docs/OPT-AFTER.md | profiler 配对 AFTER 原始数据/报告 |
| _supercu_opt/bridge/real_suite_run2.log | bridge-opt owner 真实套件 55/55 |

### 10.1 证据完整性（SHA256 锚定，verifier 独立复核）

profiler 在 AFTER 验收时锁定并在本轮由 verifier **独立重算复核一致**：

| 文件 | SHA256 |
|---|---|
| bench/opt/compare-after.json | 3FC84AE51B6FEBADD728B0850487DEA77794027471A52466731E0D184231AD66 |
| bench/opt/after-screenshot.json | 678D3CB7B26730CA2A882E92C5CB80A5505C4C68B0DBC9FD43CCBE7271B69559 |
| docs/OPT-AFTER.md | 6A2E2C4B64409261AF970F19932F1D866DA71F6288299A68C96F9618C0162B1D |
| bench/opt/baseline.json（pristine 基准） | DC5E5DC559BD1CB475EED517D94F05A538276CC7313951A90F65E5D4BEA4807E |
| fixtures（calculator/explorer_system32/mock_edge/mock_large/notepad/manifest） | c62e853f / 40d42adc / 08702f87 / bb519c0c / 870b16f2 / 05ff99f8（前16位；与 OPT-BASELINE.md 指纹表一致） |

本轮验证所引用的 bench/opt 数据与 docs/OPT-AFTER.md 因此可复现、未被事后修改；
验证器自身产物与门禁 JSON 见上表（bench/verify/out/phase_b_phaseB3.json）。

复跑命令（详见 bench/verify/README.md）：

    python -X utf8 bench/verify/phase_b.py --tag phaseB4 --memory-iters 300
    python -X utf8 bench/verify/verify.py run
    python -X utf8 bench/verify/verify.py run --real --hwnds <hwnd>
    python -X utf8 bench/verify/realgui.py --tag check
    python -X utf8 bench/verify/diffreview.py
    python -X utf8 bench/verify/bench_delta.py
