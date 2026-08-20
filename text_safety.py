from __future__ import annotations


def looks_like_binary_text(value: str) -> bool:
    text = str(value or '')
    if not text:
        return False
    sample = text[:12000]
    if '\x00' in sample:
        return True
    replacement = sample.count('\ufffd')
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in '\n\r\t')
    printable = sum(1 for ch in sample if ch.isprintable() or ch in '\n\r\t')
    ratio = printable / max(1, len(sample))
    if replacement >= 12 or replacement / max(1, len(sample)) > 0.015:
        return True
    if control >= 6 or ratio < 0.82:
        return True
    # Common binary signatures accidentally decoded into text.
    stripped = sample.lstrip()
    if stripped.startswith(('RIFF', '\x89PNG', 'GIF87a', 'GIF89a')) and len(sample) > 300:
        return True
    return False


def sanitize_text(value: str, *, context: bool = False) -> str:
    text = str(value or '')
    if not looks_like_binary_text(text):
        return text
    if context:
        return '[Binary artifact payload omitted from conversation context. Use Workspace/Artifacts tools instead.]'
    return '[Binary file content hidden. Open the real file from Workspace / Artifacts instead of rendering its bytes in chat.]'
