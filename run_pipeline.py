#!/usr/bin/env python3
"""
Booking Detection Pipeline — 9-Pass Orchestrator

Runs the full booking detection pipeline on a CSV of domains.
Uses progressive narrowing: fast/cheap methods first, expensive LLM/browser last.

Usage:
    python3 run_pipeline.py --input leads.csv --output results.csv
    python3 run_pipeline.py --input leads.csv --output results.csv --include-linkup --verbose
    python3 run_pipeline.py --input leads.csv --output results.csv --resume
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Local lib modules
from lib.firecrawl_local import scrape_url, crawl_url
from lib.text_scanner_local import load_patterns, scan_text
from lib.llm_analysis_local import analyze, load_prompt
from lib.linkup_local import search_booking, fill_prompt

load_dotenv(override=True)

logger = logging.getLogger("pipeline")

# Paths relative to this script
SCRIPT_DIR = Path(__file__).parent
CONFIGS_DIR = SCRIPT_DIR / "configs"
PROMPTS_DIR = CONFIGS_DIR / "prompts"
DATA_DIR = SCRIPT_DIR / "data"

# Pass include paths for crawling
BOOKING_INCLUDE_PATHS = [
    "/book*", "/reserv*", "/ticket*", "/tour*", "/activit*",
    "/pricing*", "/schedule*", "/shop*", "/order*", "/checkout*",
]
BOOKING_EXCLUDE_PATHS = [
    "/blog*", "/news*", "/press*", "/about*", "/contact*",
    "/faq*", "/team*", "/career*",
]

MAX_CRAWL_MARKDOWN_CHARS = 15000


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

class PipelineResults:
    """Track booking detection results across all passes."""

    def __init__(self, domains: list[str]):
        self.results: dict[str, dict] = {}
        for d in domains:
            self.results[d] = {
                "domain": d,
                "has_booking": None,
                "booking_platform": None,
                "reasoning": None,
                "source_pass": None,
            }
        # Scraped content cache (not saved to CSV, used in-memory between passes)
        self.homepage_html: dict[str, str] = {}
        self.homepage_markdown: dict[str, str] = {}
        self.crawled_pages: dict[str, list[dict]] = {}  # domain -> [{url, markdown}]

    def mark_booking(self, domain: str, has_booking: bool, platform: str | None,
                     reasoning: str | None, source_pass: str):
        if domain in self.results:
            self.results[domain].update({
                "has_booking": has_booking,
                "booking_platform": platform,
                "reasoning": reasoning,
                "source_pass": source_pass,
            })

    def unresolved(self) -> list[str]:
        """Domains not yet classified as has_booking=True."""
        return [d for d, r in self.results.items()
                if r["has_booking"] is not True]

    def resolved_count(self) -> int:
        return sum(1 for r in self.results.values() if r["has_booking"] is True)

    def write_csv(self, path: str):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "domain", "has_booking", "booking_platform", "reasoning", "source_pass",
            ])
            writer.writeheader()
            for r in self.results.values():
                row = dict(r)
                row["has_booking"] = {True: "true", False: "false", None: "unknown"}.get(
                    row["has_booking"], "unknown"
                )
                writer.writerow(row)


# ---------------------------------------------------------------------------
# Intermediate CSV helpers
# ---------------------------------------------------------------------------

def save_intermediate(data: list[dict], filename: str):
    """Save intermediate pass data to data/ directory."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / filename
    if not data:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    logger.info("Saved %d rows to %s", len(data), path)


def load_intermediate(filename: str) -> list[dict] | None:
    """Load intermediate CSV if it exists (for --resume)."""
    path = DATA_DIR / filename
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Pass implementations
# ---------------------------------------------------------------------------

def pass1_scrape_homepage(results: PipelineResults, api_key: str,
                          workers: int = 5, delay: float = 0.3) -> int:
    """Pass 1: Scrape homepage of each domain using Firecrawl."""
    domains = list(results.results.keys())
    logger.info("Pass 1/9: Scraping %d homepages...", len(domains))
    scraped_count = 0
    intermediate = []

    def scrape_one(domain):
        url = f"https://{domain}"
        result = scrape_url(url, api_key)
        return domain, result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for domain in domains:
            futures[executor.submit(scrape_one, domain)] = domain
            time.sleep(delay)

        for future in as_completed(futures):
            domain, result = future.result()
            if result["status"] == "success" and (result["html"] or result["markdown"]):
                results.homepage_html[domain] = result["html"]
                results.homepage_markdown[domain] = result["markdown"]
                scraped_count += 1
                intermediate.append({
                    "domain": domain,
                    "status": "success",
                    "html_length": len(result["html"]),
                    "markdown_length": len(result["markdown"]),
                })
            else:
                intermediate.append({
                    "domain": domain,
                    "status": "error",
                    "html_length": 0,
                    "markdown_length": 0,
                })
                # Mark dead sites
                results.mark_booking(domain, False, None,
                                     f"Failed to scrape: {result.get('error', 'unknown')}",
                                     "scrape_failed")

    save_intermediate(intermediate, "pass1_scraped.csv")
    logger.info("Pass 1 complete: %d/%d scraped successfully", scraped_count, len(domains))
    return scraped_count


def pass2_llm_html(results: PipelineResults, api_key: str,
                   workers: int = 5, delay: float = 0.2) -> int:
    """Pass 2: LLM analysis on homepage HTML."""
    prompt = load_prompt(str(PROMPTS_DIR / "booking_detection_html.txt"))
    domains = [d for d in results.unresolved() if d in results.homepage_html]
    logger.info("Pass 2/9: LLM on %d homepage HTMLs...", len(domains))
    hits = 0

    def analyze_one(domain):
        html = results.homepage_html[domain]
        # Truncate to avoid token limits
        html_truncated = html[:50000]
        result = analyze(
            text=html_truncated,
            prompt_template=prompt,
            api_key=api_key,
            placeholders={"homepage_html": html_truncated},
        )
        return domain, result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for domain in domains:
            futures[executor.submit(analyze_one, domain)] = domain
            time.sleep(delay)

        for future in as_completed(futures):
            domain, result = future.result()
            if result["status"] == "success" and result["parsed"]:
                parsed = result["parsed"]
                has_booking = parsed.get("has_booking", False)
                if has_booking:
                    hits += 1
                results.mark_booking(
                    domain, has_booking,
                    parsed.get("booking_platform"),
                    parsed.get("reasoning"),
                    "llm_html",
                )

    logger.info("Pass 2 complete: %d/%d have booking", hits, len(domains))
    return hits


def pass3_regex_homepage(results: PipelineResults, patterns: list) -> int:
    """Pass 3: Regex scan on homepage HTML/markdown."""
    domains = [d for d in results.unresolved()
               if d in results.homepage_html or d in results.homepage_markdown]
    logger.info("Pass 3/9: Regex on %d homepages...", len(domains))
    hits = 0

    for domain in domains:
        text = (results.homepage_html.get(domain, "") + "\n" +
                results.homepage_markdown.get(domain, ""))
        scan_hits = scan_text(text, patterns)
        if scan_hits:
            # Any booking_platform or payment_signal category = has_booking
            platform_hits = [h for h in scan_hits
                             if h["category"] in ("booking_platform", "ecommerce", "payment_signal")]
            if platform_hits:
                hits += 1
                platform = platform_hits[0]["label"]
                labels = ", ".join(h["label"] for h in scan_hits)
                results.mark_booking(domain, True, platform,
                                     f"Regex detected: {labels}", "regex_homepage")

    logger.info("Pass 3 complete: %d hits", hits)
    return hits


def pass4_crawl_booking_pages(results: PipelineResults, api_key: str,
                              workers: int = 3, delay: float = 1.0) -> int:
    """Pass 4: Path-filtered subpage crawl."""
    domains = results.unresolved()
    logger.info("Pass 4/9: Crawling booking pages for %d domains...", len(domains))
    crawled = 0

    def crawl_one(domain):
        url = f"https://{domain}"
        pages = crawl_url(url, api_key,
                          include_paths=BOOKING_INCLUDE_PATHS,
                          exclude_paths=BOOKING_EXCLUDE_PATHS,
                          limit=20)
        return domain, pages

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for domain in domains:
            futures[executor.submit(crawl_one, domain)] = domain
            time.sleep(delay)

        for future in as_completed(futures):
            domain, pages = future.result()
            if pages:
                results.crawled_pages[domain] = pages
                crawled += 1

    logger.info("Pass 4 complete: %d/%d domains had crawlable booking pages", crawled, len(domains))
    return crawled


def pass5_regex_subpages(results: PipelineResults, patterns: list) -> int:
    """Pass 5: Regex scan on crawled subpages from pass 4."""
    domains = [d for d in results.unresolved() if d in results.crawled_pages]
    logger.info("Pass 5/9: Regex on %d domains' subpages...", len(domains))
    hits = 0

    for domain in domains:
        pages = results.crawled_pages[domain]
        combined = "\n".join(p["markdown"] for p in pages)
        scan_hits = scan_text(combined, patterns)
        if scan_hits:
            platform_hits = [h for h in scan_hits
                             if h["category"] in ("booking_platform", "ecommerce", "payment_signal")]
            if platform_hits:
                hits += 1
                platform = platform_hits[0]["label"]
                labels = ", ".join(h["label"] for h in scan_hits)
                results.mark_booking(domain, True, platform,
                                     f"Regex on subpages: {labels}", "regex_subpages")

    logger.info("Pass 5 complete: %d hits", hits)
    return hits


def pass6_straight_crawl(results: PipelineResults, api_key: str,
                         workers: int = 3, delay: float = 1.0) -> int:
    """Pass 6: Straight crawl (no path filter) for remaining domains."""
    domains = results.unresolved()
    logger.info("Pass 6/9: Straight crawl for %d domains...", len(domains))
    crawled = 0

    def crawl_one(domain):
        url = f"https://{domain}"
        pages = crawl_url(url, api_key, limit=25)
        return domain, pages

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for domain in domains:
            futures[executor.submit(crawl_one, domain)] = domain
            time.sleep(delay)

        for future in as_completed(futures):
            domain, pages = future.result()
            if pages:
                # Merge with existing crawled pages
                existing = results.crawled_pages.get(domain, [])
                existing_urls = {p["url"] for p in existing}
                new_pages = [p for p in pages if p["url"] not in existing_urls]
                results.crawled_pages[domain] = existing + new_pages
                crawled += 1

    logger.info("Pass 6 complete: %d/%d domains crawled", crawled, len(domains))
    return crawled


def pass7_regex_straight_crawl(results: PipelineResults, patterns: list) -> int:
    """Pass 7: Regex on straight-crawl results."""
    domains = [d for d in results.unresolved() if d in results.crawled_pages]
    logger.info("Pass 7/9: Regex on %d domains' straight crawl...", len(domains))
    hits = 0

    for domain in domains:
        pages = results.crawled_pages[domain]
        combined = "\n".join(p["markdown"] for p in pages)
        scan_hits = scan_text(combined, patterns)
        if scan_hits:
            platform_hits = [h for h in scan_hits
                             if h["category"] in ("booking_platform", "ecommerce", "payment_signal")]
            if platform_hits:
                hits += 1
                platform = platform_hits[0]["label"]
                labels = ", ".join(h["label"] for h in scan_hits)
                results.mark_booking(domain, True, platform,
                                     f"Regex on crawl: {labels}", "regex_straight_crawl")

    logger.info("Pass 7 complete: %d hits", hits)
    return hits


def pass8_llm_crawled(results: PipelineResults, api_key: str,
                      workers: int = 5, delay: float = 0.2) -> int:
    """Pass 8: LLM analysis on concatenated crawled markdown."""
    prompt = load_prompt(str(PROMPTS_DIR / "booking_detection_crawled.txt"))
    domains = [d for d in results.unresolved() if d in results.crawled_pages]
    logger.info("Pass 8/9: LLM on %d domains' crawled content...", len(domains))
    hits = 0

    def analyze_one(domain):
        pages = results.crawled_pages[domain]
        combined = "\n\n---\n\n".join(
            f"Page: {p['url']}\n{p['markdown']}" for p in pages
        )[:MAX_CRAWL_MARKDOWN_CHARS]
        result = analyze(
            text=combined,
            prompt_template=prompt,
            api_key=api_key,
            placeholders={"domain": domain, "page_content": combined},
        )
        return domain, result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for domain in domains:
            futures[executor.submit(analyze_one, domain)] = domain
            time.sleep(delay)

        for future in as_completed(futures):
            domain, result = future.result()
            if result["status"] == "success" and result["parsed"]:
                parsed = result["parsed"]
                has_booking = parsed.get("has_booking", False)
                if has_booking:
                    hits += 1
                results.mark_booking(
                    domain, has_booking,
                    parsed.get("booking_platform"),
                    parsed.get("reasoning"),
                    "llm_crawled",
                )

    logger.info("Pass 8 complete: %d/%d have booking", hits, len(domains))
    return hits


def pass9_linkup_deep(results: PipelineResults, api_key: str,
                      delay: float = 0.5) -> int:
    """Pass 9 (optional): Linkup deep validation."""
    prompt_template = load_prompt(str(PROMPTS_DIR / "booking_detection_own_site.md"))
    domains = results.unresolved()
    logger.info("Pass 9/9: Linkup deep search for %d domains ($0.05 each)...", len(domains))
    hits = 0

    for i, domain in enumerate(domains, 1):
        prompt_text = fill_prompt(prompt_template, domain)
        result = search_booking(domain, prompt_text, api_key)

        if result["status"] == "success" and result["has_booking"]:
            hits += 1
            results.mark_booking(
                domain, True,
                result["booking_platform"],
                result["reasoning"],
                "linkup_deep",
            )

        if i % 10 == 0:
            logger.info("Pass 9 progress: %d/%d (%.0f%% done, %d hits)",
                        i, len(domains), 100 * i / len(domains), hits)
        time.sleep(delay)

    logger.info("Pass 9 complete: %d/%d recovered", hits, len(domains))
    return hits


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(input_csv: str, output_csv: str,
                 include_linkup: bool = False, resume: bool = False,
                 verbose: bool = False):
    """Run the full 9-pass booking detection pipeline."""

    # Setup logging
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate API keys
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    linkup_key = os.getenv("LINKUP_API_KEY")

    if not firecrawl_key:
        logger.error("FIRECRAWL_API_KEY not set. Add it to .env")
        sys.exit(1)
    if not openai_key:
        logger.error("OPENAI_API_KEY not set. Add it to .env")
        sys.exit(1)
    if include_linkup and not linkup_key:
        logger.error("LINKUP_API_KEY not set but --include-linkup was specified. Add it to .env")
        sys.exit(1)

    # Read input CSV
    with open(input_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        domains = [row["domain"].strip() for row in reader if row.get("domain", "").strip()]

    if not domains:
        logger.error("No domains found in %s", input_csv)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Booking Detection Pipeline")
    logger.info("Input: %s (%d domains)", input_csv, len(domains))
    logger.info("Output: %s", output_csv)
    logger.info("Linkup: %s", "enabled" if include_linkup else "disabled (use --include-linkup)")
    logger.info("=" * 60)

    results = PipelineResults(domains)
    patterns = load_patterns(str(CONFIGS_DIR / "booking_fingerprints.yaml"))
    start_time = time.time()

    # Pass 1: Scrape homepages
    pass1_scrape_homepage(results, firecrawl_key)
    logger.info("After pass 1: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 2: LLM on homepage HTML
    pass2_llm_html(results, openai_key)
    logger.info("After pass 2: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 3: Regex on homepage
    pass3_regex_homepage(results, patterns)
    logger.info("After pass 3: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 4: Path-filtered subpage crawl
    pass4_crawl_booking_pages(results, firecrawl_key)

    # Pass 5: Regex on subpages
    pass5_regex_subpages(results, patterns)
    logger.info("After pass 5: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 6: Straight crawl
    pass6_straight_crawl(results, firecrawl_key)

    # Pass 7: Regex on straight crawl
    pass7_regex_straight_crawl(results, patterns)
    logger.info("After pass 7: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 8: LLM on crawled markdown
    pass8_llm_crawled(results, openai_key)
    logger.info("After pass 8: %d resolved, %d unresolved",
                results.resolved_count(), len(results.unresolved()))

    # Pass 9: Linkup deep (optional)
    if include_linkup and linkup_key:
        pass9_linkup_deep(results, linkup_key)
        logger.info("After pass 9: %d resolved, %d unresolved",
                    results.resolved_count(), len(results.unresolved()))

    # Mark remaining unresolved as no_booking
    for domain in results.unresolved():
        r = results.results[domain]
        if r["has_booking"] is None:
            results.mark_booking(domain, False, None,
                                 "No booking detected after all passes", "no_booking")

    # Write final results
    results.write_csv(output_csv)

    elapsed = time.time() - start_time
    total = len(domains)
    booking = sum(1 for r in results.results.values() if r["has_booking"] is True)
    no_booking = total - booking

    logger.info("=" * 60)
    logger.info("Pipeline complete in %.0f seconds", elapsed)
    logger.info("Total: %d | Booking: %d (%.1f%%) | No booking: %d (%.1f%%)",
                total, booking, 100 * booking / total, no_booking, 100 * no_booking / total)
    logger.info("Results written to: %s", output_csv)
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Run the 9-pass booking detection pipeline on a CSV of domains."
    )
    parser.add_argument("--input", required=True, help="Input CSV with 'domain' column")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--include-linkup", action="store_true",
                        help="Enable pass 9 (Linkup deep search, $0.05/domain)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from intermediate CSVs if available")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()
    run_pipeline(args.input, args.output, args.include_linkup, args.resume, args.verbose)


if __name__ == "__main__":
    main()
