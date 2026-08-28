# v2.4.0 VIA 换层续做报告（2026-08-21）

## 结论

本轮完成了 fanout 离线复核与一个边界缺陷修复，并把首批三项 Signal VIA 尺寸轴接入独立 HFSS 脚本的 Design Variables / 原生 Optimetrics 生成链。离线 3×3 结构门禁、交付校验、GUI smoke 和模型预览 smoke 已通过。

真实 AEDT 建模、原生 Validate、求解、逐 Variation S 参数/评分、TDR 交叉核对仍未执行；当前机没有目标 AEDT/PyAEDT，也没有许可证证据，因此状态保持 `pending`，不得表述为“VIA 仿真已完成”。

## 本轮实现

1. fanout 回归与边界修复
   - 同值 `signal_via_pitch == pair_pitch` 保持旧版四点矩形，无扇出。
   - 直接覆盖 `deg45` 展开/收拢、圆弧离散、长度/方向规则和旧配置回填。
   - 修复 `arc` 在“总长恰等于横向位移且 `straight_length=0`”时错误退化为斜直线；合法边界现在保留完整 90° 圆弧且无重复端点。
2. VIA 原生 Optimetrics 首批绑定
   - 新增 `via_signal_drill_diameter`、`via_signal_connected_pad_diameter`、`via_signal_antipad_diameter` Design Variables。
   - 成品孔用 `via_signal_drill_diameter - 2*via_signal_plating_thickness` 派生；孔桶、孔内切除、功能焊盘、安装孔和逐层反焊盘直接引用变量表达式。
   - 允许上述三轴创建一个 Project / 一个 Design / 一个 Parametric Setup；离线 3×3 用功能焊盘直径 × 反焊盘直径得到 9 个 Variation。
   - 真机脚本会读回 Design Variables 和 Optimetrics Setup，并用平面/信号导体体积随变量变化及恢复名义值证明几何绑定。
   - 尚未绑定的 `pair_pitch`、`backdrill.residual_stub`、Return VIA 位置/节距继续 fail closed，避免假扫描。
3. HFSS 场景一致性
   - VIA 脚本的 plane/airbox XY 边界改为统一 VIA 几何快照，不再沿用 surface capacitor 的板框表达式。
   - VIA 入口层用于兼容 Z 变量别名，不再调用只适用于 surface 场景的外层信号层推导。
4. 依赖收尾
   - Shapely 2.1 `buffer(..., resolution=...)` 更新为 `quad_segs`。
   - 构建文档中的 EXE 名从过时的 v2.3.2 修正为 v2.4.0。

## 改动文件清单

- 源码：`src/via_transition_geometry.py`、`src/via_transition_sweeps.py`、`src/scenario_semantics.py`、`src/simulation_sweep_semantics.py`、`src/hfss_script_generator.py`、`src/model_preview.py`。
- 测试：`tests/test_via_transition_fanout.py`、`tests/test_via_native_optimetrics.py`、`tests/test_config_schema_v3.py`、`tests/test_scenario_dispatch.py`、`tests/test_via_transition_gui.py`。
- 文档：`README.md`、`CHANGELOG.md`、`docs/KNOWN_ISSUES.md`、`docs/HFSS_INTEGRATION.md`、`docs/DEVELOPMENT_AND_TESTING.md`、本报告。
- `latest_complete_package/` 哈希复核差异数为 0；没有修改审计基线。

## 验证记录

| 命令 | 退出码 | 摘要 |
|---|---:|---|
| `python -m unittest`（配置 + VIA 几何/背钻/端口/扫参/TDR/目标 + fanout + VIA/native Optimetrics + surface native 回归） | 0 | 91 tests，全部通过 |
| `python tools/run_tests.py` | 1 | Ran 258；251 通过、3 skipped、2 failures、2 errors。两个 error 为当前环境缺 Shapely；两个 failure 为既有反焊盘 GUI 布局在 Tk 9.0 下 bbox 顶边 −3 px、阈值 −2 px。随后写既有 `verification/test_results.json` 又被受管文件权限拒绝。 |
| `python tools/verify_delivery.py <工作区 verification 新文件>` | 1 | 所有内部检查执行到报告落盘；受管环境拒绝在交接包 `verification/` 下创建/覆盖报告。 |
| `python tools/verify_delivery.py %TEMP%/via_source_verification_20260821.json` | 0 | `passed=true`；默认配置往返、本地 Validate、51 个 scene 对象、4 Port、airbox/open boundary、HFSS `--validate-only`、surface 原生 Optimetrics 结构全部通过；`real_aedt_hfss_executed=false`。 |
| `python src/main_gui.py --smoke-test` | 0 | GUI 冒烟通过。 |
| `python src/main_gui.py --model-preview-smoke-test` | 0 | 软件模型预览冒烟通过。 |
| 历史 `verify_phase3_gate.py` 探针 | 1 | 该历史门禁仍断言所有 VIA 扫描必须 fail closed，和本轮首批原生轴能力冲突；写历史结果 JSON 也被受管权限拒绝。当前能力由新增 VIA native 测试覆盖。 |

当前解释器为 CPython 3.14.7 / Tk 9.0。`shapely`、`vtk`、`ansys.aedt.core`、`PyInstaller` 均未安装；未自行安装依赖。

## EXE 与真实 AEDT 状态

- `release/SurfaceCapAntipadTool_v2.4.0.exe` 不存在。
- 因缺 Shapely、VTK、PyAEDT、PyInstaller，且交接约束禁止未经授权安装软件，本轮未启动 `packaging/build_windows.ps1`，也无法运行 EXE release self-check / 隔离副本验证。
- 未发现目标路径下的 AEDT 2022 R2 / 2023 R2 `ansysedt.exe`；未启动 AEDT、未执行真实建模、Validate、Solve 或 3×3 Optimetrics。

## 下一阶段入口与剩余风险

1. 在 AEDT 2022 R2 或 2023 R2 + 匹配 PyAEDT 的许可证机器上，先运行功能焊盘直径 × 反焊盘直径的 3×3；核对 9 个 Variation 的对象体积、端口、收敛、四条混合模曲线、评分与排名。
2. 完成 `pair_pitch`（含 deg45/arc 多边形和 Port）、残桩 Z、Return VIA 坐标的 HFSS 表达式绑定后，才把其余离线安全轴提升为原生轴。
3. 用 AEDT 原生 TDR 或独立第三方算法交叉核对复数 SDD11 → TDR 链路。
4. 在完整 Python 依赖环境复跑 258+ 全测；单独处理 Tk 9.0 的既有反焊盘 1 px bbox 差异，不应为本轮 VIA 改动弱化断言。
5. 运行唯一 Windows 构建链和隔离验证，重新生成构建元数据/source hash；当前元数据仍是交接快照值，不能作为本轮源码构建身份。
6. 供应商签核默认 M6G 的 `PROVISIONAL` 材料值。
7. 交接包根目录 `VIA换层参数说明.html/.pdf` 仍是 fanout 实现前的旧参数说明；依据“只修改开发副本”约束本轮未改根目录资料，发布前应从当前源码重新生成。
