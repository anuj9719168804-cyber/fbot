

# ---------------------------------------------------------------------------------------------------------------------
# Search by show name  (/desiserial <show>  ->  main.py: _ds.search(q) then _ds.sort_newest_first(items))
# Append this block to the END of desiserial_scan.py.
# ---------------------------------------------------------------------------------------------------------------------
SITE = "https://www.desiserial.su"
# episode page = https://www.desiserial.su/<slug>/<numeric id>/   (a show / category page has no numeric id)
_EP_URL_RE = re.compile(r"^https?://(?:www\.)?desiserial\.\w+/[^/?#]+/\d+/?$", re.I)
_A_RE = re.compile(r"<a\b([^>]*?)>(.*?)</a>", re.I | re.S)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_ATTR_TITLE_RE = re.compile(r'\btitle\s*=\s*["\']([^"\']+)["\']', re.I)
_IMG_ALT_RE = re.compile(r'<img[^>]+alt\s*=\s*["\']([^"\']+)["\']', re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*"
                      r"((?:19|20)\d{2})\b", re.I)


def _title_date(title: str):
    """'Mannat 23rd October 2026 Full Episode | Zee TV' -> datetime.date(2026, 10, 23) | None"""
    import datetime
    m = _DATE_RE.search(title or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    except ValueError:
        return None


def _show_of(title: str) -> str:
    """'Yeh Rishta Kya Kehlata Hai 23rd October 2026 Full Episode | Star Plus' -> 'Yeh Rishta Kya Kehlata Hai'"""
    t = (title or "").split("|")[0]
    m = _DATE_RE.search(t)
    if m:
        t = t[:m.start()]
    t = re.sub(r"\b(?:full\s+)?episodes?\b.*$|\bwritten\s+update\b.*$", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -:,") or (title or "").strip()


def _search_page_links(page_html: str, base_url: str) -> dict:
    """All episode links on a page -> {url: best title}. An episode is usually linked 2-3 times (poster image, heading,
    'read more'); the longest text wins."""
    out = {}
    for m in _A_RE.finditer(page_html):
        attrs, inner = m.group(1), m.group(2)
        h = _HREF_RE.search(attrs)
        if not h:
            continue
        href = urljoin(base_url, _html.unescape(h.group(1)).strip()).split("#")[0].split("?")[0]
        if not _EP_URL_RE.match(href):
            continue
        text = _clean(inner)
        if not text:
            t = _ATTR_TITLE_RE.search(attrs)
            a = _IMG_ALT_RE.search(inner)
            text = _clean(t.group(1)) if t else (_clean(a.group(1)) if a else "")
        if len(text) > len(out.get(href, "")):
            out[href] = text
        else:
            out.setdefault(href, text)
    return out


def search(query: str, max_pages: int = 3) -> list:
    """Site search (WordPress-style /?s=<name>, then /page/N/?s=<name>) -> [{"title", "url"}, ...] for episode pages whose
    title contains the words of the query. [] when nothing matches or the site can't be reached."""
    from urllib.parse import quote_plus
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if w]
    if not words:
        return []
    s = _session()
    s.headers["Referer"] = SITE + "/"
    found, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{SITE}/?s={quote_plus(query)}" if page == 1 else f"{SITE}/page/{page}/?s={quote_plus(query)}"
        try:
            r = s.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[-] desiserial search {url}: {str(e)[:100]}")
            break
        links = _search_page_links(r.text, url) if r.status_code == 200 else {}
        print(f"[desiserial] search {url}: HTTP {r.status_code}, {len(links)} episode link(s)")
        if not links:
            break
        added = 0
        for href, title in links.items():
            key = href.rstrip("/").lower()
            low = (title or href).lower()
            # slug words count too (some themes only show an image link)
            hay = low + " " + re.sub(r"[-_]+", " ", urlsplit(href).path.lower())
            if key in seen or not all(w in hay for w in words):
                continue
            seen.add(key)
            found.append({"title": title or _show_of(href), "url": href})
            added += 1
        if not added:
            break
    return found


def sort_newest_first(items: list) -> list:
    """search() result -> [{"label", "url", "title", "show"}] newest episode first (date taken from the title,
    e.g. '23rd October 2026'); episodes without a date go last, in the order the site gave them."""
    rows = []
    for pos, it in enumerate(items or []):
        title = it.get("title") or ""
        d = _title_date(title)
        label = f"{d.day} {d.strftime('%b')}" if d else (_show_of(title)[:10] or f"#{pos + 1}")
        rows.append((d, pos, {"label": label, "url": it["url"], "title": title, "show": _show_of(title)}))
    dated = sorted((r for r in rows if r[0]), key=lambda r: (r[0], -r[1]), reverse=True)
    undated = [r for r in rows if not r[0]]
    return [r[2] for r in dated + undated]
