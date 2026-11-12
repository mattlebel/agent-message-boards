"""Sites without feeds: page change detection, sitemaps, Shopify stores, eBay, Claude web search."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from lxml import etree
from pydantic import Field

from app import config
from app import llm as llm_models
from app.db import utcnow
from app.sources.base import RawItem, SourceError, get, html_to_text

# --- page_watch -----------------------------------------------------------------


def _page_lines_and_links(html: str, base_url: str, selector: Optional[str]):
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(selector) if selector else soup.body or soup
    if root is None:
        raise SourceError(f"selector {selector!r} matched nothing")
    for tag in root(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()
    lines = []
    for line in root.get_text("\n").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 3 and line not in lines:
            lines.append(line)
    links = {}
    for a in root.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        label = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        if href.startswith("http") and href not in links:
            links[href] = label[:120]
    return lines[:1500], links


def fetch_page_watch(source, session) -> list[RawItem]:
    """Report what was added to a page since the last check."""
    cfg = source.config or {}
    url = cfg.get("url")
    if not url:
        raise SourceError("page_watch needs a url")
    resp = get(url)
    lines, links = _page_lines_and_links(resp.text, str(resp.url), cfg.get("selector"))
    state = dict(source.state or {})
    old_lines, old_links = state.get("lines"), state.get("links")
    source.state = {**state, "lines": lines, "links": links, "checked": utcnow().isoformat()}
    if old_lines is None:
        return []  # first run: just capture a baseline
    old_set = set(old_lines)
    added = [line for line in lines if line not in old_set]
    removed = [line for line in old_lines if line not in set(lines)]
    new_links = {h: t for h, t in links.items() if h not in (old_links or {})}
    if not added and not new_links:
        return []
    added_text = "\n".join(added)
    if len(added_text) < int(cfg.get("min_change_chars", 15)) and not new_links:
        return []
    digest = hashlib.sha1((added_text + "|".join(sorted(new_links))).encode()).hexdigest()[:16]
    body = "Added on the page:\n" + added_text[:2500]
    if new_links:
        body += "\n\nNew links:\n" + "\n".join(f"- {t or '(link)'}: {h}" for h, t in list(new_links.items())[:25])
    if removed:
        body += "\n\nRemoved:\n" + "\n".join(removed[:15])[:600]
    first_link = next(iter(new_links), url)
    return [
        RawItem(
            key=f"page:{source.id}:{digest}",
            title=f"Change on {source.name or urlparse(url).netloc}: {(added[0] if added else next(iter(new_links.values()), ''))[:90]}",
            url=first_link,
            body=body[:4000],
            extra={"page": url, "kind": "page_change"},
        )
    ]


# --- sitemap -----------------------------------------------------------------------


def _sitemap_urls(url: str, depth: int = 0) -> list[tuple[str, str]]:
    resp = get(url)
    try:
        root = etree.fromstring(resp.content)
    except etree.XMLSyntaxError as e:
        raise SourceError(f"bad sitemap XML at {url}: {e}") from e
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    out: list[tuple[str, str]] = []
    if root.tag.endswith("sitemapindex") and depth < 1:
        for loc in root.findall("s:sitemap/s:loc", ns)[:15]:
            out += _sitemap_urls(loc.text.strip(), depth + 1)
        return out
    for node in root.findall("s:url", ns):
        loc = node.find("s:loc", ns)
        mod = node.find("s:lastmod", ns)
        if loc is not None and loc.text:
            out.append((loc.text.strip(), (mod.text or "").strip() if mod is not None else ""))
    return out


def _page_title(url: str) -> tuple[str, str]:
    try:
        resp = get(url)
        soup = BeautifulSoup(resp.text, "lxml")
        title = (soup.title.string or "").strip() if soup.title else ""
        return title, html_to_text(resp.text, 1500)
    except Exception:
        return "", ""


def fetch_sitemap(source, session) -> list[RawItem]:
    """New pages showing up on a site (e.g. a new machine location page)."""
    cfg = source.config or {}
    url = cfg.get("url")
    if not url:
        raise SourceError("sitemap needs a url")
    entries = _sitemap_urls(url)
    if cfg.get("include"):
        entries = [(u, m) for u, m in entries if re.search(cfg["include"], u, re.I)]
    state = dict(source.state or {})
    seen = set(state.get("seen") or [])
    first_run = not state.get("seen")
    current = [u for u, _ in entries]
    source.state = {**state, "seen": current[-5000:]}
    if first_run:
        return []
    items = []
    for page_url, lastmod in entries:
        if page_url in seen:
            continue
        title, text = _page_title(page_url) if len(items) < 10 else ("", "")
        slug = urlparse(page_url).path.strip("/").split("/")[-1].replace("-", " ")
        items.append(
            RawItem(
                key=f"sitemap:{page_url}",
                title=f"New page: {title or slug or page_url}",
                url=page_url,
                body=text,
                extra={"lastmod": lastmod, "kind": "new_page"},
            )
        )
    return items[:30]


# --- shopify -----------------------------------------------------------------------


def fetch_shopify(source, session) -> list[RawItem]:
    """New products on a Shopify store (most small shops expose /products.json)."""
    cfg = source.config or {}
    base = (cfg.get("url") or "").rstrip("/")
    if not base:
        raise SourceError("shopify needs the store url")
    resp = get(f"{base}/products.json", params={"limit": 250})
    try:
        products = resp.json().get("products", [])
    except ValueError as e:
        raise SourceError("not a Shopify store (no products.json)") from e
    state = dict(source.state or {})
    seen = set(state.get("seen") or [])
    first_run = "seen" not in state
    source.state = {**state, "seen": [str(p["id"]) for p in products]}
    if first_run:
        return []
    items = []
    for p in products:
        if str(p["id"]) in seen:
            continue
        price = (p.get("variants") or [{}])[0].get("price")
        items.append(
            RawItem(
                key=f"shopify:{base}:{p['id']}",
                title=f"New in shop: {p.get('title')}",
                url=f"{base}/products/{p.get('handle')}",
                body=html_to_text(p.get("body_html") or "", 1500) + (f"\nPrice: ${price}" if price else ""),
                published_at=None,
                extra={"kind": "new_product", "price": price},
            )
        )
    return items


# --- websearch (Claude with the web search tool) ---------------------------------


class _Found(llm_models.Lenient):
    title: str = ""
    url: str = ""
    summary: str = ""
    published: str = Field(default="", description="ISO date if known")


class _FoundList(llm_models.Lenient):
    items: list[_Found] = Field(default_factory=list)


def _domains(value) -> list[str] | None:
    if not value:
        return None
    parts = value if isinstance(value, list) else re.split(r"[,\s]+", str(value))
    return [p.strip().lower().removeprefix("https://").removeprefix("www.").strip("/") for p in parts if p.strip()] or None


def _blocked_domains(error: Exception) -> list[str]:
    """Sites Anthropic's web search refuses to read, from its 400 error message."""
    msg = str(error)
    if "not accessible to our user agent" not in msg:
        return []
    m = re.search(r"\[([^\]]*)\]", msg.split("not accessible to our user agent", 1)[1])
    return [d.strip(" '\"").lower() for d in m.group(1).split(",") if d.strip(" '\"")] if m else []


def fetch_websearch(source, session) -> list[RawItem]:
    """For sources nobody publishes a feed for (local run clubs, pop-up announcements,
    Reddit/Facebook/Instagram chatter that shows up in search results)."""
    from app import llm

    cfg = source.config or {}
    query = cfg.get("query")
    if not query:
        raise SourceError("websearch needs a query")
    days = int(cfg.get("days", 3))
    seen = list((source.state or {}).get("recent_urls") or [])[-60:]
    system = (
        "You are a research assistant who finds fresh, specific news for a personal message board. "
        "Use web search. Only return items that are actually recent and specific (an event, a drop, "
        "a post, an announcement), never generic evergreen pages. Summaries state facts plainly: no hype, "
        "no 'highlighting the importance of', no filler. Respond with JSON only."
    )
    user = (
        f"Today is {datetime.now(config.TZ):%A %B %d, %Y}.\n"
        f"Find things published or announced in roughly the last {days} days about:\n{query}\n\n"
        + (f"Extra guidance: {cfg['instructions']}\n" if cfg.get("instructions") else "")
        + ("Skip these URLs, they were already reported:\n" + "\n".join(seen) + "\n" if seen else "")
        + f'\nReturn at most {int(cfg.get("max_results", 6))} items as JSON: '
        '{"items": [{"title": "...", "url": "...", "summary": "2-3 factual sentences", "published": "YYYY-MM-DD"}]}. '
        'Return {"items": []} if nothing new and relevant.'
    )
    domains = _domains(cfg.get("domains"))

    def search(allowed):
        return llm.generate(
            purpose="websearch",
            role="search",
            system=system,
            user=user,
            max_tokens=4000,
            effort="low",
            schema=_FoundList,
            web_search=True,
            max_searches=int(cfg.get("max_searches", 3)),
            allowed_domains=allowed,
            model=cfg.get("model") or None,
            fake=lambda: _FoundList(items=[]),
        )

    try:
        result = search(domains)
    except Exception as e:  # noqa: BLE001 - narrowed below
        blocked = _blocked_domains(e)
        if not blocked or not domains:
            raise
        remaining = [d for d in domains if not any(d == b or d.startswith(b + "/") for b in blocked)]
        if not remaining:
            raise SourceError(
                f"Claude's web search can't read {', '.join(blocked)} (the site blocks it). Turn this source "
                "off; for Reddit, forward its digest emails to the inbox address for this topic instead."
            ) from e
        # Remember the cleaned list so this doesn't fail again next time.
        source.config = {**cfg, "domains": ", ".join(remaining)}
        result = search(remaining)
    found: _FoundList = result.parsed
    items = []
    for f in found.items:
        if not f.url.startswith("http"):
            continue
        published = None
        try:
            published = datetime.fromisoformat(f.published[:10]) if f.published else None
        except ValueError:
            pass
        items.append(
            RawItem(key=f"web:{f.url}", title=f.title, url=f.url, body=f.summary, published_at=published,
                    extra={"kind": "web_search", "query": query})
        )
    source.state = {**(source.state or {}), "recent_urls": (seen + [i.url for i in items])[-60:]}
    return items
