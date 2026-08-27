# Repository Instructions

These instructions apply to Codex, Claude Code, and human contributors.

## Project overview

- Product: SurfaceCapAntipadTool 2.4.0.
- Language: Python 3.11-3.14.
- Source entry point: `src/main_gui.py`.
- Runtime configuration: `config/default_config.json` and `config/build_metadata.json`.
- Install dependencies with `python -m pip install -r requirements.txt`.
- Full AEDT/HFSS generation and solve validation requires Windows with a supported Ansys Electronics Desktop installation and license.
- macOS can be used for code editing, review, configuration checks, and platform-independent logic, but it must not claim that AEDT/HFSS execution passed.

## Git ownership and branches

- Never commit or push directly to `main`.
- Windows Codex branches must use `codex/<short-task-name>`.
- macOS Claude branches must use `claude/<short-task-name>`.
- Before starting, fetch `origin`, update local `main` with `git pull --ff-only`, then create a fresh task branch.
- Do not commit to, force-push, delete, or rewrite a branch owned by the other agent.
- Do not edit the same feature concurrently on two branches unless the user explicitly assigns separate files or review roles.
- Never use `git push --force` or destructive Git commands unless the user explicitly requests and confirms the exact operation.
- Preserve unrelated working-tree changes.

## Change discipline

- Inspect the relevant source and configuration before editing.
- Keep changes focused on the requested task; avoid unrelated refactors.
- Do not commit credentials, tokens, `.env` files, logs, generated AEDT projects, caches, or build output.
- Treat `config/default_config.json` as production data: preserve schema version and backward compatibility unless the task explicitly changes them.
- Keep Windows and macOS path handling platform-neutral wherever possible; use `pathlib.Path` instead of hard-coded separators in Python.
- Avoid case-only file renames because Windows and default macOS file systems are normally case-insensitive.

## Verification

Run the strongest checks available for the files changed.

Minimum platform-independent check:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -c "import config_manager; c=config_manager.get_default_config(); import main_gui; print(c.get('schema_version'))"
```

Windows PowerShell equivalent:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "$PWD\src"
python -B -c "import config_manager; c=config_manager.get_default_config(); import main_gui; print(c.get('schema_version'))"
```

When GUI execution is available on Windows, also run:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -B src\main_gui.py --smoke-test
```

Always run `git diff --check` before committing. If a check cannot run, state the exact reason instead of reporting it as passed.

## Handoff requirements

Every completion message and pull request must state:

1. Goal and branch name.
2. Files changed.
3. Commands/checks run and their results.
4. Known limitations or unverified behavior.
5. Recommended next action for the other agent.

Read `COLLABORATION.md` for the complete Windows/macOS workflow.
