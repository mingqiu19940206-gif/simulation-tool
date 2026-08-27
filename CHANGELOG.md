# Changelog

## 2.4.0（发布候选，未完成真实 AEDT 验收）差分 VIA 换层 Phase 1–7

> 源码版本标识已更新为 2.4.0（`VERSION`、`build_info.py`、GUI 标题、打包资源）；配置 schema 为 3。真实 AEDT 验收、EXE 构建与隔离副本验证为阶段外部门禁，Definition of Done（规格 §17）尚未满足。以下为已完成的离线开发能力。

- 2026-08-21 续做：补齐 `signal_via_pitch`/fanout 回归，并修复圆弧扇出在“总长恰等于横向位移、直线段为 0”时错误退化为斜直线的问题；同值仍保持旧版四点矩形，45° 展开/收拢和弧形离散均有直接测试。
- 2026-08-21 续做：独立 HFSS 脚本新增 Signal VIA 钻孔、功能焊盘、反焊盘 Design Variables，圆柱/焊盘/平面避空对象直接引用变量；原生 Optimetrics 支持上述三轴，新增单 Project/单 Design/单 Parametric Setup 的离线 3×3（9 Variation）生成门禁。尚未绑定的 `pair_pitch`、残桩和回流孔坐标轴继续 fail closed，真实 3×3 求解仍 pending。
- 2026-08-21 续做：VIA HFSS 脚本的 plane/airbox 边界改为读取 VIA 统一几何快照，不再沿用电容场景表达式；Shapely 2.1 `buffer(resolution=...)` 更新为 `quad_segs`。

- 配置 schema 升级到 v3，新增 `scenario.type`（`surface_capacitor` / `via_transition`）；v1 → v2 → v3 连续迁移，旧电容配置的物理尺寸、几何和生成快照不变。
- GUI“项目与走线”页增加“仿真场景”选择；几何、2D/3D 预览、ModelValidator、HFSS 生成和参数扫描统一按 `scenario.type` 分派。
- `via_transition` Phase 2 基础差分贯通孔（L1 entry、L14 exit、L1–L14 机械贯通、无背钻）：差分 P/N 信号孔、entry/exit 走线、回流孔、逐层圆形反焊盘、2D/3D 预览、本地 Validate 和独立 HFSS 脚本 `--validate-only`。
- `via_transition` Phase 3 内层退出与背钻（默认 L1 entry、L10 exit、L1–L14 机械通孔、bottom 背钻）：电气层与机械 span 分离、`from_side=auto/top/bottom` 方向推导、目标/实际残桩派生与一致性校验、背钻铜/介质切除、有效孔桶范围、背钻腔体 3D 预览与 HFSS 对象审计。
- `via_transition` Phase 4 端口与结果导出：四个 Terminal Port 携带每端独立 signal/reference 层与积分线、两个差分对（IN/OUT，各含差分与共模）、`via_transition_results.py` 混合模 S 参数转换（SDD11/SDD21/SDC21/SCD21）、`via_results.json`/CSV 导出与结果校验、独立脚本单案例 Solve/导出编排（环境变量门控，未提取到真实 S 参数时 fail closed 不伪造曲线）。
- `via_transition` Phase 5 差分 TDR（规格 §10.4）：新增 `via_transition_tdr.py` 离线 IFT 算法，由复数 SDD11 计算差分阻抗随时间/距离曲线；明确并保存全部设置（输入曲线 SDD11、差分参考阻抗 100 Ω、上升时间、DC 外推、因果处理/窗函数、传播速度或只显示时间）；禁止对 dB 曲线直接 IFFT（复数相位敏感测试）；理想负载/开路/短路方向与阻抗数值测试通过；结果携带复数 SDD11，独立脚本同步导出 `via_tdr.json`/`via_tdr.csv`。
- `via_transition` Phase 6 参数扫描与结果评分（规格 §11）：新增 `via_transition_sweeps.py`（安全数值轴 9 项、拓扑轴 11 项、残桩跨界护栏、预算 ≤200）与 `via_objective.py`（规格掩膜→无量纲惩罚、加权目标、排名与帕累托、JSON/CSV 导出、`sweep_results_summary.json` 内嵌逐 Variation 曲线与评分）；拓扑轴被明确阻止并解释原因；`residual_stub` 扫描区间跨越铜层边界时 fail closed。Signal VIA 三项孔径轴已能生成原生 Optimetrics；其余 VIA 轴绑定与 3×3 真机扫描仍为外部门禁（pending）。
- `via_transition` 专属 GUI 编辑器（规格 §12.2）：新增“VIA 换层”页，提供进出层、Signal VIA Padstack、背钻与残桩、Return VIA、输入/输出走线的直接编辑，并带派生值/校验反馈与俯视图（P/N 信号孔、回流孔、走线）；场景切换按 `scenario.type` 分派，切换后电容页与 VIA 页互不误触。
- 未进行真实 AEDT/HFSS 建模、原生 Validate 和求解；当前证据止于本地几何、预览、Validate、混合模 S 参数与差分 TDR 离线测试、目标评分/扫描计划离线测试和生成脚本 `--validate-only`。AEDT 原生 TDR 或独立算法交叉核对、3×3 小型 Optimetrics 真机扫描为外部门禁（pending）。

## 2.3.2 反焊盘独立开窗预览热修复 — 2026-08-15

- 修复反焊盘预览校正层与基础预览使用不同画布缩放、纵向偏移公式的问题；独立模式的 4 个开窗现在与各自焊盘同心，四边按相同净距显示。
- 保持几何计算、HFSS 开窗实体和单位换算语义不变；本次修复只消除二维预览叠加边界的显示漂移。
- 增加 1 mil 独立开窗的画布级回归测试，完整测试更新为 190 项并全部通过；Windows EXE 已从修复后的源码重建并通过自检与 GUI/3D 冒烟。

## 2.3.2 电容页视口利用率补丁 — 2026-08-14

- 电容整页滚动容器在内容不足一屏时填满真实视口，主体窗格吸收剩余高度，消除验证信息下方的裸空白。
- 几何预览高度按真实视口和 `TkDefaultFont` 的 `linespace` 在可读范围内调整；小窗口仍保持完整纵向滚动。
- 底部布尔容差与验证状态贴合内容底边；窗口缩放、最大化/还原和字体档切换使用合并刷新，避免 Configure 循环。
- 新增高窗口回填、主体扩展、缩小后滚动及重复缩放稳定性回归测试；配置、几何、HFSS 与脚本语义保持不变。

## 2.3.1 GUI 可读性补丁 — 2026-08-14

- 所有 Tk Canvas 改为使用真实可视宽高；初始化 1 px 阶段延迟绘制，不再按不可见的固定最小宽度布图。
- GND VIA 29 层截面使用字体 metrics 派生行高与完整请求高度，复用页面滚动；局部俯视图图例按实际宽度切换右侧/底部多列布局。
- 反焊盘叠层截面增加独立纵/横滚动条、动态标签宽度和可读性增强说明，所有层与选中层页脚均可达。
- 电容尺寸标注采用独立轨道和屏幕空间碰撞规避；几何/摘要画布及整个电容页在紧凑视口可滚动。
- 软件 3D 预览统一布局 HUD、普通对象与 Port 标签；合并空气盒/开放边界标签，并在紧凑视口保留四个 Port 标签。
- 响应式字号同时考虑窗口宽、高和当前 DPI，large/ultra 增量调整为 +2/+3 pt；窗格、工具栏和树行高同步重排。
- 新增 1120×720、1280×800、1900×1050、2560×1400/真实最大化与 normal/large/ultra 的 Canvas bbox、scrollregion、单位、VIA 开关及窗格拖动回归测试。
- 配置语义、单位换算、M6G 29 层数据、几何、HFSS 对象/端口/扫参与脚本输出保持不变。

## 2.3.0 电容安装面增量更新 — 2026-08-13

- 新增 `capacitor.mount_side = "top" | "bottom"`，默认 `top`；旧配置自动按顶层迁移，非法值明确报错。
- 统一几何结果新增安装面、表面法向、实际信号层及 Z、成品板外向表面、参考层和表层阻焊开窗字段。
- P/N 走线、焊盘、三种电容模型、电容本体、端口、参考层、安装面 GND、阻焊开窗与空气盒共同切换；底层模式使用真实底层外层铜。
- 3D 预览、结构化 Validate 与 HFSS 生成共享同一几何快照，增加顶/底层切换、保存加载、无漂移和脚本独立预检测试。
- 未进行真实 AEDT/HFSS 建模验证；当前结果仅包含本地几何、预览、Validate 和生成脚本 `--validate-only` 验证。

## 2.3.0 — 2026-08-12

- 固化当前工作区在旧 2.2.3 构建之后继续生效的全部几何、配置、预览、Validate、HFSS 与测试修改。
- 保留实际调用链中的 `main_gui_legacy.py`、`antipad_editor_legacy` 和 V1 几何兼容内核；排除被包目录遮蔽的重复顶层模块。
- 统一 GUI 标题、关于页、运行日志身份、文件/产品版本、EXE 名称、构建脚本和发布目录为 2.3.0。
- 将不可变默认配置和构建元数据整理到 `config/`，并支持源码与 PyInstaller 冻结运行时定位。
- 收敛为唯一 PyInstaller spec 和唯一自动构建脚本。
- 新增完整交付验证、清单、校验值和独立性检查。

更早版本的历史记录不作为本快照的多个并存版本保留；旧包仍留在原始工作区，未复制到本交付。
