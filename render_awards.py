# -*- coding: utf-8 -*-
"""渲染「世界建筑奖项观察」页面 awards.html

数据来自 data/awards.json（人工维护，不参与每日抓取）。
样式复用 render_html.py 的 CSS_BASE，保证全站观感一致、移动端适配一致。

用法：
    python render_awards.py                # 输出 site/awards.html（SITE_DIR 可覆盖）
    LOCAL_EXPORT=1 时同步生成 preview/awards.html 与工作区根目录「建筑奖项.html」
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from render_html import CSS_BASE, nav_bar

try:
    sys_stdout_ready = True
except Exception:
    sys_stdout_ready = False

ROOT = Path(__file__).resolve().parent

CSS_AWARDS = """
  .grp{margin:18px 0 10px;font-size:13px;font-weight:700;color:#334155;
       display:flex;align-items:center;gap:8px}
  .grp span{font-size:11px;font-weight:400;color:#94a3b8}
  .awd{background:#fff;border-radius:16px;padding:16px 14px;margin-bottom:14px;
       box-shadow:0 1px 3px rgba(15,23,42,.08),0 8px 24px rgba(15,23,42,.06)}
  .awd h2{font-size:16px;font-weight:700;line-height:1.4}
  .awd h2 small{font-size:11px;font-weight:400;color:#94a3b8;display:block;margin-top:2px}
  .badges{display:flex;flex-wrap:wrap;gap:5px;margin:10px 0 2px}
  .bdg{font-size:11px;background:#f1f5f9;border-radius:6px;padding:3px 7px;color:#334155}
  .bdg.hot{background:#dc2626;color:#fff}
  .bdg.gold{background:#b45309;color:#fff}
  .who{margin-top:10px;padding:10px;background:#f8fafc;border-radius:10px}
  .who+.who{margin-top:8px}
  .who .t{font-size:13px;font-weight:600;line-height:1.55;color:#0f172a}
  .who .l{font-size:11px;color:#7c3aed;margin-top:2px}
  .who .d{font-size:12px;color:#334155;line-height:1.8;margin-top:5px}
  .who .w{font-size:11px;color:#64748b;line-height:1.8;margin-top:5px}
  .who .w b{color:#15803d;font-weight:600}
  .prev{font-size:11px;color:#94a3b8;line-height:1.8;margin-top:10px;
        border-top:1px dashed #e2e8f0;padding-top:8px}
  .srcs{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .srcs a{font-size:11px;color:#2563eb;text-decoration:none;background:#eff6ff;
          border-radius:6px;padding:4px 8px}
  .srcs a:active{background:#dbeafe}
"""


def esc(s) -> str:
    return html.escape(str(s or ""))


def award_card(a: dict) -> str:
    badges = [f'<span class="bdg gold">{esc(a["edition"])}</span>',
              f'<span class="bdg">公布 {esc(a["announced"])}</span>',
              f'<span class="bdg">{esc(a["cycle"])}</span>']
    if a.get("weight"):
        badges.insert(0, f'<span class="bdg hot">{esc(a["weight"])}</span>')

    winners = []
    for i, w in enumerate(a.get("laureates", []), 1):
        winners.append(
            f'<div class="who">'
            f'<div class="t">{i:>2}. {esc(w["who"])}</div>'
            f'<div class="l">{esc(w["from"])}</div>'
            f'<div class="d">{esc(w["what"])}</div>'
            f'<div class="w"><b>获奖理由：</b>{esc(w["why"])}</div>'
            f'</div>')

    srcs = "".join(
        f'<a href="{esc(s["url"])}" target="_blank" rel="noopener">来源：{esc(s["label"])}</a>'
        for s in a.get("sources", []))

    return (
        f'<div class="awd">'
        f'<h2>{esc(a["name"])}<small>{esc(a["name_en"])} · {esc(a["org"])}</small></h2>'
        f'<div class="badges">{"".join(badges)}</div>'
        f'{"".join(winners)}'
        + (f'<div class="prev">{esc(a["prev"])}</div>' if a.get("prev") else "")
        + (f'<div class="srcs">{srcs}</div>' if srcs else "")
        + '</div>')


def render_awards(data: dict, links: dict) -> str:
    n_awards = sum(len(g["awards"]) for g in data["groups"])
    n_projects = sum(
        len(a.get("laureates", []))
        for g in data["groups"] for a in g["awards"])

    groups_html = []
    for g in data["groups"]:
        cards = "".join(award_card(a) for a in g["awards"])
        groups_html.append(
            f'<div class="grp">{esc(g["name"])}<span>{esc(g["desc"])}</span></div>'
            f'{cards}')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(data["meta"]["title"])}</title>
<link rel="icon" href="icon.svg">
<style>{CSS_BASE}{CSS_AWARDS}</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <h1>{esc(data["meta"]["title"])}</h1>
    <div class="sub">近年国际权威建筑奖项的最新一届结果<br/>
    数据截至 <b>{esc(data["meta"]["data_cutoff"])}</b>，全部来自奖项官网或权威媒体，来源可溯源<br/>
    奖项一年或数年一届，公布后手动更新，<b>不参与每日抓取</b></div>
    <div class="stats">
      <div class="stat"><b>{n_awards}</b>奖项体系</div>
      <div class="stat"><b>{n_projects}</b>获奖条目</div>
      <div class="stat"><b>{len(data["groups"])}</b>大类</div>
    </div>
  </div>
  {nav_bar(links, current="awards")}
  {"".join(groups_html)}
  <div class="ft">{esc(data["meta"]["note"])}<br/>
  {esc(data["meta"]["update_hint"])}</div>
</div>
</body>
</html>"""


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    data = json.loads((ROOT / "data" / "awards.json").read_text(encoding="utf-8"))

    site = os.environ.get("SITE_DIR") or "site"
    os.makedirs(site, exist_ok=True)
    links = {"home": "index.html", "archive": "archive.html",
             "library": "library.html", "awards": "awards.html"}
    out = Path(site) / "awards.html"
    out.write_text(render_awards(data, links), encoding="utf-8")
    print(f"已生成 {out}")

    if os.environ.get("LOCAL_EXPORT", "1") != "0":
        pv = ROOT / "preview"
        pv.mkdir(exist_ok=True)
        (pv / "awards.html").write_text(
            render_awards(data, {"home": "index.html", "archive": "index.html",
                                 "library": "library.html",
                                 "awards": "awards.html"}),
            encoding="utf-8")
        root_html = ROOT.parent / "建筑奖项.html"
        root_html.write_text(
            render_awards(data, {"home": "arch-daily-digest/preview/index.html",
                                 "archive": "arch-daily-digest/preview/index.html",
                                 "library": "arch-daily-digest/preview/library.html",
                                 "awards": ""}),
            encoding="utf-8")
        print(f"已生成 preview/awards.html 与 {root_html.name}（双击即看）")


if __name__ == "__main__":
    main()
