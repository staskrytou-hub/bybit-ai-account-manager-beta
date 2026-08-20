from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from paths import WORKSPACE_DIR

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif'}
_TEXT_ASSET_EXTS = {'.css', '.js', '.json', '.svg', '.html', '.htm'}
_PLACEHOLDER_HINTS = ('placeholder', 'placehold.co', 'placehold.it', 'dummyimage', 'example.com')


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k.lower(): (v or '') for k, v in attrs}
        for attr in ('src', 'href', 'poster'):
            value = data.get(attr, '').strip()
            if value:
                self.refs.append((tag.lower(), attr, value))
        srcset = data.get('srcset', '').strip()
        if srcset:
            for part in srcset.split(','):
                value = part.strip().split(' ')[0]
                if value:
                    self.refs.append((tag.lower(), 'srcset', value))


def _safe_project(project_folder: str) -> tuple[Path, str]:
    rel = (project_folder or '').strip().replace('\\', '/').strip('/')
    if not rel:
        raise ValueError('project_folder is required')
    root = WORKSPACE_DIR.resolve()
    project = (WORKSPACE_DIR / rel).resolve()
    if project == root or root not in project.parents:
        raise ValueError('Project must stay inside Bybit AI Manager Workspace.')
    if not project.exists() or not project.is_dir():
        raise FileNotFoundError(rel)
    return project, rel


def _is_external(ref: str) -> bool:
    lower = ref.lower().strip()
    return lower.startswith(('http://', 'https://', '//', 'data:', 'mailto:', 'tel:', 'javascript:', '#'))


def _clean_local_ref(ref: str) -> str:
    parsed = urlsplit(ref)
    return unquote(parsed.path).replace('\\', '/')


def _image_signature_ok(path: Path) -> bool:
    try:
        data = path.read_bytes()[:16]
    except OSError:
        return False
    ext = path.suffix.lower()
    if ext == '.png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if ext in {'.jpg', '.jpeg'}:
        return data.startswith(b'\xff\xd8')
    if ext == '.webp':
        return len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    if ext == '.gif':
        return data.startswith((b'GIF87a', b'GIF89a'))
    if ext == '.avif':
        return b'ftyp' in data[:12] and b'avif' in data[:16]
    return True


def audit_static_site(project_folder: str, entry_file: str = 'index.html', require_local_images: bool = False) -> dict[str, object]:
    project, rel = _safe_project(project_folder)
    entry = (project / entry_file).resolve()
    if project not in entry.parents and entry != project:
        raise ValueError('entry_file must stay inside the project folder')

    html_files = sorted([p for p in project.rglob('*') if p.is_file() and p.suffix.lower() in {'.html', '.htm'}])
    all_files = sorted([p for p in project.rglob('*') if p.is_file()])
    local_images = [p for p in all_files if p.suffix.lower() in _IMAGE_EXTS]
    image_details: list[dict[str, object]] = []
    for image in local_images:
        image_details.append({
            'path': image.relative_to(project).as_posix(),
            'size_bytes': image.stat().st_size,
            'signature_ok': _image_signature_ok(image),
        })

    local_refs: list[dict[str, object]] = []
    missing_refs: list[dict[str, str]] = []
    external_refs: list[dict[str, str]] = []
    placeholder_refs: list[str] = []
    referenced_local_images: set[str] = set()

    for html_path in html_files:
        try:
            text = html_path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        parser = _AssetParser()
        try:
            parser.feed(text)
        except Exception:
            pass
        for tag, attr, ref in parser.refs:
            source = html_path.relative_to(project).as_posix()
            if any(hint in ref.lower() for hint in _PLACEHOLDER_HINTS):
                placeholder_refs.append(ref)
            if _is_external(ref):
                external_refs.append({'source': source, 'tag': tag, 'attr': attr, 'ref': ref})
                continue
            clean = _clean_local_ref(ref).lstrip('/')
            if not clean:
                continue
            resolved = (html_path.parent / clean).resolve()
            if project not in resolved.parents and resolved != project:
                missing_refs.append({'source': source, 'ref': ref, 'reason': 'escapes project folder'})
                continue
            exists = resolved.exists() and resolved.is_file()
            local_refs.append({'source': source, 'tag': tag, 'attr': attr, 'ref': ref, 'exists': exists})
            if not exists:
                missing_refs.append({'source': source, 'ref': ref, 'reason': 'file not found'})
            elif resolved.suffix.lower() in _IMAGE_EXTS:
                referenced_local_images.add(resolved.relative_to(project).as_posix())

        # CSS url(...) references in inline/style blocks and linked CSS are checked separately below.

    css_files = [p for p in all_files if p.suffix.lower() == '.css']
    css_url_pattern = re.compile(r'url\(\s*[\"\']?([^\"\')]+)', re.IGNORECASE)
    for css_path in css_files:
        try:
            css = css_path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        for ref in css_url_pattern.findall(css):
            ref = ref.strip()
            source = css_path.relative_to(project).as_posix()
            if _is_external(ref):
                external_refs.append({'source': source, 'tag': 'css', 'attr': 'url', 'ref': ref})
                continue
            clean = _clean_local_ref(ref).lstrip('/')
            if not clean:
                continue
            resolved = (css_path.parent / clean).resolve()
            exists = project in resolved.parents and resolved.exists() and resolved.is_file()
            local_refs.append({'source': source, 'tag': 'css', 'attr': 'url', 'ref': ref, 'exists': exists})
            if not exists:
                missing_refs.append({'source': source, 'ref': ref, 'reason': 'file not found'})
            elif resolved.suffix.lower() in _IMAGE_EXTS:
                referenced_local_images.add(resolved.relative_to(project).as_posix())

    invalid_images = [item for item in image_details if int(item['size_bytes']) < 512 or not bool(item['signature_ok'])]
    entry_exists = entry.exists() and entry.is_file()
    image_requirement_ok = (not require_local_images) or bool(local_images and referenced_local_images)
    passed = bool(entry_exists and not missing_refs and not invalid_images and not placeholder_refs and image_requirement_ok)

    return {
        'passed': passed,
        'project_folder': rel,
        'entry_file': entry_file,
        'entry_exists': entry_exists,
        'html_file_count': len(html_files),
        'file_count': len(all_files),
        'local_image_count': len(local_images),
        'referenced_local_image_count': len(referenced_local_images),
        'referenced_local_images': sorted(referenced_local_images),
        'missing_references': missing_refs[:100],
        'invalid_images': invalid_images[:100],
        'placeholder_references': placeholder_refs[:100],
        'external_reference_count': len(external_refs),
        'require_local_images': bool(require_local_images),
    }


def audit_static_site_json(project_folder: str, entry_file: str = 'index.html', require_local_images: bool = False) -> str:
    return json.dumps(audit_static_site(project_folder, entry_file, require_local_images), ensure_ascii=False, indent=2)
