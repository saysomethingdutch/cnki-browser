#!/usr/bin/env python3
"""Use signed-in HeinOnline and Westlaw tabs in the dedicated Chrome."""
import argparse
import json
import re
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, sync_playwright

HEIN_ROWS = r"""() => [...document.querySelectorAll('.lucene_search_result_b')].map(card => {
    const heading = card.querySelector('.search-result-heading');
    const link = heading?.querySelector('a');
    const lines = card.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    const end = lines.findIndex(s => /^(PathFinder Subjects|ScholarCheck|Matching Text|Summary|Full Text Not Currently Available)/.test(s));
    const summary = lines.indexOf('Summary');
    const match = lines.indexOf('Turn to matching text');
    return {
        title: (heading?.innerText || '').trim(), url: link?.href || '',
        citation_lines: lines.slice(1, end < 0 ? 8 : end),
        provider_summary: summary >= 0 && lines[summary + 1] !== 'Turn to matching text' ? (lines[summary + 1] || '').slice(0, 700) : '',
        matching_text_excerpt: match >= 0 ? (lines[match + 1] || '').slice(0, 650) : '',
        full_text_available: !card.innerText.includes('Full Text Not Currently Available in HeinOnline'),
    };
}).filter(row => row.title)"""


def page_for(context, *domains):
    for page in context.pages:
        host = urlsplit(page.url).hostname or ""
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return page
    raise SystemExit(f"Open and sign in to {domains[0]} in the dedicated Chrome first.")


def read_rows(page):
    rows = page.evaluate(HEIN_ROWS)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
        years = re.findall(r"\b(?:19|20)\d{2}\b", " ".join(row["citation_lines"]))
        row["year"] = max(map(int, years), default=None)
    return rows


def more(page, limit, min_year=None):
    rows = read_rows(page)
    source = page
    fragment = None
    try:
        while len(rows) < limit:
            if min_year and any(row["year"] and row["year"] < min_year for row in rows[-4:]):
                break
            link = source.locator("a.thenext:visible").last
            if not link.count():
                break
            url = link.evaluate("el => el.href")
            if not fragment:
                fragment = page.context.new_page()
            time.sleep(2.1)
            fragment.goto(url, wait_until="domcontentloaded", timeout=30000)
            chunk = read_rows(fragment)
            if not chunk or chunk[0]["title"] in {row["title"] for row in rows}:
                raise RuntimeError("HeinOnline pagination did not return a new result page")
            offset = len(rows)
            for row in chunk:
                row["rank"] += offset
            rows.extend(chunk)
            source = fragment
        return rows
    finally:
        if fragment:
            fragment.close()


def run_hein_query(page, query, relevance_limit, recent_limit, min_year):
    heading = page.locator("#results_total")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        page.locator("#full_text_terms").fill(query)
        page.locator("#sendit_full_text").click(timeout=12000)
    empty_message = "No matching results found."
    page.wait_for_function(
        """message => Boolean(document.querySelector('#results_total')?.innerText.trim()) ||
            document.body.innerText.includes(message)""",
        arg=empty_message, timeout=20000,
    )
    if not heading.count():
        if page.locator("#full_text_terms").get_attribute("placeholder") != "Search Law Journal Library...":
            raise RuntimeError("HeinOnline did not confirm the Law Journal Library scope")
        return {"displayed": empty_message, "total": 0, "relevance": [], "recent": [],
                "recent_boundary_seen": True}
    displayed = heading.inner_text()
    if "Law Journal Library" not in displayed:
        raise RuntimeError(f"Unexpected search scope: {displayed}")
    match = re.search(r"([\d,]+) results?\b", displayed)
    if not match and not re.search(r"no results|0 results", displayed, re.I):
        raise RuntimeError(f"Could not verify search result count: {displayed}")
    total = int(match.group(1).replace(",", "")) if match else 0
    if not total:
        return {"displayed": displayed, "total": 0, "relevance": [], "recent": [], "recent_boundary_seen": True}
    sort = page.locator("#sortbylong")
    if sort.input_value() != "relevance":
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
            sort.select_option("relevance")
    relevance = more(page, relevance_limit)[:relevance_limit]
    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        sort.select_option("date_desc")
    if sort.input_value() != "date_desc":
        raise RuntimeError("HeinOnline did not confirm newest-first sorting")
    dated = more(page, recent_limit, min_year)
    boundary_seen = len(dated) >= total or any(row["year"] and row["year"] < min_year for row in dated)
    recent = [row for row in dated[:recent_limit] if row["year"] is None or row["year"] >= min_year]
    return {"displayed": displayed, "total": total, "relevance": relevance, "recent": recent,
            "recent_boundary_seen": boundary_seen}


def hein_batch(args):
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        page = page_for(browser.contexts[0], "heinonline.com", "heinonline.org")
        if not page.locator("#full_text_terms").count():
            page.goto(urljoin(page.url, "/HOL/Welcome"), wait_until="domcontentloaded", timeout=30000)
        if page.locator("#full_text_terms").get_attribute("placeholder", timeout=12000) != "Search Law Journal Library...":
            page.get_by_role("link", name="Law Journal Library", exact=True).first.click(timeout=15000)
        failed = False
        for item in queries:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", item["id"]):
                raise ValueError(f"Invalid query id: {item['id']}")
            output = args.out / (item["id"] + ".json")
            if output.exists() and "total" in json.loads(output.read_text(encoding="utf-8")):
                print("Existing result", output.name, flush=True)
                continue
            try:
                result = run_hein_query(page, item["query"], args.relevance, args.recent_limit, args.since)
            except (RuntimeError, PlaywrightError) as error:
                failed = True
                result = {"error": str(error).splitlines()[0]}
            payload = {"database": "HeinOnline Law Journal Library", "date": date.today().isoformat(),
                       **item, "recent_since": args.since, **result}
            output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if "error" in result:
                print(item["id"], result["error"], flush=True)
            else:
                print(item["id"], result["total"], "total;", len(result["relevance"]), "relevance;",
                      len(result["recent"]), "recent;", "date boundary:", result["recent_boundary_seen"], flush=True)
            time.sleep(2.5)
        if failed:
            raise SystemExit(1)


def hein_pdf(args):
    if args.out.exists():
        with args.out.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise RuntimeError(f"Existing file is not a PDF: {args.out}")
        print("Existing PDF", args.out)
        return
    host = urlsplit(args.url).hostname or ""
    if not any(host == domain or host.endswith("." + domain) for domain in ("heinonline.com", "heinonline.org")):
        raise ValueError("Use a HeinOnline article URL from the results")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0]
        page_for(context, "heinonline.com", "heinonline.org")
        article = context.new_page()
        downloads, popups = [], []

        def on_download(download):
            downloads.append(download)

        def on_popup(popup):
            popups.append(popup)
            popup.on("download", on_download)

        try:
            article.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            article.on("download", on_download)
            article.on("popup", on_popup)
            try:
                article.locator('a[title="Download PDF of This Section"]').first.click(timeout=15000)
            except PlaywrightTimeout as error:
                raise RuntimeError("HeinOnline did not expose the section PDF download control") from error
            deadline = time.monotonic() + 40
            while not downloads and time.monotonic() < deadline:
                article.wait_for_timeout(250)
            if not downloads:
                raise RuntimeError("HeinOnline did not produce a PDF download")
            with tempfile.TemporaryDirectory() as directory:
                stage = Path(directory) / "article.pdf"
                downloads[0].save_as(stage)
                data = stage.read_bytes()
                if not data.startswith(b"%PDF-"):
                    raise RuntimeError("Download is not a PDF")
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("xb") as stream:
                    stream.write(data)
            print("Saved PDF", args.out, len(data), "bytes")
        finally:
            for popup in popups:
                if not popup.is_closed():
                    popup.close()
            if not article.is_closed():
                article.close()


WESTLAW_JOURNAL_ROWS = """() => [...document.querySelectorAll('a[id^="cobalt_result_analytical_title"]')].map(link => {
    const card = link.closest('.co_searchContent');
    const source = [...card.querySelectorAll(':scope > :not(.co_searchContent_heading):not(.co_snippet)')]
        .map(el => el.innerText.trim()).filter(Boolean).join(' ').slice(0, 800);
    return {
        title: link.innerText.trim(), url: link.href.split('?')[0], source_text: source,
        snippets: [...card.querySelectorAll('.co_snippet_link')].map(el => el.innerText.trim().slice(0, 500))
    };
})"""


def westlaw_journals(args):
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        page = page_for(browser.contexts[0], "westlaw.com")
        home = urljoin(page.url, "/Search/Home.html")
        page.goto(home, wait_until="domcontentloaded", timeout=30000)
        page.get_by_role("link", name="Secondary Sources", exact=True).first.click(timeout=15000)
        page.get_by_role("link", name="Law Reviews & Journals", exact=True).first.click(timeout=15000)
        page.wait_for_function(
            """() => document.body.innerText.includes('Filtered Publications Law Reviews & Journals')""",
            timeout=30000,
        )
        page.locator("#searchInputId").fill(args.query)
        page.locator("#searchButton").click(timeout=15000)
        page.wait_for_url(lambda url: "/Search/Results" in urlsplit(url).path,
                          wait_until="domcontentloaded", timeout=40000)
        page.wait_for_function(
            r"""() => /Secondary Sources - Law Reviews & Journals\s*\([\d,]+\)/i.test(document.body.innerText) ||
                /no results (?:found|were found)|your search returned no results/i.test(document.body.innerText)""",
            timeout=35000,
        )
        body = page.locator("body").inner_text()
        label = re.search(r"Secondary Sources - Law Reviews & Journals\s*\(([\d,]+)\)", body, re.I)
        total = int(label.group(1).replace(",", "")) if label else 0
        if not label and "Law Reviews & Journals" not in body:
            raise RuntimeError("Westlaw did not confirm the Law Reviews & Journals scope")
        if total:
            page.locator('a[id^="cobalt_result_analytical_title"]').first.wait_for(timeout=15000)
        rows = page.evaluate(WESTLAW_JOURNAL_ROWS)
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        result = {"database": "Westlaw Classic Law Reviews & Journals", "date": date.today().isoformat(),
                  "query": args.query, "total": total, "first_page_only": True, "rows": rows}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(total, "total;", len(rows), "first-page rows; saved", args.out)


def westlaw_keycite(args):
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0]
        page = page_for(context, "westlaw.com")
        if not urlsplit(page.url).path.startswith("/Document/") or args.citation not in page.locator("body").inner_text():
            page.locator("#searchInputId").fill(args.citation)
            page.locator("#searchButton").click(timeout=12000)
            page.wait_for_url(lambda url: urlsplit(url).path.startswith("/Document/"), timeout=35000)
        history_link = page.locator("#coid_relatedInfo_kcJudicialHistory_link")
        history_link.wait_for(timeout=15000)
        page.get_by_text(args.citation, exact=True).first.wait_for(state="attached", timeout=12000)
        original_url = page.url.split("?")[0]
        history_link.click(timeout=12000)
        page.locator(".co_relatedInfo_HistoryItem_TitleArea").first.wait_for(timeout=30000)
        entries = page.locator(".co_relatedInfo_HistoryItem_TitleArea").evaluate_all("""els => els.map(e => ({
            text: e.parentElement.innerText.trim(),
            url: e.querySelector('a[href*="/Document/"][href*="/View/FullText"]')?.href.split('?')[0] || ''
        }))""")
        body = page.locator("body").inner_text()
        direct = re.search(r"Direct History \(\d+\)(.*?)(?:Related References|This graphic contains|Preferences)", body, re.S)
        if not entries or not direct:
            raise RuntimeError("KeyCite direct history could not be verified")
        record = {"source": "Westlaw KeyCite", "checked_date": date.today().isoformat(),
                  "citation": args.citation, "document_url": original_url,
                  "history_url": page.url.split("?")[0], "direct_history": direct.group(1).strip(),
                  "entries": entries, "pdfs": []}
        for request in args.pdf:
            cite, path = request.split("=", 1)
            path = Path(path)
            target = original_url if cite == args.citation else next(
                (entry["url"] for entry in entries if cite in entry["text"] and entry["url"]), "")
            if not target:
                record["pdfs"].append({"citation": cite, "status": "no matching history link"})
                continue
            article = context.new_page()
            try:
                article.goto(target, wait_until="domcontentloaded", timeout=25000)
                anchor = article.get_by_role("link", name=re.compile(r"Original Image of .*\(PDF\)")).first
                anchor.wait_for(timeout=15000)
                pdf_url = anchor.evaluate("e => e.href")
                response = article.request.get(pdf_url, timeout=25000)
                data = response.body()
                if not response.ok or not data.startswith(b"%PDF-"):
                    raise RuntimeError(f"PDF link returned HTTP {response.status} without a PDF")
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    with path.open("xb") as stream:
                        stream.write(data)
                record["pdfs"].append({"citation": cite, "status": "saved", "path": str(path),
                                       "document_url": target, "bytes": path.stat().st_size})
            except Exception as exc:
                record["pdfs"].append({"citation": cite, "status": "failed", "error": str(exc).splitlines()[0]})
            finally:
                article.close()
        args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(record["direct_history"])
        for item in record["pdfs"]:
            print(item)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp", default="http://127.0.0.1:9334")
    sub = parser.add_subparsers(dest="action", required=True)
    batch = sub.add_parser("hein-batch")
    batch.add_argument("queries", type=Path, help="JSON array of id/query objects")
    batch.add_argument("--out", type=Path, required=True)
    batch.add_argument("--relevance", type=int, default=30)
    batch.add_argument("--since", type=int, default=2024)
    batch.add_argument("--recent-limit", type=int, default=100)
    download = sub.add_parser("hein-pdf")
    download.add_argument("url")
    download.add_argument("--out", type=Path, required=True)
    journals = sub.add_parser("westlaw-journals")
    journals.add_argument("query")
    journals.add_argument("--out", type=Path, required=True)
    keycite = sub.add_parser("westlaw-keycite")
    keycite.add_argument("citation")
    keycite.add_argument("--out", type=Path, required=True)
    keycite.add_argument("--pdf", action="append", default=[], metavar="CITATION=PATH")
    args = parser.parse_args()
    if args.action == "hein-batch":
        hein_batch(args)
    elif args.action == "hein-pdf":
        hein_pdf(args)
    elif args.action == "westlaw-journals":
        westlaw_journals(args)
    else:
        westlaw_keycite(args)


if __name__ == "__main__":
    try:
        main()
    except (PlaywrightError, RuntimeError, ValueError, OSError) as error:
        print(f"error: {str(error).splitlines()[0]}", file=sys.stderr)
        sys.exit(1)
