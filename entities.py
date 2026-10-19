"""Entidades tipadas por schema.org: el modelo canónico del sistema.

El crawler no conoce rubros: convierte los 3 formatos estructurados
(JSON-LD, Microdata, RDFa) en entidades {tipo, nombre, atributos} con
confianza según fuente. Los perfiles verticales (fuera del crawler)
mapean estos tipos genéricos a columnas de negocio.
"""
import json

CONFIDENCE = {"json-ld": 1.0, "microdata": 0.9, "rdfa": 0.9,
              "table": 0.6, "list": 0.6}


def _short_type(t):
    if not t:
        return "Thing"
    return str(t).rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1] or "Thing"


def _flatten_ATTRS(data, prefix="", out=None, depth=0):
    """Atributos escalares con ruta (offers.price, address.street...)."""
    if out is None:
        out = {}
    if depth > 3 or len(out) >= 40:
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            if str(k).startswith("@") and k not in ("@type",):
                continue
            key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
            if isinstance(v, (dict, list)):
                _flatten_ATTRS(v, key, out, depth + 1)
            elif v is None or v == "":
                continue
            else:
                out[key] = str(v)[:300]
    elif isinstance(data, list):
        for i, x in enumerate(data[:10]):
            _flatten_ATTRS(x, f"{prefix}[{i}]", out, depth + 1)
    return out


def rebuild(client):
    """Reconstruye entities del cliente desde documents.structured."""
    import db
    con = db.connect()
    try:
        con.execute("DELETE FROM entities WHERE client=?", (client,))
        rows = con.execute(
            """SELECT d.url, d.structured FROM documents d
               JOIN pages p ON p.url=d.url WHERE p.client=?""", (client,)).fetchall()
        n = 0
        for r in rows:
            try:
                items = json.loads(r["structured"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                etype = _short_type(it.get("type"))
                data = it.get("data", {}) if isinstance(it.get("data"), dict) else {}
                name = str(it.get("name") or data.get("name") or "")[:200]
                attrs = _flatten_ATTRS(data)
                attrs.pop("@type", None)
                conf = CONFIDENCE.get(it.get("format", ""), 0.5)
                con.execute(
                    """INSERT INTO entities(client, url, etype, name, attrs,
                           confidence, source)
                       VALUES(?,?,?,?,?,?,?)""",
                    (client, r["url"], etype, name, json.dumps(attrs, ensure_ascii=False),
                     conf, it.get("format", "")))
                n += 1
                if n % 200 == 0:
                    db.commit(con)
        db.commit(con)
        return n
    finally:
        con.close()


def by_type(client, etype=None, limit=100):
    import db
    con = db.connect()
    try:
        q = "SELECT url, etype, name, attrs, confidence, source FROM entities WHERE client=?"
        a = [client]
        if etype:
            q += " AND etype=?"
            a.append(etype)
        q += " ORDER BY confidence DESC LIMIT ?"
        a.append(limit)
        return [dict(r) for r in con.execute(q, a).fetchall()]
    finally:
        con.close()


def types_summary(client):
    import db
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            """SELECT etype, COUNT(*) n, AVG(confidence) conf
               FROM entities WHERE client=? GROUP BY etype ORDER BY n DESC""",
            (client,)).fetchall()]
    finally:
        con.close()
