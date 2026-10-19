"""Parsers por Content-Type. Todos devuelven el mismo documento normalizado.

Documento: {title, text, headings[], links[], images[], kind,
            canonical|None, structured[]}
"""
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import trafilatura
from bs4 import BeautifulSoup


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _merge_texts(main, full):
    """Une trafilatura (preciso) con bs4 (completo): agrega lo que falte."""
    base = _clean(main)
    soup_text = _clean(full)
    if not soup_text or len(soup_text) <= len(base) * 1.1:
        return base
    extra = [s.strip() for s in re.split(r"(?<=[.!?])\s+", soup_text)
             if len(s.strip()) > 40 and s.strip() not in base]
    return base + (" " + " ".join(extra) if extra else "")


def parse_html(body, url):
    html = body.decode("utf-8", errors="replace")
    # 1) trafilatura como base precisa + fusión con bs4 para no perder nada
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    fallback = soup.get_text(" ")
    main = trafilatura.extract(html, include_links=False,
                                 include_tables=True) or fallback
    text = _merge_texts(main, fallback)
    # links/imágenes/headings desde el DOM intacto (nav/footer incluidos)
    full = BeautifulSoup(html, "html.parser")
    title = _clean(full.title.string) if full.title and full.title.string else ""
    if not title:
        h1 = full.find("h1")
        title = _clean(h1.get_text()) if h1 else ""
    headings = [_clean(h.get_text()) for h in full.find_all(["h1", "h2", "h3"])][:30]
    links = []
    for a in full.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_url = urljoin(url, href)
        if not abs_url.startswith(("http://", "https://")):
            continue
        rel = " ".join(a.get("rel", [])).lower() if a.get("rel") else ""
        zone = "main"
        for anc in a.parents:
            if getattr(anc, "name", None) in ("header", "footer", "nav"):
                zone = "chrome"
                break
        anchor = _clean(a.get_text(" "))[:150]
        links.append({"url": abs_url, "anchor": anchor, "rel": rel, "zone": zone})
        if len(links) >= 300:
            break
    images = []
    for i in full.find_all("img", src=True):
        src = (i["src"] or "").strip()
        if not src or src.startswith("data:"):
            continue
        abs_src = urljoin(url, src)
        if not abs_src.startswith(("http://", "https://")):
            continue
        parent_a = i.find_parent("a", href=True)
        zone = "main"
        for anc in i.parents:
            if getattr(anc, "name", None) in ("header", "footer", "nav"):
                zone = "chrome"
                break
        images.append({
            "src": abs_src,
            "alt": (i.get("alt") or "").strip()[:200],
            "w": i.get("width"), "h": i.get("height"),
            "link": urljoin(url, parent_a["href"]) if parent_a else None,
            "zone": zone,
        })
        if len(images) >= 60:
            break
    canon = full.find("link", rel="canonical")
    canonical = (canon["href"].strip() if canon and canon.get("href") else None)
    if canonical and not canonical.startswith("http"):
        canonical = urljoin(url, canonical)
    structured = _json_ld(full)
    for s in structured:
        s["format"] = "json-ld"
    structured += _microdata_items(full) + _rdfa_items(full)
    structured += _table_records(full)
    structured = structured[:15]
    # fotos escondidas en JSON-LD (galerías, offers) → imágenes content
    ld_photos = []
    for s in structured:
        _walk_json_images(s.get("data", {}), ld_photos)
    images = [{"src": u, "alt": "", "w": None, "h": None,
               "link": None, "zone": "main", "from": "json-ld"}
              for u in dict.fromkeys(ld_photos)][:40] + images
    # OG como respaldo de título/descripción/imagen principal
    og = _og_meta(full)
    if not title and og.get("title"):
        title = og["title"]
    if og.get("image") and not any(
            (i.get("src") if isinstance(i, dict) else i) == og["image"]
            for i in images):
        images.insert(0, {"src": og["image"],
                          "alt": og.get("title", ""), "w": None, "h": None,
                          "link": None, "zone": "main", "from": "og"})
    if og.get("description"):
        text = _clean(text + " " + og["description"])
    return {"title": title, "text": _clean(text), "headings": headings,
            "links": links, "images": images, "kind": "html",
            "canonical": canonical, "structured": structured}


def _short_type(url):
    """Normaliza tipos schema.org: URL http/https → nombre corto."""
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _microdata_items(soup):
    """Microdata: itemscope/itemtype + itemprop (igual de válido que JSON-LD)."""
    out = []
    for tag in soup.find_all(attrs={"itemscope": True}):
        if tag.find_parent(attrs={"itemscope": True}):
            continue  # solo raíces; lo anidado va dentro de data
        t = _short_type(tag.get("itemtype", ""))
        if not t:
            continue
        out.append({"type": t, "name": None, "data": _microdata_node(tag),
                    "format": "microdata"})
    return out


def _microdata_node(tag):
    data = {}
    for el in tag.find_all(attrs={"itemprop": True}):
        # el propietario es el itemscope más cercano
        owner = el.find_parent(attrs={"itemscope": True})
        if owner is not tag:
            continue
        prop = el.get("itemprop")
        if el.name in ("a", "link") and el.get("href"):
            val = el["href"]
        elif el.name in ("img", "source") and el.get("src"):
            val = el["src"]
        elif el.name == "meta" and el.get("content") is not None:
            val = el["content"]
        elif el.name in ("time",) and el.get("datetime"):
            val = el["datetime"]
        else:
            val = _clean(el.get_text(" "))
        if isinstance(val, str) and not val:
            continue
        data.setdefault(prop, val)
    name = data.get("name")
    return {"@type": _short_type(tag.get("itemtype", "")), "name": name,
            **data}


def _rdfa_items(soup):
    """RDFa Lite: typeof/property/vocab (tercer formato de Google)."""
    out = []
    for tag in soup.find_all(attrs={"typeof": True}):
        if tag.find_parent(attrs={"typeof": True}):
            continue
        t = _short_type((tag.get("typeof") or "").split()[0])
        if not t:
            continue
        data = {}
        for el in tag.find_all(attrs={"property": True}):
            owner = el.find_parent(attrs={"typeof": True})
            if owner is not tag:
                continue
            prop = _short_type(el.get("property").split()[0])
            if el.name in ("a", "link") and el.get("href"):
                val = el["href"]
            elif el.name in ("img",) and el.get("src"):
                val = el["src"]
            elif el.get("content") is not None:
                val = el["content"]
            else:
                val = _clean(el.get_text(" "))
            if val != "":
                data.setdefault(prop, val)
        out.append({"type": t, "name": data.get("name"),
                    "data": {"@type": t, **data}, "format": "rdfa"})
    return out


def _nearest_name(tag):
    """Nombre desde caption o encabezado cercano (solo estructura, sin vocabulario)."""
    cap = tag.find("caption")
    if cap and _clean(cap.get_text()):
        return _clean(cap.get_text())[:120]
    prev = tag
    for _ in range(6):
        prev = prev.find_previous(["h1", "h2", "h3", "h4"])
        if not prev:
            break
        t = _clean(prev.get_text())
        if t:
            return t[:120]
    return ""


def _table_records(soup):
    """Tablas y listas de definición como pares genéricos etiqueta→valor.

    Para sitios sin JSON-LD/Microdata/RDFa: la estructura visible (una
    ficha, una tabla de specs) también es dato. Sin vocabulario por rubro:
    las etiquetas son las que ponga la página. Tipo schema.org genérico
    StructuredValue, confianza menor que el markup explícito.
    """
    out = []
    for table in soup.find_all("table"):
        if any(getattr(a, "name", None) in ("header", "footer", "nav")
               for a in table.parents):
            continue
        pairs = {}
        for tr in table.find_all("tr"):
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            if (len(cells) == 2 and cells[0] and cells[1]
                    and len(cells[0]) <= 80 and len(cells[1]) <= 300
                    and len(pairs) < 25):
                pairs.setdefault(cells[0][:80], cells[1][:300])
        if len(pairs) >= 2:
            out.append({"type": "StructuredValue", "name": _nearest_name(table),
                        "data": {"@type": "StructuredValue", **pairs},
                        "format": "table"})
    for dl in soup.find_all("dl"):
        if any(getattr(a, "name", None) in ("header", "footer", "nav")
               for a in dl.parents):
            continue
        pairs = {}
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            k, v = _clean(dt.get_text(" ")), _clean(dd.get_text(" ") if dd else "")
            if k and v and len(k) <= 80 and len(v) <= 300 and len(pairs) < 25:
                pairs.setdefault(k[:80], v[:300])
        if len(pairs) >= 2:
            out.append({"type": "StructuredValue", "name": _nearest_name(dl),
                        "data": {"@type": "StructuredValue", **pairs},
                        "format": "table"})
    for lst in soup.find_all(["ul", "ol"]):
        if any(getattr(a, "name", None) in ("header", "footer", "nav")
               for a in lst.parents):
            continue
        pairs = {}
        for li in lst.find_all("li", recursive=False):
            if li.find(["ul", "ol"]) or len(li.find_all("a")) > 1:
                continue
            label = _clean(" ".join(
                s for s in li.find_all(string=True, recursive=False)))
            kids = [c for c in li.find_all(recursive=False)
                    if getattr(c, "name", None) in
                    ("span", "strong", "b", "em", "div", "p")]
            if not label or len(label) > 80 or len(kids) != 1:
                continue
            val = _clean(kids[0].get_text(" "))
            if val and len(val) <= 300 and len(pairs) < 25:
                pairs.setdefault(label[:80], val[:300])
        if len(pairs) >= 3:
            out.append({"type": "StructuredValue", "name": _nearest_name(lst),
                        "data": {"@type": "StructuredValue", **pairs},
                        "format": "list"})
    return out[:10]


def _json_ld(soup):
    """Datos estructurados primero: vienen servidos en bandeja."""
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        flat = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if isinstance(it.get("@graph"), list):
                flat.extend(x for x in it["@graph"] if isinstance(x, dict))
            else:
                flat.append(it)
        for it in flat:
            if "@type" in it:
                out.append({"type": it.get("@type"), "name": it.get("name"),
                            "data": it})
    return out[:10]


def _walk_json_images(node, found):
    """Caza URLs de imagen a cualquier profundidad del JSON-LD/API.

    Keys genéricas multilingües (image/photo/foto/imagen...), no de un sitio.
    """
    IMG_KEYS = ("image", "images", "photo", "photos", "foto", "fotos",
                "imagen", "imagenes", "picture", "pictures", "thumbnail",
                "thumbnails", "thumb", "contenturl", "url_foto", "url_imagen",
                "foto_url", "imagen_url", "gallery", "galeria", "slides")
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in IMG_KEYS:
                vals = v if isinstance(v, list) else [v]
                for x in vals:
                    u = x.get("url") if isinstance(x, dict) else x
                    if isinstance(u, str) and u.startswith("http"):
                        found.append(u)
            else:
                _walk_json_images(v, found)
    elif isinstance(node, list):
        for x in node:
            _walk_json_images(x, found)


def _walk_json_text(node, parts, _key=""):
    """Texto key: valor desde JSON (APIs, embeds). Descarta config/tokens."""
    SKIP_KEYS = ("color", "fondo", "style", "css", "token", "apik", "api",
                 "password", "secret", "url", "link", "href", "src", "icon",
                 "logo", "font", "width", "height", "id", "key")
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if kl.isdigit() or any(s in kl for s in SKIP_KEYS):
                _walk_json_text(v, parts, _key) if isinstance(v, (dict, list)) else None
                continue
            if isinstance(v, (dict, list)):
                _walk_json_text(v, parts, k)
            elif isinstance(v, str):
                s = v.strip()
                if not s or len(s) > 400:
                    continue
                if re.fullmatch(r"[a-f0-9]{16,}|[A-Z0-9_\-]{20,}|\d+", s):
                    continue  # tokens, hashes, códigos internos
                if len(k) <= 5 and " " not in s:
                    continue  # códigos internos (codemp, in amb...)
                label = re.sub(r"_+", " ", k).strip().rstrip(":")
                parts.append(f"{label}: {s}" if len(s) < 200 else s)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                parts.append(f"{k}: {v}")
    elif isinstance(node, list):
        for x in node:
            _walk_json_text(x, parts, _key)
    elif isinstance(node, str):
        s = node.strip()
        if len(s) > 25 and " " in s and not s.startswith(("http", "{", "<")):
            parts.append(s)


def _og_meta(soup):
    """OpenGraph/Twitter Card: título, descripción e imagen de respaldo."""
    out = {}
    for prop, key in (("og:title", "title"), ("og:description", "description"),
                      ("og:image", "image"), ("twitter:title", "title"),
                      ("twitter:description", "description"),
                      ("twitter:image", "image")):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content", "").strip() and key not in out:
            out[key] = tag["content"].strip()
    return out


def link_urls(links):
    """URLs planas desde links ricos o legacy (strings)."""
    out = []
    for l in links or []:
        u = l.get("url") if isinstance(l, dict) else l
        if u and u not in out:
            out.append(u)
    return out


def merge_links(a, b):
    """Une dos listas de links por URL, prefiriendo anchor no vacío."""
    by_url = {}
    for l in (a or []) + (b or []):
        if isinstance(l, dict):
            u = l.get("url")
            if not u:
                continue
            if u not in by_url or (not by_url[u].get("anchor") and l.get("anchor")):
                by_url[u] = l
        elif l and l not in by_url:
            by_url[l] = {"url": l, "anchor": "", "rel": "", "zone": "main"}
    return list(by_url.values())[:300]


def parse_pdf(body, url):
    import fitz
    parts = []
    with fitz.open(stream=body, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text())
    text = _clean("\n".join(parts))
    return {"title": "", "text": text, "headings": [], "links": [],
            "images": [], "kind": "pdf"}


def parse_docx(body, url):
    import docx
    doc = docx.Document(io.BytesIO(body))
    text = _clean("\n".join(p.text for p in doc.paragraphs))
    return {"title": "", "text": text, "headings": [], "links": [],
            "images": [], "kind": "docx"}


def parse_xlsx(body, url):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        rows.append(f"# {ws.title}")
        for row in ws.iter_rows(values_only=True):
            rows.append(" | ".join("" if v is None else str(v) for v in row))
    return {"title": "", "text": _clean("\n".join(rows)), "headings": [],
            "links": [], "images": [], "kind": "xlsx"}


def parse_csv(body, url):
    text_in = body.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text_in)))
    text = "\n".join(" | ".join(c for c in r) for r in rows[:500])
    return {"title": "", "text": _clean(text), "headings": [], "links": [],
            "images": [], "kind": "csv"}


def parse_txt(body, url):
    return {"title": "", "text": _clean(body.decode("utf-8", errors="replace")),
            "headings": [], "links": [], "images": [], "kind": "txt"}


def parse_xml(body, url):
    root = ET.fromstring(body)
    texts = [_clean(t) for t in root.itertext() if _clean(t)]
    return {"title": "", "text": _clean(" ".join(texts)), "headings": [],
            "links": [], "images": [], "kind": "xml"}


PARSERS = {
    "text/html": parse_html,
    "application/pdf": parse_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": parse_docx,
    "application/msword": parse_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": parse_xlsx,
    "application/vnd.ms-excel": parse_xlsx,
    "text/csv": parse_csv,
    "text/plain": parse_txt,
    "text/xml": parse_xml,
    "application/xml": parse_xml,
}


def parse(content_type, body, url):
    """Despacha por Content-Type; por extensión si el header falta o miente."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    fn = PARSERS.get(ctype)
    if fn is None:
        low = url.lower()
        if low.endswith(".pdf"):
            fn = parse_pdf
        elif low.endswith(".docx") or low.endswith(".doc"):
            fn = parse_docx
        elif low.endswith((".xlsx", ".xls")):
            fn = parse_xlsx
        elif low.endswith(".csv"):
            fn = parse_csv
        elif low.endswith(".xml"):
            fn = parse_xml
        elif low.endswith((".txt", ".md")):
            fn = parse_txt
        else:
            return None, f"unsupported:{ctype or 'unknown'}"
    try:
        return fn(body, url), None
    except Exception as e:
        return None, f"parse:{type(e).__name__}"
