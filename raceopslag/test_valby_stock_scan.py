#!/usr/bin/env python3
"""Offline regression checks for public MiniZoo category and stock parsers."""
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).parent))
import valby_stock_scan as scan

class StockScannerTests(unittest.TestCase):
    def soup(self, html):
        return BeautifulSoup(html, "html.parser")

    def test_sizes_and_ranges(self):
        for text, expected in [
            ("HUNTER Uppsala dækken 25 cm",25),
            ("HUNTER Nordby str. 27,5",27.5),
            ("Hundejakke 32.5 cm",32.5),
            ("Hundejakke str. 35",35),
            ("Dækken 30–45 cm",None),
            ("Dækken 45x30x25 cm",None),
            ("Hundesele Bryst 45 cm",None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(scan.find_size(text),expected)

    def test_apparel_not_plain_harness(self):
        self.assertTrue(scan.is_coat("HUNTER Hundejakke med hundesele str. 25"))
        self.assertTrue(scan.is_coat("HUNTER Nordby Hundedækken 27,5 cm"))
        self.assertFalse(scan.is_coat("HUNTER Hundesele 25 cm"))
        self.assertFalse(scan.is_coat("Hundeseng 45x30x25 cm"))
        self.assertFalse(scan.is_coat("Hundesko vinter 25 cm"))

    def test_valby_stock_only(self):
        tmpl = '<table id="lagerstatus"><tr><td>MiniZoo Tårnby</td><td>10</td></tr><tr><td>Hunter Valby</td><td>{}</td></tr></table>'
        for value, expected in [("2",2),("2 stk.",2),("0",0),("1,5 stk.",1.5),("Ukendt",None)]:
            with self.subTest(value=value):
                self.assertEqual(scan.valby_stock(self.soup(tmpl.format(value))),expected)
        self.assertIsNone(scan.valby_stock(self.soup("<main>Intet lager</main>")))

    def test_category_scope_pagination_products(self):
        doc=self.soup('''<main>
          <a href="/p/hunter-uppsala-25-cm-sort">Produkt</a>
          <a href="?side=2">Næste</a>
          <a href="/Hunde/Hundetoej-til-alle-hunde-med-varme-daekkener-og-sko/Hundedaekkener">Dækkener</a>
          <a href="/Hunde/Hundefoder">Andet</a>
          <a href="https://evil.example/p/fake">Falsk</a>
        </main>''')
        products, sections=scan.catalog_urls(doc,scan.CATEGORY)
        self.assertEqual(len(products),1)
        self.assertIn(scan.CATEGORY+"?side=2",sections)
        self.assertTrue(any("Hundedaekkener" in x for x in sections))
        self.assertFalse(any("Hundefoder" in x for x in sections))

    def test_product_requires_public_valby_table(self):
        orig=scan.get
        html=self.soup('''
          <h1>HUNTER Nordby Hundedækken 27,5 cm</h1>
          <table><tr><td>Størrelse</td><td>27,5 cm</td></tr></table>
          <table id="lagerstatus"><tr><td>Hunter Valby</td><td>3 stk.</td></tr></table>
        ''')
        scan.get=lambda url: html
        try:
            state, product=scan.read_product(scan.BASE+"/p/nordby-daekken-275")
            self.assertEqual(state,"positive")
            self.assertEqual(product["size"],27.5)
            self.assertEqual(product["stock"],3)
            self.assertEqual(product["url"],scan.BASE+"/p/nordby-daekken-275")
        finally:
            scan.get=orig

if __name__=="__main__":
    unittest.main()
