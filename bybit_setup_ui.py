from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core_client import ensure_running
from credential_guard import validate_candidate_credentials
from trading_config import has_bybit_credentials, save_bybit_credentials, save_trading_settings

BG = "#212121"
PANEL = "#2b2b2b"
PANEL_2 = "#252525"
TEXT = "#f2f2f2"
MUTED = "#a6a6a6"
BORDER = "#444444"
GREEN = "#19c37d"
RED = "#ff6b6b"
AMBER = "#f2b84b"


class _VerifyWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, key: str, secret: str) -> None:
        super().__init__()
        self.key = key
        self.secret = secret

    def run(self) -> None:
        try:
            self.finished.emit(validate_candidate_credentials(self.key, self.secret))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class BybitSetupDialog(QDialog):
    """Simple, safe one-time Bybit connection wizard.

    Candidate credentials are verified in memory first. They are persisted only
    after successful validation, so a typo cannot overwrite an existing working key.
    """

    connectionSaved = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bybit AI Manager — Connect Bybit")
        self.setModal(True)
        self.resize(670, 620)
        self.setMinimumWidth(620)
        self._thread: QThread | None = None
        self._worker: _VerifyWorker | None = None
        self._result: dict[str, Any] | None = None
        self._key = ""
        self._secret = ""

        self.setStyleSheet(f"""
            QDialog {{ background:{BG}; color:{TEXT}; }}
            QLabel {{ color:{TEXT}; font-family:'Segoe UI'; }}
            QLineEdit {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; border-radius:10px; padding:11px 12px; font-size:14px; }}
            QLineEdit:focus {{ border:1px solid #6ea8fe; }}
            QPushButton {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; border-radius:10px; padding:10px 14px; font-size:13px; }}
            QPushButton:hover {{ background:#383838; }}
            QPushButton#Primary {{ background:{GREEN}; color:white; border:none; font-weight:700; font-size:14px; }}
            QPushButton#Primary:hover {{ background:#21b989; }}
            QPushButton#Primary:disabled {{ background:#355c51; color:#9da9a5; }}
            QCheckBox {{ color:{MUTED}; spacing:8px; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        title = QLabel("Connect Bybit to Bybit AI Manager")
        title.setStyleSheet("font-size:26px;font-weight:750;")
        root.addWidget(title)

        subtitle = QLabel(
            "Paste the API Key and API Secret you created in Bybit. "
            "The app detects Mainnet/Testnet, validates permissions, and stores the key locally in Windows using encrypted storage."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{MUTED};font-size:13px;")
        root.addWidget(subtitle)

        permissions = QFrame()
        permissions.setStyleSheet(f"QFrame{{background:{PANEL_2};border:1px solid {BORDER};border-radius:12px;}}")
        pl = QVBoxLayout(permissions)
        pl.setContentsMargins(16, 13, 16, 13)
        ptitle = QLabel("Recommended key permissions for Live Learning")
        ptitle.setStyleSheet("font-weight:700;font-size:14px;")
        pl.addWidget(ptitle)
        ptext = QLabel(
            "✓ Read and write\n"
            "✓ Contract → Orders\n"
            "✓ Contract → Positions\n"
            "✗ Withdrawals, transfers, Wallet permissions — not required"
        )
        ptext.setStyleSheet(f"color:{MUTED};line-height:1.35;")
        pl.addWidget(ptext)
        root.addWidget(permissions)

        key_label = QLabel("API Key")
        key_label.setStyleSheet("font-weight:650;")
        root.addWidget(key_label)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste Bybit API Key")
        root.addWidget(self.key_edit)

        secret_label = QLabel("API Secret")
        secret_label.setStyleSheet("font-weight:650;")
        root.addWidget(secret_label)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Paste Bybit API Secret")
        root.addWidget(self.secret_edit)

        self.show_secret = QCheckBox("Show entered values")
        self.show_secret.toggled.connect(self._toggle_secret)
        root.addWidget(self.show_secret)

        self.status = QFrame()
        self.status.setVisible(False)
        self.status_layout = QVBoxLayout(self.status)
        self.status_layout.setContentsMargins(14, 12, 14, 12)
        self.status_title = QLabel("")
        self.status_title.setStyleSheet("font-size:15px;font-weight:700;")
        self.status_body = QLabel("")
        self.status_body.setWordWrap(True)
        self.status_body.setTextFormat(Qt.TextFormat.RichText)
        self.status_layout.addWidget(self.status_title)
        self.status_layout.addWidget(self.status_body)
        root.addWidget(self.status)

        if has_bybit_credentials():
            existing = QLabel("A Bybit key is already stored. A new key replaces it only after successful validation.")
            existing.setWordWrap(True)
            existing.setStyleSheet(f"color:{AMBER};font-size:12px;")
            root.addWidget(existing)

        root.addStretch(1)
        actions = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        actions.addStretch(1)
        self.verify_btn = QPushButton("Verify and connect")
        self.verify_btn.setObjectName("Primary")
        self.verify_btn.setMinimumHeight(46)
        self.verify_btn.clicked.connect(self._verify)
        actions.addWidget(self.verify_btn)
        root.addLayout(actions)

    def _toggle_secret(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self.key_edit.setEchoMode(mode)
        self.secret_edit.setEchoMode(mode)

    def _set_status(self, title: str, body: str, kind: str) -> None:
        color = GREEN if kind == "ok" else RED if kind == "error" else AMBER
        self.status.setStyleSheet(f"QFrame{{background:{PANEL_2};border:1px solid {color};border-radius:10px;}}")
        self.status_title.setText(title)
        self.status_title.setStyleSheet(f"font-size:15px;font-weight:700;color:{color};")
        self.status_body.setText(body)
        self.status_body.setStyleSheet(f"color:{TEXT};")
        self.status.setVisible(True)

    def _verify(self) -> None:
        key = self.key_edit.text().strip()
        secret = self.secret_edit.text().strip()
        if not key or not secret:
            self._set_status("Both values are required", "Paste both the <b>API Key</b> and <b>API Secret</b> from Bybit.", "error")
            return
        if "\n" in key + secret or "\r" in key + secret:
            self._set_status("Invalid format", "Key and Secret must each be a single line without line breaks.", "error")
            return

        self._key = key
        self._secret = secret
        self.verify_btn.setEnabled(False)
        self.verify_btn.setText("Verifying Bybit…")
        self._set_status("Verifying connection…", "The app is checking the environment and permissions. The key is <b>not stored yet</b>.", "warn")

        self._thread = QThread(self)
        self._worker = _VerifyWorker(key, secret)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._verified)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_worker)
        self._thread.start()

    def _cleanup_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def _failed(self, message: str) -> None:
        self.verify_btn.setEnabled(True)
        self.verify_btn.setText("Verify and connect")
        self._set_status(
            "Bybit rejected the key",
            "Check that Key/Secret were copied completely and that the key is active. "
            "<br><br><span style='color:#a6a6a6'>Technical reason: " + message.replace("<", "&lt;") + "</span>",
            "error",
        )

    def _verified(self, result_obj: object) -> None:
        result = dict(result_obj) if isinstance(result_obj, dict) else {}
        self._result = result
        env = "TESTNET" if result.get("testnet") else "MAINNET"
        read_only = bool(result.get("read_only"))
        can_trade = bool(result.get("can_trade_contracts"))
        unsafe_wallet = bool(result.get("unsafe_wallet_permissions"))

        if unsafe_wallet:
            self.verify_btn.setEnabled(True)
            self.verify_btn.setText("Verify again")
            self._set_status(
                "The key works, but its permissions are too broad",
                f"<b>{env}</b> connected, but the key has Wallet/transfer permissions. "
                "For Live mode this key is intentionally rejected. Create a dedicated key with only <b>ContractTrade → Order + Position</b>.",
                "error",
            )
            return

        if not can_trade and not read_only and not result.get("testnet"):
            self.verify_btn.setEnabled(True)
            self.verify_btn.setText("Verify again")
            self._set_status(
                "Connected, but required permissions are missing",
                f"<b>{env}</b> accepted the key, but autonomous futures require <b>Order + Position</b> permissions in ContractTrade.",
                "error",
            )
            return

        try:
            save_bybit_credentials(self._key, self._secret)
            save_trading_settings({"bybit_key_environment": str(result.get("key_environment", "auto"))})
            ensure_running()
            bootstrap_text = "The key is ready. Professional Bootstrap will start together with START."
        except Exception as exc:
            self.verify_btn.setEnabled(True)
            self.verify_btn.setText("Verify and connect")
            self._set_status("Could not save the key", f"{type(exc).__name__}: {exc}", "error")
            return

        mode = str(result.get("autopilot_mode", "observer"))
        checks = [
            f"✓ Environment: <b>{env}</b>",
            f"✓ Key mode: <b>{'Read-only' if read_only else 'Read/Write'}</b>",
        ]
        if can_trade:
            checks.append("✓ Futures permissions: <b>Order + Position</b>")
        if not unsafe_wallet:
            checks.append("✓ No unsafe Wallet permissions")
        if result.get("equity_usdt") is not None:
            checks.append(f"✓ Equity detected: <b>{float(result.get('equity_usdt') or 0):.4f} USDT</b>")
        if result.get("clock_offset_ms") is not None:
            raw = float(result.get("clock_drift_ms") or 0.0) / 1000.0
            corrected = float(result.get("clock_corrected_drift_ms") or 0.0) / 1000.0
            checks.append(f"✓ Bybit time sync: <b>corrected {corrected:.2f}s</b> (Windows drift {raw:.2f}s compensated)")
        elif result.get("clock_drift_ms") is not None:
            checks.append(f"✓ Clock drift: <b>{float(result.get('clock_drift_ms') or 0)/1000.0:.2f}s</b>")
        checks.append(f"✓ App mode: <b>{mode}</b>")
        checks.append("✓ " + bootstrap_text)

        self._set_status("Bybit connected successfully", "<br>".join(checks), "ok")
        self.verify_btn.setEnabled(False)
        self.verify_btn.setText("Connected ✓")
        self.connectionSaved.emit(result)
        QMessageBox.information(
            self,
            "Bybit connected",
            "Done. The key was validated and stored locally.\n\n"
            "You can now use START. If this window was opened from START, the launch continues automatically after this message closes.",
        )
        self.accept()
