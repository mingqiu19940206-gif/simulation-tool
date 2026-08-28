# PortGeometry 工程复核（适用于当前 2.4.0）

## 当前结论

**默认 `surface_capacitor` 的自动化 Port 路径已通过 HFSS 实际验证，人工场分布审查仍待完成。**

预览、Validate 和生成脚本继续共享 `GeometryResultV2.ports`。2026-08-28 在
AEDT 2022 R2 / PyAEDT 1.3.0 中创建了四个 Terminal Port sheet 和 P/N terminal
边界，`PORT-011/012/013`、原生 Validate、Setup1/Sweep1 求解及 250 点混合模
S 参数提取均通过。脱敏结果见 `verification/real_aedt_hfss_validation.json`。
本次没有人工查看端口场分布、PEC cap，也没有对比“每端一个多导体截面”方案；
因此证据只支持当前自动化默认方案，不覆盖 VIA 端口拓扑或替代端口方案。

## 已完成的自动与真实复核

- 四个对象仍为零厚度 YZ sheet；预览不增加视觉厚度。
- 下边界沿用 GND1 底面，积分线终点沿用 GND1 顶面；这两者是不同语义，
  必须在真实端口场分布中确认 sheet 是否应覆盖完整参考铜厚。
- 积分线保持 `signal copper center -> GND1 top`，输入/输出法向分别为 `+X/-X`。
- `wave_port` 使用预创建 sheet，因此 `create_port_sheet=False` 与 API 语义一致；
  `create_pec_cap=True` 会要求 PyAEDT 创建端口 PEC cap，其适用性仍待真实模型检查。
- `set_differential_pair` 继续以 P 为 assignment、N 为 reference；端口/终端命名和
  “四张单端 sheet”与“每端一个多导体截面”的选择仍待 HFSS 端口模式确认。
- P/N sheet 不在面积上重叠，但共享差分中线边界。该共边是否造成网格或端口识别问题
  仍列为 HFSS 实测项。

## Port 尺寸策略

固定 `5 * trace_width` 已替换为可追溯策略：

`max(3*trace_width, 1.5*trace_pitch, 2*reference_distance, port_lateral_margin)`

默认配置下外扩为 0.3 mm，Port Y 范围为 P `0..0.45 mm`、N
`-0.45..0 mm`。`port_size_mode=manual` 时使用 `port_lateral_margin`，并经过
配置 Validate。每个 `PortGeometry` 都记录模式、实际外扩、计算依据和是否被板框截断。
该公式只是本地预估，不是 HFSS 正确性证明。

## HFSS 验证门槛

真实工程已产生 `PORT-011/012/013` 的 HFSS 信息级结果，报告不是 stale 或
incomplete，并完成最小求解。以下清单区分已完成的自动门禁与仍需人工完成的项目：

1. 已完成：创建四个实际 Port Sheet，并由结构化规则检查其几何与终端映射。
2. 待完成：生成并对照专用 `artifacts/port_geometry_hfss_comparison.json` 字段。
3. 待完成：人工检查唯一参考导体、积分线两端、法向、传播方向和 PEC cap。
4. 待完成：人工检查 P/N 共边与四单端 sheet，并试验每端一个多导体截面方案。
5. 已完成：运行 HFSS 原生 Validate 和最小求解；待补人工端口模式/场分布审查。
6. 在第 2–4 项完成前，不创建或宣称 `hfss_actual_verified=true` 的端口比较产物。
