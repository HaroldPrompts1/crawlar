"""Bucle del crawler: frontera → robots → fetch → parse → índice → réplica.

Sin hardcodeo de comportamiento: los umbrales salen de la config por
cliente (DB) y de las señales de cada sitio (crawl-delay, latencia,
errores). El código solo trae defaults seguros.
"""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urldefrag, urlencode, urlparse, urlunparse

import db
import dedup
import fetcher
import parsers
import renderer
import replica
import robots

_last_hit = {}
_domain_locks = {}

# Pre-filtro sintáctico (capa 1 de 3: canónica del sitio + near-dup atrás).
# Cubre el 99% del tracking real (utm_* de Google, fbclid, gclid); lo que
# pase igual lo atrapa la URL canónica o el SimHash.
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
            "utm_content", "fbclid", "gclid", "msclkid", "sessionid", "sid"}

# Estrategia de linking interno: BFS ponderado desde la homepage.
# - main (contenido) pesa más que chrome (header/footer/nav).
# - tope por página + blocklist anti-trampas (carritos, buscadores,
#   calendarios infinitos, facetas) para no inundar la frontera
#   (STAR/BEAST: el presupuesto no lo quema el spam).
ZONE_WEIGHT = {"main": 0.5, "chrome": 0.1}
MAX_LINKS_PER_PAGE = 100
TRAP_SUBSTR = (
    "/carrito", "/cart", "/checkout", "/mi-cuenta", "/my-account",
    "/wp-admin", "/wp-login", "?s=", "?search", "?q=",
    "/calendar", "/calendario", "?tribe_", "/eventos/?",
    "cdc-calendar", "?add-to-cart", "&add-to-cart",
)


def _is_trap(url):
    u = url.lower()
    return any(t in u for t in TRAP_SUBSTR)


def sitemap_priorities(pairs):
    """Normaliza <lastmod> a boost 0-1 (recientes primero).

    Sin lastmod → 0.2 base. Con lastmod → 0.2 + 0.8 * recencia
    normalizada. Evita el viejo lastmod/1e9 que dejaba todo empatado.
    """
    if not pairs:
        return []
    dated = [t for _, t in pairs if t]
    if not dated:
        return [(u, 0.2) for u, _ in pairs]
    mx, mn = max(dated), min(dated)
    out = []
    for u, t in pairs:
        if not t:
            out.append((u, 0.2))
        elif mx > mn:
            out.append((u, 0.2 + 0.8 * (t - mn) / (mx - mn)))
        else:
            out.append((u, 0.5))
    return out


def seed_sitemap(con, netloc, client):
    """Agrega semillas del sitemap con boost normalizado. Devuelve cuántas."""
    import robots as _robots
    n = 0
    try:
        pairs = _robots.sitemap_urls(netloc)
    except Exception:
        return 0
    for u, boost in sitemap_priorities(pairs):
        try:
            # depth=1: solo la homepage es depth 0; si el sitemap entrara
            # a depth 0 inflaría su prioridad (2.0) por encima del BFS real.
            if add_seed(con, u, client, sitemap_boost=boost, depth=1):
                n += 1
        except Exception:
            continue
    return n


def _norm(url):
    """Canónica sintáctica: minúsculas en host, sin fragmento ni tracking."""
    u, _ = urldefrag(url.strip())
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in TRACKING]
    netloc = p.netloc.lower()
    if netloc.endswith(":80") and p.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and p.scheme == "https":
        netloc = netloc[:-4]
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), netloc, path, "", urlencode(q), ""))


def _domain(url):
    return urlparse(url).netloc.lower()


def _polite(domain, floor):
    delay = robots.smart_delay(domain, floor)
    lock = _domain_locks.setdefault(domain, threading.Lock())
    with lock:
        wait = delay - (time.time() - _last_hit.get(domain, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[domain] = time.time()


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def looks_like_url(url):
    """Valida sintaxis mínima: esquema http(s) y host con punto."""
    try:
        p = urlparse(url.strip())
        return p.scheme in ("http", "https") and "." in p.netloc
    except Exception:
        return False


def add_seed(con, url, client=None, priority=0, sitemap_boost=0.0,
             zone_weight=0.0, depth=0):
    url = _norm(url)
    if not url.startswith(("http://", "https://")):
        return False  # sin esquema no se puede descargar: se descarta en frontera
    if not looks_like_url(url):
        return False
    domain = _domain(url)
    db.upsert_page(con, url, domain, depth, client or domain, priority,
                   zone_weight=zone_weight, sitemap_boost=sitemap_boost)
    return True


def _load_cfg(client):
    con = db.connect()
    try:
        return db.get_config(con, client or "")
    finally:
        con.close()


def crawl(max_pages=50, use_render=True, progress=None, client=None,
          workers=1, should_stop=None):
    cfg = _load_cfg(client)
    workers = workers or cfg["workers"]
    if workers <= 1:
        return _crawl_seq(max_pages, use_render, progress, client, cfg,
                          should_stop)
    done = [0]
    lock = threading.Lock()

    def task(row):
        con = db.connect()
        try:
            try:
                skip = _prefilter(con, row, cfg)
            except Exception as e:
                try:
                    con.rollback()
                except Exception:
                    pass
                if progress:
                    progress({"type": "skipped", "url": row["url"],
                              "reason": f"prefilter_error:{type(e).__name__}"})
                return f"error:prefilter:{type(e).__name__}"
            if skip:
                try:
                    db.mark_skipped(con, row["url"], skip)
                    db.commit(con)
                except Exception as e:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                    if progress:
                        progress({"type": "skipped", "url": row["url"],
                                  "reason": f"db_locked:{type(e).__name__}"})
                    return f"error:skipped:{type(e).__name__}"
                if progress:
                    progress({"type": "skipped", "url": row["url"],
                              "reason": skip})
                return f"skipped:{skip}"
            try:
                outcome = _crawl_one(con, row["url"], row["client"], row["domain"],
                                     row["depth"], use_render, cfg)
                db.commit(con)
            except Exception as e:
                try:
                    con.rollback()
                except Exception:
                    pass
                # No tumbar el lote: una URL con lock no aborta las otras 499
                if progress:
                    progress({"type": "skipped", "url": row["url"],
                              "reason": f"error:{type(e).__name__}: {e}"})
                return f"error:{type(e).__name__}"
            with lock:
                done[0] += 1
                n = done[0]
            if progress:
                progress({"type": "page_done", "url": row["url"],
                          "done": n, "outcome": outcome})
            return outcome
        finally:
            con.close()

    con = db.connect()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while done[0] < max_pages:
                if should_stop and should_stop():
                    if progress:
                        progress({"type": "cancelled", "done": done[0]})
                    break
                try:
                    batch = [r for r in db.next_batch(con, limit=workers * 2,
                                                      client=client)
                             if _claim(con, r["url"])]
                    db.commit(con)
                except Exception as e:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                    time.sleep(0.5)
                    if progress:
                        progress({"type": "skipped", "url": "",
                                  "reason": f"batch_retry:{type(e).__name__}"})
                    continue
                if not batch:
                    break
                if progress:
                    for r in batch:
                        progress({"type": "page_start", "url": r["url"],
                                  "done": done[0]})
                list(ex.map(task, batch))
    finally:
        con.close()
        renderer.close()
    return done[0]


def _claim(con, url):
    with db.write_locked():
        cur = db.with_retry(lambda: con.execute(
            "UPDATE pages SET status='in_progress' WHERE url=? AND status='pending'",
            (url,)).rowcount)
    db.commit(con)
    return cur > 0


def _prefilter(con, row, cfg):
    """Mismos filtros que _crawl_seq: depth, tope por dominio y robots."""
    if row["depth"] > cfg["max_depth"]:
        return "too_deep"
    n = con.execute(
        "SELECT COUNT(*) n FROM pages WHERE domain=? AND status='ok'",
        (row["domain"],)).fetchone()["n"]
    if n >= cfg["max_pages_domain"]:
        return "domain_cap"
    if not robots.allowed(row["url"], row["domain"]):
        return "skipped_robots"
    return None


def _crawl_seq(max_pages, use_render, progress, client, cfg, should_stop):
    con = db.connect()
    done = 0
    try:
        while done < max_pages:
            if should_stop and should_stop():
                if progress:
                    progress({"type": "cancelled", "done": done})
                break
            batch = db.next_batch(con, limit=5, client=client)
            if not batch:
                break
            for row in batch:
                url = row["url"]
                if progress:
                    progress({"type": "page_start", "url": url, "done": done})
                skip = _prefilter(con, row, cfg)
                if skip:
                    db.mark_skipped(con, url, skip)
                    db.commit(con)
                    if progress:
                        progress({"type": "skipped", "url": url, "reason": skip})
                    continue
                outcome = _crawl_one(con, url, row["client"], row["domain"],
                                     row["depth"], use_render, cfg)
                db.commit(con)
                done += 1
                if progress:
                    progress({"type": "page_done", "url": url,
                              "done": done, "outcome": outcome})
                if done >= max_pages:
                    break
    finally:
        db.commit(con)
        con.close()
        renderer.close()
    return done


def _crawl_one(con, url, client, domain, depth, use_render, cfg):
    t0 = time.time()
    # Transacción cortísima: marcar y commitear ANTES de cualquier I/O.
    # (Un render de 60s con tx abierta bloquea a los demás writers.)
    db.mark_in_progress(con, url)
    db.commit(con)
    old = con.execute(
        "SELECT etag, last_modified, content_hash, text_hash FROM pages WHERE url=?",
        (url,)).fetchone()
    etag = old["etag"] if old else None
    last_mod = old["last_modified"] if old else None

    _polite(domain, cfg["delay_floor"])
    res = fetcher.fetch(url, etag, last_mod)
    blocked = res.status in (401, 403, 429)
    healthy = (res.error is None
               and res.status not in (401, 403, 429, 500, 502, 503, 504))
    robots.note_result(domain, healthy)
    used_render = False
    final_url = _norm(res.final_url) if res.final_url else None

    # WAF/antibot (403) o límite (429): el navegador suele pasar; escalar
    # a render antes de declarar el fallo. Decisión por dato, no por sitio.
    if blocked and use_render and not res.not_modified:
        html, _ = renderer.render(url)
        if html is not None:
            res = fetcher.FetchResult(200, {"content-type": "text/html"},
                                      html.encode("utf-8"), final_url=url)
            used_render = True
            healthy = True
            robots.note_result(domain, True)

    if res.not_modified:
        db.log_visit(con, url, t0, time.time(), 304, 0, False, None)
        db.mark_ok(con, url, 304,
                   old["content_hash"] and "cached" or None, etag, last_mod,
                   old["content_hash"], old["text_hash"], None, False)
        return "not_modified"

    if res.error:
        db.log_visit(con, url, t0, time.time(), res.status, 0, False, res.error)
        retryable = res.error in ("timeout", "connect_error") or res.status in fetcher.RETRYABLE
        if res.error.startswith("too_big"):
            db.mark_skipped(con, url, "too_big")
            return "skipped:too_big"
        else:
            db.mark_fail(con, url, res.error, res.status, retryable,
                         cfg["max_retries"], cfg["backoff"])
            return f"error:{res.error}"

    if res.status == 404:
        db.log_visit(con, url, t0, time.time(), 404, 0, False, "not_found")
        db.mark_skipped(con, url, "not_found")
        return "skipped:not_found"
    if res.status and res.status >= 400:
        err = f"http_{res.status}"
        db.log_visit(con, url, t0, time.time(), res.status, 0, used_render, err)
        db.mark_fail(con, url, err, res.status,
                     retryable=res.status in fetcher.RETRYABLE or blocked,
                     max_retries=cfg["max_retries"], backoff=cfg["backoff"])
        return f"error:{err}"

    ctype = res.headers.get("content-type", "")
    body = res.body
    raw_path = replica.save_raw(url, ctype, body)

    doc_pre = None
    if use_render and "text/html" in ctype:
        probe, _ = parsers.parse(ctype, body, url)
        if probe is not None:
            thin_text = len(probe["text"]) < cfg["render_min_chars"]
            # cáscara JS: mucho HTML, casi nada de texto (puro dato, sin sitios)
            density = len(probe["text"]) / max(1, len(body))
            shell = (len(body) > cfg["render_min_html"]
                     and density < cfg["render_min_density"])
            # Solo render si falta texto de verdad o es cáscara JS.
            # (Antes también renderizaba con <3 links internos: sobre-render
            # que duplicaba el tiempo por página sin ganar contenido.)
            if thin_text or shell:
                html, _, blobs, visible = renderer.render_with_api(url)
                if html is not None:
                    rendered, rerr = parsers.parse(
                        "text/html", html.encode("utf-8"), url)
                    if rerr is None:
                        # Lo visible manda: el navegador ya resolvió qué se muestra
                        vis = parsers._clean(visible or "")
                        if len(vis) > len(rendered["text"]):
                            rendered["text"] = vis
                        if len(rendered["text"]) > len(probe["text"]):
                            body = html.encode("utf-8")
                            doc_pre = rendered
                        else:
                            doc_pre = probe
                        # los links del DOM renderizado siempre se aprovechan
                        doc_pre["links"] = parsers.merge_links(
                            probe["links"], rendered["links"])
                        # JSON de XHR (tabs, galerías, fichas del backend)
                        api_parts, api_imgs = [], []
                        for b in blobs or []:
                            try:
                                import json as _json
                                node = _json.loads(b)
                            except (ValueError, TypeError):
                                continue
                            parsers._walk_json_text(node, api_parts)
                            parsers._walk_json_images(node, api_imgs)
                        if api_parts:
                            extra = " ".join(api_parts[:200])
                            doc_pre["text"] = (doc_pre["text"] + " " + extra)[:20000]
                        for u in list(dict.fromkeys(api_imgs))[:20]:
                            doc_pre["images"].append(
                                {"src": u, "alt": "", "w": None, "h": None,
                                 "link": None, "zone": "main", "from": "xhr"})
                        used_render = True

    doc, perr = parsers.parse(ctype, body, url)
    if perr is None and doc_pre is not None:
        # conservar los links del DOM renderizado aunque el texto base ganó
        doc["links"] = doc_pre["links"]
    if perr:
        db.log_visit(con, url, t0, time.time(), res.status, len(body), used_render, perr)
        if perr.startswith("unsupported"):
            db.mark_skipped(con, url, "unsupported_type")
            return "skipped:unsupported"
        else:
            db.mark_fail(con, url, perr, res.status, retryable=False)
            return f"error:{perr}"

    # Canónica: manda el dato de la propia página, no una regla nuestra
    canon = _norm(doc.get("canonical")) if doc.get("canonical") else None
    target = url
    if canon and canon != url and _domain(canon) == domain:
        target = canon
        db.upsert_page(con, canon, domain, depth, client)
        db.mark_skipped(con, url, "alias_of_canonical")

    content_hash = _sha(body)
    text_hash = _sha(doc["text"].encode("utf-8"))
    need_replica = False
    with db.write_locked():
        old_t = con.execute("SELECT text_hash FROM pages WHERE url=?",
                            (target,)).fetchone()
        changed = (old_t is None or old_t["text_hash"] is None
                   or text_hash != old_t["text_hash"])

        db.log_visit(con, url, t0, time.time(), res.status, len(body), used_render, None)
        db.mark_ok(con, target, res.status, ctype,
                   res.headers.get("etag"), res.headers.get("last-modified"),
                   content_hash, text_hash, doc["title"] or None, changed,
                   raw_path=raw_path, final_url=final_url, canonical=canon)

        has_doc = con.execute(
            "SELECT COUNT(*) n FROM documents WHERE url=?", (target,)).fetchone()["n"]
        if changed or not has_doc:
            dup_of = dedup.is_near_dup(con, client, doc["text"])
            if dup_of and dup_of != target:
                db.mark_skipped(con, target, "near_dup")
                db.commit(con)
                return f"skipped:near_dup_of:{dup_of[:60]}"
            db.save_document(con, target, doc["title"], doc["text"],
                             doc["headings"], doc["links"], doc["images"],
                             doc.get("structured"), dedup.simhash(doc["text"]))
            db.commit(con)  # commit antes de tocar archivos/red
            need_replica = True
        else:
            db.commit(con)

        if doc["kind"] == "html" and depth < cfg["max_depth"]:
            seen, queued = set(), 0
            # main primero: el contenido manda, el chrome (nav/footer)
            # se encola igual pero con menos peso y sin quemar el tope.
            def _zone_key(l):
                z = l.get("zone") if isinstance(l, dict) else "main"
                return 0 if z == "main" else 1
            ordered = sorted(doc["links"] or [], key=_zone_key)
            for item in ordered:
                raw = item.get("url") if isinstance(item, dict) else item
                if not raw:
                    continue
                link = _norm(raw)
                if not link or link in seen:
                    continue
                seen.add(link)
                if _domain(link) != domain or _is_trap(link):
                    continue
                zw = ZONE_WEIGHT.get(
                    item.get("zone") if isinstance(item, dict) else "main", 0.1)
                db.upsert_page(con, link, domain, depth + 1, client,
                               zone_weight=zw)
                queued += 1
                if queued >= MAX_LINKS_PER_PAGE:
                    break
            db.commit(con)
    if need_replica:
        replica.build_page(target, doc, client)
    return ("ok_changed" if changed else "ok_unchanged") + ("+render" if used_render else "")


def strip_boilerplate(client, ratio=None, min_docs=5, width=10):
    """Quita el boilerplate cross-page: shingles frecuentes en el cliente."""
    from collections import Counter
    if ratio is None:
        ratio = _load_cfg(client)["boiler_ratio"]
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT d.url, d.text FROM documents d JOIN pages p ON p.url=d.url
               WHERE p.client=?""", (client,)).fetchall()
        if len(rows) < min_docs:
            return 0
        words = {r["url"]: r["text"].split() for r in rows}
        freq = Counter()
        for w in words.values():
            seen = set()
            for i in range(len(w) - width + 1):
                seen.add(" ".join(w[i:i + width]))
            freq.update(seen)
        need = max(2, int(len(rows) * ratio))
        boiler = {s for s, n in freq.items() if n >= need}
        if not boiler:
            return 0
        n = 0
        touched = []
        for url, w in words.items():
            drop = [False] * len(w)
            for i in range(len(w) - width + 1):
                if " ".join(w[i:i + width]) in boiler:
                    for j in range(i, min(i + width, len(w))):
                        drop[j] = True
            if not any(drop):
                continue
            new = " ".join(x for x, d in zip(w, drop) if not d)
            con.execute("UPDATE documents SET text=?, word_count=? WHERE url=?",
                        (new, len(new.split()), url))
            con.execute("DELETE FROM documents_fts WHERE url=?", (url,))
            d = con.execute(
                "SELECT title FROM documents WHERE url=?",
                (url,)).fetchone()
            con.execute("INSERT INTO documents_fts(url, title, text) VALUES(?,?,?)",
                        (url, d["title"], new))
            touched.append(url)
            n += 1
            if n % 25 == 0:
                db.commit(con)  # no retener el lock de escritura
        db.commit(con)
    finally:
        con.close()
    # Archivos FUERA de la transacción (descargas de imágenes incluidas)
    for url in touched:
        con = db.connect()
        try:
            d = con.execute(
                "SELECT title, text, headings, links, images, structured FROM documents WHERE url=?",
                (url,)).fetchone()
            if d:
                import json as _json
                replica.build_page(url, {
                    "title": d["title"], "text": d["text"],
                    "headings": _json.loads(d["headings"]), "links": [],
                    "images": _json.loads(d["images"]),
                    "structured": _json.loads(d["structured"]),
                }, client)
        finally:
            con.close()
    return n


def reindex(client=None):
    """Re-parsea el crudo guardado con el parser actual (tras mejorarlo)."""
    import os
    con = db.connect()
    try:
        q = "SELECT url, client, content_type, raw_path FROM pages WHERE status='ok'"
        args = []
        if client:
            q += " AND client=?"
            args.append(client)
        n = 0
        saved = []
        for r in con.execute(q, args).fetchall():
            if not r["raw_path"] or not os.path.exists(r["raw_path"]):
                continue
            with open(r["raw_path"], "rb") as f:
                body = f.read()
            doc, perr = parsers.parse(r["content_type"], body, r["url"])
            if perr:
                continue
            db.save_document(con, r["url"], doc["title"], doc["text"],
                             doc["headings"], doc["links"], doc["images"],
                             doc.get("structured"), dedup.simhash(doc["text"]))
            saved.append((r["url"], r["client"], doc))
            n += 1
            if n % 25 == 0:
                db.commit(con)
        db.commit(con)
    finally:
        con.close()
    for url, client, doc in saved:
        replica.build_page(url, doc, client)
    return n


def recrawl(max_pages=200, client=None):
    """Revisita lo conocido: condicionales HTTP + hash deciden qué cambió."""
    con = db.connect()
    try:
        q = "SELECT url FROM pages WHERE status='ok'"
        args = []
        if client:
            q += " AND client=?"
            args.append(client)
        q += " ORDER BY last_crawled LIMIT ?"
        args.append(max_pages)
        for r in con.execute(q, args).fetchall():
            con.execute("UPDATE pages SET status='pending' WHERE url=?", (r["url"],))
        db.commit(con)
    finally:
        con.close()
    return crawl(max_pages=max_pages, client=client)
