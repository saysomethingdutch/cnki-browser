---
name: cnki-browser
description: Search and download from 中国知网 (CNKI) by script, not by screenshot. Use when a task needs CNKI search counts, result lists, abstracts, or full-text PDFs — 知网检索、知网下载、查文献热度、补全文. Attaches Playwright to a dedicated logged-in Chrome; reads the result table and detail pages from the DOM; downloads PDFs through Chrome; auto-solves the click-word and slider captchas locally (ddddocr + OpenCV, no external model) and skips the article if that fails.
---

# CNKI Browser

Read CNKI with scripts, not with eyes. Same pattern as `pkulaw-browser`: a
dedicated Chrome with a debugging port, logged in once by the user (on campus
the institution is recognised by IP automatically), then every script attaches
over CDP. No screenshots and no model in the loop, captchas included.

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
2. **Click-word** (`cnki_captcha.solve`, `请依次点击【呢,场,小】` or `请依次单击【呢,场,小】`). Everything is local:
   - ddddocr detects character boxes. It does **not** read each box freely — on rotated,
     coloured characters free reading is wrong more often than right. Instead, for each
     required character it asks the OCR model how likely that character is in each box
     (`probability=True`, over rotations of ±12°, ±25°, ±40°) and picks the assignment
     with the best joint score.
   - If even the best assignment has a character below `CNKI_MIN_CHAR_SCORE` (default
     0.05), it clicks the widget's refresh for a new image instead of guessing.
   - **CNKI often gives no verdict on the first correct answer** and serves a second
     captcha. That is normal, not a failure; the solver just goes again. Up to
     `CNKI_CLICK_ATTEMPTS` (default 8) images per captcha.
   - A pass is read from the widget (`.verify-msg` says 验证成功) or from the page
     leaving `/verify/` — CNKI redirects to the article (`returnUrl`) about 1 s after a pass.
     The server answers `6111` for 验证失败.
   - **Find the right tab first:** the captcha usually opens in its own `/verify/home`
     tab, not in the article tab. `solve` searches the other tabs and clicks with the
     *owning* page's mouse — clicking through the original page handle silently does
     nothing.
3. **Slider — one adaptive solver for every variant** (`solve_slider`). CNKI uses at least two
   slider widgets with unrelated DOM: kns `/verify/home?captchaType=blockPuzzle`
   (`.verify-sub-block`…) and the **download gate** `bar.cnki.net/bar/verify/…` that appears
   when clicking PDF download (Tencent `.tencent-captcha-dy__…`). Writing one solver per
   widget is what broke downloads before (101/101 skipped), so the solver knows no class names:
   - **parts by structure:** background = largest image-bearing element 200 px+ wide (but not
     page-sized); piece = image-bearing element inside it, 8–45 % of its width, at the left;
     handle = small pointer-cursor element below it. Each background candidate is tried in
     turn until one has a piece and a handle.
   - **piece shape:** screenshot the background with the piece shown and hidden; the
     difference is the piece's exact silhouette.
   - **gap, two cues:** *shade* (ring vs inside contrast of that silhouette, strong on the
     download gate) and *edge* (silhouette outline vs Canny edges, strong on kns, where decoy
     outlines exist). Each cue gets a confidence = best / best elsewhere. Drag only when the
     cues agree (and one is ≥ 1.5) or a single cue is ≥ `CNKI_SLIDER_MIN_CONF` (2.7);
     otherwise refresh for a new image. Every agreeing case in testing passed.
   - **closed-loop drag:** move the handle in small steps while reading where the piece
     actually is, release when it sits on the gap. No assumption about the handle-to-piece
     ratio, page zoom or device pixel ratio.
   - **verdict:** page leaves `/verify/` or shows 成功 → pass; 失败/错误 or the piece snapping
     back → fail, then refresh (some widgets keep the same image after a failure). Up to
     `CNKI_SLIDER_ATTEMPTS` (6).
   Measured 2026-11-02: **download gate 15/15**, **kns slider 10/10** (most on the first
   image, ~3 s; worst ~10 s), click-word unaffected (6/6).
4. If both fail: see "skipped, not waited on" below.

**Chained captchas.** CNKI often serves a second captcha right after the first one
passes — typically a slider followed by a click-word on the same `/verify/` page.
`handle_captcha` keeps solving whatever is on screen until the page leaves `/verify/`,
up to `CNKI_CAPTCHA_ROUNDS` (default 4). End-to-end check on 2026-11-01: captchas
injected before three of six article fetches (one click-word, two slider→click-word
chains) were all cleared in 13–18 s and all six articles were read.

Measured on live CNKI captchas, 2026-11-01 (verify page opened directly, nothing else
driving the browser): **click-word 15/15**, 11–19 s each, most passing on the 2nd–6th
image; slider results are under item 3. An earlier click-word version that
read characters freely and fell back to a vision model was slower (30–110 s) and failed
most attempts; it was removed.

**Before testing, make sure nothing else is driving port 9334.** On 2026-11-01 a second
session ran its own captcha tests on the same Chrome at the same time; the two
interrupted each other and produced 0/10 "failures", closed pages and stuck prompts
that had nothing to do with the code.

Pacing is an environment variable: `CNKI_PAUSE="0.8,1.8"` for the min/max seconds
between result pages (default `2.0,4.5`). Faster is riskier — CNKI answers bursts
with a captcha on every page, which looks like progress but is not.

By default a captcha the solvers cannot clear is **not** waited on: `handle_captcha`
raises, `cnki_detail.py` records the row as `error: captcha` in the output JSONL and
in a `.skipped.txt` beside `--out`, then moves to the next article — a failed captcha
costs one article, not five minutes. Rerun the same command later and the skipped
articles are retried (files already present are skipped, so nothing is downloaded
twice). Set `CNKI_CAPTCHA_WAIT=300` to restore the old wait-for-the-human behaviour.

To test the solvers on demand: a verify URL from a real trigger (with its `ident`,
`captchaId` and `returnUrl`) can be reopened and serves a fresh captcha each time;
swapping `captchaType=clickWord` for `blockPuzzle` gives the slider. The download gate is
captured the same way: click `#pdfDown` with downloads denied
(`Browser.setDownloadBehavior` `deny`) until a `bar.cnki.net/bar/verify` tab appears, save its
URL, and reopen it for fresh images. After one pass CNKI may stop showing the gate for a while.

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
| captcha, click-word / slider | 11–19 s / a few s |

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
