"""Near-duplicados: SimHash de 64 bits sobre shingles de palabras."""
import hashlib
import re


def _tokens(text):
    return re.findall(r"[a-z0-9áéíóúñü]+", text.lower())


def simhash(text):
    v = [0] * 64
    toks = _tokens(text)
    if not toks:
        return "0" * 16
    for i in range(len(toks) - 2):
        h = int(hashlib.md5(" ".join(toks[i:i + 3]).encode()).hexdigest(), 16)
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    bits = "".join("1" if x > 0 else "0" for x in v)
    return f"{int(bits, 2):016x}"


def distance(a, b):
    x = int(a, 16) ^ int(b, 16)
    return bin(x).count("1")


def is_near_dup(con, client, text, threshold=4, min_words=150):
    """¿Existe un documento del cliente casi idéntico?

    Documentos finos (<min_words) se excluyen: con poco texto propio,
    la plantilla común domina el hash y produce falsos positivos
    (ej. secciones temáticas distintas del mismo sitio).
    """
    if len(text.split()) < min_words:
        return None
    h = simhash(text)
    rows = con.execute(
        """SELECT d.url, d.simhash, d.word_count FROM documents d
           JOIN pages p ON p.url = d.url
           WHERE p.client = ? AND d.simhash IS NOT NULL""", (client,)).fetchall()
    for r in rows:
        if (r["word_count"] or 0) < min_words:
            continue
        if distance(h, r["simhash"]) <= threshold:
            return r["url"]
    return None
