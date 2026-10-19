"""Búsqueda sobre el índice FTS5 + exportación al formato del BM25."""
import logging
import os

import db

log = logging.getLogger(__name__)


def search(query, limit=10, client=None):
    """BM25 FTS + PageRank interno + frescura (pesos en config del cliente).

    Relación URL/título/contenido: BM25 pondera columnas (url, title, text)
    con fts_weights (default 3/2/1: lo que está en la URL manda, luego el
    título, luego el texto). El score final mezcla ese BM25 con PageRank
    interno y frescura según rank_weights.
    """
    import time
    import db as dbmod
    con = db.connect()
    try:
        filt = "AND p.client=?" if client else ""
        base_args = ([client] if client else [])
        cfg0 = dbmod.get_config(con, client or "")
        wu, wt, wx = cfg0.get("fts_weights", [3.0, 2.0, 1.0])
        bm25expr = f"bm25(documents_fts, {float(wu)}, {float(wt)}, {float(wx)})"
        try:
            args = [query] + base_args + [limit * 3]
            rows = con.execute(
                f"""SELECT d.url, d.title, p.client AS client,
                          p.last_changed AS changed,
                          snippet(documents_fts, 2, '<b>', '</b>', '…', 30) AS snip,
                          {bm25expr} AS rank
                   FROM documents_fts
                   JOIN documents d ON d.url = documents_fts.url
                   JOIN pages p ON p.url = d.url
                   WHERE documents_fts MATCH ? {filt} ORDER BY rank LIMIT ?""",
                args).fetchall()
        except Exception as e:
            # Fallback si la query trae sintaxis FTS5 problemática
            log.warning("FTS query falló (%r), reintentando literal: %s", e, query)
            # Fallback si la query trae sintaxis FTS5 problemática
            safe_query = '"' + query.replace('"', '""') + '"'
            args = [safe_query] + base_args + [limit * 3]
            rows = con.execute(
                f"""SELECT d.url, d.title, p.client AS client,
                          p.last_changed AS changed,
                          snippet(documents_fts, 2, '<b>', '</b>', '…', 30) AS snip,
                          {bm25expr} AS rank
                   FROM documents_fts
                   JOIN documents d ON d.url = documents_fts.url
                   JOIN pages p ON p.url = d.url
                   WHERE documents_fts MATCH ? {filt} ORDER BY rank LIMIT ?""",
                args).fetchall()
        res = [dict(r) for r in rows]
        if not res:
            return res
        import links as linksmod
        cfg = dbmod.get_config(con, client or "")
        wb, wp, wf = cfg.get("rank_weights", [0.6, 0.25, 0.15])
        pr = linksmod.pagerank(client) if client else {}
        now = time.time()

        def norm(vals, invert=False):
            lo, hi = min(vals), max(vals)
            if hi == lo:
                return [1.0] * len(vals)
            out = [(v - lo) / (hi - lo) for v in vals]
            return [1 - v for v in out] if invert else out

        b = norm([r["rank"] for r in res], invert=True)
        p = norm([pr.get(r["url"], 0) for r in res])
        f = norm([-(now - (r["changed"] or now)) for r in res])
        for r, sb, sp, sf in zip(res, b, p, f):
            r["score"] = round(wb * sb + wp * sp + wf * sf, 4)
        res.sort(key=lambda r: r["score"], reverse=True)
        return res[:limit]
    except Exception:
        log.exception("index.search falló para query=%r", query)
        return []
    finally:
        con.close()


def export_corpus(dest_dir):
    """Vuelca documentos a .md compatibles con BM25/corpus (bloques ---)."""
    os.makedirs(dest_dir, exist_ok=True)
    con = db.connect()
    try:
        rows = con.execute("SELECT url, title, text FROM documents").fetchall()
        path = os.path.join(dest_dir, "crawler_export.md")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(f"# {r['title'] or r['url']}\nFuente: {r['url']}\n\n{r['text']}\n\n---\n\n")
        return path, len(rows)
    finally:
        con.close()
