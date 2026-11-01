#!/usr/bin/env python3
"""CNKI article detail + optional PDF download, by script.

Usage:
  cnki_detail.py URL [URL ...]            print abstract/keywords/fund/source as JSONL
  cnki_detail.py --from hits.jsonl --top 5 --download --dest papers_cn/
  cnki_detail.py URL --download --dest DIR [--caj]

Download is done by clicking the page's own PDF button and catching the browser
download event; the file is named 第一作者_年份_题名前16字.pdf. After saving, the
script checks the file really is a PDF (or CAJ) and not an HTML error page.
Exit codes as in cnki_common.
"""
import argparse, json, pathlib, random, re, shutil, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cnki_common as C

DETAIL_JS = r"""() => {
  const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim().replace(/\s+/g, ' ') : ''; };
  const rows = {};
  document.querySelectorAll('.row .rowtit, .top-space .rowtit').forEach(e => {
    const k = e.innerText.trim().replace(/[：:]\s*$/, '');
    const v = (e.nextElementSibling || {}).innerText || '';
    if (k && !(k in rows)) rows[k] = v.trim().replace(/\s+/g, ' ');
  });
  const authorLinks = [...document.querySelectorAll('.wx-tit h3#authorpart a, .wx-tit h3#authorpart span')].map(a => a.innerText.trim().replace(/[\d,，;；*\s]+$/, '')).filter(Boolean);
  const orgs = [...document.querySelectorAll('.wx-tit h3:not(#authorpart) a, .wx-tit h3:not(#authorpart) span')].map(a => a.innerText.trim().replace(/^\d+\.\s*/, '')).filter(Boolean);
  return {
    title: t('.wx-tit h1'),
    authors: [...new Set(authorLinks)],
    orgs: [...new Set(orgs)],
    source_line: t('.top-tip'),
    abstract: t('#ChDivSummary') || rows['摘要'] || '',
    keywords: [...document.querySelectorAll('.keywords a, p.keywords a')].map(a => a.innerText.trim().replace(/;$/, '')),
    fund: t('.funds') || rows['基金资助'] || '',
    clc: rows['分类号'] || '',
    online: rows['在线公开时间'] || '',
    indexed: [...document.querySelectorAll('.top-tip .type, .doc-top .type')].map(e => e.innerText.trim()),
    has_pdf: !!document.querySelector('#pdfDown'),
    has_caj: !!document.querySelector('#cajDown'),
  };
}"""


def clean_author(name):
    """Drop affiliation superscripts and punctuation: '段阳伟1,' -> '段阳伟'."""
    return re.sub(r"[\d,，;；*·\s]+$", "", re.sub(r"^\s+", "", name)) or "佚名"


def pub_year(info):
    """Year from the source line (刊名 . 2026 (04)); for 网络首发 papers fall back to the online date."""
    for field in ("source_line", "online"):
        m = re.search(r"(19|20)\d{2}", info.get(field, ""))
        if m:
            return m.group(0)
    return ""


def safe_name(authors, year, title, ext):
    first = clean_author((authors[0] if authors else "佚名").split(";")[0])
    stem = re.sub(r"[\\/:*?\"<>|\s《》“”]+", "", title)[:16]
    return f"{first}_{year or '缺'}_{stem}.{ext}"


def check_file(p):
    head = p.read_bytes()[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head[:3] in (b"CAJ", b"HN\x00") or head.startswith(b"KDH"):
        return "caj"
    if b"<html" in p.read_bytes()[:2000].lower() or b"<!doctype" in p.read_bytes()[:200].lower():
        return "html"
    return "unknown"


def download(ctx, pg, dest, info, caj=False, browser=None):
    """Click the page's PDF/CAJ button; Chrome saves into a staging dir (set via CDP), then rename."""
    btn = pg.locator("#cajDown" if caj else "#pdfDown").first
    if not btn.count():
        return {"status": "no-button"}
    year = pub_year(info)
    name = safe_name(info["authors"], year, info["title"], "caj" if caj else "pdf")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    if target.exists():
        return {"status": "exists", "file": str(target)}
    stage = C.STATE / "staging"
    stage.mkdir(parents=True, exist_ok=True)
    events = {}
    cdp = browser.new_browser_cdp_session()
    cdp.send("Browser.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": str(stage), "eventsEnabled": True})
    cdp.on("Browser.downloadWillBegin", lambda ev: events.setdefault("guid", ev["guid"]))
    cdp.on("Browser.downloadProgress", lambda ev: events.update(state=ev["state"]) if ev["state"] != "inProgress" else None)
    before = {x for x in ctx.pages}
    btn.click()
    deadline = time.time() + 90
    while time.time() < deadline and events.get("state") not in ("completed", "canceled"):
        time.sleep(1)
        for np in [x for x in ctx.pages if x not in before]:
            try:
                C.handle_captcha(np)
            except C.SkillError:
                raise
            except Exception:
                pass
        C.handle_captcha(pg)
    for np in [x for x in ctx.pages if x not in before]:
        try:
            np.close()
        except Exception:
            pass
    if events.get("state") != "completed" or "guid" not in events:
        if "guid" in events:
            (stage / events["guid"]).unlink(missing_ok=True)
        return {"status": "failed", "error": f"download state={events.get('state')}"}
    staged = stage / events["guid"]
    shutil.move(str(staged), str(target))
    kind = check_file(target)
    if kind == "html" or (kind == "unknown" and target.stat().st_size < 20000):
        bad = target.with_suffix(target.suffix + ".bad")
        target.rename(bad)
        return {"status": "not-a-document", "file": str(bad), "kind": kind}
    return {"status": "ok", "file": str(target), "kind": kind, "bytes": target.stat().st_size}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--from", dest="src", help="JSONL from cnki_search.py")
    ap.add_argument("--top", type=int, default=0, help="only the first N rows of --from")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--caj", action="store_true", help="download CAJ instead of PDF")
    ap.add_argument("--dest", default=str(C.STATE / "downloads"))
    ap.add_argument("--out", help="write details JSONL here")
    ap.add_argument("--max-downloads", type=int, default=30,
                    help="stop downloading after this many new files in one run (default 30)")
    a = ap.parse_args()

    urls = list(a.urls)
    if a.src:
        rows = [json.loads(l) for l in open(a.src, encoding="utf-8") if l.strip()]
        urls += [r["url"] for r in (rows[: a.top] if a.top else rows)]
    if not urls:
        ap.error("give URLs or --from")
    dest = pathlib.Path(a.dest).expanduser()
    out_lines, n_ok, n_new = [], 0, 0
    with C.attached() as (ctx, pg):
        for i, url in enumerate(urls):
            if i:
                time.sleep(random.uniform(*C.PAUSE))
            pg.goto(url, wait_until="domcontentloaded")
            time.sleep(2)
            C.handle_captcha(pg)
            info = pg.evaluate(DETAIL_JS)
            if not info["title"]:
                info = {"url": url, "error": "no title on page"}
            else:
                info["url"] = url
                if a.download and n_new >= a.max_downloads:
                    info["download"] = {"status": "skipped-cap", "error": f"--max-downloads {a.max_downloads} reached"}
                elif a.download:
                    info["download"] = download(ctx, pg, dest, info, caj=a.caj, browser=ctx.browser)
                    n_ok += info["download"].get("status") in ("ok", "exists")
                    n_new += info["download"].get("status") == "ok"
            out_lines.append(json.dumps(info, ensure_ascii=False))
            d = info.get("download", {})
            print(f"{i + 1}. {info.get('title', url)[:40]} | 摘要{len(info.get('abstract', ''))}字"
                  + (f" | 下载 {d.get('status')} {pathlib.Path(d['file']).name if d.get('file') else d.get('error', '')}" if d else ""))
    if a.out:
        pathlib.Path(a.out).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    if a.download:
        print(f"downloaded {n_ok}/{len(urls)} -> {dest}")
    return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(C.run(main))
