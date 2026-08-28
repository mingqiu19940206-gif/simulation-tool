# 2.3.2 电容页视口利用率补丁交付报告

交付日期：2026-08-14（Asia/Shanghai）

## 交付结论

本版本以 `delivery/SurfaceCapAntipadTool_v2.3.1_latest` 的正式源码与唯一 PyInstaller 链为基线；继续只在实际开发源 `surface_cap_antipad_tool_v2.3.1` 中修改，目录名保持不变以避免再产生一套重复源码。`VERSION` 与全部发布元数据递增为 2.3.2，再由项目构建、组装和隔离验证脚本生成新交付目录。

- 唯一版本：2.3.2
- 正式源码入口：`src/main_gui.py`
- Windows 程序：`release/SurfaceCapAntipadTool_v2.3.2.exe`
- PyInstaller 配置：`packaging/SurfaceCapAntipadTool.spec`
- 自动构建脚本：`packaging/build_windows.ps1`
- 自动化测试：190 个，190 通过，0 失败，0 错误，0 跳过（与 `verification/test_results.json` 一致）
- GUI 布局矩阵：4 个视口 × 3 个字体档，并覆盖 29/5 层、mm/mil、VIA 开关与窗格拖动
- 可视化证据：12 张，1120×720、1900×1050、2560×1400 各四页
- EXE 发布自检、GUI 启动、默认等轴 3D 旋转/平移/缩放/Validate 冒烟：通过
- ZIP 隔离副本：190 个测试、源码验证、EXE 自检、GUI 与 3D 冒烟全部通过
- 真实 AEDT/HFSS：本次未启动 AEDT、未求解；仅执行脚本 `--validate-only` 与对象/端口/扫参结构验证

## 根因与布局修复

原实现把多个 Canvas 按不可见的固定最小宽度绘图，同时用固定像素行高、固定说明栏和固定 HUD/图例位置。DPI 或字号增大后，文本真实 `linespace` 大于几何行高；29 层内容又被压缩到有限高度，造成重叠、裁切和末层不可达。

2.3.1 已完成 Canvas、字体 metrics、长叠层滚动及标签碰撞修复。2.3.2 进一步修复电容整页在高视口中的高度分配：外层滚动内容高度取自然高度与真实视口高度的较大值，内容不足一屏时由主体 PanedWindow 吸收剩余高度；几何预览依据视口和 `TkDefaultFont.metrics("linespace")` 在 250–440 px 的可读范围内调整，验证状态保持贴合内容底边。小窗口中自然内容仍大于视口并完整滚动可达。

响应式字号现在同时检查实际窗口宽度、高度和当前显示器 DPI；normal/large/ultra 增量为 0/+2/+3 pt。Treeview 行高、PanedWindow、工具栏、wraplength 和 Canvas 重绘随字体档同步更新。

## 语义零变化验证

`verification/semantic_invariance.json` 对 V2.3.1 正式基线执行了两类验证：

1. 配置、单位、几何、HFSS/扫参等 9 个语义文件 SHA-256 完全一致。
2. mm/mil × GND VIA 启用/禁用四种运行时组合的归一化配置、29 层 M6G、几何快照和生成 HFSS 脚本哈希一致。

结果为 4/4 组合通过、9/9 语义文件一致。GUI 修复没有改变配置语义、单位换算、M6G 数据、几何、HFSS 对象、四 Port、扫参或脚本输出。

## 自动与视觉验证证据

- `verification/test_results.json`：正式构建链 190/190 通过。
- `verification/source_verification.json`：默认配置、保存/读取、本地 Validate、51 个 3D 场景对象、四 Port、空气盒、HFSS `--validate-only`、45°/S 弯和 Optimetrics。
- `verification/semantic_invariance.json`：V2.3.0 对比的语义零变化结果。
- `verification/visual_acceptance.json`：12 张 PNG 的尺寸、SHA-256 和人工巡检清单。
- `verification/screenshots/1120x720/`：GND VIA、反焊盘与叠层、电容与模型、模型预览。
- `verification/screenshots/1900x1050/`：同四页。
- `verification/screenshots/2560x1400/`：同四页。
- `verification/exe_model_preview_smoke.json`：EXE 构建身份、GUI 和 3D 冒烟。
- `verification/isolation_test_results.json`：ZIP 隔离副本 190/190 通过。
- `verification/isolation_source_verification.json`、`isolation_exe_self_check.json`、`isolation_summary.json`：隔离自检证据。

截图通过真实 Windows Tk HWND 的 `PrintWindow` 路径捕获，不是重新绘制的示意图。人工巡检与 Canvas bbox 测试共同确认：文本组无非预期相交；非滚动画布文本在真实视口内；滚动画布内容在 `scrollregion` 内；29 层不丢失、不重复且末层可达；P/N/X/Y、`pad_pitch`、`cap_pitch` 各出现一次；四个 Port 与合并的 AIRBOX/开放边界标签不重叠。

## 发布与清单

- `FILE_MANIFEST.txt`：交付目录全部文件的相对路径、字节数和 SHA-256。
- `SHA256SUMS.txt`：EXE、默认配置、构建元数据、构建脚本、spec、测试和视觉证据等关键文件。
- 完整 ZIP 的最终 SHA-256 写在 ZIP 旁的同名 `.sha256` 文件中；ZIP 不能在自身内部保存最终哈希而不改变该哈希。

PyInstaller 对 PyAEDT 可选 Nastran 扩展中的 Python 3.10–3.13 `.pyd` 给出缺少对应 Python DLL 的警告；这些可选扩展不在本工具调用链。当前 Python 3.14 EXE 的发布自检、GUI 和 3D 冒烟均已通过。
