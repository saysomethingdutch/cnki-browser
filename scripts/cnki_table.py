#!/usr/bin/env python3
"""Merge CNKI batch JSONL into one deduplicated table plus per-group statistics.

Usage:
  cnki_table.py search/ --out lit_records.csv
  cnki_table.py search/ --out lit_records.csv --stats stats.json --top 5

Reads index.json written by cnki_batch.py (falls back to the file-name prefix
before "__" as the group label). Duplicate titles are merged, keeping the row
with the higher citation count and unioning its group labels.

Columns: 编号 分组 作者 题名 刊物 年 期 被引 下载 核心标注 摘要 链接 检索式
摘要 stays empty here — fill it with cnki_detail.py, which also downloads PDFs.

Exit codes: 0 ok, 2 no JSONL found.
"""
import argparse, collections, csv, json, pathlib, re, sys

COLS = ["编号", "分组", "作者", "题名", "刊物", "年", "期", "被引", "下载",
        "核心标注", "摘要", "链接", "检索式"]

# CNKI 新版结果页不返回 CSSCI/北大核心标记，只能按刊物名判断。
# 名单覆盖法学与经济学的主要 CSSCI 来源刊；以刊物名精确匹配，避免"法学"误伤"法学论坛"。
CORE = {
    "中国社会科学", "中国法学", "法学研究", "中外法学", "法学评论", "现代法学", "政法论坛",
    "法学", "法制与社会发展", "环球法律评论", "清华法学", "比较法研究", "法学家", "法商研究",
    "法律科学", "政治与法律", "当代法学", "中国刑事法杂志", "行政法学研究", "知识产权",
    "法学论坛", "华东政法大学学报", "中国法律评论", "国家检察官学院学报", "南大法学",
    "交大法学", "财经法学", "经贸法律评论", "国际法研究", "武大国际法评论",
    "经济研究", "管理世界", "中国工业经济", "经济学(季刊)", "经济学（季刊）",
    "数量经济技术经济研究", "财经研究", "金融研究", "世界经济", "中国农村经济",
    "财贸经济", "中国软科学", "制度经济学研究", "公共管理学报", "社会学研究",
}


def core_mark(source, marks):
    tags = []
    if str(source or "").strip() in CORE:
        tags.append("CSSCI/核心")
    if isinstance(marks, list):
        tags += [m for m in marks if m and m not in tags]
    elif marks:
        tags.append(str(marks))
    return ";".join(tags)


def year_of(s):
    m = re.search(r"(?:19|20)\d{2}", str(s or ""))
    return m.group(0) if m else "缺"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats", default="")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--names", default="", help="label<TAB>中文名 的映射文件，补齐没有 index.json 的旧结果")
    a = ap.parse_args()

    d = pathlib.Path(a.dir)
    files = sorted(d.glob("*.jsonl"))
    if not files:
        sys.exit(f"{d} 下没有 jsonl。先用 cnki_batch.py 跑检索。")

    meta = {}
    idx = d / "index.json"
    if idx.exists():
        for e in json.loads(idx.read_text()):
            meta[e["file"]] = e

    merged, order = {}, []
    for f in files:
        m = meta.get(f.name, {})
        stem = f.name[:-6] if f.name.endswith(".jsonl") else f.name
        if m.get("label"):
            label = m["label"]
        elif "__" in stem:
            label = stem.split("__")[0]
        else:
            label = re.split(r"[_\W]", stem, maxsplit=1)[0] or stem
        name = m.get("name") or label
        qtext = f'{m.get("field", "")}:{m.get("query", "")}'.strip(":")
        for line in f.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            title = (r.get("title") or "").strip()
            if not title:
                continue
            key = re.sub(r"\s+", "", title)
            cur = merged.get(key)
            if cur is None:
                merged[key] = {
                    "labels": [label], "names": [name],
                    "authors": r.get("authors", ""), "title": title,
                    "source": r.get("source", ""), "year": year_of(r.get("date")),
                    "cited": int(str(r.get("cited") or 0).strip() or 0),
                    "downloads": r.get("downloads", ""),
                    "marks": core_mark(r.get("source"), r.get("marks")),
                    "url": r.get("url", ""), "query": qtext,
                }
                order.append(key)
            else:
                if label not in cur["labels"]:
                    cur["labels"].append(label)
                    cur["names"].append(name)
                if qtext and qtext not in cur["query"]:
                    cur["query"] += f"; {qtext}"
                if int(str(r.get("cited") or 0).strip() or 0) > cur["cited"]:
                    for k in ("authors", "source", "year", "downloads", "url"):
                        cur[k] = r.get(k, cur[k])
                    cur["marks"] = core_mark(r.get("source"), r.get("marks"))
                    cur["cited"] = int(str(r.get("cited") or 0).strip() or 0)

    rows = [merged[k] for k in order]
    # label -> 中文名：优先取 index.json 里非空的 name
    label_name = {}
    if a.names:
        import csv as _csv
        with open(a.names, encoding="utf-8-sig") as _fh:
            for r in _csv.DictReader(_fh, delimiter="\t"):
                if r.get("label"):
                    label_name[r["label"].strip()] = (r.get("name") or "").strip() or r["label"].strip()
    for f in files:
        m = meta.get(f.name, {})
        if m.get("label") and m.get("name") and m["name"] != m["label"]:
            label_name.setdefault(m["label"], m["name"])
    with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for i, r in enumerate(rows, 1):
            w.writerow([f"{r['labels'][0]}{i:04d}", label_name.get(r["labels"][0], r["labels"][0]), r["authors"], r["title"],
                        r["source"], r["year"], "缺", r["cited"], r["downloads"],
                        r["marks"], "", r["url"], r["query"]])

    stats = collections.OrderedDict()
    for label in dict.fromkeys(sum((r["labels"] for r in rows), [])):
        sub = [r for r in rows if label in r["labels"]]
        recent = [r for r in sub if r["year"].isdigit() and int(r["year"]) >= 2023]
        core = [r for r in sub if "CSSCI" in r["marks"] or "核心" in r["marks"]]
        top = sorted(sub, key=lambda r: -r["cited"])[: a.top]
        name = label_name.get(label, label)
        stats[label] = {"name": name, "unique": len(sub), "recent_2023": len(recent),
                        "core": len(core),
                        "top_cited": [{"title": r["title"], "cited": r["cited"], "year": r["year"],
                                       "source": r["source"]} for r in top]}
        print(f"{label:>4} {name[:24]:<26} 唯一 {len(sub):3d}  2023+ {len(recent):3d}  "
              f"核心 {len(core):3d}  最高被引 {top[0]['cited'] if top else 0}")

    if a.stats:
        pathlib.Path(a.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"\n写出 {a.out}：{len(rows)} 条唯一题录 / {len(files)} 个结果文件")


if __name__ == "__main__":
    main()
