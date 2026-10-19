# Crawler multi-formato + réplica simplificada (prototipo local)

Prototipo local de rastreo web con permiso: frontera BFS ponderada → descarga condicional → parse multi-formato a documento normalizado → índice SQLite + FTS5 + PageRank interno → réplica HTML propia sin CSS/JS original. Panel Flask local para operar y auditar.

Verificado: `12 passed` en `pytest tests/ -q` (Python 3.9, SQLite con FTS5).

## 1. Arquitectura y flujo

```
semilla (CLI/panel) ─┐
sitemap.xml ─────────┼─→ frontera `pages(status=pending)` ─→ _prefilter ─→ _polite ─→ fetcher.fetch
                     │                                              │ robots.allowed  │ ETag/Last-Modified
                     │                                              │ depth/cap       │ 304 / 200 / error
                     │                                              ▼                 ▼
                     │                                         skip (robots/depth/  parse por Content-Type (parsers.py)
                     │                                         cap/trampa)           + render Playwright si HTML fino
                     │                                                               │
                     │                                                               ▼
                     │                                         canónica → content_hash/text_hash (SHA-256)
                     │                                         → near-dup SimHash → save_document + FTS
                     │                                         → replica.build_page → expansión BFS (links main/chrome)
                     │
                     └─ reindex: re-parsea data/raw sin red · recrawl: pending + condicionales + hash
```

Módulos (`*.py` en raíz):

| Módulo | Responsabilidad |
|---|---|
| `crawler.py` (636 l.) | Frontera, normalización, cortesía, orquestación `_crawl_one`, `strip_boilerplate`, `reindex`, `recrawl` |
| `db.py` | SQLite WAL como fuente de verdad, `write_lock` + `with_retry`, schema, `upsert_page`, `link_priority`, `next_batch`, `mark_*`, `save_document`, `stats`, `client_config`, `delete_*` |
| `fetcher.py` | `httpx.stream GET` con `If-None-Match`/`If-Modified-Since`, tope streaming `MAX_BYTES`, `FetchResult`, `RETRYABLE` |
| `parsers.py` | 7 parsers → mismo dict `{title,text,headings,links,images,kind,canonical,structured}`; HTML = trafilatura+bs4, JSON-LD/Microdata/RDFa/tablas/listas, OG fallback |
| `robots.py` | `robots.txt` RFC 9309 + `Crawl-delay` + `Sitemap:`, caché por dominio, `smart_delay` AIMD, `sitemap_urls` |
| `renderer.py` | Playwright/Chromium thread-local, solo fallback SPA; captura HTML + `inner_text` visible + blobs JSON XHR |
| `dedup.py` | SimHash 64-bit sobre trigramas, `distance` Hamming, `is_near_dup(threshold=4, min_words=150)` |
| `index.py` | `search`: BM25 FTS5 con pesos por columna + re-rank PageRank+frescura; `export_corpus` a `.md` |
| `links.py` | Reconstruye `links`, `pagerank(damping=0.85, iters=30)` intra-dominio, `report` (huérfanas, anchors, depth) |
| `entities.py` | `rebuild` desde `documents.structured` → tabla `entities` con `CONFIDENCE {json-ld:1.0, microdata:0.9, rdfa:0.9, table/list:0.6}` |
| `images.py` | `classify(ratio=0.6, min_docs=3)`: `content/chrome/tracking` por frecuencia cross-page, tabla `site_assets` |
| `replica.py` | `save_raw` auditable + `build_page` propia (720 px, sin CSS/JS orig.) + borrado en cascada |
| `panel.py` | Flask `127.0.0.1:8766`, 8 tabs, jobs con `crawl_mutex` (un escritor), ~20 endpoints `/api/*` |
| `cli.py` | 10 subcomandos (crawl/recrawl/search/status/serve/reindex/strip/graph/entities/delete-*) |
| `config.py` | Defaults globales; override por cliente en `client_config` vía `db.get_config/set_config` |

## 2. Requisitos e instalación

`requirements.txt`: `httpx>=0.28`, `beautifulsoup4>=4.13`, `trafilatura>=2.0`, `lxml_html_clean>=0.4`, `pymupdf>=1.26`, `python-docx>=1.2`, `openpyxl>=3.1`, `playwright>=1.60`, `flask>=3.1`, `pytest>=8.0`.

```bash
pip install -r requirements.txt
playwright install chromium   # solo necesario si se usa render JS (SPAs / 403-429)
python3 cli.py status         # crea data/crawl.db vía db.init_db()
```

Directorios auto-creados (`config.py`): `data/` (`crawl.db`), `data/raw/` (crudo), `data/replica/<client>/` (+`assets/<slug>/`). Ignorados por git (`.gitignore`: `data/`, `__pycache__/`).

## 3. Uso CLI

```bash
python3 cli.py crawl URL_SEMILLA [--max-pages 50] [--with-sitemap] [--no-render] [--client ID] [--workers N]
python3 cli.py recrawl [--max-pages 200] [--client ID]
python3 cli.py reindex
python3 cli.py search "pileta climatizada" [--limit 10] [--client ID]
python3 cli.py status [--client ID]
python3 cli.py strip CLIENT
python3 cli.py graph CLIENT [--top 10]
python3 cli.py entities CLIENT [--type Tipo] [--limit 20]
python3 cli.py delete-url URL
python3 cli.py delete-client CLIENT
python3 cli.py serve   # http://127.0.0.1:8766
```

Semántica exacta (`cli.py:53-73`, `panel.py:45-100`):

- `crawl`: `client = --client o netloc(seed).lower()`; `add_seed` + opcional `seed_sitemap`; `crawl(...)`; post-paso siempre: `strip_boilerplate → images.classify → links.rebuild → entities.rebuild`.
- `recrawl`: marca `status='ok' → 'pending' ORDER BY last_crawled` y re-ejecuta `crawl` (revalida con ETag/Last-Modified y `text_hash`; solo reindexa lo cambiado).
- `reindex`: re-parsea `raw_path` existente con parsers actuales, sin red; regenera `documents`+FTS+réplica.
- `strip CLIENT`: `strip_boilerplate + images.classify + links.rebuild` sin descargar.
- `delete-url/delete-client`: borran DB (`pages, documents, documents_fts, visits, entities, links, site_assets/client_config` según alcance) + réplica + assets + crudos (`replica.delete_*`, nunca lanzan).

## 4. Frontera y cortesía (`crawler.py`, `robots.py`, `config.py`)

- **Canónica sintáctica** `_norm(url)` (`crawler.py:92-104`): `urldefrag` + filtra query `TRACKING={utm_source,utm_medium,utm_campaign,utm_term,utm_content,fbclid,gclid,msclkid,sessionid,sid}` + host en minúsculas + strip `:80/:443` por defecto + `path or "/"`. `looks_like_url`: esquema `http/https` y `"." in netloc`.
- **Prioridad** `db.link_priority(depth,inlinks,sitemap_boost,zone_weight)` (`db.py:172-197`):
  `score = sitemap_boost[0..1] + 2.0/(1+depth) + 0.5*log1p(inlinks) + zone_weight`
  con `ZONE_WEIGHT={main:0.5, chrome:0.1}`. `upsert_page`: inserta (`inlinks=0` si `depth==0` else `1`); si existe y está `pending/fetch_error`: `depth=min`, `inlinks++`, `sitemap_boost=max`, `priority=max`. Si ya está `ok`/skip no se toca (no duplica trabajo).
- **Orden de consumo** `next_batch` (`db.py:247-264`): `pending` o `fetch_error` con `next_retry_at<=now`, `ORDER BY priority DESC, depth, first_seen`. Claim atómico `UPDATE ... WHERE status='pending' → 'in_progress'` (`_claim`).
- **Filtros** `_prefilter` (`crawler.py:265-276`): `depth>max_depth → too_deep`; `COUNT ok por dominio>=max_pages_domain → domain_cap`; `not robots.allowed → skipped_robots`. Anti-trampas `_is_trap` (`crawler.py:37-47`): substring `/carrito,/cart,/checkout,/mi-cuenta,/my-account,/wp-admin,/wp-login,?s=,?search,?q=,/calendar,/calendario,?tribe_,/eventos/?,cdc-calendar,?add-to-cart,&add-to-cart`. Tope `MAX_LINKS_PER_PAGE=100`, `main` antes que `chrome`.
- **Sitemap** (`robots.py:107-160`, `crawler.py:50-89`): fuentes = `Sitemap:` de robots + `/sitemap.xml` (https luego http); soporta `sitemapindex` recursivo; devuelve `[(url, lastmod_ts)]` ordenado reciente-primero (`limit=500`). Boost `sitemap_priorities`: sin fechas → `0.2`; con fechas → `0.2+0.8*(t-mn)/(mx-mn)`; una sola fecha → `0.5`. Semillas entran con `depth=1` (la homepage es la única `depth=0`).
- **Cortesía**: `_polite` = `sleep(smart_delay - elapsed)` con lock por dominio. `smart_delay` (`robots.py:73-90`): piso = `crawl-delay` del sitio (`max(default, declarado)`, default `PER_DOMAIN_DELAY=1.0`); AIMD por `note_result`: `ok → max(piso, delay*0.9)`, fallo → `min(30, max(delay*2, 2.0))`. Transacción a `in_progress` con commit **antes** de I/O para no retener lock en renders de hasta 60 s.

Defaults globales (`config.py:11-20`): `REQUEST_TIMEOUT=30 s`, `RENDER_TIMEOUT=60 s`, `RENDER_MIN_CHARS=200`, `PER_DOMAIN_DELAY=1.0 s`, `MAX_DEPTH=3`, `MAX_PAGES_PER_DOMAIN=200`, `MAX_BYTES=15 MiB`, `MAX_RETRIES=3`, `BACKOFF=[2,8,30] s`, `USER_AGENT="PrototipoCrawler/0.1 (+uso local con permiso del cliente)"`.

## 5. Descarga (`fetcher.py`)

`fetch(url, etag, last_modified)` → `FetchResult(status,headers,body,not_modified,error,final_url)`:

- Envía `User-Agent` + `If-None-Match`/`If-Modified-Since` si hay valores guardados. `304 → not_modified=True, body=b""`.
- Streaming `httpx.stream(..., follow_redirects=True)`, chunks 64 KiB; si `total>MAX_BYTES → error="too_big:{total}", body=b""` (luego `skipped:too_big`, no reintento).
- Errores tipados: `timeout`, `connect_error`, `fetch:{ExcName}`. Reintentables: `error in (timeout,connect_error)` o `status in RETRYABLE={408,425,429,500,502,503,504}`.
- En `_crawl_one`: `401/403/429` + `use_render` → intenta `renderer.render` antes de fallar; `404 → skipped:not_found`; `>=400` restante → `mark_fail` (reintentable si en `RETRYABLE` o venía bloqueado). Cada visita se registra en `visits(started,finished,http_status,bytes,used_render,error)`.

## 6. Parsers → documento normalizado (`parsers.py`)

`parse(content_type, body, url) → (doc, None) | (None, "unsupported:{ctype}" | "parse:{Exc}")`. Si el header no mapea, fallback por extensión (`.pdf/.docx/.doc/.xlsx/.xls/.csv/.xml/.txt/.md`). Todos devuelven `{title,text,headings[≤30 h1-h3],links[≤300 {url,anchor≤150,rel,zone}],images[≤60 {src,alt≤200,w,h,link,zone}],kind,canonical?,structured[≤15]}`.

- **HTML** (`parse_html`): `trafilatura.extract(include_tables=True)` como base precisa + fusión `_merge_texts` con `bs4.get_text` (agrega oraciones `>40` chars no contenidas si el fallback es >10% más largo). Limpieza `_clean`: colapsa whitespace. Elimina `script/style/nav/footer` solo para texto; links/imágenes/headings se extraen del DOM intacto con zona `main|chrome` según ancestros `header/footer/nav`. `title = <title> o h1 o og:title`. `canonical` resuelto a absoluto.
- **Estructurados** (todos acaban en `entities`): JSON-LD (`@graph` aplanado, ≤10) + Microdata (raíces `itemscope`, owner = itemscope más cercano) + RDFa Lite (`typeof/property`) + tablas/`dl`/`ul-ol` como `StructuredValue` (`format: table|list`, pares `≤25`, `k≤80/v≤300`, tablas ≥2 pares, listas ≥3). `_walk_json_images` caza fotos en JSON con keys multilingües (`image,photo,foto,imagen,picture,thumbnail,thumb,contenturl,url_foto,...,gallery,galeria,slides`) → imágenes `from: json-ld` (≤40, primero). OpenGraph (`og:title/description/image`, `twitter:*`) como respaldo.
- **Binarios**: PDF vía PyMuPDF (`page.get_text()` por página); DOCX vía `python-docx` (párrafos); XLSX vía `openpyxl` (`read_only,data_only`, `# hoja` + filas `a | b`); CSV (≤500 filas, ` | `); TXT/MD (`utf-8/replace`); XML (`itertext`).

## 7. Render SPA (fallback, `renderer.py` + `crawler.py:384-431`)

Solo si `use_render` y `ctype` es HTML:

1. Parse rápido `probe`; si `len(text)<render_min_chars(200)` (default, configurable por cliente) o cáscara JS (`len(body)>render_min_html(15000)` y `densidad=len(text)/len(body)<render_min_density(0.02)`) → `render_with_api`.
2. `render_with_api`: browser Chromium **thread-local** (`workers>1` → un browser por hilo), `new_page` por URL, intercepta `response` JSON no-analytics (`skip_hosts`: `google-analytics,googletagmanager,doubleclick,facebook.com/tr,hotjar,analytics`; `max_blobs=8`, `200<len<512 KiB`), `goto(domcontentloaded)` + scroll abajo/arriba + `settle 1000 ms + 500 ms`; devuelve `(html, error, blobs_json, inner_text visible)`.
3. Fusión: `visible` (lo que el navegador muestra) reemplaza al texto si es más largo; `links = merge_links(probe, rendered)` (por URL, ≤300); texto XHR: `_walk_json_text` (descarta `color,style,token,url,src,icon,...`, hashes/tokens, códigos) hasta 200 partes, truncado a 20000 chars; imágenes XHR hasta 20 (`zone:main, from:xhr`). Se adopta el render solo si su texto supera al `probe`; `used_render=True` queda en `visits`.
4. Caso `401/403/429` en fetch directo: `renderer.render(url)` simple y se reinyecta como `FetchResult(200, text/html, html_bytes)`.

## 8. Cambio, dedup y réplica (`crawler.py:446-513`, `dedup.py`, `replica.py`)

- Hashes: `content_hash=SHA256(body)`, `text_hash=SHA256(texto)`. `304 → mark_ok(304, hashes viejos, changed=False)`. Si cambia (o no hay `documents`): `dedup.is_near_dup` → si `dup_of != target`, `mark_skipped(near_dup)`; si no, `save_document` + `need_replica=True`. Réplica se construye **después** del commit DB (nunca dentro de la tx).
- `dedup`: `simhash` 64-bit (trigramas de `[a-z0-9áéíóúñü]+`, MD5 por shingle, voto por bit, hex 16). `distance` = Hamming. `is_near_dup(con,client,text,threshold=4,min_words=150)`: ignora textos `<150` palabras (evita falsos positivos por plantilla) y compara solo contra docs del cliente con `word_count>=150`.
- `strip_boilerplate(client, ratio=0.6, min_docs=5, width=10)`: shingles de 10 palabras presentes en `>=max(2, n_docs*ratio)` docs → se borran, se reescribe `documents+FTS` y se regeneran réplicas fuera de tx (commit cada 25).
- `save_raw(url,ctype,body)`: `data/raw/<sha256(url)[:16]>.{html,pdf,bin}`. `build_page(url,doc,client,identity)`: `data/replica/<client>/<slug>.html` (`slug = netloc+path sanitizado [^A-Za-z0-9_-]→-, ≤80`), plantilla propia `max-width:720px`, `headings[:10]`, párrafos `split(". ")[:40]` con `len>40`, imágenes `[:8]` solo `role==content` espejadas a `assets/<slug>/` (descarga ≤3 MiB, percent-encoding para `ñ/?`, `tracking` jamás incluido), ficha `_ficha` con `_flat` genérico (prof. ≤3, ≤20 filas). `identity` (logos `role=chrome`) solo en cabecera.

## 9. Índice, ranking y grafo (`index.py`, `links.py`, `entities.py`, `images.py`)

- **Búsqueda** `index.search(query, limit=10, client)`: `bm25(documents_fts, w_url, w_title, w_text)` con `fts_weights` default `[3.0,2.0,1.0]` (URL manda, luego título, luego texto) + `snippet(..., '<b>','</b>','…',30)`. Fallback a query literal `"..."` si FTS5 falla por sintaxis. Re-rank: `score = wb*bm25_norm_inv + wp*pagerank + wf*frescura` con `rank_weights` default `[0.6,0.25,0.15]` (normalización min-max por lote; frescura = `-(now-last_changed)`). `client_config` permite ajustar ambos vectores por cliente.
- **Grafo** `links.rebuild(client)` materializa `links(client,from_url,to_url,anchor,rel,zone)` desde `documents.links`. `pagerank(damping=0.85, iters=30)`: solo aristas mismo-dominio con ambos extremos `status='ok'`; dangling reparte a todos. `report`: `pagerank_top`, `orphans` (ok, no-semilla, `indeg==0`, ≤50 listadas), `depth_hist`, `generic_anchors` (stoplist `click aquí,ver más,aquí,...`), `top_anchors`, `externals`, `edges`.
- **Entidades** `entities.rebuild(client)`: `DELETE+INSERT` desde `documents.structured`; `_flatten_ATTRS` con rutas (`offers.price`, prof. ≤3, ≤40 attrs, listas ≤10); `confidence` por `CONFIDENCE`; `by_type/types_summary` para CLI/panel.
- **Imágenes** `images.classify(client)`: `norm_src` = esquema+host minúsculas sin query (ignora cache-busters); `TRACKING_HINTS=(facebook.com/tr,google-analytics,googletagmanager,doubleclick,hotjar,beacon,pixel)` → `tracking`; frecuencia `>=max(2, n_docs*0.6)` → `chrome`, resto `content`. Requiere `>=3` docs. Persiste roles en `documents.images` + `site_assets(client,src_norm,src_seen,alt,pages_count,role,caption)` (upsert) y regenera réplicas con identidad chrome (`_apply_identity`).

## 10. Persistencia (`db.py`)

SQLite `data/crawl.db`, `connect()`: `timeout=30`, `check_same_thread=False`, `isolation_level=None` (autocommit), `PRAGMA journal_mode=WAL, busy_timeout=30000, synchronous=NORMAL`. Escritura serializada: `write_locked()` (`RLock` global) + `with_retry(tries=4, base 0.05 s exponencial)` ante `database is locked`; `commit()` igual.

Tablas: `pages(url PK, client, domain, depth, status, http_status, content_type, final_url, canonical, priority, inlinks, sitemap_boost, etag, last_modified, content_hash, text_hash, raw_path, title, first_seen, last_crawled, last_changed, retry_count, next_retry_at, error)` + índices `(client,status,depth,first_seen)`, `(domain,status)`; `documents(url PK, title, text, headings/links/images/structured JSON, simhash, word_count, indexed_at)`; `documents_fts(url,title,text)` FTS5 `unicode61`; `visits(id, url, started_at, finished_at, http_status, bytes, used_render, error)`; `links`, `entities`, `site_assets`, `client_config(client, cfg JSON)`.

Estados `pages.status`: `pending | in_progress | ok | fetch_error | too_deep | domain_cap | skipped_robots | too_big | not_found | unsupported_type | near_dup | alias_of_canonical | <custom>`. `mark_ok` resetea `retry_count/error` y actualiza `last_changed` solo si `changed`. `mark_fail(error,http,retryable)`: si reintentable y `n<=MAX_RETRIES` → `fetch_error` con `next_retry_at=now+BACKOFF[n-1]`; si no → cuarentena (`next_retry_at=NULL`, visible en `status`/`/api/stats`). `init_db()` aplica schema, migra columnas (`client,final_url,canonical,priority,inlinks,sitemap_boost,structured,simhash`), backfill `client=domain` y rescata `in_progress→pending (rescued_after_restart)`.

Config por cliente (`DEFAULTS` en `db.py:387-400`): `max_depth=3, delay_floor=1.0, max_pages_domain=200, render_min_chars=200, render_min_density=0.02, render_min_html=15000, max_retries=3, backoff=[2,8,30], boiler_ratio=0.6, workers=1, rank_weights=[0.6,0.25,0.15], fts_weights=[3.0,2.0,1.0]`. `get_config` = defaults + JSON guardado; `set_config` solo acepta claves conocidas. Panel `⚙️ Ajustes` y `GET/POST /api/config` lo editan aislado por cliente.

## 11. Panel local (`panel.py` + `static/`)

`python3 cli.py serve` → Flask `http://127.0.0.1:8766` (`static/index.html+app.js+style.css`, 8 tabs: Dashboard, Crawler Live, Búsqueda FTS, Documentos, Enlaces & Grafo, Imágenes & Assets, Errores, Ajustes).

- Jobs: `POST /api/crawl {seeds,max_pages,use_render,with_sitemap,workers}` (normaliza `dominio → https://`), `POST /api/recrawl`, `POST /api/cancel/<job>`, `GET /api/job/<job>` (`status,done,planned,current,events[-60]`; anillo 300 con timestamp `HH:MM:SS`). `crawl_mutex` (no bloqueante): un solo job de escritura; si ocupado → `finished {error:"busy:..."}`. `should_stop`/`cancel` cooperativo chequeado por lote/página.
- Lecturas: `GET /api/dashboard {words_total,clients_count,clients}`, `/api/clients`, `/api/stats` (=`db.stats`: `by_status,docs,visits,failures[≤50],recent[≤20]`), `/api/docs`, `/api/doc?url=`, `/api/search?q=&client=`, `/api/graph?client=`, `/api/entities?client=&type=&limit=`, `/api/assets?client=`, `GET/POST /api/config`, `POST /api/retry {url|all,client}`, `POST /api/strip|/api/reindex`, `DELETE /api/doc?url=` y `/api/client?client=` (con `crawl_mutex`, cancelan jobs del cliente), `GET /replica/<path>` (sirve `data/replica/`), `/favicon.ico → 204`.

## 12. Tests y fixtures

```bash
python3 -m pytest tests/ -q
```

- `tests/test_core.py`: `_norm` quita tracking/fragmento y minúsculas host; `looks_like_url`; `parse_html` sobre `fixtures/site/index.html` (título/texto `Hotel Paraíso/pileta climatizada`, link a `servicios.html`); CSV/TXT/XML ok + `unsupported` binario; SimHash cercano `≤6` / lejano `>6`; `is_near_dup` no toca DB con texto fino; `_prefilter` (`too_deep/domain_cap/skipped_robots`); `allow_all` robotparser; escrituras concurrentes (8 hilos → 8 `pages`, valida `write_lock`).
- `tests/test_delete.py`: `delete_url` (7 tablas a 0 + `None` en segunda llamada); `delete_client` aísla por cliente; `replica.delete_page` borra archivo sin lanzar.
- `fixtures/site/`: `index.html, servicios.html, app.html, catalogo.csv, folleto.pdf, info.txt, robots.txt, sitemap.xml` (URLs `http://127.0.0.1:8765/...`) para pruebas offline.

## 13. Límites conocidos

- Render exige Chromium de Playwright; sin él, SPAs puras y `403/429` con antibot quedan en `fetch_error`/texto fino.
- `MAX_BYTES=15 MiB` en streaming: binarios grandes → `too_big` sin descarga completa ni índice.
- `dedup` es O(N) por página y `pagerank` itera en memoria: prototipo local, no pensado para >miles de docs por cliente sin paginar.
- `sitemap_urls(limit=500)` y `MAX_LINKS_PER_PAGE=100` acotan la frontera; sitios con paginación infinita/facetas dependen además de `TRAP_SUBSTR`.
- Réplica es extractiva y propia por diseño (texto+`StructuredValue`+`content` espejado): no preserva layout, CSS/JS, formularios ni multimedia no-`<img>`.
