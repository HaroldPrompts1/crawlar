"""Borrado completo: URL individual y cliente entero (DB + archivos)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbmod


def _tmpdb(tmp_path, monkeypatch):
    import config
    tmpdb = str(tmp_path / "del.db")
    monkeypatch.setattr(dbmod, "DB_PATH", tmpdb)
    monkeypatch.setattr(config, "DB_PATH", tmpdb)
    con0 = dbmod.connect()
    dbmod.init_db()
    con0.close()


def _seed(con, url, client):
    dbmod.upsert_page(con, url, "e.com", 0, client)
    dbmod.mark_ok(con, url, 200, "text/html", None, None,
                  "ch", "th-" + url, "T", True, raw_path=None)
    dbmod.save_document(con, url, "T", "texto con más de ciento cincuenta palabras " * 20,
                        [], [], [], [], "ab12")
    dbmod.log_visit(con, url, 1, 2, 200, 10, False, None)
    con.execute("INSERT INTO entities(client, url, etype, name, attrs, confidence, source)"
                " VALUES(?,?,?,?,?,?,?)", (client, url, "Thing", "N", "{}", 1.0, "json-ld"))
    con.execute("INSERT INTO links(client, from_url, to_url, anchor, rel, zone)"
                " VALUES(?,?,?,?,?,?)", (client, url, url + "/otro", "a", "", "main"))
    dbmod.commit(con)


def test_delete_url_borra_todo(tmp_path, monkeypatch):
    _tmpdb(tmp_path, monkeypatch)
    con = dbmod.connect()
    try:
        _seed(con, "http://e.com/a", "e.com")
        info = dbmod.delete_url(con, "http://e.com/a")
        assert info is not None and info["client"] == "e.com"
        assert con.execute("SELECT COUNT(*) n FROM pages").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) n FROM documents_fts").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) n FROM visits").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) n FROM entities").fetchone()["n"] == 0
        assert con.execute("SELECT COUNT(*) n FROM links").fetchone()["n"] == 0
        assert dbmod.delete_url(con, "http://e.com/a") is None
    finally:
        con.close()


def test_delete_client_borra_todo(tmp_path, monkeypatch):
    _tmpdb(tmp_path, monkeypatch)
    con = dbmod.connect()
    try:
        _seed(con, "http://e.com/a", "e.com")
        _seed(con, "http://e.com/b", "e.com")
        _seed(con, "http://o.com/a", "o.com")
        info = dbmod.delete_client(con, "e.com")
        assert len(info["urls"]) == 2
        left = [r[0] for r in con.execute("SELECT url FROM pages").fetchall()]
        assert left == ["http://o.com/a"]
        assert con.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 1
        assert con.execute("SELECT COUNT(*) n FROM entities WHERE client='e.com'").fetchone()["n"] == 0
    finally:
        con.close()


def test_replica_delete_page_no_lanza(tmp_path, monkeypatch):
    import replica as repmod
    import config
    monkeypatch.setattr(config, "REPLICA_DIR", str(tmp_path / "rep"))
    monkeypatch.setattr(repmod, "REPLICA_DIR", str(tmp_path / "rep"))
    url = "http://e.com/a"
    dest = repmod.build_page(url, {"title": "T", "text": "texto largo " * 20,
                                   "headings": [], "links": [], "images": [],
                                   "structured": []}, "e.com")
    assert os.path.exists(dest)
    removed = repmod.delete_page(url, "e.com", raw_path=None)
    assert removed and not os.path.exists(dest)
