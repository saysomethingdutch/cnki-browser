#!/usr/bin/env python3
"""CNKI search by script: attach to the dedicated Chrome, run an advanced search,
read the result table, write JSONL. No screenshots, no model in the loop.

Usage:
  cnki_search.py "促进型立法 法律责任" --out hits.jsonl
  cnki_search.py "民族团结进步促进法" --field TI --db JOURNAL --sort CF --pages 2 --out x.jsonl
  cnki_search.py "..." --count-only

Fields: SU 主题 TKA 篇关摘 KY 关键词 TI 篇名 FT 全文 AU 作者 AF 作者单位 AB 摘要 ...
Sort:   FFD 相关度  PT 发表时间  CF 被引  DFR 下载  ZH 综合
DB:     ALL 总库  JOURNAL 学术期刊  DISSERTATION 学位论文  CONFERENCE 会议  NEWSPAPER 报纸

Exit codes: 0 ok, 3 not logged in / Chrome not running, 5 captcha not solved, 6 page changed.
"""
import argparse, json, pathlib, random, re, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cnki_common as C

FIELD_NAMES = {"SU": "主题", "TKA": "篇关摘", "KY": "关键词", "TI": "篇名", "FT": "全文", "AU": "作者",
               "FI": "第一作者", "AF": "作者单位", "FU": "基金", "AB": "摘要", "RF": "参考文献"}

ROWS_JS = r"""() => [...document.querySelectorAll('table.result-table-list tbody tr')].map(tr => {
  const q = s => tr.querySelector(s);
  const txt = s => { const e = q(s); return e ? e.innerText.trim().replace(/\s+/g, ' ') : ''; };
  const a = q('td.name a');
  const marks = [...tr.querySelectorAll('td.name .marktip, td.name b, td.name .icon-pre')].map(e => e.innerText.trim()).filter(Boolean);
  return {
    n: txt('td.seq') || txt('td:nth-child(1)'),
    title: a ? a.innerText.trim().replace(/\s+/g, ' ') : '',
    url: a ? a.href : '',
    authors: txt('td.author'),
    source: txt('td.source'),
    date: txt('td.date'),
    doc_type: txt('td.data'),
    cited: txt('td.quote'),
    downloads: txt('td.download'),
    marks,
  };
})"""


def set_field(pg, code):
    if code == "SU":
        return
    pg.locator("dd").first.locator(".sort-default").first.click()
    time.sleep(0.4)
    pg.locator(f"dd .sort-list li[data-val='{code}']").first.click()
    time.sleep(0.4)


def total_count(pg):
    m = re.search(r"共找到\s*([\d,]+)\s*条结果", pg.inner_text("body"))
    return int(m.group(1).replace(",", "")) if m else None


def wait_results(pg, timeout=25):
    for _ in range(timeout * 2):
        time.sleep(0.5)
        C.handle_captcha(pg)
        body = pg.inner_text("body")
        if re.search(r"共找到|暂无数据|抱歉", body) and not C.captcha_visible(pg):
            time.sleep(0.8)
            return
    raise C.SkillError("search results did not load", C.EXIT_LAYOUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--field", default="SU", choices=sorted(FIELD_NAMES))
    ap.add_argument("--db", default="JOURNAL", help="ALL, JOURNAL, DISSERTATION, CONFERENCE, NEWSPAPER")
    ap.add_argument("--sort", default="FFD", choices=["FFD", "PT", "CF", "DFR", "ZH"])
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--out")
    ap.add_argument("--count-only", action="store_true")
    a = ap.parse_args()

    with C.attached() as (ctx, pg):
        pg.goto(C.ADV_URL, wait_until="domcontentloaded")
        time.sleep(2.5)
        C.handle_captcha(pg)
        set_field(pg, a.field)
        box = pg.locator("dd").first.locator("input[type=text]").first
        box.click()
        box.fill(a.query)
        time.sleep(0.5)
        pg.keyboard.press("Escape")
        pg.mouse.click(5, 5)
        time.sleep(0.4)
        try:
            pg.locator("input.btn-search").first.click(timeout=5000)
        except Exception:
            box.click()
            box.press("Enter")
        wait_results(pg)

        DB_LABEL = {"JOURNAL": "学术期刊", "DISSERTATION": "学位论文", "CONFERENCE": "会议", "NEWSPAPER": "报纸"}
        if a.db != "ALL":
            tab = pg.locator(f"a[resource='{a.db}']:visible").first
            if not tab.count():
                raise C.SkillError(f"database tab {a.db} not found", C.EXIT_LAYOUT)
            tab.click()
            for _ in range(30):
                time.sleep(0.5)
                C.handle_captcha(pg)
                active = pg.evaluate("()=>[...document.querySelectorAll('.cur [resource], a.cur[resource]')].map(e=>e.getAttribute('resource'))")
                if a.db in active:
                    break
            else:
                raise C.SkillError(f"could not switch scope to {a.db}", C.EXIT_LAYOUT)
            wait_results(pg)
        total = total_count(pg)
        if not total:
            body = pg.inner_text("body")
            if re.search(r"暂无数据|抱歉", body) or total == 0:
                total = 0
                if a.count_only:
                    print(json.dumps({"query": a.query, "field": a.field, "db": a.db, "total": 0}, ensure_ascii=False))
                    return C.EXIT_OK
                out = pathlib.Path(a.out) if a.out else C.default_out(a.query)
                out.write_text("", encoding="utf-8")
                C.digest({"status": "ok", "query": a.query, "field": FIELD_NAMES[a.field], "db": a.db,
                          "total": 0, "rows": 0, "saved": str(out)})
                return C.EXIT_OK
        if a.sort != "FFD":
            pg.locator(f"li#{a.sort}").first.click()
            wait_results(pg)
            total = total_count(pg)
        crit = re.search(r"检索范围：[^\n]+", pg.inner_text("body"))
        criteria = crit.group(0) if crit else ""
        if a.query.split()[0] not in criteria:
            raise C.SkillError(f"query not reflected on page ({criteria!r}); autocomplete may have replaced it",
                               C.EXIT_LAYOUT)
        if a.count_only:
            print(json.dumps({"query": a.query, "field": a.field, "db": a.db, "total": total,
                              "criteria": criteria}, ensure_ascii=False))
            return C.EXIT_OK

        rows, seen = [], set()
        for page_no in range(1, a.pages + 1):
            for r in pg.evaluate(ROWS_JS):
                if not r["url"] or r["url"] in seen:
                    continue
                seen.add(r["url"])
                r.update(page=page_no, query=a.query, field=a.field, scope=a.db, sort=a.sort)
                rows.append(r)
            if page_no == a.pages:
                break
            nxt = pg.locator("#PageNext")
            if not nxt.count():
                break
            time.sleep(random.uniform(*C.PAUSE))
            nxt.click()
            wait_results(pg)

        out = pathlib.Path(a.out) if a.out else C.default_out(a.query)
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        C.digest({"status": "ok", "query": a.query, "field": FIELD_NAMES[a.field], "db": a.db,
                  "sort": a.sort, "total": total, "rows": len(rows), "criteria": criteria, "saved": str(out)},
                 [f"{r['n']}. {r['title'][:40]} | {r['authors'][:16]} | {r['source'][:14]} | {r['date'][:10]} | 被引{r['cited'] or 0}"
                  for r in rows[:5]])
        return C.EXIT_OK


if __name__ == "__main__":
    sys.exit(C.run(main))
