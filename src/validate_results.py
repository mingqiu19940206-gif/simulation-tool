"""Structured Validate results page for local and HFSS checks."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Optional

from model_validator import CATEGORIES, ValidationItem, ValidationReport, export_report, load_report


FILTERS = (
    "全部", "仅错误", "仅警告", "仅未检查", "仅本地检查", "仅HFSS检查",
)
ICONS = {"error": "✖", "warning": "⚠", "info": "✓", "not_checked": "○"}
LEVEL_TEXT = {"error": "错误", "warning": "警告", "info": "通过", "not_checked": "未检查"}
STATE_TEXT = {
    "passed": "通过：具备建模/求解条件（不保证收敛或结果准确）",
    "passed_with_warnings": "通过但有警告：确认后可继续",
    "failed": "失败：存在错误，禁止生成或运行",
    "stale": "结果已过期：参数已发生变化",
    "incomplete": "检查未完成",
}
STATE_COLOR = {
    "passed": "#166534", "passed_with_warnings": "#b45309", "failed": "#b42318",
    "stale": "#64748b", "incomplete": "#64748b",
}


class ValidateResultsWorkbench(ttk.Frame):
    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.app = app
        self.report: Optional[ValidationReport] = None
        self.visible_items: Dict[str, ValidationItem] = {}
        self.filter_var = tk.StringVar(value="全部")
        self.state_var = tk.StringVar(value="尚未运行模型检查")
        self.mode_var = tk.StringVar(value="检查模式：—")
        self.time_var = tk.StringVar(value="检查时间：—")
        self.revision_var = tk.StringVar(value="配置版本：—")
        self.progress_var = tk.StringVar(value="等待检查")
        self.detail_var = tk.StringVar(value="选择一条检查结果查看详情。")
        self.count_vars = {name: tk.StringVar(value="0") for name in ("passed", "errors", "warnings", "not_checked")}
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self, padding=(4, 2)); toolbar.pack(fill="x")
        ttk.Button(toolbar, text="快速检查", command=self.app.validate_model).pack(side="left")
        ttk.Button(toolbar, text="HFSS工程检查", command=self.app.run_hfss_project_validate).pack(side="left", padx=4)
        ttk.Button(toolbar, text="取消HFSS检查", command=self.app.cancel_hfss_validate).pack(side="left")
        ttk.Button(toolbar, text="导入HFSS结果…", command=self._import_hfss).pack(side="left", padx=(12, 3))
        ttk.Button(toolbar, text="导出 JSON…", command=lambda: self._export(".json")).pack(side="left")
        ttk.Button(toolbar, text="导出TXT…", command=lambda: self._export(".txt")).pack(side="left", padx=3)
        ttk.Label(toolbar, text="筛选：").pack(side="left", padx=(18, 2))
        combo = ttk.Combobox(toolbar, textvariable=self.filter_var, values=FILTERS, state="readonly", width=12)
        combo.pack(side="left"); combo.bind("<<ComboboxSelected>>", lambda _event: self._rebuild())

        summary = ttk.LabelFrame(self, text="检查汇总", padding=7); summary.pack(fill="x", padx=4, pady=3)
        for index, (key, label, color) in enumerate((
            ("passed", "通过", "#166534"), ("errors", "错误", "#b42318"),
            ("warnings", "警告", "#b45309"), ("not_checked", "未检查", "#64748b"),
        )):
            card = ttk.Frame(summary, padding=(12, 3)); card.grid(row=0, column=index, sticky="nsew", padx=3)
            ttk.Label(card, text=label).pack()
            ttk.Label(card, textvariable=self.count_vars[key], foreground=color,
                      font=("TkDefaultFont", 15, "bold")).pack()
            summary.columnconfigure(index, weight=1)
        metadata = ttk.Frame(summary); metadata.grid(row=0, column=4, sticky="nsew", padx=(12, 0))
        ttk.Label(metadata, textvariable=self.mode_var).pack(anchor="w")
        ttk.Label(metadata, textvariable=self.time_var).pack(anchor="w")
        ttk.Label(metadata, textvariable=self.revision_var).pack(anchor="w")
        summary.columnconfigure(4, weight=2)
        self.state_label = ttk.Label(summary, textvariable=self.state_var, font=("TkDefaultFont", 10, "bold"))
        self.state_label.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        progress_row = ttk.Frame(summary); progress_row.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(5, 0))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, maximum=100); self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_row, textvariable=self.progress_var).grid(row=0, column=1, padx=(8, 0))

        pane = ttk.PanedWindow(self, orient="horizontal"); pane.pack(fill="both", expand=True, padx=4, pady=3)
        left, right = ttk.Frame(pane), ttk.Frame(pane, padding=(7, 2)); pane.add(left, weight=3); pane.add(right, weight=2)
        columns = ("level", "rule", "source", "message")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="类别"); self.tree.column("#0", width=150, stretch=False)
        for column, label, width in (("level", "状态", 70), ("rule", "问题编号", 90),
                                     ("source", "来源", 95), ("message", "说明", 520)):
            self.tree.heading(column, text=label); self.tree.column(column, width=width, stretch=column == "message")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.tag_configure("error", foreground="#b42318")
        self.tree.tag_configure("warning", foreground="#b45309")
        self.tree.tag_configure("info", foreground="#166534")
        self.tree.tag_configure("not_checked", foreground="#64748b")

        ttk.Label(right, text="检查详情", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.detail_var, justify="left", wraplength=440).pack(fill="x", pady=(5, 10))
        actions = ttk.Frame(right); actions.pack(fill="x")
        ttk.Button(actions, text="定位模型", command=self._locate).pack(side="left")
        ttk.Button(actions, text="前往参数", command=self._goto).pack(side="left", padx=3)
        ttk.Button(actions, text="重新检查", command=self.app.validate_model).pack(side="left")
        self.fix_button = ttk.Button(actions, text="自动修复", command=self._auto_fix, state="disabled")
        self.fix_button.pack(side="left", padx=3)

        raw = ttk.LabelFrame(right, text="原始日志", padding=4); raw.pack(fill="both", expand=True, pady=(10, 0))
        self.raw_text = tk.Text(raw, wrap="word", height=12, font=("TkFixedFont", 9),
                                background="#101418", foreground="#e6edf3")
        self.raw_text.pack(fill="both", expand=True)

    def set_progress(self, text: str, percent: int) -> None:
        self.progress["value"] = percent; self.progress_var.set(text)
        try:
            self.update_idletasks()
        except tk.TclError:
            pass

    def show_report(self, report: ValidationReport) -> None:
        self.report = report
        counts = report.counts
        for key, value in counts.items(): self.count_vars[key].set(str(value))
        self.mode_var.set("检查模式：" + report.mode)
        self.time_var.set("检查时间：" + (report.timestamp or "—"))
        self.revision_var.set("配置版本：" + (report.config_revision or "—"))
        self.state_var.set(STATE_TEXT[report.state])
        self.state_label.configure(foreground=STATE_COLOR[report.state])
        self.progress["value"] = 100 if not report.incomplete else 0
        self.progress_var.set("检查完成" if not report.incomplete else "检查未完成")
        self.raw_text.delete("1.0", "end"); self.raw_text.insert("1.0", report.raw_log)
        self._rebuild()

    def mark_stale(self, revision: str = "") -> None:
        if not self.report or self.report.stale:
            return
        if not revision or revision != self.report.config_revision:
            self.report.stale = True
            self.state_var.set(STATE_TEXT["stale"]); self.state_label.configure(foreground=STATE_COLOR["stale"])

    def _matches(self, item: ValidationItem) -> bool:
        selected = self.filter_var.get()
        return (selected == "全部" or
                selected == "仅错误" and item.level == "error" or
                selected == "仅警告" and item.level == "warning" or
                selected == "仅未检查" and item.level == "not_checked" or
                selected == "仅本地检查" and item.source == "local" or
                selected == "仅HFSS检查" and item.source == "hfss")

    def _rebuild(self) -> None:
        for iid in self.tree.get_children(): self.tree.delete(iid)
        self.visible_items.clear()
        if not self.report: return
        index = 0
        for category in CATEGORIES:
            matching = [item for item in self.report.items if item.category == category and self._matches(item)]
            if not matching: continue
            parent = f"category:{category}"; self.tree.insert("", "end", iid=parent, text=category, open=True)
            for item in matching:
                iid = f"item:{index}"; index += 1; self.visible_items[iid] = item
                source = "本地快速预检" if item.source == "local" else "HFSS工程检查"
                self.tree.insert(parent, "end", iid=iid, text="", tags=(item.level,),
                                 values=(f"{ICONS[item.level]} {LEVEL_TEXT[item.level]}", item.rule_id, source, item.message))

    def _current(self) -> Optional[ValidationItem]:
        selected = self.tree.selection()
        return self.visible_items.get(selected[0]) if selected else None

    def _selected(self, _event: Any = None) -> None:
        item = self._current()
        if not item: return
        self.detail_var.set(
            f"{ICONS[item.level]} {item.rule_id} · {LEVEL_TEXT[item.level]}\n"
            f"来源：{'本地快速预检' if item.source == 'local' else 'HFSS工程检查'}\n"
            f"类别：{item.category}\n\n{item.message}\n\n"
            f"涉及对象：{', '.join(item.object_keys) or '—'}\n"
            f"对应参数：{item.parameter_path or '—'}\n"
            f"修改建议：{item.suggestion or '—'}"
        )
        self.fix_button.configure(state="normal" if item.auto_fix_id else "disabled")

    def _locate(self) -> None:
        item = self._current()
        if item: self.app.locate_validate_item(item)

    def _goto(self) -> None:
        item = self._current()
        if item and item.parameter_path: self.app._navigate_to_parameter(item.parameter_path)

    def _auto_fix(self) -> None:
        item = self._current()
        if item and item.auto_fix_id: self.app.apply_validate_auto_fix(item)

    def _export(self, suffix: str) -> None:
        if not self.report:
            messagebox.showinfo("导出检查报告", "尚无检查结果。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=suffix,
                                            filetypes=((suffix[1:].upper(), "*" + suffix),),
                                            initialfile="validate_report" + suffix)
        if path:
            export_report(self.report, Path(path)); self.app.status.set("检查报告已导出：" + Path(path).name)

    def _import_hfss(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=(("Validate JSON", "*.json"), ("所有文件", "*.*")))
        if not path: return
        try:
            self.app.merge_hfss_report(load_report(Path(path)))
        except Exception as exc:
            messagebox.showerror("导入HFSS检查结果失败", str(exc), parent=self)
