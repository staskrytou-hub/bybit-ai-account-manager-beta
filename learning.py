from __future__ import annotations

import re

from journal import log_event
from memory import save_memory

_CORRECTION_HINT = re.compile(
    r"(я просив|я написав|не так|не те|не роби|ти не|ти забув|ти пропустив|маєш|повинен|"
    r"помилка|виправ|не вигад|конкретн|буквальн|instead|i asked|you forgot|wrong|fix this)",
    re.IGNORECASE,
)


def capture_user_correction(text: str) -> int | None:
    """Persist explicit user corrections as reusable lessons.

    This is deliberately conservative: ordinary requests are not treated as training data.
    """
    clean = " ".join((text or "").strip().split())
    if len(clean) < 12 or not _CORRECTION_HINT.search(clean):
        return None
    lesson = clean[:1600]
    memory_id = save_memory(
        "User correction / execution rule",
        lesson,
        category="lesson",
        importance=5,
    )
    log_event("learning.correction_saved", {"memory_id": memory_id, "text": lesson[:500]})
    return memory_id
