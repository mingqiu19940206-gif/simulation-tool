# Git 状态与快照基线

更新时间：2026-08-27（Asia/Shanghai）

## 当前状态

本项目已置于 Git 版本控制之下。

- 远端：`https://github.com/mingqiu19940206-gif/simulation-tool`
- 稳定分支：`main`，当前 `303c37b`（合并 PR #1）
- 协作分支命名：Windows Codex 用 `codex/<任务名>`，macOS Claude Code 用 `claude/<任务名>`
- 分支保护约定：不直接提交到 `main`，一律经 Pull Request 合并（见 `AGENTS.md` 与 `COLLABORATION.md`）

`main` 的提交历史：

| 提交 | 说明 |
| --- | --- |
| `55ce6b2` | 初始化跨平台仓库（54 文件） |
| `e22fd87` | 恢复 `tests/`、`docs/`、`packaging/`、`tools/`、`verification/` |
| `81ba5ce` | 修复验证门（round-trip 比较、Tk 刷新循环） |
| `303c37b` | 合并 PR #1 |

当前测试套件规模：`tests/` 下 344 个用例，其中包含 Windows 关闭路径与
`report_callback_exception` 交互/headless 行为的回归覆盖。

## 测试计数说明

`verification/` 下的 `*_test_results.json` 是**特定时点在 Windows 上的运行记录**，不是当前套件的实时状态。各文件记录的数量不同是因为记录时间不同，并非互相矛盾：

| 文件 | 记录数量 | 时点 |
| --- | --- | --- |
| `baseline_v2.3.0_test_results.json` | 184 | v2.3.0 基线 |
| `test_results.json` | 344 | 2.4.0 PR #2 Windows 接手验证 |
| `isolation_test_results.json` | 344 | 同一候选 ZIP 的 Windows 隔离验证 |
| `via_antipad_test_results.json` | 286 | 2026-08-21 反焊盘修复 |

引用这些文件时应连同时点一起引用，不要当作当前套件的通过状态。

## 历史存档（2026-08-14，Git 之前）

以下内容记录本仓库建立 Git 之前的磁盘快照策略，仅作历史存档保留，**不再描述当前状态**：

> 当时工作区根目录及候选项目目录均不存在 `.git` 元数据，系统 `PATH` 和常见安装位置也没有可用 Git，因此无法报告分支、HEAD、暂存区或未跟踪文件。交付采取磁盘快照策略：以 `delivery/SurfaceCapAntipadTool_v2.3.0_latest` 为发布基线；从 `main_gui.py` 解析运行时 import 确认真实加载路径；检查默认配置、模板、资源与 PyInstaller 数据引用；在正式开发源运行测试；通过原 PyInstaller 构建链生成 EXE 与交付 ZIP。
>
> 2.3.2 仅修改 GUI 布局文件、GUI 测试、验证工具和发布元数据；语义不变工具确认配置、几何、叠层、HFSS 和扫参调用链相对 2.3.1 未变化。原始工作区未执行 reset、checkout、clean、删除或覆盖操作。
