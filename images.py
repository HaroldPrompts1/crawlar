"""Clasificación de imágenes por comparación cross-page (sin descargar).

Misma imagen en muchas páginas → rol chrome (plantilla: se excluye del
cuerpo pero se conserva una vez como identidad). En pocas → content.
Píxeles de seguimiento → tracking (se excluye de todo).
"""
import json
import re
from collections import Counter
from urllib.parse import urlparse, urlunparse

TRACKING_HINTS = ("facebook.com/tr", "google-analytics", "googletagmanager",
                  "doubleclick", "hotjar", "beacon", "pixel")


def norm_src(src):
    """Normaliza para comparar: minúsculas + sin query (cache-busters)."""
    try:
        p = urlparse(src)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))
    except Exception:
        return src


def _seq_group(name):
    """bg1/bg2, slide-01/slide-02 → misma serie (galería/carrusel)."""
    m = re.sub(r"\d+", "#", name.rsplit("/", 1)[-1].lower())
    return m if "#" in m else None


def classify(client, ratio=0.6, min_docs=3):
    """Clasifica y guarda roles en site_assets + documents.images. Devuelve conteo."""
    import db
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT d.url, d.images FROM documents d
               JOIN pages p ON p.url=d.url WHERE p.client=?""", (client,)).fetchall()
        if len(rows) < min_docs:
            return {"chrome": 0, "content": 0, "tracking": 0}
        per_doc, freq, meta = {}, Counter(), {}
        for r in rows:
            try:
                imgs = json.loads(r["images"] or "[]")
            except (json.JSONDecodeError, TypeError):
                imgs = []
            seen = set()
            normed = []
            for im in imgs:
                src = im if isinstance(im, str) else im.get("src", "")
                if not src:
                    continue
                n = norm_src(src)
                seen.add(n)
                base = im if isinstance(im, dict) else {"src": src}
                meta.setdefault(n, {"src_seen": src, "alt": base.get("alt", "")})
                if base.get("alt") and not meta[n]["alt"]:
                    meta[n]["alt"] = base["alt"]
                normed.append((n, base))
            per_doc[r["url"]] = normed
            freq.update(seen)
        need = max(2, int(len(rows) * ratio))
        roles = {}
        for n, c in freq.items():
            low = n.lower()
            if any(h in low for h in TRACKING_HINTS):
                roles[n] = "tracking"
            elif c >= need:
                roles[n] = "chrome"
            else:
                roles[n] = "content"
        counts = Counter()
        for url, normed in per_doc.items():
            enriched = []
            for n, base in normed:
                role = roles.get(n, "content")
                counts[role] += 1
                if isinstance(base, dict):
                    base = dict(base)
                    base["role"] = role
                    enriched.append(base)
                else:
                    enriched.append({"src": base, "role": role})
            con.execute("UPDATE documents SET images=? WHERE url=?",
                        (json.dumps(enriched), url))
            if sum(counts.values()) % 50 == 0:
                db.commit(con)
        for n, c in freq.items():
            m = meta.get(n, {})
            cap = m.get("alt", "") or ""
            con.execute(
                """INSERT INTO site_assets(client, src_norm, src_seen, alt,
                       pages_count, role, caption)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(client, src_norm) DO UPDATE SET
                     src_seen=excluded.src_seen, alt=excluded.alt,
                     pages_count=excluded.pages_count, role=excluded.role,
                     caption=excluded.caption""",
                (client, n, m.get("src_seen", n), m.get("alt", ""),
                 c, roles.get(n, "content"), cap))
        db.commit(con)
        # identidad del sitio = chrome con alt de marca (logo) primero
        ident = con.execute(
            """SELECT src_seen, alt FROM site_assets
               WHERE client=? AND role='chrome' ORDER BY pages_count DESC LIMIT 3""",
            (client,)).fetchall()
        _apply_identity(con, client, per_doc,
                        [{"src": r[0], "alt": r[1]} for r in ident])
        db.commit(con)
        return dict(counts)
    finally:
        con.close()


def _apply_identity(con, client, per_doc, identity):
    """Regenera réplicas: chrome solo en cabecera de identidad."""
    import replica
    if not identity:
        return
    for url in per_doc:
        d = con.execute(
            "SELECT title, text, headings, images, structured FROM documents WHERE url=?",
            (url,)).fetchone()
        if not d:
            continue
        replica.build_page(url, {
            "title": d["title"], "text": d["text"],
            "headings": json.loads(d["headings"] or "[]"),
            "links": [],
            "images": json.loads(d["images"] or "[]"),
            "structured": json.loads(d["structured"] or "[]"),
        }, client, identity=[{**a, "local": None} for a in identity])
