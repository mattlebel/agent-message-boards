"""Source kinds. Each fetcher takes (source, session) and returns RawItems.

KINDS also drives the "add source" form in admin: the fields listed are the
config keys the fetcher reads.
"""

from app.sources import ebay, feeds, web

FETCHERS = {
    "rss": feeds.fetch_rss,
    "reddit": feeds.fetch_reddit,
    "youtube": feeds.fetch_youtube,
    "page_watch": web.fetch_page_watch,
    "sitemap": web.fetch_sitemap,
    "shopify": web.fetch_shopify,
    "ebay": ebay.fetch_ebay,
    "websearch": web.fetch_websearch,
}

KINDS = {
    "rss": {
        "label": "RSS / Atom feed (tip: Google News search feeds work here)",
        "fields": {"url": "Feed URL", "include": "Only keep items matching this regex (optional)",
                   "exclude": "Drop items matching this regex (optional)"},
    },
    "youtube": {
        "label": "YouTube (your subscriptions)",
        "fields": {"channel_ids": "Extra channel IDs, comma separated (optional)",
                   "max_age_days": "Ignore videos older than this (default 3)"},
    },
    "page_watch": {
        "label": "Watch a web page for changes",
        "fields": {"url": "Page URL", "selector": "CSS selector for the part to watch (optional)"},
    },
    "sitemap": {
        "label": "New pages in a sitemap",
        "fields": {"url": "Sitemap URL (usually /sitemap.xml)", "include": "Only URLs matching this regex (optional)"},
    },
    "shopify": {
        "label": "New products in a Shopify shop",
        "fields": {"url": "Shop URL"},
    },
    "ebay": {
        "label": "eBay market tracker (catalog, first sightings, auction results)",
        "fields": {"query": "Search terms", "min_price": "Post listings asking at least this much (default 25)"},
    },
    "websearch": {
        "label": "Claude web search (~$0.02-0.05 per run)",
        "fields": {"query": "What to look for", "instructions": "Extra guidance (optional)",
                   "domains": "Only search these sites, comma separated (optional, e.g. reddit.com)",
                   "days": "How recent, in days (default 3)", "max_results": "Max items (default 6)"},
    },
}

NUMERIC_FIELDS = {"min_score", "max_age_days", "min_price", "days", "max_results", "limit", "max_searches"}
LIST_FIELDS = {"channel_ids"}
