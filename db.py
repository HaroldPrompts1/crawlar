"""SQLite como fuente de verdad: páginas, documentos, visitas + FTS5."""
import json
import sqlite3
import threading
import time

from config import DB_PATH

_write_lock = threading.RLock()


def write_locked():
    """Un solo escritor global: evita 'database is locked' entre jobs/threads."""
    return _write_lock


def with_retry(fn, tries=4, base_wait=0.05):
    """Reintenta la operación ante database is locked (backoff)."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            last = e
            time.sleep(base_wait * (2 ** i))
    raise last


def commit(con):
    """Commit serializado + con reintento ante 'database is locked'."""
    with _write_lock:
        return with_retry(lambda: con.commit())


def _write(con, sql, args=()):
    """Execute de escritura serializado con reintento."""
    with _write_lock:
        return with_retry(lambda: con.execute(sql, args))

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    client TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    http_status INTEGER,
    content_type TEXT,
    final_url TEXT,
    canonical TEXT,
    priority REAL NOT NULL DEFAULT 0,
    inlinks INTEGER NOT NULL DEFAULT 0,
    sitemap_boost REAL NOT NULL DEFAULT 0,
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT,
    text_hash TEXT,
    raw_path TEXT,
    title TEXT,
    first_seen REAL NOT NULL,
    last_crawled REAL,
    last_changed REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(client, status, depth, first_seen);
CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain, status);

CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY REFERENCES pages(url),
    title TEXT,
    text TEXT NOT NULL DEFAULT '',
    headings TEXT NOT NULL DEFAULT '[]',
    links TEXT NOT NULL DEFAULT '[]',
    images TEXT NOT NULL DEFAULT '[]',
    structured TEXT NOT NULL DEFAULT '[]',
    simhash TEXT,
    word_count INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
    USING fts5(url, title, text, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS client_config (
    client TEXT PRIMARY KEY,
    cfg TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS site_assets (
    client TEXT NOT NULL,
    src_norm TEXT NOT NULL,
    src_seen TEXT NOT NULL DEFAULT '',
    alt TEXT NOT NULL DEFAULT '',
    pages_count INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'content',
    caption TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (client, src_norm)
);

CREATE TABLE IF NOT EXISTS links (
    client TEXT NOT NULL,
    from_url TEXT NOT NULL,
    to_url TEXT NOT NULL,
    anchor TEXT NOT NULL DEFAULT '',
    rel TEXT NOT NULL DEFAULT '',
    zone TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_links_client ON links(client, to_url);

CREATE TABLE IF NOT EXISTS entities (
    client TEXT NOT NULL,
    url TEXT NOT NULL,
    etype TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    attrs TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entities_client ON entities(client, etype);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    http_status INTEGER,
    bytes INTEGER,
    used_render INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(pages)")}
    for col, ddl in (("client", "TEXT NOT NULL DEFAULT ''"),
                     ("final_url", "TEXT"), ("canonical", "TEXT"),
                     ("priority", "REAL NOT NULL DEFAULT 0"),
                     ("inlinks", "INTEGER NOT NULL DEFAULT 0"),
                     ("sitemap_boost", "REAL NOT NULL DEFAULT 0")):
        if col not in cols:
            con.execute(f"ALTER TABLE pages ADD COLUMN {col} {ddl}")
    dcols = {r["name"] for r in con.execute("PRAGMA table_info(documents)")}
    for col, ddl in (("structured", "TEXT NOT NULL DEFAULT '[]'"),
                     ("simhash", "TEXT")):
        if col not in dcols:
            con.execute(f"ALTER TABLE documents ADD COLUMN {col} {ddl}")
    # Migración y rescate (ver §11 buenas prácticas)
    con.execute("UPDATE pages SET client=domain WHERE client=''")
    con.execute("UPDATE pages SET status='pending', error='rescued_after_restart' "
                "WHERE status='in_progress'")
    commit(con)
    con.close()


def link_priority(depth, inlinks, sitemap_boost=0.0, zone_weight=0.0):
    """Frontera con criterio (BFS ponderado, Najork & Wiener + OPIC-lite).

    - BFS desde la homepage: 2.0/(1+depth) premia lo superficial.
    - Autoridad interna: 0.5*log(1+inlinks) premia lo muy linkeado.
    - Sitemap: boost 0-1 por recencia (<lastmod> normalizado).
    - Zona: main pesa más que chrome (header/footer/nav).
    """
    import math
    try:
        d = max(0, int(depth))
    except (TypeError, ValueError):
        d = 0
    try:
        n = max(0, int(inlinks))
    except (TypeError, ValueError):
        n = 0
    try:
        sb = max(0.0, min(1.0, float(sitemap_boost or 0.0)))
    except (TypeError, ValueError):
        sb = 0.0
    try:
        zw = max(0.0, float(zone_weight or 0.0))
    except (TypeError, ValueError):
        zw = 0.0
    return sb + 2.0 / (1 + d) + 0.5 * math.log1p(n) + zw


def upsert_page(con, url, domain, depth, client=None, priority=0,
                zone_weight=0.0, sitemap_boost=0.0):
    now = time.time()
    try:
        sb = max(0.0, min(1.0, float(sitemap_boost or 0.0)))
    except (TypeError, ValueError):
        sb = 0.0
    try:
        zw = max(0.0, float(zone_weight or 0.0))
    except (TypeError, ValueError):
        zw = 0.0
    row = con.execute(
        "SELECT depth, inlinks, sitemap_boost, priority, status FROM pages WHERE url=?",
        (url,)).fetchone()
    if row is None:
        inlinks = 0 if depth == 0 else 1
        prio = link_priority(depth, inlinks, sb, zw)
        if priority and priority > prio:
            prio = float(priority)  # compat: boost explícito mayor manda
        _write(
            con,
            """INSERT INTO pages(url, client, domain, depth, status, priority,
                   inlinks, sitemap_boost, first_seen)
               VALUES(?,?,?,?,?,?,?, ?,?)""",
            (url, client or domain, domain, depth, "pending", prio,
             inlinks, sb, now),
        )
        return
    # Ya existe: no duplicar trabajo hecho; pero sí mejorar el pendiente:
    # depth mínima (atajo desde homepage), inlinks++, boost máximo.
    if row["status"] not in ("pending", "fetch_error"):
        return
    new_depth = min(row["depth"], depth)
    new_inlinks = (row["inlinks"] or 0) + 1
    new_sb = max(row["sitemap_boost"] or 0.0, sb)
    new_prio = link_priority(new_depth, new_inlinks, new_sb, zw)
    best = max(row["priority"] or 0.0, new_prio,
               float(priority or 0.0))
    _write(
        con,
        """UPDATE pages SET depth=?, inlinks=?, sitemap_boost=?,
              priority=MAX(priority, ?)
           WHERE url=? AND status IN ('pending','fetch_error')""",
        (new_depth, new_inlinks, new_sb, best, url),
    )


def next_batch(con, limit=10, client=None):
    """Frontera: pendientes o reintentos vencidos; prioridad, luego profundidad."""
    now = time.time()
    if client:
        q = """SELECT url, client, domain, depth, retry_count FROM pages
               WHERE client=? AND (status='pending'
                 OR (status='fetch_error' AND next_retry_at IS NOT NULL
                     AND next_retry_at <= ?))
               ORDER BY priority DESC, depth, first_seen LIMIT ?"""
        args = [client, now, limit]
    else:
        q = """SELECT url, client, domain, depth, retry_count FROM pages
               WHERE status='pending'
                  OR (status='fetch_error' AND next_retry_at IS NOT NULL
                      AND next_retry_at <= ?)
               ORDER BY priority DESC, depth, first_seen LIMIT ?"""
        args = [now, limit]
    return con.execute(q, args).fetchall()


def mark_in_progress(con, url):
    _write(con, "UPDATE pages SET status='in_progress' WHERE url=?", (url,))


def mark_ok(con, url, http_status, content_type, etag, last_modified,
            content_hash, text_hash, title, changed, raw_path=None,
            final_url=None, canonical=None):
    now = time.time()
    _write(
        con,
        """UPDATE pages SET status='ok', http_status=?, content_type=?,
              etag=?, last_modified=?, content_hash=?, text_hash=?, title=?,
              raw_path=COALESCE(?, raw_path),
              final_url=COALESCE(?, final_url),
              canonical=COALESCE(?, canonical),
              last_crawled=?, last_changed=CASE WHEN ? THEN ? ELSE last_changed END,
              retry_count=0, next_retry_at=NULL, error=NULL
           WHERE url=?""",
        (http_status, content_type, etag, last_modified, content_hash,
         text_hash, title, raw_path, final_url, canonical,
         now, 1 if changed else 0, now, url),
    )


def mark_fail(con, url, error, http_status=None, retryable=True,
              max_retries=None, backoff=None):
    """Fallo con backoff; tras agotar reintentos queda en cuarentena visible."""
    from config import MAX_RETRIES, BACKOFF
    max_retries = MAX_RETRIES if max_retries is None else max_retries
    backoff = BACKOFF if backoff is None else backoff
    row = con.execute(
        "SELECT retry_count FROM pages WHERE url=?", (url,)).fetchone()
    n = (row["retry_count"] if row else 0) + 1
    if retryable and n <= max_retries:
        wait = backoff[min(n - 1, len(backoff) - 1)]
        _write(
            con,
            """UPDATE pages SET status='fetch_error', retry_count=?,
                  next_retry_at=?, error=?, http_status=?
               WHERE url=?""",
            (n, time.time() + wait, error, http_status, url),
        )
    else:
        _write(
            con,
            """UPDATE pages SET status='fetch_error', retry_count=?,
                  next_retry_at=NULL, error=?, http_status=?
               WHERE url=?""",
            (n, error, http_status, url),
        )


def mark_skipped(con, url, reason):
    _write(con, "UPDATE pages SET status=?, error=NULL WHERE url=?",
           (reason, url))


def save_document(con, url, title, text, headings, links, images,
                  structured=None, simhash=None):
    now = time.time()
    with _write_lock:
        with_retry(lambda: con.execute(
            """INSERT INTO documents(url, title, text, headings, links, images,
                   structured, simhash, word_count, indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET title=excluded.title, text=excluded.text,
                 headings=excluded.headings, links=excluded.links, images=excluded.images,
                 structured=excluded.structured, simhash=excluded.simhash,
                 word_count=excluded.word_count, indexed_at=excluded.indexed_at""",
            (url, title, text, json.dumps(headings), json.dumps(links),
             json.dumps(images), json.dumps(structured or []),
             simhash, len(text.split()), now),
        ))
        with_retry(lambda: con.execute("DELETE FROM documents_fts WHERE url=?", (url,)))
        with_retry(lambda: con.execute(
            "INSERT INTO documents_fts(url, title, text) VALUES(?,?,?)",
            (url, title, text)))


def log_visit(con, url, started, finished, http_status, nbytes, used_render, error):
    _write(
        con,
        """INSERT INTO visits(url, started_at, finished_at, http_status, bytes, used_render, error)
           VALUES(?,?,?,?,?,?,?)""",
        (url, started, finished, http_status, nbytes, 1 if used_render else 0, error),
    )


def stats(con, client=None):
    f = "WHERE client=?" if client else ""
    a = [client] if client else []
    out = {}
    out["by_status"] = {
        r["status"]: r["n"] for r in
        con.execute(f"SELECT status, COUNT(*) n FROM pages {f} GROUP BY status", a)
    }
    out["docs"] = con.execute(
        f"""SELECT COUNT(*) n FROM documents d JOIN pages p ON p.url=d.url {f}""", a
    ).fetchone()["n"]
    out["visits"] = con.execute("SELECT COUNT(*) n FROM visits").fetchone()["n"]
    out["failures"] = [
        dict(r) for r in con.execute(
            f"""SELECT url, error, retry_count, http_status FROM pages {f}
               {"AND" if f else "WHERE"} status='fetch_error'
               ORDER BY retry_count DESC LIMIT 50""", a)
    ]
    out["recent"] = [
        dict(r) for r in con.execute(
            f"""SELECT url, title, last_crawled FROM pages {f}
               {"AND" if f else "WHERE"} status='ok'
               ORDER BY last_crawled DESC LIMIT 20""", a)
    ]
    return out


def clients(con):
    return [r["client"] for r in
            con.execute("SELECT DISTINCT client FROM pages ORDER BY client")]


DEFAULTS = {
    "max_depth": 3,
    "delay_floor": 1.0,
    "max_pages_domain": 200,
    "render_min_chars": 200,
    "render_min_density": 0.02,
    "render_min_html": 15000,
    "max_retries": 3,
    "backoff": [2, 8, 30],
    "boiler_ratio": 0.6,
    "workers": 1,
    "rank_weights": [0.6, 0.25, 0.15],
    "fts_weights": [3.0, 2.0, 1.0],
}


def get_config(con, client):
    """Config por cliente (datos, no código): defaults + lo guardado en DB."""
    cfg = dict(DEFAULTS)
    try:
        r = con.execute("SELECT cfg FROM client_config WHERE client=?",
                        (client,)).fetchone()
        if r and r["cfg"]:
            cfg.update(json.loads(r["cfg"]))
    except sqlite3.OperationalError:
        pass
    return cfg


def set_config(con, client, patch):
    cur = get_config(con, client)
    cur.update({k: v for k, v in patch.items() if k in DEFAULTS})
    _write(
        con,
        """INSERT INTO client_config(client, cfg) VALUES(?,?)
           ON CONFLICT(client) DO UPDATE SET cfg=excluded.cfg""",
        (client, json.dumps(cur)))
    commit(con)
    return cur


def delete_url(con, url):
    """Elimina el registro completo de una URL: pages, documents, FTS,
    visits, entities y aristas del grafo. Devuelve conteos + raw_path."""
    row = con.execute(
        "SELECT client, raw_path FROM pages WHERE url=?", (url,)).fetchone()
    if not row:
        return None
    info = {"client": row["client"], "raw_path": row["raw_path"]}
    counts = {}
    with _write_lock:
        for sql, args in (
            ("DELETE FROM documents_fts WHERE url=?", (url,)),
            ("DELETE FROM documents WHERE url=?", (url,)),
            ("DELETE FROM visits WHERE url=?", (url,)),
            ("DELETE FROM entities WHERE url=?", (url,)),
            ("DELETE FROM links WHERE from_url=?", (url,)),
            ("DELETE FROM links WHERE to_url=?", (url,)),
            ("DELETE FROM pages WHERE url=?", (url,)),
        ):
            cur = with_retry(lambda s=sql, a=args: con.execute(s, a))
            key = sql.split()[2]
            counts[key] = counts.get(key, 0) + cur.rowcount
        with_retry(lambda: con.commit())
    info["counts"] = counts
    return info


def delete_client(con, client):
    """Elimina todos los registros de un cliente. Devuelve urls + raw_paths."""
    urls = [r[0] for r in con.execute(
        "SELECT url FROM pages WHERE client=?", (client,)).fetchall()]
    raws = [r[0] for r in con.execute(
        "SELECT raw_path FROM pages WHERE client=? AND raw_path IS NOT NULL",
        (client,)).fetchall()]
    counts = {}
    with _write_lock:
        url_list = ",".join("?" for _ in urls) if urls else None
        if urls:
            for tbl in ("documents_fts", "documents"):
                cur = with_retry(lambda t=tbl: con.execute(
                    f"DELETE FROM {t} WHERE url IN ({url_list})", urls))
                counts[tbl] = cur.rowcount
            cur = with_retry(lambda: con.execute(
                f"DELETE FROM visits WHERE url IN ({url_list})", urls))
            counts["visits"] = cur.rowcount
        for sql, args in (
            ("DELETE FROM entities WHERE client=?", (client,)),
            ("DELETE FROM links WHERE client=?", (client,)),
            ("DELETE FROM site_assets WHERE client=?", (client,)),
            ("DELETE FROM pages WHERE client=?", (client,)),
            ("DELETE FROM client_config WHERE client=?", (client,)),
        ):
            cur = with_retry(lambda s=sql, a=args: con.execute(s, a))
            key = sql.split()[2]
            counts[key] = counts.get(key, 0) + cur.rowcount
        with_retry(lambda: con.commit())
    return {"urls": urls, "raw_paths": raws, "counts": counts}
