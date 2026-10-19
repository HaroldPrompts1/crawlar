"""Grafo de enlaces por cliente: tabla links, PageRank interno y auditoría."""
import json
from collections import Counter, defaultdict
from urllib.parse import urlparse

GENERIC_ANCHORS = {"click aquí", "clic aquí", "click here", "ver más",
                   "leer más", "aquí", "here", "more", "click", "ver",
                   "ir", "entrar", "link", "ver detalle"}


def _domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def rebuild(client):
    """Reconstruye la tabla links desde los documentos del cliente."""
    import db
    con = db.connect()
    try:
        con.execute("DELETE FROM links WHERE client=?", (client,))
        rows = con.execute(
            """SELECT d.url, d.links FROM documents d
               JOIN pages p ON p.url=d.url WHERE p.client=?""", (client,)).fetchall()
        n = 0
        for r in rows:
            try:
                items = json.loads(r["links"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            for it in items:
                if isinstance(it, dict):
                    u, a, rel, z = (it.get("url", ""), it.get("anchor", "")[:150],
                                    it.get("rel", ""), it.get("zone", "main"))
                else:
                    u, a, rel, z = it, "", "", "main"
                if not u:
                    continue
                con.execute(
                    "INSERT INTO links(client, from_url, to_url, anchor, rel, zone)"
                    " VALUES(?,?,?,?,?,?)", (client, r["url"], u, a, rel, z))
                n += 1
                if n % 200 == 0:
                    db.commit(con)
        db.commit(con)
        return n
    finally:
        con.close()


def pagerank(client, damping=0.85, iters=30):
    """PageRank interno: solo aristas mismo-dominio ya indexadas."""
    import db
    con = db.connect()
    try:
        pages = [r[0] for r in con.execute(
            "SELECT url FROM pages WHERE client=? AND status='ok'",
            (client,)).fetchall()]
        if not pages:
            return {}
        known = set(pages)
        out = defaultdict(list)
        for f, t in con.execute(
                "SELECT from_url, to_url FROM links WHERE client=?", (client,)):
            if f in known and t in known and t != f:
                out[f].append(t)
        n = len(pages)
        rank = {p: 1.0 / n for p in pages}
        for _ in range(iters):
            new = {p: (1 - damping) / n for p in pages}
            for f, targets in out.items():
                if targets:
                    share = damping * rank[f] / len(targets)
                    for t in targets:
                        new[t] += share
                else:
                    share = damping * rank[f] / n
                    for p in pages:
                        new[p] += share
            rank = new
        return rank
    finally:
        con.close()


def report(client, top=10):
    """Informe: autoridad interna, huérfanas, profundidad, anchors, salientes."""
    import db
    con = db.connect()
    try:
        pr = pagerank(client)
        indeg = Counter()
        outdeg = Counter()
        anchors = Counter()
        generic = Counter()
        externals = Counter()
        for f, t, a, rel in con.execute(
                "SELECT from_url, to_url, anchor, rel FROM links WHERE client=?",
                (client,)):
            same = _domain(f) == _domain(t)
            if same:
                indeg[t] += 1
                outdeg[f] += 1
                if a:
                    anchors[a.lower()] += 1
                    if a.lower().strip() in GENERIC_ANCHORS:
                        generic[a] += 1
            else:
                externals[t] += 1
        seeds = {r[0] for r in con.execute(
            "SELECT url FROM pages WHERE client=? AND depth=0", (client,))}
        known = {r[0] for r in con.execute(
            "SELECT url FROM pages WHERE client=? AND status='ok'", (client,))}
        orphans = sorted(p for p in known - seeds if indeg.get(p, 0) == 0)
        depth = dict(Counter(r[0] for r in con.execute(
            "SELECT depth FROM pages WHERE client=? AND status='ok'",
            (client,)).fetchall()))
        titles = {r[0]: r[1] for r in con.execute(
            "SELECT url, title FROM pages WHERE client=?", (client,))}
        top_pr = sorted(pr.items(), key=lambda x: x[1], reverse=True)[:top]
        return {
            "pagerank_top": [{"url": u, "title": titles.get(u, ""),
                              "score": round(s, 5)} for u, s in top_pr],
            "orphans": orphans[:50],
            "orphans_count": len(orphans),
            "depth_hist": depth,
            "generic_anchors": [{"anchor": a, "count": c}
                                for a, c in generic.most_common(15)],
            "top_anchors": [{"anchor": a, "count": c}
                            for a, c in anchors.most_common(15)],
            "externals": [{"url": u, "count": c}
                          for u, c in externals.most_common(20)],
            "edges": sum(outdeg.values()),
        }
    finally:
        con.close()
