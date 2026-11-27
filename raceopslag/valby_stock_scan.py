#!/usr/bin/env python3
"""MiniZoo Valby stock snapshot. Public product pages only; no admin or writes to MiniZoo."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://www.minizoo.dk"
ROOT = "/Hunde/Hundetoej-til-alle-hunde-med-varme-daekkener-og-sko"
CATEGORY = BASE + ROOT
DATA = Path(__file__).with_name("valby-lager.json")
AGENT = "MiniZoo-Raceopslag-Lagerbot/1.0 (+https://www.minizoo.dk)"
MAX_LISTS, MAX_PRODUCTS = 125, 1800
LOG = logging.getLogger("minizoo-valby")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": AGENT, "Accept": "text/html,application/xhtml+xml"})
ROBOTS = None

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()

def url_of(link: str, base: str) -> str:
    try:
        u = urlsplit(urljoin(base, str(link)))
        if u.scheme != "https" or u.hostname not in ("www.minizoo.dk", "minizoo.dk"):
            return ""
        q = ""
        if u.path.rstrip("/") == ROOT.lower():  # not used; path case may differ
            q = ""
        if re.search(r"/p/[^/]+", u.path, re.I):
            return urlunsplit(("https", "www.minizoo.dk", u.path, "", ""))
        if u.path == ROOT or u.path.startswith(ROOT + "/"):
            side = parse_qs(u.query).get("side", [])
            if side and side[0].isdigit() and 0 < int(side[0]) <= 200:
                q = "side=" + side[0]
            return urlunsplit(("https", "www.minizoo.dk", u.path.rstrip("/"), q, ""))
        return ""
    except Exception:
        return ""

def check_robots() -> None:
    global ROBOTS
    u = BASE + "/robots.txt"
    try:
        r = SESSION.get(u, timeout=15)
        if r.status_code == 404:
            LOG.info("No robots.txt (404)")
            ROBOTS = None
            return
        if r.status_code >= 400:
            raise RuntimeError("robots.txt HTTP " + str(r.status_code))
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        if not rp.can_fetch(AGENT, CATEGORY):
            raise RuntimeError("robots.txt disallows category scanning")
        ROBOTS = rp
    except requests.RequestException as exc:
        raise RuntimeError("Could not verify site's robots policy; scan stopped") from exc

def get(url: str) -> BeautifulSoup:
    if ROBOTS and not ROBOTS.can_fetch(AGENT, url):
        raise RuntimeError("robots.txt disallows " + url)
    last = None
    for attempt in range(3):
        try:
            time.sleep(0.50)  # courteous request rate
            resp = SESSION.get(url, timeout=24)
            if resp.status_code in (403, 429):
                raise RuntimeError("Site rejected scanner HTTP " + str(resp.status_code))
            if resp.status_code in (500, 502, 503, 504):
                raise requests.HTTPError("HTTP " + str(resp.status_code))
            resp.raise_for_status()
            if len(resp.content) < 400:
                raise RuntimeError("Truncated product or category page")
            return BeautifulSoup(resp.content, "html.parser")
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("Fetch failed: " + url + " (" + str(last) + ")")

def product_url(url: str) -> bool:
    return bool(re.search(r"^https://www\.minizoo\.dk/p/[^/?#]+", url))

def catalog_urls(doc: BeautifulSoup, base: str):
    products, sections = set(), set()
    body = doc.select_one("main") or doc.select_one("#main") or doc.body
    if not body:
        return products, sections
    for a in body.select("a[href]"):
        href = url_of(a.get("href", ""), base)
        if not href:
            continue
        if product_url(href):
            products.add(href)
            continue
        if a.find_parent(["header", "footer", "nav"]) and not (a.find_parent(class_=re.compile("pagination", re.I))):
            continue
        p = urlsplit(href)
        base_path = urlsplit(base).path.rstrip("/")
        is_page = p.path == base_path and bool(p.query.startswith("side="))
        is_child = p.path.startswith(base_path + "/") and not a.find_parent(class_=re.compile("breadcrumb", re.I))
        if (is_page or is_child) and href != base:
            sections.add(href)
    return products, sections

def all_product_urls():
    queue = deque([CATEGORY])
    pages, products = set(), set()
    errors = 0
    while queue and len(pages) < MAX_LISTS:
        url = queue.popleft()
        if url in pages:
            continue
        pages.add(url)
        try:
            doc = get(url)
            links, subpages = catalog_urls(doc, url)
            products.update(links)
            for x in sorted(subpages):
                if x not in pages and x not in queue:
                    queue.append(x)
            LOG.info("Catalog %s / %s: %s products found", len(pages), MAX_LISTS, len(products))
            if len(products) > MAX_PRODUCTS:
                raise RuntimeError("Too many products, review category scope")
        except RuntimeError as exc:
            if "disallows" in str(exc) or "rejected scanner" in str(exc) or "Too many products" in str(exc):
                raise
            errors += 1
            LOG.warning("Catalog page failed %s: %s", url, exc)
    if not products:
        raise RuntimeError("No product links discovered; refusing empty stock update")
    return sorted(products), {"listings": len(pages), "catalogErrors": errors,
                              "truncated": bool(queue), "discoveredProducts": len(products)}

def spec(doc, labels) -> str:
    names = {x.lower() for x in labels}
    for tr in doc.select("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        for i in range(len(cells)-1):
            if clean(cells[i].get_text(" ", strip=True)).rstrip(":").lower() in names:
                return clean(cells[i+1].get_text(" ", strip=True))
    for dt in doc.select("dt"):
        if clean(dt.get_text(" ", strip=True)).rstrip(":").lower() in names:
            next_dl = dt.find_next_sibling("dd")
            if next_dl:
                return clean(next_dl.get_text(" ", strip=True))
    return ""

def is_coat(title: str) -> bool:
    t = title.lower()
    if re.search(r"hundesko|hundestøvle|potevoks|hundeseng|liggeunderlag|legetøj|fodertøj|hunderedningsvest", t):
        return False
    matches = bool(re.search(r"dækken|daekken|frakke|jakke|pullover|strik|sweater|trøje|troeje|tørre|toerre|fleece|vest|regn|anorak|thermo|termo|vinter|coat", t))
    if re.search(r"halsbånd|hundesele|snor\b", t) and not matches:
        return False
    return matches

def find_size(size_text: str):
    t = clean(size_text)
    t = re.sub(r"\b\d{2,3}\s*[x×]\s*\d{2,3}(?:\s*[x×]\s*\d{2,3})?\s*cm\b", " ", t, flags=re.I)
    t = re.sub(r"\b(?:hals|bryst|mave|omkreds|pote|krave)\s*[:.]?\s*\d{2,3}(?:[.,]\d)?(?:\s*[-–]\s*\d{2,3}(?:[.,]\d)?)?\s*cm\b", " ", t, flags=re.I)
    t = re.sub(r"\b\d{2,3}(?:[.,]\d)?\s*[-–]\s*\d{2,3}(?:[.,]\d)?\s*cm\b", " ", t, flags=re.I)
    m = re.search(r"\b(?:str(?:\.|ørrelse)?|size)\s*[:.]?\s*(\d{2,3}(?:[.,]5)?)\b", t, re.I)
    if not m:
        m = re.search(r"\b(\d{2,3}(?:[.,]5)?)\s*cm\b", t, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if 15 <= v <= 100 else None

def valby_stock(doc: BeautifulSoup):
    box = doc.select_one("#lagerstatus")
    if box is None:
        return None
    for tr in box.select("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        store = clean(cells[0].get_text(" ", strip=True))
        if not re.fullmatch(r"(?:Hunter Valby|MiniZoo Valby(?: ApS)?)", store, re.I):
            continue
        qty = clean(cells[1].get_text(" ", strip=True)).replace(",", ".")
        m = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*(?:stk\.?)?", qty, re.I)
        return float(m.group(1)) if m else None
    return None

def read_product(url: str, original: dict | None = None):
    doc = get(url)
    title = clean((doc.select_one("h1") or doc.select_one("title")).get_text(" ", strip=True))[:200]
    if not is_coat(title):
        return "skip", None
    size = find_size(spec(doc, ["Størrelse", "Str.", "Size"]))
    if size is None:
        size = find_size(title)
    if size is None:
        size = find_size(spec(doc, ["Dimensioner", "Ryglængde", "Rygmål"]))
    if size is None:
        return "no_size", None
    stock = valby_stock(doc)
    if stock is None:
        return "no_valby", None
    if stock <= 0:
        return "zero", {"url": url, "title": title, "size": size, "stock": 0}
    pid = spec(doc, ["Produkt ID", "ProduktID"])
    return "positive", {"url":url,"title":title,"size":size,"stock":stock,
                        "productId":pid[:40], "checkedAt":now()}

def read_previous() -> dict:
    if not DATA.exists():
        return {}
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    return raw if raw.get("schema") == "mz-valby-race-stock-v1" else {}

def scan(mode: str):
    old = read_previous()
    previous = {p["url"]:p for p in old.get("products", []) if isinstance(p, dict) and p.get("url")}
    if mode == "refresh" and not previous:
        LOG.warning("No previous products, switching to full scan")
        mode = "full"
    if mode == "full":
        urls, discovered = all_product_urls()
    else:
        urls, discovered = sorted(previous), {"listings":0,"catalogErrors":0,
                                               "truncated":False,"discoveredProducts":len(previous)}
    stock, errors, without_size, no_valby, skipped, zeros, checked = [], 0, 0, 0, 0, 0, 0
    changes = []
    for pos, url in enumerate(urls, 1):
        try:
            state, item = read_product(url, previous.get(url))
            checked += 1
            before = previous.get(url)
            if state == "positive":
                stock.append(item)
                if before and item["stock"] != before.get("stock"):
                    changes.append({"title":item["title"],"url":url,
                                    "before":before["stock"],"after":item["stock"],"at":now()})
            elif state == "zero":
                zeros += 1
                if before and before.get("stock",0)>0:
                    changes.append({"title":item["title"],"url":url,
                                    "before":before["stock"],"after":0,"at":now()})
            elif state == "no_valby":
                no_valby += 1
            elif state == "no_size":
                without_size += 1
            else:
                skipped += 1
        except RuntimeError as exc:
            if "disallows" in str(exc) or "rejected scanner" in str(exc):
                raise
            errors += 1
            LOG.warning("Product %s failed: %s", url, exc)
        if pos % 30 == 0:
            LOG.info("Checked %s/%s; %s in stock, %s errors", pos, len(urls), len(stock), errors)
    if not checked:
        raise RuntimeError("No products read successfully")
    if errors > max(10, int(len(urls) * 0.2)):
        raise RuntimeError(f"Too many product errors ({errors}); previous file preserved")
    if no_valby == checked:
        raise RuntimeError("No Valby stock table found on any product; previous file preserved")
    if mode == "full" and discovered["catalogErrors"] > 0:
        LOG.warning("Incomplete catalog; retaining previously verified products not discovered is unsafe")
    stock.sort(key=lambda p: (p["size"],p["title"].lower()))
    prev_changes = old.get("changes", [])
    payload = {
      "schema":"mz-valby-race-stock-v1",
      "scannedAt":now(),
      "complete": errors==0 and no_valby==0 and not discovered["truncated"] and discovered["catalogErrors"]==0,
      "category":CATEGORY,
      "mode":mode,
      "stats":{"listings":discovered["listings"],"found":len(urls),"checked":checked,
               "failed":errors+discovered["catalogErrors"],"unknownStock":no_valby,
               "withoutSize":without_size,"otherOrOutOfStock":skipped+zeros,
               "positive":len(stock),"soldOutSinceLastScan":len([x for x in changes if x["after"]==0])},
      "changes":(changes+prev_changes)[:60],
      "products":stock,
    }
    DATA.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    LOG.info("Saved %s: %s stocked products; %s stock changes", DATA, len(stock), len(changes))
    return payload

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",choices=("full","refresh"),default="full")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    check_robots()
    scan(args.mode)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        LOG.exception("Scanner stopped without overwriting existing stock snapshot: %s", exc)
        sys.exit(1)
