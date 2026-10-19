"""Guarda crudo auditable y genera la réplica simplificada propia."""
import hashlib
import logging
import os
import re
import urllib.request
from html import escape
from urllib.parse import urlparse

from config import RAW_DIR, REPLICA_DIR

log = logging.getLogger(__name__)

PAGE_TPL = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;
padding:0 1rem;line-height:1.6;color:#222}}h1{{font-size:1.6rem}}h2{{font-size:1.2rem;
color:#444}}img{{max-width:100%}}.src{{font-size:.8rem;color:#888}}
.ficha{{background:#f4f4f4;border:1px solid #ddd;padding:.8rem;margin:1rem 0}}</style>
</head><body>
<p class="src">Réplica simplificada de: {url}</p>
<h1>{title}</h1>
{ficha}
{body}
<hr><p class="src">Generada por el prototipo local con permiso del cliente.
Diseño propio; contenido extraído de la web original.</p>
</body></html>"""


def save_raw(url, content_type, body):
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    ext = ".bin"
    ct = (content_type or "").lower()
    if "html" in ct:
        ext = ".html"
    elif "pdf" in ct:
        ext = ".pdf"
    path = os.path.join(RAW_DIR, h + ext)
    with open(path, "wb") as f:
        f.write(body)
    return path


def _slug(url):
    p = urlparse(url)
    base = (p.netloc + p.path).strip("/")
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "index"
    return base[:80]


def _flat(prefix, value, out, depth=0):
    """Aplana cualquier JSON-LD a filas (clave, valor): sin keys hardcodeadas."""
    if depth > 3 or len(out) >= 20:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if k.startswith("@"):
                continue
            _flat(f"{prefix}{k}." if prefix else f"{k}.", v, out, depth + 1)
    elif isinstance(value, list):
        parts = []
        for v in value[:5]:
            if isinstance(v, (dict, list)):
                _flat(prefix, v, out, depth + 1)
            else:
                parts.append(str(v))
        if parts:
            out.append((prefix.rstrip("."), ", ".join(parts)))
    elif value is None or value == "":
        return
    else:
        out.append((prefix.rstrip("."), str(value)[:200]))


def _ficha(structured):
    """Caja de datos estructurados: funciona con cualquier @type."""
    rows = []
    for s in structured or []:
        if not isinstance(s, dict):
            continue
        t = s.get("type", "?")
        rows.append(f'<tr><td colspan="2"><b>{escape(str(t))}</b></td></tr>')
        pairs = []
        _flat("", s.get("data", {}), pairs)
        for k, v in pairs[:15]:
            rows.append(f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>")
    if not rows:
        return ""
    return f'<div class="ficha"><table>{"".join(rows)}</table></div>'


def _local_image(img_url, client, slug):
    try:
        from urllib.parse import quote as _quote
        d = os.path.join(REPLICA_DIR, client, "assets", slug)
        os.makedirs(d, exist_ok=True)
        p = urlparse(img_url)
        # Las subidas de WordPress a veces traen ¿?ñ en el nombre: sin
        # percent-encoding, http.client falla con UnicodeEncodeError.
        safe_url = p._replace(path=_quote(p.path, safe="/%"),
                              query=_quote(p.query, safe="=&%")).geturl()
        name = os.path.basename(p.path) or "img"
        dest = os.path.join(d, name[:80])
        if os.path.exists(dest):
            return f"assets/{slug}/{name[:80]}"
        req = urllib.request.Request(safe_url, headers={"User-Agent": "PrototipoCrawler/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(3 * 1024 * 1024)
        with open(dest, "wb") as f:
            f.write(data)
        return f"assets/{slug}/{name[:80]}"
    except Exception:
        log.warning("no se pudo espejar imagen %s", img_url, exc_info=True)
        return None


def build_page(url, doc, client=None, identity=None):
    """Réplica propia: texto extraído + imágenes locales, sin CSS/JS original.

    identity: lista de assets rol=chrome (logo/identidad) para la cabecera.
    El cuerpo usa solo imágenes rol=content; tracking nunca se incluye.
    """
    from urllib.parse import urlparse as _up
    client = client or _up(url).netloc.lower()
    slug = _slug(url)
    parts = []
    if identity:
        logos = "".join(
            f'<img src="{escape(a.get("local") or a.get("src", ""))}" '
            f'alt="{escape(a.get("alt", "logo"))}" style="max-height:60px">'
            for a in identity[:2] if (a.get("local") or a.get("src")))
        if logos:
            parts.append(f'<div class="identity">{logos}</div>')
    for h in doc["headings"][:10]:
        parts.append(f"<h2>{escape(h)}</h2>")
    for para in doc["text"].split(". ")[:40]:
        para = para.strip()
        if len(para) > 40:
            parts.append(f"<p>{escape(para)}.</p>")
    for img in doc["images"][:8]:
        if isinstance(img, dict):
            if img.get("role", "content") != "content":
                continue
            src, alt = img.get("src"), img.get("alt") or ""
        else:
            src, alt = img, ""
        local = _local_image(src, client, slug)
        if local:
            cap = f"<figcaption>{escape(alt)}</figcaption>" if alt else ""
            parts.append(f'<figure><img src="{escape(local)}" loading="lazy" '
                         f'alt="{escape(alt)}">{cap}</figure>')
    html = PAGE_TPL.format(title=escape(doc["title"] or slug),
                           url=escape(url),
                           ficha=_ficha(doc.get("structured")),
                           body="\n".join(parts))
    dest = os.path.join(REPLICA_DIR, client, slug + ".html")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(html)
    return dest


def delete_page(url, client=None, raw_path=None):
    """Borra réplica + assets locales + crudo de una URL. Nunca lanza."""
    import shutil
    from urllib.parse import urlparse as _up
    client = client or _up(url).netloc.lower()
    slug = _slug(url)
    removed = []
    for path in (os.path.join(REPLICA_DIR, client, slug + ".html"),
                 os.path.join(REPLICA_DIR, client, "assets", slug)):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(path)
            elif os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except Exception:
            log.warning("no se pudo borrar %s", path, exc_info=True)
    if raw_path:
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
                removed.append(raw_path)
        except Exception:
            log.warning("no se pudo borrar crudo %s", raw_path, exc_info=True)
    return removed


def delete_client_files(client, raw_paths=()):
    """Borra el directorio de réplicas del cliente + crudos. Nunca lanza."""
    import shutil
    removed = []
    cdir = os.path.join(REPLICA_DIR, client)
    try:
        if os.path.isdir(cdir):
            shutil.rmtree(cdir)
            removed.append(cdir)
    except Exception:
        log.warning("no se pudo borrar réplicas de %s", client, exc_info=True)
    for rp in raw_paths or ():
        try:
            if rp and os.path.exists(rp):
                os.remove(rp)
                removed.append(rp)
        except Exception:
            log.warning("no se pudo borrar crudo %s", rp, exc_info=True)
    return removed
