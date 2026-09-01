"""Optional PySide6 desktop shell for the existing synchronous Agent runtime."""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import config, interactive, ui
from .checkpoint import CheckpointError
from .command_runtime import read_saved_output_range
from .evidence_report import report_markdown
from .gui_data import (
    ProjectEntry,
    RecentWorkspaceStore,
    WorkspaceDataError,
    project_entries,
    read_workspace_text,
    switch_workspace,
)
from .gui_presenter import ActivityItem, GuiPresenter, OutcomeView
from .log import RunLog
from .session import SessionStore

try:
    from PySide6.QtCore import (
        QFileSystemWatcher,
        QObject,
        Qt,
        QThread,
        QTimer,
        QUrl,
        Signal,
        Slot,
    )
    from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSplitter,
        QTextBrowser,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # CLI remains dependency-light when GUI extras are absent.
    QObject = None  # type: ignore[assignment]


COLORS = {
    "background": "#111318",
    "panel": "#171a21",
    "panel_alt": "#1d212a",
    "border": "#2a303b",
    "text": "#e7eaf0",
    "muted": "#8f98a8",
    "accent": "#74b3ff",
    "success": "#66d19e",
    "failure": "#ff7b86",
    "warning": "#e6bd69",
}


if QObject is not None:

    PROJECT_NODE_PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    class ApprovalTicket:
        """One blocking permission question bridged safely to the GUI thread."""

        def __init__(self, prompt: str):
            self.prompt = prompt
            self.answer = "3"
            self.ready = threading.Event()

        def resolve(self, answer: str) -> None:
            if self.ready.is_set():
                return
            self.answer = answer
            self.ready.set()


    class AgentWorker(QObject):
        event_emitted = Signal(object)
        approval_requested = Signal(object)
        done = Signal(object, int)

        def __init__(
            self,
            runtime: Any,
            task: str | None = None,
            active: Any = None,
            resume_selector: str | None = None,
        ):
            super().__init__()
            self.runtime = runtime
            self.task = task
            self.active = active
            self.resume_selector = resume_selector
            self.stop_event = threading.Event()
            self._ticket: ApprovalTicket | None = None
            self._ticket_lock = threading.Lock()

        def _answer(self, prompt: str) -> str:
            if self.stop_event.is_set():
                return "3"
            ticket = ApprovalTicket(prompt)
            with self._ticket_lock:
                self._ticket = ticket
            self.approval_requested.emit(ticket)
            ticket.ready.wait()
            with self._ticket_lock:
                self._ticket = None
            return ticket.answer

        def _confirm_resume(self, expected: str, actual: str) -> bool:
            answer = self._answer(
                "会话的 Git 基线已变化。\n\n"
                f"原基线：{expected[:12]}\n当前基线：{actual[:12]}\n\n"
                "  [1] 在当前代码上恢复会话\n"
                "  [2] 在当前代码上恢复会话\n"
                "  [3] 取消恢复\n"
                "Choose [1/2/3]: "
            )
            return answer in {"1", "2"}

        def request_stop(self) -> None:
            self.stop_event.set()
            if self.active is not None:
                self.active.st.request_cancel()
            with self._ticket_lock:
                ticket = self._ticket
            if ticket is not None:
                ticket.resolve("3")

        @Slot()
        def run(self) -> None:
            try:
                logger = RunLog(event_sink=self.event_emitted.emit)
                if self.resume_selector is not None:
                    self.active = self.runtime._resume_active(
                        self.resume_selector,
                        logger,
                        cancel_event=self.stop_event,
                        confirm_stale=self._confirm_resume,
                    )
                else:
                    task, references = interactive.expand_file_references(
                        self.task or "",
                        config.WORKSPACE_DIR,
                    )
                    if self.active is None:
                        self.active = self.runtime._new_active(
                            task,
                            logger,
                            references,
                            cancel_event=self.stop_event,
                        )
                    else:
                        self.active.st.cancel_event = self.stop_event
                        self.runtime._start_followup(self.active, task, logger, references)
                self.active.st.permissions.answerer = self._answer
                code = self.runtime._run_active(self.active, logger)
            except Exception as exc:  # noqa: BLE001 - worker boundary reports fatal errors.
                self.event_emitted.emit(
                    {
                        "event": "gui_error",
                        "message": f"无法运行任务：{type(exc).__name__}: {exc}",
                    }
                )
                code = 1
            finally:
                if self.active is not None:
                    self.active.st.permissions.answerer = None
            self.done.emit(self.active, code)


    class AgentWindow(QMainWindow):
        def __init__(self, runtime: Any):
            super().__init__()
            self.runtime = runtime
            self.presenter = GuiPresenter()
            self.active = None
            self.recent_store = RecentWorkspaceStore()
            self._thread: QThread | None = None
            self._worker: AgentWorker | None = None
            self._running = False
            self._run_started_at = 0.0
            self._all_project_entries: list[ProjectEntry] = []
            self._build()
            self._project_watcher = QFileSystemWatcher(self)
            self._project_watcher.directoryChanged.connect(self._schedule_project_refresh)
            self._project_refresh_timer = QTimer(self)
            self._project_refresh_timer.setSingleShot(True)
            self._project_refresh_timer.setInterval(150)
            self._project_refresh_timer.timeout.connect(self._refresh_project_files)
            try:
                self.recent_store.remember(config.WORKSPACE_DIR)
            except (OSError, WorkspaceDataError):
                pass
            self._refresh_project_files()
            self._render_all()

        def _build(self) -> None:
            self.setWindowTitle("zxn Coding Agent")
            self.resize(1240, 800)
            self.setMinimumSize(900, 600)

            root = QWidget()
            outer = QVBoxLayout(root)
            outer.setContentsMargins(22, 18, 22, 18)
            outer.setSpacing(14)

            header = QHBoxLayout()
            title = QLabel("zxn Coding Agent")
            title.setObjectName("appTitle")
            self.workspace_label = QLabel(config.WORKSPACE_DIR)
            self.workspace_label.setObjectName("workspace")
            self.workspace_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.model_label = QLabel(config.MODEL_NAME)
            self.model_label.setObjectName("workspace")
            self.status_label = QLabel("● 就绪")
            self.status_label.setObjectName("status")
            self.open_button = QPushButton("打开工作区")
            self.open_button.setObjectName("headerButton")
            self.open_button.clicked.connect(self._open_workspace)
            self.recent_button = QPushButton("最近使用")
            self.recent_button.setObjectName("headerButton")
            self.recent_button.clicked.connect(self._open_recent_workspace)
            self.history_button = QPushButton("历史记录")
            self.history_button.setObjectName("headerButton")
            self.history_button.clicked.connect(self._show_history)
            header.addWidget(title)
            header.addWidget(self.workspace_label, 1)
            header.addWidget(self.model_label)
            header.addWidget(self.status_label)
            header.addWidget(self.open_button)
            header.addWidget(self.recent_button)
            header.addWidget(self.history_button)
            outer.addLayout(header)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            project_panel = QWidget()
            project_panel.setObjectName("projectPanel")
            project_layout = QVBoxLayout(project_panel)
            project_layout.setContentsMargins(12, 14, 12, 14)
            project_title = QLabel("项目")
            project_title.setObjectName("sectionTitle")
            project_layout.addWidget(project_title)
            self.file_filter = QLineEdit()
            self.file_filter.setPlaceholderText("筛选文件…  Ctrl+P")
            self.file_filter.textChanged.connect(self._filter_project_files)
            project_layout.addWidget(self.file_filter)
            self.project_tree = QTreeWidget()
            self.project_tree.setHeaderHidden(True)
            self.project_tree.setObjectName("projectTree")
            self.project_tree.itemActivated.connect(self._open_project_item)
            project_layout.addWidget(self.project_tree, 3)
            changes_title = QLabel("改动")
            changes_title.setObjectName("sectionTitle")
            project_layout.addWidget(changes_title)
            self.changes_list = QListWidget()
            self.changes_list.setObjectName("changesList")
            self.changes_list.itemActivated.connect(self._open_change_item)
            project_layout.addWidget(self.changes_list, 2)
            change_actions = QHBoxLayout()
            self.restore_button = QPushButton("恢复")
            self.restore_button.setToolTip("恢复本次任务由 Agent 文件工具造成的改动")
            self.restore_button.clicked.connect(self._restore_task_changes)
            self.restore_button.setEnabled(False)
            self.export_button = QPushButton("导出")
            self.export_button.setToolTip("导出 Runtime 证据报告")
            self.export_button.clicked.connect(self._export_evidence_report)
            self.export_button.setEnabled(False)
            change_actions.addWidget(self.restore_button)
            change_actions.addWidget(self.export_button)
            project_layout.addLayout(change_actions)
            splitter.addWidget(project_panel)

            self.activity = QTextBrowser()
            self.activity.setObjectName("activity")
            self.activity.setOpenExternalLinks(False)
            self.activity.setFrameShape(QFrame.Shape.NoFrame)
            self.activity.anchorClicked.connect(self._open_activity_link)
            splitter.addWidget(self.activity)

            sidebar = QWidget()
            sidebar.setObjectName("sidebar")
            side_layout = QVBoxLayout(sidebar)
            side_layout.setContentsMargins(18, 16, 18, 16)
            side_layout.setSpacing(20)

            self.plan_title = QLabel("计划")
            self.plan_title.setObjectName("sectionTitle")
            side_layout.addWidget(self.plan_title)
            self.plan_host = QWidget()
            self.plan_host.setObjectName("planHost")
            self.plan_layout = QVBoxLayout(self.plan_host)
            self.plan_layout.setContentsMargins(0, 0, 0, 0)
            self.plan_layout.setSpacing(9)
            side_layout.addWidget(self.plan_host)
            side_layout.addStretch(1)

            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setObjectName("separator")
            side_layout.addWidget(separator)
            verification_title = QLabel("验证")
            verification_title.setObjectName("sectionTitle")
            side_layout.addWidget(verification_title)
            self.verification = QLabel()
            self.verification.setWordWrap(True)
            self.verification.setTextFormat(Qt.TextFormat.RichText)
            self.verification.setObjectName("verification")
            side_layout.addWidget(self.verification)
            splitter.addWidget(sidebar)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 3)
            splitter.setStretchFactor(2, 1)
            splitter.setSizes([250, 700, 290])
            outer.addWidget(splitter, 1)

            input_row = QHBoxLayout()
            input_row.setSpacing(12)
            self.task_input = QPlainTextEdit()
            self.task_input.setPlaceholderText("输入编程任务…  (Ctrl+Enter 运行)")
            self.task_input.setMaximumHeight(92)
            self.task_input.setObjectName("taskInput")
            self.run_button = QPushButton("运行")
            self.run_button.setObjectName("runButton")
            self.run_button.setMinimumSize(96, 46)
            self.run_button.clicked.connect(self._run_or_stop)
            input_row.addWidget(self.task_input, 1)
            input_row.addWidget(self.run_button)
            outer.addLayout(input_row)

            QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._start_task)
            QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._start_task)
            QShortcut(QKeySequence("Ctrl+P"), self, activated=self._focus_file_filter)
            self.setCentralWidget(root)
            self.setStyleSheet(_stylesheet())
            self.task_input.setFocus()

        @Slot()
        def _run_or_stop(self) -> None:
            if self._running:
                self._request_stop()
            else:
                self._start_task()

        @Slot()
        def _start_task(self) -> None:
            if self._running:
                return
            task = self.task_input.toPlainText().strip()
            if not task:
                return
            self.task_input.clear()
            self._start_worker(task=task)

        def _start_worker(
            self,
            *,
            task: str | None = None,
            resume_selector: str | None = None,
        ) -> None:
            self._set_running(True)
            self._run_started_at = time.monotonic()
            self.run_button.setEnabled(False)
            QTimer.singleShot(
                300,
                lambda: self.run_button.setEnabled(True) if self._running else None,
            )

            self._thread = QThread(self)
            self._worker = AgentWorker(
                self.runtime,
                task,
                self.active if resume_selector is None else None,
                resume_selector,
            )
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            self._worker.event_emitted.connect(self._consume_event)
            self._worker.approval_requested.connect(self._show_approval)
            self._worker.done.connect(self._task_done)
            thread = self._thread
            self._worker.done.connect(lambda _active, _code, owner=thread: owner.quit())
            self._worker.done.connect(self._worker.deleteLater)
            self._thread.finished.connect(self._thread_finished)
            self._thread.start()

        @Slot()
        def _request_stop(self) -> None:
            if not self._running or self._worker is None:
                return
            if time.monotonic() - self._run_started_at < 0.25:
                return
            self.run_button.setText("正在停止…")
            self.run_button.setEnabled(False)
            self.status_label.setText("■ 正在停止")
            self._worker.request_stop()

        @Slot(object)
        def _consume_event(self, event: object) -> None:
            self.presenter.consume(event)
            if isinstance(event, dict) and (
                event.get("event") == "turn_summary"
                or (
                    event.get("event") == "tool_result"
                    and isinstance(event.get("file_changes"), list)
                    and bool(event.get("file_changes"))
                )
            ):
                self._refresh_project_files()
            self._render_all()

        @Slot(object)
        def _show_approval(self, ticket: ApprovalTicket) -> None:
            prompt = _redact(ticket.prompt)
            body, options = _approval_parts(prompt)
            box = QMessageBox(self)
            box.setWindowTitle("需要确认")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(body)
            box.setInformativeText("本次授权仅在当前 Agent 进程中有效。")
            buttons: dict[Any, str] = {}
            for number in ("1", "2", "3"):
                label = options.get(number, "拒绝" if number == "3" else f"选项 {number}")
                role = (
                    QMessageBox.ButtonRole.RejectRole
                    if number == "3"
                    else QMessageBox.ButtonRole.AcceptRole
                )
                buttons[box.addButton(label, role)] = number
            stop_button = box.addButton("停止任务", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            if box.clickedButton() is stop_button:
                self._request_stop()
                ticket.resolve("3")
            else:
                ticket.resolve(buttons.get(box.clickedButton(), "3"))

        @Slot(object, int)
        def _task_done(self, active: object, code: int) -> None:
            self.active = active
            if code != 0 and not any(item.tone == "failure" for item in self.presenter.activity[-3:]):
                self.presenter.consume(
                    {"event": "gui_error", "message": "Agent 因运行时错误而停止。"}
                )
                self._render_all()

        @Slot()
        def _thread_finished(self) -> None:
            owner = self.sender()
            if isinstance(owner, QThread):
                owner.deleteLater()
            self._thread = None
            self._worker = None
            self._set_running(False)
            self._refresh_project_files()
            self._render_all()

        def _set_running(self, running: bool) -> None:
            self._running = running
            self.run_button.setEnabled(True)
            self.task_input.setEnabled(not running)
            self.open_button.setEnabled(not running)
            self.recent_button.setEnabled(not running)
            self.history_button.setEnabled(not running)
            self.restore_button.setEnabled(not running and self.active is not None)
            self.export_button.setEnabled(
                not running and self.presenter.evidence_report is not None
            )
            self.run_button.setText("停止" if running else "运行")
            self.status_label.setText("● 运行中" if running else self._idle_status())
            if not running:
                self.task_input.setFocus()

        def _idle_status(self) -> str:
            state = self.presenter.verification
            if state.current and state.adequate and state.task_completed:
                return "✓ 已验证"
            if state.required and not state.current:
                return "⚠ 验证已失效"
            return "● 就绪"

        @Slot()
        def _focus_file_filter(self) -> None:
            self.file_filter.setFocus()
            self.file_filter.selectAll()

        @Slot()
        def _open_workspace(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "打开工作区",
                config.WORKSPACE_DIR,
            )
            if selected:
                self._switch_workspace(selected)

        @Slot()
        def _open_recent_workspace(self) -> None:
            recent = self.recent_store.load()
            if not recent:
                QMessageBox.information(self, "最近使用的工作区", "还没有最近使用的工作区。")
                return
            labels = [f"{Path(path).name}  —  {path}" for path in recent]
            selected, ok = QInputDialog.getItem(
                self,
                "最近使用的工作区",
                "选择要打开的工作区：",
                labels,
                0,
                False,
            )
            if ok and selected:
                self._switch_workspace(recent[labels.index(selected)])

        def _switch_workspace(self, workspace: str) -> None:
            try:
                resolved = switch_workspace(workspace, running=self._running)
            except WorkspaceDataError as exc:
                QMessageBox.warning(self, "工作区", str(exc))
                return
            config.WORKSPACE_DIR = str(resolved)
            self.active = None
            self.presenter.reset(str(resolved))
            try:
                self.recent_store.remember(resolved)
            except OSError as exc:
                self.presenter.consume(
                    {"event": "gui_error", "message": f"无法保存最近使用的工作区：{exc}"}
                )
            self.file_filter.clear()
            self._refresh_project_files()
            self._render_all()

        def _refresh_project_files(self) -> None:
            try:
                self._all_project_entries = project_entries(config.WORKSPACE_DIR)
            except WorkspaceDataError:
                self._all_project_entries = []
            self._sync_project_watcher()
            self._filter_project_files(self.file_filter.text() if hasattr(self, "file_filter") else "")

        @Slot(str)
        def _schedule_project_refresh(self, _changed_directory: str) -> None:
            """Coalesce bursts of file-system notifications into one tree refresh."""

            self._project_refresh_timer.start()

        def _sync_project_watcher(self) -> None:
            """Watch the workspace and every currently visible directory."""

            if not hasattr(self, "_project_watcher"):
                return
            try:
                root = Path(config.WORKSPACE_DIR).resolve()
            except OSError:
                return
            desired = {str(root)} if root.is_dir() else set()
            for entry in self._all_project_entries:
                if not entry.is_directory:
                    continue
                path = (root / entry.path).resolve(strict=False)
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                if path.is_dir():
                    desired.add(str(path))

            current = set(self._project_watcher.directories())
            stale = sorted(current - desired)
            missing = sorted(desired - current)
            if stale:
                self._project_watcher.removePaths(stale)
            if missing:
                self._project_watcher.addPaths(missing)

        def _expanded_project_paths(self) -> set[str]:
            if not hasattr(self, "project_tree"):
                return set()
            expanded: set[str] = set()
            root = self.project_tree.invisibleRootItem()
            pending = [root.child(index) for index in range(root.childCount())]
            while pending:
                item = pending.pop()
                path = item.data(0, PROJECT_NODE_PATH_ROLE)
                if item.isExpanded() and isinstance(path, str):
                    expanded.add(path)
                pending.extend(item.child(index) for index in range(item.childCount()))
            return expanded

        @Slot(str)
        def _filter_project_files(self, query: str) -> None:
            if not hasattr(self, "project_tree"):
                return
            needle = query.casefold().strip()
            entries = [
                entry for entry in self._all_project_entries
                if not needle or needle in entry.path.casefold()
            ]
            expanded = self._expanded_project_paths()
            self.project_tree.clear()
            nodes: dict[str, QTreeWidgetItem] = {}
            for entry in entries:
                parent = self.project_tree.invisibleRootItem()
                parts = entry.path.split("/")
                key_parts: list[str] = []
                for index, part in enumerate(parts):
                    key_parts.append(part)
                    key = "/".join(key_parts)
                    item = nodes.get(key)
                    if item is None:
                        item = QTreeWidgetItem([part])
                        parent.addChild(item)
                        item.setData(0, PROJECT_NODE_PATH_ROLE, key)
                        if key in expanded:
                            item.setExpanded(True)
                        nodes[key] = item
                    if index == len(parts) - 1 and not entry.is_directory:
                        item.setData(0, Qt.ItemDataRole.UserRole, entry.path)
                    parent = item
            if needle:
                self.project_tree.expandAll()

        @Slot(QTreeWidgetItem, int)
        def _open_project_item(self, item: QTreeWidgetItem, _column: int) -> None:
            relative = item.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(relative, str):
                item.setExpanded(not item.isExpanded())
                return
            try:
                text, truncated = read_workspace_text(config.WORKSPACE_DIR, relative)
            except WorkspaceDataError as exc:
                QMessageBox.warning(self, "文件预览", str(exc))
                return
            if truncated:
                text += "\n\n[预览内容已截断]"
            self._show_text_dialog(relative, text)

        @Slot(QListWidgetItem)
        def _open_change_item(self, item: QListWidgetItem) -> None:
            relative = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(relative, str) or self.active is None:
                QMessageBox.information(
                    self,
                    "差异",
                    "当前 Agent 会话创建文件检查点后，才能查看可信差异。",
                )
                return
            try:
                diff = self.active.st.checkpoints.file_diff(
                    relative,
                    start_index=self.active.st.turn_checkpoint_index,
                )
            except CheckpointError as exc:
                QMessageBox.information(self, "无法查看差异", str(exc))
                return
            self._show_text_dialog(f"差异 — {relative}", diff.text)

        @Slot()
        def _restore_task_changes(self) -> None:
            if self._running or self.active is None:
                QMessageBox.information(
                    self,
                    "恢复任务改动",
                    "当前没有可恢复的已完成 Agent 任务。",
                )
                return
            answer = QMessageBox.question(
                self,
                "恢复任务改动",
                "要恢复本次任务由 Agent 文件工具直接造成的改动吗？\n\n"
                "Checkpoint 不保证恢复 Shell 命令的副作用。若文件在 Agent 编辑后又被修改，"
                "恢复操作将被拒绝，以保护后续内容。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                logger = RunLog(event_sink=self._consume_event)
                self.runtime._restore_task_changes(self.active, logger=logger)
            except (CheckpointError, OSError) as exc:
                QMessageBox.warning(self, "恢复被拒绝", str(exc))
                return
            self._refresh_project_files()
            self._render_all()

        @Slot()
        def _export_evidence_report(self) -> None:
            report = self.presenter.evidence_report
            if not isinstance(report, dict):
                QMessageBox.information(
                    self,
                    "导出证据报告",
                    "请先运行或恢复一个任务，再导出 Runtime 证据。",
                )
                return
            default = str(Path(config.WORKSPACE_DIR) / "agent-evidence-report.md")
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "导出证据报告",
                default,
                "Markdown (*.md)",
            )
            if not selected:
                return
            try:
                Path(selected).write_text(
                    report_markdown(report),
                    encoding="utf-8",
                    newline="\n",
                )
            except OSError as exc:
                QMessageBox.warning(self, "导出失败", str(exc))
                return
            QMessageBox.information(self, "证据报告", f"已保存到：\n{selected}")

        @Slot(QUrl)
        def _open_activity_link(self, url: QUrl) -> None:
            value = url.toString()
            if not value.startswith("output:") or self.active is None:
                return
            output_id = value.split(":", 1)[1]
            try:
                text, total = read_saved_output_range(
                    config.WORKSPACE_DIR,
                    self.active.st.session_id,
                    output_id,
                    0,
                    60_000,
                )
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "命令输出", str(exc))
                return
            if len(text) < total:
                text += f"\n\n[当前显示 {len(text)} / {total} 个字符]"
            self._show_text_dialog(f"命令输出 — {output_id}", text)

        def _show_text_dialog(self, title: str, text: str) -> None:
            dialog = QDialog(self)
            dialog.setWindowTitle(title)
            dialog.resize(900, 650)
            layout = QVBoxLayout(dialog)
            viewer = QPlainTextEdit()
            viewer.setReadOnly(True)
            viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            viewer.setPlainText(_redact(text))
            close = QPushButton("关闭")
            close.clicked.connect(dialog.accept)
            layout.addWidget(viewer, 1)
            layout.addWidget(close)
            dialog.exec()

        @Slot()
        def _show_history(self) -> None:
            if self._running:
                QMessageBox.information(self, "历史记录", "请先停止当前任务，再恢复历史会话。")
                return
            summaries = SessionStore.summaries(config.WORKSPACE_DIR, limit=30)
            if not summaries:
                QMessageBox.information(self, "历史记录", "当前工作区没有可读取的历史会话。")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("会话历史")
            dialog.resize(820, 520)
            layout = QHBoxLayout(dialog)
            sessions = QListWidget()
            details = QPlainTextEdit()
            details.setReadOnly(True)
            layout.addWidget(sessions, 2)
            right = QVBoxLayout()
            right.addWidget(details, 1)
            resume = QPushButton("恢复")
            close = QPushButton("关闭")
            buttons = QHBoxLayout()
            buttons.addWidget(resume)
            buttons.addWidget(close)
            right.addLayout(buttons)
            layout.addLayout(right, 3)
            by_id = {str(item["id"]): item for item in summaries}
            symbols = {"verified": "✓", "completed": "✓", "stopped": "■", "unknown": "•"}
            for summary in summaries:
                session_id = str(summary["id"])
                row = QListWidgetItem(
                    f"{symbols.get(summary['status'], '•')}  {summary['updated']:%m-%d %H:%M}  {summary['task']}"
                )
                row.setData(Qt.ItemDataRole.UserRole, session_id)
                sessions.addItem(row)

            def show_selected() -> None:
                selected = sessions.currentItem()
                if selected is None:
                    details.clear()
                    return
                summary = by_id[str(selected.data(Qt.ItemDataRole.UserRole))]
                outcome = summary.get("outcome")
                lines = [
                    f"任务：{summary['task']}",
                    f"模型：{summary['model'] or '未知'}",
                    f"更新时间：{summary['updated']:%Y-%m-%d %H:%M:%S}",
                    f"状态：{_status_zh(str(summary['status']))}",
                ]
                if isinstance(outcome, dict):
                    lines.extend([
                        "",
                        str(outcome.get("text", "")),
                        "",
                        f"步骤：{outcome.get('steps', 0)}",
                        f"改动文件：{len(outcome.get('changes', [])) if isinstance(outcome.get('changes'), list) else 0}",
                    ])
                details.setPlainText(_redact("\n".join(lines)))

            sessions.currentItemChanged.connect(lambda _current, _previous: show_selected())
            if sessions.count():
                sessions.setCurrentRow(0)
            close.clicked.connect(dialog.reject)

            def resume_selected() -> None:
                selected = sessions.currentItem()
                if selected is None:
                    return
                selector = str(selected.data(Qt.ItemDataRole.UserRole))
                dialog.accept()
                self.presenter.reset(config.WORKSPACE_DIR)
                self.active = None
                self._start_worker(resume_selector=selector)

            resume.clicked.connect(resume_selected)
            dialog.exec()

        def _render_all(self) -> None:
            self.workspace_label.setText(self.presenter.workspace or config.WORKSPACE_DIR)
            self.model_label.setText(config.MODEL_NAME)
            if not self._running:
                self.status_label.setText(self._idle_status())
            self._render_activity()
            self._render_changes()
            self._render_plan()
            self._render_verification()
            if not self._running:
                self.restore_button.setEnabled(self.active is not None)
                self.export_button.setEnabled(self.presenter.evidence_report is not None)

        def _render_activity(self) -> None:
            blocks = [_activity_html(item) for item in self.presenter.activity]
            if self.presenter.outcome.visible:
                blocks.append(_outcome_html(self.presenter.outcome, self.presenter.verification))
            content = "".join(blocks) or '<div class="empty">输入编程任务即可开始。</div>'
            self.activity.setHtml(_activity_document(content))
            bar = self.activity.verticalScrollBar()
            bar.setValue(bar.maximum())

        def _render_changes(self) -> None:
            self.changes_list.clear()
            labels = {"added": "A", "modified": "M", "deleted": "D", "changed": "M"}
            for change in self.presenter.changes:
                counts = ""
                if change.additions is not None and change.deletions is not None:
                    counts = f"   +{change.additions} -{change.deletions}"
                item = QListWidgetItem(
                    f"{labels.get(change.kind, 'M')}  {change.path}{counts}"
                )
                item.setData(Qt.ItemDataRole.UserRole, change.path)
                self.changes_list.addItem(item)

        def _render_plan(self) -> None:
            while self.plan_layout.count():
                item = self.plan_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            total = len(self.presenter.plan)
            completed = sum(item.status == "completed" for item in self.presenter.plan)
            self.plan_title.setText(f"计划  {completed} / {total}" if total else "计划")
            if not self.presenter.plan:
                empty = QLabel("当前没有计划")
                empty.setObjectName("muted")
                self.plan_layout.addWidget(empty)
                return
            symbols = {"completed": "✓", "in_progress": "●", "pending": "○"}
            for item in self.presenter.plan:
                evidence = "".join(f"\n    {fact}" for fact in item.evidence)
                label = QLabel(f"{symbols[item.status]}  {item.step}{evidence}")
                label.setWordWrap(True)
                label.setProperty("planStatus", item.status)
                self.plan_layout.addWidget(label)

        def _render_verification(self) -> None:
            state = self.presenter.verification
            rows = [
                _metric("工作区版本", str(state.workspace_revision)),
                _metric("已验证版本", str(state.verified_revision) if state.verified_revision >= 0 else "—"),
                _metric("检查次数", str(state.check_attempts)),
                _metric("修复进度", _status_zh(state.progress)),
            ]
            if state.verifier:
                rows.append(
                    f'<div style="color:{COLORS["muted"]}; margin-top:10px">验证命令</div>'
                    f'<div style="margin:3px 0 10px 0">{html.escape(state.verifier)}</div>'
                )
            if state.current and state.adequate and state.task_completed:
                rows.append(_metric("工作区指纹", "匹配", "success"))
                rows.append(
                    f'<div style="color:{COLORS["success"]}; font-weight:700; margin-top:12px">'
                    '✓ 最终验证通过</div>'
                )
            elif state.current and not state.adequate:
                rows.append(_metric("工作区指纹", "匹配", "success"))
                rows.append(
                    f'<div style="color:{COLORS["warning"]}; font-weight:700; margin-top:12px">'
                    '部分验证通过</div>'
                    f'<div style="color:{COLORS["muted"]}">尚未完成全量测试验证</div>'
                )
            elif state.current:
                rows.append(_metric("工作区指纹", "匹配", "success"))
                rows.append(
                    f'<div style="color:{COLORS["success"]}; font-weight:700; margin-top:12px">'
                    '最近一次验证仍有效</div>'
                    f'<div style="color:{COLORS["muted"]}">任务尚未完成</div>'
                )
            elif state.required:
                rows.append(
                    f'<div style="color:{COLORS["warning"]}; font-weight:700; margin-top:12px">验证已失效</div>'
                    f'<div style="color:{COLORS["muted"]}">需要重新验证</div>'
                )
            else:
                rows.append(
                    f'<div style="color:{COLORS["muted"]}; margin-top:12px">'
                    '当前尚不需要验证</div>'
                )
            if not state.tracking_complete:
                rows.append(
                    f'<div style="color:{COLORS["warning"]}; margin-top:8px">'
                    '工作区跟踪处于受限模式</div>'
                )
            metrics = "".join(row for row in rows if row.startswith("<tr"))
            details = "".join(row for row in rows if not row.startswith("<tr"))
            self.verification.setText(
                '<table width="100%" cellspacing="0" cellpadding="3">'
                + metrics
                + "</table>"
                + details
            )

        def closeEvent(self, event: QCloseEvent) -> None:
            if self._running:
                QMessageBox.information(
                    self,
                    "Agent 正在运行",
                    "请等待当前任务或确认请求结束后再关闭窗口。",
                )
                event.ignore()
                return
            event.accept()


def launch(runtime_module: Any = None, *, prefer_recent: bool = False) -> int:
    """Open the desktop GUI without changing the CLI/runtime dependency surface."""

    if QObject is None:
        print(
            "桌面 GUI 需要 PySide6。"
            '请执行：python -m pip install -e ".[gui]"',
            file=sys.stderr,
        )
        return 2
    if runtime_module is None:
        from . import agent as runtime_module

    if prefer_recent:
        recent = RecentWorkspaceStore().load()
        if recent:
            config.WORKSPACE_DIR = recent[0]

    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        config.get_api_key()
        config.get_model()
    except RuntimeError as exc:
        QMessageBox.critical(None, "配置错误", str(exc))
        return 2
    workspace = Path(config.WORKSPACE_DIR)
    if not workspace.is_dir():
        QMessageBox.critical(None, "工作区错误", f"工作区不存在：\n{workspace}")
        return 2

    ui.set_output_enabled(False)
    window = AgentWindow(runtime_module)
    window.show()
    try:
        return app.exec()
    finally:
        ui.set_output_enabled(True)


def _activity_html(item: ActivityItem) -> str:
    title = html.escape(item.title)
    details = "".join(f'<div class="detail">{html.escape(line)}</div>' for line in item.detail)
    if item.kind == "task":
        return f'<div class="task"><span class="task-mark">❯</span> {title}</div>'
    if item.kind == "file" and item.change is not None:
        labels = {"added": "新增", "modified": "修改", "deleted": "删除", "changed": "变更"}
        stats = _stats(item.change.additions, item.change.deletions)
        return (
            '<div class="file-change">'
            f'<div><span class="file-label">{labels.get(item.change.kind, "变更")}</span> '
            f'<span class="path">{title}</span></div>{stats}</div>'
        )
    if item.kind == "completion":
        symbol = "✓" if item.tone == "success" else "⚠"
        return f'<div class="completion {item.tone}">{symbol} {title}{details}</div>'
    symbols = {"success": "✓", "failure": "✗", "warning": "⚠", "running": "•", "neutral": "•", "accent": "•"}
    symbol = symbols.get(item.tone, "•")
    output = (
        f'<div class="detail"><a href="output:{html.escape(item.output_ref)}">查看完整输出</a></div>'
        if item.output_ref
        else ""
    )
    return f'<div class="activity-row {item.tone}"><span class="symbol">{symbol}</span> {title}{details}{output}</div>'


def _outcome_html(outcome: OutcomeView, verification) -> str:
    labels = {
        "final_verified": ("✓ 任务完成", "✓ 最终验证通过", "success"),
        "partial": ("⚠ 任务完成", "部分验证通过", "warning"),
        "stale": ("⚠ 任务完成", "验证已失效", "warning"),
        "stopped": ("■ 任务已停止", "未通过最终验证", "warning"),
        "restored": ("↶ 已恢复任务改动", "验证已失效", "warning"),
        "completed": ("✓ 任务完成", "无需验证", "success"),
    }
    title, status, tone = labels.get(
        outcome.status,
        ("任务结果", "未知", "warning"),
    )
    additions = "—" if outcome.additions is None else f"+{outcome.additions}"
    deletions = "—" if outcome.deletions is None else f"-{outcome.deletions}"
    elapsed = f"{outcome.elapsed_seconds:.1f}s" if outcome.elapsed_seconds is not None else "—"
    rows = [
        _metric("改动文件", f"{outcome.changed_files} 个"),
        _metric("新增行", additions),
        _metric("删除行", deletions),
        _metric("执行步骤", str(outcome.steps)),
        _metric("模型调用", str(outcome.model_calls)),
        _metric("工具调用", str(outcome.tool_calls)),
        _metric("验证次数", str(outcome.checks)),
        _metric("总 Token", _token_count(outcome.tokens)),
        _metric("输入 Token", _token_count(outcome.prompt_tokens)),
        _metric("输出 Token", _token_count(outcome.completion_tokens)),
        *(
            [_metric("缓存命中", _token_count(outcome.cache_hit_tokens))]
            if outcome.cache_hit_tokens is not None
            else []
        ),
        *(
            [_metric("缓存未命中", _token_count(outcome.cache_miss_tokens))]
            if outcome.cache_miss_tokens is not None
            else []
        ),
        *(
            [_metric("推理 Token", _token_count(outcome.reasoning_tokens))]
            if outcome.reasoning_tokens is not None
            else []
        ),
        _metric("用时", elapsed),
        _metric("工作区版本", str(verification.workspace_revision)),
        _metric(
            "已验证版本",
            str(verification.verified_revision) if verification.verified_revision >= 0 else "—",
        ),
        _metric("工作区指纹", "匹配" if verification.fingerprint_matched else "—"),
        _metric("验证范围", _status_zh(verification.verified_scope or "none")),
        _metric("修复进度", _status_zh(outcome.repair_progress)),
    ]
    return (
        f'<div class="outcome {tone}"><div class="outcome-title">{html.escape(title)}</div>'
        '<table width="100%" cellspacing="0" cellpadding="3">'
        + "".join(rows)
        + "</table>"
        + f'<div class="outcome-status">{html.escape(status)}</div></div>'
    )


def _stats(additions: int | None, deletions: int | None) -> str:
    if additions is None or deletions is None:
        return '<div class="stats muted">无法统计行数</div>'
    parts = []
    if additions:
        parts.append(f'<span class="plus">+{additions}</span>')
    if deletions:
        parts.append(f'<span class="minus">-{deletions}</span>')
    return f'<div class="stats">{"&nbsp;&nbsp;".join(parts) or "行数未变化"}</div>'


def _token_count(value: int) -> str:
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _status_zh(value: str) -> str:
    """Translate stable Runtime status codes without touching technical output."""

    normalized = value.strip().casefold()
    return {
        "verified": "已验证",
        "completed": "已完成",
        "stopped": "已停止",
        "unknown": "未知",
        "passed": "通过",
        "failed": "失败",
        "warning": "需要关注",
        "no_progress": "无进展",
        "not_checked": "未检查",
        "full": "全量",
        "targeted": "局部",
        "unknown_scope": "范围未知",
        "none": "无",
    }.get(normalized, value)


def _activity_document(body: str) -> str:
    return f"""
    <html><head><style>
      body {{ background: {COLORS['background']}; color: {COLORS['text']};
              font-family: Consolas, 'Cascadia Mono', monospace; font-size: 14px; line-height: 1.5; }}
      .empty {{ color: {COLORS['muted']}; margin: 24px 8px; }}
      .task {{ font-size: 16px; font-weight: 600; margin: 12px 4px 24px 4px; }}
      .task-mark, .path {{ color: {COLORS['accent']}; }}
      .activity-row {{ margin: 8px 4px; color: {COLORS['text']}; }}
      .symbol {{ color: {COLORS['muted']}; }}
      .success, .success .symbol {{ color: {COLORS['success']}; }}
      .failure, .failure .symbol {{ color: {COLORS['failure']}; }}
      .warning, .warning .symbol {{ color: {COLORS['warning']}; }}
      .detail {{ color: {COLORS['muted']}; margin: 3px 0 0 24px; }}
      .file-change {{ margin: 16px 4px 16px 24px; }}
      .file-label {{ font-weight: 600; }}
      .stats {{ margin: 4px 0 0 16px; color: {COLORS['muted']}; }}
      .plus {{ color: {COLORS['success']}; }} .minus {{ color: {COLORS['failure']}; }}
      .completion {{ border-top: 1px solid {COLORS['border']}; margin: 22px 4px 8px 4px;
                     padding-top: 18px; font-weight: 600; }}
      .outcome {{ border: 1px solid {COLORS['border']}; margin: 22px 4px 12px 4px;
                  padding: 16px; background: {COLORS['panel']}; }}
      .outcome-title {{ font-size: 16px; font-weight: 700; margin-bottom: 10px; }}
      .outcome-status {{ font-weight: 700; margin-top: 12px; }}
      .outcome.success .outcome-status {{ color: {COLORS['success']}; }}
      .outcome.warning .outcome-status {{ color: {COLORS['warning']}; }}
      a {{ color: {COLORS['accent']}; text-decoration: none; }}
      .summary {{ margin: 18px 4px; }} .summary-title {{ font-weight: 600; margin-bottom: 8px; }}
      .change-row {{ margin: 5px 0; }} .change-kind {{ color: {COLORS['muted']}; }}
      .muted {{ color: {COLORS['muted']}; }}
    </style></head><body>{body}</body></html>
    """


def _metric(label: str, value: str, style: str = "") -> str:
    value_color = COLORS["success"] if style == "success" else COLORS["text"]
    return (
        '<tr>'
        f'<td style="color:{COLORS["muted"]}">{html.escape(label)}</td>'
        f'<td align="right" style="color:{value_color}; font-weight:600">{html.escape(value)}</td>'
        '</tr>'
    )


def _approval_parts(prompt: str) -> tuple[str, dict[str, str]]:
    options: dict[str, str] = {}
    body: list[str] = []
    for line in prompt.splitlines():
        match = re.match(r"\s*\[([123])\]\s*(.+)", line)
        if match:
            options[match.group(1)] = _approval_label_zh(match.group(2).strip())
        elif not line.strip().lower().startswith("choose ["):
            body.append(line)
    translated = "\n".join(body).strip()
    replacements = {
        "Command requires approval:": "命令需要确认：",
        "Permission required for": "修改文件需要确认：",
        "unrecognized command requires approval": "该命令尚未识别，需要确认",
        "manual mode requires command approval": "手动权限模式要求确认命令",
        "manual mode requires edit approval": "手动权限模式要求确认文件修改",
    }
    for source, target in replacements.items():
        translated = translated.replace(source, target)
    return translated, options


def _approval_label_zh(label: str) -> str:
    replacements = (
        ("Allow this command family for the session:", "本次会话允许此命令系列："),
        ("Deny this command family for the session:", "本次会话拒绝此命令系列："),
        ("Allow edits to this file for the session", "本次会话允许修改此文件"),
        ("Allow clean-file edits for the session", "本次会话允许修改未预先变更的文件"),
        ("Allow this edit once", "仅允许本次修改"),
        ("Allow once", "仅允许一次"),
        ("Deny once", "本次拒绝"),
        ("Deny", "拒绝"),
    )
    for source, target in replacements:
        if label.startswith(source):
            return target + label[len(source):]
    return label


def _redact(value: str) -> str:
    secret = os.environ.get("AGENT_API_KEY", "")
    return value.replace(secret, "[REDACTED]") if secret else value


def _stylesheet() -> str:
    return f"""
    QMainWindow, QWidget {{ background: {COLORS['background']}; color: {COLORS['text']}; }}
    QLabel {{ background: transparent; }}
    #appTitle {{ font-size: 18px; font-weight: 650; }}
    #workspace, #muted {{ color: {COLORS['muted']}; }}
    #status {{ color: {COLORS['accent']}; font-weight: 650; }}
    #headerButton {{ background: {COLORS['panel']}; border: 1px solid {COLORS['border']};
                     border-radius: 5px; padding: 6px 10px; }}
    #headerButton:hover {{ border-color: {COLORS['accent']}; }}
    #projectPanel {{ background: {COLORS['panel']}; border-right: 1px solid {COLORS['border']}; }}
    #projectTree, #changesList {{ background: {COLORS['panel']}; border: none;
                                  color: {COLORS['text']}; }}
    #projectTree::item:selected, #changesList::item:selected {{ background: {COLORS['panel_alt']}; }}
    QLineEdit {{ background: {COLORS['panel_alt']}; border: 1px solid {COLORS['border']};
                 border-radius: 5px; padding: 7px; }}
    #activity {{ background: {COLORS['background']}; padding: 8px; }}
    #sidebar {{ background: {COLORS['panel']}; border-left: 1px solid {COLORS['border']}; }}
    #planHost {{ background: transparent; }}
    #sectionTitle {{ font-size: 15px; font-weight: 650; }}
    QLabel[planStatus="completed"] {{ color: {COLORS['success']}; }}
    QLabel[planStatus="in_progress"] {{ color: {COLORS['accent']}; font-weight: 600; }}
    QLabel[planStatus="pending"] {{ color: {COLORS['muted']}; }}
    #separator {{ color: {COLORS['border']}; background: {COLORS['border']}; max-height: 1px; }}
    #verification {{ font-family: Consolas, 'Cascadia Mono', monospace; }}
    #taskInput {{ background: {COLORS['panel']}; border: 1px solid {COLORS['border']};
                  border-radius: 7px; padding: 10px; selection-background-color: {COLORS['accent']}; }}
    #taskInput:focus {{ border-color: {COLORS['accent']}; }}
    #runButton {{ background: {COLORS['accent']}; color: #0d1117; border: none;
                  border-radius: 7px; font-weight: 700; padding: 10px 18px; }}
    #runButton:disabled {{ background: #384252; color: {COLORS['muted']}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; }}
    QScrollBar::handle:vertical {{ background: #394252; min-height: 28px; border-radius: 4px; }}
    """


def _standalone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="zxn Coding Agent desktop GUI")
    parser.add_argument("--workspace", help="Workspace directory for Agent tasks.")
    parser.add_argument("--permission-mode", choices=("balanced", "manual"))
    return parser


def main() -> int:
    args = _standalone_parser().parse_args()
    if args.workspace:
        config.WORKSPACE_DIR = str(Path(args.workspace).expanduser().resolve())
    if args.permission_mode:
        config.PERMISSION_MODE = args.permission_mode
    return launch(
        prefer_recent=args.workspace is None and "AGENT_WORKSPACE" not in os.environ
    )


if __name__ == "__main__":
    raise SystemExit(main())
