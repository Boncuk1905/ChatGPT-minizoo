#!/usr/bin/env python3
"""MiniZoo Valby hourly public dog-coat stock snapshot. No admin credentials."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://www.minizoo.dk"
ROOT_PATH = "/Hunde/Hundetoej-til-alle-hunde-med-varme-daekkener-og-sko"
ROOT_URL = BASE + ROOT_PATH
HERE = Path(__file__).resolve().parent
CATALOG = HERE / "valby-product-catalog.json"
OUTPUT = HERE / "valby-hourly-stock.json"
HEADERS = {
    "User-Agent": "MiniZoo-Raceopslag-Stock/1.1 (+https://www.minizoo.dk; public stock only)",
    "Accept": "text/html,application/xhtml+xml",
}
MAX_LISTINGS = 120
MAX_PRODUCTS = 1600
WORKERS = 3
CATEGORIES_MAX_AGE = 23 * 3600


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def date_age(value):
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return float("inf")


def text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def valid_url(url, base=BASE):
    try:
        joined = urljoin(base, str(url))
        p = urlsplit(joined)
        if p.scheme != "https" or p.hostname not in ("www.minizoo.dk", "minizoo.dk"):
            return ""
        if not p.path.startswith("/"):
            return ""
        # Retain 'side' pagination; remove tracking params and fragments.
        qs = parse_qs(p.query)
        query = ("side=" + qs["side"][0]) if "side" in qs and qs["side"][0].isdigit() else ""
        return urlunsplit(("https", "www.minizoo.dk", p.path.rstrip("/") or "/", query, ""))
    except (ValueError, TypeError):
        return ""


def get_soup(url):
    time.sleep(0.14)  # Limit burstiness; 3 concurrent workers at most.
    response = requests.get(url, headers=HEADERS, timeout=(8, 24), allow_redirects=True)
    response.raise_for_status()
    final = urlsplit(response.url)
    if final.hostname not in ("www.minizoo.dk", "minizoo.dk"):
        raise ValueError("Unexpected redirect outside minizoo.dk")
    if len(response.content) < 400:
        raise ValueError("Empty or incomplete product/category HTML")
    return BeautifulSoup(response.text, "html.parser")


def is_product(url):
    return bool(re.match(r"^/p/[^/]+", urlsplit(url).path, flags=re.I))


def category_links(soup, base):
    products, pages = set(), set()
    root = ROOT_PATH.lower()
    current = urlsplit(base)
    scope = soup.select_one("main") or soup.select_one("#main") or soup
    for anchor in scope.select("a[href]"):
        u = valid_url(anchor.get("href"), base)
        if not u:
            continue
        p = urlsplit(u)
        low = p.path.lower()
        if is_product(u):
            products.add(u)
        elif low == root and p.query.startswith("side="):
            if u != base:
                pages.add(u)
        elif low.startswith(root + "/") and not low.endswith((".jpg", ".jpeg", ".png", ".webp", ".svg", ".pdf")):
            if u != base:
                pages.add(u)
        elif low == current.path.lower() and p.query.startswith("side="):
            if u != base:
                pages.add(u)
    return products, pages


def discover(previous):
    pending = [ROOT_URL]
    seen, urls, errors = set(), set(), []
    while pending and len(seen) < MAX_LISTINGS:
        page = pending.pop(0)
        if page in seen:
            continue
        seen.add(page)
        try:
            products, links = category_links(get_soup(page), page)
            urls.update(products)
            for u in sorted(links):
                if u not in seen and u not in pending:
                    pending.append(u)
        except Exception as exc:
            errors.append(f"{page}: {type(exc).__name__}")
        if len(urls) > MAX_PRODUCTS:
            raise RuntimeError(f"Too many product links ({len(urls)}); safety limit {MAX_PRODUCTS}")
    complete = not pending and not errors
    if not urls:
        if previous.get("urls"):
            print("Discovery returned no products; using previous catalog", file=sys.stderr)
            return previous, False
        raise RuntimeError("No product links discovered; NOT publishing an empty stock file")
    if not complete and previous.get("urls"):
        urls.update(u for u in previous["urls"] if valid_url(u) and is_product(u))
    result = {
        "schema": "mz-valby-product-catalog-v1",
        "discoveredAt": now_iso() if complete else previous.get("discoveredAt"),
        "urls": sorted(urls),
        "listings": len(seen),
        "discoveryComplete": complete,
        "discoveryErrors": len(errors),
    }
    return result, complete


def stock_from(soup):
    container = soup.select_one("#lagerstatus")
    if not container:
        return None
    for tr in container.select("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        if not re.fullmatch(r"(?:Hunter Valby|MiniZoo Valby(?: ApS)?)", text(cells[0].get_text(" ", strip=True)), flags=re.I):
            continue
        quantity = text(cells[1].get_text(" ", strip=True)).replace(",", ".")
        match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*(?:stk\.?)?", quantity, flags=re.I)
        return float(match.group(1)) if match else None
    return None


def value_for(soup, labels):
    wanted = {x.lower() for x in labels}
    for tr in soup.select("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        for i in range(len(cells) - 1):
            if text(cells[i].get_text(" ", strip=True)).lower().rstrip(":") in wanted:
                return text(cells[i + 1].get_text(" ", strip=True))
    for el in soup.select("dt"):
        if text(el.get_text(" ", strip=True)).lower().rstrip(":") in wanted:
            nxt = el.find_next_sibling("dd")
            if nxt:
                return text(nxt.get_text(" ", strip=True))
    return ""


def coat_title(title):
    t = title.lower()
    if re.search(r"hundesko|hundestøvle|potevoks|hundeseng|liggeunderlag|legetøj|redningsvest", t):
        return False
    apparel = r"dækken|daekken|frakke|jakke|pullover|strik|sweater|trøje|troeje|tørre|toerre|fleece|vest|regn|anorak|thermo|termo|vinter|coat"
    if re.search(r"halsbånd|hundesele|\bsnor\b", t) and not re.search(apparel, t):
        return False
    return bool(re.search(apparel, t))


def parse_size(s):
    s = text(s)
    s = re.sub(r"\b\d{2,3}\s*[x×]\s*\d{2,3}(?:\s*[x×]\s*\d{2,3})?\s*cm\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:hals|bryst|mave|omkreds|pote|krave)\s*[:.]?\s*\d{2,3}(?:[.,]\d)?(?:\s*[-–]\s*\d{2,3}(?:[.,]\d)?)?\s*cm\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d{2,3}(?:[.,]\d)?\s*[-–]\s*\d{2,3}(?:[.,]\d)?\s*cm\b", " ", s, flags=re.I)
    match = re.search(r"\b(?:str(?:\.|ørrelse)?|size)\s*[:.]?\s*(\d{2,3}(?:[.,]5)?)\b", s, flags=re.I)
    if not match:
        match = re.search(r"\b(\d{2,3}(?:[.,]5)?)\s*cm\b", s, flags=re.I)
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    return amount if 15 <= amount <= 100 else None


def product_check(url):
    try:
        soup = get_soup(url)
        title = text(soup.select_one("h1").get_text(" ", strip=True)) if soup.select_one("h1") else ""
        if not title:
            meta = soup.select_one('meta[property="og:title"]')
            title = text(meta.get("content")) if meta else ""
        if not title:
            return ("skip", url, None)
        if not coat_title(title):
            return ("skip", url, None)
        size = (parse_size(value_for(soup, ["Størrelse", "Str.", "Size"]))
                or parse_size(title)
                or parse_size(value_for(soup, ["Dimensioner", "Ryglængde", "Rygmål"])))
        if size is None:
            return ("withoutSize", url, None)
        stock = stock_from(soup)
        if stock is None:
            return ("unknownStock", url, None)
        record = {"title": title[:200], "url": url, "size": size, "stock": stock}
        return ("ok", url, record)
    except Exception as exc:
        return ("failed", url, type(exc).__name__)


def load_json(file):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_json(file, value):
    file.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def main():
    before = load_json(OUTPUT)
    catalog = load_json(CATALOG)
    if not catalog.get("urls") or date_age(catalog.get("discoveredAt")) >= CATEGORIES_MAX_AGE:
        catalog, discovered_ok = discover(catalog)
        if discovered_ok or not CATALOG.exists():
            save_json(CATALOG, catalog)
    else:
        discovered_ok = bool(catalog.get("discoveryComplete"))
    urls = [valid_url(x) for x in catalog.get("urls", [])]
    urls = sorted(set(u for u in urls if u and is_product(u)))
    if not urls or len(urls) > MAX_PRODUCTS:
        raise RuntimeError("Invalid/empty product catalog; old snapshot remains untouched")

    outcomes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, value in enumerate(pool.map(product_check, urls), 1):
            outcomes.append(value)
            if i % 50 == 0:
                print(f"Checked {i}/{len(urls)} product pages")

    positive, checked = [], {}
    skipped = {"failed": 0, "unknownStock": 0, "withoutSize": 0, "skip": 0}
    for status, url, row in outcomes:
        if status == "ok":
            checked[url] = row
            if row["stock"] > 0:
                positive.append(row)
        else:
            skipped[status] = skipped.get(status, 0) + 1
    # Guard against publishing a misleadingly empty or unsuccessful scan.
    if not checked and positive == []:
        raise RuntimeError("Zero products could be verified; retaining previous stock file")

    old_positive = {x.get("url"): x for x in before.get("products", []) if x.get("url")}
    latest = {x["url"]: x for x in positive}
    changes = []
    if before.get("scannedAt"):
        for url, row in checked.items():
            previous = old_positive.get(url)
            if previous is None:
                if row["stock"] > 0:
                    changes.append({"title": row["title"], "url": url, "size": row["size"], "before": None, "after": row["stock"], "type": "available"})
            else:
                old_amount = float(previous.get("stock") or 0)
                amount = row["stock"]
                if amount != old_amount:
                    changes.append({"title": row["title"], "url": url, "size": row["size"], "before": old_amount, "after": amount,
                                    "type": "out" if amount <= 0 else ("down" if amount < old_amount else "up")})
    positives = sorted(positive, key=lambda x: (x["size"], x["title"].lower()))
    summary = {
        "checked": len(checked),
        "found": len(urls),
        "failed": skipped["failed"],
        "unknownStock": skipped["unknownStock"],
        "withoutSize": skipped["withoutSize"],
        "other": skipped["skip"],
        "positive": len(positives),
        "listings": catalog.get("listings", 0),
    }
    snapshot = {
        "schema": "mz-valby-race-stock-v1",
        "source": "github-actions",
        "scannedAt": now_iso(),
        "category": ROOT_URL,
        "complete": discovered_ok and not skipped["failed"] and not skipped["unknownStock"],
        "stats": summary,
        "products": positives,
        "changes": changes[:100],
        "discoveredAt": catalog.get("discoveredAt"),
    }
    save_json(OUTPUT, snapshot)
    print(f"Valby scan: {summary}; changes={len(changes)}; complete={snapshot['complete']}")
    if not snapshot["complete"]:
        print("Partial scan: missing/unverified stock is not presented as available.", file=sys.stderr)


if __name__ == "__main__":
    main()
