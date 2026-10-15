# -*- coding: utf-8 -*-
"""
每日建筑案例抓取器
- 支持 HTML 卡片型信源（ArchDaily / 谷德 / 有方）与 RSS 型信源（Dezeen / Designboom / 建筑学院）
- 所有时间统一归一化到 Asia/Shanghai，按目标日期（默认前一天）过滤
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SH = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 只抓「首页」的窗口太窄：站点每天上新约 18 条，隔天就被顶掉，
# 一旦某天自动化没跑成，第二天补跑就会丢失大量内容。
# 因此每个 HTML 源配置一组 URL：首页 + 归档列表页 + 第 2 页归档，把窗口拉宽到约两周。
SOURCES = [
    dict(name="ArchDaily", kind="html", weight=1.00, lang="en",
         link_re=r"/\d{6,}/[a-z0-9\-]+",
         urls=["https://www.archdaily.com/",
               "https://www.archdaily.com/architecture-news",
               "https://www.archdaily.com/architecture-news?page=2"]),
    dict(name="ArchDaily中国", kind="html", weight=0.88, lang="zh",
         link_re=r"/\d{6,}/[a-z0-9\-]+",
         urls=["https://www.archdaily.cn/",
               "https://www.archdaily.cn/architecture-news",
               "https://www.archdaily.cn/architecture-news?page=2"]),
    dict(name="谷德设计网", kind="html", weight=0.92, lang="zh",
         link_re=r"\.htm(\?|$)",
         urls=["https://www.gooood.cn/",
               "https://www.gooood.cn/page/2",
               "https://www.gooood.cn/page/3"]),
    # 有方空间（archiposition）已移除：
    # 1) 首页 HTML 里 /items/ 链接全部来自「有方服务」导航下拉菜单，抓到的不是文章；
    # 2) 其 WordPress REST API 最新文章停在 2021-01-13，站点已不再通过该渠道发布。
    dict(name="Dezeen", kind="rss", weight=0.95, lang="en",
         urls=["https://www.dezeen.com/feed/"]),
    # designboom 内容横跨汽车/艺术/设计，只取 architecture 栏目
    dict(name="Designboom", kind="rss", weight=0.82, lang="en",
         path_include=r"/architecture/",
         urls=["https://www.designboom.com/feed"]),
    dict(name="建筑学院", kind="rss", weight=0.65, lang="zh",
         urls=["https://www.archcollege.com/feed"]),
]

# 明显的非项目页：招聘、广告、投稿、分类聚合页
NOISE_PAT = re.compile(
    r"/(category|tag|author|jobs?|submissions?|about|contact|advertis|login|register"
    r"|filter|search|page|wp-|privacy|terms)",
    re.I,
)
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|avif)(\?|$)", re.I)


# ---------------------------------------------------------------- 时间处理
def parse_dt(s: str | None) -> dt.datetime | None:
    """把各种写法的时间字符串转成北京时间 datetime；无时区信息者按北京时间处理"""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    # 中文：2026年10月13日
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return dt.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=SH)
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            tup = email.utils.parsedate(s)
            if not tup:
                return None
            d = dt.datetime(*tup[:6])
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=SH)
    return d.astimezone(SH)


def rss_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        v = entry.get(key)
        if v:
            try:
                return dt.datetime(*v[:6], tzinfo=UTC).astimezone(SH)
            except Exception:
                pass
    return parse_dt(entry.get("published") or entry.get("updated") or entry.get("pubDate"))


# ---------------------------------------------------------------- 字段提取
def rss_image(entry) -> str | None:
    for mc in entry.get("media_content", []) or []:
        if mc.get("url"):
            return mc["url"]
    for mt in entry.get("media_thumbnail", []) or []:
        if mt.get("url"):
            return mt["url"]
    for enc in entry.get("enclosures", []) or []:
        u = enc.get("href") or enc.get("url")
        if u and IMG_EXT.search(u):
            return u
    blob = ""
    if entry.get("content"):
        blob = entry["content"][0].get("value", "")
    else:
        blob = entry.get("summary", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blob)
    if m and not m.group(1).endswith(".svg"):
        return m.group(1)
    return None


def rss_summary(entry) -> str:
    raw = entry.get("summary", "") or ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "") or raw
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:400]


def img_from_tag(im) -> str | None:
    for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-echo", "data-url"):
        v = im.get(attr)
        if not v:
            continue
        v = v.strip()
        if v.startswith("//"):
            v = "https:" + v
        if v.startswith("http") and not v.lower().endswith(".svg"):
            return v
    # srcset 兜底
    ss = im.get("srcset") or im.get("data-srcset")
    if ss:
        first = ss.split(",")[0].strip().split(" ")[0]
        if first.startswith("//"):
            first = "https:" + first
        if first.startswith("http"):
            return first
    return None


def normalize_url(u: str) -> str:
    """去跟踪参数，统一成可比较的规范 URL"""
    try:
        p = urlparse(u)
    except Exception:
        return u
    q = [(k, v) for k, v in __import__("urllib.parse", fromlist=["x"]).parse_qsl(p.query)
         if not k.lower().startswith(("ad_", "utm_", "fbclid", "gclid"))]
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "",
                       __import__("urllib.parse", fromlist=["x"]).urlencode(q), ""))


# ---------------------------------------------------------------- RSS 抓取
def fetch_rss(src: dict) -> list[dict]:
    d = feedparser.parse(src["urls"][0], agent=UA)
    inc = re.compile(src["path_include"]) if src.get("path_include") else None
    out = []
    for e in d.entries[:80]:
        link = e.get("link") or ""
        if not link or NOISE_PAT.search(link):
            continue
        if inc and not inc.search(link):
            continue
        pub = rss_time(e)
        if not pub:
            continue
        out.append(dict(
            source=src["name"], weight=src["weight"], lang=src["lang"],
            title=re.sub(r"\s+", " ", (e.get("title") or "").strip()),
            url=normalize_url(link),
            cover=rss_image(e),
            summary=rss_summary(e),
            published_at=pub.isoformat(),
        ))
    return out


# ---------------------------------------------------------------- HTML 抓取
def get_html(url: str) -> str:
    """部分站点（如谷德）响应头不带 charset，requests 会误判为 ISO-8859-1 导致中文乱码"""
    r = requests.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _cards_from_soup(soup, base, src, link_re) -> list[dict]:
    """从单个列表页里解析出所有「带发布时间的卡片」"""
    out = []
    for t in soup.find_all("time"):
        pub = parse_dt(t.get("datetime")) or parse_dt(t.get("title")) \
            or parse_dt(t.get_text(strip=True))
        if not pub:
            continue
        # 向上扩展到「只含这一个 time」的最大容器，即一张卡片
        node = t
        for _ in range(8):
            p = node.parent
            if p is None or p.name == "body":
                break
            if len(p.find_all("time")) > 1:
                break
            node = p

        # 选取文章链接
        link_tag, href = None, None
        for a in node.find_all("a", href=True):
            h = a["href"]
            if NOISE_PAT.search(h):
                continue
            if link_re and link_re.search(h):
                link_tag, href = a, h
                break
        if link_tag is None:
            for a in node.find_all("a", href=True):
                if NOISE_PAT.search(a["href"]):
                    continue
                link_tag, href = a, a["href"]
                break
        if link_tag is None:
            continue

        url = normalize_url(urljoin(base, href))

        title = None
        for tag in ("h1", "h2", "h3", "h4"):
            h = node.find(tag)
            if h and h.get_text(strip=True):
                title = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
                break
        if not title:
            title = (link_tag.get("title") or link_tag.get("aria-label") or "").strip()
        if not title:
            title = re.sub(r"\s+", " ", link_tag.get_text(" ", strip=True))
        if not title or len(title) < 4:
            continue

        cover = None
        for im in node.find_all("img"):
            cover = img_from_tag(im)
            if cover:
                break

        summary = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        summary = summary.replace(title, "", 1).strip()[:400]

        out.append(dict(
            source=src["name"], weight=src["weight"], lang=src["lang"],
            title=title, url=url, cover=cover, summary=summary,
            published_at=pub.isoformat(),
        ))
    return out


def fetch_html(src: dict) -> list[dict]:
    """首页 + 归档列表页逐页抓取：单页只有约 18 条，多页合并才能覆盖到更早的日期"""
    link_re = re.compile(src["link_re"]) if src.get("link_re") else None
    out, seen = [], set()
    for page_url in src["urls"]:
        try:
            soup = BeautifulSoup(get_html(page_url), "lxml")
        except Exception as e:
            print(f"    [WARN] {src['name']} 分页失败 {page_url}: {type(e).__name__}",
                  file=sys.stderr)
            continue
        for it in _cards_from_soup(soup, page_url, src, link_re):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            out.append(it)
        time.sleep(0.8)  # 别把对方站点打太狠
    return out


# ------------------------------------------- HTML（日期编码在链接 ID 里，如有方）
ID_DATE = re.compile(r"/items/(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")


def fetch_html_id(src: dict) -> list[dict]:
    base = src["urls"][0]
    soup = BeautifulSoup(get_html(base), "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        m = ID_DATE.search(a["href"])
        if not m:
            continue
        y, mo, d, hh, mm, ss = map(int, m.groups())
        if not (1990 <= y <= 2100):
            continue
        try:
            pub = dt.datetime(y, mo, d, hh, mm, ss, tzinfo=SH)
        except ValueError:
            continue

        # 标题：链接自身文本 → 向上 4 层内的标题标签
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        node = a
        for _ in range(4):
            if title and len(title) >= 6:
                break
            node = node.parent
            if node is None:
                break
            for tag in ("h1", "h2", "h3", "h4"):
                h = node.find(tag)
                if h and h.get_text(strip=True):
                    title = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
                    break
            if not title:
                title = re.sub(r"\s+", " ", node.get_text(" ", strip=True))[:200]
        if not title or len(title) < 4:
            continue

        cover = None
        for im in node.find_all("img") if node is not None else []:
            cover = img_from_tag(im)
            if cover:
                break

        out.append(dict(
            source=src["name"], weight=src["weight"], lang=src["lang"],
            title=title, url=normalize_url(urljoin(base, a["href"])),
            cover=cover, summary="", published_at=pub.isoformat(),
        ))
    return out


# ---------------------------------------------------------------- 主流程
def collect(verbose=True) -> tuple[list[dict], list[dict]]:
    """抓取全部信源，返回 (所有条目, 各源统计)。

    这里刻意不做日期过滤：抓到什么就返回什么，由落库层（store）负责留存，
    这样即使某天漏跑，之前几天抓到的内容也不会凭空消失。
    """
    raw_all, stats = [], []
    fetchers = {"rss": fetch_rss, "html": fetch_html, "html_id": fetch_html_id}
    for s in SOURCES:
        try:
            got = fetchers[s["kind"]](s)
        except Exception as e:  # 单源失败不影响整体
            stats.append(dict(source=s["name"], ok=False, error=str(e)[:120], total=0))
            if verbose:
                print(f"[FAIL] {s['name']}: {e}", file=sys.stderr)
            continue
        dates = sorted({dt.datetime.fromisoformat(x["published_at"]).date().isoformat()
                        for x in got})
        stats.append(dict(source=s["name"], ok=True, total=len(got),
                          oldest=dates[0] if dates else None,
                          newest=dates[-1] if dates else None))
        raw_all += got
        if verbose:
            span = f"{dates[0]} ~ {dates[-1]}" if dates else "-"
            print(f"[OK] {s['name']}: {len(got)} 条，覆盖 {span}", file=sys.stderr)

    # 去重：按规范 URL（跨分页会有重复）
    seen, uniq = set(), []
    for it in raw_all:
        k = it["url"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)

    uniq.sort(key=lambda x: x["published_at"], reverse=True)
    return uniq, stats


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else None
    if day:
        y, m, d = map(int, day.split("-"))
        target = dt.date(y, m, d)
    else:
        target = (dt.datetime.now(SH) - dt.timedelta(days=1)).date()

    items, stats = collect()
    os.makedirs("data", exist_ok=True)
    path = f"data/raw_pool_{dt.datetime.now(SH):%Y%m%d_%H%M}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(date=target.isoformat(), stats=stats, count=len(items), items=items),
                  f, ensure_ascii=False, indent=2)
    print(f"saved {path} -> {len(items)} items", file=sys.stderr)
