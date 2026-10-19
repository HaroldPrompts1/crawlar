"""Suite dorada mínima: parsers, dedup, frontera, robots y db."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crawler
import dedup
import parsers
import robots


def test_norm_quita_tracking_y_fragmento():
    u = crawler._norm("https://Example.com/Foo?utm_source=x&fbclid=y&a=1#sec")
    assert u == "https://example.com/Foo?a=1"


def test_looks_like_url():
    assert crawler.looks_like_url("https://example.com/a")
    assert not crawler.looks_like_url("notaurl")
    assert not crawler.looks_like_url("ftp://example.com")


def test_parse_html_fixture():
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "fixtures", "site", "index.html")
    with open(base, "rb") as f:
        body = f.read()
    doc, err = parsers.parse("text/html", body, "http://x/index.html")
    assert err is None
    assert "Hotel Paraíso" in doc["title"] or "Hotel" in doc["text"]
    assert "pileta climatizada" in doc["text"]
    urls = parsers.link_urls(doc["links"])
    assert any("servicios.html" in u for u in urls)


def test_parse_csv_txt_xml():
    d, e = parsers.parse("text/csv", b"a,b\n1,2\n", "http://x/c.csv")
    assert e is None and "1 | 2" in d["text"]
    d, e = parsers.parse("text/plain", b"hola mundo", "http://x/a.txt")
    assert e is None and d["text"] == "hola mundo"
    d, e = parsers.parse("text/xml", b"<r><a>hola</a></r>", "http://x/a.xml")
    assert e is None and "hola" in d["text"]
    d, e = parsers.parse("application/octet-stream", b"xx", "http://x/a.bin")
    assert d is None and e.startswith("unsupported")


def test_simhash_cercanos_y_lejanos():
    base = "el hotel tiene pileta climatizada y desayuno buffet incluido "
    a = base * 10
    b = base * 10 + "fin"
    c = "manual de soldadura industrial con electrodos y tornos " * 10
    assert dedup.distance(dedup.simhash(a), dedup.simhash(b)) <= 6
    assert dedup.distance(dedup.simhash(a), dedup.simhash(c)) > 6


def test_is_near_dup_ignora_textos_finos():
    class FakeCon:
        def execute(self, *a, **k):
            raise AssertionError("no debe consultar la DB con texto fino")
    assert dedup.is_near_dup(FakeCon(), "cli", "muy corto") is None


def test_prefilter_respeta_depth_y_robots(monkeypatch):
    import db
    rows = {"n": 0}

    class FakeCon2:
        def execute(self, q, args=()):
            class R:
                def fetchone(self):
                    return {"n": rows["n"]}
            return R()

    row = {"url": "http://example.com/a", "domain": "example.com", "depth": 99}
    cfg = {"max_depth": 3, "max_pages_domain": 200}
    assert crawler._prefilter(FakeCon2(), row, cfg) == "too_deep"

    row2 = {"url": "http://example.com/a", "domain": "example.com", "depth": 0}
    monkeypatch.setattr(robots, "allowed", lambda u, d: False)
    assert crawler._prefilter(FakeCon2(), row2, cfg) == "skipped_robots"

    rows["n"] = 500
    monkeypatch.setattr(robots, "allowed", lambda u, d: True)
    assert crawler._prefilter(FakeCon2(), row2, cfg) == "domain_cap"


def test_robots_allow_all_existe():
    import urllib.robotparser as rp
    r = rp.RobotFileParser()
    r.allow_all = True
    r.parse([])
    assert r.can_fetch("PrototipoCrawler/0.1", "http://example.com/x")


def test_db_writes_serializados_tmp(tmp_path, monkeypatch):
    import threading
    import db as dbmod
    import config
    tmpdb = str(tmp_path / "t.db")
    monkeypatch.setattr(dbmod, "DB_PATH", tmpdb)
    monkeypatch.setattr(config, "DB_PATH", tmpdb)
    con0 = dbmod.connect()
    dbmod.init_db()
    con0.close()

    def worker(i):
        con = dbmod.connect()
        try:
            with dbmod.write_locked():
                dbmod.upsert_page(con, f"http://e.com/{i}", "e.com", 0, "e.com")
                dbmod.mark_in_progress(con, f"http://e.com/{i}")
                dbmod.log_visit(con, f"http://e.com/{i}", 1, 2, 200, 10, False, None)
                dbmod.commit(con)
        finally:
            con.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    con = dbmod.connect()
    try:
        n = con.execute("SELECT COUNT(*) n FROM pages").fetchone()["n"]
        assert n == 8
    finally:
        con.close()
