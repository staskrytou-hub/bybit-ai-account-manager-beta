from __future__ import annotations

import html
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import markdown as markdown_lib
from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from artifacts import artifact_watermark, list_artifacts, sync_workspace_artifacts
from autonomy_store import list_runs, list_steps
from chat_store import (
    add_message,
    build_compact_context,
    create_chat,
    create_project,
    delete_chat,
    ensure_default_structure,
    get_chat,
    get_project,
    list_chats,
    list_messages,
    list_projects,
    maybe_title_chat,
    rename_chat,
    set_active,
)
from config import clear_api_key, has_api_key, load_local_env, save_api_key
from journal import recent_events
from learning import capture_user_correction
from memory import delete_memory, list_memories
from model_router import choose_model
from paths import ASSETS_DIR, WORKSPACE_DIR
from settings import load_settings, save_settings
from tasks import list_tasks, update_task
from usage import usage_today, usage_totals
from verification import verify_artifacts
from text_safety import sanitize_text

load_local_env()

APP_VERSION = "4.0.0"

BG = "#212121"
SIDEBAR = "#171717"
PANEL = "#2f2f2f"
PANEL_HOVER = "#383838"
TEXT = "#f2f2f2"
MUTED = "#a6a6a6"
BORDER = "#424242"
ACCENT = "#ffffff"
ACCENT_TEXT = "#171717"
GREEN = "#19c37d"
RED = "#ff6b6b"
BLUE = "#6ea8fe"

APP_QSS = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: 'Segoe UI';
    font-size: 14px;
}}
QMainWindow {{ background: {BG}; }}
QFrame#Sidebar {{ background: {SIDEBAR}; border: none; }}
QPushButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 9px 12px;
    text-align: left;
}}
QPushButton:hover {{ background: {PANEL_HOVER}; }}
QPushButton#Primary {{
    background: {ACCENT};
    color: {ACCENT_TEXT};
    font-weight: 600;
    text-align: center;
}}
QPushButton#Primary:hover {{ background: #e7e7e7; }}
QPushButton#IconButton {{
    border: 1px solid {BORDER};
    background: {PANEL};
    text-align: center;
    padding: 7px 10px;
}}
QPushButton#IconButton:hover {{ background: {PANEL_HOVER}; }}
QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 8px;
    padding: 9px 10px;
    margin: 1px 0px;
}}
QListWidget::item:selected {{ background: {PANEL}; color: {TEXT}; }}
QListWidget::item:hover {{ background: {PANEL_HOVER}; }}
QComboBox, QLineEdit, QPlainTextEdit {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 8px 10px;
    selection-background-color: #0d6efd;
    selection-color: white;
}}
QComboBox::drop-down {{ border: none; width: 25px; }}
QTextBrowser {{
    background: {BG};
    border: none;
    selection-background-color: #0d6efd;
    selection-color: white;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #555; min-height: 30px; border-radius: 5px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QDialog {{ background: {BG}; }}
"""

TRANSCRIPT_CSS = """
<style>
body { color:#f2f2f2; font-family:'Segoe UI'; font-size:15px; line-height:1.55; margin:0; padding:0 26px 30px 26px; }
.msg { max-width: 900px; margin: 0 auto; padding: 18px 4px; }
.user-wrap { background:#2f2f2f; border-radius:18px; padding:12px 16px; margin-left:18%; }
.assistant-wrap { padding:4px 2px; }
.role { font-size:12px; color:#9d9d9d; font-weight:600; margin-bottom:7px; letter-spacing:.2px; }
p { margin: 7px 0; }
ul,ol { margin-top:6px; margin-bottom:8px; }
pre { background:#111827; color:#f8fafc; border:1px solid #334155; border-radius:10px; padding:14px; white-space:pre-wrap; font-family:Consolas,monospace; font-size:13px; }
code { background:#343434; border-radius:4px; padding:1px 4px; font-family:Consolas,monospace; }
pre code { background:transparent; padding:0; }
a { color:#7ab7ff; text-decoration:none; }
blockquote { border-left:3px solid #666; color:#d0d0d0; margin-left:0; padding-left:12px; }
table { border-collapse:collapse; margin:10px 0; }
th,td { border:1px solid #555; padding:7px 9px; }
.system { color:#a6a6a6; font-size:12px; padding:8px 4px; max-width:900px; margin:0 auto; }
.error { background:#3a2020; border:1px solid #7f3030; color:#ffdada; border-radius:10px; padding:12px 14px; }
.artifacts { border-top:1px solid #444; margin-top:12px; padding-top:10px; }
</style>
"""


def _render_markdown(text: str) -> str:
    safe_text = sanitize_text(text or "", context=False)
    try:
        return markdown_lib.markdown(
            safe_text,
            extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
            output_format="html5",
        )
    except Exception:
        return "<p>" + html.escape(safe_text).replace("\n", "<br>") + "</p>"


def _friendly_error(exc: BaseException) -> tuple[str, str]:
    raw = f"{type(exc).__name__}: {exc}"
    lower = raw.lower()
    if "required reasoning item" in lower or "reasoning item" in lower:
        return (
            "Стара API-сесія містить несумісний reasoning-контекст. Stan v2 не повинен повторно використовувати його; створіть новий чат, якщо ця помилка повториться після оновлення.",
            raw,
        )
    if "429" in lower or "rate limit" in lower:
        return (
            "OpenAI тимчасово обмежив швидкість запитів. Stan уже має retry-механізм, але цей запуск вичерпав повтори. Спробуйте ще раз через кілька секунд або зменште розмір автономної задачі.",
            raw,
        )
    return ("Stan не зміг завершити цей запуск. Технічні деталі нижче.", raw)


class ComposerEdit(QPlainTextEdit):
    submitRequested = Signal()

    def keyPressEvent(self, event):  # type: ignore[override]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            self.submitRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class AgentWorker(QObject):
    finished = Signal(str, dict, dict)
    failed = Signal(object)
    progress = Signal(dict)

    def __init__(self, task: str, mode: str, session_id: str, project_name: str, context: str, stop_event: threading.Event) -> None:
        super().__init__()
        self.task = task
        self.mode = mode
        self.session_id = session_id
        self.project_name = project_name
        self.context = context
        self.stop_event = stop_event

    def _progress(self, payload: dict[str, object]) -> None:
        self.progress.emit(dict(payload))

    def run(self) -> None:
        try:
            if self.mode == "Autonomous":
                from autonomy import run_autonomous
                answer, usage, meta = run_autonomous(
                    self.task,
                    approval_handler=lambda _info: True,
                    progress_handler=self._progress,
                    stop_event=self.stop_event,
                    session_id=self.session_id,
                    project_name=self.project_name,
                    conversation_context=self.context,
                )
            elif self.mode == "Chat":
                from agent import run_task
                start_artifact_id = artifact_watermark()
                selection = choose_model(self.task, autonomous=False, role="worker")
                answer, usage = run_task(
                    self.task,
                    approval_handler=lambda _info: True,
                    session_id=self.session_id,
                    model=selection.model,
                    context_note=(
                        f"Active project: {self.project_name}. Continue this saved chat using the supplied compact local context. "
                        "Execute the user's request exactly. Create real artifacts instead of fake links.\n\n"
                        + self.context
                    ),
                )
                meta = {"mode": "direct", "model": selection.model, "artifacts": verify_artifacts(start_artifact_id)["artifacts"]}
            else:
                from universal import run_universal
                answer, usage, meta = run_universal(
                    self.task,
                    approval_handler=lambda _info: True,
                    progress_handler=self._progress,
                    stop_event=self.stop_event,
                    session_id=self.session_id,
                    project_name=self.project_name,
                    conversation_context=self.context,
                )
            self.finished.emit(str(answer), dict(usage), dict(meta))
        except Exception as exc:
            self.failed.emit(exc)


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bybit AI Manager Settings")
        self.setMinimumWidth(560)
        self.cfg = load_settings()
        layout = QVBoxLayout(self)
        title = QLabel("Settings")
        title.setStyleSheet("font-size:22px;font-weight:700;margin-bottom:8px;")
        layout.addWidget(title)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Configured — leave blank to keep current key" if has_api_key() else "Paste OpenAI API key")
        form.addRow("OpenAI API key", self.key)
        self.profile = QComboBox(); self.profile.addItems(["economy", "balanced", "quality"]); self.profile.setCurrentText(str(self.cfg.get("model_profile", "balanced")))
        form.addRow("Model profile", self.profile)
        self.image_quality = QComboBox(); self.image_quality.addItems(["low", "medium", "high", "auto"]); self.image_quality.setCurrentText(str(self.cfg.get("image_quality", "medium")))
        form.addRow("Image quality", self.image_quality)
        self.steps = QLineEdit(str(self.cfg.get("autonomy_max_steps", 8)))
        form.addRow("Autonomous max steps", self.steps)
        self.task_budget = QLineEdit(str(self.cfg.get("autonomy_token_budget", 60000)))
        form.addRow("Task token budget", self.task_budget)
        self.daily_budget = QLineEdit(str(self.cfg.get("autonomy_daily_token_budget", 300000)))
        form.addRow("Daily token budget", self.daily_budget)
        self.ctx_messages = QLineEdit(str(self.cfg.get("context_history_messages", 10)))
        form.addRow("Context messages", self.ctx_messages)
        self.ctx_chars = QLineEdit(str(self.cfg.get("context_history_chars", 18000)))
        form.addRow("Context char budget", self.ctx_chars)
        layout.addLayout(form)
        note = QLabel("Projects, chats, Memory and Workspace are stored locally. Stan v2 keeps fragile SDK session-history disabled by default to avoid reasoning-chain 400 errors.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{MUTED};padding:8px 0;")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        try:
            key = self.key.text().strip()
            if key:
                save_api_key(key)
            save_settings({
                "model_profile": self.profile.currentText(),
                "image_quality": self.image_quality.currentText(),
                "autonomy_max_steps": int(self.steps.text()),
                "autonomy_token_budget": int(self.task_budget.text()),
                "autonomy_daily_token_budget": int(self.daily_budget.text()),
                "context_history_messages": int(self.ctx_messages.text()),
                "context_history_chars": int(self.ctx_chars.text()),
                "use_sdk_session_history": False,
                "workspace_write_policy": "always_allow",
            })
        except Exception as exc:
            QMessageBox.critical(self, "Settings", str(exc))
            return
        self.accept()


class DataListDialog(QDialog):
    def __init__(self, title: str, rows: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 600)
        layout = QVBoxLayout(self)
        head = QLabel(title); head.setStyleSheet("font-size:20px;font-weight:700;")
        layout.addWidget(head)
        box = QTextBrowser(); box.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard | Qt.LinksAccessibleByMouse)
        box.setPlainText("\n\n".join(rows) if rows else "No data yet.")
        layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject); buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.close)
        layout.addWidget(buttons)


class BybitAIWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bybit AI Account Manager — Beta")
        self.resize(1420, 900)
        self.setMinimumSize(980, 660)
        icon = ASSETS_DIR / "bybit-ai-manager.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self._active_project, self._active_chat = ensure_default_structure()
        self._thread: QThread | None = None
        self._worker: AgentWorker | None = None
        self._stop_event: threading.Event | None = None
        self._last_answer = ""
        self._last_artifacts: list[dict[str, Any]] = []
        self._busy = False
        self._build_ui()
        self._install_shortcuts()
        self._refresh_projects_and_chats()
        self._load_chat()
        self._refresh_usage()
        if not has_api_key():
            self.status.setText("API key required")
            self.status.setStyleSheet(f"color:{RED};")
        else:
            try:
                from core_client import ensure_running
                ensure_running()
            except Exception:
                pass

    def _build_ui(self) -> None:
        root = QWidget(); self.setCentralWidget(root)
        outer = QHBoxLayout(root); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)

        sidebar = QFrame(); sidebar.setObjectName("Sidebar"); sidebar.setFixedWidth(278)
        s = QVBoxLayout(sidebar); s.setContentsMargins(12,14,12,14); s.setSpacing(8)
        brand_row = QHBoxLayout()
        brand = QLabel("BYBIT AI"); brand.setStyleSheet("font-size:20px;font-weight:800;letter-spacing:.5px;")
        version = QLabel("v2"); version.setStyleSheet(f"color:{MUTED};font-size:11px;")
        version.setText("v4.6.9")
        brand_row.addWidget(brand); brand_row.addStretch(); brand_row.addWidget(version)
        s.addLayout(brand_row)
        new_chat = QPushButton("＋  New chat"); new_chat.setObjectName("Primary"); new_chat.clicked.connect(self.new_chat)
        s.addWidget(new_chat)
        project_row = QHBoxLayout()
        self.project_combo = QComboBox(); self.project_combo.currentIndexChanged.connect(self._project_changed)
        project_row.addWidget(self.project_combo, 1)
        add_project = QPushButton("＋"); add_project.setObjectName("IconButton"); add_project.setFixedWidth(38); add_project.clicked.connect(self.new_project)
        project_row.addWidget(add_project)
        s.addLayout(project_row)
        chats_label = QLabel("CHATS"); chats_label.setStyleSheet(f"color:{MUTED};font-size:11px;font-weight:700;padding:8px 8px 0 8px;")
        s.addWidget(chats_label)
        self.chat_list = QListWidget(); self.chat_list.itemClicked.connect(self._chat_clicked); self.chat_list.setContextMenuPolicy(Qt.CustomContextMenu); self.chat_list.customContextMenuRequested.connect(self._chat_menu)
        s.addWidget(self.chat_list, 1)
        for text, handler in [
            ("◆  START / Account OS", self.open_account_os), ("◫  Runs", self.open_runs), ("✓  Tasks", self.open_tasks), ("◉  Memory", self.open_memory),
            ("▣  Workspace", self.open_workspace), ("◇  Artifacts", self.open_artifacts), ("≡  Activity", self.open_activity),
            ("⚙  Settings", self.open_settings),
        ]:
            btn = QPushButton(text); btn.clicked.connect(handler); s.addWidget(btn)
        self.sidebar_usage = QLabel(""); self.sidebar_usage.setStyleSheet(f"color:{MUTED};font-size:11px;padding:8px;"); self.sidebar_usage.setWordWrap(True)
        s.addWidget(self.sidebar_usage)
        outer.addWidget(sidebar)

        main = QWidget(); main_l = QVBoxLayout(main); main_l.setContentsMargins(0,0,0,0); main_l.setSpacing(0)
        header = QFrame(); header.setFixedHeight(70); header.setStyleSheet(f"background:{BG};border-bottom:1px solid {BORDER};")
        h = QHBoxLayout(header); h.setContentsMargins(24,10,18,10)
        title_col = QVBoxLayout(); self.chat_title = QLabel("Chat"); self.chat_title.setStyleSheet("font-size:17px;font-weight:650;")
        self.status = QLabel("Agent ready"); self.status.setStyleSheet(f"color:{GREEN};font-size:11px;")
        title_col.addWidget(self.chat_title); title_col.addWidget(self.status)
        h.addLayout(title_col); h.addStretch()
        self.copy_last_btn = QPushButton("Copy last"); self.copy_last_btn.setObjectName("IconButton"); self.copy_last_btn.clicked.connect(self.copy_last)
        h.addWidget(self.copy_last_btn)
        main_l.addWidget(header)

        self.transcript = QTextBrowser(); self.transcript.setOpenExternalLinks(False); self.transcript.anchorClicked.connect(self._anchor_clicked)
        self.transcript.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard | Qt.LinksAccessibleByMouse)
        self.transcript.setContextMenuPolicy(Qt.DefaultContextMenu)
        main_l.addWidget(self.transcript, 1)

        composer_shell = QWidget(); csh = QHBoxLayout(composer_shell); csh.setContentsMargins(20,12,20,18); csh.setSpacing(10)
        composer_card = QFrame(); composer_card.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:18px;}}")
        cc = QVBoxLayout(composer_card); cc.setContentsMargins(14,10,12,10); cc.setSpacing(8)
        self.composer = ComposerEdit(); self.composer.setPlaceholderText("Message Bybit AI Manager…   Ctrl+Enter to send"); self.composer.setMinimumHeight(72); self.composer.setMaximumHeight(180); self.composer.setFrameShape(QFrame.NoFrame)
        self.composer.setStyleSheet("QPlainTextEdit{border:none;background:transparent;padding:4px;font-size:15px;}")
        self.composer.submitRequested.connect(self.send_message)
        cc.addWidget(self.composer)
        controls = QHBoxLayout(); controls.setSpacing(8)
        self.mode = QComboBox(); self.mode.addItems(["Auto", "Chat", "Autonomous"]); self.mode.setFixedWidth(130)
        controls.addWidget(self.mode)
        controls.addStretch()
        paste = QPushButton("Paste"); paste.setObjectName("IconButton"); paste.clicked.connect(self.paste_text); controls.addWidget(paste)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setObjectName("IconButton"); self.stop_btn.clicked.connect(self.stop_run); self.stop_btn.setEnabled(False); controls.addWidget(self.stop_btn)
        self.send_btn = QPushButton("Send"); self.send_btn.setObjectName("Primary"); self.send_btn.setFixedWidth(90); self.send_btn.clicked.connect(self.send_message); controls.addWidget(self.send_btn)
        cc.addLayout(controls)
        csh.addWidget(composer_card, 1)
        main_l.addWidget(composer_shell)
        outer.addWidget(main, 1)

    def _install_shortcuts(self) -> None:
        copy_all = QAction(self); copy_all.setShortcut(QKeySequence("Ctrl+Shift+C")); copy_all.triggered.connect(self.copy_all_chat); self.addAction(copy_all)
        copy_last = QAction(self); copy_last.setShortcut(QKeySequence("Ctrl+Shift+L")); copy_last.triggered.connect(self.copy_last); self.addAction(copy_last)
        new_chat = QAction(self); new_chat.setShortcut(QKeySequence("Ctrl+N")); new_chat.triggered.connect(self.new_chat); self.addAction(new_chat)
        focus = QAction(self); focus.setShortcut(QKeySequence("Ctrl+L")); focus.triggered.connect(self.composer.setFocus); self.addAction(focus)

    def paste_text(self) -> None:
        cb = QApplication.clipboard(); text = cb.text()
        if text:
            self.composer.insertPlainText(text)
            self.composer.setFocus()

    def copy_last(self) -> None:
        if not self._last_answer:
            QMessageBox.information(self, "Copy last", "There is no assistant response to copy yet.")
            return
        QApplication.clipboard().setText(self._last_answer)
        self.status.setText("Last answer copied")

    def copy_all_chat(self) -> None:
        QApplication.clipboard().setText(self.transcript.toPlainText())
        self.status.setText("Chat copied")

    def _refresh_projects_and_chats(self) -> None:
        current_project_id = int(self._active_project["id"])
        self.project_combo.blockSignals(True); self.project_combo.clear()
        projects = list_projects()
        for row in projects:
            self.project_combo.addItem(str(row["name"]), int(row["id"]))
        idx = self.project_combo.findData(current_project_id)
        if idx >= 0: self.project_combo.setCurrentIndex(idx)
        self.project_combo.blockSignals(False)
        self._refresh_chat_list()

    def _refresh_chat_list(self) -> None:
        self.chat_list.clear(); project_id = int(self._active_project["id"])
        for row in list_chats(project_id):
            item = QListWidgetItem(str(row["title"])); item.setData(Qt.UserRole, int(row["id"]))
            if int(row["id"]) == int(self._active_chat["id"]):
                font = item.font(); font.setBold(True); item.setFont(font)
            self.chat_list.addItem(item)

    def _project_changed(self, index: int) -> None:
        if index < 0: return
        project_id = self.project_combo.itemData(index)
        if project_id is None: return
        project = get_project(int(project_id)); chats = list_chats(int(project_id))
        if project and chats:
            self._active_project = project; self._active_chat = chats[0]; set_active(int(project["id"]), int(chats[0]["id"]))
            self._refresh_chat_list(); self._load_chat()

    def _chat_clicked(self, item: QListWidgetItem) -> None:
        if self._busy: return
        chat = get_chat(int(item.data(Qt.UserRole)))
        if not chat: return
        project = get_project(int(chat["project_id"]))
        if not project: return
        self._active_chat = chat; self._active_project = project; set_active(int(project["id"]), int(chat["id"]))
        self._refresh_projects_and_chats(); self._load_chat()

    def _chat_menu(self, point) -> None:
        item = self.chat_list.itemAt(point)
        if item is None: return
        chat_id = int(item.data(Qt.UserRole))
        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        selected = menu.exec(self.chat_list.mapToGlobal(point))
        if selected == rename_action:
            current = get_chat(chat_id); title, ok = QInputDialog.getText(self, "Rename chat", "Title", text=str(current["title"]) if current else "")
            if ok and title.strip(): rename_chat(chat_id, title.strip()); self._refresh_chat_list(); self._load_chat()
        elif selected == delete_action:
            if QMessageBox.question(self, "Delete chat", "Delete this chat from the local chat list?") == QMessageBox.Yes:
                if delete_chat(chat_id):
                    chats = list_chats(int(self._active_project["id"])); self._active_chat = chats[0]; set_active(int(self._active_project["id"]), int(self._active_chat["id"])); self._refresh_chat_list(); self._load_chat()

    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "New project", "Project name")
        if not ok or not name.strip(): return
        pid = create_project(name.strip()); project = get_project(pid); chats = list_chats(pid)
        if project and chats:
            self._active_project = project; self._active_chat = chats[0]; set_active(pid, int(chats[0]["id"])); self._refresh_projects_and_chats(); self._load_chat()

    def new_chat(self) -> None:
        if self._busy: return
        cid = create_chat(int(self._active_project["id"]), "New chat"); chat = get_chat(cid)
        if chat:
            self._active_chat = chat; set_active(int(self._active_project["id"]), cid); self._refresh_chat_list(); self._load_chat(); self.composer.setFocus()

    def _load_chat(self) -> None:
        self.chat_title.setText(str(self._active_chat.get("title", "Chat")))
        messages = list_messages(int(self._active_chat["id"]), limit=3000)
        parts = [TRANSCRIPT_CSS]
        self._last_answer = ""
        for msg in messages:
            role = str(msg.get("role", "")); content = str(msg.get("content", ""))
            if role == "user":
                parts.append(f'<div class="msg"><div class="user-wrap"><div class="role">You</div>{_render_markdown(content)}</div></div>')
            elif role == "assistant":
                self._last_answer = content
                parts.append(f'<div class="msg"><div class="assistant-wrap"><div class="role">Bybit AI Manager</div>{_render_markdown(content)}</div></div>')
            else:
                parts.append(f'<div class="system">{html.escape(sanitize_text(content, context=False))}</div>')
        if not messages:
            parts.append('<div class="msg"><div class="assistant-wrap"><div class="role">Bybit AI Manager</div><p>Новий чат. Напишіть задачу — історія збережеться локально.</p></div></div>')
        self.transcript.setHtml("".join(parts))
        self.transcript.moveCursor(QTextCursor.End)

    def _append_system(self, text: str) -> None:
        current = self.transcript.toHtml()
        # QTextBrowser rewraps HTML; easiest and safest is to persist system progress only visually using append.
        self.transcript.append(f'<div class="system">{html.escape(sanitize_text(text, context=False))}</div>')
        self.transcript.moveCursor(QTextCursor.End)

    def _append_live_user(self, text: str) -> None:
        self.transcript.append(f'<div class="msg"><div class="user-wrap"><div class="role">You</div>{_render_markdown(text)}</div></div>')
        self.transcript.moveCursor(QTextCursor.End)

    def _append_live_assistant(self, text: str, artifacts: list[dict[str, Any]] | None = None) -> None:
        block = f'<div class="msg"><div class="assistant-wrap"><div class="role">Bybit AI Manager</div>{_render_markdown(text)}'
        if artifacts:
            links = []
            for item in artifacts[:80]:
                if not item.get("exists"): continue
                path = str(item.get("absolute_path", "")); rel = html.escape(str(item.get("relative_path", "")))
                url = QUrl.fromLocalFile(path).toString()
                links.append(f'<div>↗ <a href="{url}">{rel}</a></div>')
            if links:
                block += '<div class="artifacts"><b>Real artifacts</b>' + ''.join(links) + '</div>'
        block += '</div></div>'
        self.transcript.append(block); self.transcript.moveCursor(QTextCursor.End)

    def _append_error(self, summary: str, detail: str) -> None:
        block = f'<div class="msg"><div class="error"><b>{html.escape(summary)}</b><br><br><code>{html.escape(detail)}</code></div></div>'
        self.transcript.append(block); self.transcript.moveCursor(QTextCursor.End)

    def send_message(self) -> None:
        if self._busy: return
        task = self.composer.toPlainText().strip()
        if not task: return
        if not has_api_key():
            QMessageBox.warning(self, "OpenAI API key", "Open Settings and save your OpenAI API key first.")
            self.open_settings(); return
        chat_id = int(self._active_chat["id"]); session_id = str(self._active_chat["session_id"]); project_name = str(self._active_project["name"])
        add_message(chat_id, "user", task, {"mode": self.mode.currentText(), "ui": "v2"}); maybe_title_chat(chat_id, task)
        try:
            if bool(load_settings().get("auto_learn_corrections", True)): capture_user_correction(task)
        except Exception: pass
        cfg = load_settings(); context = build_compact_context(chat_id, recent_limit=int(cfg.get("context_history_messages",10)), max_chars=int(cfg.get("context_history_chars",18000)))
        fresh = get_chat(chat_id)
        if fresh: self._active_chat = fresh; self.chat_title.setText(str(fresh["title"])); self._refresh_chat_list()
        self.composer.clear(); self._append_live_user(task); self._set_busy(True)
        self._stop_event = threading.Event()
        self._thread = QThread(self); self._worker = AgentWorker(task, self.mode.currentText(), session_id, project_name, context, self._stop_event)
        self._worker.moveToThread(self._thread); self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress); self._worker.finished.connect(lambda a,u,m: self._on_result(chat_id,a,u,m)); self._worker.failed.connect(lambda e: self._on_error(chat_id,e))
        self._worker.finished.connect(self._thread.quit); self._worker.failed.connect(self._thread.quit); self._thread.finished.connect(self._cleanup_worker)
        self._thread.start()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy; self.send_btn.setEnabled(not busy); self.mode.setEnabled(not busy); self.stop_btn.setEnabled(busy)
        self.status.setText("Working…" if busy else "Agent ready"); self.status.setStyleSheet(f"color:{BLUE if busy else GREEN};font-size:11px;")

    def stop_run(self) -> None:
        if self._stop_event: self._stop_event.set(); self.status.setText("Stopping after current API/tool call…")

    def _on_progress(self, event: dict) -> None:
        kind = str(event.get("type", ""))
        if kind == "route": self.status.setText(f"Auto → {event.get('mode')}")
        elif kind == "planning": self.status.setText("Planning…")
        elif kind == "step_start": self.status.setText(f"Step {event.get('step_no')}/{event.get('total_steps')}: {event.get('title')}")
        elif kind == "step_retry": self.status.setText(f"Retrying step {event.get('step_no')}…")
        elif kind == "step_passed": self.status.setText(f"Step {event.get('step_no')} verified")

    def _on_result(self, chat_id: int, answer: str, usage: dict, meta: dict) -> None:
        add_message(chat_id, "assistant", answer, {"mode": meta.get("mode","direct"), "run_id": meta.get("run_id"), "total_tokens": int(usage.get("total_tokens",0)), "ui":"v2"})
        self._last_answer = answer; artifacts = meta.get("artifacts", []) if isinstance(meta, dict) else []; self._last_artifacts = artifacts if isinstance(artifacts,list) else []
        if int(self._active_chat["id"]) == chat_id:
            self._append_live_assistant(answer, self._last_artifacts)
        self._refresh_usage(); self._set_busy(False)

    def _on_error(self, chat_id: int, exc: BaseException) -> None:
        summary, detail = _friendly_error(exc); add_message(chat_id, "system", f"{summary}\n{detail}", {"error":True,"ui":"v2"})
        if int(self._active_chat["id"]) == chat_id: self._append_error(summary, detail)
        self._set_busy(False)

    def _cleanup_worker(self) -> None:
        if self._worker: self._worker.deleteLater()
        if self._thread: self._thread.deleteLater()
        self._worker = None; self._thread = None; self._stop_event = None

    def _refresh_usage(self) -> None:
        today = usage_today(); total = usage_totals()
        self.sidebar_usage.setText(f"Today: {today['total_tokens']:,} tokens\nTotal: {total['total_tokens']:,} tokens")

    def _anchor_clicked(self, url: QUrl) -> None:
        if url.isLocalFile():
            path = url.toLocalFile()
            if os.path.exists(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        QDesktopServices.openUrl(url)


    def open_account_os(self) -> None:
        from account_os_ui import AccountOSDialog
        AccountOSDialog(self).exec()

    def open_trading(self) -> None:
        # Legacy entrypoint kept for compatibility. v4.5.5 intentionally routes trading into the single Account OS.
        self.open_account_os()

    def open_settings(self) -> None:
        SettingsDialog(self).exec(); self._refresh_usage()

    def open_artifacts(self) -> None:
        try: sync_workspace_artifacts()
        except Exception: pass
        rows=[]
        for a in list_artifacts(limit=500):
            rows.append(f"[{a.get('kind')}] {a.get('relative_path')}\n{a.get('absolute_path')}\n{int(a.get('size_bytes',0) or 0):,} bytes")
        DataListDialog("Artifacts", rows, self).exec()

    def open_workspace(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(WORKSPACE_DIR)))

    def open_memory(self) -> None:
        rows=[f"#{m.get('id')}  [{m.get('category')}]  {m.get('topic')}  importance={m.get('importance')}\n{m.get('content')}" for m in list_memories(limit=500)]
        DataListDialog("Memory", rows, self).exec()

    def open_tasks(self) -> None:
        rows=[f"#{t.get('id')}  [{t.get('status')}]  priority={t.get('priority')}\n{t.get('title')}\n{t.get('details','')}" for t in list_tasks(limit=500)]
        DataListDialog("Tasks", rows, self).exec()

    def open_runs(self) -> None:
        rows=[]
        for r in list_runs(limit=150):
            rows.append(f"Run #{r.get('id')}  [{r.get('status')}]  tokens={r.get('total_tokens')}\nGoal: {r.get('goal')}\nSummary: {r.get('final_summary','')}\nReason: {r.get('stop_reason','')}")
        DataListDialog("Runs", rows, self).exec()

    def open_activity(self) -> None:
        rows=[f"{e.get('time','')}  {e.get('event','')}\n{json.dumps(e.get('payload',{}),ensure_ascii=False,indent=2)}" for e in recent_events(250)]
        DataListDialog("Activity", rows, self).exec()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._stop_event: self._stop_event.set()
        try:
            from trading_engine import TRADING_CONTROLLER
            TRADING_CONTROLLER.stop()
        except Exception:
            pass
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    try:
        from core_client import ensure_running
        ensure_running()
    except Exception:
        pass
    app.setApplicationName("Bybit AI Account Manager")
    app.setApplicationVersion(APP_VERSION)
    app.setStyleSheet(APP_QSS)
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    window = BybitAIWindow(); window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
