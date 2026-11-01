#!/opt/homebrew/bin/python3
# -*- coding: utf-8 -*-
"""从合并后的题录表里挑出要下全文的文章，输出 cnki_detail.py 能吃到的 JSONL。

每组取：被引最高的 per_group 篇 + 近三年核心期刊里被引最高的 per_group 篇，去重。

用法：
  select_download.py lit_records_script.csv --out selected.jsonl --per-group 3
"""
import argparse, csv, json, pathlib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-group", type=int, default=3)
    ap.add_argument("--since", type=int, default=2023)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    groups = {}
    for r in rows:
        groups.setdefault(r["分组"], []).append(r)

    picked, seen = [], set()

    skipped = 0

    def take(r):
        nonlocal skipped
        if not str(r.get("链接", "")).startswith("http"):
            skipped += 1
            return
        t = r["题名"].strip()
        if t in seen:
            return
        seen.add(t)
        picked.append({
            "title": t, "url": r["链接"], "authors": r["作者"],
            "source": r["刊物"], "date": r["年"], "cited": r["被引"],
            "marks": r["核心标注"], "group": r["分组"],
        })

    for g, sub in groups.items():
        by_cited = sorted(sub, key=lambda r: -int(r["被引"] or 0))
        for r in by_cited[: a.per_group]:
            take(r)
        recent_core = [r for r in sub
                       if r["年"].isdigit() and int(r["年"]) >= a.since and r["核心标注"]]
        for r in sorted(recent_core, key=lambda r: -int(r["被引"] or 0))[: a.per_group]:
            take(r)

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"挑了 {len(picked)} 篇，覆盖 {len(groups)} 组 → {a.out}"
          + (f"（跳过 {skipped} 条无有效链接）" if skipped else ""))


if __name__ == "__main__":
    main()
