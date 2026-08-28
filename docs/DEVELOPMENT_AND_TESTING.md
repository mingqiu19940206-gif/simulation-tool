# 开发、测试与构建

## 源码入口与调用链

正式入口：`src/main_gui.py`。

它继承 `src/main_gui_legacy.py` 的通用 Tk 页面和配置/生成流程，再接入反焊盘、GND VIA、3D 预览、结构化 Validate 和仿真扫参工作台。`legacy` 文件仍被正式 import，不能删除：

- `main_gui.py -> main_gui_legacy.py`
- `antipad_editor/__init__.py -> antipad_editor_legacy/__init__.py -> antipad_editor_preview_base.py -> antipad_editor_core.py`
- `geometry_calculator.py -> geometry_v1.py`

顶层重复的 `antipad_editor.py`、`antipad_editor_legacy.py` 和 `antipad_semantic.py` 未进入交付，因为运行时解析实际加载同名包目录；对应逻辑已由包入口和 `*_core.py` 提供。

## 建议环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
```

## 测试

```powershell
python -m unittest discover -s tests -v
python tools\verify_delivery.py verification\source_verification.json
python src\main_gui.py --smoke-test
python src\main_gui.py --model-preview-smoke-test
```

`--model-preview-smoke-test` 会实际创建 GUI，切换页面，检查四个 Port、空气盒和开放边界，绘制 3D 软件视口，并通过合成鼠标事件验证旋转、平移、滚轮缩放和 Validate 后对象保持。

## EXE 构建

唯一构建入口：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

脚本使用临时工作目录，不在项目中留下 `build/`、缓存或构建日志。结果固定为 `release/SurfaceCapAntipadTool_v2.4.0.exe`。唯一 spec 为 `packaging/SurfaceCapAntipadTool.spec`。

## 配置与用户输出

默认配置是 `config/default_config.json`。源码模式相对输出以 `src/` 为基准，EXE 模式相对输出以 EXE 所在 `release/` 为基准。用户保存的配置、最近记录、日志和 `.aedt` 工程不是源码交付内容。

### 电容安装面配置

- `capacitor.mount_side`: `top` 或 `bottom`，默认 `top`。
- 缺失字段按 `top` 迁移并记录修正；其他字符串会产生配置错误。
- `gnd_vias.create_top_ground_copper` / `top_ground_clearance` 为旧名称兼容字段，实际作用于当前安装面。
- `GeometryResult`/快照中的 `mount_side`、`surface_normal`、`signal_layer_name`、`signal_z_bottom`、`signal_z_top`、`component_outward_z`、`reference_layer_name` 和 `soldermask_openings` 是预览、Validate 与 HFSS 的共同坐标契约。
- 底层要求最后一个 `signal` 层为真实外层铜，其下只能有阻焊层；自动参考层从底层向板内选择最近的有效参考平面。
