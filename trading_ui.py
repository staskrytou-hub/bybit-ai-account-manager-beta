from __future__ import annotations

import json
import threading
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget
)

from research_bootstrap import run_professional_bootstrap
from research_store import get_research_state, latest_bootstrap, strategy_leaderboard
from promotion_ai import refresh_promotions
from promotion_store import latest_promotion_scan
from trading_config import (
    clear_bybit_credentials, has_bybit_credentials, load_trading_settings,
    save_bybit_credentials, save_trading_settings,
)
from trading_engine import TRADING_CONTROLLER, test_bybit_private_connection
from core_client import command as core_command, status as core_status
from credential_guard import validate_autopilot_key
from trading_store import recent_assessments, recent_paper_trades

MUTED="#a6a6a6"; GREEN="#19c37d"; RED="#ff6b6b"; PANEL="#2f2f2f"; AMBER="#f2b84b"


def _fmt_snapshot(s: dict[str, Any] | None) -> str:
    if not s:
        return "No market snapshot yet."
    return (
        f"{s.get('symbol')}  {s.get('interval')}m\n"
        f"Price: {float(s.get('price',0)):,.4f}   Spread: {float(s.get('spread_bps',0)):.2f} bps\n"
        f"Structure: slope20 {s.get('trend_slope_20_pct')}%   range20 {s.get('range_position_20')}   VWAP dist {s.get('vwap_distance_20_pct')}%\n"
        f"Volatility: {s.get('realized_vol_20_pct')}%   Participation z: {s.get('volume_z_20')}   Book imbalance: {s.get('orderbook_imbalance_10')}\n"
        f"OI change: {s.get('open_interest_change_pct')}%   Funding: {s.get('funding_rate')}   OI/price: {s.get('oi_price_regime')}\n"
        f"Local evidence bias: {s.get('local_bias')}   Setup strength: {s.get('setup_strength')}"
    )


class TradingDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bybit AI Manager — Futures Research Core")
        self.resize(1120, 820)
        self.cfg = load_trading_settings()
        self._notice: tuple[str, str, bool] | None = None
        self._analysis_inflight = False
        self._bootstrap_inflight = False
        self._bootstrap_progress = ""
        self._promotion_inflight = False

        root = QVBoxLayout(self)
        title = QLabel("Stan Trading Core v4.5.2 (Advanced)")
        title.setStyleSheet("font-size:24px;font-weight:750;")
        root.addWidget(title)
        subtitle = QLabel(
            "Professional Bybit futures research • liquid-universe scan • multi-timeframe regime • local strategy lab • "
            "current macro/news synthesis • Promotion Intelligence • token-aware AI • deterministic risk engine"
        )
        subtitle.setWordWrap(True); subtitle.setStyleSheet(f"color:{MUTED};")
        root.addWidget(subtitle)

        self.tabs = QTabWidget(); root.addWidget(self.tabs, 1)
        self._build_dashboard(); self._build_research(); self._build_promotions(); self._build_setup(); self._build_history()
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1000)
        self.refresh(); self.refresh_research(); self.refresh_promotions_view()

    def _build_dashboard(self) -> None:
        tab = QWidget(); l = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.state = QLabel("Idle"); self.state.setStyleSheet("font-size:16px;font-weight:700;")
        row.addWidget(self.state); row.addStretch()
        self.start_btn = QPushButton("Start 24/7 loop"); self.start_btn.clicked.connect(self.start_engine); row.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.clicked.connect(self.stop_engine); row.addWidget(self.stop_btn)
        self.analyze_btn = QPushButton("Analyze current market"); self.analyze_btn.clicked.connect(self.analyze_now); row.addWidget(self.analyze_btn)
        l.addLayout(row)
        self.status_note = QLabel(""); self.status_note.setWordWrap(True); self.status_note.setStyleSheet(f"color:{MUTED};")
        l.addWidget(self.status_note)
        self.dashboard = QTextBrowser(); self.dashboard.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        l.addWidget(self.dashboard, 1)
        note = QLabel(
            "Use Account OS / Start Stan Autopilot for the normal one-button workflow. Mainnet live execution is armed only for a dedicated "
            "ContractTrade Order+Position API key after Professional Research and every hard Risk Engine check passes."
        )
        note.setWordWrap(True); note.setStyleSheet(f"color:{MUTED};padding:8px;")
        l.addWidget(note)
        self.tabs.addTab(tab, "Dashboard")

    def _build_research(self) -> None:
        tab = QWidget(); l = QVBoxLayout(tab)
        head = QHBoxLayout()
        title = QLabel("Professional First Setup / Research Lab"); title.setStyleSheet("font-size:18px;font-weight:700;")
        head.addWidget(title); head.addStretch()
        self.bootstrap_btn = QPushButton("Run Professional First Setup")
        self.bootstrap_btn.clicked.connect(self.start_bootstrap)
        head.addWidget(self.bootstrap_btn)
        refresh = QPushButton("Refresh report"); refresh.clicked.connect(self.refresh_research); head.addWidget(refresh)
        l.addLayout(head)
        explain = QLabel(
            "This bootstrap does not place orders. It validates the Bybit key/permissions, scans liquid USDT perpetuals, builds 5m/15m/1h/4h regimes, "
            "runs fee/slippage-aware local backtests with an out-of-sample split, then makes ONE Chief Analyst web/news synthesis call."
        )
        explain.setWordWrap(True); explain.setStyleSheet(f"color:{MUTED};")
        l.addWidget(explain)
        self.research_status = QLabel(""); self.research_status.setWordWrap(True); self.research_status.setStyleSheet(f"color:{AMBER};")
        l.addWidget(self.research_status)
        self.research = QTextBrowser(); self.research.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        l.addWidget(self.research, 1)
        self.tabs.addTab(tab, "Research Lab")

    def _build_promotions(self) -> None:
        tab = QWidget(); l = QVBoxLayout(tab)
        head = QHBoxLayout()
        title = QLabel("Bybit Promotion Intelligence"); title.setStyleSheet("font-size:18px;font-weight:700;")
        head.addWidget(title); head.addStretch()
        self.promo_refresh_btn = QPushButton("Refresh official promotions")
        self.promo_refresh_btn.clicked.connect(self.refresh_promotions_now)
        head.addWidget(self.promo_refresh_btn)
        l.addLayout(head)
        explain = QLabel(
            "Stan searches only official Bybit / Bybit EU campaign sources. Promotion alignment can only break ties among already-valid trades. "
            "It never weakens the Risk Engine and never uses wash trading, matched trading, self-trading or artificial volume. "
            "Account-specific registration/claim actions may still require the official Rewards Hub because the normal trading API does not expose every personalized campaign action."
        )
        explain.setWordWrap(True); explain.setStyleSheet(f"color:{MUTED};")
        l.addWidget(explain)
        self.promotion_status = QLabel(""); self.promotion_status.setWordWrap(True); self.promotion_status.setStyleSheet(f"color:{AMBER};")
        l.addWidget(self.promotion_status)
        self.promotions = QTextBrowser(); self.promotions.setOpenExternalLinks(True); self.promotions.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard | Qt.LinksAccessibleByMouse)
        l.addWidget(self.promotions, 1)
        self.tabs.addTab(tab, "Promotions")

    def _build_setup(self) -> None:
        tab = QWidget(); l = QVBoxLayout(tab)
        form = QFormLayout()
        self.mode = QComboBox(); self.mode.addItems(["observer", "paper", "shadow", "testnet", "autopilot_live"]); self.mode.setCurrentText(str(self.cfg['mode'])); form.addRow("Mode", self.mode)
        self.symbol = QLineEdit(str(self.cfg['symbol'])); form.addRow("Primary symbol", self.symbol)
        self.interval = QComboBox(); self.interval.addItems(["5", "15", "30", "60", "120", "240"]); self.interval.setCurrentText(str(self.cfg['interval'])); form.addRow("Decision candle", self.interval)
        self.poll = QLineEdit(str(self.cfg['poll_seconds'])); form.addRow("Poll seconds", self.poll)
        self.auto_symbol = QCheckBox("Auto-select the strongest liquid futures candidate locally (no AI tokens)"); self.auto_symbol.setChecked(bool(self.cfg.get("auto_symbol_selection", True))); form.addRow("", self.auto_symbol)
        self.scan_minutes = QLineEdit(str(self.cfg.get("symbol_scan_minutes", 30))); form.addRow("Auto-symbol scan minutes", self.scan_minutes)
        self.watchlist_size = QLineEdit(str(self.cfg.get("live_watchlist_size", 6))); form.addRow("Live liquid watchlist size", self.watchlist_size)
        self.risk = QLineEdit(str(self.cfg['risk_per_trade_pct'])); form.addRow("Risk / trade %", self.risk)
        self.daily = QLineEdit(str(self.cfg['max_daily_loss_pct'])); form.addRow("Max daily loss %", self.daily)
        self.leverage = QLineEdit(str(self.cfg['max_leverage'])); form.addRow("Max leverage", self.leverage)
        self.notional = QLineEdit(str(self.cfg['max_notional_usdt'])); form.addRow("Max notional USDT", self.notional)
        self.conf = QLineEdit(str(self.cfg['min_confidence'])); form.addRow("Minimum AI confidence", self.conf)
        self.threshold = QLineEdit(str(self.cfg['ai_candidate_threshold'])); form.addRow("Local prefilter threshold", self.threshold)
        self.token_budget = QLineEdit(str(self.cfg['trading_token_budget_daily'])); form.addRow("Trading AI daily tokens", self.token_budget)
        self.universe_n = QLineEdit(str(self.cfg.get('research_universe_top_n', 12))); form.addRow("Research: top liquid markets", self.universe_n)
        self.backtest_n = QLineEdit(str(self.cfg.get('research_backtest_symbols', 3))); form.addRow("Research: backtest symbols", self.backtest_n)
        self.backtest_candles = QLineEdit(str(self.cfg.get('research_backtest_candles', 1600))); form.addRow("Research: candles / timeframe", self.backtest_candles)
        self.ai = QCheckBox("Enable LLM Futures Analyst only after local prefilter"); self.ai.setChecked(bool(self.cfg['ai_enabled'])); form.addRow("", self.ai)
        self.news = QCheckBox("Allow current web/news research with cooldown"); self.news.setChecked(bool(self.cfg['news_enabled'])); form.addRow("", self.news)
        self.promotions_enabled = QCheckBox("Enable official Bybit Promotion Intelligence (safe alignment only)"); self.promotions_enabled.setChecked(bool(self.cfg.get('promotion_intelligence_enabled', True))); form.addRow("", self.promotions_enabled)
        self.promo_region = QLineEdit(str(self.cfg.get('promotion_region_hint', 'auto'))); self.promo_region.setPlaceholderText("auto, Poland/EEA, Bybit EU, Global..."); form.addRow("Promotion region/account hint", self.promo_region)
        self.promo_refresh_hours = QLineEdit(str(self.cfg.get('promotion_refresh_hours', 12))); form.addRow("Promotion refresh hours", self.promo_refresh_hours)
        self.auto_bootstrap = QCheckBox("Automatically run Professional First Setup after the first successful Bybit connection")
        self.auto_bootstrap.setChecked(bool(self.cfg.get('auto_bootstrap_after_connection', True))); form.addRow("", self.auto_bootstrap)
        self.autostart = QCheckBox("Auto-start Trading Core when Bybit AI Manager launches"); self.autostart.setChecked(bool(self.cfg.get('auto_start', False))); form.addRow("", self.autostart)
        l.addLayout(form)

        key_title = QLabel("Bybit API connection"); key_title.setStyleSheet("font-size:16px;font-weight:700;margin-top:12px;"); l.addWidget(key_title)
        keyform = QFormLayout()
        self.key_env = QComboBox(); self.key_env.addItems(["auto", "testnet", "mainnet_readonly", "mainnet_trade"]); self.key_env.setCurrentText(str(self.cfg.get("bybit_key_environment", "auto"))); keyform.addRow("Saved key environment", self.key_env)
        self.bybit_key = QLineEdit(); self.bybit_key.setEchoMode(QLineEdit.Password); self.bybit_key.setPlaceholderText("Configured — leave blank to keep" if has_bybit_credentials() else "Bybit API key")
        self.bybit_secret = QLineEdit(); self.bybit_secret.setEchoMode(QLineEdit.Password); self.bybit_secret.setPlaceholderText("Configured — leave blank to keep" if has_bybit_credentials() else "Bybit API secret")
        keyform.addRow("API key", self.bybit_key); keyform.addRow("API secret", self.bybit_secret); l.addLayout(keyform)
        actions = QHBoxLayout()
        save = QPushButton("Save settings"); save.clicked.connect(self.save); actions.addWidget(save)
        test = QPushButton("Test Bybit + permissions"); test.clicked.connect(self.test_connection); actions.addWidget(test)
        clear = QPushButton("Remove Bybit credentials"); clear.clicked.connect(self.clear_creds); actions.addWidget(clear)
        actions.addStretch(); l.addLayout(actions)
        warning = QLabel(
            "Simple setup: paste a dedicated Bybit API key + secret once. Stan auto-detects Mainnet/Testnet and permissions. "
            "For live Autopilot the key must have ContractTrade Order + Position only; Wallet/withdrawal permissions are rejected. Never paste API secrets into chat."
        )
        warning.setWordWrap(True); warning.setStyleSheet(f"color:{MUTED};padding-top:8px;"); l.addWidget(warning)
        l.addStretch()
        self.tabs.addTab(tab, "Setup & Risk")

    def _build_history(self) -> None:
        tab = QWidget(); l = QVBoxLayout(tab)
        self.history = QTextBrowser(); self.history.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        l.addWidget(self.history)
        btn = QPushButton("Refresh history"); btn.clicked.connect(self.refresh_history); l.addWidget(btn)
        self.tabs.addTab(tab, "Journal")

    def _persist(self, notify: bool = False) -> bool:
        try:
            save_trading_settings({
                "mode": self.mode.currentText(), "symbol": self.symbol.text().strip().upper(), "interval": self.interval.currentText(),
                "poll_seconds": int(self.poll.text()), "auto_symbol_selection": self.auto_symbol.isChecked(), "symbol_scan_minutes": int(self.scan_minutes.text()), "live_watchlist_size": int(self.watchlist_size.text()), "risk_per_trade_pct": float(self.risk.text()), "max_daily_loss_pct": float(self.daily.text()),
                "max_leverage": float(self.leverage.text()), "max_notional_usdt": float(self.notional.text()), "min_confidence": float(self.conf.text()),
                "ai_candidate_threshold": float(self.threshold.text()), "trading_token_budget_daily": int(self.token_budget.text()),
                "research_universe_top_n": int(self.universe_n.text()), "research_backtest_symbols": int(self.backtest_n.text()),
                "research_backtest_candles": int(self.backtest_candles.text()),
                "ai_enabled": self.ai.isChecked(), "news_enabled": self.news.isChecked(), "auto_start": self.autostart.isChecked(),
                "promotion_intelligence_enabled": self.promotions_enabled.isChecked(), "promotion_region_hint": self.promo_region.text().strip() or "auto",
                "promotion_refresh_hours": int(self.promo_refresh_hours.text()),
                "auto_bootstrap_after_connection": self.auto_bootstrap.isChecked(), "market_data_testnet": False,
                "execution_environment": ("testnet" if self.key_env.currentText() == "testnet" or self.mode.currentText() == "testnet" else "mainnet"),
                "bybit_key_environment": self.key_env.currentText(),
            })
            key = self.bybit_key.text().strip(); secret = self.bybit_secret.text().strip()
            if key or secret:
                if not (key and secret):
                    raise ValueError("Paste both Bybit key and secret, or leave both blank.")
                save_bybit_credentials(key, secret)
                self.bybit_key.clear(); self.bybit_secret.clear()
                self.bybit_key.setPlaceholderText("Configured — leave blank to keep")
                self.bybit_secret.setPlaceholderText("Configured — leave blank to keep")
            self.cfg = load_trading_settings()
            if notify:
                QMessageBox.information(self, "Trading Core", "Trading settings saved.")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Trading settings", str(exc))
            return False

    def save(self) -> None:
        new_credentials = bool(self.bybit_key.text().strip() or self.bybit_secret.text().strip())
        if not self._persist(notify=True):
            return
        # First professional connection flow: after the user saves a new Bybit key,
        # validate permissions immediately. A successful validation then triggers
        # Professional First Setup when auto_bootstrap_after_connection is enabled.
        if new_credentials:
            self.test_connection()

    def clear_creds(self) -> None:
        if QMessageBox.question(self, "Bybit credentials", "Remove locally stored Bybit API credentials?") == QMessageBox.Yes:
            clear_bybit_credentials(); QMessageBox.information(self, "Bybit", "Credentials removed.")

    def test_connection(self) -> None:
        if not self._persist(notify=False):
            return
        def work() -> None:
            try:
                guard = validate_autopilot_key()
                label = "Bybit Testnet" if guard.get("testnet") else "Bybit Mainnet"
                text = (
                    f"{label} connection OK. Read-only: {bool(guard.get('read_only'))}. "
                    f"Safe Autopilot mode: {guard.get('autopilot_mode')}. "
                    f"Permissions: {json.dumps(guard.get('permissions') or {}, ensure_ascii=False)}\n\n"
                    f"{guard.get('message','')}"
                )
                self._notice = (label, text, False)
                save_trading_settings({"bybit_key_environment": str(guard.get("key_environment", "auto"))})
                if bool(load_trading_settings().get("auto_bootstrap_after_connection", True)) and get_research_state("bootstrap_complete", "0") != "1":
                    self.start_bootstrap(from_worker=True)
            except Exception as exc:
                self._notice = ("Bybit connection", str(exc), True)
        threading.Thread(target=work, daemon=True).start()

    def refresh_promotions_now(self) -> None:
        if self._promotion_inflight:
            return
        if not self._persist(notify=False):
            return
        self._promotion_inflight = True
        self.promotion_status.setText("Scanning current official Bybit promotions...")
        def work() -> None:
            try:
                cfg = load_trading_settings()
                result = refresh_promotions(region_hint=str(cfg.get("promotion_region_hint", "auto")), force=True)
                count = len(result.get("campaigns", [])) if isinstance(result, dict) else 0
                self._notice = ("Promotion Intelligence", f"Official promotion scan completed. Campaigns found: {count}.", False)
            except Exception as exc:
                self._notice = ("Promotion Intelligence", str(exc), True)
            finally:
                self._promotion_inflight = False
        threading.Thread(target=work, daemon=True).start()

    def refresh_promotions_view(self) -> None:
        latest = latest_promotion_scan()
        if not latest:
            self.promotions.setPlainText("No promotion scan yet. Run Professional First Setup or click Refresh official promotions.")
            self.promotion_status.setText("Promotion scan: not run yet")
            return
        campaigns = latest.get("campaigns") or []
        self.promotion_status.setText(f"Last official scan: {latest.get('scanned_at','')} • {len(campaigns)} campaign(s)")
        chunks = [str((latest.get("summary") or {}).get("scan_summary", ""))]
        for i, c in enumerate(campaigns, start=1):
            tasks = "\n    - ".join(str(x) for x in (c.get("tasks") or []))
            restrictions = "; ".join(str(x) for x in (c.get("restrictions") or []))
            flags = ", ".join(str(x) for x in (c.get("safety_flags") or []))
            symbols = ", ".join(str(x) for x in (c.get("eligible_symbols") or []))
            chunks.append(
                f"{i}. {c.get('name')}\n"
                f"   Region: {c.get('region')}   Ends: {c.get('ends_at') or 'unknown'}   Reward: {c.get('reward_type')}\n"
                f"   Registration: {bool(c.get('requires_registration'))}   Probabilistic: {bool(c.get('probabilistic'))}   Action: {c.get('actionability')}\n"
                f"   Trading volume threshold: {c.get('trading_volume_requirement_usd')}   Symbols: {symbols or 'not specified'}\n"
                f"   Tasks:\n    - {tasks if tasks else 'not fully available / account-specific'}\n"
                f"   Restrictions: {restrictions or 'see official terms'}\n"
                f"   Safety: {flags or 'strict no-abuse policy'}\n"
                f"   Official source: {c.get('source_url') or 'not captured — verify in Bybit Rewards Hub'}"
            )
        self.promotions.setPlainText("\n\n".join(x for x in chunks if x))

    def start_bootstrap(self, from_worker: bool = False) -> None:
        if self._bootstrap_inflight:
            return
        if not from_worker and not self._persist(notify=False):
            return
        self._bootstrap_inflight = True
        self._bootstrap_progress = "Starting professional first-run research..."
        def progress(event: dict[str, Any]) -> None:
            self._bootstrap_progress = str(event.get("message", "Research running..."))
        def work() -> None:
            try:
                report = run_professional_bootstrap(progress=progress)
                chief = report.get("chief_research") or {}
                self._notice = (
                    "Professional First Setup",
                    "Initial futures research baseline completed.\n\n" + str(chief.get("market_regime", "Open Research Lab for details.")),
                    False,
                )
            except Exception as exc:
                self._notice = ("Professional First Setup", str(exc), True)
            finally:
                self._bootstrap_inflight = False
        threading.Thread(target=work, daemon=True).start()

    def start_engine(self) -> None:
        if not self._persist(notify=False):
            return
        try:
            result = core_command("start_autopilot", timeout=240.0)
            self._notice = ("Stan Autopilot", str(result.get("message", "Started")) if isinstance(result, dict) else str(result), False)
        except Exception as exc:
            self._notice = ("Stan Autopilot", str(exc), True)
        self.refresh()

    def stop_engine(self) -> None:
        try:
            core_command("stop_trading", timeout=20.0)
        except Exception as exc:
            self._notice = ("Trading Core", str(exc), True)
        self.refresh()

    def analyze_now(self) -> None:
        if not self._persist(notify=False):
            return
        self._analysis_inflight = True; self.analyze_btn.setEnabled(False)
        def work() -> None:
            try:
                core_command("analyze_now", timeout=240.0)
            except Exception as exc:
                self._notice = ("Trading analysis", str(exc), True)
            finally:
                self._analysis_inflight = False
        threading.Thread(target=work, daemon=True).start()

    def refresh(self) -> None:
        core = core_status()
        st = core.get("trading", {}) if isinstance(core, dict) else {}
        if not isinstance(st, dict): st = {}
        running = bool(st.get('running'))
        self.state.setText("RUNNING" if running else "STOPPED")
        self.state.setStyleSheet(f"font-size:16px;font-weight:700;color:{GREEN if running else MUTED};")
        self.status_note.setText(str(st.get('message','')) + f"   Trading AI today: {int(st.get('trading_tokens_today',0)):,} tokens")
        snapshot = st.get('last_snapshot'); assessment = st.get('last_assessment'); risk = st.get('last_risk'); execution = st.get('last_execution')
        chunks = ["MARKET\n" + _fmt_snapshot(snapshot if isinstance(snapshot, dict) else None)]
        current_symbol = str((snapshot or {}).get("symbol") or "").upper() if isinstance(snapshot, dict) else ""
        assessment_symbol = str((assessment or {}).get("analysis_symbol") or "").upper() if isinstance(assessment, dict) else ""
        if not assessment_symbol:
            parts = str(st.get("last_completed_analysis_key") or "").split(":")
            if len(parts) >= 4 and parts[0] == "futures":
                assessment_symbol = str(parts[1]).upper()
        stale = bool(current_symbol and assessment_symbol and current_symbol != assessment_symbol)
        if isinstance(assessment, dict): chunks.append((f"LAST AI FUTURES ANALYST ({assessment_symbol}, previous market)\n" if stale else "AI FUTURES ANALYST\n") + json.dumps(assessment, ensure_ascii=False, indent=2))
        if isinstance(risk, dict): chunks.append(("LAST HARD RISK ENGINE (previous market)\n" if stale else "HARD RISK ENGINE\n") + json.dumps(risk, ensure_ascii=False, indent=2))
        if isinstance(execution, dict): chunks.append(("LAST EXECUTION (previous market)\n" if stale else "EXECUTION\n") + json.dumps(execution, ensure_ascii=False, indent=2))
        if st.get('last_error'): chunks.append("ERROR\n" + str(st.get('last_error')))
        pos = st.get('paper_position')
        if pos: chunks.append("PAPER POSITION\n" + json.dumps(pos, ensure_ascii=False, indent=2))
        self.dashboard.setPlainText("\n\n".join(chunks))
        self.start_btn.setEnabled(not running); self.stop_btn.setEnabled(running); self.analyze_btn.setEnabled(not self._analysis_inflight)
        self.bootstrap_btn.setEnabled(not self._bootstrap_inflight)
        self.promo_refresh_btn.setEnabled(not self._promotion_inflight)
        latest_promo = latest_promotion_scan()
        promo_stamp = str(latest_promo.get("scanned_at", "")) if latest_promo else ""
        if promo_stamp and promo_stamp != getattr(self, "_last_rendered_promo", ""):
            self.refresh_promotions_view(); self._last_rendered_promo = promo_stamp
        if self._promotion_inflight:
            self.promotion_status.setText("PROMOTION SCAN RUNNING • official Bybit sources only")
        if self._bootstrap_inflight:
            self.research_status.setText("RESEARCH RUNNING • " + self._bootstrap_progress)
        else:
            last_at = get_research_state("last_bootstrap_at", "")
            self.research_status.setText("Last professional baseline: " + (last_at or "not run yet"))
        notice = self._notice
        if notice is not None:
            self._notice = None
            title, text, is_error = notice
            if is_error: QMessageBox.critical(self, title, text)
            else: QMessageBox.information(self, title, text)
        # Cheap local refresh of research text only when a run has just finished.
        if not self._bootstrap_inflight and get_research_state("last_bootstrap_at", ""):
            if getattr(self, "_last_rendered_bootstrap", "") != get_research_state("last_bootstrap_at", ""):
                self.refresh_research()

    def refresh_research(self) -> None:
        item = latest_bootstrap()
        if not item:
            self.research.setPlainText("No professional baseline yet. You may run it even before private Bybit credentials are added; account-specific diagnostics will be added once a key is configured.")
            return
        report = item.get("report") or {}
        chunks = [
            f"BOOTSTRAP #{item.get('id')} • {item.get('status')} • started {item.get('started_at')} • finished {item.get('finished_at')}",
        ]
        account = report.get("account") or {}
        chunks.append("ACCOUNT / API DIAGNOSTICS\n" + json.dumps({
            "configured": account.get("configured"), "environment": account.get("environment"),
            "read_only": account.get("read_only"), "permissions": account.get("permissions"),
            "open_positions": len(account.get("open_positions", [])) if isinstance(account.get("open_positions"), list) else 0,
            "recent_execution_count": account.get("recent_execution_count", 0),
        }, ensure_ascii=False, indent=2))
        universe = report.get("universe") or []
        chunks.append("LIQUID FUTURES UNIVERSE\n" + "\n".join(
            f"{i+1:>2}. {x.get('symbol')} turnover={float(x.get('turnover_24h',0)):,.0f} 24h={float(x.get('price_24h_pct',0)):+.2f}% funding={x.get('funding_rate')} spread={float(x.get('spread_bps',0)):.2f}bps"
            for i, x in enumerate(universe[:15])
        ))
        regimes = report.get("regimes") or []
        chunks.append("MULTI-TIMEFRAME REGIME\n" + "\n".join(
            f"{x.get('symbol')}: {x.get('dominant')} alignment={x.get('alignment')}" for x in regimes
        ))
        derivatives = report.get("derivatives_snapshots") or []
        if derivatives:
            chunks.append("DERIVATIVES CONTEXT\n" + "\n".join(
                f"{x.get('symbol')}: funding={x.get('funding_rate')} OI-change={x.get('open_interest_change_pct')}% "
                f"long/short={x.get('long_ratio')}/{x.get('short_ratio')} spread={x.get('spread_bps')}bps setup={x.get('setup_strength')}"
                for x in derivatives
            ))
        leader = strategy_leaderboard(25)
        chunks.append("LOCAL STRATEGY LAB — BEST RECENT TESTS\n" + "\n".join(
            f"{x.get('symbol')} {x.get('interval')}m • {x.get('name')} • robust={x.get('robust')} score={x.get('robustness_score')} "
            f"OOS expectancy={((x.get('out_of_sample') or {}).get('expectancy_r'))}R PF={((x.get('out_of_sample') or {}).get('profit_factor'))} trades={((x.get('out_of_sample') or {}).get('trades'))}"
            for x in leader[:15]
        ))
        promo = report.get("promotions") or {}
        if promo:
            campaigns = promo.get("campaigns") or []
            chunks.append("PROMOTION INTELLIGENCE\n" + str(promo.get("scan_summary", "")) + "\n" + "\n".join(
                f"- {c.get('name')} • region={c.get('region')} • ends={c.get('ends_at')} • action={c.get('actionability')} • symbols={','.join(c.get('eligible_symbols') or [])}"
                for c in campaigns[:12] if isinstance(c, dict)
            ))
        chief = report.get("chief_research") or {}
        if chief:
            chunks.append("CHIEF FUTURES RESEARCH ANALYST\n" + json.dumps(chief, ensure_ascii=False, indent=2))
        if item.get("error"):
            chunks.append("ERROR\n" + str(item.get("error")))
        self.research.setPlainText("\n\n".join(chunks))
        self._last_rendered_bootstrap = get_research_state("last_bootstrap_at", "")

    def refresh_history(self) -> None:
        rows = []
        for item in recent_assessments(30):
            rows.append(
                f"{item.get('ts')}  {item.get('symbol')}  {item.get('action')}  confidence={item.get('confidence')}\n"
                f"Risk: {json.dumps(item.get('risk',{}),ensure_ascii=False)}\nExecution: {json.dumps(item.get('execution',{}),ensure_ascii=False)}"
            )
        trades = recent_paper_trades(30)
        if trades:
            rows.append("\nPAPER TRADES\n" + "\n".join(json.dumps(t, ensure_ascii=False) for t in trades))
        self.history.setPlainText("\n\n".join(rows) if rows else "No trading history yet.")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.timer.stop(); event.accept()
