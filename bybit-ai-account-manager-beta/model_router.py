from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from journal import log_event
from settings import load_settings

ModelTier = Literal["luna", "terra", "sol"]


@dataclass(frozen=True)
class ModelSelection:
    tier: ModelTier
    model: str
    reason: str


MODEL_IDS: dict[ModelTier, str] = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
}

# Last-resort compatibility fallbacks. They are used only when the preferred
# model is unavailable for the current account/project, not for ordinary 429s.
MODEL_FALLBACKS: dict[str, list[str]] = {
    "gpt-5.6-sol": ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5-nano"],
    "gpt-5.6-terra": ["gpt-5.6-luna", "gpt-5.4-mini", "gpt-5-nano"],
    "gpt-5.6-luna": ["gpt-5.4-mini", "gpt-5-nano"],
}

_COMPLEX_BUILD = re.compile(
    r"\b(створ|зроб|побуд|розроб|перероб|дороб|виправ|рефактор|сайт|додат|app\b|website|project|проєкт|"
    r"інтеграц|автоматиз|дослід|проаналізуй.*і|build|implement|debug|migrate)\w*",
    re.IGNORECASE,
)
_CODE = re.compile(r"\b(code|код|python|javascript|typescript|html|css|api|sql|bug|помилк|debug|backend|frontend)\b", re.IGNORECASE)
_RESEARCH = re.compile(r"\b(актуаль|сьогодні|знайди|порівняй|дослід|research|latest|current|ринок|ваканс)\w*", re.IGNORECASE)
_SIMPLE = re.compile(r"\b(переклад|translate|що таке|поясни|коротко|скільки буде|define|summarize)\b", re.IGNORECASE)


def choose_model(request: str, *, autonomous: bool = False, role: str = "worker") -> ModelSelection:
    """Choose a capability tier without spending an extra model call.

    The router is intentionally deterministic so model selection itself never
    consumes tokens or becomes another point of failure.
    """
    text = (request or "").strip()
    cfg = load_settings()
    profile = str(cfg.get("model_profile", "balanced")).lower()

    if profile == "economy":
        base: ModelTier = "luna" if not autonomous else "terra"
    elif profile == "quality":
        base = "terra" if not autonomous else "sol"
    else:
        base = "terra" if autonomous else "luna"

    score = 0
    if len(text) > 1400:
        score += 1
    if len(text) > 5000:
        score += 1
    if _COMPLEX_BUILD.search(text):
        score += 2
    if _CODE.search(text):
        score += 1
    if _RESEARCH.search(text):
        score += 1
    if any(ch.isdigit() for ch in text) and len(re.findall(r"\d+", text)) >= 3:
        score += 1
    if autonomous:
        score += 1
    if role in {"planner", "judge"}:
        score += 1

    if profile == "quality":
        score += 1
    if profile == "economy":
        score -= 1
    if _SIMPLE.search(text) and len(text) < 700 and not autonomous:
        score -= 1

    if score >= 5:
        tier: ModelTier = "sol"
    elif score >= 2:
        tier = "terra"
    else:
        tier = "luna"

    # Never downgrade below the profile's base for autonomous work.
    order: list[ModelTier] = ["luna", "terra", "sol"]
    if autonomous and order.index(tier) < order.index(base):
        tier = base

    selection = ModelSelection(tier=tier, model=MODEL_IDS[tier], reason=f"profile={profile}; score={score}; role={role}")
    log_event("model.route", {"tier": selection.tier, "model": selection.model, "reason": selection.reason, "request": text[:500]})
    return selection


def fallback_models(preferred: str) -> list[str]:
    return [preferred] + [m for m in MODEL_FALLBACKS.get(preferred, []) if m != preferred]
