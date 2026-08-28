# 已知问题与待验证事项

1. 2.4.0 当前快照已在真实 AEDT 2022 R2 中完成默认 `surface_capacitor`（`ideal_bridge`、关闭 GND VIA）的工程创建、原生 Validate、Setup1/Sweep1 求解及四条混合模 S 参数提取，脱敏证据见 `verification/real_aedt_hfss_validation.json`。该结果不覆盖 `via_transition`、原生 3×3 Optimetrics、背钻、Touchstone、RLC、底层安装或 TDR，以上项目仍为 pending。
2. 正式支持目标为 AEDT 2022 R2 与 2023 R2；本次实际覆盖 AEDT 2022 R2 / PyAEDT 1.3.0，尚未覆盖 2023 R2。PyAEDT/AEDT 小版本差异可能影响 Lumped RLC、Touchstone 网络组件、Terminal Port、Radiation/PML 和 Optimetrics API，未覆盖组合首次联调仍须逐项核对。
3. 默认 M6G 叠层中 Excel 未明确提供的 CORE/PP Dk、全部 Df 和阻焊 Dk/Df 仍标记为 `PROVISIONAL`，不能视为供应商签核材料数据。
4. 自动 TDR 仅记录复核请求；完整时域报告、上升时间、参考阻抗、去嵌和合规判据仍需在 AEDT 中人工确认。
5. 会改变拓扑的扫参轴会被明确阻止，当前原生 Optimetrics 只允许同一 Design 中可安全变量化的轴。
6. S 参数电容模型依赖用户提供有效 Touchstone 文件；该文件是用户输入，不随项目交付。
7. Shapely 圆形缓冲调用已从弃用的 `resolution` 更新为 2.1 API `quad_segs`；本次使用带 Shapely 的 Python 3.14.6 完整跑过软件 3D/GUI 回归。系统中的精简 `C:\Python314\python.exe` 仍不含 Shapely，不能作为本项目验收解释器。
8. `via_transition` Phase 3 背钻已离线实现，但残桩采用“背钻终点 = exit 连接铜边界 ± 目标残桩”的直接派生方式，实际残桩等于目标残桩（构造性一致）；尚未在真实 AEDT 中验证背钻腔体对底部阻焊/介质边界的切除，真实截面/对象体积证据仍为 pending。
9. `via_transition` 场景的参数扫描与目标评分已在 Phase 6 离线实现。Signal VIA `drill_diameter`、`connected_pad_diameter`、规范路径 `padstack.antipad.diameter` 已发射为独立 HFSS Design Variables；旧 `padstack.antipad_diameter` 只作为兼容输入/别名。`pair_pitch`、`residual_stub`、非圆形 length/width/rotation 与回流孔位置/节距尚未完成 HFSS 表达式绑定，进入原生生成时会 fail closed；真实 AEDT 3×3 求解及逐 Variation 曲线/评分仍为外部门禁（pending）。
10. `via_transition` 的“反焊盘与叠层”页现为权威入口，包含拓扑、四种形状、默认/逐层尺寸、Return VIA 避空、真实叠层状态、顶视图、Z 截面和扫描入口；“VIA 换层”页的旧信号反焊盘直径仅保留只读兼容摘要。非圆形轮廓已进入生成脚本，但尚未在真实 AEDT 2022 R2/2023 R2 中完成对象创建、Subtract 后对象审计和求解验收，因此真实 AEDT 证据仍为 pending。
11. `via_transition` Phase 4 的单案例 Solve 与 S 参数导出已实现离线结构（混合模转换、`via_results.json`/CSV、独立脚本 solve/export 编排），但真实 AEDT 收敛、四条混合模曲线与频率点一致性、端口映射/参考阻抗写入结果仍未在真机验证；S 参数提取的 PyAEDT `get_solution_data` 接口需在目标 AEDT 版本逐项核对，失败时按 fail closed 写出占位结构而非伪造曲线。
12. `via_transition` Phase 5 的差分 TDR 已离线实现（`via_transition_tdr.py`：由复数 SDD11 做 IFT、DC 外推、窗函数/上升时间、因果化与阻抗换算，导出 `via_tdr.json`/CSV 并保存完整设置）。已知理想负载/开路/短路与相位敏感数值测试通过；但尚未与 AEDT 原生 TDR 或第三方独立算法交叉核对，真实 AEDT 提取的复数 SDD11 到 TDR 曲线的端到端链路仍为外部门禁（pending）。TDR 目前假设 DC 锚定的均匀频率网格（`freq[k]=(k+1)·Δf`），非均匀或未从 DC 起采的 Sweep 需要重采样后才可用。
