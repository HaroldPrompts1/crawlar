"""Interfaz local moderna: Dashboard, consola live, búsqueda FTS, réplicas y diagnósticos."""
import json
import os
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

import crawler
import db as dbmod
import index as idx
import robots
from config import BASE_DIR, REPLICA_DIR

STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

jobs = {}
jobs_lock = threading.Lock()
# Un solo trabajo de escritura a la vez: SQLite + jobs solapados = locked
crawl_mutex = threading.Lock()


def _emit(job, ev):
    ev["t"] = time.strftime("%H:%M:%S")
    with jobs_lock:
        job["events"].append(ev)
        job["events"] = job["events"][-300:]
        if ev["type"] == "page_start":
            job["current"] = ev["url"]
        elif ev["type"] == "page_done":
            job["done"] = ev["done"]
            job["current"] = ""
        elif ev["type"] == "finished":
            job["status"] = "done"
            job["current"] = ""
        elif ev["type"] == "cancelled":
            job["status"] = "cancelled"
            job["current"] = ""


def _run_job(job_id, seeds, client, max_pages, use_render, with_sitemap, workers):
    job = jobs[job_id]
    stop = lambda: job.get("cancel", False)
    if not crawl_mutex.acquire(blocking=False):
        _emit(job, {"type": "finished", "processed": 0,
                    "error": "busy: otro trabajo en curso"})
        return
    try:
        try:
            con = dbmod.connect()
            try:
                # rescata páginas atoradas por un job anterior caído
                con.execute("UPDATE pages SET status='pending', error=NULL "
                            "WHERE client=? AND status='in_progress'", (client,))
                invalid = 0
                for s in seeds:
                    ok = crawler.add_seed(con, s, client)
                    if not ok:
                        invalid += 1
                if invalid:
                    _emit(job, {"type": "skipped", "url": "",
                                "reason": f"{invalid} semillas inválidas (no son URLs)"})
                    if invalid == len(seeds):
                        _emit(job, {"type": "finished", "processed": 0,
                                    "error": "ninguna semilla válida"})
                        return
                if with_sitemap:
                    for s in seeds:
                        crawler.seed_sitemap(con, urlparse(s).netloc.lower(),
                                             client)
                dbmod.commit(con)
            finally:
                con.close()
            n = crawler.crawl(max_pages=max_pages, use_render=use_render,
                              progress=lambda ev: _emit(job, ev),
                              client=client, workers=workers, should_stop=stop)
            if stop():
                _emit(job, {"type": "cancelled", "done": n})
                return
            stripped = crawler.strip_boilerplate(client)
            import images as imagesmod
            imgroles = imagesmod.classify(client)
            import links as linksmod
            edges = linksmod.rebuild(client)
            graph = linksmod.report(client)
            import entities as entitiesmod
            nent = entitiesmod.rebuild(client)
            _emit(job, {"type": "finished", "processed": n,
                        "outcome": f"boilerplate:{stripped} images:{imgroles} "
                                   f"edges:{edges} orphans:{graph['orphans_count']} "
                                   f"entities:{nent}"})
        except Exception as e:
            _emit(job, {"type": "finished", "processed": 0,
                        "error": f"{type(e).__name__}: {e}"})
    finally:
        crawl_mutex.release()


@app.route("/")
def home():
    return send_from_directory(STATIC_DIR, "index.html")


def _client_from(req_json):
    seeds = req_json.get("seeds", [])
    if seeds:
        return urlparse(seeds[0]).netloc.lower()
    return (req_json.get("client") or "").strip()


@app.route("/api/dashboard")
def api_dashboard():
    client = request.args.get("client") or None
    con = dbmod.connect()
    try:
        q_words = "SELECT SUM(word_count) w FROM documents d JOIN pages p ON p.url=d.url"
        a_words = []
        if client:
            q_words += " WHERE p.client=?"
            a_words.append(client)
        r_words = con.execute(q_words, a_words).fetchone()
        words_total = r_words["w"] or 0 if r_words else 0

        clients_list = dbmod.clients(con)
        return jsonify({
            "words_total": words_total,
            "clients_count": len(clients_list),
            "clients": clients_list
        })
    finally:
        con.close()


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    data = request.get_json(force=True)
    seeds = []
    for s in data.get("seeds", []):
        s = s.strip()
        if not s:
            continue
        if "://" not in s:
            s = "https://" + s
        seeds.append(s)
    if not seeds:
        return jsonify({"error": "sin URLs"}), 400
    client = _client_from(data)
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "running", "done": 0, "planned": 0,
                        "current": "", "events": [], "client": client}
    t = threading.Thread(target=_run_job,
                         args=(job_id, seeds, client,
                               int(data.get("max_pages", 50)),
                               bool(data.get("use_render", True)),
                               bool(data.get("with_sitemap", False)),
                               int(data.get("workers", 1))),
                         daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "client": client})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job desconocido"}), 404
        job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        client = request.args.get("client") or ""
        con = dbmod.connect()
        try:
            return jsonify(dbmod.get_config(con, client))
        finally:
            con.close()
    data = request.get_json(force=True)
    client = (data.get("client") or "").strip()
    if not client:
        return jsonify({"error": "falta client"}), 400
    con = dbmod.connect()
    try:
        return jsonify(dbmod.set_config(con, client, data.get("cfg", {})))
    finally:
        con.close()


@app.route("/api/recrawl", methods=["POST"])
def api_recrawl():
    data = request.get_json(force=True, silent=True) or {}
    client = (data.get("client") or "").strip()
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "running", "done": 0, "planned": 0,
                        "current": "", "events": [], "client": client}
    t = threading.Thread(target=_run_recrawl, args=(job_id, client,), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


def _run_recrawl(job_id, client):
    job = jobs[job_id]
    if not crawl_mutex.acquire(blocking=False):
        _emit(job, {"type": "finished", "processed": 0,
                    "error": "busy: otro trabajo en curso"})
        return
    try:
        con = dbmod.connect()
        try:
            q, a = "SELECT url FROM pages WHERE status='ok'", []
            if client:
                q += " AND client=?"
                a.append(client)
            rows = con.execute(q, a).fetchall()
            for r in rows:
                con.execute("UPDATE pages SET status='pending' WHERE url=?", (r["url"],))
            dbmod.commit(con)
            with jobs_lock:
                job["planned"] = len(rows)
        finally:
            con.close()
        n = crawler.crawl(max_pages=max(200, len(rows)), client=client or None,
                          progress=lambda ev: _emit(job, ev),
                          should_stop=lambda: job.get("cancel", False))
        _emit(job, {"type": "finished", "processed": n})
    except Exception as e:
        _emit(job, {"type": "finished", "processed": 0,
                    "error": f"{type(e).__name__}: {e}"})
    finally:
        crawl_mutex.release()


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job desconocido"}), 404
        pending = 0
        if job["status"] == "running":
            con = dbmod.connect()
            try:
                q, a = ("SELECT COUNT(*) n FROM pages WHERE status='pending'", [])
                if job.get("client"):
                    q += " AND client=?"
                    a.append(job["client"])
                pending = con.execute(q, a).fetchone()["n"]
            finally:
                con.close()
        planned = max(job["planned"], job["done"] + pending)
        return jsonify({"status": job["status"], "done": job["done"],
                        "planned": planned, "current": job["current"],
                        "events": job["events"][-60:]})


@app.route("/api/clients")
def api_clients():
    con = dbmod.connect()
    try:
        return jsonify({"clients": dbmod.clients(con)})
    finally:
        con.close()


@app.route("/api/stats")
def api_stats():
    client = request.args.get("client") or None
    con = dbmod.connect()
    try:
        return jsonify(dbmod.stats(con, client))
    finally:
        con.close()


@app.route("/api/docs")
def api_docs():
    import replica as repmod
    client = request.args.get("client") or None
    con = dbmod.connect()
    try:
        q = ("SELECT d.url, d.title, d.word_count, p.client AS client "
             "FROM documents d JOIN pages p ON p.url=d.url")
        a = []
        if client:
            q += " WHERE p.client=?"
            a.append(client)
        q += " ORDER BY d.indexed_at DESC LIMIT 100"
        out = []
        for r in con.execute(q, a).fetchall():
            slug = repmod._slug(r["url"]) + ".html"
            out.append({"url": r["url"], "title": r["title"],
                        "words": r["word_count"],
                        "replica": f"{r['client']}/{slug}"})
        return jsonify({"docs": out})
    finally:
        con.close()


@app.route("/api/doc")
def api_doc():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url requerida"}), 400
    con = dbmod.connect()
    try:
        r = con.execute("SELECT * FROM documents WHERE url=?", (url,)).fetchone()
        if not r:
            return jsonify({"error": "no encontrado"}), 404
        return jsonify({
            "url": r["url"],
            "title": r["title"],
            "text": r["text"],
            "headings": json.loads(r["headings"] or "[]"),
            "links": json.loads(r["links"] or "[]"),
            "images": json.loads(r["images"] or "[]"),
            "structured": json.loads(r["structured"] or "[]"),
            "word_count": r["word_count"],
            "indexed_at": r["indexed_at"]
        })
    finally:
        con.close()


@app.route("/api/retry", methods=["POST"])
def api_retry():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url")
    client = data.get("client")
    all_failed = data.get("all", False)

    con = dbmod.connect()
    try:
        if all_failed:
            q = "UPDATE pages SET status='pending', retry_count=0, next_retry_at=NULL WHERE status='fetch_error'"
            args = []
            if client:
                q += " AND client=?"
                args.append(client)
            count = con.execute(q, args).rowcount
            dbmod.commit(con)
            return jsonify({"ok": True, "count": count})
        elif url:
            count = con.execute(
                "UPDATE pages SET status='pending', retry_count=0, next_retry_at=NULL WHERE url=?",
                (url,)).rowcount
            dbmod.commit(con)
            return jsonify({"ok": True, "count": count})
        return jsonify({"error": "especifica url o all:true"}), 400
    finally:
        con.close()


@app.route("/api/strip", methods=["POST"])
def api_strip():
    data = request.get_json(force=True, silent=True) or {}
    client = (data.get("client") or "").strip()
    if not client:
        return jsonify({"error": "falta client"}), 400
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "running", "done": 0, "planned": 1,
                        "current": "", "events": [], "client": client}
    threading.Thread(target=_run_maint,
                     args=(job_id, "strip", client), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    data = request.get_json(force=True, silent=True) or {}
    client = (data.get("client") or "").strip() or None
    job_id = uuid.uuid4().hex[:8]
    with jobs_lock:
        jobs[job_id] = {"status": "running", "done": 0, "planned": 1,
                        "current": "", "events": [], "client": client or ""}
    threading.Thread(target=_run_maint,
                     args=(job_id, "reindex", client), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_maint(job_id, kind, client):
    import images as imagesmod
    job = jobs[job_id]
    if not crawl_mutex.acquire(blocking=False):
        _emit(job, {"type": "finished", "processed": 0,
                    "error": "busy: otro trabajo en curso"})
        return
    try:
        _emit(job, {"type": "page_start", "url": f"{kind}:{client or 'todos'}"})
        if kind == "strip":
            n = crawler.strip_boilerplate(client)
            m = imagesmod.classify(client)
            _emit(job, {"type": "finished", "processed": n,
                        "outcome": f"stripped:{n} images:{m}"})
        else:
            n = crawler.reindex(client)
            _emit(job, {"type": "finished", "processed": n,
                        "outcome": f"reindexed:{n}"})
    except Exception as e:
        _emit(job, {"type": "finished", "processed": 0,
                    "error": f"{type(e).__name__}: {e}"})
    finally:
        crawl_mutex.release()


def dbmod_single_client(url):
    con = dbmod.connect()
    try:
        r = con.execute("SELECT client FROM pages WHERE url=?", (url,)).fetchone()
        return r["client"] if r else ""
    finally:
        con.close()


@app.route("/api/search")
def api_search():
    import replica as repmod
    q = request.args.get("q", "")
    client = request.args.get("client") or None
    out = []
    for r in (idx.search(q, client=client) if q else []):
        cli = r.get("client") or client or ""
        r["replica"] = f"{cli}/{repmod._slug(r['url'])}.html" if cli else ""
        out.append(r)
    return jsonify({"results": out})


@app.route("/api/graph")
def api_graph():
    import links as linksmod
    client = request.args.get("client") or ""
    if not client:
        return jsonify({"error": "falta client"}), 400
    return jsonify(linksmod.report(client))


@app.route("/api/entities")
def api_entities():
    import entities as entitiesmod
    client = request.args.get("client") or ""
    if not client:
        return jsonify({"error": "falta client"}), 400
    return jsonify({
        "types": entitiesmod.types_summary(client),
        "entities": entitiesmod.by_type(
            client, request.args.get("type"),
            limit=int(request.args.get("limit", 50))),
    })


@app.route("/api/assets")
def api_assets():
    client = request.args.get("client") or ""
    con = dbmod.connect()
    try:
        rows = con.execute(
            """SELECT src_seen AS src, alt, pages_count, role, caption
               FROM site_assets WHERE client=? ORDER BY pages_count DESC LIMIT 100""",
            (client,)).fetchall()
        return jsonify({"assets": [dict(r) for r in rows]})
    finally:
        con.close()


@app.route("/api/doc", methods=["DELETE"])
def api_delete_doc():
    import replica as repmod
    url = request.args.get("url") or (request.get_json(force=True, silent=True) or {}).get("url")
    if not url:
        return jsonify({"error": "url requerida"}), 400
    if not crawl_mutex.acquire(blocking=False):
        return jsonify({"error": "busy: otro trabajo en curso"}), 409
    try:
        con = dbmod.connect()
        try:
            info = dbmod.delete_url(con, url)
            if info is None:
                return jsonify({"error": "no encontrado"}), 404
        finally:
            con.close()
        files = repmod.delete_page(url, info.get("client"), info.get("raw_path"))
        return jsonify({"ok": True, "client": info.get("client"), "files": len(files)})
    finally:
        crawl_mutex.release()


@app.route("/api/client", methods=["DELETE"])
def api_delete_client():
    import replica as repmod
    client = request.args.get("client") or (request.get_json(force=True, silent=True) or {}).get("client")
    client = (client or "").strip()
    if not client:
        return jsonify({"error": "falta client"}), 400
    if not crawl_mutex.acquire(blocking=False):
        return jsonify({"error": "busy: otro trabajo en curso"}), 409
    try:
        con = dbmod.connect()
        try:
            # rescata in_progress del cliente para no dejarlo colgado
            con.execute("UPDATE pages SET status='pending', error=NULL "
                        "WHERE client=? AND status='in_progress'", (client,))
            dbmod.commit(con)
            info = dbmod.delete_client(con, client)
        finally:
            con.close()
        files = repmod.delete_client_files(client, info.get("raw_paths"))
        with jobs_lock:
            for j in jobs.values():
                if j.get("client") == client and j.get("status") == "running":
                    j["cancel"] = True
        return jsonify({"ok": True, "urls": len(info.get("urls", [])),
                        "files": len(files), "counts": info.get("counts", {})})
    finally:
        crawl_mutex.release()


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/replica/<path:name>")
def replica_file(name):
    return send_from_directory(REPLICA_DIR, name)


if __name__ == "__main__":
    dbmod.init_db()
    app.run(host="127.0.0.1", port=8766, debug=False)

