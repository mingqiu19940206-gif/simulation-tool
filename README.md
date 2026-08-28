# SurfaceCapAntipadTool 2.4.0

这是 v2.4.0 差分 VIA 换层版本（发布候选）。默认 `surface_capacitor` 最小方案已在 Windows / AEDT 2022 R2 完成真实建模、原生 Validate、求解和混合模 S 参数提取；VIA 真机验收仍在进行。正式源码入口为 `src/main_gui.py`。Windows 发布程序由 `packaging/build_windows.ps1` 生成到 `release/SurfaceCapAntipadTool_v2.4.0.exe`；`release/` 属于构建产物，被 `.gitignore` 排除，不在仓库中。

## 快速开始

环境要求：64 位 Windows、CPython 3.11-3.14。源码 GUI 使用 Tkinter；HFSS 工程生成/执行需要目标机安装兼容的 Ansys Electronics Desktop 2022 R2 或 2023 R2。

```powershell
python -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
python src\main_gui.py
```

运行全部测试：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```

运行交付级功能检查：

```powershell
$env:PYTHONPATH = "$PWD\src"
python tools\verify_delivery.py verification\source_verification.json
python src\main_gui.py --smoke-test
python src\main_gui.py --model-preview-smoke-test
```

重建 Windows EXE：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

构建脚本会重新生成 `config/build_metadata.json`，执行全部测试，编译源码，以唯一 spec 构建 EXE，并运行发布自检。详细步骤见 `docs/DEVELOPMENT_AND_TESTING.md`、`docs/HFSS_INTEGRATION.md` 和 `docs/DELIVERY_REPORT.md`。

## 目录

- `src/`：正式入口、GUI、几何内核、预览、Validate、HFSS 和扫参实现。
- `config/`：默认配置与构建身份。
- `examples/`：当前示例配置和 V1 迁移样例。
- `tests/`：当前有效的 unittest 回归与安装面专项测试。
- `packaging/`：唯一 PyInstaller spec、版本资源和自动构建脚本。
- `release/`：本快照重建并通过冒烟测试的 Windows EXE。
- `docs/`：运行、开发、测试、HFSS 联调、Git 状态与已知问题。
- `verification/`：机器可读验证结果，包括脱敏后的真实 AEDT 验证摘要 `real_aedt_hfss_validation.json`；不包含构建/运行日志或用户工程。

没有外部图片、图标、材料库或 HFSS 模板文件被运行时引用；HFSS 脚本模板由 `src/hfss_script_generator.py` 生成，材料和叠层默认值位于 `config/default_config.json`。

## 电容安装面切换

“电容与模型”页的“电容安装面（当前）”支持 `top`（顶层）和 `bottom`（底层），配置字段为 `capacitor.mount_side`，默认 `top`。旧配置缺少该字段时按顶层加载；显式非法值会阻止 Validate 和 HFSS 生成。

安装面由统一几何快照驱动：P/N 走线、焊盘、ideal bridge、Lumped RLC/Touchstone Sheet、电容本体、端口及积分线、最近板内参考层、表层阻焊开窗、可选安装面 GND 铜皮和空气盒会一起切换。XY 布局不变，叠层本身不翻转。底层安装要求叠层具有真实底层外层 `signal` 铜（其下仅允许 `soldermask`）。

兼容字段 `gnd_vias.create_top_ground_copper` 和 `gnd_vias.top_ground_clearance` 名称保留，但从本版本起语义为“当前电容安装面 GND 铜皮/净距”。

## 差分 VIA 换层（开发中，v2.4.0）

本开发副本正在向 v2.4.0 扩展，配置 schema 已升级到 3。在“项目与走线”页的“仿真场景”中可选择：

- **AC 耦合电容**（`surface_capacitor`）：原有已发布路径，行为不变。
- **差分 VIA 换层**（`via_transition`）：Phase 2 基础差分贯通孔、Phase 3 内层退出/背钻、Phase 4 端口与结果导出、Phase 5 差分 TDR、Phase 6 扫描计划与目标评分均已离线实现；并提供“VIA 换层”专属参数编辑器。

`via_transition` 支持两类配置：

- 外层到外层贯通孔（Phase 2）：L1 entry、L14 exit、L1–L14 机械贯通、无背钻。示例见 `examples/via_transition_through.json`。
- 内层退出 + 背钻（Phase 3）：L1 entry、L10 exit、L1–L14 机械通孔、bottom 背钻；支持 `backdrill.from_side=auto/top/bottom`，派生目标/实际残桩并校验一致性。示例见 `examples/via_transition_backdrill.json`。

已实现：差分 P/N 信号孔、entry/exit 走线、回流孔、逐层圆形反焊盘、背钻铜/介质切除与腔体、四个 Terminal Port 与两个差分对、混合模 S 参数转换（SDD11/SDD21/SDC21/SCD21）、`via_results.json`/CSV 结果导出、差分 TDR（由复数 SDD11 离线 IFT 计算差分阻抗随时间/距离曲线，导出 `via_tdr.json`/CSV，保存完整 TDR 设置）、扫描计划（安全数值轴/拓扑护栏/残桩跨界护栏/预算）与目标评分（规格掩膜、加权目标、排名与帕累托）、Signal VIA 钻孔/功能焊盘/反焊盘三项 HFSS Design Variables 与原生 Parametric Setup 离线生成（含 3×3 结构门禁）、专属“VIA 换层”参数编辑器（进出层/Padstack/背钻/回流孔/走线 + 派生值校验 + 俯视图）、2D/3D 预览、本地 Validate 和独立 HFSS 脚本 `--validate-only`。

尚未实现或仍待真机验收：

- VIA 原生 Optimetrics 的 `pair_pitch`、残桩与回流孔坐标表达式绑定；这些轴当前明确 fail closed。已绑定的 Signal VIA 钻孔/功能焊盘/反焊盘轴仍需真实 AEDT 3×3 求解验收。
- `via_transition` 的真实 AEDT/HFSS 建模、原生 Validate、求解、3×3 Optimetrics 与端到端 TDR 验收；默认 `surface_capacitor` 最小方案的真机结果见 `verification/real_aedt_hfss_validation.json`。

2026-08-21 的续做改动、命令退出码与下一阶段入口见 `docs/CONTINUATION_REPORT_2026-08-21.md`。
