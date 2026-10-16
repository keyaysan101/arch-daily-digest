# -*- coding: utf-8 -*-
"""详情页补抓元数据

卡片摘要里抽不到面积 / 年份 / 摄影等结构化字段（全量覆盖率仅 14~16%），
这些字段只存在于详情页。本模块抓详情页补齐，结果按 URL 持久化缓存。

设计约束：
  1. 只补展示用的元数据，**不参与打分**——评分改动会重写全部历史期，
     让已发布的「当天 10 个推荐」发生变化，代价远大于收益。
  2. 请求失败不写缓存（并发下偶发失败若被缓存，就永远不会重试）。
  3. 抽不到就留空，不猜。新闻类条目（Dezeen 占相当比例）天然没有面积，
     属于数据本身性质。
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fetch_daily import get_html

CACHE_PATH = "data/detail_meta.json"

# ---------------------------------------------------------------- 抽取规则
# 面积：标签式。允许标签里夹斜杠（「面积 / Area:」），也允许冒号后先有一段说明
# 再跟数字（「Area: 净楼面面积 / Net floor area 5,824 m²」）
RE_AREA = re.compile(
    r"(?:Floor\s*Area|Gross\s*(?:Floor\s*)?Area|Net\s*(?:Floor\s*)?Area"
    r"|Total\s*(?:Usable\s*)?Area|建筑面积|净楼面面积|使用面积|总面积|面积|用地面积)"
    r"[^\n:：]{0,30}?[:：][^\n]{0,45}?([\d][\d,\.\s]{0,12}"
    r"(?:m\s*[²2]|sqm|sq\.?\s?m|square\s*met(?:er|re)s?|平方米|平米))", re.I)

# 中文叙述式：「建筑面积约 900 平方米」——正文里没有标签也能救回来
RE_AREA_CN = re.compile(
    r"(?:建筑面积|总建筑面积|净楼面面积|使用面积|建筑规模)[^。；\n]{0,25}?"
    r"([\d][\d,\.\s]{0,12}(?:平方米|平米|m\s*[²2]))", re.I)

# 英文连字符式：「a 34-square-metre house」——Dezeen 常见写法。
# 捕获组必须含单位，否则只会得到光秃秃的数字
RE_AREA_HYPHEN = re.compile(
    r"([\d][\d,\.]*\s*[-‐-―]?\s*(?:square[- ]met(?:er|re)s?|sqm|sq\.?\s?m|m\s*[²2]))",
    re.I)

# 兜底：正文里 "…area of the house is 34 square metres"
RE_AREA_LOOSE = re.compile(
    r"\barea\b[^.。]{0,90}?([\d][\d,\.]{0,10}\s*"
    r"(?:m\s*[²2]|sqm|sq\.?\s?m|square\s*met(?:er|re)s?|平方米|平米))", re.I)

# 年份：支持 "Completion: 2026"、"Year: 2019/2021"（取后者为竣工年）
RE_YEAR = re.compile(
    r"(?:Completion|项目年份|竣工年份|竣工|建成|Year\s*of\s*Completion|Year)"
    r"\s*[:：]\s*(\d{4})(?:\s*[/–—-]\s*(\d{4}))?", re.I)

# 摄影师：先取整行，再按后续字段名 / 正文虚词截断——
# 单靠 lookahead 会漏（各源「摄影」后的字段名不统一），单靠长度又会吞正文
RE_PHOTO = re.compile(
    r"(?:Photography|摄影师|摄影|图片版权)\s*[:：]\s*([^\n\r]{2,100})", re.I)
RE_PHOTO_LOOSE = re.compile(
    r"(?:photography|photographs?)\s+(?:is|are)?\s*by\s+([^.;\n]{2,60})", re.I)

# 摄影字段后可能出现的、属于下一个字段的起始词
RE_FIELD_STOP = re.compile(
    r"\s+(?:Collaborators?|Client|Structure|Structural|Landscape|Mechanical|Electrical"
    r"|Contractor|Engineering|Consultants?|Team|厂家|项目年份|竣工|面积|Completion"
    r"|Year|Floor\s*Area|Site\s*Area|Typology|Status|Location|©)", re.I)

# 摄影字段后处理：中文正文常紧跟在人名后面（如「Nadia Boyea 在"再利用"……」）
RE_NAME_CUT = re.compile(r"\s+(?:在|与|和|的|是|为|将|从|以|并|对|把|由)[^\s，。]{0,4}")

# 源站自己填的占位文字，不是真实数据（用 search，因为它们常带后缀）
RE_PLACEHOLDER = re.compile(
    r"to\s*be\s*confirmed|tbc|not\s*applicable|待定|暂无|未知", re.I)
RE_PLACEHOLDER_EXACT = re.compile(r"^\s*(?:n/?a|-{1,})\s*$", re.I)


def _real(s: str) -> bool:
    """排除源站占位文字"""
    s = (s or "").strip()
    return bool(s) and not RE_PLACEHOLDER.search(s) and not RE_PLACEHOLDER_EXACT.match(s)

RE_BRAND = re.compile(r"(?:厂家|Manufacturers?|Brands?)\s*[:：]\s*([^;\n]{2,120})", re.I)

# 图集：数正文里的图片资源（各源图床路径不同）
IMG_PATTERNS = [
    re.compile(r"images\.adsttc\.com/media/images/", re.I),
    re.compile(r"gooood\.cn/wp-content/uploads/", re.I),
    re.compile(r"dezeen\.com/wp-content/uploads/", re.I),
    re.compile(r"designboom\.com/wp-content/uploads/", re.I),
]
RE_IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def to_text(h: str) -> str:
    """HTML -> 纯文本（去脚本/标签、还原实体），正则才好写"""
    h = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", h)
    t = htmlmod.unescape(t)
    return re.sub(r"[ \t\u3000\xa0]+", " ", t)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" ,;:：·-–—")


def _norm_area(s: str) -> str:
    """统一面积写法：2,000-square-metre / 480 sqm / 34 m2 / 11150平方米 -> 2,000 m²"""
    s = _clean(s)
    # 注意：连字符也必须纳入匹配，否则 "square-metre" 换不动
    s = re.sub(r"square[-\s]*met(?:er|re)s?", "m²", s, flags=re.I)
    s = re.sub(r"sq[-\s]?\.?\s?m\b", "m²", s, flags=re.I)
    s = re.sub(r"\s*m\s*[²2]", " m²", s)
    s = re.sub(r"[-‐-―]\s*(?=m²)", "", s)
    s = re.sub(r"(\d)\s*(m²|平方米|平米)", r"\1 \2", s)
    return _clean(s)


def count_images(h: str) -> int:
    srcs = set()
    for m in RE_IMG_TAG.finditer(h):
        u = m.group(1)
        if any(p.search(u) for p in IMG_PATTERNS):
            # 去掉缩略图尺寸后缀以便去重
            srcs.add(re.sub(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}$)", "", u))
    return len(srcs)


def parse_detail(source: str, raw: str) -> dict:
    """按来源从详情页抽元数据；抽不到就留空，不猜"""
    t = to_text(raw)
    out = {}

    m = (RE_AREA.search(t) or RE_AREA_CN.search(t)
         or (RE_AREA_HYPHEN.search(t) if source in ("Dezeen", "Designboom") else None)
         or (RE_AREA_LOOSE.search(t) if source in ("Dezeen", "Designboom") else None))
    if m:
        out["area"] = _norm_area(m.group(1))

    m = RE_YEAR.search(t)
    if m:
        out["year"] = (m.group(2) or m.group(1))

    m = RE_PHOTO.search(t) or RE_PHOTO_LOOSE.search(t)
    if m:
        name = _clean(m.group(1))
        for pat in (RE_FIELD_STOP, RE_NAME_CUT):
            cut = pat.search(name)
            if cut:
                name = name[:cut.start()]
        name = re.split(r"[;；]", name)[0]
        # 「Juan Hernandez unless otherwise stated」——截断尾巴而不是整条丢弃
        name = re.split(r"\s+unless\s+otherwise", name, flags=re.I)[0]
        name = _clean(name)[:40]
        if 1 < len(name) < 40 and _real(name):
            out["photographer"] = name

    m = RE_BRAND.search(t)
    if m:
        brands = [b.strip() for b in re.split(r"[,，]", m.group(1))
                  if 2 <= len(b.strip()) <= 40 and _real(b.strip())]
        if brands:
            out["brands"] = brands[:6]

    n = count_images(raw)
    if n:
        out["image_count"] = n

    return out


# ---------------------------------------------------------------- 缓存
def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(c: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 抓取
def _probe(item: dict, tries=2):
    """抓单条详情页。

    返回 (url, meta, fetched)：
      fetched=False —— 请求失败，不写缓存，下次重试
      fetched=True  —— 抓到了；meta 可能为空（该页本来就没有这些字段），
                       此时缓存空值，避免每天重复抓同一个页面
    """
    url = item["url"]
    for k in range(tries):
        try:
            raw = get_html(url)
            if len(raw) > 5000:
                return url, parse_detail(item.get("source", ""), raw), True
        except Exception:
            pass
        if k + 1 < tries:
            time.sleep(1.2 * (k + 1))
    return url, None, False


def enrich_items(items: list[dict], workers=4, verbose=True) -> dict:
    """补抓一批条目的详情页元数据，返回 {url: meta}"""
    cache = load_cache()
    todo = [i for i in items if i["url"] not in cache]
    if not todo:
        if verbose:
            print(f"详情页元数据：{len(items)} 条全部命中缓存")
        return cache

    if verbose:
        print(f"详情页补抓：{len(todo)} 条（缓存已有 {len(items) - len(todo)} 条）")

    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for url, meta, fetched in ex.map(_probe, todo):
            done += 1
            if fetched:
                cache[url] = meta or {}
            else:
                failed += 1  # 请求失败不写缓存，下次重试
            if verbose and done % 10 == 0:
                print(f"  进度 {done}/{len(todo)}")
    save_cache(cache)
    if verbose:
        print(f"  完成：缓存累计 {len(cache)} 条，本次请求失败 {failed} 条（将在下次重试）")
    return cache


def apply_cache(items: list[dict], cache: dict = None) -> int:
    """把缓存里的元数据合并进条目的 meta（只填空，不覆盖已有值）"""
    cache = cache if cache is not None else load_cache()
    filled = 0
    for it in items:
        extra = cache.get(it["url"])
        if not extra:
            continue
        meta = it.setdefault("meta", {})
        for k, v in extra.items():
            if v and not meta.get(k):
                meta[k] = v
                filled += 1
    return filled


def main():
    """CLI：--backfill 对全部历史条目补抓；--stats 查看当前覆盖率"""
    import glob

    args = sys.argv[1:]
    if "--stats" in args or not args:
        rows = []
        for fp in sorted(glob.glob("data/digest_*.json")):
            with open(fp, encoding="utf-8") as f:
                rows.append(json.load(f))
        items = [it for d in rows for it in d["items"]]
        apply_cache(items)
        n = len(items)
        print(f"历史条目 {n} 条，元数据覆盖率：")
        for k, label in [("area", "面积"), ("year", "年份"),
                         ("photographer", "摄影"), ("image_count", "图集数"),
                         ("brands", "厂家")]:
            c = sum(1 for i in items if (i.get("meta") or {}).get(k))
            print(f"  {label:<6} {c}/{n} ({round(c * 100 / n)}%)")
        cache = load_cache()
        print(f"详情页缓存：{len(cache)} 条")
        return

    if "--backfill" in args:
        rows = []
        for fp in sorted(glob.glob("data/digest_*.json")):
            with open(fp, encoding="utf-8") as f:
                rows.append(json.load(f))
        items = [it for d in rows for it in d["items"]]

        # 先把 meta 重置为「纯卡片摘要抽取」的结果再合并补抓值——
        # 这样补抓是幂等的：早期规则的脏值不会残留（合并只填空、不覆盖）
        try:
            from score import parse_meta
            for it in items:
                it["meta"] = parse_meta(it)
        except Exception as e:
            print(f"  [警告] meta 重置失败，改为直接合并：{type(e).__name__}")

        enrich_items(items)
        apply_cache(items)
        # 回写 digest，让渲染结果直接带上
        for d in rows:
            with open(f"data/digest_{d['date']}.json", "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        print("已回写 data/digest_*.json，请重新运行 render_html.py")
        return


if __name__ == "__main__":
    main()
