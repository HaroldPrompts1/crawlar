"""CLI: crawl | recrawl | search | status | serve."""
import argparse
import sys

import crawler
import db
import index
import robots


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("crawl")
    p.add_argument("seeds", nargs="+")
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--with-sitemap", action="store_true")
    p.add_argument("--client", default="")
    p.add_argument("--workers", type=int, default=1)

    p = sub.add_parser("recrawl")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--client", default="")

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--client", default="")

    p = sub.add_parser("status")
    p.add_argument("--client", default="")
    sub.add_parser("serve")
    sub.add_parser("reindex")
    p = sub.add_parser("strip")
    p.add_argument("client")
    p = sub.add_parser("graph")
    p.add_argument("client")
    p.add_argument("--top", type=int, default=10)
    p = sub.add_parser("entities")
    p.add_argument("client")
    p.add_argument("--type", default="")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("delete-url")
    p.add_argument("url")
    p = sub.add_parser("delete-client")
    p.add_argument("client")

    a = ap.parse_args()
    db.init_db()

    if a.cmd == "crawl":
        from urllib.parse import urlparse
        client = a.client or urlparse(a.seeds[0]).netloc.lower()
        con = db.connect()
        for s in a.seeds:
            crawler.add_seed(con, s, client)
            if a.with_sitemap:
                crawler.seed_sitemap(con, urlparse(s).netloc.lower(), client)
        db.commit(con)
        con.close()
        n = crawler.crawl(max_pages=a.max_pages, use_render=not a.no_render,
                          client=client, workers=a.workers)
        stripped = crawler.strip_boilerplate(client)
        import images as imagesmod
        imgroles = imagesmod.classify(client)
        import links as linksmod
        edges = linksmod.rebuild(client)
        import entities as entitiesmod
        nent = entitiesmod.rebuild(client)
        print(f"listo: {n} páginas (boilerplate:{stripped} images:{imgroles} "
              f"edges:{edges} entities:{nent})")
    elif a.cmd == "recrawl":
        n = crawler.recrawl(max_pages=a.max_pages,
                            client=a.client or None)
        print(f"revisita: {n} páginas procesadas")
    elif a.cmd == "search":
        for r in index.search(a.query, a.limit, client=a.client or None):
            print(f"- {r['title'] or r['url']}\n  {r['url']}\n  {r['snip']}\n")
    elif a.cmd == "status":
        con = db.connect()
        try:
            s = db.stats(con, a.client or None)
        finally:
            con.close()
        print("estados:", s["by_status"])
        print("documentos:", s["docs"], "| visitas:", s["visits"])
        for f in s["failures"]:
            print(f"FAIL x{f['retry_count']}: {f['url']} ({f['error']})")
    elif a.cmd == "reindex":
        print(f"reindexados: {crawler.reindex()} documentos")
    elif a.cmd == "graph":
        import json as _json
        import links as linksmod
        linksmod.rebuild(a.client)
        print(_json.dumps(linksmod.report(a.client, top=a.top),
                          indent=1, ensure_ascii=False)[:3000])
    elif a.cmd == "entities":
        import json as _json
        import entities as entitiesmod
        print("tipos:", entitiesmod.types_summary(a.client))
        for e in entitiesmod.by_type(a.client, a.type or None, limit=a.limit):
            print(f"- [{e['etype']}] {e['name'] or e['url'][:70]} (conf {e['confidence']})")
    elif a.cmd == "strip":
        import images as imagesmod
        import links as linksmod
        print(f"boilerplate: {crawler.strip_boilerplate(a.client)} docs; "
              f"images: {imagesmod.classify(a.client)}; "
              f"edges: {linksmod.rebuild(a.client)}")
    elif a.cmd == "delete-url":
        import replica as repmod
        con = db.connect()
        try:
            info = db.delete_url(con, a.url)
        finally:
            con.close()
        if info is None:
            print("no encontrado")
        else:
            files = repmod.delete_page(a.url, info.get("client"), info.get("raw_path"))
            print(f"eliminado {a.url} (archivos: {len(files)})")
    elif a.cmd == "delete-client":
        import replica as repmod
        con = db.connect()
        try:
            info = db.delete_client(con, a.client)
        finally:
            con.close()
        files = repmod.delete_client_files(a.client, info.get("raw_paths"))
        print(f"eliminado cliente {a.client}: {len(info.get('urls', []))} urls, "
              f"{len(files)} archivos")
    elif a.cmd == "serve":
        from panel import app
        app.run(host="127.0.0.1", port=8766, debug=False)


if __name__ == "__main__":
    sys.exit(main())
