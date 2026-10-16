# -*- coding: utf-8 -*-
"""热度信号采集

先说清楚一件事：没有任何一家建筑站公开「阅读量 / 点赞 / 分享」数字。
实测过的死路：ArchDaily /hottest → 404；Dezeen /topics/popular/ → 403；
各站详情页零计数器；Dezeen JSON-LD commentCount=0；WordPress REST 接口 404/403；
Reddit / Pinterest / Facebook / Buffer 等社交分享计数接口被墙或恒返回 0。

所以热度只能用「站方自己给出的排行」+「首页排序」两类代理信号：

  强信号（站方真实阅读排行）
    谷德「7 天最热」   站方按近 7 日阅读量排的名次榜，12 条，带 01~12 名次
    Dezeen Most popular 站方热门榜，5 条（实测含数月前的文章，说明热度需要时间沉淀）
  弱信号（编辑排序，不是阅读量，但越靠前越被站方推）
    各站首页 / 栏目页文章出现顺序

输出：{规范URL: {heat, tags, labels}}，供 score.py 作为打分主项。
任何一源抓取失败都不影响其它源，热度全无时程序照常出刊。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from fetch_daily import get_html, normalize_url

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CACHE_PATH = os.path.join("data", "heat_snapshot.json")

# 热度总分上限：多个信号叠加时不让单项刷爆，保证质量项仍有话语权
HEAT_CAP = 60.0

# 谷德「7 天最热」名次 → 热度分（第 1 名 45 分，第 12 名 12 分）
def _rank_gooood(r: int) -> float:
    return max(0.0, 45.0 - (r - 1) * 3.0)


# Dezeen 热门榜名次 → 热度分（第 1 名 35 分，第 5 名 11 分）
def _rank_dezeen(r: int) -> float:
    return max(0.0, 35.0 - (r - 1) * 6.0)


# 首页位置 → 热度分：base 为该源首页第一位的分值，span 位之后归零
def _pos_score(base: float, pos: int, span: int = 24) -> float:
    if pos >= span:
        return 0.0
    return round(base * (1 - pos / span), 2)


# 首页 / 栏目页顺序信号配置（弱信号）
HOME_SOURCES = [
    dict(name="ArchDaily", url="https://www.archdaily.com/", base=22.0,
         pat=r"https://www\.archdaily\.com/(\d{6,}/[a-z0-9\-]+)"),
    dict(name="ArchDaily中国", url="https://www.archdaily.cn/", base=10.0,
         pat=r"https://www\.archdaily\.cn/([a-z]{2}/\d{6,}/[a-z0-9\-]+)"),
    dict(name="Dezeen", url="https://www.dezeen.com/", base=16.0,
         pat=r"https://www\.dezeen\.com/(\d{4}/\d{2}/\d{2}/[a-z0-9\-]+)"),
    dict(name="Designboom", url="https://www.designboom.com/architecture/", base=12.0,
         pat=r"https://www\.designboom\.com/(architecture/[a-z0-9\-]+)"),
    dict(name="建筑学院", url="https://www.archcollege.com/", base=8.0,
         pat=r"https://www\.archcollege\.com/(\d{5,7}\.html)"),
]

# 谷德热榜区块标识：侧栏 widget，内部 <a class="hot"> 才带真实名次
GOOOD_HOT = re.compile(
    r'<a class="hot"[^>]*href="([^"]+)"[^>]*>\s*<div class="rank">(\d+)</div>',
)
# 非文章链接兜底过滤：招聘、栏目、翻页、服务页
BAD_GOOOOD_ANY = re.compile(
    r"/(job|jobs|category|tag|about|contact|submit|ad-|advertis|page|author|search|login)",
    re.I,
)


def _add(sig: dict, url: str, score: float, label: str) -> None:
    """累加信号；同一 URL 多个信号叠加，标签全部保留用于页面展示"""
    if score <= 0:
        return
    u = normalize_url(url)
    rec = sig.setdefault(u, dict(heat=0.0, labels=[]))
    rec["heat"] += score
    if label not in rec["labels"]:
        rec["labels"].append(label)


# ------------------------------------------------------------ 强信号：谷德 7 天最热
def gooood_hot(sig: dict, verbose=True) -> int:
    """谷德首页侧栏「7 天最热」：站方按近 7 日阅读量排的名次榜。

    页面结构（sidebar-week-hot 面板）：
        <a class="hot" href="/xxx.htm"><div class="rank">01</div><div><b data-zh="标题">…
    名次是站方写死的，直接读 <div class="rank">，不靠出现顺序推测。

    注意：同页还有「最新工作」招聘区块，链接同样是 /xxx.htm 且带 data-job-id，
    早期版本按「锚点后顺序取链接」会把招聘混进热榜，所以这里只认 class="hot"。
    """
    try:
        h = get_html("https://www.gooood.cn/")
    except Exception as e:
        if verbose:
            print(f"    [热度] 谷德热榜抓取失败：{type(e).__name__}")
        return 0
    n = 0
    seen = set()
    for m in GOOOD_HOT.finditer(h):
        href = m.group(1)
        if href.startswith("/"):
            href = "https://www.gooood.cn" + href
        if BAD_GOOOOD_ANY.search(href):
            continue
        if href in seen:
            continue
        seen.add(href)
        rank = int(m.group(2))
        n += 1
        _add(sig, href, _rank_gooood(rank), f"谷德 7 天最热 第 {rank:02d} 名")
    if verbose:
        print(f"    [热度] 谷德 7 天最热：{n} 条")
    return n


# ------------------------------------------------------------ 强信号：Dezeen 热门榜
def dezeen_hot(sig: dict, verbose=True) -> int:
    try:
        h = get_html("https://www.dezeen.com/")
    except Exception as e:
        if verbose:
            print(f"    [热度] Dezeen 热榜抓取失败：{type(e).__name__}")
        return 0
    i = h.lower().find("most popular")
    if i < 0:
        if verbose:
            print("    [热度] Dezeen 热榜锚点未找到")
        return 0
    seg = h[i:i + 20000]
    n = 0
    seen = set()
    for m in re.finditer(
            r'href="(https://www\.dezeen\.com/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?)"', seg):
        u = m.group(1)
        if u in seen:
            continue
        seen.add(u)
        n += 1
        _add(sig, u, _rank_dezeen(n), f"Dezeen 热门榜 第 {n} 名")
        if n >= 5:
            break
    if verbose:
        print(f"    [热度] Dezeen 热门榜：{n} 条")
    return n


# ------------------------------------------------------------ 弱信号：首页顺序
def _home_one(job: tuple) -> tuple[str, int]:
    """线程任务：(配置, 信号容器, 是否打印) → (源名, 命中条数)"""
    cfg, sig, verbose = job
    try:
        h = get_html(cfg["url"])
    except Exception as e:
        if verbose:
            print(f"    [热度] {cfg['name']} 首页抓取失败：{type(e).__name__}")
        return cfg["name"], 0
    # 站点域名前缀，用于把抓到的路径补全成绝对 URL
    prefix = re.match(r"https?://[^/]+", cfg["url"]).group(0)
    n, seen = 0, set()
    for m in re.finditer(cfg["pat"], h):
        u = prefix + "/" + m.group(1).rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        n += 1
        s = _pos_score(cfg["base"], n - 1)
        if s > 0:
            _add(sig, u, s, f"{cfg['name']} 首页第 {n} 位")
        if n >= 30:
            break
    if verbose:
        print(f"    [热度] {cfg['name']} 首页顺序：{n} 条")
    return cfg["name"], n


def home_orders(sig: dict, verbose=True) -> int:
    """各站首页 / 栏目页文章出现顺序（编辑排序，弱信号）"""
    jobs = [(c, sig, verbose) for c in HOME_SOURCES]
    total = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for _name, n in ex.map(_home_one, jobs):
            total += n
    return total


# ------------------------------------------------------------ 汇总
def collect(verbose=True, use_cache_if_fail=True) -> dict:
    """采集全部热度信号，返回 {规范URL: {heat, labels}}"""
    sig: dict = {}
    if verbose:
        print("  [热度] 开始采集热度信号")
    gooood_hot(sig, verbose)
    dezeen_hot(sig, verbose)
    home_orders(sig, verbose)

    for u, rec in sig.items():
        rec["heat"] = round(min(rec["heat"], HEAT_CAP), 2)

    # 快照落盘：抓取失败时下次可回退用上一次的信号，不至于热度全零
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    if sig:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(captured_at=dt.datetime.now().isoformat(timespec="seconds"),
                           signals=sig), f, ensure_ascii=False, indent=2)
    elif use_cache_if_fail and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                old = json.load(f).get("signals", {})
            if old:
                sig = old
                if verbose:
                    print(f"    [热度] 本次未采到信号，回退用上次快照 {len(old)} 条")
        except Exception:
            pass
    if verbose:
        print(f"  [热度] 合计 {len(sig)} 条带热度信号")
    return sig


# 强热度标签：站方自己按阅读量排的名次榜（区别于首页编辑排序）
STRONG_LABEL = re.compile(r"(7 天最热|热门榜)")


def is_strong(labels: list[str] | None) -> bool:
    """是否来自站方真实阅读排行"""
    return any(STRONG_LABEL.search(x) for x in (labels or []))


def strong_urls(sig: dict) -> set:
    """返回所有「站方阅读榜在榜」条目的 URL"""
    return {u for u, r in sig.items() if is_strong(r.get("labels"))}


def load_snapshot() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f).get("signals", {})
        except Exception:
            return {}
    return {}


if __name__ == "__main__":
    s = collect()
    top = sorted(s.items(), key=lambda kv: kv[1]["heat"], reverse=True)[:20]
    print()
    for i, (u, r) in enumerate(top, 1):
        print(f"{i:>2}. {r['heat']:>5.1f}  {u}")
        print(f"      {' + '.join(r['labels'])}")
