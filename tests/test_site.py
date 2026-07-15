#!/usr/bin/env python3
"""Deterministic validation for the built portfolio. Run after Jekyll builds _site."""

from __future__ import annotations

import re
import sys
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "_site"
PROJECT_ART = ROOT / "assets" / "images" / "projects"
EXPECTED_REPOS = {
    "pixel-intelligence", "lychee", "wasm_battle_arena", "angular_shop", "inputflow",
    "sixteenbit", "0x1337", "react-tictactoe", "dropper", "usb_host_passthrough",
    "go-webapi-utils", "benchme", "pokemon-git-tracker", "segfault-ranks",
    "segfault_database", "Bob-OS", "biobox", "NerdSimulator", "rocket-web-service",
    "Rusted-Teensy-Base", "SuperMarbleBattle", "crossy_cars", "ember-lang", "swarm-pi",
}


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.stack: list[str] = []
        self.ids: list[str] = []
        self.links: list[dict[str, str | None]] = []
        self.images: list[dict[str, str | None]] = []
        self.iframes: list[dict[str, str | None]] = []
        self.headings: list[int] = []
        self.text_parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append((tag, values))
        self.stack.append(tag)
        if values.get("id"):
            self.ids.append(values["id"] or "")
        if tag == "a": self.links.append(values)
        if tag == "img": self.images.append(values)
        if tag == "iframe": self.iframes.append(values)
        if re.fullmatch(r"h[1-6]", tag): self.headings.append(int(tag[1]))
        if tag == "title": self._in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title": self._in_title = False
        if tag in self.stack:
            self.stack.reverse(); self.stack.remove(tag); self.stack.reverse()

    def handle_data(self, data: str) -> None:
        if self._in_title: self.title += data
        if data.strip(): self.text_parts.append(data.strip())


def parse_html(path: Path) -> DocumentParser:
    parser = DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def local_target(source: Path, raw_url: str) -> tuple[Path, str] | None:
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme or parsed.netloc or raw_url.startswith(("mailto:", "tel:")):
        return None
    path = urllib.parse.unquote(parsed.path)
    if not path:
        return source, parsed.fragment
    candidate = SITE / path.lstrip("/") if path.startswith("/") else source.parent / path
    if candidate.is_dir(): candidate /= "index.html"
    if not candidate.exists() and not candidate.suffix:
        html_candidate = candidate.with_suffix(".html")
        index_candidate = candidate / "index.html"
        candidate = html_candidate if html_candidate.exists() else index_candidate
    return candidate.resolve(), parsed.fragment


class BuildOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html_paths = sorted(SITE.rglob("*.html"))
        cls.docs = {path: parse_html(path) for path in cls.html_paths}

    def test_standard_required_build_outputs_exist(self) -> None:
        for relative in ("index.html", "ghost.html", "assets/css/style.css"):
            path = SITE / relative
            self.assertTrue(path.is_file(), f"missing build output: {relative}")
            self.assertGreater(path.stat().st_size, 100, f"empty build output: {relative}")

    def test_standard_document_metadata_and_landmarks(self) -> None:
        self.assertGreaterEqual(len(self.docs), 2)
        titles = []
        for path, doc in self.docs.items():
            tags = doc.tags
            html_tags = [attrs for tag, attrs in tags if tag == "html"]
            self.assertEqual(html_tags[0].get("lang"), "en", str(path))
            self.assertTrue(any(tag == "meta" and (attrs.get("charset") or "").lower() == "utf-8" for tag, attrs in tags), str(path))
            self.assertTrue(any(tag == "meta" and attrs.get("name") == "viewport" and attrs.get("content") for tag, attrs in tags), str(path))
            self.assertTrue(any(tag == "meta" and attrs.get("name") == "description" and attrs.get("content") for tag, attrs in tags), str(path))
            self.assertEqual(sum(tag == "main" for tag, _ in tags), 1, str(path))
            self.assertEqual(doc.headings.count(1), 1, str(path))
            self.assertTrue(doc.title.strip(), str(path))
            titles.append(doc.title.strip())
        self.assertEqual(len(titles), len(set(titles)), "page titles must be unique")

    def test_should_fail_unsafe_or_empty_urls_are_rejected(self) -> None:
        for path, doc in self.docs.items():
            for tag, attrs in doc.tags:
                for name, value in attrs.items():
                    self.assertFalse(name.lower().startswith("on"), f"inline handler in {path}: {name}")
                for attr in ("href", "src"):
                    if attr in attrs:
                        value = (attrs[attr] or "").strip()
                        self.assertTrue(value, f"empty {attr} in {path}")
                        self.assertNotEqual(value, "#", f"placeholder {attr} in {path}")
                        self.assertFalse(value.lower().startswith("javascript:"), f"unsafe URL in {path}")

    def test_edge_duplicate_ids_heading_order_and_fragments(self) -> None:
        for path, doc in self.docs.items():
            duplicates = [item for item, count in Counter(doc.ids).items() if count > 1]
            self.assertFalse(duplicates, f"duplicate IDs in {path}: {duplicates}")
            for before, after in zip(doc.headings, doc.headings[1:]):
                self.assertLessEqual(after, before + 1, f"heading level skipped in {path}: h{before} to h{after}")
            for link in doc.links:
                href = (link.get("href") or "").strip()
                target = local_target(path, href)
                if not target: continue
                target_path, fragment = target
                self.assertTrue(target_path.exists(), f"broken local link in {path}: {href}")
                if fragment:
                    target_doc = self.docs.get(target_path) or (parse_html(target_path) if target_path.suffix == ".html" else None)
                    self.assertIsNotNone(target_doc, f"fragment points outside HTML: {href}")
                    self.assertIn(urllib.parse.unquote(fragment), target_doc.ids, f"missing fragment in {href}")

    def test_standard_local_assets_exist(self) -> None:
        for path, doc in self.docs.items():
            for tag, attrs in doc.tags:
                for attr in ({"img": "src", "script": "src", "link": "href"}.get(tag),):
                    if not attr or not attrs.get(attr): continue
                    target = local_target(path, attrs[attr] or "")
                    if target: self.assertTrue(target[0].exists(), f"missing {tag} asset in {path}: {attrs[attr]}")
            icons = [attrs for tag, attrs in doc.tags if tag == "link" and "icon" in (attrs.get("rel") or "").split()]
            self.assertEqual(len(icons), 1, f"missing favicon in {path}")

    def test_accessibility_images_iframes_and_skip_link(self) -> None:
        for path, doc in self.docs.items():
            skip_links = [a for a in doc.links if "skip-link" in (a.get("class") or "").split()]
            self.assertEqual([a.get("href") for a in skip_links], ["#main-content"], str(path))
            for image in doc.images:
                self.assertIn("alt", image, f"missing image alt in {path}")
                self.assertTrue(image.get("width") and image.get("height"), f"missing image dimensions in {path}")
            for iframe in doc.iframes:
                self.assertTrue((iframe.get("title") or "").strip(), f"untitled iframe in {path}")
        index = self.docs[SITE / "index.html"]
        artworks = [img for img in index.images if "/projects/" in (img.get("src") or "")]
        self.assertTrue(all(img.get("alt") == "" for img in artworks), "project artwork must remain decorative")
        meme = [img for img in index.images if (img.get("src") or "").endswith("thisisfine.png")]
        self.assertEqual(len(meme), 1)
        self.assertTrue((meme[0].get("alt") or "").strip())

    def test_standard_project_pages_and_card_links(self) -> None:
        index_path = SITE / "index.html"
        index = self.docs[index_path]
        card_images = [a for a in index.links if "project-image" in (a.get("class") or "").split()]
        project_pages = sorted(SITE.glob("projects/*.html")) + [SITE / "car.html", SITE / "ghost.html"]
        self.assertEqual(len(project_pages), 24)
        self.assertEqual(len(card_images), 24)
        self.assertTrue(all((a.get("href") or "").startswith(("/projects/", "/car", "/ghost")) for a in card_images))
        for path in project_pages:
            doc = self.docs[path]
            self.assertTrue(any("project-page" in (attrs.get("class") or "").split() for tag, attrs in doc.tags if tag == "article"), str(path))
            source_links = [a for a in doc.links if "source-link" in (a.get("class") or "").split()]
            self.assertEqual(len(source_links), 1, f"missing source link in {path}")
            self.assertRegex(source_links[0].get("href") or "", r"^https://github\.com/ConnorBP/[^/]+$")
            self.assertTrue(any("project-overview" in (attrs.get("class") or "").split() for tag, attrs in doc.tags if tag == "div"), f"missing overview in {path}")
            self.assertTrue(any("repo-card" in (attrs.get("class") or "").split() for tag, attrs in doc.tags), f"missing repository card in {path}")
            if any("project-gallery" in (attrs.get("class") or "").split() for tag, attrs in doc.tags):
                self.assertTrue(any(attrs.get("aria-label") == "Repository files" for tag, attrs in doc.tags if tag == "ul"), f"gallery must retain repository files in {path}")

    def test_regression_game_pages_combine_overview_and_demo(self) -> None:
        for page, title, host in (("car.html", "Crossy Cars", "car.segfault.site"), ("ghost.html", "WASM Battle Arena", "ghost.segfault.site")):
            path = SITE / page
            doc = self.docs[path]
            self.assertEqual(len(doc.iframes), 1, str(path))
            self.assertIn(host, doc.iframes[0].get("src") or "")
            fullscreen = [a for a in doc.links if "fullscreen-link" in (a.get("class") or "").split()]
            self.assertEqual(len(fullscreen), 1, str(path))
            self.assertIn(host, fullscreen[0].get("href") or "")
            self.assertIn(title, " ".join(doc.text_parts))

    def test_regression_project_catalog_is_complete_and_featured(self) -> None:
        index_path = SITE / "index.html"
        source = index_path.read_text(encoding="utf-8")
        doc = self.docs[index_path]
        cards = [attrs for tag, attrs in doc.tags if tag == "article" and "project-card" in (attrs.get("class") or "").split()]
        artwork_urls = [img.get("src") for img in doc.images if "/projects/" in (img.get("src") or "")]
        repo_names = set(re.findall(r'https://github\.com/ConnorBP/([^"/#?]+)', source))
        self.assertEqual(len(cards), 24)
        self.assertEqual(len(artwork_urls), 24)
        self.assertEqual(len(set(artwork_urls)), 24)
        self.assertEqual(repo_names, EXPECTED_REPOS)
        featured_end = source.index('<section class="section archive"')
        featured_source = source[:featured_end]
        for repo in ("crossy_cars", "ember-lang", "swarm-pi"):
            self.assertIn(f"https://github.com/ConnorBP/{repo}", featured_source)

    def test_regression_svg_catalog_is_safe_and_complete(self) -> None:
        svgs = sorted(PROJECT_ART.glob("*.svg"))
        self.assertEqual(len(svgs), 24)
        rendered = [img for img in self.docs[SITE / "index.html"].images if "/projects/" in (img.get("src") or "")]
        self.assertEqual(len(rendered), 24)
        self.assertEqual(len({img.get("src") for img in rendered}), 24)
        for image in rendered:
            self.assertTrue(local_target(SITE / "index.html", image.get("src") or "")[0].is_file())
        for path in svgs:
            root = ET.parse(path).getroot()
            self.assertEqual(root.attrib.get("viewBox"), "0 0 1000 562", str(path))
            self.assertGreater(len(list(root.iter())), 10, str(path))
            for element in root.iter():
                for name, value in element.attrib.items():
                    if name.endswith("href"):
                        self.assertFalse(urllib.parse.urlsplit(value).scheme, f"external SVG resource in {path}")

    def test_regression_responsive_and_accessible_css(self) -> None:
        css = (SITE / "assets/css/style.css").read_text(encoding="utf-8")
        for token in ("@media (max-width: 900px)", "@media (max-width: 620px)", "prefers-reduced-motion", ":focus-visible", ".featured-grid", ".archive-grid", ".meme-signoff img"):
            self.assertIn(token, css)
        self.assertRegex(css, r"(?s)@media \(max-width: 620px\).*?\.featured-grid,\s*\.archive-grid\s*\{\s*grid-template-columns:\s*1fr", "mobile grids must collapse to one column")
        self.assertRegex(css, r"\.meme-signoff img\s*\{[^}]*width:\s*56px;[^}]*height:\s*72px", "meme image must remain compact")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BuildOutputTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
