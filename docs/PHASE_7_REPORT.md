# v2.4.0 Phase 7 阶段报告（发布候选）

记录日期：2026-08-16  
开发副本：`E:\deepseek harness workspace\PCIe链路仿真+Via仿真\surface_cap_antipad_tool_v2.4.0_dev`  
唯一基线：`latest_complete_package/project`（SurfaceCapAntipadTool 2.3.2 post-fix snapshot）  
权威需求：`VIA_TRANSITION_CODEX_DEVELOPMENT_SPEC.md`（§13 Phase 7、§17 Definition of Done）

## 1. Phase 7 完成的能力（离线部分）

- **版本更新为 2.4.0**：`VERSION`、`src/build_info.py`（`SOFTWARE_VERSION`）、`src/main_gui_legacy.py`（`APP_VERSION`）、`src/main_gui.py`（窗口标题/崩溃目录/启动错误标题）、`packaging/version_info.txt`（Windows 版本资源）、`packaging/SurfaceCapAntipadTool.spec`（EXE 名）、`packaging/build_windows.ps1`（版本校验 + EXE 名 + 修正 `config_schema_version` 2→3）、`packaging/assemble_delivery.ps1`、`packaging/verify_isolated_delivery.ps1`、`config/build_metadata.json`（`software_version`）、`tools/gui_visual_acceptance.py`（探针标题）。
- **配置 schema 为 3**：保持 schema v3，`build_windows.ps1` 的构建元数据 `config_schema_version` 由 2 修正为 3。
- **文档更新**：`CHANGELOG.md`（2.4.0 发布候选 + Phase 1–7 条目）、`README.md`（标题与发布状态 + Phase 5/6 能力）、`docs/KNOWN_ISSUES.md`（2.4.0 快照与剩余风险）。

## 2. 测试与验证证据

### 2.1 可复现门禁（纯 Python，无 tkinter/shapely/AEDT）

| 门禁 | 结果 |
|---|---|
| `verify_phase3_gate.py` | 18/18 |
| `verify_phase4_gate.py` | 13/13 |
| `verify_phase5_gate.py` | 15/15 |
| `verify_phase6_gate.py` | 15/15 |

### 2.2 可运行单测（纯 Python，无 tkinter/shapely）

`python -m unittest tests.test_via_transition_backdrill tests.test_via_transition_ports tests.test_via_results_parser tests.test_via_transition_tdr tests.test_via_objective tests.test_via_transition_sweeps`：**45/45 通过**。

### 2.3 无法在本沙箱复跑的项

- 完整 unittest / GUI smoke：沙箱缺 `tkinter`/`shapely`，系统临时目录写被拒。
- EXE 构建（PyInstaller + Python 3.14 + tkinter/shapely）与隔离副本验证：需完整构建环境。
- 真实 AEDT 验收：本机无 `ansysedt.exe`。

## 3. 仍未满足 Definition of Done 的项（规格 §17）

v2.4.0 尚未“完成”。以下硬性条件仍为外部门禁（pending）：

1. L1–L14 基础差分通孔与 L1→L10 + bottom 背钻案例通过真实 AEDT Build/Validate/Solve。
2. SDD11/SDD21/SDC21/SCD21 成功导出并可追溯到 Variation。
3. 至少一个小型 Optimetrics 扫描完成逐 Variation 结果提取和评分（3×3 真机）。
4. 全部自动测试、EXE smoke、模型预览 smoke 和隔离构建通过（需完整环境）。
5. 真实 AEDT TDR 交叉核对与验收报告加入交付资料。

## 4. 正确完成状态表述（规格 §1.3）

> Phase 0–7 的离线实现与结构验证已完成，源码版本标识更新为 2.4.0；真实 AEDT 建模、Validate、求解、TDR 交叉核对、Optimetrics 3×3 扫描、EXE 构建与隔离副本验证**等待验收**。不得表述为“VIA 仿真功能完成”或“2.4.0 已发布”。

## 5. 本轮修改文件

- `VERSION`、`src/build_info.py`、`src/main_gui.py`、`src/main_gui_legacy.py`、`config/build_metadata.json`、`tools/gui_visual_acceptance.py`：版本标识 2.4.0。
- `packaging/version_info.txt`、`packaging/SurfaceCapAntipadTool.spec`、`packaging/build_windows.ps1`、`packaging/assemble_delivery.ps1`、`packaging/verify_isolated_delivery.ps1`：发布元数据 2.4.0。
- `CHANGELOG.md`、`README.md`、`docs/KNOWN_ISSUES.md`：文档同步。

## 6. 后续外部待办

1. 在目标机（AEDT 2022 R2/2023 R2 + Python 3.14 + tkinter/shapely）运行 `packaging/build_windows.ps1` 完成 EXE 构建与自检。
2. 运行完整 unittest 与 `tools/verify_delivery.py`。
3. 执行真实 AEDT 验收（Phase 2 通孔、Phase 3 背钻、Phase 4 端口/求解、Phase 5 TDR 交叉核对、Phase 6 3×3 扫描）。
4. 将 AEDT 验收报告加入交付资料后，方可宣告 Definition of Done。
