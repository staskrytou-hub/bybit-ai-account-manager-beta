from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from urllib import error, request

from artifacts import register_artifact
from journal import log_event
from paths import WORKSPACE_DIR
from settings import load_settings

_ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
_ALLOWED_QUALITY = {"low", "medium", "high", "auto"}
_ALLOWED_FORMATS = {"png", "jpeg", "webp"}


def _safe_image_path(filename: str, output_format: str) -> tuple[Path, str]:
    name = (filename or "").strip().replace("\\", "/")
    if not name:
        name = f"generated/image-{int(time.time())}.{output_format}"
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError("Image filename must be relative to Bybit AI Manager Workspace.")
    path = (WORKSPACE_DIR / name).resolve()
    root = WORKSPACE_DIR.resolve()
    if root not in path.parents:
        raise ValueError("Image path must stay inside Bybit AI Manager Workspace.")
    if path.suffix.lower().lstrip(".") not in _ALLOWED_FORMATS:
        path = path.with_suffix("." + output_format)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.relative_to(WORKSPACE_DIR).as_posix()


def _retry_after_seconds(headers: object, attempt: int) -> float:
    try:
        value = headers.get("Retry-After")  # type: ignore[attr-defined]
        if value:
            return min(30.0, max(0.75, float(value) + 0.35))
    except Exception:
        pass
    return min(30.0, 1.5 * (2 ** max(0, attempt - 1)))


def generate_image_to_workspace(
    prompt: str,
    filename: str,
    *,
    size: str = "1536x1024",
    quality: str = "medium",
    output_format: str = "png",
) -> str:
    """Generate a real image with GPT Image 2 and save it inside Workspace."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Image prompt is required.")
    size = size if size in _ALLOWED_SIZES else "1536x1024"
    cfg = load_settings()
    if quality == "auto":
        quality = str(cfg.get("image_quality", "medium"))
    quality = quality if quality in _ALLOWED_QUALITY else "medium"
    output_format = output_format if output_format in _ALLOWED_FORMATS else "png"
    path, relative = _safe_image_path(filename, output_format)

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    body = json.dumps(
        {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "n": 1,
        }
    ).encode("utf-8")
    retries = max(0, int(cfg.get("api_rate_limit_retries", 5)))

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = request.Request(
            "https://api.openai.com/v1/images/generations",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "BybitAIAccountManager/1.0",
            },
        )
        try:
            with request.urlopen(req, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data") or []
            if not data:
                raise RuntimeError("Image API returned no image data.")
            first = data[0]
            if first.get("b64_json"):
                image_bytes = base64.b64decode(first["b64_json"])
            elif first.get("url"):
                with request.urlopen(str(first["url"]), timeout=120) as img_response:
                    image_bytes = img_response.read()
            else:
                raise RuntimeError("Image API response did not contain b64_json or url.")
            if len(image_bytes) < 100:
                raise RuntimeError("Generated image payload was unexpectedly small.")
            path.write_bytes(image_bytes)
            register_artifact(relative, kind="image", source="gpt-image-2", description=prompt[:500])
            log_event("tool.image.generate", {"filename": relative, "size": size, "quality": quality, "bytes": len(image_bytes)})
            return relative
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1500]
            except Exception:
                detail = str(exc)
            last_error = RuntimeError(f"Image API HTTP {exc.code}: {detail}")
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise last_error
            wait = _retry_after_seconds(exc.headers, attempt + 1)
            log_event("image.retry", {"attempt": attempt + 1, "wait_seconds": wait, "status": exc.code})
            time.sleep(wait)
        except (error.URLError, TimeoutError) as exc:
            last_error = RuntimeError(f"Image API network error: {exc}")
            if attempt >= retries:
                raise last_error
            wait = min(20.0, 1.5 * (2 ** attempt))
            log_event("image.retry", {"attempt": attempt + 1, "wait_seconds": wait, "status": "network"})
            time.sleep(wait)

    raise last_error or RuntimeError("Image generation failed.")
