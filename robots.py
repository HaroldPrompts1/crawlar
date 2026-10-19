"""robots.txt + descubrimiento de sitemaps, con caché por dominio."""
import urllib.robotparser as robotparser
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from config import REQUEST_TIMEOUT, USER_AGENT

_robots_cache = {}
_delay_cache = {}
_sitemaps_cache = {}


def _load(domain):
    """Lee robots.txt: reglas + crawl-delay + directivas Sitemap: (RFC 9309)."""
    rp = robotparser.RobotFileParser()
    delay, sitemaps = None, []
    for scheme in ("https", "http"):
        try:
            r = httpx.get(f"{scheme}://{domain}/robots.txt",
                          timeout=REQUEST_TIMEOUT,
                          headers={"User-Agent": USER_AGENT},
                          follow_redirects=True)
            if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                rp.parse(r.text.splitlines())
                for line in r.text.splitlines():
                    l = line.strip()
                    if l.lower().startswith("crawl-delay:"):
                        try:
                            delay = float(l.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                    elif l.lower().startswith("sitemap:"):
                        sm_url = l.split(":", 1)[1].strip()
                        if sm_url:
                            sitemaps.append(sm_url)
                break
            elif r.status_code in (401, 403, 404):
                rp.allow_all = True
                break
        except Exception:
            continue
    else:
        rp.allow_all = True
    _robots_cache[domain] = rp
    _delay_cache[domain] = delay
    _sitemaps_cache[domain] = sitemaps
    return rp, delay, sitemaps


def allowed(url, domain):
    if domain not in _robots_cache:
        _load(domain)
    try:
        return _robots_cache[domain].can_fetch(USER_AGENT, url)
    except Exception:
        return True


def crawl_delay(domain, default=1.0):
    from config import PER_DOMAIN_DELAY
    if domain not in _delay_cache:
        _load(domain)
    d = _delay_cache.get(domain)
    base = default if default is not None else PER_DOMAIN_DELAY
    return max(base, d) if d else base


_adaptive = {}


def smart_delay(domain, floor=None):
    """Cortesía adaptativa: piso = crawl-delay del sitio, techo por AIMD.

    El delay no se hardcodea: parte del piso que declara cada sitio y
    sube/baja según sus respuestas (429/5xx/timeout ⇒ ×2, ok ⇒ −10%)."""
    if floor is None:
        floor = crawl_delay(domain)
    st = _adaptive.setdefault(domain, {"delay": floor})
    st["delay"] = max(floor, st["delay"])
    return st["delay"]


def note_result(domain, ok):
    st = _adaptive.setdefault(domain, {"delay": crawl_delay(domain)})
    if ok:
        st["delay"] = max(crawl_delay(domain), st["delay"] * 0.9)
    else:
        st["delay"] = min(30.0, max(st["delay"] * 2.0, 2.0))


def _fetch_xml(url):
    try:
        r = httpx.get(url, timeout=REQUEST_TIMEOUT,
                      headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        return ET.fromstring(r.content)
    except ET.ParseError:
        return None


def sitemap_urls(domain, limit=500):
    """Semillas [(url, lastmod_ts)] desde robots:Sitemap + /sitemap.xml + índices."""
    found, seen = [], set()

    def add(u, lastmod=None):
        if u and u not in seen:
            seen.add(u)
            try:
                ts = datetime.fromisoformat(
                    lastmod.replace("Z", "+00:00")).timestamp() if lastmod else 0
            except Exception:
                ts = 0
            found.append((u.strip(), ts))

    for sm in _declared_sitemaps(domain) + [f"https://{domain}/sitemap.xml",
                                            f"http://{domain}/sitemap.xml"]:
        if len(found) >= limit:
            break
        root = _fetch_xml(sm)
        if root is None:
            continue
        if root.tag.endswith("sitemapindex"):
            for loc in root.iter():
                if loc.tag.endswith("loc") and loc.text and len(found) < limit:
                    sub = _fetch_xml(loc.text.strip())
                    if sub is not None:
                        _collect_urls(sub, add, limit, found)
        else:
            _collect_urls(root, add, limit, found)
        if found:
            break
    # recientes primero
    found.sort(key=lambda x: x[1], reverse=True)
    return found[:limit]


def _collect_urls(root, add, limit, found):
    for url_el in root.iter():
        if not url_el.tag.endswith("url"):
            continue
        loc, lastmod = None, None
        for child in url_el:
            if child.tag.endswith("loc"):
                loc = child.text
            elif child.tag.endswith("lastmod"):
                lastmod = child.text
        if loc and len(found) < limit:
            add(loc, lastmod)


def _declared_sitemaps(domain):
    if domain not in _sitemaps_cache:
        _load(domain)
    return list(_sitemaps_cache.get(domain, []))
