# -*- coding: utf-8 -*-
"""每日流水线：抓取 → 热度信号 → 去重 → 打分 → 配额选取 → 落盘"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

from fetch_daily import SH, collect
from score import run as score_run
from store import merge_new, items_in_range, load_all

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

N = int(os.environ.get("DIGEST_N", "10"))
# 候选窗口天数：热度需要时间沉淀，只筛单日等于把热度信号全排除（见 store.items_in_range）
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))


def build(target_date: dt.date, n=N, verbose=True, fetch=True,
          window_days=WINDOW_DAYS) -> dict:
    """fetch=False 时跳过联网抓取，直接用库存补生成历史日期的日报。

    target_date 是窗口的最后一天（默认=昨天），窗口为 [target-6, target] 共 7 天。
    今天的不收：当天发布的内容还没来得及产生任何热度。
    """
    new_stored = 0
    if fetch:
        # 1) 抓取：抓到什么算什么，不在抓取阶段做日期过滤
        fetched, fetch_stats = collect(verbose=verbose)
        # 2) 落库：并入 data/seen.jsonl 长期留存，这样补跑历史日期不会丢内容
        pooled, new_stored = merge_new(fetched)
    else:
        fetch_stats = []
        pooled = load_all()

    # 4) 热度信号：真实阅读排行 + 各站首页排序。失败不阻断出刊
    heat_sig, heat_stats = {}, []
    if fetch:
        try:
            import heat
            heat_sig = heat.collect(verbose=verbose)
        except Exception as e:
            print(f"  [提示] 热度信号采集跳过（不影响出刊）：{type(e).__name__} {str(e)[:80]}")

    # 3) 取候选：近 7 天发布的全部条目（不是单日）
    start_date = target_date - dt.timedelta(days=window_days - 1)
    items = items_in_range(start_date, target_date)

    # 3b) 站方阅读榜条目豁免时间窗口。
    # 谷德「7 天最热」按的是近 7 日阅读量，榜上条目完全可能发布于 8~9 天前
    # （实测第 1 名发布于窗口前一天）。它此刻就在榜上 = 此刻最热，
    # 因为发布日期早几天就丢掉，等于把最热的案例排除掉，与「以热度为标准」相悖。
    extra = []
    if heat_sig:
        try:
            from heat import strong_urls
            strong = strong_urls(heat_sig)
        except Exception:
            strong = set()
        if strong:
            have = {it["url"] for it in items}
            for o in pooled:
                if o.get("url") in strong and o["url"] not in have:
                    have.add(o["url"])
                    extra.append(o)
    if extra:
        items = items + extra

    # 统计各信源在窗口内的命中数（失败源 hit 记 0 并保留错误信息）
    hit: dict[str, int] = {}
    for it in items:
        hit[it["source"]] = hit.get(it["source"], 0) + 1

    if not fetch_stats:  # 离线补生成时，用窗口内实际命中的信源反推统计
        fetch_stats = [dict(source=k, ok=True, total=v, hit=v, oldest=None,
                            newest=target_date.isoformat())
                       for k, v in hit.items()]

    stats = []
    for s in fetch_stats:
        row = dict(s, hit=hit.get(s["source"], 0))
        newest = s.get("newest")
        if newest:
            # 窗口是 7 天，最新内容落后超过整个窗口才说明该源停更或入口失效
            gap = (target_date - dt.date.fromisoformat(newest)).days
            row["stale_days"] = gap
            row["stale"] = gap > window_days
        stats.append(row)

    picked, dropped, info = score_run(items, n, heat_sig)

    for i, it in enumerate(picked, 1):
        it["rank"] = i
    # 记录命中热度信号的候选比例，用于判断热度信号是否真的起效
    heat_hit = sum(1 for it in items if heat_sig.get(it["url"]))

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
        window_start=start_date.isoformat(),
        window_end=target_date.isoformat(),
        window_days=window_days,
        generated_at=dt.datetime.now(SH).isoformat(timespec="seconds"),
        sources=stats,
        candidate_count=len(items),
        new_stored=new_stored,
        store_total=len(pooled),
        after_dedupe=info["after_dedupe"],
        noise_count=len(dropped),
        heat_signal_count=len(heat_sig),
        heat_hit_count=heat_hit,
        out_of_window_count=len(extra),
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
    print(f"[{res['window_start']} ~ {res['window_end']}] 库存 {res['store_total']} 条"
          f"（本次新增 {res['new_stored']} 条），窗口内候选 {res['candidate_count']} 条，"
          f"其中带热度信号 {res['heat_hit_count']} 条（信号库共 {res['heat_signal_count']} 条），"
          f"榜上热文破窗补入 {res['out_of_window_count']} 条，"
          f"过滤噪音 {res['noise_count']} 条，入选 {res['count']} 条")
    for s in res["sources"]:
        if not s.get("ok"):
            print(f"  [失败] {s['source']}: {s.get('error')}")
        elif s.get("stale"):
            print(f"  [过期] {s['source']}: 最新内容落后 {s['stale_days']} 天"
                  f"（最新 {s.get('newest')}）")
    print()
    for it in res["items"]:
        tag = f"{it.get('btype') or '-'}/{it.get('region') or '-'}"
        hot = it.get("heat_labels") or []
        print(f"{it['rank']:>2}. [{it['source']}] 热度 {it.get('heat_score', 0):>5.1f}"
              f" + 质量 {it.get('quality_score', 0):>4.1f} = {it['score']:>5.1f}  {tag}")
        print(f"    {it['title']}")
        if hot:
            print(f"    热度来源：{' + '.join(hot)}")
        print(f"    {it['url']}")
        print()
