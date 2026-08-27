# PortGeometry 工程复核（适用于当前 2.3.2）

## 当前结论

**本地 PortGeometry 一致，但尚未经过 HFSS 实际验证。**

预览、Validate 和生成脚本继续共享 `GeometryResultV2.ports`。本机只有
`ansys.aedt.core 1.3.0` Python 包，没有 `ansysedt.exe`/HFSS 安装及可用许可证，
因此本轮没有创建真实 AEDT 工程、运行 HFSS 原生 Validate、查看端口模式或求解。
任何本地测试通过都不得把 Port 状态提升为“HFSS已验证”。

## 已完成的静态复核

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

只有真实工程同时产生 `PORT-011/012/013` 的 HFSS 信息级结果，且报告不是 stale 或
incomplete，界面才允许显示“HFSS已验证”。实测时必须完成：

1. 创建四个实际 Port Sheet，并读回每张 sheet 的 XYZ 边界。
2. 对照 `artifacts/port_geometry_hfss_comparison.json` 的每个字段。
3. 检查唯一参考导体、积分线两端、法向、传播方向和 PEC cap。
4. 检查 P/N 共边与四单端 sheet 方案；同时试验每端一个多导体截面方案。
5. 运行 HFSS 原生 Validate，查看端口模式/场分布并执行最小求解。
6. 只有全部通过后，将比较文件的 `hfss_actual_verified` 改为 `true` 并附工程证据。
