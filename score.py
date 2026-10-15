# -*- coding: utf-8 -*-
"""
筛选打分：跨源去重 → 类型识别 → 元数据抽取 → 综合打分 → 多样性配额取前 N

打分 = 站点权重 + 项目属性 + 内容完整度 + 元数据丰富度 + 事务所声望 − 噪音惩罚
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from fetch_daily import SH, get_html, normalize_url

# 双语映射缓存：中文详情页 → 英文原版 URL。
# 该映射是永久性的（同一项目不会变），不缓存的话每次补生成往期日报都会重复请求详情页。
CACHE_PATH = os.path.join("data", "bilingual_cache.json")


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(c: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ------------------------------------------------ 噪音：新闻/观点/产品/讣告/广告
# 只作用于「标题 + URL」。正文里出现「产品/招生」等词属正常表述，不能据此杀掉项目
NOISE_WORDS = re.compile(
    r"(?:\bdies\b|\bdied\b|obituary|dezeen\s+showroom|\bshortlist\b|\binterview\b"
    r"|\bpodcast\b|\bopinion\b|advertorial|\bsponsored\b|partner content"
    r"|\bpartners with\b|\bconference\b|\bwebinar\b|\bnewsletter\b|five new products"
    r"|招聘|征文启事|直播预告|线上课程|讲座报名|讣告|榜单|招生简章|实习生招募|合作推广)",
    re.I,
)

# ------------------------------------------------ ArchDaily 卡片元数据抽取
# 卡片文本形如：© 摄影师 + 15 • Cape Town, 南非 建筑师: XX 面积: 17370 m² 项目年份: 2025 厂家: A , B
RE_PHOTO = re.compile(r"©\s*([^+]{1,40}?)\s*\+\s*(\d{1,3})")
RE_LOC_BULLET = re.compile(
    r"[•·]\s*([A-Za-z\u4e00-\u9fff][^,，•·。；;（）()]{1,25}?)\s*[,，]\s*"
    r"([A-Za-z\u4e00-\u9fff]{2,15})(?=[\s，,。]|$)")
# 地点片段里出现这些词说明匹配到了正文而非元数据块
BAD_LOC = re.compile(r"(图片|视频|音频|©|\d{4}|项目|面积|厂家|建筑师|改造|再利用|设计)")
RE_ARCHITECT = re.compile(
    r"(?:建筑师|Architects)\s*[:：]\s*(.+?)(?=\s*(?:面积|Area|项目年份|Project Year|厂家|Manufacturers|项目类型|Categories|https|http|$))"
)
RE_AREA = re.compile(r"(?:面积|Area)\s*[:：]\s*([\d,.]+\s*m²)", re.I)
RE_YEAR = re.compile(r"(?:项目年份|Project Year)\s*[:：]\s*(\d{4})", re.I)
RE_BRAND = re.compile(
    r"(?:厂家|Manufacturers)\s*[:：]\s*(.+?)(?=\s*(?:https|http|分类|Categories|项目类型|$))"
)


def parse_meta(it: dict) -> dict:
    """从卡片正文里抽取结构化元数据：地点/面积/图片数/建成年份/事务所/材料厂家"""
    blob = f"{it['title']} {it.get('summary','') or ''}"
    meta = {}
    m = RE_PHOTO.search(blob)
    if m:
        meta["photographer"] = m.group(1).strip()
        try:
            meta["image_count"] = int(m.group(2))
        except ValueError:
            pass
    m = RE_LOC_BULLET.search(blob)
    if m and not (BAD_LOC.search(m.group(1)) or BAD_LOC.search(m.group(2))):
        meta["city"] = m.group(1).strip()
        meta["country"] = m.group(2).strip()
    m = RE_ARCHITECT.search(blob)
    if m:
        meta["architect"] = re.sub(r"\s+", " ", m.group(1)).strip()[:60]
    m = RE_AREA.search(blob)
    if m:
        meta["area"] = m.group(1).strip()
    m = RE_YEAR.search(blob)
    if m:
        meta["year"] = m.group(1)
    m = RE_BRAND.search(blob)
    if m:
        brands = [b.strip() for b in re.split(r"[,，]", m.group(1)) if 2 <= len(b.strip()) <= 40]
        meta["brands"] = brands[:6]
    return meta

# 建成项目特征：ArchDaily「项目名 / 事务所」、谷德「项目名，地点 / 事务所」
PROJECT_SLASH = re.compile(r"[^\s/]{2,}\s*/\s*[A-Za-z\u4e00-\u9fff][^\s/]{1,}")

# 建筑类型词典（中英）
TYPE_LEXICON = [
    ("住宅", r"house|housing|residential|apartment|villa|cabin|duplex|住宅|公寓|别墅|民居|住区"),
    ("文化", r"museum|gallery|cultural|theatre|theater|concert|library|博物馆|美术馆|图书馆|剧院|文化中?心|艺术馆"),
    ("教育", r"school|university|campus|kindergarten|college|academy|学校|大学|校园|幼儿园|书院|学院"),
    ("办公", r"office|workplace|headquarters|办公|总部|写字楼"),
    ("商业", r"retail|shop|store|mall|market|commercial|商业|零售|市集|商店"),
    ("酒店", r"hotel|resort|hostel|民宿|酒店|客栈|度假"),
    ("景观", r"landscape|park|garden|plaza|waterfront|promenade|景观|公园|广场|滨水|花园"),
    ("室内", r"interior|室内|软装"),
    ("改造", r"renovation|restoration|adaptive reuse|conversion|refurbish|改造|更新|再生|修缮|活化"),
    ("宗教", r"church|chapel|mosque|temple|monastery|教堂|寺庙|清真寺|礼拜"),
    ("交通", r"station|airport|terminal|bridge|transit|车站|机场|桥梁|交通|枢纽"),
    ("医疗", r"hospital|clinic|healthcare|medical|医院|医疗|康养"),
    ("体育", r"stadium|arena|sports|gym|pool|体育|体育馆|运动|游泳"),
    ("公共", r"civic|community|public|municipal|社区|公共|市民|市政"),
    ("工业", r"factory|warehouse|industrial|workshop|工厂|仓库|产业园|厂房"),
    ("展览", r"pavilion|exhibition|installation|展亭|装置|展陈"),
]

# 声望事务所 / 建筑师（命中即视为质量与关注度信号）
FAME_STUDIOS = re.compile(
    r"(OMA|Rem Koolhaas|Foster\s*\+\s*Partners|Norman Foster|BIG|Bjarke|MVRDV|Zaha|Herzog|"
    r"Kengo Kuma|隈研吾|Tadao Ando|安藤忠雄|Renzo Piano|Jean Nouvel|SANAA|Kazuyo Sejima|"
    r"David Chipperfield|Snøhetta|Snohetta|SOM|Gensler|Perkins|KPF|Heatherwick|"
    r"Studio Mumbai|Bijoy|Vo Trong Nghia|WOHA|Kerry Hill|Neri\s*&?\s*Hu|如恩|郭锡恩|胡如珊|"
    r"王澍|董功|张雷|刘家琨|崔愷|崔恺|李兴钢|马岩松|MAD|朱锫|徐甜甜|DnA|OPEN|"
    r"直向建筑|大舍|致正|阿科米星|TAO|迹建筑|line\+|goa| gad|"
    r"Sou Fujimoto|藤本壮介|Junya Ishigami|石上纯也|Alvaro Siza|Siza|RCR|"
    r"Christ\s*&\s*Gantenbein|Caruso|Lacaton|Anne Lacaton|Studio Gang|"
    r"Tatiana Bilbao|Alberto Kalach|Pezo von|Smiljan|OMA|Mecanoo|Neutelings|"
    r"Kohn Pedersen|Rafael Moneo|Eduardo Souto|Alison Brooks|Sergison|"
    r"6a architects|Adam Khan|Sandy Rendel|Níall McLaughlin)",
    re.I,
)

# 地点抽取：谷德「项目名，地点 / 事务所」、英文 "in Japan/China"
LOC_CN = re.compile(r"[，,]\s*([\u4e00-\u9fff]{2,10}(?:市|省|区|县)?)\s*/")
LOC_EN = re.compile(r"\bin\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)")
KNOWN_REGIONS = [
    # 中国（具体城市优先于省级与大区）
    "中国香港", "中国台湾", "中国澳门", "北京", "上海", "深圳", "广州", "成都", "杭州", "南京",
    "武汉", "西安", "重庆", "苏州", "厦门", "青岛", "天津", "长沙", "昆明", "福州", "合肥",
    "宁波", "无锡", "大理", "温州", "珠海", "东莞", "佛山", "济南", "郑州", "沈阳", "长春",
    "哈尔滨", "贵阳", "南宁", "太原", "南昌", "兰州", "海口", "三亚", "丽江", "桂林",
    "浙江", "江苏", "四川", "福建", "广东", "云南", "贵州", "山东", "河南", "河北", "湖南",
    "湖北", "安徽", "陕西", "甘肃", "辽宁", "吉林", "黑龙江", "江西", "山西", "青海", "宁夏",
    "西藏", "新疆", "内蒙古", "香港", "澳门", "台湾", "中国",
    # 世界
    "Japan", "Tokyo", "日本", "东京", "大阪", "京都", "Korea", "Seoul", "韩国", "首尔",
    "Spain", "Spain", "西班牙", "马德里", "巴塞罗那", "毕尔巴鄂",
    "Italy", "意大利", "米兰", "罗马", "威尼斯", "France", "法国", "巴黎",
    "Germany", "德国", "柏林", "慕尼黑", "Netherlands", "荷兰", "阿姆斯特丹", "鹿特丹",
    "Portugal", "葡萄牙", "里斯本", "波尔图", "UK", "United Kingdom", "英国", "伦敦",
    "United States", "USA", "美国", "纽约", "洛杉矶", "芝加哥", "休斯敦", "休斯顿",
    "旧金山", "西雅图", "波士顿", "德克萨斯",
    "Australia", "澳大利亚", "悉尼", "墨尔本", "Brazil", "巴西", "圣保罗", "里约",
    "Chile", "智利", "Mexico", "墨西哥", "Switzerland", "瑞士", "苏黎世", "巴塞尔",
    "Austria", "奥地利", "维也纳", "Denmark", "丹麦", "哥本哈根", "Sweden", "瑞典",
    "Norway", "挪威", "Finland", "芬兰", "Belgium", "比利时", "Poland", "波兰", "华沙",
    "Turkey", "土耳其", "伊斯坦布尔", "India", "印度", "孟买", "Vietnam", "越南", "河内",
    "Thailand", "泰国", "曼谷", "Canada", "加拿大", "多伦多", "温哥华", "Ireland", "爱尔兰",
    "Greece", "希腊", "雅典", "Croatia", "克罗地亚", "South Africa", "南非", "开普敦",
    "Argentina", "阿根廷", "Singapore", "新加坡", "Malaysia", "吉隆坡", "Indonesia", "雅加达",
    "迪拜", "Israel", "以色列", "Russia", "莫斯科", "布拉格", "布达佩斯", "奥克兰",
    "Cape Town", "South Africa",
]


# ---------------------------------------------------------------- 跨源去重
def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t


# 事务所名不会是长句：连续 6 个以上汉字基本说明抓到的是正文而非机构名
BAD_STUDIO = re.compile(r"[\u4e00-\u9fff]{6,}")


def studio_display(title: str) -> str:
    """提取原始事务所名用于展示"""
    m = re.search(r"\s/\s*(.+)$", title.strip())
    if m:
        s = m.group(1).strip()
        if 2 <= len(s) <= 45 and not BAD_STUDIO.search(s):
            return s
        return ""
    m = re.search(r"\bby\s+([A-Za-z][\w&+.\s\-]{2,40})$", title.strip(), re.I)
    if m:
        return m.group(1).strip()
    return ""


def studio_of(title: str) -> str:
    """提取事务所名（归一化）用于跨语言判重"""
    s = studio_display(title)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s).lower() if s else ""


def merge_archdaily_bilingual(items: list[dict], workers=4) -> list[dict]:
    """中文站详情页带 hreflang='en' 指向英文原版，据此精确合并双语条目。

    映射结果落 data/bilingual_cache.json 复用——同一项目的中英对应关系不会变，
    不缓存则每次补生成往期日报都要把详情页重新请求一遍。
    只缓存「成功解析」的结果：请求失败多为并发被限流导致的偶发抖动，
    若把 None 也写进缓存，这条目就永远不会再重试，中英双语会漏网。
    """
    cn = [i for i in items if i["source"] == "ArchDaily中国"]
    en_by_url = {i["url"]: i for i in items if i["source"] == "ArchDaily"}
    if not cn:
        return items

    def probe_en_url(html: str) -> str | None:
        """在详情页里找 hreflang='en' 的 alternate 链接（属性顺序不固定）"""
        for tag in re.findall(r"<link\b[^>]*>", html):
            if not re.search(r"hreflang=['\"](?:en|en-US|en-GB)['\"]", tag):
                continue
            m = re.search(r"href=['\"]([^'\"]+)", tag)
            if m and re.search(r"archdaily\.com/\d{5,}/", m.group(1)):
                return normalize_url(m.group(1))
        return None

    cache = _load_cache()
    todo = [i for i in cn if not cache.get(i["url"])]
    if todo:
        def probe(it):
            # 详情页体积大且并发易被限流，失败重试三次（每次退避）
            for attempt in range(3):
                try:
                    en_url = probe_en_url(get_html(it["url"]))
                except Exception:
                    en_url = None
                if en_url:
                    return it, en_url
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
            return it, None

        ok = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for it, en_url in ex.map(probe, todo):
                if en_url:          # 失败不落缓存，下次仍有重试机会
                    cache[it["url"]] = en_url
                    ok += 1
        _save_cache(cache)
        print(f"  双语映射：本次解析 {todo and len(todo)} 条，成功 {ok} 条")

    pairs = [(i, cache.get(i["url"])) for i in cn]

    merged, drop_urls = [], set()
    for it, en_url in pairs:
        if en_url and en_url in en_by_url:
            en = en_by_url[en_url]
            it["alt_url"] = en["url"]
            it["bilingual"] = True
            it["weight"] = max(it["weight"], en["weight"])
            it["source"] = "ArchDaily"
            it["lang"] = "zh"
            drop_urls.add(en["url"])
            merged.append(it)
    return [i for i in items if i["url"] not in drop_urls]


def fame_key(text: str) -> str | None:
    m = FAME_STUDIOS.search(text)
    return re.sub(r"[^\w]+", "", m.group(0)).lower() if m else None


def dedupe_sorted(items: list[dict]) -> list[dict]:
    """输入已按分数降序，保留首次出现的（即高分者），丢弃后续重复项"""
    out, seen_title, seen_studio, seen_fame = [], set(), set(), set()
    for it in items:
        nt = norm_title(it["title"])
        if nt and nt in seen_title:
            continue
        st = studio_of(it["title"])
        if st and len(st) >= 4 and st in seen_studio:
            continue
        # 同一知名事务所 + 同一建筑类型，视为同一项目的不同站点报道
        fk = fame_key(f"{it['title']} {it.get('summary','')}")
        if fk:
            key = (fk, it.get("btype") or "")
            if key in seen_fame:
                continue
            seen_fame.add(key)
        seen_title.add(nt)
        if st:
            seen_studio.add(st)
        out.append(it)
    return out


# ---------------------------------------------------------------- 元数据
def detect_type(text: str) -> str | None:
    for name, pat in TYPE_LEXICON:
        if re.search(pat, text, re.I):
            return name
    return None


def match_known(text: str) -> str | None:
    """中英文地域词典的子串匹配，按列表顺序优先（具体城市在前）"""
    for r in KNOWN_REGIONS:
        if re.search(rf"(?<![A-Za-z]){re.escape(r)}(?![A-Za-z])", text):
            return r
    return None


def detect_region(text: str) -> str | None:
    m = LOC_CN.search(text)
    if m:
        cand = m.group(1)
        for r in KNOWN_REGIONS:
            if r in cand or (len(cand) >= 2 and cand in r):
                return r
        return cand
    r = match_known(text)
    if r:
        return r
    m = LOC_EN.search(text)
    if m:
        return m.group(1)
    return None


# 地域别名归一：中英文写法、译名差异统一成常用中文名
REGION_ALIAS = {
    "休斯顿": "休斯敦", "USA": "美国", "United States": "美国", "UK": "英国",
    "United Kingdom": "英国", "Japan": "日本", "China": "中国", "Spain": "西班牙",
    "Italy": "意大利", "France": "法国", "Germany": "德国", "Netherlands": "荷兰",
    "Portugal": "葡萄牙", "Australia": "澳大利亚", "Brazil": "巴西", "Chile": "智利",
    "Mexico": "墨西哥", "Switzerland": "瑞士", "Austria": "奥地利", "Denmark": "丹麦",
    "Sweden": "瑞典", "Norway": "挪威", "Finland": "芬兰", "Belgium": "比利时",
    "Poland": "波兰", "Turkey": "土耳其", "India": "印度", "Vietnam": "越南",
    "Thailand": "泰国", "Korea": "韩国", "Canada": "加拿大", "Ireland": "爱尔兰",
    "Greece": "希腊", "Croatia": "克罗地亚", "South Africa": "南非",
    "Tokyo": "东京", "Seoul": "首尔", "Paris": "巴黎", "Singapore": "新加坡",
}


# ---------------------------------------------------------------- 打分
def score_one(it: dict) -> dict:
    title, summary = it["title"], it.get("summary", "") or ""
    blob = f"{title} {summary} {it['url']}"
    s, reasons = 0.0, []

    base = it["weight"] * 40
    s += base
    reasons.append(f"站点权重{it['weight']:.2f}→{base:.0f}")

    # 噪音判定只看标题与 URL，正文里的普通用词不参与
    if NOISE_WORDS.search(f"{title} {it['url']}"):
        it["score"], it["reasons"] = -999.0, ["噪音内容：新闻/产品/讣告/广告"]
        it["is_noise"] = True
        return it

    meta = parse_meta(it)
    it["meta"] = meta
    ic = meta.get("image_count", 0)
    if ic >= 20:
        s += 6
        reasons.append(f"图集{ic}张+6")
    elif ic >= 10:
        s += 4
        reasons.append(f"图集{ic}张+4")
    elif ic:
        s += 2
        reasons.append(f"图集{ic}张+2")
    if meta.get("area"):
        s += 3
        reasons.append("含面积数据+3")
    if meta.get("brands"):
        s += 2
        reasons.append("含材料厂家+2")

    is_project = bool(PROJECT_SLASH.search(title)) or bool(
        re.search(r"\.htm(\?|$)", it["url"]) and "gooood" not in it["source"]
    ) or it["source"] in ("谷德设计网", "ArchDaily")
    if PROJECT_SLASH.search(title):
        s += 25
        reasons.append("建成项目格式+25")
        it["is_project"] = True
    else:
        s += 5
        it["is_project"] = False

    if it.get("cover"):
        s += 8
        reasons.append("封面图+8")
    if len(summary) > 50:
        s += 4
        reasons.append("摘要充分+4")
    if len(summary) > 150:
        s += 4
        reasons.append("摘要详实+4")

    t = detect_type(blob)
    it["btype"] = t
    if t:
        s += 5
        reasons.append(f"类型「{t}」+5")

    r = meta.get("country") or detect_region(title) or detect_region(summary)
    r = REGION_ALIAS.get(r, r) if r else None
    it["region"] = r
    it["city"] = meta.get("city") or ""
    it["architect"] = meta.get("architect") or studio_display(title) or ""
    if r:
        s += 5
        reasons.append(f"地域「{r}」+5")

    if FAME_STUDIOS.search(blob):
        s += 12
        reasons.append("知名事务所+12")

    if it.get("bilingual"):
        s += 3
        reasons.append("中英双版+3")

    it["score"] = round(s, 1)
    it["reasons"] = reasons
    return it


# ---------------------------------------------------------------- 配额选取
def pick_top(items: list[dict], n=10, quota=None) -> list[dict]:
    """按分数排序后按来源配额贪心选取，保证信源多样性"""
    quota = quota or {"ArchDaily": 4, "谷德设计网": 3, "Dezeen": 3,
                      "Designboom": 2, "有方空间": 2, "建筑学院": 2}
    ranked = sorted(items, key=lambda x: x["score"], reverse=True)
    used, picked = {}, []
    for it in ranked:
        if len(picked) >= n:
            break
        src = it["source"]
        cap = quota.get(src, 2)
        if used.get(src, 0) >= cap:
            continue
        used[src] = used.get(src, 0) + 1
        picked.append(it)
    # 配额未满则放宽补齐
    if len(picked) < n:
        for it in ranked:
            if len(picked) >= n:
                break
            if it in picked:
                continue
            picked.append(it)
    return picked


def run(items: list[dict], n=10) -> tuple[list[dict], list[dict], dict]:
    """先打分（分数用于决定重复项保留谁）→ 去噪 → 去重 → 配额选取"""
    merged = merge_archdaily_bilingual(items)
    scored = [score_one(x) for x in merged]
    clean = [x for x in scored if not x.get("is_noise")]
    dropped = [x for x in scored if x.get("is_noise")]
    clean.sort(key=lambda x: x["score"], reverse=True)
    uniq = dedupe_sorted(clean)
    info = dict(raw=len(items), after_dedupe=len(uniq), clean=len(clean))
    return pick_top(uniq, n), dropped, info
