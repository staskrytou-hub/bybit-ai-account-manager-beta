from __future__ import annotations

"""Lightweight portfolio UI translation layer.

The beta evolved through several local-language iterations. The portfolio build
keeps the underlying behaviour intact and normalizes only visible Qt text to
English at runtime. No trading, risk, research, or execution logic is changed.
"""

import re


def _translate(value: str) -> str:
    if not value or not re.search(r"[А-Яа-яІіЇїЄєҐґ]", value):
        return value

    exact = {
        "Підключаюся до Stan Core…": "Connecting to the core…",
        "Підключити Bybit": "Connect Bybit",
        "Bybit підключено ✓": "Bybit connected ✓",
        "тільки START STAN може запустити систему знову": "only START can run the system again",
        "Стартує після перших Stan trades": "Starts after the first managed trades",
        "Стара історія акаунта не підвищує growth stage автоматично.": "Existing account history does not automatically increase the growth stage.",
        "Новий чат. Напишіть задачу — історія збережеться локально.": "New chat. Describe a task — history is stored locally.",
    }
    if value in exact:
        return exact[value]

    replacements = [
        ("Одна кнопка запускає весь цикл: перевірка Bybit → професійний bootstrap → ринок/новини/стратегії → Risk Engine → Futures/Spot → журнал → навчання → Promotions/Rewards/Alpha/Earn discovery. Технічні параметри Stan веде сам.",
         "One button starts the full workflow: Bybit verification → professional bootstrap → market/news/strategy research → Risk Engine → Futures/Spot → journal → learning → Promotions/Rewards/Alpha/Earn discovery. Technical parameters are managed automatically."),
        ("Після першого успішного підключення API тобі не треба вручну обирати монету, RSI, стратегію чи плече. Stan сам сканує, досліджує й масштабує роботу тільки за підтвердженою статистикою; hard-risk межі модель обійти не може.",
         "After the first successful API connection, there is no need to manually select a coin, RSI setting, strategy, or leverage. The system scans, researches, and scales only on validated evidence; deterministic hard-risk limits cannot be bypassed by the model."),
        ("Стара API-сесія містить несумісний reasoning-контекст. Stan v2 не повинен повторно використовувати його; створіть новий чат, якщо ця помилка повториться після оновлення.",
         "The previous API session contains incompatible reasoning context. The app does not reuse it; create a new chat if the error persists after refresh."),
        ("OpenAI тимчасово обмежив швидкість запитів. Stan уже має retry-механізм, але цей запуск вичерпав повтори. Спробуйте ще раз через кілька секунд або зменште розмір автономної задачі.",
         "OpenAI temporarily rate-limited requests. The retry budget for this run was exhausted. Try again in a few seconds or reduce the autonomous task size."),
        ("Stan не зміг завершити цей запуск. Технічні деталі нижче.",
         "The app could not complete this run. Technical details are shown below."),
        ("Stan запущено", "Autopilot started"),
        ("Stan зупинено", "Autopilot stopped"),
        ("Аналіз завершено. Результат оновлено в Overview; технічні деталі є в останній вкладці.",
         "Analysis completed. Results are updated in Overview; technical details are available in the final tab."),
        ("Відкрито окремий браузерний профіль Stan. Увійди в Bybit / пройди 2FA нормально. Вікно МОЖНА залишити відкритим — v4.5.8 підключається до цієї самої сесії й більше не запускає другий Chrome поверх заблокованого профілю.",
         "A dedicated browser profile was opened. Sign in to Bybit and complete 2FA normally. The window may remain open; the app reuses the same authorized session."),
        ("Не вдалося відкрити браузер.", "Could not open the browser."),
        ("Додай API один раз — далі START STAN робить усе сам.", "Add the API once; after that START runs the workflow automatically."),
        ("Професійний bootstrap", "Professional bootstrap"),
        ("професійний bootstrap", "professional bootstrap"),
        ("ринок/новини/стратегії", "market/news/strategies"),
        ("журнал", "journal"),
        ("навчання", "learning"),
        ("Підключено", "Connected"),
        ("Не підключено", "Not connected"),
        ("підключено", "connected"),
        ("не підключено", "not connected"),
        ("запущено", "started"),
        ("зупинено", "stopped"),
        ("працює", "running"),
        ("у фоні", "in the background"),
        ("не вдалося", "failed"),
    ]
    text = value
    for source, target in replacements:
        text = text.replace(source, target)
    return text


def install_english_dialog_layer() -> None:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import (
            QAbstractButton,
            QComboBox,
            QDialog,
            QLabel,
            QLineEdit,
            QMainWindow,
            QTabWidget,
            QTextBrowser,
            QWidget,
        )
    except Exception:
        return

    if getattr(QDialog, "_portfolio_english_layer", False):
        return

    original_exec = QDialog.exec
    original_show = QMainWindow.show
    original_set_html = QTextBrowser.setHtml
    original_set_plain_text = QTextBrowser.setPlainText

    def translate_tree(root: QWidget) -> None:
        try:
            title = root.windowTitle()
            translated_title = _translate(title)
            if translated_title != title:
                root.setWindowTitle(translated_title)
        except Exception:
            pass

        for label in root.findChildren(QLabel):
            try:
                current = label.text()
                translated = _translate(current)
                if translated != current:
                    label.setText(translated)
            except Exception:
                pass

        for button in root.findChildren(QAbstractButton):
            try:
                current = button.text()
                translated = _translate(current)
                if translated != current:
                    button.setText(translated)
            except Exception:
                pass

        for field in root.findChildren(QLineEdit):
            try:
                current = field.placeholderText()
                translated = _translate(current)
                if translated != current:
                    field.setPlaceholderText(translated)
            except Exception:
                pass

        for tabs in root.findChildren(QTabWidget):
            try:
                for index in range(tabs.count()):
                    current = tabs.tabText(index)
                    translated = _translate(current)
                    if translated != current:
                        tabs.setTabText(index, translated)
            except Exception:
                pass

        for combo in root.findChildren(QComboBox):
            try:
                for index in range(combo.count()):
                    current = combo.itemText(index)
                    translated = _translate(current)
                    if translated != current:
                        combo.setItemText(index, translated)
            except Exception:
                pass

    def install_refresh_timer(root: QWidget) -> QTimer:
        translate_tree(root)
        timer = QTimer(root)
        timer.setInterval(300)
        timer.timeout.connect(lambda: translate_tree(root))
        timer.start()
        return timer

    def english_exec(self: QDialog) -> int:
        timer = install_refresh_timer(self)
        try:
            return original_exec(self)
        finally:
            timer.stop()

    def english_show(self: QMainWindow) -> None:
        if not hasattr(self, "_portfolio_english_timer"):
            self._portfolio_english_timer = install_refresh_timer(self)
        return original_show(self)

    def english_set_html(self: QTextBrowser, text: str) -> None:
        return original_set_html(self, _translate(text))

    def english_set_plain_text(self: QTextBrowser, text: str) -> None:
        return original_set_plain_text(self, _translate(text))

    try:
        QDialog.exec = english_exec
        QMainWindow.show = english_show
        QTextBrowser.setHtml = english_set_html
        QTextBrowser.setPlainText = english_set_plain_text
        QDialog._portfolio_english_layer = True
    except Exception:
        return
