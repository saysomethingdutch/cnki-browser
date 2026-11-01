---
name: cnki-browser
description: Search and download from 中国知网 (CNKI) by script, not by screenshot. Use when a task needs CNKI search counts, result lists, abstracts, or full-text PDFs — 知网检索、知网下载、查文献热度、补全文. Attaches Playwright to a dedicated logged-in Chrome; reads the result table and detail pages from the DOM; downloads PDFs through Chrome; auto-solves the click-word captcha (ddddocr + vision model) and falls back to the user.
---

# CNKI Browser

Read CNKI with scripts, not with eyes. Same pattern as `pkulaw-browser`: a
dedicated Chrome with a debugging port, logged in once by the user (on campus
the institution is recognised by IP automatically), then every script attaches
over CDP. No screenshots, no model in the loop except for the occasional captcha.

Run with `/opt/homebrew/bin/python3` (system Python 3.9 is too old). Scripts are
in `scripts/`. State lives in `~/.agents/state/cnki-browser/`.

## Hard discipline

- **Never drive CNKI with computer-use or screenshots.** The scripts read the DOM.
- Searches pause 2–4.5 s between pages; downloads pause between articles. Keep it
  that way: CNKI raises captchas and temporary limits on bursts.
- Never type the user's credentials. Login is the user's job, in the dedicated window.
- Do not rerun a failed command unchanged; read the exit code.

## Start

```bash
python3 cnki_chrome.py start     # opens the dedicated Chrome on :9334
python3 cnki_chrome.py status
```

First time: the user confirms the window shows "中国政法大学图书馆" (off campus:
机构登录 / CARSI). The profile keeps the login afterwards.

## Search

```bash
python3 cnki_search.py "促进型立法 法律责任" --count-only
python3 cnki_search.py "民族团结进步促进法" --field TI --db JOURNAL --sort CF --pages 2 --out hits.jsonl
```

- `--field`: SU 主题 (default), TKA 篇关摘, KY 关键词, TI 篇名, FT 全文, AU 作者, AF 作者单位, AB 摘要, RF 参考文献
- `--db`: JOURNAL (default), ALL, DISSERTATION, CONFERENCE, NEWSPAPER
- `--sort`: FFD 相关度 (default), PT 发表时间, CF 被引, DFR 下载, ZH 综合
- 20 rows per page. Each JSONL row: `n, title, url, authors, source, date, doc_type, cited, downloads, marks, page, query, field, scope, sort`.
- The script checks the page's 检索范围 line contains the query. CNKI's search box
  autocompletes; if the query was replaced, it exits 6 instead of returning wrong results.
- `--count-only` prints the hit count without collecting rows — use it to compare phrasings cheaply.
- CNKI's sort buttons toggle (DESC/ASC) and the page remembers the last sort, so `ensure_sort` clicks until the
  requested button is current and descending. Relevance (`FFD`) is set explicitly too.
- `doc_type` is always empty on the JOURNAL tab, which has no 数据库 column.
- Zero results are a normal outcome (exit 0, empty file). The sort bar only exists when there
  are hits, so the script records 0 before trying to sort.
- Long titles often return 0 under `--field TI`. Use 10–15 distinctive characters, or search the
  author with `--field AU --sort PT` and filter the rows.
- Author searches open a suggestion dropdown over the search button; the script closes it and
  falls back to pressing Enter.

## Detail and download

```bash
python3 cnki_detail.py URL --out detail.jsonl                    # abstract, keywords, fund, 分类号, source line
python3 cnki_detail.py --from hits.jsonl --top 5 --download --dest papers_cn/
python3 cnki_detail.py URL --download --caj --dest papers_cn/    # CAJ when there is no PDF
```

- Downloads click the page's own `#pdfDown` button. Chrome saves into a staging
  folder (set via CDP `Browser.setDownloadBehavior`), then the file is renamed to
  `第一作者_年份_题名前16字.pdf` and checked: real `%PDF` / CAJ, not an HTML error page.
- Playwright's `expect_download` does **not** see these downloads (they start in a
  short-lived popup); the CDP download events do. Don't switch back.
- Existing files are skipped (`status: exists`).
- `--max-downloads N` (default 30) caps new downloads per run; later rows get
  `status: skipped-cap` but still have their details extracted.
- File names: first author with affiliation digits and punctuation stripped
  (`段阳伟1,` → `段阳伟`); the year comes from the source line, or, for 网络首发
  papers that have no issue yet, from the online date.

## Captcha

`cnki_common.handle_captcha` runs on every page load and while waiting for results.

1. Detects the Tencent widget (`#tCaptchaDyContent`; it sits off-screen at
   y=-1000000 until triggered — visibility is checked by position, not existence)
   and the full-page `/verify/` click-word captcha.
2. `cnki_captcha.solve`: reads the prompt (`请依次点击【呢,场,小】`) and the base64
   image; ddddocr detects character boxes and reads each; characters OCR misses are
   assigned by a vision model through the relay (default DeepSeek-V4.1-Flash, override
   with `CNKI_VISION_MODEL`); then clicks the centres in order with jitter.
   **Retry ladder:** each failed attempt issues a fresh captcha, so `solve` makes up
   to 5 OCR-only attempts before spending one vision call. A failure costs ~2 s and a
   new image; the vision model costs 10 s+, so the ladder is the cheap path. The
   streak resets when a captcha passes.
   **Find the right tab first:** the captcha usually opens in its own `/verify/home`
   tab, not in the article tab. `solve` searches the other tabs and clicks with the
   *owning* page's mouse — clicking through the original page handle silently does
   nothing (this cost an hour of false debugging on 2026-11-01).
3. If the ladder fails: macOS notification, and the script waits up to
   5 minutes for the user to solve it in the dedicated window, then continues.
   Still unsolved → exit 5.

**Slider (`solve_slider`, added 2026-11-01).** When the click-word path declines,
`handle_captcha` also tries the Tencent slider: it pulls the background and the
piece out of the widget's CSS/canvas as data URLs, trims the piece by its alpha
channel, matches it into the background with `cv2.matchTemplate` (masked), then
drags the handle along an ease-out path with overshoot and jitter. It logs
`slider drag dx=… match=…`. It gives up (returns False → human) when the widget
exposes only one image, when matching scores below 0.35, or when the background
looks segmented — Tencent shuffles the background into strips in some variants,
and no amount of template matching fixes that.

Pacing is an environment variable: `CNKI_PAUSE="0.8,1.8"` for the min/max seconds
between result pages (default `2.0,4.5`). Faster is riskier — CNKI answers bursts
with a captcha on every page, which looks like progress but is not.

By default a captcha that survives the ladder is **not** waited on: `handle_captcha`
raises, `cnki_detail.py` records the row as `error: captcha` in the output JSONL and
in a `.skipped.txt` beside `--out`, then moves to the next article — a failed captcha
costs one article, not five minutes. Rerun the same command later and the skipped
articles are retried (files already present are skipped, so nothing is downloaded
twice). Set `CNKI_CAPTCHA_WAIT=300` to restore the old wait-for-the-human behaviour.

Offline test on a real captcha (2026-10-31): ddddocr read 呢 and 小, missed 场;
the vision model placed 场 correctly; click order correct. Not yet exercised on a
live captcha — the verify page needs a real `ident` from CNKI, so it can't be
summoned on demand. Vision notes: GLM-5.3 burned its whole token budget on
reasoning and returned nothing; DeepSeek with `reasoning.effort=low` answered in
~11 s. Box labels must be large (image scaled 2×, white-on-red numbers) or the
model misreads them.

The user's school network office approved automated captcha solving
(2026-11-01). CNKI's own terms may still disallow it; keep volumes modest.

## Running it from another model (Codex CLI)

Tested 2026-11-01 with GLM-5.3. Give it the task in a file and launch:

```bash
codex -m <glm slug> --disable plugins -s workspace-write \
  --add-dir ~/.agents/state/cnki-browser -c sandbox_workspace_write.network_access=true \
  -a on-request -C <project> "Read <task.md> and follow it exactly."
```

`--disable plugins` removes Computer Use so the model can only call the scripts;
`--add-dir` lets the scripts write their staging folder. Tell the model never to edit
shared tables it is not meant to touch.

## Measured speed (2026-11-01, campus network)

| Step | Time |
|---|---|
| `--count-only` | ~7 s |
| one result page (20 rows) | ~8 s |
| detail + PDF download | 3–5 s per paper |

Before testing, confirm nothing else is driving port 9334: two runs on one Chrome interrupt each other and
produce misleading failures.

## Exit codes

| Code | Meaning | Next step |
|---|---|---|
| 0 | ok | |
| 3 | not logged in | user logs in in the dedicated window |
| 4 | dedicated Chrome not running | `cnki_chrome.py start` |
| 5 | captcha not solved | user solves it, rerun |
| 6 | page changed / query replaced / tab not found | inspect the page; selectors may need updating |

## Selectors (verified 2026-11-01)

- Advanced search: `https://kns.cnki.net/kns8s/AdvSearch`; first row `dd` → `.sort-default` (field menu,
  options `dd .sort-list li[data-val=SU|TI|...]`), `input[type=text]`; submit `input.btn-search`.
- Database tabs: `a[resource=JOURNAL]:visible`; the active one sits under `.cur`.
  The 检索范围 line keeps saying 总库 after switching — check the tab, not the text.
- Sort: `li#FFD|PT|CF|DFR|ZH`. Next page: `#PageNext`. Count: `共找到 N 条结果`.
- Result rows: `table.result-table-list tbody tr` → `td.name a`, `td.author`, `td.source`,
  `td.date`, `td.data`, `td.quote`, `td.download`.
- Detail page: `.wx-tit h1`, `h3#authorpart`, `#ChDivSummary`, `.keywords a`, `.funds`,
  `.top-tip`, `.row .rowtit` (+ next sibling), `#pdfDown`, `#cajDown`.
- Captcha API (from `/verify/js/342.*.js`): `POST /verify-api/get`, `POST /verify-api/web/check`;
  clickWord returns `wordList`, `originalImageBase64`, `token`, `secretKey`; points are AES-ECB
  encrypted with `secretKey`. The scripts click in the page rather than call the API.

## Batch search, merge, and download selection

Three helpers for "survey a field" work — dozens of queries, then one table:

```bash
# 1) batch: one JSONL per query, resumable, index.json records what ran
python3 cnki_batch.py queries.tsv --out-dir search/
python3 cnki_batch.py queries.tsv --out-dir search/ --only A,B

# 2) merge: dedupe by title (keeps the higher-cited copy), mark core journals,
#    print per-group counts, write the table and an optional stats JSON
python3 cnki_table.py search/ --out lit_records.csv --stats stats.json --top 5
python3 cnki_table.py search/ --out lit_records.csv --names labels.tsv

# 3) pick what to download, then fetch details + PDFs
python3 select_download.py lit_records.csv --out selected.jsonl --per-group 3
python3 cnki_detail.py --from selected.jsonl --download --dest papers_cn/
```

`queries.tsv` (tab-separated, header required):

| label | query | field | db | sort | pages | name |
|---|---|---|---|---|---|---|
| A | 法律的经济分析 | SU | JOURNAL | CF,PT | 2 | 法经济学基础理论 |

- `label` groups rows into a theme; `name` is the human-readable group title.
- `sort` takes a comma list — `CF,PT` runs the same query under both citation and recency
  sorting, which is what you want for a field survey: skipping one silently biases the sample.
- Blank field/db/sort/pages fall back to `SU`/`JOURNAL`/`FFD`/`1`.

Behaviour worth knowing:
- `cnki_batch.py` skips a query only when its JSONL exists **and is non-empty**, so a killed run
  resumes; it exits non-zero when every query failed, so a broken path or expired login is loud.
- A query whose last run exited 0 with 0 rows is also skipped on resume. `--stop-on-captcha` stops the batch on exit 5.
- `cnki_table.py` writes the core-journal whitelist to 核心标注 and tags such as 网络首发 to 其他标记;
  `select_download.py` reads only 核心标注.
- `cnki_table.py` keeps the first group as 分组 and only counts the others in the stats, so
  cross-listed papers do not fragment the table. Pass `--names labels.tsv` when the JSONL came
  from an older run that has no `index.json`.
- CNKI's result page no longer exposes CSSCI/北大核心 badges (`marks` only carries 网络首发 and
  similar), so core-journal marking uses a built-in whitelist of law and economics journals.
  Extend `CORE` in `cnki_table.py` when the field moves.

Measured on 2026-11-01: 112 queries (28 terms × CF/PT × 2 pages) in ~22 minutes,
3,438 unique records; a 56-query first pass over the same field had produced 1,561.
Treat the output as a sample — the top 40 by citation plus the top 40 by date per
term, not the field. Say so in any report built from it, and use `cnki_detail.py`
citation chasing for the handful of papers whose reference lists matter.
