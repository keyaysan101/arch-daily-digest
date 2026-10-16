# -*- coding: utf-8 -*-
"""每日流水线：抓取 → 去重 → 打分 → 配额选取 → 落盘"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

from fetch_daily import SH, collect
from score import run as score_run
from store import merge_new, items_for_date, load_all

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

N = int(os.environ.get("DIGEST_N", "10"))


def build(target_date: dt.date, n=N, verbose=True, fetch=True) -> dict:
    """fetch=False 时跳过联网抓取，直接用库存补生成历史日期的日报"""
    new_stored = 0
    if fetch:
        # 1) 抓取：抓到什么算什么，不在抓取阶段做日期过滤
        fetched, fetch_stats = collect(verbose=verbose)
        # 2) 落库：并入 data/seen.jsonl 长期留存，这样补跑历史日期不会丢内容
        pooled, new_stored = merge_new(fetched)
    else:
        fetch_stats = []
        pooled = load_all()
    # 3) 取当日候选：从库存里按发布日期筛选
    items = items_for_date(target_date)

    # 统计各信源在目标日的命中数（失败源 hit 记 0 并保留错误信息）
    hit: dict[str, int] = {}
    for it in items:
        hit[it["source"]] = hit.get(it["source"], 0) + 1

    if not fetch_stats:  # 离线补生成时，用当日实际入选的信源反推统计
        fetch_stats = [dict(source=k, ok=True, total=v, hit=v, oldest=None,
                            newest=target_date.isoformat())
                       for k, v in hit.items()]

    stats = []
    for s in fetch_stats:
        row = dict(s, hit=hit.get(s["source"], 0))
        newest = s.get("newest")
        if newest:
            # 信源最新内容落后目标日 3 天以上，说明该源已停更或抓取入口失效
            gap = (target_date - dt.date.fromisoformat(newest)).days
            row["stale_days"] = gap
            row["stale"] = gap >= 3
        stats.append(row)

    picked, dropped, info = score_run(items, n)

    for i, it in enumerate(picked, 1):
        it["rank"] = i

    # 详情页补抓元数据（面积/年份/摄影等卡片摘要里没有的字段）。
    # 这是增强项：任何异常都不能阻断出刊，所以整体包在 try 里。
    try:
        import enrich
        enrich.enrich_items(picked, verbose=False)
        enrich.apply_cache(picked)
    except Exception as e:
        print(f"  [提示] 详情页补抓跳过（不影响出刊）：{type(e).__name__} {str(e)[:80]}")

    out = dict(
        date=target_date.isoformat(),
        generated_at=dt.datetime.now(SH).isoformat(timespec="seconds"),
        sources=stats,
        candidate_count=len(items),
        new_stored=new_stored,
        store_total=len(pooled),
        after_dedupe=info["after_dedupe"],
        noise_count=len(dropped),
        count=len(picked),
        noise=[dict(title=x["title"], source=x["source"], url=x["url"]) for x in dropped],
        items=picked,
    )
    os.makedirs("data", exist_ok=True)
    with open(f"data/digest_{target_date.isoformat()}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


if __name__ == "__main__":
    offline = any(a in ("--offline", "--no-fetch") for a in sys.argv[1:])
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = rest[0] if rest else None
    if day:
        y, m, d = map(int, day.split("-"))
        target = dt.date(y, m, d)
    else:
        target = (dt.datetime.now(SH) - dt.timedelta(days=1)).date()

    res = build(target, fetch=not offline)
    print(f"[{res['date']}] 库存 {res['store_total']} 条（本次新增 {res['new_stored']} 条），"
          f"当日候选 {res['candidate_count']} 条，过滤噪音 {res['noise_count']} 条，"
          f"入选 {res['count']} 条")
    for s in res["sources"]:
        if not s.get("ok"):
            print(f"  [失败] {s['source']}: {s.get('error')}")
        elif s.get("stale"):
            print(f"  [过期] {s['source']}: 最新内容落后 {s['stale_days']} 天"
                  f"（最新 {s.get('newest')}）")
    print()
    for it in res["items"]:
        tag = f"{it.get('btype') or '-'}/{it.get('region') or '-'}"
        print(f"{it['rank']:>2}. [{it['source']}] {it['score']:>5.1f}  {tag}")
        print(f"    {it['title']}")
        print(f"    {it['url']}")
        print()
