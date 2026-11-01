#!/usr/bin/env python3
"""Run many CNKI searches from a TSV, one JSONL per query, resumable.

Usage:
  cnki_batch.py queries.tsv --out-dir search/
  cnki_batch.py queries.tsv --out-dir search/ --only A,B

queries.tsv (tab-separated, header required):
  label   query   field   db   sort   pages   name
  A       法律的经济分析   SU    JOURNAL   CF   2   法经济学基础理论
  A       法经济学         SU    JOURNAL   PT   1   法经济学基础理论

label groups rows into a theme; name is the human-readable group title.
field/db/sort/pages follow cnki_search.py (SU/TI/AU..., JOURNAL/ALL..., CF/PT/FFD..., 1-5).
Blank field/db/sort fall back to SU/JOURNAL/FFD; blank pages to 1.

Writes <out-dir>/<label>__<n>_<sort>.jsonl and <out-dir>/index.json.
Existing non-empty JSONL files are skipped, so an interrupted run resumes.
Prints one line per query; exits 0 even when individual queries return 0 rows.

Exit codes: 0 ok, 3 not logged in, 4 Chrome not running, 5 captcha unsolved, 6 page changed.
"""
import argparse, csv, json, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable
COLS = ["label", "query", "field", "db", "sort", "pages", "name"]


def find_scripts_dir(explicit=""):
    """定位 cnki_search.py：显式参数 > 本文件同级 > 常见 skill 安装位置。"""
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        if (p / "cnki_search.py").exists():
            return p
        sys.exit(f"--scripts-dir {p} 下没有 cnki_search.py")
    if (HERE / "cnki_search.py").exists():
        return HERE
    for cand in (pathlib.Path.home() / ".claude/skills/cnki-browser/scripts",
                 pathlib.Path.home() / ".codex/skills/cnki-browser/scripts",
                 pathlib.Path.home() / ".agents/skills/cnki-browser/scripts"):
        if (cand / "cnki_search.py").exists():
            return cand
    sys.exit("找不到 cnki_search.py：用 --scripts-dir 指定 cnki-browser/scripts 的位置")


def read_queries(path):
    with open(path, encoding="utf-8-sig") as fh:
        r = csv.DictReader(fh, delimiter="\t")
        missing = [c for c in COLS[:2] if c not in (r.fieldnames or [])]
        if missing:
            sys.exit(f"queries.tsv 缺列: {missing}，表头应为 {COLS}")
        out = []
        for row in r:
            q = {c: (row.get(c) or "").strip() for c in COLS}
            if not q["query"]:
                continue
            q["field"] = q["field"] or "SU"
            q["db"] = q["db"] or "JOURNAL"
            q["sort"] = q["sort"] or "FFD"
            q["pages"] = q["pages"] or "1"
            sorts = [x.strip().upper() for x in (q["sort"] or "FFD").split(",") if x.strip()]
            for st in sorts:
                qq = dict(q, sort=st)
                out.append(qq)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--only", default="", help="只跑这些 label，逗号分隔")
    ap.add_argument("--scripts-dir", default="", help="cnki_search.py 所在目录")
    ap.add_argument("--stop-on-captcha", action="store_true",
                    help="遇 exit 5 立即停止，其余失败的查询继续")
    a = ap.parse_args()

    out_dir = pathlib.Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = find_scripts_dir(a.scripts_dir)
    qs = read_queries(a.queries)
    if a.only:
        keep = {s.strip() for s in a.only.split(",") if s.strip()}
        qs = [q for q in qs if q["label"] in keep]

    index_path = out_dir / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else []
    done = {e["file"] for e in index}
    summary = []

    for i, q in enumerate(qs, 1):
        slug = f'{q["label"]}__{i:03d}_{q["sort"]}'
        fname = f"{slug}.jsonl"
        out = (out_dir / fname).resolve()
        if fname in done and out.exists() and out.stat().st_size > 0:
            print(f"[skip] {fname}", flush=True)
            continue
        cmd = [PY, str(scripts_dir / "cnki_search.py"), q["query"],
               "--field", q["field"], "--db", q["db"], "--sort", q["sort"],
               "--pages", q["pages"], "--out", str(out)]
        t0 = time.time()
        p = subprocess.run(cmd, cwd=scripts_dir, capture_output=True, text=True)
        rows = sum(1 for _ in out.open()) if out.exists() else 0
        sec = round(time.time() - t0, 1)
        entry = {"file": fname, "label": q["label"], "name": q["name"], "query": q["query"],
                 "field": q["field"], "db": q["db"], "sort": q["sort"],
                 "pages": int(q["pages"]), "rows": rows, "exit": p.returncode, "sec": sec}
        summary.append(entry)
        print(f'[{i}/{len(qs)}] {q["label"]} {q["query"]} {q["sort"]} '
              f'rows={rows} exit={p.returncode} {sec}s', flush=True)
        if p.returncode not in (0, 5):
            print("   stderr:", (p.stderr or "").strip()[-300:], flush=True)
        if p.returncode == 3:
            sys.exit("未登录或 Chrome 未启动：先运行 cnki_chrome.py start，并在窗口里登录。")
        if p.returncode == 5 and a.stop_on_captcha:
            sys.exit("验证码未通过：在专用窗口里手动完成，然后重跑同一命令（会跳过已完成的查询）。")

    index_map = {e["file"]: e for e in index}
    for e in summary:
        index_map[e["file"]] = e
    index_path.write_text(json.dumps(list(index_map.values()), ensure_ascii=False, indent=1))
    ok = sum(1 for e in summary if e["exit"] == 0)
    print(f"\nDONE {len(summary)} 组，成功 {ok}，总行数 {sum(e['rows'] for e in summary)}")
    if summary and ok == 0:
        sys.exit(f"全部 {len(summary)} 组都失败（退出码见上），检查登录状态、Chrome 与网络。")


if __name__ == "__main__":
    main()
