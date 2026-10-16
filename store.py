# -*- coding: utf-8 -*-
"""增量落库层：把每次抓到的条目按 URL 去重后追加进 data/seen.jsonl

存在的意义：站点首页只展示最新约 18 条，隔天就被新内容顶掉。
如果只在「生成日报的当天」现抓现用，一旦某天自动化没跑成，那天的内容就永远补不回来。
落库之后，任何一次抓取的产出都会留存下来，之后补跑历史日期仍能拿到完整候选集。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
STORE_PATH = os.path.join("data", "seen.jsonl")


def _load(path: str = STORE_PATH) -> dict:
    """读取库存，返回 {url: item}"""
    items: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                u = o.get("url")
                if u:
                    items[u] = o
    return items


def merge_new(fetched: list[dict], path: str = STORE_PATH) -> tuple[list[dict], int]:
    """把新抓到的条目并入库存，返回 (全部条目, 新增条数)"""
    known = _load(path)
    now = dt.datetime.now(SH).isoformat(timespec="seconds")
    new = 0
    for it in fetched:
        u = it.get("url")
        if not u:
            continue
        if u in known:
            known[u]["last_seen"] = now
            # 补齐之前缺失的字段（如封面图、摘要）
            for k, v in it.items():
                if v and not known[u].get(k):
                    known[u][k] = v
            continue
        rec = dict(it)
        rec["first_seen"] = now
        rec["last_seen"] = now
        known[u] = rec
        new += 1

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in known.values():
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return list(known.values()), new


def load_all(path: str = STORE_PATH) -> list[dict]:
    """返回库存里的全部条目"""
    return list(_load(path).values())


def items_for_date(day: dt.date, path: str = STORE_PATH) -> list[dict]:
    """从库存里取出某一天发布的全部条目，按时间倒序"""
    out = []
    for o in _load(path).values():
        pub = o.get("published_at")
        if not pub:
            continue
        try:
            d = dt.datetime.fromisoformat(pub).date()
        except ValueError:
            continue
        if d == day:
            out.append(o)
    out.sort(key=lambda x: x["published_at"], reverse=True)
    return out


def items_in_range(start: dt.date, end: dt.date, path: str = STORE_PATH) -> list[dict]:
    """从库存里取出 [start, end] 闭区间内发布的全部条目，按时间倒序。

    存在的原因：热度需要时间沉淀——昨天刚发布的案例不可能已经爬上阅读榜
    （实测 Dezeen 热门榜里最旧的一条是 5 月的文章）。只筛「前一天」等于
    把热度信号全部排除在外，所以候选池必须是一个时间窗口而不是单日。
    """
    out = []
    for o in _load(path).values():
        pub = o.get("published_at")
        if not pub:
            continue
        try:
            d = dt.datetime.fromisoformat(pub).date()
        except ValueError:
            continue
        if start <= d <= end:
            out.append(o)
    out.sort(key=lambda x: x["published_at"], reverse=True)
    return out


def all_dates(path: str = STORE_PATH) -> list[str]:
    """库存里已有的全部日期，倒序"""
    ds = set()
    for o in _load(path).values():
        pub = o.get("published_at")
        if pub:
            ds.add(pub[:10])
    return sorted(ds, reverse=True)


def seed_from_raw(path: str = STORE_PATH) -> int:
    """把历史 raw_*.json 里的内容灌进库存，避免升级后旧数据丢失"""
    fetched = []
    for fp in glob.glob("data/raw_*.json"):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        fetched += d.get("items", [])
    _, new = merge_new(fetched, path)
    return new


if __name__ == "__main__":
    n = seed_from_raw()
    print(f"库存初始化完成，新增 {n} 条")
    print("已有日期：", ", ".join(all_dates()))
