# -*- coding: utf-8 -*-
"""把每日结果渲染成手机尺寸的站点页面

产出四类页面：
  digest_<日期>.html  当日日报（index.html 为最新一期）
  archive.html        往期归档索引
  library.html        全量灵感库：搜索 / 筛选 / 排序 / 收藏 / 导出
  all.json            全量结构化数据（供外部调用）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "by", "for", "with", "to", "is"}


def pretty_title(t: str) -> str:
    """英文站标题常为全小写长句（如 designboom），展示时统一为标题格式"""
    t = re.sub(r"\s+", " ", t).strip()
    if re.search(r"[\u4e00-\u9fff]", t):
        return t
    words = t.split(" ")
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) > 1:
            out.append(w)
        elif i > 0 and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def esc(s) -> str:
    return html.escape(str(s or ""))


SOURCE_TAG = {
    "ArchDaily": "#c2410c", "ArchDaily中国": "#c2410c", "谷德设计网": "#15803d",
    "Dezeen": "#1d4ed8", "Designboom": "#7c3aed", "有方空间": "#0f766e",
    "建筑学院": "#b45309",
}

# ---------------------------------------------------------------- 样式与脚本
# 单独抽出，避免 CSS/JS 的花括号与 f-string 的 {} 冲突

CSS_BASE = """
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#eef1f6;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",
       "Microsoft YaHei","Helvetica Neue",sans-serif;color:#0f172a;padding:20px 12px 40px;
       -webkit-text-size-adjust:100%}
  .wrap{max-width:420px;margin:0 auto}
  .wrap.wide{max-width:760px}
  .hd{background:#1e293b;color:#fff;border-radius:18px;padding:20px 18px;margin-bottom:12px}
  .hd h1{font-size:20px;letter-spacing:.5px}
  .hd .sub{font-size:12px;opacity:.75;margin-top:6px;line-height:1.7}
  .stats{display:flex;gap:8px;margin-top:12px}
  .stat{background:rgba(255,255,255,.14);border-radius:10px;padding:7px 10px;
        font-size:11px;line-height:1.5;flex:1}
  .stat b{display:block;font-size:16px;font-weight:700}
  .nav{display:flex;gap:8px;margin-bottom:14px}
  .nav a{flex:1;text-align:center;background:#fff;border-radius:10px;padding:9px 4px;
         font-size:12px;color:#334155;text-decoration:none;
         box-shadow:0 1px 3px rgba(15,23,42,.07)}
  .nav a.on{background:#1e293b;color:#fff}
  .card{background:#fff;border-radius:16px;overflow:hidden;margin-bottom:14px;color:inherit;
        position:relative;-webkit-tap-highlight-color:transparent;cursor:pointer;
        box-shadow:0 1px 3px rgba(15,23,42,.08),0 8px 24px rgba(15,23,42,.06)}
  .card:active{transform:scale(.99)}
  .rank{position:absolute;top:10px;left:10px;z-index:2;background:rgba(15,23,42,.82);
        color:#fff;width:26px;height:26px;border-radius:9px;font-size:13px;font-weight:700;
        display:flex;align-items:center;justify-content:center}
  .pic{aspect-ratio:3/2;background:#e2e8f0;overflow:hidden;position:relative}
  .pic img{width:100%;height:100%;object-fit:cover;display:block}
  .pic.bad img{display:none}
  .pic.bad:after{content:"图片暂不可用";position:absolute;inset:0;display:flex;
        align-items:center;justify-content:center;color:#94a3b8;font-size:12px}
  .pic.none{aspect-ratio:auto;height:120px}
  .pic.none:after{content:"暂无图片";position:absolute;inset:0;display:flex;
        align-items:center;justify-content:center;color:#94a3b8;font-size:12px}
  .body{padding:14px}
  .title{font-size:16px;font-weight:600;line-height:1.45;margin-bottom:6px}
  .title a{color:inherit;text-decoration:none}
  .arch{font-size:12px;color:#64748b;margin-bottom:8px}
  .tags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
  .chip{font-size:11px;background:#f1f5f9;border-radius:6px;padding:3px 7px}
  .foot{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .src{font-size:11px;color:#fff;border-radius:6px;padding:3px 8px}
  .score{font-size:11px;color:#94a3b8;margin-right:auto}
  .mini{font-size:11px;color:#334155;background:#f1f5f9;border:0;border-radius:6px;
        padding:4px 9px;cursor:pointer;text-decoration:none;line-height:1.6}
  .mini:active{background:#e2e8f0}
  .mini.faved{background:#1e293b;color:#fff}
  .more{display:none;margin-top:12px;border-top:1px dashed #e2e8f0;padding-top:12px}
  .more.open{display:block}
  .kv{display:flex;font-size:12px;line-height:1.9}
  .kv b{font-weight:400;color:#94a3b8;width:66px;flex:none}
  .kv span{color:#334155}
  .why{margin-top:10px;font-size:11px;color:#64748b;line-height:1.8}
  .why b{color:#334155;font-weight:600;display:block;margin-bottom:4px}
  .ft{text-align:center;font-size:11px;color:#94a3b8;line-height:1.9;padding:8px}
  .ft-link{color:#2563eb;text-decoration:none;font-size:12px}
"""

CSS_LIBRARY = """
  .bar{background:#fff;border-radius:14px;padding:12px;margin-bottom:14px;
       box-shadow:0 1px 3px rgba(15,23,42,.07)}
  .bar input,.bar select{width:100%;font-size:13px;padding:9px 10px;margin-bottom:8px;
       border:1px solid #e2e8f0;border-radius:8px;background:#fff;color:#0f172a;
       font-family:inherit}
  .row2{display:flex;gap:8px}
  .row2>*{flex:1;margin-bottom:8px}
  .toggles{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .tg{font-size:12px;color:#334155;background:#f1f5f9;border:0;border-radius:8px;
      padding:7px 11px;cursor:pointer}
  .tg.on{background:#1e293b;color:#fff}
  .cnt{font-size:12px;color:#64748b;margin-left:auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .grid .card{margin-bottom:0}
  .empty{text-align:center;color:#94a3b8;font-size:13px;padding:40px 0}
  .hint{font-size:11px;color:#94a3b8;margin-top:10px;line-height:1.8}
"""

JS_CARD = """
var FAVKEY='adfav_v1';
function favList(){try{return JSON.parse(localStorage.getItem(FAVKEY)||'[]')}catch(e){return []}}
function favSave(a){try{localStorage.setItem(FAVKEY,JSON.stringify(a))}catch(e){}}
function favHas(u){return favList().some(function(x){return x.u===u})}
function favToggle(u,t,s,c,d){
  var a=favList(),i=-1;
  for(var k=0;k<a.length;k++){if(a[k].u===u){i=k;break}}
  if(i>=0){a.splice(i,1)}else{a.push({u:u,t:t,s:s,c:c,d:d,at:Date.now()})}
  favSave(a);
  syncFavBtns();
  return i<0;
}
function syncFavBtns(){
  var bs=document.querySelectorAll('[data-fav]');
  for(var i=0;i<bs.length;i++){
    var b=bs[i],on=favHas(b.getAttribute('data-fav'));
    b.textContent=on?'已收藏':'收藏';
    b.className='mini'+(on?' faved':'');
  }
}
document.addEventListener('click',function(e){
  var t=e.target;
  var more=t.closest?t.closest('[data-more]'):null;
  if(more){
    var box=document.getElementById(more.getAttribute('data-more'));
    if(box){box.classList.toggle('open');more.textContent=box.classList.contains('open')?'收起':'详情';}
    return;
  }
  var fb=t.closest?t.closest('[data-fav]'):null;
  if(fb){
    e.stopPropagation();
    favToggle(fb.getAttribute('data-fav'),fb.getAttribute('data-t'),
              fb.getAttribute('data-s'),fb.getAttribute('data-c'),fb.getAttribute('data-d'));
    return;
  }
  if(t.closest&&t.closest('.more'))return;
  if(t.closest&&t.closest('a'))return;
  var card=t.closest?t.closest('[data-url]'):null;
  if(card){window.open(card.getAttribute('data-url'),'_blank');}
});
syncFavBtns();
"""

JS_LIBRARY = """
var DATA=JSON.parse(document.getElementById('items').textContent);
var onlyFav=false,sortBy='date';
function uniq(k){var s={},o=[];for(var i=0;i<DATA.length;i++){var v=DATA[i][k]||'';if(v&&!s[v]){s[v]=1;o.push(v)}}return o.sort()}
function fill(id,k,label){
  var el=document.getElementById(id),vs=uniq(k);
  el.innerHTML='<option value="">'+label+'（全部）</option>'+
    vs.map(function(v){return '<option value="'+v.replace(/"/g,'&quot;')+'">'+v+'</option>'}).join('');
}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
function filtered(){
  var q=(document.getElementById('q').value||'').trim().toLowerCase();
  var bt=document.getElementById('f-bt').value,rg=document.getElementById('f-rg').value;
  var so=document.getElementById('f-so').value,ar=document.getElementById('f-ar').value;
  var out=DATA.filter(function(x){
    if(onlyFav&&!favHas(x.u))return false;
    if(bt&&x.bt!==bt)return false;
    if(rg&&x.rg!==rg)return false;
    if(so&&x.s!==so)return false;
    if(ar&&x.ct!==ar&&x.rg!==ar)return false;
    if(q){
      var hay=(x.t+' '+(x.a||'')+' '+(x.ct||'')+' '+(x.rg||'')+' '+(x.s||'')+' '+(x.bt||'')).toLowerCase();
      if(hay.indexOf(q)<0)return false;
    }
    return true;
  });
  out.sort(function(a,b){return sortBy==='score'?(b.sc-a.sc):(a.d<b.d?1:a.d>b.d?-1:b.sc-a.sc)});
  return out;
}
function cardHtml(x){
  var chips='';
  if(x.bt)chips+='<span class="chip">类型 '+esc(x.bt)+'</span>';
  if(x.ct||x.rg)chips+='<span class="chip">'+(x.ct?esc(x.ct):esc(x.rg))+'</span>';
  if(x.ar)chips+='<span class="chip">面积 '+esc(x.ar)+'</span>';
  if(x.ic)chips+='<span class="chip">图 '+x.ic+' 张</span>';
  var img=x.c?'<img src="'+esc(x.c)+'" referrerpolicy="no-referrer" loading="lazy" decoding="async" onerror="this.parentNode.classList.add(\\'bad\\')"/>':'';
  return '<div class="card" data-url="'+esc(x.u)+'">'+
    '<div class="pic'+(x.c?'':' none')+'">'+img+'</div>'+
    '<div class="body"><div class="title"><a href="'+esc(x.u)+'" target="_blank" rel="noopener">'+esc(x.t)+'</a></div>'+
    (x.a?'<div class="arch">设计：'+esc(x.a)+'</div>':'')+
    '<div class="tags">'+chips+'</div>'+
    '<div class="foot"><span class="src" style="background:'+(SRCCOLOR[x.s]||'#475569')+'">'+esc(x.s)+'</span>'+
    '<span class="score">'+esc(x.d)+' · '+x.sc+' 分</span>'+
    '<button class="mini" data-fav="'+esc(x.u)+'" data-t="'+esc(x.t)+'" data-s="'+esc(x.s)+'" data-c="'+esc(x.c)+'" data-d="'+esc(x.d)+'">收藏</button></div>'+
    '</div></div>';
}
function draw(){
  var out=filtered(),g=document.getElementById('grid');
  document.getElementById('cnt').textContent='共 '+out.length+' 个';
  g.innerHTML=out.length?out.map(cardHtml).join(''):'<div class="empty">没有匹配的案例，换个条件试试</div>';
  syncFavBtns();
}
function download(name,text,mime){
  var b=new Blob([text],{type:mime}),u=URL.createObjectURL(b),a=document.createElement('a');
  a.href=u;a.download=name;document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(u);a.remove()},400);
}
function exportMd(){
  var out=filtered();
  if(!out.length){alert('当前筛选结果为空');return}
  var lines=['# 建筑案例收藏 · '+new Date().toISOString().slice(0,10),''];
  out.forEach(function(x,i){
    lines.push((i+1)+'. ['+x.t+']('+x.u+')');
    var m=[];if(x.a)m.push(x.a);if(x.bt)m.push(x.bt);if(x.ct||x.rg)m.push(x.ct||x.rg);
    if(x.ar)m.push('面积 '+x.ar);if(x.yr)m.push(x.yr+' 年');if(x.s)m.push(x.s);
    lines.push('   - '+(m.join(' · ')||'—')+'（'+x.d+'，评分 '+x.sc+'）');
    lines.push('   - 封面：'+x.c);
    lines.push('');
  });
  download('建筑案例_'+out.length+'条.md',lines.join('\\n'),'text/markdown;charset=utf-8');
}
function exportCsv(){
  var out=filtered();
  if(!out.length){alert('当前筛选结果为空');return}
  var head=['日期','标题','设计机构','类型','城市','国家或地区','面积','建成年份','图集张数','摄影师','评分','来源','链接','封面'];
  var rows=[head.join(',')];
  out.forEach(function(x){
    rows.push([x.d,x.t,x.a,x.bt,x.ct,x.rg,x.ar,x.yr,x.ic,x.ph,x.sc,x.s,x.u,x.c]
      .map(function(v){v=String(v==null?'':v);return '"'+v.replace(/"/g,'""')+'"'}).join(','));
  });
  download('建筑案例_'+out.length+'条.csv','\\ufeff'+rows.join('\\r\\n'),'text/csv;charset=utf-8');
}
fill('f-bt','bt','类型');fill('f-rg','rg','地域');fill('f-so','s','来源');fill('f-ar','ct','城市');
['q','f-bt','f-rg','f-so','f-ar'].forEach(function(id){
  document.getElementById(id).addEventListener('input',draw);
  document.getElementById(id).addEventListener('change',draw);
});
document.getElementById('tg-fav').addEventListener('click',function(){
  onlyFav=!onlyFav;this.className='tg'+(onlyFav?' on':'');draw();
});
document.getElementById('tg-sort').addEventListener('click',function(){
  sortBy=sortBy==='date'?'score':'date';
  this.textContent=sortBy==='score'?'排序：评分优先':'排序：日期优先';draw();
});
document.getElementById('ex-md').addEventListener('click',exportMd);
document.getElementById('ex-csv').addEventListener('click',exportCsv);
window.addEventListener('storage',function(){syncFavBtns();draw()});
draw();
"""


# ---------------------------------------------------------------- 卡片
def slim(it: dict, date: str) -> dict:
    """灵感库用的精简字段——内联进页面，避免 file:// 下 fetch 被 CORS 拦截"""
    m = it.get("meta", {}) or {}
    return {
        "t": pretty_title(it["title"]), "u": it["url"], "c": it.get("cover") or "",
        "s": it["source"], "a": it.get("architect") or "", "bt": it.get("btype") or "",
        "rg": it.get("region") or "", "ct": it.get("city") or "",
        "ar": str(m.get("area") or ""), "yr": str(m.get("year") or ""),
        "ic": m.get("image_count") or 0, "ph": str(m.get("photographer") or ""),
        "sc": round(float(it.get("score") or 0), 1), "d": date,
        "au": it.get("alt_url") or "",
    }


def detail_block(it: dict) -> str:
    """展开区：完整技术数据 + 评分理由 + 英文原版链接"""
    m = it.get("meta", {}) or {}
    rows = []
    if m.get("area"):
        rows.append(("建筑面积", m["area"]))
    if m.get("year"):
        rows.append(("建成年份", f"{m['year']} 年"))
    if m.get("image_count"):
        rows.append(("图集", f"{m['image_count']} 张"))
    if m.get("photographer"):
        rows.append(("摄影", m["photographer"]))
    city = it.get("city") or ""
    country = m.get("country") or ""
    loc = " / ".join(x for x in (city, country) if x) or it.get("region") or ""
    if loc:
        rows.append(("所在地", loc))
    if m.get("brands"):
        rows.append(("材料厂家", m["brands"]))

    kv = "".join(f'<div class="kv"><b>{esc(k)}</b><span>{esc(v)}</span></div>'
                 for k, v in rows)

    # 技术数据覆盖率有限（约 15% 的卡片能抽到面积），摘要兜底保证详情区不空
    summary = re.sub(r"\s+", " ", (it.get("summary") or "")).strip()
    if summary:
        kv += (f'<div class="kv"><b>摘要</b><span>{esc(summary[:200])}'
               f'{"…" if len(summary) > 200 else ""}</span></div>')

    reasons = it.get("reasons") or []
    why = ""
    if reasons:
        lis = "".join(f"<div>· {esc(r)}</div>" for r in reasons)
        why = (f'<div class="why"><b>为什么推荐 · 合计 {it["score"]:.0f} 分</b>{lis}</div>')

    extra = ""
    if it.get("alt_url"):
        extra = (f'<div class="kv"><b>英文原版</b><span>'
                 f'<a href="{esc(it["alt_url"])}" target="_blank" rel="noopener">'
                 f'打开 ArchDaily 国际站原文</a></span></div>')

    return f'<div class="more" id="{esc(_mid(it["url"]))}">{kv}{extra}{why}</div>'


def _mid(url: str) -> str:
    """由 URL 派生稳定的 DOM id——截断 URL 有碰撞风险，改用哈希"""
    return "m" + hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def card(it: dict) -> str:
    tags = []
    if it.get("btype"):
        tags.append(("类型 " + it["btype"], "#334155"))
    if it.get("region"):
        tags.append(("地域 " + it["region"], "#0f766e"))
    meta = it.get("meta", {})
    if meta.get("area"):
        tags.append(("面积 " + meta["area"], "#7c3aed"))
    if meta.get("year"):
        tags.append(("建成 " + str(meta["year"]), "#b45309"))
    if meta.get("image_count"):
        tags.append((f"图 {meta['image_count']} 张", "#475569"))

    tag_html = "".join(
        f'<span class="chip" style="color:{c}">{esc(t)}</span>' for t, c in tags)

    cover = it.get("cover") or ""
    img = (f'<img src="{esc(cover)}" referrerpolicy="no-referrer" loading="lazy" '
           f'decoding="async" onerror="this.parentNode.classList.add(\'bad\')"/>'
           if cover else "")

    src_color = SOURCE_TAG.get(it["source"], "#475569")
    arch = esc(it.get("architect") or "")
    arch_row = f'<div class="arch">设计：{arch}</div>' if arch else ""
    mid = _mid(it["url"])

    return f"""
    <div class="card" data-url="{esc(it['url'])}">
      <div class="rank">{it['rank']}</div>
      <div class="pic{'' if cover else ' none'}">{img}</div>
      <div class="body">
        <div class="title"><a href="{esc(it['url'])}" target="_blank" rel="noopener">{esc(pretty_title(it['title']))}</a></div>
        {arch_row}
        <div class="tags">{tag_html}</div>
        <div class="foot">
          <span class="src" style="background:{src_color}">{esc(it['source'])}</span>
          <span class="score">{it['score']:.0f} 分</span>
          <button type="button" class="mini" data-more="{mid}">详情</button>
          <button type="button" class="mini" data-fav="{esc(it['url'])}"
            data-t="{esc(pretty_title(it['title']))}" data-s="{esc(it['source'])}"
            data-c="{esc(cover)}" data-d="{esc(it.get('date',''))}">收藏</button>
          <a class="mini" href="{esc(it['url'])}" target="_blank" rel="noopener">原文</a>
        </div>
        {detail_block(it)}
      </div>
    </div>"""


def nav_bar(links: dict, current: str = "") -> str:
    items = [("home", "最新一期"), ("archive", "往期归档"), ("library", "灵感库")]
    out = []
    for k, label in items:
        href = links.get(k)
        if not href:
            continue
        out.append(f'<a href="{esc(href)}"'
                   + (' class="on"' if k == current else "")
                   + f'>{label}</a>')
    return f'<div class="nav">{"".join(out)}</div>' if out else ""


# ---------------------------------------------------------------- 页面
def render(data: dict, links=None, current: str = "") -> str:
    d = data["date"]
    links = links or {"home": "index.html", "archive": "archive.html",
                      "library": "library.html"}
    # 收藏按钮需要日期，补进每条
    for it in data["items"]:
        it.setdefault("date", d)

    src_stat = " · ".join(
        f"{s['source']} {s.get('hit', 0)}" for s in data.get("sources", []) if s.get("ok"))
    cards = "\n".join(card(it) for it in data["items"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#1e293b"/>
<meta name="format-detection" content="telephone=no"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="default"/>
<meta name="apple-mobile-web-app-title" content="建筑案例日报"/>
<link rel="manifest" href="manifest.json"/>
<title>每日建筑案例 · {d}</title>
<style>{CSS_BASE}</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <h1>每日建筑案例 · Top {data['count']}</h1>
    <div class="sub">{d} 发布 · 生成于 {esc(data['generated_at'][11:19])}<br/>{esc(src_stat)}</div>
    <div class="stats">
      <div class="stat"><b>{data['candidate_count']}</b>抓取</div>
      <div class="stat"><b>{data['after_dedupe']}</b>去重后</div>
      <div class="stat"><b>{data['noise_count']}</b>过滤噪音</div>
    </div>
  </div>
  {nav_bar(links, current)}
  {cards}
  <div class="ft">数据来源：ArchDaily / ArchDaily中国 / 谷德设计网 / Dezeen / Designboom / 建筑学院<br/>
  点卡片跳原文，点「详情」看完整数据与评分理由，点「收藏」存进灵感库<br/>
  <a class="ft-link" href="{esc(links.get('library','library.html'))}">打开全量灵感库（搜索 / 筛选 / 导出）›</a></div>
</div>
<script>{JS_CARD}</script>
</body>
</html>"""


WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def render_index(datas: list[dict], links=None, current: str = "archive") -> str:
    """往期归档索引：每天一张卡，展示当日 Top3 缩略图"""
    if not datas:
        return "<html><body style='font-family:sans-serif;padding:40px'>暂无日报</body></html>"
    links = links or {"home": "index.html", "archive": "archive.html",
                      "library": "library.html"}
    rows = []
    for d in sorted(datas, key=lambda x: x["date"], reverse=True):
        items = d["items"][:3]
        thumbs = "".join(
            f'<img src="{esc(i.get("cover") or "")}" referrerpolicy="no-referrer" '
            f'loading="lazy" decoding="async" onerror="this.style.display=\'none\'"/>'
            for i in items)
        lines = "".join(f"<li>{esc(pretty_title(i['title']))}</li>" for i in items)
        try:
            wd = WEEKDAY[dt.date.fromisoformat(d["date"]).weekday()]
        except ValueError:
            wd = ""
        rows.append(f"""
      <a class="day" href="digest_{d['date']}.html">
        <div class="day-hd">
          <span class="day-date">{d['date']}</span>
          <span class="day-wd">{wd}</span>
          <span class="day-num">{d['count']} 个案例</span>
        </div>
        <div class="thumbs">{thumbs}</div>
        <ol class="day-list">{lines}</ol>
        <div class="day-more">查看全部 {d['count']} 个 ›</div>
      </a>""")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#1e293b"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-title" content="建筑案例日报"/>
<link rel="manifest" href="manifest.json"/>
<title>建筑案例日报 · 往期归档</title>
<style>{CSS_BASE}{CSS_ARCHIVE}</style>
</head>
<body><div class="wrap">
  <div class="hd">
    <h1>往期归档</h1>
    <div class="sub">共 {len(datas)} 期 · 累计 {sum(len(x['items']) for x in datas)} 个案例
      · 点击任意一期查看当天全部入选</div>
  </div>
  {nav_bar(links, current)}
  {"".join(rows)}
  <div class="ft"><a class="ft-link" href="{esc(links.get('library','library.html'))}">打开全量灵感库（搜索 / 筛选 / 导出）›</a></div>
</div></body></html>"""


CSS_ARCHIVE = """
  .day{display:block;background:#fff;border-radius:16px;padding:16px;margin-bottom:14px;
       text-decoration:none;color:inherit;-webkit-tap-highlight-color:transparent;
       box-shadow:0 1px 3px rgba(15,23,42,.07),0 8px 22px rgba(15,23,42,.05)}
  .day:active{transform:scale(.99);opacity:.92}
  .day-hd{display:flex;align-items:baseline;gap:8px}
  .day-date{font-size:17px;font-weight:700}
  .day-wd{font-size:12px;color:#94a3b8}
  .day-num{font-size:12px;color:#64748b;margin-left:auto}
  .thumbs{display:flex;gap:6px;margin:12px 0}
  .thumbs img{width:33%;aspect-ratio:4/3;object-fit:cover;border-radius:8px;background:#e2e8f0}
  .day-list{font-size:13px;color:#475569;line-height:1.75;padding-left:18px}
  .day-more{font-size:12px;color:#2563eb;margin-top:10px}
"""


def render_library(datas: list[dict], links=None) -> str:
    """全量灵感库：搜索 / 筛选 / 排序 / 收藏 / 导出"""
    links = links or {"home": "index.html", "archive": "archive.html",
                      "library": "library.html"}
    rows = []
    seen = set()
    for d in sorted(datas, key=lambda x: x["date"], reverse=True):
        for it in d["items"]:
            s = slim(it, d["date"])
            if s["u"] in seen:
                continue
            seen.add(s["u"])
            rows.append(s)

    # 内联进页面（file:// 直接双击打开时 fetch 会被 CORS 拦，必须内联）
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    colors = json.dumps(SOURCE_TAG, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="theme-color" content="#1e293b"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-title" content="建筑案例日报"/>
<link rel="manifest" href="manifest.json"/>
<title>建筑案例 · 全量灵感库</title>
<style>{CSS_BASE}{CSS_LIBRARY}</style>
</head>
<body><div class="wrap wide">
  <div class="hd">
    <h1>全量灵感库</h1>
    <div class="sub">累计 {len(rows)} 个案例 · 覆盖 {len(datas)} 期日报
      · 支持搜索、筛选、收藏与导出</div>
  </div>
  {nav_bar(links, "library")}
  <div class="bar">
    <input id="q" type="search" placeholder="搜索标题 / 事务所 / 城市，例如：住宅、BIG、上海"/>
    <div class="row2">
      <select id="f-bt"></select>
      <select id="f-rg"></select>
    </div>
    <div class="row2">
      <select id="f-so"></select>
      <select id="f-ar"></select>
    </div>
    <div class="toggles">
      <button type="button" class="tg" id="tg-fav">只看收藏</button>
      <button type="button" class="tg" id="tg-sort">排序：日期优先</button>
      <button type="button" class="tg" id="ex-md">导出 Markdown</button>
      <button type="button" class="tg" id="ex-csv">导出表格 CSV</button>
      <span class="cnt" id="cnt"></span>
    </div>
    <div class="hint">收藏存在本机浏览器里，换设备不同步；想长期留存请用导出。导出的范围是当前筛选结果。</div>
  </div>
  <div class="grid" id="grid"></div>
</div>
<script id="items" type="application/json">{payload}</script>
<script>var SRCCOLOR={colors};</script>
<script>{JS_CARD}</script>
<script>{JS_LIBRARY}</script>
</body></html>"""


MANIFEST = {
    "name": "每日建筑案例日报",
    "short_name": "建筑案例",
    "start_url": "index.html",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#eef1f6",
    "theme_color": "#1e293b",
    "lang": "zh-CN",
    "icons": [
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
    ],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="42" fill="#1e293b"/>
<g fill="none" stroke="#f8fafc" stroke-width="7" stroke-linejoin="round" stroke-linecap="round">
<path d="M40 148V64l56-30 56 30v84"/><path d="M60 148v-40h32v40"/><path d="M112 108h20v40"/>
<path d="M40 148h112"/><path d="M112 88h20v20h-20z"/>
</g></svg>"""


def build_all_json(datas: list[dict]) -> list[dict]:
    """全量结构化数据，供外部程序调用"""
    out, seen = [], set()
    for d in sorted(datas, key=lambda x: x["date"]):
        for it in d["items"]:
            s = slim(it, d["date"])
            if s["u"] in seen:
                continue
            seen.add(s["u"])
            out.append(s)
    return out


def write_site(datas: list[dict]) -> str:
    """生成自包含站点目录 site/ —— 供发布为在线链接、手机浏览器直接打开

    云端的 GitHub Actions 直接把页面生成到仓库根目录（Pages 从根发布），
    通过环境变量 SITE_DIR 覆盖，本地默认仍是 site/。
    """
    site = os.environ.get("SITE_DIR") or "site"
    os.makedirs(site, exist_ok=True)
    latest = max(datas, key=lambda x: x["date"])
    links = {"home": "index.html", "archive": "archive.html", "library": "library.html"}

    for d in datas:
        with open(f"{site}/digest_{d['date']}.html", "w", encoding="utf-8") as f:
            f.write(render(d, links=links))

    with open(f"{site}/index.html", "w", encoding="utf-8") as f:
        f.write(render(latest, links=links, current="home"))

    with open(f"{site}/archive.html", "w", encoding="utf-8") as f:
        f.write(render_index(datas, links=links))

    allrows = build_all_json(datas)
    with open(f"{site}/library.html", "w", encoding="utf-8") as f:
        f.write(render_library(datas, links=links))

    with open(f"{site}/all.json", "w", encoding="utf-8") as f:
        json.dump(allrows, f, ensure_ascii=False, indent=1)

    with open(f"{site}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)

    with open(f"{site}/icon.svg", "w", encoding="utf-8") as f:
        f.write(ICON_SVG)

    return (f"{site}/index.html（最新 {latest['date']}） · "
            f"{site}/archive.html（{len(datas)} 期） · "
            f"{site}/library.html（{len(allrows)} 个案例） · {site}/all.json")


def load_datas() -> list[dict]:
    import glob
    datas = []
    for fp in sorted(glob.glob("data/digest_*.json")):
        with open(fp, encoding="utf-8") as f:
            datas.append(json.load(f))
    return datas


def main():
    datas = load_datas()
    if not datas:
        print("未找到 data/digest_*.json，请先运行 run_daily.py")
        return

    # 本地专属产物（预览目录、工作区根目录那份双击即看的 HTML）
    # 云端 CI 没有这层目录结构，用 LOCAL_EXPORT=0 跳过
    if os.environ.get("LOCAL_EXPORT", "1") != "0":
        write_local_extras(datas)

    print(write_site(datas))


def write_local_extras(datas: list[dict]) -> None:
    import glob
    os.makedirs("preview", exist_ok=True)
    links = {"home": "index.html", "archive": "index.html", "library": "library.html"}

    for d in datas:
        with open(f"preview/digest_{d['date']}.html", "w", encoding="utf-8") as f:
            f.write(render(d, links=links))

    with open("preview/index.html", "w", encoding="utf-8") as f:
        f.write(render_index(datas, links=links))

    with open("preview/library.html", "w", encoding="utf-8") as f:
        f.write(render_library(datas, links=links))

    # 最新一期直接放在工作区根目录，双击即看，不必进目录翻找
    latest = max(datas, key=lambda x: x["date"])
    root = os.path.join("..", "每日建筑案例.html")
    root_links = {"home": "", "archive": "arch-daily-digest/preview/index.html",
                  "library": "arch-daily-digest/preview/library.html"}
    with open(root, "w", encoding="utf-8") as f:
        f.write(render(latest, links=root_links, current="home"))

    print(f"preview/（{len(datas)} 期 + 灵感库）\n{os.path.normpath(root)}（最新 {latest['date']}）")


if __name__ == "__main__":
    main()
