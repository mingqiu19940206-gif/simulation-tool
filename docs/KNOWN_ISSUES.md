# 已知问题与待验证事项

1. 2.4.0 当前快照已在真实 AEDT 2022 R2 中完成默认 `surface_capacitor`（`ideal_bridge`、关闭 GND VIA）的工程创建、原生 Validate、Setup1/Sweep1 求解及四条混合模 S 参数提取，脱敏证据见 `verification/real_aedt_hfss_validation.json`。该基线本身不覆盖 `via_transition`、原生 3×3 Optimetrics、背钻、Touchstone、RLC、底层安装或 TDR；其中反向 `via_transition`、背钻及专用 TDR Sweep 的后续证据见第 8、11、12 项。
2. 正式支持目标为 AEDT 2022 R2 与 2023 R2；本次实际覆盖 AEDT 2022 R2 / PyAEDT 1.3.0，尚未覆盖 2023 R2。PyAEDT/AEDT 小版本差异可能影响 Lumped RLC、Touchstone 网络组件、Terminal Port、Radiation/PML 和 Optimetrics API，未覆盖组合首次联调仍须逐项核对。
3. 默认 M6G 叠层中 Excel 未明确提供的 CORE/PP Dk、全部 Df 和阻焊 Dk/Df 仍标记为 `PROVISIONAL`，不能视为供应商签核材料数据。
4. 自动 TDR 现在使用独立的离散 `TDRSweep`，并只从该已求解扫频读取真实复数 `St(IN_DIFF,IN_DIFF)`；不会再把主插值扫频重采样或伪装为可用的 TDR 输入。新默认配置为 0.05–32 GHz、0.05 GHz 步进（640 点）；旧配置导入时默认禁用该新增扫频，须由用户明确启用。AEDT 原生 TDR、去嵌和合规判据仍需人工或第三方工具交叉确认。
5. 会改变拓扑的扫参轴会被明确阻止，当前原生 Optimetrics 只允许同一 Design 中可安全变量化的轴。
6. S 参数电容模型依赖用户提供有效 Touchstone 文件；该文件是用户输入，不随项目交付。
7. Shapely 圆形缓冲调用已从弃用的 `resolution` 更新为 2.1 API `quad_segs`；本次使用带 Shapely 的 Python 3.14.6 完整跑过软件 3D/GUI 回归。系统中的精简 `C:\Python314\python.exe` 仍不含 Shapely，不能作为本项目验收解释器。
8. `via_transition` Phase 3 背钻现在按物理上/下功能连接层和钻孔跨度推导，不再假定 entry 位于叠层上侧；正向 L10→L14 与反向 L14→L10 共用同一物理残桩定义。2026-08-31 已在真实 AEDT 2022 R2 / PyAEDT 1.4.0 对 L14→L10、L1→L14 钻孔、auto→top 背钻完成对象创建、原生 Validate、最小求解和生产 0.01–32 GHz 全频求解；尚未完成截面/场分布人工审查。
9. `via_transition` 场景的参数扫描与目标评分已在 Phase 6 离线实现。Signal VIA `drill_diameter`、`connected_pad_diameter`、规范路径 `padstack.antipad.diameter` 已发射为独立 HFSS Design Variables；旧 `padstack.antipad_diameter` 只作为兼容输入/别名。输入/输出 `entry_trace.gap`、`exit_trace.gap`、`residual_stub`、非圆形 length/width/rotation 与回流孔位置/节距尚未完成 HFSS 表达式绑定，进入原生生成时会 fail closed；旧 `pair_pitch` 扫描已弃用。真实 AEDT 3×3 求解及逐 Variation 曲线/评分仍为外部门禁（pending）。
10. `via_transition` 的“反焊盘与叠层”页现为权威入口，包含拓扑、四种形状、默认/逐层尺寸、Return VIA 避空、真实叠层状态、顶视图、Z 截面和扫描入口；“VIA 换层”页的旧信号反焊盘直径仅保留只读兼容摘要。非圆形轮廓已进入生成脚本，但尚未在真实 AEDT 2022 R2/2023 R2 中完成对象创建、Subtract 后对象审计和求解验收，因此真实 AEDT 证据仍为 pending。
11. `via_transition` Phase 4 已在真实 AEDT 2022 R2 / PyAEDT 1.4.0 完成反向 VIA 最小及生产全频验收：原生 Validate 18/0/0，生产 0.01–32 GHz 插值扫频得到 250 个有限频点，Sdd11/Sdd21/Sdc21/Scd21 均成功导出。历史工程的 `No trace data` 根因是工程只在求解前保存，重开后虽有 Sweep 频点元数据但 reporter 取不到 trace；现已在 solve 后保存，再提取。quantity 或 trace 缺失时会写诊断 JSON 并使运行失败，不再把空曲线结果当成功。AEDT 2023 R2 与人工场分布审查仍为 pending。
12. `via_transition` Phase 5 的差分 TDR 已离线实现（`via_transition_tdr.py`：由复数 SDD11 做 IFT、DC 外推、窗函数/上升时间、因果化与阻抗换算，导出 `via_tdr.json`/CSV 并保存完整设置）。已知理想负载/开路/短路与相位敏感数值测试通过。TDR 必须使用 DC 锚定的均匀频率网格（`freq[k]=(k+1)·Δf`）；生成器为此创建独立离散扫频，并在网格不满足要求时 fail closed，不做重采样或伪造结果。2026-08-31 已在 AEDT 2022 R2 / PyAEDT 1.4.0 完成 0.1–1.6 GHz、16 点专用 `TDRSweep` 的真实端到端验收，实际读取 `St(IN_DIFF,IN_DIFF)`，输出 17 个有限时域点且无 TDR warning。默认 0.05–32 GHz、640 点生产 TDR 与 AEDT 原生 TDR/第三方独立算法交叉核对仍为 pending。
13. 回流孔在已有完整参考平面的连接层不再创建与平面完全重叠的冗余 pad；成品孔直接从平面切除后，再将空心桶壁与连接平面合并。相同反向 VIA 配置的真实网格记录由 94 条（AIRBOX 52、顶层阻焊 30、PLANE_L13 12）降为 30 条，且只剩 AIRBOX；求解仍收敛。AIRBOX 的内部相交小段尚未消除，不能把本次改动描述为“无网格警告”。
