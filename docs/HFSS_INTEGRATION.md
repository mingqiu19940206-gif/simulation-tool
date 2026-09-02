# HFSS / AEDT 联调说明

## 本地生成与预检

GUI 中先执行“Validate模型检查”，再生成 HFSS 脚本。生成目录包含快照 JSON、几何清单和 `<Project>_build_hfss.py`。在不启动 AEDT 的机器上可运行：

```powershell
python <Project>_build_hfss.py --validate-only
```

本地预检通过只说明配置、几何、对象计划和脚本结构一致，不代表 HFSS 已创建或求解成功。

安装面切换时，生成脚本直接读取嵌入快照中的最终 `signal_layer_name`、Z 范围、端口、参考层、本体、阻焊开窗、安装面 GND 和空气盒。脚本不得再次根据顶/底层猜测几何。`--validate-only` 只验证嵌入数据和脚本结构；底层模式仍须在真实 AEDT 中检查对象边界、端口终端、RLC/Touchstone 端子、阻焊布尔和原生 Design Validate。

## 2026-08-28 真实 AEDT 验证基线

Windows 上使用 AEDT 2022 R2、PyAEDT 1.3.0 与 Python 3.14.6，以非图形模式执行默认
`surface_capacitor`、`ideal_bridge`、关闭 GND VIA 的 Terminal 方案。真实工程创建和
原生 Validate 通过（19 passed、0 error、0 warning，报告非 stale/incomplete）；
Setup1 与 Sweep1 正常完成，插值扫频收敛且被动。通过产品兼容的
`get_solution_data_per_variation` 路径提取 0.01–32 GHz 的 250 个频点，
Sdd11、Sdd21、Sdc21、Scd21 全部为有限值。退出后 AEDT 进程数为 0，工程无 lock。

机器可读脱敏证据和原始运行产物哈希见
`verification/real_aedt_hfss_validation.json`。仓库不保存 AEDT 工程、results、日志、
许可证或机器标识。本基线不覆盖 `via_transition`、原生 3×3 Optimetrics、背钻、
Touchstone、RLC、底层安装、TDR、AEDT 2023 R2 或人工端口场分布审查。

## 2026-08-31 反向 VIA 最小真实验收

Windows 上使用 AEDT 2022 R2、PyAEDT 1.4.0 与 Python 3.14.7，验证
`entry_layer=L14`、`exit_layer=L10`、钻孔 L1→L14、auto→top 背钻。
本地门禁为 0 error；真实 AEDT 原生 Validate 为 18 passed、0 error、0 warning。
脚本回读确认两组差分对和四个 Terminal，枚举出 16 个混模 `St(...)` quantity；
1.0–1.1 GHz 两点最小扫频自适应收敛，Sdd11、Sdd21、Sdc21、Scd21 均成功导出。

历史 `No trace data` 工程的差分对和 quantity 实际存在，但工程最后一次保存发生在
solve 之前；重开后只有 Sweep 频点元数据，Sweep/LastAdaptive 的单端和混模 trace
都无法由 AEDT reporter 读取。生成脚本现在在 solve 后再次保存，再提取结果；同时
记录差分对回读、实际 quantity 和命中的 solution context。提取失败会写诊断 JSON
并使运行返回失败，不会把空 CSV/JSON 当作成功。随后同一反向 VIA 配置已完成生产
0.01–32 GHz 插值扫频：250 个频点和四条混模曲线均为有限值，历史 `No trace data`
没有复现。该生产验收仍不替代网格/场分布人工审查或 AEDT 2023 R2 验收。

TDR 不复用主插值扫频。生成器会按 `simulation.tdr_sweep` 创建独立离散
`TDRSweep`，要求首点等于步进且终点为步进的整数倍，从而得到
`freq[k]=(k+1)·Δf`。结果导出只读取该已求解上下文中的真实复数
`St(IN_DIFF,IN_DIFF)`；专用扫频关闭、网格无效、quantity 缺失或 trace 为空都会
明确失败，不会重采样主扫频或输出占位曲线。旧配置导入时该新增扫频保持关闭，
避免兼容迁移后无提示地增加数百个离散求解点。

同日使用相同 L14→L10、L1→L14、auto→top 反向 VIA 配置完成了专用 TDR
真机门禁：主 `Sweep1` 为 1.0/1.1 GHz 两点，`TDRSweep` 为 0.1–1.6 GHz、
0.1 GHz 步进的 16 个离散点。AEDT 原生 Validate 为 19 passed、0 error、
0 warning、0 not checked；自适应求解收敛，导出源明确记录为
`Setup1 : TDRSweep / St(IN_DIFF,IN_DIFF)`。输出包含 17 个有限时域/阻抗点，
`via_results.json` 与 `via_tdr.json` 均无 warning。该小规模门禁证明端到端 API
兼容性，不代表默认 0.05–32 GHz、640 点生产 TDR 已完成。

同一次求解还验证了回流孔连接平面的几何修正。移除完全重叠的连接层 pad、直接
从完整参考平面切除成品孔后，`nominalmesh.g3derr` 的 small-segment 记录从旧工程
94 条降到 30 条，`PLANE_L13` 与顶层阻焊记录消失；剩余 30 条均属于 AIRBOX
内部实体交界，求解仍正常收敛。该剩余网格告警尚待进一步定位。

## 真实 AEDT 联调顺序

1. 在安装 AEDT 2022 R2 或 2023 R2 的机器上选择一致的目标版本。
2. 先用默认 `ideal_bridge`、关闭 GND VIA 的最小方案生成工程。
3. 核对 29 层 M6G 叠层、材料、L1 信号、参考平面和反焊盘布尔结果。
4. 核对四个 Terminal Port 的 sheet、参考导体与积分线方向。
5. 核对空气盒及 Radiation/PML 边界，再运行 AEDT 原生 Validate。
6. 分别验证 direct 45°、smooth S bend、RLC 和用户 Touchstone 模型。
7. 对原生 Optimetrics 核对一个 Project、一个 Design、一个 Parametric Setup、Design Variables 和所有 Variations；确认频率 Sweep 仍位于 HFSS Setup 下。
8. 最后再求解并检查混合模 S 参数、网格、收敛、去嵌和 TDR 设置。

### VIA 原生 Optimetrics 当前门禁

- 已完成脚本表达式绑定的轴：`via_transition.padstack.drill_diameter`、`connected_pad_diameter`、`antipad.diameter`（仅 circle）。钻孔变化时成品孔由 `drill - 2*plating` 派生，不能冻结为常数。
- 离线 3×3 门禁只证明生成结果为一个 Project、一个 Design、一个 Parametric Setup、9 个 Variation，且 `--validate-only` 通过；它不证明 AEDT 已创建对象或完成求解。
- 真机验收优先用功能焊盘直径 × 反焊盘直径各 3 点。脚本会读回 Design Variable/Optimetrics Setup，并通过平面或信号导体体积随变量变化且恢复名义值来证明几何绑定。
- 输入/输出 `entry_trace.gap`、`exit_trace.gap`、`backdrill.residual_stub` 和 Return VIA 位置/节距虽可用于离散重建计划/评分，但当前原生坐标表达式尚未完成，原生生成入口会明确拒绝，不会创建几何不动的假扫描。旧 `pair_pitch` 扫描已弃用并直接拒绝。
- `ellipse`、`capsule`、`rounded_rectangle` 使用统一轮廓点沿铜层厚度拉伸后 Subtract；`merged_pair` 每层一个工具，`independent` 每层 P/N 各一个。非圆形 length/width/corner/rotation 暂不进入原生 Optimetrics，直到真实 AEDT 变量化几何验收完成。

## 版本/API 风险

生成脚本同时保留现代 `version/new_desktop` 与较旧 `specified_version/new_desktop_session` 构造路径。Lumped RLC、Touchstone、Terminal Port 和 Parametric Setup 的真实 API 行为必须以目标 AEDT/PyAEDT 组合为准。失败时应保留结构化 Validate 报告和原始 AEDT 日志，但不要把这些用户/运行产物加入源码交付包。
