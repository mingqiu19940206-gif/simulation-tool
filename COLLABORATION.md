# Windows Codex + macOS Claude Code 协作指南

远端私有仓库：`https://github.com/mingqiu19940206-gif/simulation-tool`

## 一、协作原则

- `main` 是稳定分支，只接收已经检查过的改动。
- Windows Codex 使用 `codex/...` 分支。
- macOS Claude Code 使用 `claude/...` 分支。
- 每个任务创建一个新分支，通过 Pull Request 合并。
- 两端不要同时直接修改同一分支。
- 提交前查看改动并执行可用检查。

## 二、Mac 第一次配置

在 Mac 终端安装 Git 和 Claude Code，并确认已经登录同一个 GitHub 账号。然后执行：

```bash
cd ~/Documents
git clone https://github.com/mingqiu19940206-gif/simulation-tool.git
cd simulation-tool
git config pull.ff only
git config user.name "你的 GitHub 用户名"
git config user.email "你的 GitHub 邮箱"
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

如果 GitHub 要求认证，按照终端提示在浏览器中登录。不要把密码或访问令牌写入项目文件。

启动 Claude Code：

```bash
cd ~/Documents/simulation-tool
source .venv/bin/activate
claude
```

Claude Code 会读取根目录的 `CLAUDE.md`，并由它加载共享的 `AGENTS.md`。

## 三、Windows Codex 开始任务

告诉 Codex任务内容并要求它使用新分支。标准命令为：

```powershell
git fetch origin
git switch main
git pull --ff-only
git switch -c codex/任务简短名称
```

完成后：

```powershell
git status
git diff --check
git add -A
git commit -m "feat: 简短说明"
git push -u origin codex/任务简短名称
```

然后在 GitHub 创建 Pull Request，目标分支选择 `main`。

## 四、Mac Claude 开始任务

```bash
git fetch origin
git switch main
git pull --ff-only
git switch -c claude/任务简短名称
```

完成后：

```bash
git status
git diff --check
git add -A
git commit -m "fix: 简短说明"
git push -u origin claude/任务简短名称
```

然后在 GitHub 创建 Pull Request，目标分支选择 `main`。

## 五、让 Claude 审查 Codex 的改动

假设 Codex 分支为 `codex/example-task`：

```bash
git fetch origin
git switch --detach origin/codex/example-task
git diff origin/main...HEAD
claude
```

给 Claude 的任务示例：

> 审查当前 Codex 分支相对 origin/main 的改动。重点检查正确性、配置兼容性、路径跨平台问题和遗漏的验证。不要修改代码，先输出按严重程度排序的问题清单。

审查完成后回到主分支：

```bash
git switch main
```

## 六、同步已经合并的改动

Pull Request 合并后，两台电脑都执行：

```bash
git switch main
git fetch origin
git pull --ff-only
```

已经完成且不再需要的本地分支可以稍后删除；不确定时先保留，不要强制删除。

## 七、冲突时怎么做

- 先停止编辑，不要强制推送。
- 保存 `git status` 和冲突文件列表。
- 让其中一端负责解决冲突，另一端暂时不要继续提交。
- 解决后重新执行检查，再正常提交和推送。
- 不要使用 `git reset --hard`、`git checkout -- .` 或 `git push --force` 作为快捷修复。

## 八、平台职责

- Windows：GUI 冒烟、Ansys Electronics Desktop、HFSS、原生 Optimetrics 与最终运行验收。
- macOS：Claude Code 实现、代码审查、配置与算法检查、跨平台无关测试。
- 涉及 AEDT/HFSS 的最终结论必须以 Windows 端真实运行结果为准。
