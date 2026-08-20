from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTabWidget,
    QTextBrowser, QVBoxLayout
)

from core_client import command, ensure_running, events, status

BG = "#202123"
PANEL = "#292a2d"
PANEL2 = "#242528"
TEXT = "#f4f4f4"
MUTED = "#a5a7ad"
BORDER = "#3b3d43"
GREEN = "#19c37d"
AMBER = "#f2b84b"
RED = "#ff6b6b"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


class AccountOSDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bybit AI Manager — Opportunity OS")
        self.resize(1030, 760)
        self.setMinimumSize(900, 650)
        self.setStyleSheet(f"""
            QDialog {{ background:{BG}; color:{TEXT}; }}
            QLabel {{ color:{TEXT}; font-family:'Segoe UI'; }}
            QPushButton {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; border-radius:10px; padding:10px 14px; font-size:13px; }}
            QPushButton:hover {{ background:#36383d; }}
            QTabWidget::pane {{ border:1px solid {BORDER}; border-radius:10px; background:{PANEL2}; }}
            QTabBar::tab {{ background:{PANEL}; color:{MUTED}; padding:10px 15px; border:1px solid {BORDER}; }}
            QTabBar::tab:selected {{ color:{TEXT}; background:#34363b; }}
            QTextBrowser {{ background:{PANEL2}; color:{TEXT}; border:none; padding:8px; }}
        """)

        # v4.5.8: UI refresh must never throw the user back to the top.
        self._content_cache: dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        title = QLabel("Bybit AI Manager — Bybit Opportunity OS")
        title.setStyleSheet("font-size:27px;font-weight:750;")
        root.addWidget(title)
        subtitle = QLabel(
            "Одна кнопка запускає весь цикл: перевірка Bybit → професійний bootstrap → ринок/новини/стратегії → "
            "Risk Engine → Futures/Spot → журнал → навчання → Promotions/Rewards/Alpha/Earn discovery. Технічні параметри Stan веде сам."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"font-size:12px;color:{MUTED};")
        root.addWidget(subtitle)

        card = QFrame()
        card.setStyleSheet(f"QFrame{{background:{PANEL2};border:1px solid {BORDER};border-radius:14px;}}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(10)

        self.state = QLabel("Підключаюся до Stan Core…")
        self.state.setWordWrap(True)
        self.state.setStyleSheet("font-size:13px;font-weight:600;")
        cl.addWidget(self.state)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("▶  START STAN")
        self.start_btn.setMinimumHeight(54)
        self.start_btn.setStyleSheet(
            f"QPushButton{{font-size:15px;font-weight:750;background:{GREEN};color:white;border:none;border-radius:11px;padding:12px 20px;}}"
            "QPushButton:hover{background:#20b987;} QPushButton:disabled{background:#355c51;color:#a8b4b0;}"
        )
        self.start_btn.clicked.connect(self._start_one_button)
        buttons.addWidget(self.start_btn, 1)

        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setMinimumHeight(54)
        self.stop_btn.clicked.connect(lambda: self._action("hard_stop"))
        buttons.addWidget(self.stop_btn)

        self.bybit_btn = QPushButton("Підключити Bybit")
        self.bybit_btn.setMinimumHeight(54)
        self.bybit_btn.clicked.connect(self._open_setup)
        buttons.addWidget(self.bybit_btn)
        cl.addLayout(buttons)

        hint = QLabel(
            "Після першого успішного підключення API тобі не треба вручну обирати монету, RSI, стратегію чи плече. "
            "Stan сам сканує, досліджує й масштабує роботу тільки за підтвердженою статистикою; hard-risk межі модель обійти не може."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED};font-size:11px;")
        cl.addWidget(hint)
        root.addWidget(card)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.overview = QTextBrowser()
        self.learning = QTextBrowser()
        self.opportunities = QTextBrowser()
        self.details = QTextBrowser()
        for widget, name in [
            (self.overview, "Overview"),
            (self.learning, "Learning & Research"),
            (self.opportunities, "Opportunity OS"),
            (self.details, "Technical details"),
        ]:
            widget.setOpenExternalLinks(True)
            widget.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
                | Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            self.tabs.addTab(widget, name)

        tools = QHBoxLayout()
        analyze = QPushButton("Analyze now")
        analyze.clicked.connect(lambda: self._action("analyze_now"))
        tools.addWidget(analyze)
        research = QPushButton("Refresh research")
        research.clicked.connect(lambda: self._action("run_bootstrap"))
        tools.addWidget(research)
        promos = QPushButton("Refresh promotions")
        promos.clicked.connect(lambda: self._action("refresh_promotions"))
        tools.addWidget(promos)
        opportunities = QPushButton("Refresh Opportunity OS")
        opportunities.clicked.connect(lambda: self._action("refresh_opportunities"))
        tools.addWidget(opportunities)
        auth_browser = QPushButton("Authorize Bybit Browser")
        auth_browser.clicked.connect(lambda: self._action("authorize_bybit_browser"))
        tools.addWidget(auth_browser)
        tools.addStretch(1)
        root.addLayout(tools)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        ensure_running()
        self.refresh()

    def _open_setup(self) -> bool:
        from bybit_setup_ui import BybitSetupDialog
        dlg = BybitSetupDialog(self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        self.refresh()
        return accepted

    def _start_one_button(self) -> None:
        st = status()
        if not bool(st.get("bybit_credentials_configured")):
            if not self._open_setup():
                return
        self._action("start_autopilot")

    def _action(self, action: str) -> None:
        self.start_btn.setEnabled(False)
        try:
            timeout = 240.0 if action in {"analyze_now", "start_autopilot"} else (8.0 if action == "hard_stop" else 35.0)
            result = command(action, timeout=timeout)
            if action == "start_autopilot":
                result = result if isinstance(result, dict) else {}
                pre = result.get("prelaunch") or {}
                eq = pre.get("equity_usdt")
                mode = result.get("mode", "")
                QMessageBox.information(
                    self,
                    "Stan запущено",
                    f"Autopilot прийняв запуск.\n\nMode: {mode}\n"
                    + (f"Bybit equity: {eq:.4f} USDT\n" if isinstance(eq, (int, float)) else "")
                    + "Professional Bootstrap і 24/7 monitoring працюють у фоні. Live entry автоматично залишається заблокованим, доки всі pre-launch/research gates не PASS.",
                )
            elif action == "hard_stop":
                QMessageBox.information(
                    self,
                    "Stan зупинено",
                    "STOP зафіксовано. Trading Core і фоновий Stan Core завершуються.\n\n"
                    "Stan не має права автоматично запуститися знову — ні через 24/7 режим, ні через status polling, ні після перезапуску Windows.\n"
                    "Тільки явний START STAN зніме цей stop-lock.",
                )
            elif action == "analyze_now":
                QMessageBox.information(self, "Stan Analysis", "Аналіз завершено. Результат оновлено в Overview; технічні деталі є в останній вкладці.")
            elif action == "authorize_bybit_browser":
                if isinstance(result, dict) and result.get("opened"):
                    QMessageBox.information(self, "Bybit Browser", "Відкрито окремий браузерний профіль Stan. Увійди в Bybit / пройди 2FA нормально. Вікно МОЖНА залишити відкритим — v4.5.8 підключається до цієї самої сесії й більше не запускає другий Chrome поверх заблокованого профілю.")
                else:
                    QMessageBox.warning(self, "Bybit Browser", str((result or {}).get("reason", "Не вдалося відкрити браузер.")))
        except Exception as exc:
            QMessageBox.critical(self, "Bybit AI Manager", f"{type(exc).__name__}: {exc}")
        finally:
            self.start_btn.setEnabled(True)
            self.refresh()

    def _set_preserve_scroll(self, widget: QTextBrowser, key: str, content: str, *, html_mode: bool = True) -> None:
        """Update a live panel without stealing the user's scroll position.

        QTextBrowser.setHtml()/setPlainText() resets the viewport to the top. The Account OS
        refreshes every ~1.5s, so preserve the exact position (or bottom-follow state) and skip
        pointless repainting when content is unchanged.
        """
        if self._content_cache.get(key) == content:
            return
        bar = widget.verticalScrollBar()
        old_value = int(bar.value())
        old_max = int(bar.maximum())
        was_bottom = old_max > 0 and old_value >= old_max - 4
        self._content_cache[key] = content
        if html_mode:
            widget.setHtml(content)
        else:
            widget.setPlainText(content)

        def restore() -> None:
            new_bar = widget.verticalScrollBar()
            if was_bottom:
                new_bar.setValue(new_bar.maximum())
            else:
                new_bar.setValue(min(old_value, new_bar.maximum()))

        QTimer.singleShot(0, restore)

    def _card(self, label: str, value: str, detail: str = "", color: str = TEXT) -> str:
        return (
            f"<div class='card'><div class='k'>{_esc(label)}</div>"
            f"<div class='v' style='color:{color}'>{_esc(value)}</div>"
            + (f"<div class='d'>{_esc(detail)}</div>" if detail else "")
            + "</div>"
        )

    def refresh(self) -> None:
        st = status()
        core_running = bool(st.get("running"))
        manual_stop = bool((st.get("manual_stop") or {}).get("active")) if isinstance(st.get("manual_stop"), dict) else False
        trade = st.get("trading") if isinstance(st.get("trading"), dict) else {}
        settings = st.get("settings") if isinstance(st.get("settings"), dict) else {}
        pre = st.get("prelaunch") if isinstance(st.get("prelaunch"), dict) else {}
        strategy = st.get("strategy_governor") if isinstance(st.get("strategy_governor"), dict) else {}
        opp = st.get("opportunity_plan") if isinstance(st.get("opportunity_plan"), dict) else {}
        opp_os = st.get("opportunity_os") if isinstance(st.get("opportunity_os"), dict) else {}
        promo_lifecycle = st.get("promotion_lifecycle") if isinstance(st.get("promotion_lifecycle"), dict) else {}
        ai_budget = st.get("ai_budget") if isinstance(st.get("ai_budget"), dict) else {}
        runtime = st.get("runtime") if isinstance(st.get("runtime"), dict) else {}

        mode = str(settings.get("mode", "observer"))
        mode_text = {"autopilot_live": "LIVE LEARNING", "testnet": "TESTNET", "shadow": "SHADOW", "paper": "PAPER", "observer": "OBSERVER"}.get(mode, mode.upper())
        api_ok = bool(st.get("bybit_credentials_configured"))
        running = bool(trade.get("running"))
        bootstrap = bool(st.get("bootstrap_running"))
        promo_running = bool(st.get("promotion_running"))
        opportunity_running = bool(st.get("opportunity_running"))
        runtime_state = str(runtime.get("state") or "").upper()
        if manual_stop or runtime_state in {"STOPPING", "STOPPED"}:
            self.state.setText(
                f"{('STOPPING' if runtime_state == 'STOPPING' else 'STOPPED MANUALLY')}  •  Stan: OFF  •  Bybit: {'CONNECTED' if api_ok else 'NOT CONNECTED'}  •  "
                "тільки START STAN може запустити систему знову"
            )
        else:
            provider = ai_budget.get("provider") if isinstance(ai_budget, dict) and isinstance(ai_budget.get("provider"), dict) else {}
            ai_state_text = "AI: PAUSED (CREDIT)" if bool(provider.get("paused")) and str(provider.get("code")) == "credit_balance_exhausted" else ("AI: PAUSED" if bool(provider.get("paused")) else f"AI today: {int(st.get('trading_tokens_today', 0) or 0):,} tokens")
            self.state.setText(
                f"Core: {'RUNNING' if core_running else 'OFFLINE'}   •   Stan: {'RUNNING' if running else 'idle'}   •   "
                f"Mode: {mode_text}   •   Bybit: {'CONNECTED' if api_ok else 'NOT CONNECTED'}   •   "
                f"{ai_state_text}"
            )
        self.bybit_btn.setText("Bybit підключено ✓" if api_ok else "Підключити Bybit")
        self.stop_btn.setEnabled(core_running or running)
        self.start_btn.setEnabled(not running)

        snapshot = trade.get("last_snapshot") if isinstance(trade.get("last_snapshot"), dict) else {}
        assessment = trade.get("last_assessment") if isinstance(trade.get("last_assessment"), dict) else {}
        risk = trade.get("last_risk") if isinstance(trade.get("last_risk"), dict) else {}
        execution = trade.get("last_execution") if isinstance(trade.get("last_execution"), dict) else {}

        equity = pre.get("equity_usdt")
        pre_ready = bool(pre.get("ready")) if pre else False
        api_detail = "Додай API один раз — далі START STAN робить усе сам." if not api_ok else (
            f"{pre.get('environment','').upper()} • equity {float(equity or 0):.4f} USDT" if pre else "Ключ збережено; pre-launch ще не запускався."
        )
        pre_value = "READY ✓" if pre_ready else ("Очікує першого START" if api_ok and not pre else "BLOCKED" if pre else "—")
        pre_color = GREEN if pre_ready else AMBER

        market = "Ще немає snapshot."
        if snapshot:
            market = f"{snapshot.get('symbol','')} • {snapshot.get('interval','')}m • price {snapshot.get('price','—')} • setup {snapshot.get('setup_strength','—')}"
        decision = "Ще немає AI-рішення."
        decision_label = "LAST DECISION"
        decision_detail = ""
        assessment_symbol = str(assessment.get("analysis_symbol") or "").upper() if assessment else ""
        if not assessment_symbol:
            # Upgrade-safe fallback for v4.5.x persisted decisions.
            completed_key = str(trade.get("last_completed_analysis_key") or "")
            parts = completed_key.split(":")
            if len(parts) >= 4 and parts[0] == "futures":
                assessment_symbol = str(parts[1]).upper()
        current_symbol = str(snapshot.get("symbol") or "").upper() if snapshot else ""
        assessment_matches_current = not (assessment and assessment_symbol and current_symbol and assessment_symbol != current_symbol)
        if assessment:
            decision = f"{str(assessment.get('action','HOLD')).upper()} • confidence {float(assessment.get('confidence',0) or 0):.0%}"
            decision_detail = str(assessment.get("thesis", ""))[:500]
            if not assessment_matches_current:
                decision_label = f"LAST AI DECISION • {assessment_symbol}"
                decision += " • previous market"
                decision_detail = f"Current {current_symbol} snapshot has no fresh AI decision yet. " + decision_detail
        risk_text = "Ще не оцінював угоду."
        risk_color = MUTED
        if risk:
            allowed = bool(risk.get("allowed"))
            if risk.get("sizing_evaluated") is False or str(risk.get("action") or "").lower() not in {"long", "short"}:
                risk_text = "NO ENTRY • AI HOLD" + (f" • stage {risk.get('growth_stage')}" if risk.get("growth_stage") else "")
                risk_color = MUTED
            else:
                risk_text = ("PASS ✓" if allowed else "BLOCK") + (f" • stage {risk.get('growth_stage')}" if risk.get("growth_stage") else "")
                risk_color = GREEN if allowed else AMBER
        exec_text = "Виконань ще не було."
        exec_detail = ""
        if execution:
            confirmation = execution.get("confirmation") if isinstance(execution.get("confirmation"), dict) else {}
            if execution.get("confirmed") and execution.get("executed"):
                exec_text = "CONFIRMED FILL ✓"
            elif execution.get("submitted"):
                exec_text = "ORDER SUBMITTED — NOT CONFIRMED"
            else:
                exec_text = str(execution.get("message") or execution.get("error") or execution.get("submit_error") or execution.get("mode") or "Recorded")[:240]
            parts = []
            if execution.get("order_link_id"):
                parts.append(f"link {execution.get('order_link_id')}")
            if confirmation.get("lifecycle"):
                parts.append(f"lifecycle {confirmation.get('lifecycle')}")
            if confirmation.get("cum_exec_qty") not in (None, "", 0, 0.0):
                parts.append(f"filled qty {confirmation.get('cum_exec_qty')}")
            if confirmation.get("position_open") is not None:
                parts.append(f"position {'OPEN' if confirmation.get('position_open') else 'FLAT'}")
            if confirmation.get("protected") is not None:
                parts.append(f"protection {'VERIFIED' if confirmation.get('protected') else 'UNVERIFIED'}")
            if execution.get("slippage_tolerance_pct") is not None:
                parts.append(f"slippage cap {execution.get('slippage_tolerance_pct')}%")
            if execution.get("submit_error"):
                parts.append(f"submit error {str(execution.get('submit_error'))[:180]}")
            if confirmation.get("reason"):
                parts.append(str(confirmation.get("reason"))[:220])
            exec_detail = " • ".join(parts)[:900]
        risk_label = "RISK ENGINE"
        execution_label = "LAST EXECUTION"
        if assessment and not assessment_matches_current:
            risk_label = f"LAST RISK ENGINE • {assessment_symbol}"
            execution_label = f"LAST EXECUTION • {assessment_symbol}"
            if risk_text != "Ще не оцінював угоду.":
                risk_text += " • previous market"

        style = f"""
        <style>
          body {{ color:{TEXT}; font-family:'Segoe UI'; font-size:14px; }}
          .card {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:10px; padding:14px; margin:8px 0; }}
          .k {{ color:{MUTED}; font-size:11px; text-transform:uppercase; }}
          .v {{ font-size:17px; font-weight:650; margin-top:3px; }}
          .d {{ color:{MUTED}; font-size:12px; margin-top:4px; }}
        </style>
        """
        overview = style
        overview += self._card("BYBIT", "ПІДКЛЮЧЕНО ✓" if api_ok else "НЕ ПІДКЛЮЧЕНО", api_detail, GREEN if api_ok else AMBER)
        overview += self._card("PRE-LAUNCH", pre_value, "; ".join(pre.get("fatal_failures") or []) if pre else "", pre_color)
        overview += self._card("STAN CORE", "STOPPED MANUALLY" if manual_stop else ("24/7 ACTIVE" if running else ("BOOTSTRAP…" if bootstrap else "READY TO START")), str(st.get("message", "")), RED if manual_stop else (GREEN if running else AMBER))
        if ai_budget:
            provider = ai_budget.get("provider") if isinstance(ai_budget.get("provider"), dict) else {}
            by_kind = ai_budget.get("budgeted_by_kind") if isinstance(ai_budget.get("budgeted_by_kind"), dict) else (ai_budget.get("by_kind") if isinstance(ai_budget.get("by_kind"), dict) else {})
            top_kinds = sorted(by_kind.items(), key=lambda kv: int((kv[1] or {}).get("tokens", 0) or 0), reverse=True)[:5]
            kind_detail = " • ".join(f"{k}: {int((v or {}).get('calls',0))} calls/{int((v or {}).get('tokens',0)):,} tok" for k, v in top_kinds)
            if bool(provider.get("paused")):
                code = str(provider.get("code") or "provider_paused")
                title = "AI PAUSED — API CREDIT EXHAUSTED" if code == "credit_balance_exhausted" else "AI PAUSED — PROVIDER UNAVAILABLE"
                detail = str(provider.get("reason") or provider.get("last_error") or code)[:420]
                if provider.get("next_probe_at"):
                    detail += f" • next automatic recovery probe: {provider.get('next_probe_at')}"
                detail += " • Bybit scanning, deterministic filters, cached research and Browser Operator continue without paid AI calls"
                overview += self._card("AI GOVERNOR", title, detail, RED)
            elif ai_budget.get("unlimited_tokens") and ai_budget.get("unlimited_calls"):
                budget_value = f"AI FUNNEL ACTIVE • {int(ai_budget.get('used_tokens',0)):,} tokens used today"
                epoch = ai_budget.get("budget_epoch") if isinstance(ai_budget.get("budget_epoch"), dict) else {}
                pacing = trade.get("ai_entry_pacing") if isinstance(trade.get("ai_entry_pacing"), dict) else {}
                normal_pacing = pacing.get("normal") if isinstance(pacing.get("normal"), dict) else {}
                reserve_pacing = pacing.get("reserve") if isinstance(pacing.get("reserve"), dict) else {}
                pacing_text = ""
                if pacing.get("enabled"):
                    pacing_text = f" • paced now: normal {normal_pacing.get('paced_max_calls','—')}/{int(settings.get('futures_entry_verify_calls_daily',10))}, reserve {reserve_pacing.get('paced_max_calls','—')}/{int(settings.get('futures_entry_reserve_calls_daily',8))}"
                budget_detail = (
                    f"Current-patch budget calls: {int(ai_budget.get('budgeted_calls',0))} • Action Engine lanes: "
                    f"Futures verify {int(settings.get('futures_entry_verify_calls_daily',10))} + reserve {int(settings.get('futures_entry_reserve_calls_daily',8))} calls; "
                    f"Spot verify {int(settings.get('spot_entry_verify_calls_daily',7))} + reserve {int(settings.get('spot_entry_reserve_calls_daily',4))} calls • "
                    "AI reviews executable entry/SL/TP proposals instead of repeatedly generating HOLD from raw snapshots"
                    + pacing_text
                    + (f" • {kind_detail}" if kind_detail else "")
                    + (f" • budget epoch {epoch.get('version','')} {epoch.get('started_at','')}" if epoch else "")
                )
                overview += self._card("AI GOVERNOR", budget_value, budget_detail, GREEN)
            else:
                budget_value = f"{int(ai_budget.get('used_tokens',0)):,} / {int(ai_budget.get('budget_tokens',0)):,} tokens"
                budget_detail = f"AI calls {int(ai_budget.get('calls',0))}/{int(ai_budget.get('max_calls',0))} • deterministic market monitoring stays active while AI cools"
                overview += self._card("AI GOVERNOR", "COOLING — LOCAL MONITORING ACTIVE" if ai_budget.get("cooling") else budget_value, budget_detail, AMBER if ai_budget.get("cooling") else GREEN)
        caps = opp_os.get("capabilities") if isinstance(opp_os.get("capabilities"), dict) else {}
        if opp_os:
            overview += self._card("OPPORTUNITY OS", "SCANNING…" if opportunity_running else "ACTIVE ✓", f"Futures {'ON' if caps.get('futures_trade') else 'OFF'} • Spot {'ON' if caps.get('spot_trade') else 'DISCOVERY'} • Events {len(opp_os.get('official_events') or [])} • Earn {len(opp_os.get('earn_opportunities') or [])}", GREEN)
        action_summary = st.get("action_summary") if isinstance(st.get("action_summary"), dict) else {}
        if action_summary:
            counts = action_summary.get("lifecycle_counts") if isinstance(action_summary.get("lifecycle_counts"), dict) else {}
            action_bits = " • ".join(f"{k} {v}" for k, v in sorted(counts.items()) if int(v or 0) > 0)[:500]
            last_actions = list(action_summary.get("last_actions") or [])
            last_text = " • ".join(f"{x.get('status','')}: {x.get('campaign','')}" for x in last_actions if isinstance(x, dict))[:500]
            reward_detail = (action_bits or "no lifecycle actions yet") + (f" • last: {last_text}" if last_text else "")
            reward_detail += f" • browser checks ~{int(settings.get('browser_action_max_cycles_daily',2))}/day, {int(settings.get('browser_action_refresh_hours',12))}h cadence, background-only; action AI 0 calls / 0 tokens"
            overview += self._card("REWARDS ACTIONS", str(action_summary.get("browser_state") or "UNKNOWN"), reward_detail, GREEN if any(str((x or {}).get('status','')).lower() in {'registered','claimed','completed','verified_complete'} for x in last_actions if isinstance(x, dict)) else AMBER)
        restrictions = trade.get("execution_restrictions") if isinstance(trade.get("execution_restrictions"), dict) else {}
        active_restrictions = list(restrictions.get("active") or []) if restrictions else []
        if active_restrictions:
            block_value = f"{len(active_restrictions)} ELIGIBILITY BLOCK(S) BEFORE AI"
            def _restriction_label(x: dict[str, Any]) -> str:
                scope = str(x.get("scope") or "symbol")
                target = str(x.get("family") or "").upper() if scope == "family" else str(x.get("symbol") or "")
                if x.get("persistent") or str(x.get("class") or "") == "agreement_required":
                    return f"{target or scope} {x.get('class')} PERSISTENT until exchange/human confirmation"
                return f"{target or scope} {x.get('class')} until {x.get('blocked_until','')}"
            block_detail = " • ".join(_restriction_label(x) for x in active_restrictions[:5] if isinstance(x, dict))
            overview += self._card("EXCHANGE ELIGIBILITY", block_value, block_detail + " • no paid verifier calls while blocked", AMBER)
        live_inventory = trade.get("live_position_inventory") if isinstance(trade.get("live_position_inventory"), dict) else {}
        live_positions = list(live_inventory.get("positions") or [])
        if live_positions:
            pos_bits = []
            for pos in live_positions[:4]:
                if not isinstance(pos, dict):
                    continue
                protection = "SL/TP ✓" if pos.get("protected") else "UNPROTECTED"
                pos_bits.append(f"{pos.get('symbol')} {pos.get('side')} size {pos.get('size')} • {protection} • uPnL {float(pos.get('unrealised_pnl',0) or 0):+.3f}")
            overview += self._card("LIVE FUTURES INVENTORY", f"{len(live_positions)} POSITION(S) ADOPTED FROM BYBIT", " • ".join(pos_bits) + " • STOP/restart does not make Stan assume the account is flat", GREEN if not live_inventory.get("unprotected") else RED)
        capacity = trade.get("live_account_capacity") if isinstance(trade.get("live_account_capacity"), dict) else {}
        if capacity:
            if capacity.get("error"):
                overview += self._card("ACCOUNT CAPACITY", "UNAVAILABLE — NEW AI MAY BE BLOCKED WHILE FUTURES ARE OPEN", str(capacity.get("error"))[:500], AMBER)
            else:
                available = float(capacity.get("total_available_balance_usd",0) or 0)
                equity_capacity = float(capacity.get("total_equity_usd",0) or 0)
                initial_margin = float(capacity.get("total_initial_margin_usd",0) or 0)
                capacity_source = str(capacity.get("source") or "Bybit live available balance")
                overview += self._card("ACCOUNT CAPACITY", f"{available:.2f} USD AVAILABLE / {equity_capacity:.2f} EQUITY", f"{capacity_source} • initial margin {initial_margin:.2f} • Futures sizing now shrinks to live available balance before submit instead of discovering 110007 after AI", GREEN if available > 0 else AMBER)
        overview += self._card("MARKET", market)
        proposal = trade.get("last_proposal") if isinstance(trade.get("last_proposal"), dict) else {}
        proposal_stats_raw = trade.get("proposal_stats") if isinstance(trade.get("proposal_stats"), dict) else {}
        futures_ps = proposal_stats_raw.get("futures") if isinstance(proposal_stats_raw.get("futures"), dict) else {}
        spot_ps = proposal_stats_raw.get("spot") if isinstance(proposal_stats_raw.get("spot"), dict) else {}
        if proposal:
            if bool(proposal.get("eligible")):
                prop_value = f"{str(proposal.get('action','')).upper()} PROPOSAL • q={float(proposal.get('quality',0) or 0):.2f} • {str(proposal.get('priority','normal')).upper()}"
                prop_detail = f"entry {proposal.get('entry')} • SL {proposal.get('stop_loss')} • TP {proposal.get('take_profit')} • RR {proposal.get('reward_risk')} • waiting for/under AI safety verification"
                prop_color = GREEN
            else:
                prop_value = "NO EXECUTABLE PROPOSAL"
                prop_detail = str(proposal.get("reason") or "deterministic proposal filter waiting for better structure")[:420]
                prop_color = MUTED
            overview += self._card("ACTION ENGINE", prop_value, prop_detail, prop_color)
        if futures_ps or spot_ps:
            stats_detail = (
                f"Futures created {int(futures_ps.get('created',0))} • AI approved {int(futures_ps.get('ai_approved',0))} + reused {int(futures_ps.get('ai_reused',0))} • vetoed {int(futures_ps.get('ai_vetoed',0))} • risk PASS {int(futures_ps.get('risk_passed',0))} • capacity-resized {int(futures_ps.get('capacity_resized',0))} • 110007 {int(futures_ps.get('capacity_rejected',0))} • submitted {int(futures_ps.get('submitted',0))} • confirmed fills {int(futures_ps.get('confirmed',0))} • executed {int(futures_ps.get('executed',0))}; "
                f"Spot created {int(spot_ps.get('created',0))} • AI approved {int(spot_ps.get('ai_approved',0))} + reused {int(spot_ps.get('ai_reused',0))} • vetoed {int(spot_ps.get('ai_vetoed',0))} • risk PASS {int(spot_ps.get('risk_passed',0))} • submitted {int(spot_ps.get('submitted',0))} • confirmed fills {int(spot_ps.get('confirmed',0))} • executed {int(spot_ps.get('executed',0))}"
            )
            overview += self._card("TRADE FUNNEL", "PROPOSAL → AI VERIFY/REUSE → RISK → SUBMIT → CONFIRMED FILL", stats_detail, GREEN if int(futures_ps.get('confirmed',0) or 0)+int(spot_ps.get('confirmed',0) or 0) > 0 else AMBER)
        try:
            watch_raw = trade.get("last_watchlist_scan") or ""
            watch = json.loads(watch_raw) if isinstance(watch_raw, str) and watch_raw else (watch_raw if isinstance(watch_raw, dict) else {})
        except Exception:
            watch = {}
        if isinstance(watch, dict):
            candidates = list(watch.get("candidates") or [])[:5]
            rotation = watch.get("rotation") if isinstance(watch.get("rotation"), dict) else {}
            if candidates:
                watch_detail = " • ".join(f"{x.get('symbol')} {float(x.get('scanner_score',0) or 0):.2f}" for x in candidates)
                overview += self._card("LIVE WATCHLIST", f"{len(list(watch.get('candidates') or []))} scanned • {str(rotation.get('mode','top')).upper()}", watch_detail + (f" • {rotation.get('reason')}" if rotation.get('reason') else ""), GREEN)
        overview += self._card(decision_label, decision, decision_detail)
        overview += self._card(risk_label, risk_text, "; ".join(risk.get("reasons") or [])[:500], risk_color)
        if risk:
            eq = float(risk.get('equity_basis',0) or 0)
            cap_pct = float(risk.get('portfolio_risk_cap_pct',0) or 0)
            cap_cash = float(risk.get('portfolio_risk_cap_usdt',0) or 0) or (eq * cap_pct / 100.0 if eq > 0 else 0.0)
            margin_detail = ""
            if risk.get("account_available_balance_usd") is not None:
                margin_detail = f" • Bybit available {float(risk.get('account_available_balance_usd',0) or 0):.2f} • usable margin budget {float(risk.get('margin_budget_usd',0) or 0):.2f} • selected leverage {float(risk.get('leverage',0) or 0):.2f}x"
                if risk.get("margin_resized"):
                    margin_detail += " • RESIZED TO FIT LIVE CAPACITY"
            overview += self._card("RISK-AT-STOP BUDGET", f"{float(risk.get('portfolio_open_risk_pct',0) or 0):.3f}% open → {float(risk.get('projected_portfolio_risk_pct',0) or 0):.3f}% projected", f"effective max-loss cap {cap_pct:.3f}% / {cap_cash:.2f} USDT • capital exposure cap {float(risk.get('exposure_cap_pct_equity',0) or 0):.0f}% equity • slots {risk.get('max_positions_allowed','—')} • this is loss-at-stop risk, NOT percent of capital deployed" + margin_detail, GREEN if not risk.get('same_symbol_open') else AMBER)
        overview += self._card(execution_label, exec_text, exec_detail)
        if bool(st.get("execution_safety_lock")):
            overview += self._card("EXECUTION SAFETY LOCK", "ACTIVE — NEW ENTRIES BLOCKED", str(st.get("execution_safety_reason", "")), RED)
        err = str(trade.get("last_error") or st.get("last_error") or "")
        if err:
            overview += self._card("ERROR", err, "", RED)
        self._set_preserve_scroll(self.overview, "overview", overview)

        latest_research = st.get("latest_research") or {}
        report = latest_research.get("report") if isinstance(latest_research, dict) and isinstance(latest_research.get("report"), dict) else {}
        chief = report.get("chief_research") if isinstance(report.get("chief_research"), dict) else {}
        approved = list(strategy.get("approved") or [])
        learning_state = trade.get("current_learning_state") if isinstance(trade.get("current_learning_state"), dict) else {}
        if not learning_state:
            learning_state = assessment.get("live_learning_state") if isinstance(assessment.get("live_learning_state"), dict) else {}
        learning_html = style
        learning_html += self._card("RESEARCH", "RUNNING…" if bootstrap else ("BASELINE READY ✓" if str(latest_research.get("status", "")) == "completed" else "WAITING"), str(chief.get("market_regime", latest_research.get("summary", ""))))
        learning_html += self._card("APPROVED STRATEGY SUPPORT", f"{len(approved)} OOS-supported setup(s)", ", ".join(f"{x.get('symbol')} {x.get('interval')}m {x.get('name')}" for x in approved[:5]))
        if learning_state:
            learning_html += self._card("GROWTH STAGE", str(learning_state.get("growth_stage", "learning")).upper(), f"closed Stan trades {learning_state.get('recent_closed_trades',0)} • PF {((learning_state.get('performance_metrics') or {}).get('profit_factor','—'))} • risk {learning_state.get('effective_risk_pct','—')}%")
            learning_html += self._card("PORTFOLIO LEARNING", f"up to {learning_state.get('max_positions_allowed','—')} positions • max loss-at-stop {learning_state.get('portfolio_risk_cap_pct','—')}% / {learning_state.get('portfolio_risk_cap_usdt','—')} USDT", f"capital exposure cap {learning_state.get('exposure_cap_pct','—')}% equity • leverage cap {learning_state.get('leverage_cap','—')}x • max trades/day {learning_state.get('max_trades_today_allowed','—')} • risk % is stop-loss risk, not portfolio allocation", GREEN)
            learning_html += self._card("LEARNING GOVERNOR", "PAUSED" if learning_state.get("pause") else "ACTIVE", "; ".join(learning_state.get("notes") or [])[:900], AMBER if learning_state.get("pause") else GREEN)
        else:
            learning_html += self._card("LIVE LEARNING", "Стартує після перших Stan trades", "Стара історія акаунта не підвищує growth stage автоматично.")
        self._set_preserve_scroll(self.learning, "learning", learning_html)

        automatic = list(opp.get("automatic_trade_alignment") or [])
        manual = list(opp.get("human_action_required") or [])
        tracked = list(opp.get("tracked") or [])
        browser_status = st.get("browser_operator") if isinstance(st.get("browser_operator"), dict) else {}
        browser_actions = list(browser_status.get("actions") or []) if isinstance(browser_status, dict) else []
        caps = opp_os.get("capabilities") if isinstance(opp_os.get("capabilities"), dict) else {}
        gaps = list(opp_os.get("permission_gaps") or [])
        spots = list(opp_os.get("spot_candidates") or [])
        spot_research = opp_os.get("spot_research") if isinstance(opp_os.get("spot_research"), dict) else {}
        events_data = list(opp_os.get("official_events") or [])
        event_summary = opp_os.get("event_summary") if isinstance(opp_os.get("event_summary"), dict) else {}
        earn_items = list(opp_os.get("earn_opportunities") or [])
        spot_decision = opp_os.get("spot_last_decision") if isinstance(opp_os.get("spot_last_decision"), dict) else {}
        active_spot = opp_os.get("spot_active_trade") if isinstance(opp_os.get("spot_active_trade"), dict) else {}

        promo_html = style
        browser_state = str(browser_status.get("state") or ("AVAILABLE" if browser_status.get("available", True) else "UNAVAILABLE")).upper()
        capability_text = (
            f"Futures: {'ACTIVE' if caps.get('futures_trade') else 'NO'} • "
            f"Spot: {'ACTIVE' if caps.get('spot_trade') else 'DISCOVERY ONLY'} • "
            f"Earn: {'permission available' if caps.get('earn') else 'discovery only'} • Browser: {browser_state}"
        )
        promo_html += self._card("OPPORTUNITY OS", "SCANNING…" if opportunity_running else "ACTIVE ✓", capability_text, GREEN if opp_os else AMBER)
        if gaps:
            promo_html += self._card("PERMISSIONS / NEXT CAPABILITY", str(gaps[0].get("permission", "")), str(gaps[0].get("status", "")), AMBER)
        spot_names = ", ".join(f"{x.get('symbol')} {float(x.get('setup_strength',0) or 0):.2f}" for x in spots[:5]) or "No Spot candidates yet"
        spot_value = f"{len(spots)} candidate(s) • OOS support {len(spot_research.get('approved') or [])}"
        promo_html += self._card("SPOT OPPORTUNITY ENGINE", spot_value, spot_names, GREEN if caps.get("spot_trade") else AMBER)
        if spot_decision:
            ass = spot_decision.get("assessment") if isinstance(spot_decision.get("assessment"), dict) else {}
            promo_html += self._card("LAST SPOT DECISION", str(spot_decision.get("execution") or spot_decision.get("action") or "watch").upper(), f"{ass.get('action','')} confidence {float(ass.get('confidence',0) or 0):.0%} • {spot_decision.get('reason','')}")
        if active_spot and str(active_spot.get("state", "")) not in {"", "closed", "cancelled", "error"}:
            promo_html += self._card("ACTIVE STAN SPOT TRADE", f"{active_spot.get('symbol')} • {str(active_spot.get('state')).upper()}", f"notional {float(active_spot.get('notional_usdt',0) or 0):.2f} USDT • risk {float(active_spot.get('actual_risk_pct',0) or 0):.3f}%", GREEN)
        event_detail = " • ".join(f"{k}:{v}" for k,v in event_summary.items() if v) or "No classified official events yet"
        promo_html += self._card("OFFICIAL BYBIT EVENTS / ALPHA / PREDICTION", str(len(events_data)), event_detail)
        earn_detail = ", ".join(f"{x.get('coin')} {float(x.get('estimated_apr_pct',0) or 0):.2f}%" for x in earn_items[:5]) or "No public Earn opportunities found"
        promo_html += self._card("EARN OPPORTUNITY DISCOVERY", f"{len(earn_items)} product(s) • discovery only", earn_detail, AMBER)
        promo_html += self._card("PROMOTION INTELLIGENCE", "SCANNING…" if promo_running else f"{len(automatic)+len(manual)+len(tracked)} campaign item(s)", "Promotions may improve an already valid opportunity; they never create artificial trading volume.")
        counts = promo_lifecycle.get("counts") if isinstance(promo_lifecycle.get("counts"), dict) else {}
        if counts:
            lifecycle_value = " • ".join(f"{k} {v}" for k, v in (("REGISTERED", counts.get("REGISTERED",0)), ("SENT/VERIFY", counts.get("ACTION_SENT_UNVERIFIED",0) + counts.get("IN_PROGRESS",0)), ("RETRY", counts.get("RETRY_WAIT",0)), ("COMPLETED", counts.get("COMPLETED",0) + counts.get("VERIFIED_COMPLETE",0)), ("CLAIMED", counts.get("CLAIMED",0)), ("AUTH", counts.get("AUTH_REQUIRED",0))) if v) or "DISCOVERED"
            recent_life = list(promo_lifecycle.get("items") or [])[:5]
            lifecycle_detail = "; ".join(f"{x.get('state')}: {x.get('campaign')}" for x in recent_life)[:1100]
            promo_html += self._card("PROMO EXECUTION LIFECYCLE", lifecycle_value, lifecycle_detail, GREEN if counts.get("CLAIMED") or counts.get("REGISTERED") else AMBER)
        promo_html += self._card("AUTOMATIC TRADE ALIGNMENT", str(len(automatic)), ", ".join(str(x.get("name")) for x in automatic[:5]))
        promo_html += self._card("OPPORTUNITY ACTION PLAN", str(len(manual)), (", ".join(str(x.get("name")) for x in manual[:5]) or "None") + " • discovery plan; Browser Operator filters/deduplicates safe executable actions")
        action_summary = "; ".join(f"{x.get('campaign')}: {x.get('status')}" for x in browser_actions[:6])
        if bool(st.get("browser_running")):
            browser_value = "WORKING…"
        elif browser_status.get("available") is False:
            browser_value = "UNAVAILABLE"
        elif browser_actions:
            browser_value = f"{browser_state} • {len(browser_actions)} recent action result(s)"
        else:
            browser_value = browser_state
        browser_detail = action_summary or str(browser_status.get("reason", ""))
        if bool(st.get("browser_running")):
            connection = "CDP connected" if browser_status.get("session_connected") else "connecting/idle"
            deadline = str(browser_status.get("watchdog_deadline_at") or "")
            browser_detail = f"current cycle started {browser_status.get('cycle_started_at','now')} • {connection}" + (f" • watchdog {deadline}" if deadline else "") + " • previous persisted results are hidden until this cycle finishes"
        elif not browser_detail and browser_status.get("available"):
            health = browser_status.get("browser_health") if isinstance(browser_status.get("browser_health"), dict) else {}
            mode_detail = health.get("connection_mode") or ("cdp" if browser_status.get("session_connected") else "idle")
            stamp = browser_status.get("cycle_finished_at") or browser_status.get("updated_at") or ""
            browser_detail = f"{browser_status.get('browser','Browser')} • {mode_detail} • last cycle {stamp} • safe Register/Join/Claim/Spin; canonical dedupe; 0 paid action-AI tokens; login/2FA/CAPTCHA remains manual"
        browser_bad = browser_state in {"ERROR", "STALE", "UNAVAILABLE"}
        promo_html += self._card("BYBIT WEB OPERATOR", browser_value, browser_detail, AMBER if browser_bad else GREEN)
        recent_actions = [x for x in list(browser_status.get("actions") or []) if isinstance(x, dict)][:6]
        if recent_actions:
            activity_value = f"{len(recent_actions)} recent action result(s)"
            activity_detail = "; ".join(f"{x.get('campaign','Bybit')}: {x.get('status','track')}" for x in recent_actions)[:1200]
            promo_html += self._card("RECENT PROMO ACTIONS", activity_value, activity_detail, GREEN)
        promo_html += self._card("RULE", "NO ARTIFICIAL VOLUME", "Stan does not wash/matched/self-trade and does not raise risk/leverage merely for reward eligibility.", GREEN)
        self._set_preserve_scroll(self.opportunities, "opportunities", promo_html)

        technical = {
            "prelaunch": pre,
            "trading": trade,
            "strategy_governor": strategy,
            "opportunity_plan": opp,
            "opportunity_os": opp_os,
            "browser_operator": browser_status,
            "promotion_lifecycle": st.get("promotion_lifecycle", {}),
            "reward_audit": st.get("reward_audit", {}),
            "action_summary": st.get("action_summary", {}),
            "current_learning_state": trade.get("current_learning_state", {}),
            "ai_entry_pacing": trade.get("ai_entry_pacing", {}),
            "runtime": runtime,
            "active_background_tasks": st.get("active_background_tasks", 0),
            "execution_environment": {
                "canonical": settings.get("execution_environment"),
                "legacy_execution_testnet": settings.get("execution_testnet"),
                "api_key_environment": pre.get("api_key_environment") or pre.get("environment"),
                "market_data_testnet": settings.get("market_data_testnet"),
            },
            "ai_budget": ai_budget,
            "manual_stop": st.get("manual_stop"),
            "latest_research_status": {k: latest_research.get(k) for k in ("id", "started_at", "finished_at", "status", "summary", "error") if isinstance(latest_research, dict)},
            "recent_core_events": events()[:40],
        }
        self._set_preserve_scroll(self.details, "details", json.dumps(technical, ensure_ascii=False, indent=2, default=str)[:80000], html_mode=False)
