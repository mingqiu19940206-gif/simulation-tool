# VIA 过孔反焊盘与叠层页修复报告

日期：2026-08-21  
源码目录：`surface_cap_antipad_tool_v2.4.0_dev`  
结论：配置、统一轮廓、Validate、GUI、2D/3D 预览、HFSS 生成和扫描能力已接通；本地门禁通过。真实 AEDT Build/Validate/Solve/Optimetrics 未执行，状态为 pending。

## 1. 修改文件

- 配置与迁移：`config/default_config.json`、`src/config_manager.py`。
- 权威几何：新增 `src/via_antipad.py`；修改 `src/via_transition_geometry.py`、`src/via_transition_semantics.py`。
- GUI/预览：`src/antipad_editor_core.py`、`src/antipad_editor_preview_base.py`、`src/antipad_editor/__init__.py`、`src/model_preview.py`、`src/main_gui.py`、`src/main_gui_legacy.py`。
- HFSS/扫描：`src/hfss_script_generator.py`、`src/simulation_sweep_semantics.py`、`src/simulation_sweep_editor.py`、`src/via_transition_sweeps.py`。
- 示例：`examples/via_transition_through.json`、`examples/via_transition_backdrill.json`。
- 测试/验收：新增 `tests/test_via_antipad_shapes.py`、`tools/verify_via_antipad_delivery.py`；修改 `tests/test_via_transition_gui.py`、`tests/test_simulation_sweep_workbench.py`、`tests/test_via_transition_sweeps.py`、`tests/_antipad_editor_tests.py`、`tools/gui_visual_acceptance.py`。
- 文档：`docs/HFSS_INTEGRATION.md`、`docs/KNOWN_ISSUES.md`、本报告。

未修改或覆盖历史交付包、ZIP、EXE、`latest_complete_package` 和用户结果目录。

## 2. 配置与迁移

规范路径为 `via_transition.padstack.antipad`：

- `topology`: `independent | merged_pair`
- `shape`: `circle | ellipse | capsule | rounded_rectangle`
- `diameter`, `length`, `width`, `corner_radius`, `rotation_deg`
- `per_layer.<layer>`: `enabled`, `inherit_global` 及可选拓扑/形状/尺寸覆盖

schema 仍为 3，采用同 schema 的向后兼容扩展。旧 `antipad_diameter` 和 `antipad_per_layer` 会迁移为 circle/independent；规范字段与旧别名冲突时规范字段胜出；保存时由规范字段单向派生旧别名，避免双向漂移。mm/mil 缩放覆盖全局和逐层新尺寸。

## 3. 精确轮廓定义

- circle：半径 `diameter / 2`。
- ellipse：局部半轴 `length / 2`、`width / 2`。
- capsule：端到端长 `length`、总宽 `width`，端圆半径 `width / 2`，中部直段总长 `length - width`。
- rounded rectangle：外包尺寸 `length × width`，四角半径 `corner_radius`。
- `rotation_deg` 均表示绕轮廓中心从全局 +X 逆时针旋转。
- independent 在每个有效层产生 P、N 两个轮廓；merged_pair 在 P/N 中点产生一个轮廓。

`ViaAntipadContour` 保存 layer、owner、topology、shape、center、尺寸、旋转、来源、点集、bounds、面积。GUI 顶视图/Z 截面、软件 3D 场景、制造校验和 HFSS 脚本都消费这一个对象，不再各自推导近似圆。

## 4. 逐层与层状态语义

无逐层节点时使用 global；`inherit_global=true` 时沿用 global；否则覆盖指定字段；`enabled=false` 时该层不生成轮廓。叠层表显示信号孔逐层状态、拓扑、形状、尺寸来源、最终有效尺寸、信号开窗数量、Return VIA 状态和有效尺寸。entry/exit、功能焊盘、NFP、clearance、backdrill removed、outside drill span 分开显示。

## 5. Validate

新增稳定规则：

- `VIA-TRN-080`：配置不可解析/非有限值。
- `081`/`082`：拓扑/形状枚举。
- `083`/`084`：正尺寸和有限旋转。
- `085`：capsule `length >= width`。
- `086`：圆角半径范围。
- `087`/`088`：逐层层名与钻孔铜层跨度。
- `089`：逐层覆盖字段与形状不匹配。
- `090`：真实轮廓未完整包含孔桶/背钻投影并保留正净距。
- `091`：旋转后 bounds 超出 plane。

Return VIA 与信号反焊盘的相交检查使用真实多边形轮廓与圆的距离/包含关系；无效配置进入 ModelValidator 后 fail closed。生成脚本还审计嵌入轮廓的层、形状、拓扑、owner、点数和每层数量。

## 6. GUI、HFSS 与扫描

“反焊盘与叠层”页是 VIA 信号/Return 反焊盘的权威入口；“VIA 换层”旧直径控件仅保留只读兼容摘要。形状切换会联动字段，顶视图、Z 截面和软件 3D 同步刷新，并保留点击尺寸定位参数。

HFSS：circle 使用 cylinder；ellipse/capsule/rounded rectangle 使用同一权威点集沿铜厚拉伸成 polygon solid 后 Subtract。independent 每层两个工具，merged_pair 每层一个。不存在整面 plane 时记录“不适用”，不伪造 Subtract 成功。

原生数值轴：

- `via_transition.padstack.drill_diameter`
- `via_transition.padstack.connected_pad_diameter`
- `via_transition.padstack.antipad.diameter`（仅 circle）

旧 `via_transition.padstack.antipad_diameter` 仅兼容。非圆 length/width/corner/rotation 尚未完成真实 AEDT 表达式绑定，明确 fail closed。`topology`、`shape`、逐层启停属于拓扑变化，不进入同一 Parametric Setup。VIA 场景不再整体禁用扫描页；Variation 预算、预检和选中 Variation 回填已可用，且不会伪造 S 参数、TDR 或评分。

## 7. 验证记录

解释器：`C:\Users\admin\AppData\Local\Python\pythoncore-3.14-64\python.exe`，Python 3.14.6。

1. 定向测试：`python -B -m unittest -v tests.test_via_antipad_shapes tests.test_via_transition_gui tests.test_via_transition_geometry`；退出码 0；27 项通过。此前包含扫描套件的 34 项定向回归也退出码 0。
2. 完整 unittest：`python -B -m unittest discover -s tests -q`；退出码 0；284 项通过（随后新增两项制造规则测试）。
3. 权威入口：`python -B tools/run_tests.py <result.json>`；退出码 0；最终 286 项，failure/error/skipped 均为 0；结果：`verification/via_antipad_test_results.json`。
4. 交付验证：`python -B tools/verify_delivery.py <result.json>`；退出码 0；四端口、空气盒、开放边界、HFSS `--validate-only`、原生 Optimetrics 结构均通过；结果：`verification/via_antipad_source_verification.json`。
5. GUI smoke：`src/main_gui.py --smoke-test`；退出码 0。
6. 3D preview smoke：`src/main_gui.py --model-preview-smoke-test`；退出码 0。
7. VIA GUI 截图：三条 `tools/gui_visual_acceptance.py --page antipad --scenario via --capture ...`；退出码均为 0；实际尺寸分别为 1120×720、1900×1050、2560×1400，并已人工查看。
8. 两个 VIA 示例：`tools/verify_via_antipad_delivery.py verification/via_antipad`；退出码 0；2/2 本地 Validate 可继续，2/2 生成脚本 `--validate-only` 退出码 0；报告：`verification/via_antipad/via_antipad_acceptance.json`。
9. 无落盘编译检查：对 16 个主要 Python 文件调用内置 `compile()`；退出码 0。

## 8. 截图与 HFSS 审计物

- `verification/screenshots/via_antipad/1120x720.png`，SHA-256 `22A91F47367D757A6C6F664DD6A0FA04D4E3C0321E432FE4A85F7513EB76B285`。
- `verification/screenshots/via_antipad/1900x1050.png`，SHA-256 `BECE61647442B8C56270625D5AC0C8C55DA1AE8C7814526FF92863DFEF478D2A`。
- `verification/screenshots/via_antipad/2560x1400.png`，SHA-256 `6EC16C2DF34A957294FC6669D8D69B39540FF1D3DBF94A4A75E602360435CB0B`。
- `verification/via_antipad/via_transition_through/Surface_Cap_Antipad_V2_0402/`。
- `verification/via_antipad/via_transition_backdrill/Surface_Cap_Antipad_V2_0402/`。

每个示例目录包含配置/几何快照、几何摘要、本地 Validate JSON/TXT、构建日志和自包含 HFSS 构建脚本。

## 9. Pending 与风险

本机没有在 AEDT 2022 R2/2023 R2 中执行真实 Build、原生 Validate、Subtract 后对象审计、Solve、S 参数/TDR 或 Optimetrics，因此这些门禁均为 pending。非圆形原生数值扫描也保持 fail closed。仓库 `build_info` 仍代表既有 2.4.0 构建元数据；本次没有重打 EXE，不应把该旧构建哈希当成本次源码的新发布哈希。
