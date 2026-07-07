#!/usr/bin/env python3
"""
SEO audit + auto-fix for hrexperttogo.com.

Runs weekly via GitHub Actions. Two modes controlled by --mode:
  - safe-fix    : mechanical fixes (canonical, OG, Twitter, JSON-LD, alt text,
                  sitemap lastmod, robots) committed directly on main.
  - content-pr  : suggested title / meta description tweaks tuned to the
                  target audience — opened as a pull request for human review.

The script also writes reports/seo-report.md and reports/seo-report.json which
are emailed by the workflow.

Design goals: dependency-light (stdlib + BeautifulSoup + requests), safe by
default (only edits inside <head> or specific attributes), idempotent (running
twice produces the same output), and diff-friendly (stable ordering).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment

SITE_URL = "https://hrexperttogo.com"
SITE_NAME = "HR Expert to go"
ROOT = Path(__file__).resolve().parent.parent

# --- Target audience keywords (parents funding early-career coaching) --------
# These bias title/description suggestions. Chosen for the parent-buyer +
# early-career persona based on competitor positioning (Robin Ryan,
# Manuia SCS) and standard search intent for the niche.
AUDIENCE_KEYWORDS = [
    "career coaching for college graduates",
    "career coach for new grads",
    "help my college graduate find a job",
    "virtual career coaching",
    "resume help for recent graduates",
    "interview coaching for college students",
    "salary negotiation coaching",
    "job search coach for early career professionals",
    "career coach for parents of college students",
    "SHRM-certified career coach",
]

# Pages that shouldn't be indexed prominently or need special handling.
LOW_PRIORITY_PAGES = {"thank-you.html", "intake-form.html"}


@dataclass
class Issue:
    page: str
    severity: str  # "high" | "medium" | "low"
    category: str
    message: str
    fixed: bool = False
    fix_note: str = ""


@dataclass
class PageInfo:
    filename: str
    title: str = ""
    description: str = ""
    h1_count: int = 0
    canonical: str = ""
    og_tags: dict[str, str] = field(default_factory=dict)
    twitter_tags: dict[str, str] = field(default_factory=dict)
    has_jsonld: bool = False
    images_missing_alt: int = 0
    internal_links: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

def load_pages() -> list[Path]:
    return sorted(p for p in ROOT.glob("*.html"))


def parse(html_path: Path) -> BeautifulSoup:
    return BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")


def write_html(html_path: Path, soup: BeautifulSoup) -> None:
    html_path.write_text(str(soup), encoding="utf-8")


def canonical_url_for(filename: str) -> str:
    if filename == "index.html":
        return f"{SITE_URL}/"
    return f"{SITE_URL}/{filename}"


def humanize_filename(name: str) -> str:
    stem = name.replace(".html", "")
    return stem.replace("-", " ").replace("_", " ").strip().title()


def get_meta(soup: BeautifulSoup, name: str = "", prop: str = "") -> str:
    if name:
        tag = soup.find("meta", attrs={"name": name})
    else:
        tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag else ""


def ensure_meta(soup: BeautifulSoup, *, name: str = "", prop: str = "",
                content: str) -> bool:
    """Add or update a meta tag inside <head>. Returns True if changed."""
    head = soup.find("head")
    if head is None:
        return False
    if name:
        tag = soup.find("meta", attrs={"name": name})
        attr_key, attr_val = "name", name
    else:
        tag = soup.find("meta", attrs={"property": prop})
        attr_key, attr_val = "property", prop
    if tag is None:
        new_tag = soup.new_tag("meta", attrs={attr_key: attr_val, "content": content})
        head.append(new_tag)
        return True
    if (tag.get("content") or "").strip() != content.strip():
        tag["content"] = content
        return True
    return False


def ensure_canonical(soup: BeautifulSoup, url: str) -> bool:
    head = soup.find("head")
    if head is None:
        return False
    tag = soup.find("link", attrs={"rel": "canonical"})
    if tag is None:
        new_tag = soup.new_tag("link", rel="canonical", href=url)
        head.append(new_tag)
        return True
    if (tag.get("href") or "").strip() != url:
        tag["href"] = url
        return True
    return False


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def audit_page(path: Path) -> tuple[PageInfo, list[Issue]]:
    soup = parse(path)
    info = PageInfo(filename=path.name)
    issues: list[Issue] = []

    head = soup.find("head")
    if head is None:
        issues.append(Issue(path.name, "high", "structure", "No <head> element"))
        return info, issues

    # Title
    title_tag = soup.find("title")
    info.title = title_tag.get_text(strip=True) if title_tag else ""
    if not info.title:
        issues.append(Issue(path.name, "high", "title", "Missing <title>"))
    elif len(info.title) < 30:
        issues.append(Issue(path.name, "medium", "title",
                            f"Title too short ({len(info.title)} chars)"))
    elif len(info.title) > 65:
        issues.append(Issue(path.name, "low", "title",
                            f"Title long ({len(info.title)} chars, prefer ≤60)"))

    # Description
    info.description = get_meta(soup, name="description")
    if not info.description:
        issues.append(Issue(path.name, "high", "description",
                            "Missing meta description"))
    elif len(info.description) < 70:
        issues.append(Issue(path.name, "medium", "description",
                            f"Description short ({len(info.description)} chars)"))
    elif len(info.description) > 160:
        issues.append(Issue(path.name, "low", "description",
                            f"Description long ({len(info.description)} chars)"))

    # H1
    h1s = soup.find_all("h1")
    info.h1_count = len(h1s)
    if info.h1_count == 0:
        issues.append(Issue(path.name, "high", "h1", "No <h1> on page"))
    elif info.h1_count > 1:
        issues.append(Issue(path.name, "medium", "h1",
                            f"Multiple <h1> tags ({info.h1_count})"))

    # Canonical
    can = soup.find("link", attrs={"rel": "canonical"})
    info.canonical = (can.get("href") or "").strip() if can else ""
    if not info.canonical:
        issues.append(Issue(path.name, "medium", "canonical",
                            "Missing canonical link"))

    # Open Graph
    for prop in ("og:title", "og:description", "og:type", "og:url", "og:image"):
        val = get_meta(soup, prop=prop)
        if val:
            info.og_tags[prop] = val
        else:
            issues.append(Issue(path.name, "medium", "open-graph",
                                f"Missing {prop}"))

    # Twitter
    for name in ("twitter:card", "twitter:title", "twitter:description",
                 "twitter:image"):
        val = get_meta(soup, name=name)
        if val:
            info.twitter_tags[name] = val
        else:
            issues.append(Issue(path.name, "low", "twitter",
                                f"Missing {name}"))

    # JSON-LD structured data
    jsonld = soup.find_all("script", attrs={"type": "application/ld+json"})
    info.has_jsonld = len(jsonld) > 0
    if not info.has_jsonld:
        issues.append(Issue(path.name, "medium", "structured-data",
                            "No JSON-LD structured data"))

    # Images missing alt
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt is None or not alt.strip():
            info.images_missing_alt += 1
            issues.append(Issue(path.name, "medium", "alt-text",
                                f"Image missing alt: {img.get('src', '')}"))

    # Internal links (relative, .html)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if href.startswith("http"):
            if urlparse(href).netloc.endswith("hrexperttogo.com"):
                info.internal_links.append(href)
            continue
        info.internal_links.append(href)

    # Language
    if not (soup.html and soup.html.get("lang")):
        issues.append(Issue(path.name, "low", "html-lang",
                            "Missing lang attribute on <html>"))

    return info, issues


def audit_internal_links(pages: dict[str, PageInfo]) -> list[Issue]:
    """Verify internal links resolve to existing files."""
    issues: list[Issue] = []
    valid_targets = set(p for p in pages)
    for source, info in pages.items():
        for href in info.internal_links:
            # Strip fragment/query and leading path
            target = href.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue
            if target.startswith("http"):
                path = urlparse(target).path.lstrip("/")
                if path == "":
                    path = "index.html"
                target = path
            target = target.lstrip("/")
            if target.endswith("/"):
                target = target + "index.html"
            if target and target not in valid_targets:
                issues.append(Issue(source, "high", "broken-link",
                                    f"Broken internal link: {href}"))
    return issues


def audit_sitemap(pages: dict[str, PageInfo]) -> list[Issue]:
    issues: list[Issue] = []
    sitemap_path = ROOT / "sitemap.xml"
    if not sitemap_path.exists():
        issues.append(Issue("sitemap.xml", "high", "sitemap", "sitemap.xml missing"))
        return issues
    text = sitemap_path.read_text(encoding="utf-8")
    listed = set(re.findall(r"<loc>(.*?)</loc>", text))
    # Normalize
    listed_paths = set()
    for u in listed:
        p = urlparse(u).path.lstrip("/")
        if p == "":
            p = "index.html"
        listed_paths.add(p)
    for filename in pages:
        if filename in LOW_PRIORITY_PAGES:
            continue
        if filename not in listed_paths:
            issues.append(Issue("sitemap.xml", "medium", "sitemap",
                                f"Missing from sitemap: {filename}"))
    # lastmod freshness
    if "<lastmod>" not in text:
        issues.append(Issue("sitemap.xml", "low", "sitemap",
                            "No <lastmod> entries"))
    return issues


# --------------------------------------------------------------------------- #
# Safe auto-fixes
# --------------------------------------------------------------------------- #

def default_og_image() -> str:
    return f"{SITE_URL}/HR_Expert_to_Go_Primary.png"


def apply_safe_fixes(path: Path, info: PageInfo) -> list[str]:
    """Apply mechanical, non-copy-changing fixes. Returns list of change notes."""
    soup = parse(path)
    changes: list[str] = []

    # <html lang="en">
    if soup.html and not soup.html.get("lang"):
        soup.html["lang"] = "en"
        changes.append("Set html lang=en")

    canonical_url = canonical_url_for(path.name)
    if ensure_canonical(soup, canonical_url):
        changes.append("Added/updated canonical link")

    # Open Graph
    og_title = info.title or humanize_filename(path.name)
    og_desc = info.description or f"{SITE_NAME} — practical 1:1 career coaching."
    og_type = "website"
    og_url = canonical_url
    og_image = default_og_image()
    for prop, val in [
        ("og:title", og_title),
        ("og:description", og_desc),
        ("og:type", og_type),
        ("og:url", og_url),
        ("og:image", og_image),
        ("og:site_name", SITE_NAME),
    ]:
        if ensure_meta(soup, prop=prop, content=val):
            changes.append(f"Added/updated {prop}")

    # Twitter cards
    for name, val in [
        ("twitter:card", "summary_large_image"),
        ("twitter:title", og_title),
        ("twitter:description", og_desc),
        ("twitter:image", og_image),
    ]:
        if ensure_meta(soup, name=name, content=val):
            changes.append(f"Added/updated {name}")

    # robots meta (index, follow) on all indexable pages
    if path.name in LOW_PRIORITY_PAGES:
        if ensure_meta(soup, name="robots", content="noindex, follow"):
            changes.append("Set robots=noindex,follow (utility page)")
    else:
        if ensure_meta(soup, name="robots", content="index, follow"):
            changes.append("Set robots=index,follow")

    # Alt text — filename-based fallback (avoid clobbering existing alts)
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt is None or not alt.strip():
            src = (img.get("src") or "").rsplit("/", 1)[-1]
            base = re.sub(r"\.[a-z]+$", "", src, flags=re.I)
            base = re.sub(r"[_\-]+", " ", base).strip()
            if not base:
                base = f"{SITE_NAME} image"
            img["alt"] = f"{base} — {SITE_NAME}"
            changes.append(f"Added alt text: {img['alt']}")

    # JSON-LD — inject Organization on index, Service on service pages,
    # FAQPage skipped (needs Q&A extraction), Person on about.
    head = soup.find("head")
    if head is not None:
        existing_ld = soup.find_all("script", attrs={"type": "application/ld+json"})
        # Deduplicate by @type
        existing_types = set()
        for s in existing_ld:
            try:
                data = json.loads(s.string or "{}")
                if isinstance(data, dict) and "@type" in data:
                    existing_types.add(data["@type"])
            except (json.JSONDecodeError, TypeError):
                pass
        ld_blocks = build_jsonld_for(path.name, info)
        for block in ld_blocks:
            if block.get("@type") in existing_types:
                continue
            tag = soup.new_tag("script", type="application/ld+json")
            tag.string = json.dumps(block, indent=2, ensure_ascii=False)
            head.append(tag)
            changes.append(f"Added JSON-LD: {block['@type']}")

    if changes:
        write_html(path, soup)
    return changes


def build_jsonld_for(filename: str, info: PageInfo) -> list[dict[str, Any]]:
    base_org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "url": SITE_URL,
        "logo": f"{SITE_URL}/HR_Expert_to_Go_Primary.png",
        "founder": {"@type": "Person", "name": "Ty Smith"},
        "description": (
            "Virtual 1:1 career coaching for college students, recent "
            "graduates, and early-career professionals."
        ),
        "sameAs": [],
    }
    if filename == "index.html":
        return [base_org, {
            "@context": "https://schema.org",
            "@type": "ProfessionalService",
            "name": SITE_NAME,
            "url": SITE_URL,
            "description": info.description or base_org["description"],
            "areaServed": "United States",
            "serviceType": "Career coaching",
        }]
    if filename == "about.html":
        return [{
            "@context": "https://schema.org",
            "@type": "Person",
            "name": "Ty Smith",
            "jobTitle": "HR Expert & Career Coach, SHRM-CP",
            "worksFor": {"@type": "Organization", "name": SITE_NAME},
            "url": f"{SITE_URL}/about.html",
            "description": (
                "SHRM-certified HR leader with 20+ years of experience "
                "guiding early-career professionals through hiring."
            ),
        }]
    service_map = {
        "resume-coaching.html": "Resume coaching",
        "interview-preparation.html": "Interview preparation",
        "salary-negotiation.html": "Salary negotiation coaching",
        "job-search-strategy.html": "Job search strategy",
        "profile-optimization.html": "Professional profile optimization",
    }
    if filename in service_map:
        return [{
            "@context": "https://schema.org",
            "@type": "Service",
            "name": service_map[filename],
            "provider": {"@type": "Organization", "name": SITE_NAME,
                         "url": SITE_URL},
            "areaServed": "United States",
            "audience": {
                "@type": "PeopleAudience",
                "audienceType": ("College students, recent graduates, and "
                                 "early-career professionals"),
            },
            "url": canonical_url_for(filename),
            "description": info.description,
        }]
    return []


# --------------------------------------------------------------------------- #
# Sitemap refresh
# --------------------------------------------------------------------------- #

def refresh_sitemap(pages: dict[str, PageInfo]) -> list[str]:
    changes: list[str] = []
    today = dt.date.today().isoformat()
    priorities = {
        "index.html": "1.0",
        "about.html": "0.9",
        "pricing.html": "0.9",
        "resume-coaching.html": "0.9",
        "interview-preparation.html": "0.9",
        "salary-negotiation.html": "0.8",
        "job-search-strategy.html": "0.8",
        "profile-optimization.html": "0.8",
        "faq.html": "0.8",
        "why-your-college-grad-needs-career-coaching.html": "0.7",
        "contact.html": "0.7",
        "privacy.html": "0.3",
        "terms.html": "0.3",
    }
    changefreq = {
        "privacy.html": "yearly",
        "terms.html": "yearly",
    }
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for filename in sorted(pages):
        if filename in LOW_PRIORITY_PAGES:
            continue
        loc = canonical_url_for(filename)
        freq = changefreq.get(filename, "monthly")
        prio = priorities.get(filename, "0.5")
        lines.extend([
            "  <url>",
            f"    <loc>{loc}</loc>",
            f"    <lastmod>{today}</lastmod>",
            f"    <changefreq>{freq}</changefreq>",
            f"    <priority>{prio}</priority>",
            "  </url>",
        ])
    lines.append("</urlset>")
    new_content = "\n".join(lines) + "\n"
    sitemap_path = ROOT / "sitemap.xml"
    if sitemap_path.read_text(encoding="utf-8") != new_content:
        sitemap_path.write_text(new_content, encoding="utf-8")
        changes.append(f"Refreshed sitemap.xml (lastmod={today})")
    return changes


# --------------------------------------------------------------------------- #
# Content-PR suggestions (audience-tuned title / description)
# --------------------------------------------------------------------------- #

# Tuned drafts. Kept conservative — reviewer must merge PR.
CONTENT_SUGGESTIONS: dict[str, dict[str, str]] = {
    "index.html": {
        "title": "Virtual Career Coach for New College Grads & Early Career Pros | HR Expert to go",
        "description": ("1:1 virtual career coaching for college students, "
                        "recent graduates, and their parents. Resume, "
                        "interview, and salary help from a SHRM-certified "
                        "HR expert with 20+ years of hiring experience."),
    },
    "about.html": {
        "title": "About Ty Smith, SHRM-CP | Career Coach for New Grads",
        "description": ("Meet Ty Smith, SHRM-CP — a career coach with 20+ "
                        "years of HR leadership who helps recent college "
                        "graduates and early-career professionals land the "
                        "right role."),
    },
    "pricing.html": {
        "title": "Career Coaching Pricing for New Grads | HR Expert to go",
        "description": ("Transparent pricing for 1:1 virtual career coaching "
                        "designed for college students, recent graduates, "
                        "and the parents supporting them."),
    },
    "resume-coaching.html": {
        "title": "Resume Coaching for Recent Graduates | HR Expert to go",
        "description": ("Resume coaching for new college graduates and "
                        "early-career professionals from an HR expert who "
                        "has read thousands of resumes. Build one recruiters "
                        "actually respond to."),
    },
    "interview-preparation.html": {
        "title": "Interview Prep for New Grads | Virtual Career Coach",
        "description": ("Interview coaching for college students and recent "
                        "graduates. Practice real questions with a "
                        "SHRM-certified HR expert and walk in ready."),
    },
    "salary-negotiation.html": {
        "title": "Salary Negotiation Coaching for New Grads | HR Expert to go",
        "description": ("Salary negotiation coaching for early-career "
                        "professionals. Understand the offer, know your "
                        "worth, and ask with confidence."),
    },
    "job-search-strategy.html": {
        "title": "Job Search Strategy for New Grads | HR Expert to go",
        "description": ("A focused job search strategy for recent graduates "
                        "and early-career professionals so your applications "
                        "reach the right people."),
    },
    "profile-optimization.html": {
        "title": "LinkedIn Profile Optimization for New Grads | HR Expert to go",
        "description": ("Professional profile optimization for early-career "
                        "professionals — position yourself so recruiters "
                        "find you and want to reach out."),
    },
    "faq.html": {
        "title": "FAQ | HR Expert to go — Career Coaching for New Grads",
        "description": ("Common questions about virtual career coaching for "
                        "college students, recent graduates, and the parents "
                        "helping them launch a career."),
    },
    "contact.html": {
        "title": "Contact HR Expert to go | Career Coach for New Grads",
        "description": ("Get in touch with Ty Smith, SHRM-CP — virtual "
                        "career coach for college students and recent "
                        "graduates."),
    },
    "why-your-college-grad-needs-career-coaching.html": {
        "title": "Why Your College Grad Needs a Career Coach | HR Expert to go",
        "description": ("A guide for parents of new college graduates: how "
                        "career coaching helps your grad land the right job "
                        "faster and with more confidence."),
    },
}


def content_suggestions(pages: dict[str, PageInfo]) -> list[dict[str, str]]:
    """Return list of proposed content changes (not yet applied)."""
    proposals: list[dict[str, str]] = []
    for filename, page in pages.items():
        suggestion = CONTENT_SUGGESTIONS.get(filename)
        if not suggestion:
            continue
        current_title = page.title
        current_desc = page.description
        if (current_title != suggestion["title"] or
                current_desc != suggestion["description"]):
            proposals.append({
                "page": filename,
                "current_title": current_title,
                "proposed_title": suggestion["title"],
                "current_description": current_desc,
                "proposed_description": suggestion["description"],
            })
    return proposals


def apply_content_suggestions(proposals: list[dict[str, str]]) -> list[str]:
    changes: list[str] = []
    for prop in proposals:
        path = ROOT / prop["page"]
        soup = parse(path)
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text() != prop["proposed_title"]:
            title_tag.string = prop["proposed_title"]
            changes.append(f"{prop['page']}: updated <title>")
        desc_tag = soup.find("meta", attrs={"name": "description"})
        if desc_tag:
            if (desc_tag.get("content") or "") != prop["proposed_description"]:
                desc_tag["content"] = prop["proposed_description"]
                changes.append(f"{prop['page']}: updated meta description")
        else:
            head = soup.find("head")
            if head:
                new_tag = soup.new_tag("meta", attrs={
                    "name": "description",
                    "content": prop["proposed_description"],
                })
                head.append(new_tag)
                changes.append(f"{prop['page']}: added meta description")
        write_html(path, soup)
    return changes


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def write_report(pages: dict[str, PageInfo], all_issues: list[Issue],
                 fixes_applied: list[str],
                 content_proposals: list[dict[str, str]],
                 mode: str) -> tuple[Path, Path]:
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    md_path = reports_dir / "seo-report.md"
    json_path = reports_dir / "seo-report.json"

    today = dt.date.today().isoformat()

    # Group issues
    by_severity: dict[str, list[Issue]] = {"high": [], "medium": [], "low": []}
    for issue in all_issues:
        by_severity[issue.severity].append(issue)

    lines: list[str] = []
    lines.append(f"# SEO Audit Report — {today}")
    lines.append("")
    lines.append(f"**Site:** {SITE_URL}  ")
    lines.append(f"**Mode:** `{mode}`  ")
    lines.append(f"**Pages scanned:** {len(pages)}  ")
    lines.append(f"**Issues found:** {len(all_issues)} "
                 f"(high: {len(by_severity['high'])}, "
                 f"medium: {len(by_severity['medium'])}, "
                 f"low: {len(by_severity['low'])})  ")
    lines.append(f"**Automated fixes applied:** {len(fixes_applied)}")
    lines.append("")
    lines.append("## Target audience keywords")
    lines.append("")
    lines.append("Titles and descriptions are tuned toward:")
    for kw in AUDIENCE_KEYWORDS:
        lines.append(f"- {kw}")
    lines.append("")

    if fixes_applied:
        lines.append("## Fixes applied this run")
        lines.append("")
        for change in fixes_applied:
            lines.append(f"- {change}")
        lines.append("")

    if content_proposals:
        lines.append("## Content changes proposed (in pull request)")
        lines.append("")
        for prop in content_proposals:
            lines.append(f"### `{prop['page']}`")
            lines.append("")
            lines.append("**Title**")
            lines.append(f"- Current: `{prop['current_title'] or '(none)'}`")
            lines.append(f"- Proposed: `{prop['proposed_title']}`")
            lines.append("")
            lines.append("**Meta description**")
            lines.append(f"- Current: {prop['current_description'] or '(none)'}")
            lines.append(f"- Proposed: {prop['proposed_description']}")
            lines.append("")

    if all_issues:
        lines.append("## Remaining issues")
        lines.append("")
        for sev in ("high", "medium", "low"):
            group = by_severity[sev]
            if not group:
                continue
            lines.append(f"### {sev.title()} ({len(group)})")
            lines.append("")
            for issue in group:
                lines.append(f"- **{issue.page}** · {issue.category} — {issue.message}")
            lines.append("")

    lines.append("## Page inventory")
    lines.append("")
    lines.append("| Page | Title length | Desc length | H1s | Canonical | JSON-LD |")
    lines.append("|---|---|---|---|---|---|")
    for name in sorted(pages):
        p = pages[name]
        lines.append(
            f"| {name} | {len(p.title)} | {len(p.description)} | "
            f"{p.h1_count} | {'yes' if p.canonical else 'no'} | "
            f"{'yes' if p.has_jsonld else 'no'} |"
        )
    lines.append("")
    lines.append("---")
    lines.append(f"Generated by `scripts/seo_audit.py` on {today}.")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_path.write_text(json.dumps({
        "date": today,
        "mode": mode,
        "pages": {k: asdict(v) for k, v in pages.items()},
        "issues": [asdict(i) for i in all_issues],
        "fixes_applied": fixes_applied,
        "content_proposals": content_proposals,
    }, indent=2), encoding="utf-8")

    return md_path, json_path


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(mode: str) -> int:
    page_paths = load_pages()
    pages: dict[str, PageInfo] = {}
    all_issues: list[Issue] = []

    # Initial audit
    for path in page_paths:
        info, issues = audit_page(path)
        pages[path.name] = info
        all_issues.extend(issues)

    all_issues.extend(audit_internal_links(pages))
    all_issues.extend(audit_sitemap(pages))

    fixes_applied: list[str] = []
    content_proposals: list[dict[str, str]] = []

    if mode == "safe-fix":
        # Refresh sitemap
        fixes_applied.extend(refresh_sitemap(pages))
        # Per-page mechanical fixes
        for path in page_paths:
            page_changes = apply_safe_fixes(path, pages[path.name])
            fixes_applied.extend(f"{path.name}: {c}" for c in page_changes)
        # Re-audit after fixes so the report reflects the new state
        pages = {}
        all_issues = []
        for path in page_paths:
            info, issues = audit_page(path)
            pages[path.name] = info
            all_issues.extend(issues)
        all_issues.extend(audit_internal_links(pages))
        all_issues.extend(audit_sitemap(pages))
        # Content proposals — reported but NOT applied here
        content_proposals = content_suggestions(pages)

    elif mode == "content-pr":
        # Apply content suggestions on a branch (workflow handles branching)
        content_proposals = content_suggestions(pages)
        applied = apply_content_suggestions(content_proposals)
        fixes_applied.extend(applied)

    elif mode == "audit-only":
        content_proposals = content_suggestions(pages)

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        return 2

    md_path, json_path = write_report(
        pages, all_issues, fixes_applied, content_proposals, mode)

    # Emit GitHub Actions outputs if available
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"issues_high={sum(1 for i in all_issues if i.severity == 'high')}\n")
            f.write(f"issues_total={len(all_issues)}\n")
            f.write(f"fixes_applied={len(fixes_applied)}\n")
            f.write(f"content_proposals={len(content_proposals)}\n")
            f.write(f"report_md={md_path.relative_to(ROOT)}\n")

    print(f"Report: {md_path}")
    print(f"JSON:   {json_path}")
    print(f"Issues: {len(all_issues)}  |  Fixes: {len(fixes_applied)}  |  "
          f"Content proposals: {len(content_proposals)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SEO audit + fix for hrexperttogo.com")
    parser.add_argument("--mode", default="safe-fix",
                        choices=["safe-fix", "content-pr", "audit-only"])
    args = parser.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    sys.exit(main())
