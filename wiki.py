#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""星海百科 · 独立 WIKI 网站（替代原「斗地主」网页服务）

不是 QQ 指令插件，而是一个**对外可访问的网站**：
  * 监听 0.0.0.0:8081（沿用斗地主原来的端口，防火墙/端口转发不用改）
  * 公开可读：首页 / 词条页 / 搜索 / 标签 / 最近更新 / 历史版本
  * 受控可写：新建与编辑需要编辑口令（WIKI_EDIT_TOKEN 或 wiki_data/config.json 里的 token），
    启动日志会打印网址与口令，登录后写 cookie，之后正常编辑
  * 数据存在 src/plugins/wiki_data/pages.json（纯 JSON，随时可备份/手工改）
  * 自带 Markdown-lite 渲染（标题 / 粗体 / 行内代码 / 代码块 / 列表 / 链接 / 引用 / 表格分隔线）

设计原则：零前端依赖（不用 CDN、不打包），服务端渲染，断网也能开。
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
import uvicorn

from nonebot import get_driver
from nonebot.log import logger

# ============================================================
# 配置与数据
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "wiki_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PAGES_FILE = DATA_DIR / "pages.json"
CONFIG_FILE = DATA_DIR / "config.json"

WEB_HOST = "0.0.0.0"
WEB_PORT = 8081
PUBLIC_URL = "http://42.193.21.193:8081"

SITE_NAME = "星海百科"
SITE_SUB = "群友共建的知识库"

MAX_TITLE = 60
MAX_BODY = 40000


def _now() -> float:
    return time.time()


def _empty() -> Dict[str, Any]:
    return {"pages": {}, "created": _now()}


def load_pages() -> Dict[str, Any]:
    if not PAGES_FILE.exists():
        return _empty()
    try:
        data = json.loads(PAGES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("pages", {})
        return data
    except Exception as e:
        logger.error(f"[wiki] 读取词条失败: {e}")
        return _empty()


def save_pages(data: Dict[str, Any]) -> bool:
    try:
        tmp = PAGES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PAGES_FILE)
        return True
    except Exception as e:
        logger.error(f"[wiki] 保存词条失败: {e}")
        return False


def load_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    tok = str(cfg.get("token") or "")
    if not tok or tok == "__AUTO_GENERATED__":
        cfg["token"] = secrets.token_urlsafe(12)
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception as e:
            logger.warning(f"[wiki] 写入配置失败: {e}")
    return cfg


CONFIG: Dict[str, Any] = {}
EDIT_TOKEN = ""
COOKIE_NAME = "wiki_token"


# ============================================================
# Markdown-lite（够用就行：标题/粗体/斜体/代码/列表/链接/引用）
# ============================================================

def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def md_inline(text: str) -> str:
    s = _esc(text)
    s = re.sub(r"`([^`]*?)`", r"<code>\1</code>", s)          # 行内代码
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" rel="noopener" target="_blank">\1</a>', s)
    s = re.sub(r"(?<![\"'=])(https?://[^\s<]+)",
               r'<a href="\1" rel="noopener" target="_blank">\1</a>', s)
    return s


def md_to_html(body: str) -> str:
    """把正文渲染成 HTML（先转义，只放行我们自己生成的标签，避免 XSS）"""
    lines = str(body or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    in_code = False
    in_ul = in_ol = False
    para: List[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            out.append("<p>" + md_inline(" ".join(para)) + "</p>")
            para = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    for raw in lines:
        line = raw.rstrip()

        fence = line.strip().startswith("```")
        if fence:
            flush_para()
            close_lists()
            if not in_code:
                out.append('<pre class="code"><code>')
                in_code = True
            else:
                out.append("</code></pre>")
                in_code = False
            continue
        if in_code:
            out.append(_esc(line))
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_para()
            close_lists()
            level = len(m.group(1)) + 1
            out.append(f"<h{level} id='s{len(out)}'>{md_inline(m.group(2))}</h{level}>")
            continue

        m = re.match(r"^\s*==\s*(.+?)\s*==\s*$", line)
        if m:
            flush_para()
            close_lists()
            out.append(f"<h2>{md_inline(m.group(1))}</h2>")
            continue

        if re.match(r"^\s*([-*+])\s+", line):
            flush_para()
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append("<li>" + md_inline(re.sub(r"^\s*[-*+]\s+", "", line)) + "</li>")
            continue

        m = re.match(r"^\s*(\d+)[.、]\s+(.*)$", line)
        if m:
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append("<li>" + md_inline(m.group(2)) + "</li>")
            continue

        if re.match(r"^\s*>\s?", line):
            flush_para()
            close_lists()
            out.append("<blockquote>" + md_inline(re.sub(r"^\s*>\s?", "", line)) + "</blockquote>")
            continue

        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            close_lists()
            out.append("<hr/>")
            continue

        if not line.strip():
            flush_para()
            close_lists()
            continue

        para.append(line.strip())

    flush_para()
    close_lists()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


# ============================================================
# 页面模板
# ============================================================

CSS = """
:root{
  --bg:#f7f8f5; --panel:#ffffff; --ink:#1f2733; --sub:#6b7686;
  --line:#e5e8e2; --accent:#2f6f4f; --accent2:#3f7ea6; --soft:#eef2ec;
  --code:#282c34;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#14181d; --panel:#1b2027; --ink:#e6ebf2; --sub:#93a0b0;
    --line:#2a313a; --accent:#6fd39a; --accent2:#79b8e6; --soft:#212833;
    --code:#0d1013; }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink); line-height:1.75;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",sans-serif;
  font-size:15.5px;
}
a{color:var(--accent2);text-decoration:none}
a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
.side{
  width:250px;flex:0 0 250px;background:var(--panel);border-right:1px solid var(--line);
  padding:22px 18px;position:sticky;top:0;height:100vh;overflow:auto;
}
.logo{display:flex;align-items:center;gap:9px;font-size:19px;font-weight:800;letter-spacing:2px;color:var(--ink)}
.logo-img{width:30px;height:30px;border-radius:8px;display:block;flex-shrink:0}
.logo span{color:var(--accent)}
.sub{font-size:12px;color:var(--sub);margin-top:4px;letter-spacing:.5px}
.search{margin:18px 0 10px;display:flex;gap:6px}
.search input{
  flex:1;min-width:0;padding:8px 11px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--ink);font-size:13.5px;outline:none;
}
.search input:focus{border-color:var(--accent)}
.search button{
  padding:8px 12px;border:1px solid var(--accent);background:var(--accent);color:#fff;
  border-radius:8px;font-size:13.5px;cursor:pointer;
}
.nav{margin-top:14px;display:flex;flex-direction:column;gap:2px}
.nav a{
  display:block;padding:6px 10px;border-radius:7px;color:var(--ink);font-size:14px;
}
.nav a:hover{background:var(--soft);text-decoration:none}
.nav a.on{background:var(--soft);color:var(--accent);font-weight:600}
.side h3{
  font-size:11.5px;color:var(--sub);letter-spacing:1.5px;margin:20px 0 6px;
  font-weight:600;text-transform:uppercase;
}
.side ul{list-style:none;margin:0;padding:0}
.side ul li a{
  display:block;padding:4px 8px;border-radius:6px;font-size:13px;color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.side ul li a:hover{background:var(--soft);color:var(--ink);text-decoration:none}
.main{flex:1;min-width:0;padding:30px 40px 60px;max-width:1000px}
.crumb{font-size:12.5px;color:var(--sub);margin-bottom:10px}
h1{font-size:31px;line-height:1.3;margin:0 0 6px;letter-spacing:.5px}
.meta{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--sub);
  padding-bottom:14px;border-bottom:1px solid var(--line);margin-bottom:22px}
.tag{display:inline-block;padding:1px 9px;border-radius:10px;background:var(--soft);
  color:var(--accent2);font-size:12px}
.article{font-size:16px}
.article h2{font-size:22px;margin:30px 0 12px;padding-left:11px;border-left:4px solid var(--accent)}
.article h3{font-size:18px;margin:22px 0 10px}
.article p{margin:0 0 13px}
.article ul,.article ol{margin:0 0 14px;padding-left:26px}
.article li{margin:3px 0}
.article blockquote{
  margin:14px 0;padding:8px 16px;border-left:3px solid var(--line);
  color:var(--sub);background:var(--soft);border-radius:0 8px 8px 0;
}
.article code{
  background:var(--soft);padding:1.5px 6px;border-radius:5px;font-size:.92em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
.article pre.code{
  background:var(--code);color:#e6edf3;padding:14px 16px;border-radius:10px;
  overflow:auto;font-size:13.5px;line-height:1.6;
}
.article pre.code code{background:none;padding:0;color:inherit}
.article hr{border:none;border-top:1px solid var(--line);margin:26px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:15px 17px;
  display:block;transition:border-color .15s,transform .15s;
}
.card:hover{border-color:var(--accent);text-decoration:none;transform:translateY(-1px)}
.card .t{font-size:15.5px;font-weight:700;color:var(--ink);margin-bottom:6px}
.card .d{font-size:13px;color:var(--sub);line-height:1.65;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.empty{color:var(--sub);text-align:center;padding:50px 0;font-size:15px}
.btn{
  display:inline-block;padding:8px 16px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);font-size:14px;cursor:pointer;
}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn:hover{text-decoration:none;opacity:.92}
.form{max-width:820px}
.form label{display:block;font-size:13px;color:var(--sub);margin:16px 0 6px}
.form input[type=text],.form textarea{
  width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel);color:var(--ink);font-size:15px;font-family:inherit;outline:none;
}
.form textarea{min-height:380px;line-height:1.8;resize:vertical;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px}
.form input:focus,.form textarea:focus{border-color:var(--accent)}
.actions{margin-top:20px;display:flex;gap:10px;align-items:center}
.notice{
  padding:10px 15px;border-radius:9px;margin-bottom:18px;font-size:14px;
  background:var(--soft);color:var(--accent);border:1px solid var(--line);
}
.notice.warn{color:#b4573c}
.foot{margin-top:50px;padding-top:16px;border-top:1px solid var(--line);
  font-size:12px;color:var(--sub);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.hist .row{padding:9px 0;border-bottom:1px dashed var(--line);font-size:13.5px;color:var(--sub)}
@media (max-width:820px){
  .layout{flex-direction:column}
  .side{width:auto;flex:none;height:auto;position:static;border-right:none;
    border-bottom:1px solid var(--line)}
  .main{padding:22px 18px 50px}
  .side ul{display:flex;flex-wrap:wrap;gap:4px}
  h1{font-size:25px}
}
"""


ADMIN_CSS = """
.admin-head{display:flex;align-items:baseline;gap:12px;margin-bottom:6px}
.admin-head h1{font-size:27px;margin:0}
.admin-head .t{font-size:12.5px;color:var(--sub)}
.tabs{display:flex;gap:6px;margin:16px 0 22px;border-bottom:1px solid var(--line);
  padding-bottom:2px;flex-wrap:wrap}
.tabs a{padding:7px 14px;border-radius:8px 8px 0 0;font-size:14px;color:var(--sub)}
.tabs a.on{background:var(--soft);color:var(--accent);font-weight:600}
.tabs a:hover{text-decoration:none;color:var(--ink)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px}
.metric .k{font-size:12.5px;color:var(--sub);margin-bottom:8px}
.metric .v{font-size:26px;font-weight:700;line-height:1.1}
.metric .u{font-size:13px;font-weight:400;color:var(--sub);margin-left:3px}
.metric .s{font-size:12px;color:var(--sub);margin-top:7px}
.bar{height:7px;border-radius:4px;background:var(--soft);overflow:hidden;margin-top:11px}
.bar i{display:block;height:100%;border-radius:4px;transition:width .4s}
.bar i.ok{background:var(--accent)}
.bar i.warn{background:#e6a23c}
.bar i.bad{background:#d9534f}
table.t{width:100%;border-collapse:collapse;font-size:13.5px}
table.t th{text-align:left;font-weight:600;color:var(--sub);font-size:12px;
  padding:9px 10px;border-bottom:1px solid var(--line)}
table.t td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
table.t tr:hover td{background:var(--soft)}
.thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:12px}
.thumb{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  overflow:hidden}
.thumb img{width:100%;height:104px;object-fit:cover;display:block;background:var(--soft)}
.thumb .m{padding:7px 9px;font-size:11.5px;color:var(--sub);line-height:1.5}
.thumb .m b{display:block;color:var(--ink);font-size:12px;margin-bottom:2px}
.drop{border:2px dashed var(--line);border-radius:12px;padding:26px;text-align:center;
  color:var(--sub);font-size:14px;cursor:pointer;transition:.15s;background:var(--panel)}
.drop:hover,.drop.over{border-color:var(--accent);color:var(--accent)}
.drop b{color:var(--ink)}
#pv{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
#pv img{height:82px;border-radius:8px;border:1px solid var(--line)}
"""



def _js(text: str) -> str:
    """转义成可安全放进 onclick/onsubmit 字符串字面量的文本"""
    return (str(text or "").replace("\\", "\\\\").replace("'", "\\'")
            .replace('"', "&quot;").replace("<", "\\x3c").replace(">", "\\x3e")
            .replace("\n", "\\n").replace("\r", ""))

def _layout(title: str, inner: str, active: str = "", crumb: str = "",
            notice: str = "") -> str:
    data = load_pages()
    pages = data.get("pages") or {}
    recent = sorted(pages.items(), key=lambda kv: -float(kv[1].get("updated", 0)))[:12]
    total = len(pages)
    nav_items = [("/", "首页", "home"), ("/all", "全部词条", "all"),
                 ("/tags", "标签", "tags"), ("/recent", "最近更新", "recent"),
                 ("/new", "新建词条", "new"), ("/admin", "后台", "admin")]
    nav = "".join(
        f'<a class="{"on" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in nav_items)
    side_recent = "".join(
        f'<li><a href="/p/{quote(name)}">{_esc(name)}</a></li>' for name, _ in recent)
    _u = CUR_USER["v"] if "CUR_USER" in globals() else None
    if _u:
        _role = effective_role(_u) if "effective_role" in globals() else "guest"
        signed = (f'{_esc(_u.get("username") or _u.get("uid"))}'
                  f'<span class="badge {_role}">{ROLE_NAMES.get(_role, _role)}</span>'
                  if "ROLE_NAMES" in globals() else "已登录")
        if _role == "owner":
            signed += ' · <a href="/admin/cookies">审核</a>'
        signed += ' · <a href="/logout">退出</a>'
    else:
        signed = '<a href="/login">登录</a> · <a href="/register">注册</a>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{SITE_SUB}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<style>{CSS}{ADMIN_CSS}{globals().get("AUTH_CSS", "")}</style>
</head><body>
<div class="layout">
  <aside class="side">
    <div class="logo"><img src="/favicon.svg" alt="" class="logo-img">星海<span>百科</span></div>
    <div class="sub">{SITE_SUB} · {total} 个词条</div>
    <form class="search" action="/search" method="get">
      <input name="q" placeholder="搜索词条…" autocomplete="off">
      <button type="submit">搜</button>
    </form>
    <nav class="nav">{nav}</nav>
    <h3>最近更新</h3>
    <ul>{side_recent or '<li style="color:var(--sub);font-size:12.5px">还没有内容</li>'}</ul>
    <div style="margin-top:24px;font-size:12px;color:var(--sub)">
      {signed or '<a href="/login">编辑登录</a>'}
    </div>
  </aside>
  <main class="main">
    {f'<div class="crumb">{crumb}</div>' if crumb else ''}
    {notice}
    {inner}
    <div class="foot">
      <span>{SITE_NAME} · 群友共建 · 数据存放于 wiki_data/pages.json</span>
      <span>{datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
    </div>
  </main>
</div></body></html>"""


def page_card(name: str, page: Dict[str, Any]) -> str:
    """词条卡片的摘要：去掉 markdown 记号，只留纯文本"""
    text = str(page.get("body") or "")
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*==\s*(.+?)\s*==\s*$", r"\1", text, flags=re.M)
    text = text.replace(chr(96) * 3, " ")
    text = re.sub(r"[*_>" + re.escape(chr(96)) + r"]", "", text)
    text = re.sub(r"^\s*([-+]|\d+[.、])\s+", "\u00b7 ", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    body = re.sub(r"\s+", " ", text).strip()
    return (f'<a class="card" href="/p/{quote(name)}">'
            f'<div class="t">{_esc(name)}</div>'
            f'<div class="d">{_esc(body[:110])}</div></a>')


def view_home() -> str:
    data = load_pages()
    pages = data.get("pages") or {}
    if not pages:
        inner = ('<h1>欢迎来到星海百科</h1>'
                 '<div class="meta"><span>这里还没有词条</span></div>'
                 '<div class="empty">发个词条吧：点左侧「新建词条」，'
                 '或用编辑口令登录后创建。</div>')
        return _layout("首页", inner, "home")
    recent = sorted(pages.items(), key=lambda kv: -float(kv[1].get("updated", 0)))
    cards = "".join(page_card(n, p) for n, p in recent[:12])
    inner = (f'<h1>{SITE_NAME}</h1>'
             f'<div class="meta"><span>{len(pages)} 个词条</span>'
             f'<span>最近更新 {datetime.fromtimestamp(recent[0][1].get("updated", _now())).strftime("%Y-%m-%d")}</span>'
             f'<span>{SITE_SUB}</span></div>'
             f'<div class="cards">{cards}</div>')
    return _layout("首页", inner, "home")


def view_page(name: str) -> str:
    data = load_pages()
    page = (data.get("pages") or {}).get(name)
    if not page:
        inner = (f'<h1>没有这个词条</h1><div class="empty">'
                 f'「{_esc(name)}」还不存在。'
                 f'<a href="/new?title={quote(name)}">创建一个？</a></div>')
        return _layout("未找到", inner)
    tags = "".join(f'<a class="tag" href="/tag/{quote(t)}">{_esc(t)}</a>'
                   for t in (page.get("tags") or []))
    updated = datetime.fromtimestamp(float(page.get("updated", _now()))).strftime("%Y-%m-%d %H:%M")
    body_html = md_to_html(page.get("body") or "")
    edit_btn = (f'<a class="btn" href="/edit/{quote(name)}">编辑</a>'
                f'<a class="btn" href="/history/{quote(name)}">历史</a>')
    # 有编辑权限的人可以在详情页直接删除（带二次确认）
    if can_edit(CUR_USER["v"]):
        edit_btn += (f'<form method="post" action="/admin/pages/delete" style="display:inline" '
                     f'onsubmit="return confirm(\'确定删除「{_js(name)}」？删除后可在回收站恢复\')">'
                     f'<input type="hidden" name="name" value="{_esc(name)}">'
                     f'<button class="btn" style="color:#b4573c;border-color:#e0b9ad">'
                     f'删除</button></form>')
    inner = (f'<h1>{_esc(name)}</h1>'
             f'<div class="meta"><span>🕒 {updated}</span>'
             f'<span>✏️ {int(page.get("edits", 0))} 次编辑</span>{tags}</div>'
             f'<article class="article">{body_html or "<p style=color:var(--sub)>（还没有正文）</p>"}</article>'
             f'<div class="actions" style="margin-top:30px">{edit_btn}</div>')
    return _layout(name, inner, crumb=f'<a href="/">首页</a> / {_esc(name)}')


def view_all() -> str:
    data = load_pages()
    pages = data.get("pages") or {}
    cards = "".join(page_card(n, p) for n, p in sorted(pages.items()))
    inner = (f'<h1>全部词条</h1><div class="meta"><span>{len(pages)} 个</span></div>'
             f'<div class="cards">{cards or "<div class=empty>暂无词条</div>"}</div>')
    return _layout("全部词条", inner, "all")


def view_search(q: str) -> str:
    q = (q or "").strip()
    data = load_pages()
    pages = data.get("pages") or {}
    hits: List[Tuple[str, Dict[str, Any]]] = []
    if q:
        k = q.lower()
        for n, p in pages.items():
            body = str(p.get("body") or "").lower()
            tags = " ".join(p.get("tags") or []).lower()
            score = (10 if k in n.lower() else 0) + (6 if k in tags else 0) \
                    + (3 + body.count(k) if k in body else 0)
            if score:
                hits.append((score, n, p))  # type: ignore[arg-type]
        hits.sort(key=lambda x: -x[0])      # type: ignore[index]
    cards = "".join(page_card(n, p) for _, n, p in hits)  # type: ignore[misc]
    inner = (f'<h1>搜索：{_esc(q)}</h1>'
             f'<div class="meta"><span>命中 {len(hits)} 个词条</span></div>'
             + (f'<div class="cards">{cards}</div>' if hits else
                '<div class="empty">没有找到相关词条。'
                f'<a href="/new?title={quote(q)}">创建一个？</a></div>'))
    return _layout("搜索", inner, crumb=f'搜索 / {_esc(q)}')


def view_tags() -> str:
    data = load_pages()
    counts: Dict[str, int] = {}
    for p in (data.get("pages") or {}).values():
        for t in (p.get("tags") or []):
            counts[t] = counts.get(t, 0) + 1
    chips = "".join(
        f'<a class="tag" style="font-size:13.5px;padding:4px 12px;margin:4px" '
        f'href="/tag/{quote(t)}">{_esc(t)} ×{c}</a>'
        for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))
    inner = (f'<h1>标签</h1><div class="meta"><span>{len(counts)} 个标签</span></div>'
             f'<div>{chips or "<div class=empty>还没有标签</div>"}</div>')
    return _layout("标签", inner, "tags")


def view_tag(tag: str) -> str:
    data = load_pages()
    pages = {n: p for n, p in (data.get("pages") or {}).items()
             if tag in (p.get("tags") or [])}
    cards = "".join(page_card(n, p) for n, p in sorted(pages.items()))
    inner = (f'<h1>标签：{_esc(tag)}</h1>'
             f'<div class="meta"><span>{len(pages)} 个词条</span></div>'
             f'<div class="cards">{cards or "<div class=empty>这个标签下还没有词条</div>"}</div>')
    return _layout("标签", inner, "tags", crumb=f'<a href="/tags">标签</a> / {_esc(tag)}')


def view_recent() -> str:
    data = load_pages()
    pages = sorted((data.get("pages") or {}).items(),
                   key=lambda kv: -float(kv[1].get("updated", 0)))[:50]
    cards = "".join(page_card(n, p) for n, p in pages)
    inner = (f'<h1>最近更新</h1><div class="meta"><span>最近 {len(pages)} 条</span></div>'
             f'<div class="cards">{cards or "<div class=empty>暂无</div>"}</div>')
    return _layout("最近更新", inner, "recent")


def view_new(title: str = "") -> str:
    inner = f"""
    <h1>新建词条</h1>
    <div class="meta"><span>标题建议用名词短语，例如「电压表」「群规」</span></div>
    <form class="form" method="post" action="/new">
      <label>标题</label>
      <input type="text" name="title" value="{_esc(title)}" maxlength="{MAX_TITLE}" required>
      <label>标签（英文逗号分隔，可留空）</label>
      <input type="text" name="tags" placeholder="物理, 电学">
      <label>正文（支持 Markdown：## 标题、**粗体**、- 列表、`代码`）</label>
      <div style="display:flex;gap:8px;align-items:center;margin:10px 0 8px">
        <button type="button" class="btn" id="imgbtn">🖼 插入图片</button>
        <span style="font-size:12.5px;color:var(--sub)" id="imgtip">
          单张不超过 {MAX_UPLOAD_MB}MB，会自动插到光标处</span>
        <input type="file" id="imgfile" accept="image/*" hidden>
      </div>
      <textarea name="body" id="body" required placeholder="写点什么…"></textarea>

      {EDITOR_JS}
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <a class="btn" href="/">取消</a>
      </div>
    </form>"""
    return _layout("新建词条", inner, "new")


def view_edit(name: str) -> str:
    data = load_pages()
    page = (data.get("pages") or {}).get(name)
    if not page:
        return view_page(name)
    inner = f"""
    <h1>编辑：{_esc(name)}</h1>
    <div class="meta"><span>保存后会留一份历史版本</span></div>
    <form class="form" method="post" action="/edit/{quote(name)}">
      <label>标签（英文逗号分隔）</label>
      <input type="text" name="tags" value="{_esc(','.join(page.get('tags') or []))}">
      <label>正文</label>
      <textarea name="body" required>{_esc(page.get('body') or '')}</textarea>

      {EDITOR_JS}
      <div class="actions">
        <button class="btn primary" type="submit">保存</button>
        <a class="btn" href="/p/{quote(name)}">取消</a>
      </div>
    </form>"""
    return _layout("编辑", inner, crumb=f'<a href="/p/{quote(name)}">{_esc(name)}</a> / 编辑')


def view_history(name: str) -> str:
    data = load_pages()
    page = (data.get("pages") or {}).get(name)
    if not page:
        return view_page(name)
    rows = page.get("history") or []
    body = "".join(
        f'<div class="row">#{len(rows) - i}　'
        f'{datetime.fromtimestamp(float(h.get("time", 0))).strftime("%Y-%m-%d %H:%M")}'
        f'　{h.get("by", "")}　（{len(str(h.get("body") or ""))} 字符）'
        f'<pre style="white-space:pre-wrap;color:var(--sub);font-size:12.5px;margin:6px 0 0">'
        f'{_esc(str(h.get("body") or "")[:600])}</pre></div>'
        for i, h in enumerate(rows[-10:]))
    inner = (f'<h1>历史：{_esc(name)}</h1>'
             f'<div class="meta"><span>共 {len(rows)} 个历史版本（显示最近 10 个）</span></div>'
             f'<div class="hist">{body or "<div class=empty>还没有历史版本</div>"}</div>'
             f'<div class="actions"><a class="btn" href="/p/{quote(name)}">返回词条</a></div>')
    return _layout("历史", inner, crumb=f'<a href="/p/{quote(name)}">{_esc(name)}</a> / 历史')


# ============================================================
# 图片上传与静态资源
# ============================================================
# 上传落地在 wiki_data/uploads/，用 /media/<文件名> 对外提供。
# 安全约束（缺一不可）：
#   * 体积上限 MAX_UPLOAD_MB（默认 5MB），超了直接拒；
#   * 只允许 png/jpg/gif/webp/svg，并且**校验文件头**（防伪装扩展名）；
#   * 文件名重新生成（内容哈希 + 扩展名），杜绝路径穿越；
#   * 上传接口必须已登录（编辑口令），匿名只能看不能传。

import hashlib

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
}
MEDIA_INDEX = DATA_DIR / "media.json"


EDITOR_JS = """
<script>
(function() {
  const ib = document.getElementById('imgbtn');
  const iff = document.getElementById('imgfile');
  const itip = document.getElementById('imgtip');
  const ta = document.getElementById('body');
  if (!ib) return;
  ib.addEventListener('click', () => iff.click());
  iff.addEventListener('change', async () => {
    const f = iff.files[0];
    if (!f) return;
    if (f.size > MAXBYTES) {
      itip.textContent = '太大（' + (f.size / 1048576).toFixed(1) + 'MB），上限 MAXMB';
      return;
    }
    itip.textContent = '上传中…';
    const fd = new FormData(); fd.append('file', f);
    try {
      const r = await fetch('/api/upload', {method: 'POST', body: fd});
      const j = await r.json();
      if (!j.ok) { itip.textContent = '失败：' + j.error; return; }
      const pos = ta.selectionStart || ta.value.length;
      const md = '\\n![' + f.name.replace(/\\.[^.]+$/, '') + '](' + j.url + ')\\n';
      ta.value = ta.value.slice(0, pos) + md + ta.value.slice(pos);
      itip.textContent = '已插入 ' + j.url;
    } catch (e) { itip.textContent = '上传失败：' + e; }
    iff.value = '';
  });
})();
</script>
""".replace("MAXBYTES", str(MAX_UPLOAD_BYTES)).replace("MAXMB", str(MAX_UPLOAD_MB))



def load_media() -> Dict[str, Any]:
    if not MEDIA_INDEX.exists():
        return {"items": {}}
    try:
        data = json.loads(MEDIA_INDEX.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("items", {})
            return data
    except Exception as e:
        logger.warning(f"[wiki] 读取媒体索引失败: {e}")
    return {"items": {}}


def save_media(data: Dict[str, Any]) -> None:
    try:
        tmp = MEDIA_INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MEDIA_INDEX)
    except Exception as e:
        logger.warning(f"[wiki] 保存媒体索引失败: {e}")


def sniff_image_ext(head: bytes) -> Optional[str]:
    """按文件头判断真实图片类型，返回安全扩展名（不认识返回 None）"""
    for magic, ext in IMAGE_MAGIC.items():
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    stripped = head.lstrip()[:200].lower()
    if stripped.startswith(b"<svg") or b"<svg" in stripped[:120]:
        return ".svg"
    return None


def media_url(name: str) -> str:
    return "/media/" + quote(name)


def save_upload(filename: str, content: bytes, uid: str = "") -> Tuple[bool, str]:
    """保存上传的图片，返回 (是否成功, 相对URL或错误信息)"""
    if not content:
        return False, "空文件"
    if len(content) > MAX_UPLOAD_BYTES:
        return False, f"图片太大（{len(content) / 1024 / 1024:.1f}MB，上限 {MAX_UPLOAD_MB}MB）"
    ext = sniff_image_ext(content[:2048])
    if not ext:
        return False, "只支持 png / jpg / gif / webp / svg 图片（已校验文件内容）"
    digest = hashlib.sha1(content).hexdigest()[:16]
    target = UPLOAD_DIR / f"{digest}{ext}"
    if not target.exists():
        try:
            target.write_bytes(content)
        except Exception as e:
            logger.error(f"[wiki] 保存图片失败: {e}")
            return False, "服务器写入失败"
    url = media_url(target.name)
    data = load_media()
    item = data["items"].get(target.name) or {}
    item.update({
        "name": target.name, "url": url, "size": len(content), "ext": ext,
        "time": item.get("time") or _now(), "uses": int(item.get("uses", 0)) + 1,
        "origin": (filename or "")[:80], "by": uid or "web",
    })
    data["items"][target.name] = item
    save_media(data)
    logger.info(f"[wiki] 图片已保存：{target.name}（{len(content)} 字节）")
    return True, url


def uploads_stats() -> Dict[str, Any]:
    data = load_media()
    items = list((data.get("items") or {}).values())
    total = 0
    for it in items:
        try:
            total += (UPLOAD_DIR / it["name"]).stat().st_size
        except OSError:
            continue
    return {"count": len(items), "bytes": total,
            "items": sorted(items, key=lambda x: -float(x.get("time", 0)))[:60]}


# ============================================================
# 服务器负载（直接读 /proc，不依赖 psutil）
# ============================================================

_BOOT_TIME = 0.0
try:
    _BOOT_TIME = float(Path("/proc/stat").read_text().splitlines()[0].split()[1]) \
        if False else time.time() - float(
            next(l for l in Path("/proc/uptime").read_text().splitlines()).split()[0])
except Exception:
    _BOOT_TIME = 0.0

_CPU_SAMPLE = {"time": 0.0, "idle": 0, "total": 0}


def _cpu_times() -> Tuple[int, int]:
    """返回 (总 jiffies, 空闲 jiffies)"""
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        nums = [int(x) for x in parts]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums), idle
    except Exception:
        return 0, 0


def cpu_percent() -> float:
    """两次采样之间的 CPU 使用率（首次调用返回 0）"""
    total, idle = _cpu_times()
    prev_t, prev_i = _CPU_SAMPLE["total"], _CPU_SAMPLE["idle"]
    _CPU_SAMPLE.update({"time": _now(), "total": total, "idle": idle})
    if prev_t and total > prev_t:
        d_total = total - prev_t
        d_idle = idle - prev_i
        return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))
    return 0.0


def mem_info() -> Dict[str, float]:
    """内存（MB）：总数 / 已用 / 可用"""
    info: Dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            info[k.strip()] = float(rest.strip().split()[0]) / 1024.0
    except Exception:
        return {"total": 0, "used": 0, "available": 0, "percent": 0}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0.0, total - avail)
    return {"total": total, "used": used, "available": avail,
            "percent": (used / total * 100) if total else 0}


def disk_info(path: Path = Path("/")) -> Dict[str, float]:
    try:
        st = os.statvfs(str(path))
        total = st.f_frsize * st.f_blocks / 1024 / 1024 / 1024
        free = st.f_frsize * st.f_bavail / 1024 / 1024 / 1024
        used = total - free
        return {"total": round(total, 1), "used": round(used, 1),
                "free": round(free, 1),
                "percent": round(used / total * 100, 1) if total else 0}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0}


def uptime_text() -> str:
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return "-"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分钟"


def server_load() -> Dict[str, Any]:
    """服务器负载快照"""
    mem = mem_info()
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1
    pages = load_pages().get("pages") or {}
    up = uploads_stats()
    data_dir_size = 0
    for p in (PAGES_FILE, MEDIA_INDEX):
        try:
            data_dir_size += p.stat().st_size
        except OSError:
            pass
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": {"percent": round(cpu_percent(), 1), "count": cpu_count,
                "load1": round(load1, 2), "load5": round(load5, 2),
                "load15": round(load15, 2)},
        "mem": {k: round(v, 1) for k, v in mem.items()},
        "disk": disk_info(),
        "uptime": uptime_text(),
        "pages": len(pages),
        "uploads": {"count": up["count"],
                    "mb": round(up["bytes"] / 1024 / 1024, 2)},
        "data_mb": round(data_dir_size / 1024 / 1024, 2),
        "site": {"name": SITE_NAME, "port": WEB_PORT, "version": "1.1"},
    }


# ============================================================
# 后台：仪表盘 / 负载 / 媒体库
# ============================================================




def _pct_class(p: float) -> str:
    return "bad" if p >= 90 else ("warn" if p >= 70 else "ok")


def _metric(key: str, value: str, unit: str = "", sub: str = "",
            pct: Optional[float] = None) -> str:
    bar = ""
    if pct is not None:
        bar = (f'<div class="bar"><i class="{_pct_class(pct)}" '
               f'style="width:{min(100, max(2, pct)):.0f}%"></i></div>')
    return (f'<div class="metric"><div class="k">{_esc(key)}</div>'
            f'<div class="v">{_esc(value)}<span class="u">{_esc(unit)}</span></div>'
            f'{bar}<div class="s">{_esc(sub)}</div></div>')


def _admin_tabs(active: str) -> str:
    items = [("/admin", "总览", "home"), ("/admin/load", "服务器负载", "load"),
             ("/admin/media", "媒体库", "media"), ("/admin/pages", "词条管理", "pages"),
             ("/admin/cookies", "Cookie 分发", "cookies"), ("/admin/mail", "邮件", "mail")]
    return '<div class="tabs">' + "".join(
        f'<a class="{"on" if k == active else ""}" href="{href}">{label}</a>'
        for href, label, k in items) + "</div>"


def view_admin_home() -> str:
    load = server_load()
    up = uploads_stats()
    data = load_pages()
    pages = list((data.get("pages") or {}).items())
    recent = sorted(pages, key=lambda kv: -float(kv[1].get("updated", 0)))[:8]
    rows = "".join(
        f'<tr><td><a href="/p/{quote(n)}">{_esc(n)}</a></td>'
        f'<td>{_esc(",".join(p.get("tags") or []) or "-")}</td>'
        f'<td>{int(p.get("edits", 0))}</td>'
        f'<td>{datetime.fromtimestamp(float(p.get("updated", 0))).strftime("%m-%d %H:%M")}</td></tr>'
        for n, p in recent)
    inner = f"""
    <div class="admin-head"><h1>后台</h1><div class="t">星海百科 · 管理控制台</div></div>
    {_admin_tabs("home")}
    <div class="grid2">
      {_metric("词条总数", str(load['pages']), "个", "更新时间 " + datetime.now().strftime('%H:%M'))}
      {_metric("图片", str(up['count']), "张", f"占用 {up['bytes']/1024/1024:.1f} MB")}
      {_metric("CPU", f"{load['cpu']['percent']:.0f}", "%",
               f"{load['cpu']['count']} 核 · load {load['cpu']['load1']}", load['cpu']['percent'])}
      {_metric("内存", f"{load['mem']['used']:.0f}", "MB",
               f"共 {load['mem']['total']:.0f}MB · 可用 {load['mem']['available']:.0f}MB",
               load['mem']['percent'])}
      {_metric("磁盘", f"{load['disk']['used']}", "GB",
               f"共 {load['disk']['total']}GB · 剩 {load['disk']['free']}GB", load['disk']['percent'])}
      {_metric("运行时长", load['uptime'], "", "服务器连续运行")}
    </div>
    <h2 style="font-size:17px;margin:26px 0 12px">最近编辑</h2>
    <table class="t"><thead><tr><th>词条</th><th>标签</th><th>编辑次数</th><th>更新时间</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=4 style="color:var(--sub)">还没有词条</td></tr>'}</tbody></table>
    """
    return _layout("后台", inner)


def view_admin_load() -> str:
    s = server_load()
    cpu, mem, disk = s["cpu"], s["mem"], s["disk"]
    inner = f"""
    <div class="admin-head"><h1>服务器负载</h1>
      <div class="t">实时 · {s['time']}</div></div>
    {_admin_tabs("load")}
    <div class="grid2">
      {_metric("CPU 使用率", f"{cpu['percent']:.0f}", "%",
               f"{cpu['count']} 核 · 平均负载 {cpu['load1']} / {cpu['load5']} / {cpu['load15']}",
               cpu['percent'])}
      {_metric("内存", f"{mem['used']:.0f}", "MB",
               f"共 {mem['total']:.0f}MB · 可用 {mem['available']:.0f}MB", mem['percent'])}
      {_metric("磁盘", f"{disk['used']}", "GB",
               f"共 {disk['total']}GB · 剩余 {disk['free']}GB", disk['percent'])}
      {_metric("运行时长", s['uptime'], "", "服务器 uptime")}
      {_metric("数据占用", f"{s['data_mb']}", "MB", "词条 + 媒体索引")}
      {_metric("上传图片", f"{s['uploads']['mb']}", "MB", f"{s['uploads']['count']} 张")}
    </div>
    <h2 style="font-size:17px;margin:26px 0 10px">说明</h2>
    <div style="font-size:13px;color:var(--sub);line-height:2">
      · 数据直接读自 /proc/stat、/proc/meminfo、statvfs，不依赖 psutil<br>
      · 本页每 5 秒自动刷新<br>
      · 接口：<a href="/api/admin/load">/api/admin/load</a>（需登录）
    </div>
    <script>setTimeout(() => location.reload(), 5000);</script>
    """
    return _layout("服务器负载", inner)


def view_admin_media(msg: str = "", err: str = "") -> str:
    st = uploads_stats()
    notice = ""
    if msg:
        notice = f'<div class="notice">{_esc(msg)}</div>'
    if err:
        notice = f'<div class="notice warn">{_esc(err)}</div>'
    thumbs = "".join(
        f'<div class="thumb"><img src="{media_url(it["name"])}" loading="lazy">'
        f'<div class="m"><b>{_esc(it["name"])}</b>'
        f'{it["size"]/1024:.0f}KB · 用 {int(it.get("uses", 0))} 次'
        f'<form method="post" action="/admin/media/delete" style="margin-top:6px" '
        f'onsubmit="return confirm(\'删除这张图片？\')">'
        f'<input type="hidden" name="name" value="{_esc(it["name"])}">'
        f'<button class="mini no" style="width:100%">删除</button></form></div></div>'
        for it in st["items"])
    inner = f"""
    <div class="admin-head"><h1>媒体库</h1>
      <div class="t">{st['count']} 张 · 共 {st['bytes']/1024/1024:.1f}MB</div></div>
    {_admin_tabs("media")}
    {notice}
    <div class="drop" id="drop">
      <b>点击选择</b> 或把图片拖进来<br>
      <span style="font-size:12.5px">支持 png / jpg / gif / webp / svg，
        单张不超过 {MAX_UPLOAD_MB}MB</span>
      <input type="file" name="file" id="file" accept="image/*" hidden>
      <div id="pv"></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
      <button class="btn primary" id="up" disabled>上传</button>
      <span style="font-size:12.5px;color:var(--sub)" id="tip">选好图片后点上传</span>
    </div>
    <p style="font-size:12.5px;color:var(--sub);margin-top:12px">
      上传后可在编辑页写 <code>![说明](/media/文件名)</code> 插入
    </p>
    <div style="margin-top:16px;display:flex;justify-content:flex-end">
      <form method="post" action="/admin/media/clear"
        onsubmit="return confirm('确定清空媒体库？所有上传图片都会被删除')">
        <button class="mini no">清空媒体库</button>
      </form>
    </div>
    <div class="thumbs" style="margin-top:12px">
      {thumbs or '<div class="empty">还没有图片</div>'}
    </div>
    <script>
    const drop = document.getElementById('drop');
    const file = document.getElementById('file');
    const pv = document.getElementById('pv');
    const up = document.getElementById('up');
    const tip = document.getElementById('tip');
    const MAX = {MAX_UPLOAD_BYTES};
    function pick(f) {{
      if (!f) return;
      if (f.size > MAX) {{
        tip.textContent = '太大：' + (f.size/1048576).toFixed(1) + 'MB，上限 {MAX_UPLOAD_MB}MB';
        up.disabled = true; pv.innerHTML = ''; return;
      }}
      const dt = new DataTransfer(); dt.items.add(f); file.files = dt.files;
      up.disabled = false;
      tip.textContent = f.name + '　' + (f.size/1024).toFixed(0) + 'KB';
      pv.innerHTML = ''; const img = new Image();
      img.src = URL.createObjectURL(f); pv.appendChild(img);
    }}
    drop.addEventListener('click', e => {{ if (e.target !== file) file.click(); }});
    file.addEventListener('change', () => pick(file.files[0]));
    drop.addEventListener('dragover', e => {{ e.preventDefault(); drop.classList.add('over'); }});
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', e => {{
      e.preventDefault(); drop.classList.remove('over'); pick(e.dataTransfer.files[0]);
    }});
    up.addEventListener('click', () => {{
      if (!file.files.length) return;
      const fd = new FormData(); fd.append('file', file.files[0]);
      up.disabled = true; tip.textContent = '上传中…';
      fetch('/admin/upload', {{method:'POST', body: fd}})
        .then(r => r.text())
        .then(t => {{ tip.textContent = t; setTimeout(() => location.reload(), 600); }})
        .catch(e => {{ tip.textContent = '上传失败：' + e; up.disabled = false; }});
    }});
    </script>
    """
    return _layout("媒体库", inner)


def view_admin_pages() -> str:
    data = load_pages()
    pages = sorted((data.get("pages") or {}).items(),
                   key=lambda kv: -float(kv[1].get("updated", 0)))
    rows = "".join(
        f'<tr><td><a href="/p/{quote(n)}">{_esc(n)}</a></td>'
        f'<td>{_esc(",".join(p.get("tags") or []) or "-")}</td>'
        f'<td>{len(str(p.get("body") or ""))}</td>'
        f'<td>{int(p.get("edits", 0))}</td>'
        f'<td>{datetime.fromtimestamp(float(p.get("updated", 0))).strftime("%Y-%m-%d %H:%M")}</td>'
        f'<td class="row-actions"><a class="mini" href="/edit/{quote(n)}">编辑</a>'
        f'<a class="mini" href="/history/{quote(n)}">历史</a>'
        f'<form method="post" action="/admin/pages/delete" '
        f'onsubmit="return confirm(\'确定删除词条「{_js(n)}」？\\n删除后不可恢复（建议先导出备份）\')">'
        f'<input type="hidden" name="name" value="{_esc(n)}">'
        f'<button class="mini no">删除</button></form></td></tr>'
        for n, p in pages)
    inner = f"""
    <div class="admin-head"><h1>词条管理</h1><div class="t">{len(pages)} 个词条</div></div>
    {_admin_tabs("pages")}
    <div style="margin-bottom:14px;display:flex;gap:10px;align-items:center">
      <a class="btn primary" href="/new">＋ 新建词条</a>
      <a class="btn" href="/admin/pages/deleted">🗑 回收站</a>
      <form method="post" action="/admin/pages/clear" style="margin-left:auto"
        onsubmit="return confirm('确定清空所有词条？会先放进回收站')">
        <button class="mini no">清空所有词条</button>
      </form>
    </div>
    <table class="t"><thead><tr><th>标题</th><th>标签</th><th>字数</th>
    <th>编辑</th><th>更新时间</th><th>操作</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=6 style="color:var(--sub)">还没有词条</td></tr>'}</tbody></table>
    """
    return _layout("词条管理", inner)


# ============================================================
# 账号相关页面：注册 / 登录 / 申请 Cookie / 审核台
# ============================================================

AUTH_CSS = """
.auth{max-width:430px;margin:26px auto 0}
.auth h1{font-size:26px;margin:0 0 6px;text-align:center}
.auth .sub2{text-align:center;font-size:13px;color:var(--sub);margin-bottom:22px}
.auth .field{margin-bottom:15px}
.auth label{display:block;font-size:12.5px;color:var(--sub);margin-bottom:6px}
.auth input{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel);color:var(--ink);font-size:15px;outline:none}
.auth input:focus{border-color:var(--accent)}
.auth button{width:100%;padding:12px;border:none;border-radius:9px;
  background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.auth .foot2{text-align:center;font-size:13px;color:var(--sub);margin-top:18px;line-height:2}
.auth .cap{display:flex;align-items:center;gap:10px;margin-bottom:15px}
.auth .cap .q{flex:1;background:var(--soft);border-radius:9px;padding:11px 14px;
  font-size:16px;font-weight:700;letter-spacing:1px;text-align:center;
  font-family:ui-monospace,Menlo,monospace}
.auth .cap input{width:110px;text-align:center}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11.5px;
  background:var(--soft);color:var(--accent);margin-left:8px}
.badge.owner{background:#fde8c8;color:#a05a00}
.badge.editor{background:#dff0e2;color:#1f6b3a}
.row-actions{display:flex;gap:6px}
.row-actions form{display:inline}
.mini{padding:5px 11px;font-size:12.5px;border-radius:7px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);cursor:pointer}
.mini.ok{border-color:#1f6b3a;color:#1f6b3a}
.mini.no{border-color:#b4573c;color:#b4573c}
.gear{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
  word-break:break-all;background:var(--soft);padding:2px 7px;border-radius:5px}
"""


def view_register(msg: str = "", err: str = "") -> str:
    q, tok = new_captcha()
    notice = f'<div class="notice">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="notice warn">{_esc(err)}</div>' if err else ""
    inner = f"""
    <div class="auth">
      <h1>注册账号</h1>
      <div class="sub2">注册后可申请 Cookie，拿到 Cookie 才能编辑百科</div>
      {notice}{warn}
      <form method="post" action="/register">
        <div class="field"><label>用户名（3~20 位，字母/数字/下划线）</label>
          <input name="username" required minlength="3" maxlength="20" autocomplete="username"></div>
        <div class="field"><label>邮箱（用于接收验证码）</label>
          <input name="email" type="email" required autocomplete="email"></div>
        <div class="field"><label>密码（至少 8 位，不能纯数字）</label>
          <input name="password" type="password" required minlength="8" autocomplete="new-password"></div>
        <div class="field"><label>确认密码</label>
          <input name="password2" type="password" required minlength="8" autocomplete="new-password"></div>
        <div class="cap">
          <div class="q">{_esc(q)}</div>
          <input name="captcha" required placeholder="答案" inputmode="numeric">
          <input type="hidden" name="ctoken" value="{_esc(tok)}">
        </div>
        <button type="submit">注册</button>
      </form>
      <div class="foot2">已经有账号？<a href="/login">去登录</a></div>
    </div>"""
    return _layout("注册", inner)


def view_login(msg: str = "", err: str = "") -> str:
    notice = f'<div class="notice">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="notice warn">{_esc(err)}</div>' if err else ""
    inner = f"""
    <div class="auth">
      <h1>登录</h1>
      <div class="sub2">用注册时的账号密码登录；也可以直接用站长发的 Cookie</div>
      {notice}{warn}
      <form method="post" action="/login" autocomplete="on">
        <div class="field"><label>用户名 或 邮箱</label>
          <input name="account" required autocomplete="username"
                 placeholder="注册时填的用户名 / 邮箱"></div>
        <div class="field"><label>密码</label>
          <input name="password" type="password" required autocomplete="current-password"
                 placeholder="至少 8 位"></div>
        <button type="submit">登录</button>
      </form>
      <div style="display:flex;align-items:center;gap:10px;margin:22px 0">
        <div style="flex:1;height:1px;background:var(--line)"></div>
        <span style="font-size:12px;color:var(--sub)">或者已经有 Cookie</span>
        <div style="flex:1;height:1px;background:var(--line)"></div>
      </div>
      <form method="post" action="/login-cookie">
        <div class="field"><label>Cookie 令牌（站长审核通过后发给你）</label>
          <input name="cookie" placeholder="粘贴 Cookie 令牌"></div>
        <button type="submit" style="background:var(--panel);color:var(--ink);
          border:1px solid var(--line)">用 Cookie 登录</button>
      </form>
      <div class="foot2">
        还没有账号？<a href="/register">注册一个</a><br>
        注册了但还没权限？<a href="/apply">申请 Cookie</a>
      </div>
    </div>"""
    return _layout("登录", inner)


def view_apply() -> str:
    user = CUR_USER["v"]
    if not user:
        return view_login(err="请先登录再申请")
    role = effective_role(user)
    req = user.get("cookie_request") or {}
    status = req.get("status") or ""
    body = ""
    if role in (ROLE_OWNER, ROLE_EDITOR):
        cookie = (user.get("cookie") or {}).get("token", "")
        body = (f'<div class="notice">你已经有编辑权限了（{ROLE_NAMES[role]}）。'
                + (f'<br>你的 Cookie：<span class="gear">{_esc(cookie)}</span>' if cookie else "")
                + "</div>")
    elif status == "pending":
        body = ('<div class="notice">申请已提交，等站长（%s）审核，通过后会给你发 Cookie。</div>'
                % OWNER_ID)
    elif status == "rejected":
        body = '<div class="notice warn">上次申请被拒绝了，可以补充理由再申请一次。</div>'
    if role not in (ROLE_OWNER, ROLE_EDITOR) and status != "pending":
        body += """
        <form method="post" action="/apply">
          <div class="field"><label>申请理由（说说你想写什么内容）</label>
            <textarea name="reason" required></textarea></div>
          <button type="submit">提交申请</button>
        </form>"""
    inner = f"""
    <div class="auth" style="max-width:520px">
      <h1>申请 Cookie</h1>
      <div class="sub2">Cookie 由站长审核发放，拿到后才能编辑词条</div>
      {body}
      <div class="foot2"><a href="/">返回首页</a></div>
    </div>"""
    return _layout("申请 Cookie", inner)


def view_admin_cookies(msg: str = "", err: str = "") -> str:
    pending = pending_cookie_requests()
    users = list((load_users().get("users") or {}).values())
    users.sort(key=lambda u: -float(u.get("created", 0)))
    notice = f'<div class="notice">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="notice warn">{_esc(err)}</div>' if err else ""
    rows = ""
    for it in pending:
        rows += (f'<tr><td>{_esc(it["username"])}</td><td>{_esc(it["email"])}</td>'
                 f'<td style="max-width:280px">{_esc(it["reason"] or "-")}</td>'
                 f'<td>{datetime.fromtimestamp(float(it["time"])).strftime("%m-%d %H:%M")}</td>'
                 f'<td class="row-actions">'
                 f'<form method="post" action="/admin/cookie">'
                 f'<input type="hidden" name="uid" value="{_esc(it["uid"])}">'
                 f'<input type="hidden" name="action" value="approve">'
                 f'<button class="mini ok">批准</button></form>'
                 f'<form method="post" action="/admin/cookie">'
                 f'<input type="hidden" name="uid" value="{_esc(it["uid"])}">'
                 f'<input type="hidden" name="action" value="reject">'
                 f'<button class="mini no">拒绝</button></form></td></tr>')
    urows = ""
    for u in users[:50]:
        role = effective_role(u)
        cookie = (u.get("cookie") or {}).get("token", "")
        ck = ('<span class="gear">' + _esc(cookie[:14]) + '…</span>') if cookie else "-"
        uid_v = _esc(u.get("uid"))
        if role == ROLE_OWNER:
            # 站长这一行不给"发放/收回"按钮（避免误点把自己的权限降级）
            acts = ('<span style="font-size:12px;color:var(--sub)">'
                    '站长账号（角色不可改）</span>')
        else:
            acts = (f'<form method="post" action="/admin/cookie">'
                    f'<input type="hidden" name="uid" value="{uid_v}">'
                    f'<input type="hidden" name="action" value="grant">'
                    f'<button class="mini ok">直接发放</button></form>'
                    f'<form method="post" action="/admin/cookie">'
                    f'<input type="hidden" name="uid" value="{uid_v}">'
                    f'<input type="hidden" name="action" value="revoke">'
                    f'<button class="mini no">收回</button></form>')
            if role != ROLE_EDITOR:
                acts += (f'<form method="post" action="/admin/cookie">'
                         f'<input type="hidden" name="uid" value="{uid_v}">'
                         f'<input type="hidden" name="action" value="set_editor">'
                         f'<button class="mini">设为编辑</button></form>')
            if role != ROLE_GUEST:
                acts += (f'<form method="post" action="/admin/cookie">'
                         f'<input type="hidden" name="uid" value="{uid_v}">'
                         f'<input type="hidden" name="action" value="set_guest">'
                         f'<button class="mini">降为访客</button></form>')
            acts += (f'<form method="post" action="/admin/cookie" '
                     f'onsubmit="return confirm(\'删除账号 {_js(u.get("username"))}？'
                     f'该账号会话会立即失效，不可恢复\')">'
                     f'<input type="hidden" name="uid" value="{uid_v}">'
                     f'<input type="hidden" name="action" value="delete">'
                     f'<button class="mini no">删除账号</button></form>')
        urows += (f'<tr><td>{_esc(u.get("username"))}'
                  f'<span class="badge {role}">{ROLE_NAMES.get(role, role)}</span></td>'
                  f'<td>{_esc(u.get("email"))}</td>'
                  f'<td>{"已验证" if u.get("email_verified") else "未验证"}</td>'
                  f'<td>{ck}</td>'
                  f'<td>{datetime.fromtimestamp(float(u.get("created", 0))).strftime("%m-%d %H:%M")}</td>'
                  f'<td class="row-actions">{acts}</td></tr>')
    inner = f"""
    <div class="admin-head"><h1>Cookie 分发</h1>
      <div class="t">审核人：{OWNER_ID} · 待审 {len(pending)} 条</div></div>
    {_admin_tabs("cookies")}
    {notice}{warn}
    <h2 style="font-size:16px;margin:0 0 10px">待审核申请</h2>
    <table class="t"><thead><tr><th>用户名</th><th>邮箱</th><th>理由</th><th>时间</th><th>操作</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=5 style="color:var(--sub)">没有待审申请</td></tr>'}</tbody></table>
    <h2 style="font-size:16px;margin:26px 0 10px">全部账号</h2>
    <table class="t"><thead><tr><th>用户名</th><th>邮箱</th><th>邮箱验证</th><th>Cookie</th>
    <th>注册时间</th><th>操作</th></tr></thead>
    <tbody>{urows or '<tr><td colspan=6 style="color:var(--sub)">还没有注册用户</td></tr>'}</tbody></table>
    """
    return _layout("Cookie 分发", inner)


# ============================================================
# 应用与路由
# ============================================================

app = FastAPI(title=SITE_NAME, docs_url=None, redoc_url=None)

# ============================================================
# 账号体系：注册 / 登录 / 邮箱验证码 / 人机验证 / Cookie 分发审核
# ============================================================
# 角色（权限从高到低）：
#   owner  站长：审批 Cookie 分发、封禁用户
#   editor 编辑：可新建/编辑词条、上传图片
#   guest  访客：只能看，需要 Cookie 才能升级为编辑
#
# Cookie 分发流程（即"Cookie 分发交给站长审核"）：
#   注册 → 邮箱验证码验证 → 申请 Cookie（填理由）
#   → 站长后台「Cookie 分发」页批准 → 申请者拿到 Cookie 令牌
#   → 用令牌登录获得 editor 权限

import hmac
import random
import threading
from email.message import EmailMessage

USERS_FILE = DATA_DIR / "users.json"
SESSION_FILE = DATA_DIR / "sessions.json"
MAIL_DIR = DATA_DIR / "mail_outbox"
MAIL_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_NAME = "wiki_session"
LEGACY_COOKIE = "wiki_token"      # 旧版编辑口令 cookie（站长快捷登录）
COOKIE_MAX_AGE = 7 * 86400
# 站长 ID：从 wiki_data/config.json 的 "owner" 读取；
# 留空时，第一个通过邮箱验证注册的账号会自动成为站长。
OWNER_ID = (load_config().get("owner") or "") if "load_config" in globals() else ""
ROLE_OWNER, ROLE_EDITOR, ROLE_GUEST = "owner", "editor", "guest"
ROLE_NAMES = {ROLE_OWNER: "站长", ROLE_EDITOR: "编辑", ROLE_GUEST: "访客"}

CODE_TTL = 600
CODE_MAX_TRIES = 5
CODE_RESEND_GAP = 60
CODE_LEN = 6
CAPTCHA_TTL = 300

_LOCK = threading.RLock()
_CAPTCHA: Dict[str, Dict[str, Any]] = {}
_MAIL: Dict[str, Any] = {"last": None, "error": ""}


def _load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception as e:
        logger.warning(f"[wiki] 读取 {path.name} 失败: {e}")
        return default


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.error(f"[wiki] 保存 {path.name} 失败: {e}")



# 登录失败限流：同一 IP 10 分钟内失败 8 次就锁 15 分钟
_LOGIN_FAIL: Dict[str, Dict[str, float]] = {}
LOGIN_MAX_FAIL = 8
LOGIN_FAIL_WINDOW = 600
LOGIN_LOCK_SEC = 900


def login_blocked(ip: str) -> int:
    """返回剩余锁定秒数，0 表示可继续尝试"""
    rec = _LOGIN_FAIL.get(ip or "")
    if not rec:
        return 0
    now = _now()
    if rec.get("lock_until", 0) > now:
        return int(rec["lock_until"] - now)
    if now - rec.get("first", 0) > LOGIN_FAIL_WINDOW:
        _LOGIN_FAIL.pop(ip or "", None)
    return 0


def login_fail(ip: str) -> None:
    now = _now()
    rec = _LOGIN_FAIL.setdefault(ip or "", {"count": 0, "first": now, "lock_until": 0})
    if now - rec["first"] > LOGIN_FAIL_WINDOW:
        rec.update({"count": 0, "first": now})
    rec["count"] = int(rec.get("count", 0)) + 1
    if rec["count"] >= LOGIN_MAX_FAIL:
        rec["lock_until"] = now + LOGIN_LOCK_SEC


def login_ok(ip: str) -> None:
    _LOGIN_FAIL.pop(ip or "", None)


def load_users() -> Dict[str, Any]:
    data = _load_json(USERS_FILE, {"users": {}})
    data.setdefault("users", {})
    return data


def save_users(data: Dict[str, Any]) -> None:
    _save_json(USERS_FILE, data)


def load_sessions() -> Dict[str, Any]:
    data = _load_json(SESSION_FILE, {"sessions": {}})
    data.setdefault("sessions", {})
    return data


def save_sessions(data: Dict[str, Any]) -> None:
    _save_json(SESSION_FILE, data)


def find_user(key: str) -> Optional[Dict[str, Any]]:
    key = (key or "").strip().lower()
    if not key:
        return None
    for u in (load_users().get("users") or {}).values():
        if str(u.get("username", "")).lower() == key or \
                str(u.get("email", "")).lower() == key:
            return u
    return None


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                             salt.encode("utf-8"), 120_000)
    return dk.hex(), salt


def verify_password(password: str, record: Dict[str, Any]) -> bool:
    salt = str(record.get("salt") or "")
    expect = str(record.get("password") or "")
    if not salt or not expect:
        return False
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, expect)


def password_problem(password: str) -> str:
    if len(password or "") < 8:
        return "密码至少 8 位"
    if password.isdigit():
        return "密码不能是纯数字"
    return ""


def _smtp_config() -> Dict[str, str]:
    return {k: str(v) for k, v in (load_config().get("smtp") or {}).items()}


def render_code_mail(to: str, code: str, username: str) -> str:
    return (f"【{SITE_NAME}】注册验证码\n\n"
            f"{username} 你好，你的验证码是：{code}\n"
            f"有效期 {CODE_TTL // 60} 分钟，请勿转告他人。\n\n"
            f"如果不是你本人操作，忽略本邮件即可。\n{SITE_NAME} {PUBLIC_URL}")


def send_code_email(to: str, code: str, username: str = "") -> Tuple[bool, str]:
    """发验证码邮件：配了 SMTP 就真发，没配就落本地 outbox（后台可查）"""
    text = render_code_mail(to, code, username)
    smtp = _smtp_config()
    if smtp.get("host"):
        try:
            import smtplib
            msg = EmailMessage()
            msg["Subject"] = f"【{SITE_NAME}】注册验证码"
            msg["From"] = smtp.get("from") or smtp.get("user", "")
            msg["To"] = to
            msg.set_content(text)
            use_ssl = smtp.get("ssl") in ("1", "true", "yes")
            port = int(smtp.get("port") or (465 if use_ssl else 587))
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp["host"], port, timeout=15)
            else:
                server = smtplib.SMTP(smtp["host"], port, timeout=15)
                try:
                    server.starttls()
                except Exception:
                    pass
            if smtp.get("user"):
                server.login(smtp["user"], smtp.get("password", ""))
            server.send_message(msg)
            server.quit()
            _MAIL.update({"last": _now(), "error": ""})
            return True, f"验证码已发到 {to}"
        except Exception as e:
            logger.warning(f"[wiki] 发邮件失败: {e}")
            _MAIL.update({"last": _now(), "error": str(e)[:200]})
    try:
        fn = MAIL_DIR / f"{int(_now())}_{secrets.token_hex(3)}.txt"
        fn.write_text(f"To: {to}\n\n{text}\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"[wiki] 写 outbox 失败: {e}")
    return True, "验证码已生成（未配置 SMTP，可让站长在后台「邮件」里查看）"


def new_email_code() -> str:
    return "".join(random.choice("0123456789") for _ in range(CODE_LEN))


def new_captcha() -> Tuple[str, str]:
    a, b = random.randint(6, 19), random.randint(3, 12)
    op = random.choice(["+", "-", "×"])
    if op == "+":
        ans = a + b
    elif op == "-":
        a, b = max(a, b), min(a, b)
        ans = a - b
    else:
        a, b = random.randint(3, 9), random.randint(2, 9)
        ans = a * b
    token = secrets.token_urlsafe(10)
    _CAPTCHA[token] = {"ans": ans, "time": _now()}
    for k in [k for k, v in _CAPTCHA.items() if _now() - v["time"] > CAPTCHA_TTL]:
        _CAPTCHA.pop(k, None)
    return f"{a} {op} {b} = ?", token


def check_captcha(token: str, answer: str) -> Tuple[bool, str]:
    item = _CAPTCHA.pop((token or "").strip(), None)
    if not item:
        return False, "人机验证已过期，请刷新重试"
    if _now() - item["time"] > CAPTCHA_TTL:
        return False, "人机验证已过期，请刷新重试"
    try:
        if int(str(answer).strip()) != int(item["ans"]):
            return False, "计算结果不对"
    except Exception:
        return False, "请填写计算结果"
    return True, ""


def new_session(user: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    with _LOCK:
        data = load_sessions()
        data["sessions"][token] = {"uid": user["uid"],
                                   "username": user.get("username", ""),
                                   "time": _now()}
        save_sessions(data)
    return token


def session_user(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    sess = (load_sessions().get("sessions") or {}).get(token)
    if not sess:
        return None
    if _now() - float(sess.get("time", 0)) > COOKIE_MAX_AGE:
        return None
    uid = sess.get("uid")
    for u in (load_users().get("users") or {}).values():
        if u.get("uid") == uid:
            return None if u.get("status") == "banned" else u
    return None


def revoke_session(token: str) -> None:
    with _LOCK:
        data = load_sessions()
        data["sessions"].pop(token, None)
        save_sessions(data)


def request_cookie(user: Dict[str, Any], reason: str) -> Tuple[bool, str]:
    users = load_users()
    rec = users["users"].get(user["uid"])
    if not rec:
        return False, "用户不存在"
    if not rec.get("email_verified"):
        return False, "请先完成邮箱验证"
    cur = rec.get("cookie_request") or {}
    if cur.get("status") == "pending":
        return False, "你已经提交过申请，等站长审核中"
    if rec.get("role") in (ROLE_OWNER, ROLE_EDITOR):
        return False, "你已经有编辑权限了"
    rec["cookie_request"] = {"status": "pending", "reason": (reason or "")[:300],
                             "time": _now()}
    save_users(users)
    logger.info(f"[wiki] Cookie 申请：{rec.get('username')}（{rec.get('email')}）")
    return True, "申请已提交，等站长审核"


def review_cookie(uid: str, approve: bool, reviewer: str = OWNER_ID) -> Tuple[bool, str]:
    users = load_users()
    rec = users["users"].get(uid)
    if not rec:
        return False, "用户不存在"
    req = rec.get("cookie_request") or {}
    if req.get("status") != "pending":
        return False, "该申请不在待审核状态"
    if approve:
        rec["role"] = ROLE_EDITOR
        cookie = secrets.token_urlsafe(24)
        rec["cookie"] = {"token": cookie, "issued": _now(), "by": reviewer}
        rec["cookie_request"] = {**req, "status": "approved", "time": _now()}
        save_users(users)
        logger.info(f"[wiki] Cookie 已批准：{rec.get('username')}（审核人 {reviewer}）")
        return True, f"已批准，Cookie：{cookie}"
    rec["cookie_request"] = {**req, "status": "rejected", "time": _now()}
    save_users(users)
    logger.info(f"[wiki] Cookie 被拒绝：{rec.get('username')}")
    return True, "已拒绝该申请"


def live_cookie(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    for u in (load_users().get("users") or {}).values():
        c = u.get("cookie") or {}
        if c.get("token") and hmac.compare_digest(str(c["token"]), str(token)):
            return None if u.get("status") == "banned" else u
    return None


def pending_cookie_requests() -> List[Dict[str, Any]]:
    out = []
    for u in (load_users().get("users") or {}).values():
        req = u.get("cookie_request") or {}
        if req.get("status") == "pending":
            out.append({"uid": u["uid"], "username": u.get("username"),
                        "email": u.get("email"), "reason": req.get("reason", ""),
                        "time": req.get("time", 0)})
    return sorted(out, key=lambda x: -float(x.get("time", 0)))


def latest_mail() -> str:
    try:
        files = sorted(MAIL_DIR.glob("*.txt"), key=lambda p: -p.stat().st_mtime)
        return files[0].read_text(encoding="utf-8") if files else "（暂无邮件）"
    except Exception as e:
        return f"（读取失败：{e}）"


IS_EDITOR = {"v": False}      # 兼容旧口令的站长快捷登录
CUR_USER: Dict[str, Any] = {"v": None}     # 当前请求的登录用户（中间件填充）


def current_token() -> str:
    """取编辑口令（站长快捷登录用；没初始化就现场读配置）"""
    global CONFIG, EDIT_TOKEN
    if not EDIT_TOKEN:
        if not CONFIG:
            try:
                CONFIG = load_config()
            except Exception as e:
                logger.warning(f"[wiki] 读取配置失败: {e}")
                return ""
        EDIT_TOKEN = str(CONFIG.get("token") or "")
    return EDIT_TOKEN


def _check_token(token: str) -> bool:
    expect = current_token()
    return bool(token) and bool(expect) and secrets.compare_digest(str(token), expect)


def effective_role(user: Optional[Dict[str, Any]]) -> str:
    """算用户角色：站长/编辑/访客"""
    if not user:
        return ROLE_GUEST
    role = str(user.get("role") or ROLE_GUEST)
    if role not in ROLE_NAMES:
        role = ROLE_GUEST
    # 老账号没有角色但有 Cookie 令牌 → 视为编辑
    if role == ROLE_GUEST and (user.get("cookie") or {}).get("token"):
        role = ROLE_EDITOR
    return role


def is_owner(user: Optional[Dict[str, Any]]) -> bool:
    return bool(user) and effective_role(user) == ROLE_OWNER


def can_edit(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    return effective_role(user) in (ROLE_OWNER, ROLE_EDITOR)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """每个请求解析一次身份：会话 Cookie → 用户；同时兼容旧口令"""
    token = request.cookies.get(COOKIE_NAME, "")
    user = session_user(token)
    if user is None:
        # 兼容旧版编辑口令 cookie（站长快捷登录）
        legacy = request.cookies.get(LEGACY_COOKIE, "")
        if legacy and _check_token(legacy):
            IS_EDITOR["v"] = True
            CUR_USER["v"] = {"uid": OWNER_ID, "username": OWNER_ID,
                             "role": ROLE_OWNER, "email": "", "legacy": True}
            resp = await call_next(request)
            resp.headers.update(SECURITY_HEADERS)
            return resp
    IS_EDITOR["v"] = False
    CUR_USER["v"] = user
    resp = await call_next(request)
    resp.headers.update(SECURITY_HEADERS)
    return resp


def _require_editor() -> Optional[HTMLResponse]:
    """需要编辑权限（站长或已获 Cookie 的编辑）"""
    user = CUR_USER["v"]
    if can_edit(user):
        return None
    if not user:
        notice = ('<div class="notice warn">请先登录，并且拿到站长发放的 Cookie 才能编辑。'
                  '<a href="/login">去登录</a> · <a href="/register">注册</a></div>')
    else:
        notice = ('<div class="notice warn">你的账号还没有编辑权限（Cookie）。'
                  '请在 <a href="/apply">申请 Cookie</a>，由站长审核发放。</div>')
    return HTMLResponse(_layout("需要权限", notice + "<h1>需要编辑权限</h1>", "new"),
                        status_code=403)


def _require_owner() -> Optional[HTMLResponse]:
    """需要站长权限（Cookie 分发审核只有站长能做）"""
    user = CUR_USER["v"]
    if is_owner(user):
        return None
    notice = ('<div class="notice warn">这个页面只有站长（%s）能进。'
              '<a href="/login">换个账号登录</a></div>' % OWNER_ID)
    return HTMLResponse(_layout("仅站长", notice + "<h1>仅站长可见</h1>"), status_code=403)


@app.get("/", response_class=HTMLResponse)
async def route_home() -> HTMLResponse:
    return HTMLResponse(view_home())


@app.get("/all", response_class=HTMLResponse)
async def route_all() -> HTMLResponse:
    return HTMLResponse(view_all())


@app.get("/recent", response_class=HTMLResponse)
async def route_recent() -> HTMLResponse:
    return HTMLResponse(view_recent())


@app.get("/tags", response_class=HTMLResponse)
async def route_tags() -> HTMLResponse:
    return HTMLResponse(view_tags())


@app.get("/tag/{tag}", response_class=HTMLResponse)
async def route_tag(tag: str) -> HTMLResponse:
    return HTMLResponse(view_tag(unquote(tag)))


@app.get("/p/{name}", response_class=HTMLResponse)
async def route_page(name: str) -> HTMLResponse:
    return HTMLResponse(view_page(unquote(name)))


@app.get("/search", response_class=HTMLResponse)
async def route_search(q: str = "") -> HTMLResponse:
    return HTMLResponse(view_search(q))


@app.get("/random")
async def route_random() -> RedirectResponse:
    pages = list((load_pages().get("pages") or {}).keys())
    if not pages:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/p/" + quote(secrets.choice(pages)), status_code=303)


@app.get("/new", response_class=HTMLResponse)
async def route_new(title: str = "") -> HTMLResponse:
    blocked = _require_editor()
    if blocked:
        return blocked
    return HTMLResponse(view_new(title))


@app.post("/new", response_class=HTMLResponse)
async def route_new_post(title: str = Form(...), tags: str = Form(""),
                         body: str = Form(...)) -> HTMLResponse:
    blocked = _require_editor()
    if blocked:
        return blocked
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        return HTMLResponse(_layout("新建", '<div class="notice warn">标题不能为空</div>'
                                    + view_new()), status_code=400)
    data = load_pages()
    pages = data.setdefault("pages", {})
    if title in pages:
        return RedirectResponse("/edit/" + quote(title), status_code=303)
    tag_list = [t.strip() for t in re.split(r"[,，\s]+", tags or "") if t.strip()][:8]
    pages[title] = {
        "body": (body or "")[:MAX_BODY], "tags": tag_list,
        "created": _now(), "updated": _now(), "edits": 1,
        "history": [], "author": "web",
    }
    save_pages(data)
    logger.info(f"[wiki] 新建词条：{title}")
    return RedirectResponse("/p/" + quote(title), status_code=303)


@app.get("/edit/{name}", response_class=HTMLResponse)
async def route_edit(name: str) -> HTMLResponse:
    blocked = _require_editor()
    if blocked:
        return blocked
    return HTMLResponse(view_edit(unquote(name)))


@app.post("/edit/{name}", response_class=HTMLResponse)
async def route_edit_post(name: str, tags: str = Form(""),
                          body: str = Form(...)) -> HTMLResponse:
    blocked = _require_editor()
    if blocked:
        return blocked
    name = unquote(name)
    data = load_pages()
    page = (data.get("pages") or {}).get(name)
    if not page:
        return HTMLResponse(view_page(name), status_code=404)
    old = str(page.get("body") or "")
    if old:
        page.setdefault("history", []).append(
            {"time": _now(), "by": "web", "body": old[:4000]})
        page["history"] = page["history"][-20:]
    page["body"] = (body or "")[:MAX_BODY]
    page["tags"] = [t.strip() for t in re.split(r"[,，\s]+", tags or "") if t.strip()][:8]
    page["updated"] = _now()
    page["edits"] = int(page.get("edits", 0)) + 1
    save_pages(data)
    logger.info(f"[wiki] 编辑词条：{name}")
    return RedirectResponse("/p/" + quote(name), status_code=303)


@app.get("/history/{name}", response_class=HTMLResponse)
async def route_history(name: str) -> HTMLResponse:
    return HTMLResponse(view_history(unquote(name)))


@app.get("/login", response_class=HTMLResponse)
async def route_login_page_q(token: str = "", msg: str = "", err: str = "") -> HTMLResponse:
    """GET /login：既处理「旧编辑口令快捷登录」，也显示登录页

    旧实现把口令直接写进 wiki_session cookie，但中间件是按"用户会话"解析的，
    所以口令 cookie 进不了后台 —— 现在改成走独立的 wiki_token cookie（站长快捷登录）。
    """
    if token and _check_token(token):
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(LEGACY_COOKIE, token, max_age=30 * 86400,
                        httponly=True, samesite="lax")
        logger.info("[wiki] 站长口令快捷登录")
        return resp
    return HTMLResponse(view_login(msg, err))


def release_zip() -> Optional[Path]:
    """发布包位置：先看配置里的 release_zip，再找几个常见目录"""
    here = Path(__file__).resolve().parent
    cands = []
    cfg_name = str(load_config().get("release_zip") or "").strip()
    if cfg_name:
        cands.append(Path(cfg_name))
        cands.append(here / cfg_name)
    cands += [here.parent.parent / "WIKI.HS.zip", here / "WIKI.HS.zip",
              DATA_DIR / "WIKI.HS.zip"]
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return c
        except Exception:
            continue
    return None


@app.get("/download/WIKI.HS.zip")
async def route_download_wiki():
    """发布包下载（放在站点上方便直接点按钮下载）"""
    p = release_zip()
    if p is None:
        return PlainTextResponse("发布包不存在", status_code=404)
    return FileResponse(str(p), media_type="application/zip",
                        filename="WIKI.HS.zip",
                        headers={"Content-Disposition": 'attachment; filename="WIKI.HS.zip"'})


@app.get("/download", response_class=HTMLResponse)
async def route_download_page() -> HTMLResponse:
    """下载页（带一个大按钮）"""
    p = release_zip()
    size = (p.stat().st_size / 1024) if p else 0
    import hashlib
    md5 = ""
    if p.exists():
        try:
            md5 = hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            md5 = ""
    box = ("background:var(--panel);border:1px solid var(--line);"
           "border-radius:12px;padding:18px 22px;font-size:13.5px;line-height:2;"
           "text-align:left;margin-top:16px")
    inner = f"""
    <div style="max-width:640px;margin:34px auto">
      <div style="text-align:center">
        <h1 style="font-size:30px;margin-bottom:6px">📦 WIKI.HS</h1>
        <p style="color:var(--sub);margin:0 0 6px">星海百科 · 群知识库网站</p>
        <p style="color:var(--accent);font-weight:600;font-size:15px">
          🎁 公益免费 · 可自由使用与二创</p>
        <div style="margin:22px 0 8px">
          <a href="/download/WIKI.HS.zip"
             style="display:inline-block;padding:16px 46px;background:var(--accent);
                    color:#fff;font-size:18px;font-weight:600;border-radius:12px;
                    text-decoration:none;box-shadow:0 4px 14px rgba(47,111,79,.28)">
            ⬇ 下载 WIKI.HS.zip</a>
        </div>
        <p style="color:var(--sub);font-size:12.5px">
          {size:.0f} KB　版本 1.1　MD5：<code>{md5}</code></p>
      </div>

      <div style="{box}">
        <b>🚀 一键部署（Linux 服务器）</b><br>
        <code>sudo bash deploy.sh</code>　约 1 分钟装完，自动开机自启<br>
        <span style="color:var(--sub)">可选参数：--port 9000 / --domain wiki.xxx.com（自动 HTTPS）</span>
      </div>

      <div style="{box}">
        <b>🧩 装进现有机器人</b><br>
        1. 解压，把 <code>WIKI.HS</code> 里所有文件放进 <code>机器人/src/plugins/wiki/</code><br>
        2. 重启机器人，看日志打印的地址和口令<br>
        3. 打开 <code>/register</code> 注册第一个账号 → 自动成为站长
      </div>

      <div style="{box}">
        <b>✨ 包含什么</b><br>
        · 服务端渲染 WIKI，零前端依赖、断网可用<br>
        · 后台：仪表盘 / 服务器负载 / 媒体库 / 词条管理 / Cookie 分发 / 邮件<br>
        · 账号体系：注册 + 邮箱验证码 + 人机验证，三级权限<br>
        · 图片上传（5MB 上限，校验文件头）<br>
        · 删除有回收站，误删可恢复
      </div>

      <div style="text-align:center;color:var(--sub);font-size:12.5px;margin-top:22px">
        用爱发电 · 如果帮到你了，留个 Star 或转给需要的朋友就够啦 💚
      </div>
    </div>"""
    return HTMLResponse(_layout("下载", inner))


@app.get("/health", response_class=JSONResponse)
async def route_health() -> JSONResponse:
    data = load_pages()
    return JSONResponse({"ok": True, "site": SITE_NAME, "version": "1.0",
                         "pages": len(data.get("pages") or {})})


@app.get("/api/pages", response_class=JSONResponse)
async def route_api_pages() -> JSONResponse:
    data = load_pages()
    return JSONResponse({"pages": [{"title": n, "updated": p.get("updated"),
                                    "tags": p.get("tags") or []}
                                   for n, p in (data.get("pages") or {}).items()]})


@app.get("/api/page/{name}", response_class=JSONResponse)
async def route_api_page(name: str) -> JSONResponse:
    page = (load_pages().get("pages") or {}).get(unquote(name))
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = {"title": unquote(name)}
    payload.update(page)
    return JSONResponse(payload)


# ============================================================
# 账号路由：注册 / 邮箱验证 / 登录 / 登出 / 申请 / 审核
# ============================================================

@app.get("/register", response_class=HTMLResponse)
async def route_register(msg: str = "", err: str = "") -> HTMLResponse:
    return HTMLResponse(view_register(msg, err))


@app.post("/register", response_class=HTMLResponse)
async def route_register_post(username: str = Form(""), email: str = Form(""),
                              password: str = Form(""), password2: str = Form(""),
                              captcha: str = Form(""), ctoken: str = Form("")):
    username = (username or "").strip()
    email = (email or "").strip().lower()
    # 1) 人机验证
    ok_cap, why_cap = check_captcha(ctoken, captcha)
    if not ok_cap:
        return HTMLResponse(view_register(err=why_cap), status_code=400)
    # 2) 字段校验
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{3,20}", username):
        return HTMLResponse(view_register(err="用户名 3~20 位，字母/数字/下划线/中文"), status_code=400)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return HTMLResponse(view_register(err="邮箱格式不对"), status_code=400)
    problem = password_problem(password)
    if problem:
        return HTMLResponse(view_register(err=problem), status_code=400)
    if password != password2:
        return HTMLResponse(view_register(err="两次密码不一致"), status_code=400)
    if find_user(username):
        return HTMLResponse(view_register(err="这个用户名已被注册"), status_code=400)
    if find_user(email):
        return HTMLResponse(view_register(err="这个邮箱已注册过"), status_code=400)

    # 3) 建账号（先不激活，等邮箱验证）
    pw_hash, salt = hash_password(password)
    code = new_email_code()
    uid = "u" + secrets.token_hex(6)
    users = load_users()
    users["users"][uid] = {
        "uid": uid, "username": username, "email": email,
        "password": pw_hash, "salt": salt, "role": ROLE_GUEST,
        "email_verified": False,
        "verify": {"code": code, "time": _now(), "tries": 0},
        "created": _now(),
    }
    save_users(users)
    sent, info = send_code_email(email, code, username)
    logger.info(f"[wiki] 新注册：{username}（{email}）")
    return RedirectResponse(f"/verify?uid={uid}&msg={quote(info)}", status_code=303)


@app.get("/verify", response_class=HTMLResponse)
async def route_verify(uid: str = "", msg: str = "", err: str = "") -> HTMLResponse:
    user = (load_users().get("users") or {}).get(uid or "")
    notice = f'<div class="notice">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="notice warn">{_esc(err)}</div>' if err else ""
    inner = f"""
    <div class="auth">
      <h1>邮箱验证</h1>
      <div class="sub2">验证码已生成，{CODE_TTL // 60} 分钟内有效</div>
      {notice}{warn}
      <form method="post" action="/verify">
        <input type="hidden" name="uid" value="{_esc(uid)}">
        <div class="field"><label>邮箱验证码（6 位数字）</label>
          <input name="code" inputmode="numeric" maxlength="6" required
                 style="text-align:center;letter-spacing:6px;font-size:20px"></div>
        <button type="submit">验证</button>
      </form>
      <div class="foot2">
        收不到邮件？联系站长 {OWNER_ID} 手动帮你通过，或稍后重发<br>
        <a href="/login">先去登录</a>
      </div>
    </div>"""
    if not user:
        warn = '<div class="notice warn">找不到这个账号</div>'
        return HTMLResponse(_layout("邮箱验证", f'<div class="auth">{warn}</div>'))
    return HTMLResponse(_layout("邮箱验证", inner))


@app.post("/verify", response_class=HTMLResponse)
async def route_verify_post(uid: str = Form(""), code: str = Form("")):
    users = load_users()
    rec = users["users"].get(uid or "")
    if not rec:
        return RedirectResponse("/register?err=" + quote("账号不存在"), status_code=303)
    v = rec.get("verify") or {}
    if rec.get("email_verified"):
        return RedirectResponse("/login?msg=" + quote("邮箱已验证，请登录"), status_code=303)
    if int(v.get("tries", 0)) >= CODE_MAX_TRIES:
        return RedirectResponse(f"/verify?uid={uid}&err=" + quote("尝试次数太多，请联系站长"),
                                status_code=303)
    if _now() - float(v.get("time", 0)) > CODE_TTL:
        return RedirectResponse(f"/verify?uid={uid}&err=" + quote("验证码已过期，请联系站长重发"),
                                status_code=303)
    rec["verify"] = {**v, "tries": int(v.get("tries", 0)) + 1}
    if not hmac.compare_digest(str(v.get("code", "")), str(code or "").strip()):
        save_users(users)
        return RedirectResponse(f"/verify?uid={uid}&err=" + quote("验证码不对"),
                                status_code=303)
    rec["email_verified"] = True
    rec.pop("verify", None)
    # 首次部署方便：还没有站长时，第一个通过验证的账号自动成为站长
    has_owner = any(str(u.get("role")) == ROLE_OWNER for u in users["users"].values())
    if not has_owner:
        rec["role"] = ROLE_OWNER
        logger.info(f"[wiki] 🎉 首个账号自动设为站长：{rec.get('username')}")
    save_users(users)
    logger.info(f"[wiki] 邮箱验证通过：{rec.get('username')}")
    return RedirectResponse("/login?msg=" + quote("邮箱验证成功，请登录"), status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def route_login_page(msg: str = "", err: str = "") -> HTMLResponse:
    return HTMLResponse(view_login(msg, err))


@app.post("/login", response_class=HTMLResponse)
async def route_login_post(req: Request, account: str = Form(""),
                            password: str = Form("")):
    ip = (req.client.host if req.client else "") or ""
    wait = login_blocked(ip)
    if wait:
        return HTMLResponse(
            view_login(err=f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试"),
            status_code=429)
    user = find_user(account)
    if not user or not verify_password(password, user):
        login_fail(ip)
        return HTMLResponse(view_login(err="用户名或密码不对"), status_code=400)
    login_ok(ip)
    if user.get("status") == "banned":
        return HTMLResponse(view_login(err="账号已被封禁"), status_code=403)
    if not user.get("email_verified"):
        return RedirectResponse(f"/verify?uid={user['uid']}&err=" + quote("请先验证邮箱"),
                                status_code=303)
    token = new_session(user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax")
    logger.info(f"[wiki] 登录：{user.get('username')}")
    return resp


@app.post("/login-cookie", response_class=HTMLResponse)
async def route_login_cookie_post(cookie: str = Form("")):
    user = live_cookie((cookie or "").strip())
    if not user:
        return HTMLResponse(view_login(err="Cookie 无效或已被收回"), status_code=400)
    token = new_session(user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax")
    logger.info(f"[wiki] Cookie 登录：{user.get('username')}")
    return resp


@app.get("/logout")
async def route_logout(request: Request) -> RedirectResponse:
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        revoke_session(token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    resp.delete_cookie(LEGACY_COOKIE)
    return resp


@app.get("/apply", response_class=HTMLResponse)
async def route_apply() -> HTMLResponse:
    return HTMLResponse(view_apply())


@app.post("/apply", response_class=HTMLResponse)
async def route_apply_post(reason: str = Form("")):
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    ok, msg = request_cookie(user, reason)
    cls = "notice" if ok else "notice warn"
    box = f'<div class="{cls}">{_esc(msg)}</div>'
    return HTMLResponse(_layout("申请 Cookie",
                                f'<div class="auth" style="max-width:520px">'
                                f'<h1>申请 Cookie</h1>{box}'
                                f'<div class="foot2"><a href="/apply">刷新看看</a> · '
                                f'<a href="/">返回首页</a></div></div>'))


@app.get("/admin/cookies", response_class=HTMLResponse)
async def route_admin_cookies(msg: str = "", err: str = "") -> HTMLResponse:
    blocked = _require_owner()
    return blocked or HTMLResponse(view_admin_cookies(msg, err))


@app.post("/admin/pages/delete")
async def route_admin_page_delete(name: str = Form("")):
    """删除词条（站长或编辑）"""
    if not can_edit(CUR_USER["v"]):
        return RedirectResponse("/login", status_code=303)
    name = (name or "").strip()
    data = load_pages()
    page = (data.get("pages") or {}).pop(name, None)
    if not page:
        return RedirectResponse("/admin/pages?err=" + quote(f"没有词条「{name}」"),
                                status_code=303)
    # 删除前留一份到回收站，误删还能救
    try:
        bin_file = DATA_DIR / "deleted_pages.json"
        bin_data = _load_json(bin_file, {"items": []})
        bin_data.setdefault("items", []).append(
            {"name": name, "page": page, "time": _now(),
             "by": (CUR_USER["v"] or {}).get("username", "")})
        bin_data["items"] = bin_data["items"][-100:]
        _save_json(bin_file, bin_data)
    except Exception as e:
        logger.warning(f"[wiki] 回收站写入失败: {e}")
    save_pages(data)
    logger.info(f"[wiki] 删除词条：{name}")
    return RedirectResponse("/admin/pages?msg=" + quote(f"已删除词条「{name}」"),
                            status_code=303)


@app.get("/admin/pages/deleted", response_class=HTMLResponse)
async def route_admin_pages_deleted() -> HTMLResponse:
    """回收站：最近删除的词条，可一键恢复"""
    blocked = _admin_blocked()
    if blocked:
        return blocked
    bin_data = _load_json(DATA_DIR / "deleted_pages.json", {"items": []})
    items = list(reversed(bin_data.get("items") or []))[:80]
    rows = ""
    for it in items:
        rows += (f'<tr><td>{_esc(it.get("name"))}</td>'
                 f'<td>{len(str((it.get("page") or {}).get("body") or ""))}</td>'
                 f'<td>{_esc(it.get("by") or "-")}</td>'
                 f'<td>{datetime.fromtimestamp(float(it.get("time", 0))).strftime("%m-%d %H:%M")}</td>'
                 f'<td class="row-actions"><form method="post" action="/admin/pages/restore">'
                 f'<input type="hidden" name="name" value="{_esc(it.get("name"))}">'
                 f'<button class="mini ok">恢复</button></form></td></tr>')
    inner = f"""
    <div class="admin-head"><h1>回收站</h1>
      <div class="t">最近删除的词条 · 最多保留 100 条</div></div>
    {_admin_tabs("pages")}
    <div style="margin-bottom:14px"><a class="btn" href="/admin/pages">← 回到词条管理</a></div>
    <table class="t"><thead><tr><th>标题</th><th>字数</th><th>删除者</th><th>时间</th><th>操作</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=5 style="color:var(--sub)">回收站是空的</td></tr>'}</tbody></table>
    """
    return HTMLResponse(_layout("回收站", inner))


@app.post("/admin/pages/restore")
async def route_admin_page_restore(name: str = Form("")):
    if not can_edit(CUR_USER["v"]):
        return RedirectResponse("/login", status_code=303)
    name = (name or "").strip()
    bin_file = DATA_DIR / "deleted_pages.json"
    bin_data = _load_json(bin_file, {"items": []})
    items = bin_data.get("items") or []
    hit = next((x for x in reversed(items) if x.get("name") == name), None)
    if not hit:
        return RedirectResponse("/admin/pages/deleted?err=" + quote("回收站里没有这条"),
                                status_code=303)
    data = load_pages()
    data.setdefault("pages", {})[name] = hit["page"]
    save_pages(data)
    items = [x for x in items if x is not hit]
    bin_data["items"] = items
    _save_json(bin_file, bin_data)
    logger.info(f"[wiki] 恢复词条：{name}")
    return RedirectResponse("/admin/pages/deleted?msg=" + quote(f"已恢复「{name}」"),
                            status_code=303)


@app.post("/admin/media/delete")
async def route_admin_media_delete(name: str = Form("")):
    """删除上传的图片（同时清掉媒体索引）"""
    if not can_edit(CUR_USER["v"]):
        return RedirectResponse("/login", status_code=303)
    safe = Path(name or "").name
    target = (UPLOAD_DIR / safe)
    removed = False
    try:
        if target.exists() and target.is_file():
            target.unlink()
            removed = True
    except Exception as e:
        logger.warning(f"[wiki] 删除图片失败 {safe}: {e}")
    data = load_media()
    data["items"].pop(safe, None)
    save_media(data)
    logger.info(f"[wiki] 删除图片：{safe}")
    msg = f"已删除图片 {safe}" if removed else f"图片 {safe} 不存在（已清理索引）"
    return RedirectResponse("/admin/media?msg=" + quote(msg), status_code=303)


@app.post("/admin/pages/clear")
async def route_admin_pages_clear():
    """危险操作：清空所有词条（仅站长，会先备份到回收站）"""
    if not is_owner(CUR_USER["v"]):
        return RedirectResponse("/admin/pages?err=" + quote("只有站长能清空词条"),
                                status_code=303)
    data = load_pages()
    pages = data.get("pages") or {}
    try:
        bin_file = DATA_DIR / "deleted_pages.json"
        bin_data = _load_json(bin_file, {"items": []})
        for n, p in pages.items():
            bin_data.setdefault("items", []).append(
                {"name": n, "page": p, "time": _now(), "by": "站长清空"})
        bin_data["items"] = bin_data["items"][-100:]
        _save_json(bin_file, bin_data)
    except Exception as e:
        logger.warning(f"[wiki] 备份失败: {e}")
    count = len(pages)
    data["pages"] = {}
    save_pages(data)
    logger.warning(f"[wiki] 站长清空了所有词条（{count} 个，已进回收站）")
    return RedirectResponse("/admin/pages?msg=" + quote(f"已清空 {count} 个词条（可在回收站恢复）"),
                            status_code=303)


@app.post("/admin/media/clear")
async def route_admin_media_clear():
    """清空媒体库（仅站长）"""
    if not is_owner(CUR_USER["v"]):
        return RedirectResponse("/admin/media?err=" + quote("只有站长能清空媒体库"),
                                status_code=303)
    n = 0
    try:
        for f in UPLOAD_DIR.iterdir():
            if f.is_file():
                f.unlink()
                n += 1
    except Exception as e:
        logger.warning(f"[wiki] 清空媒体库失败: {e}")
    save_media({"items": {}})
    logger.warning(f"[wiki] 站长清空媒体库（{n} 张）")
    return RedirectResponse("/admin/media?msg=" + quote(f"已清空媒体库（{n} 张）"),
                            status_code=303)


@app.post("/admin/cookie", response_class=HTMLResponse)
async def route_admin_cookie_action(uid: str = Form(""), action: str = Form("")):
    """站长操作：approve/reject 申请、grant 直接发、revoke 收回"""
    blocked = _require_owner()
    if blocked:
        return blocked
    users = load_users()
    rec = users["users"].get(uid or "")
    if not rec:
        return RedirectResponse("/admin/cookies?err=" + quote("用户不存在"), status_code=303)

    if action in ("approve", "reject"):
        ok, msg = review_cookie(uid, approve=(action == "approve"),
                                reviewer=OWNER_ID)
        return RedirectResponse("/admin/cookies?" + ("msg=" if ok else "err=") + quote(msg),
                                status_code=303)
    if action == "grant":
        # ⚠️ 不能覆盖站长的角色：站长点到自己那行「直接发放」会把 owner 降成 editor
        if effective_role(rec) == ROLE_OWNER:
            return RedirectResponse(
                "/admin/cookies?err=" + quote("这是站长账号，不需要发放 Cookie"),
                status_code=303)
        cookie = secrets.token_urlsafe(24)
        rec["role"] = ROLE_EDITOR
        rec["cookie"] = {"token": cookie, "issued": _now(), "by": OWNER_ID}
        save_users(users)
        logger.info(f"[wiki] 站长直接发放 Cookie：{rec.get('username')}")
        return RedirectResponse("/admin/cookies?msg=" + quote(f"已发放：{cookie}"),
                                status_code=303)
    if action == "revoke":
        if effective_role(rec) == ROLE_OWNER:
            return RedirectResponse(
                "/admin/cookies?err=" + quote("站长账号的权限不能被收回"),
                status_code=303)
        rec["cookie"] = {}
        rec["role"] = ROLE_GUEST
        save_users(users)
        logger.info(f"[wiki] 收回 Cookie：{rec.get('username')}")
        return RedirectResponse("/admin/cookies?msg=" + quote("已收回该用户的 Cookie"),
                                status_code=303)
    if action == "delete":
        if effective_role(rec) == ROLE_OWNER:
            return RedirectResponse(
                "/admin/cookies?err=" + quote("站长账号不能删除"), status_code=303)
        # 删账号时顺手清掉它的会话，避免残留登录态
        sess = load_sessions()
        sess["sessions"] = {k: v for k, v in (sess.get("sessions") or {}).items()
                            if v.get("uid") != uid}
        save_sessions(sess)
        users["users"].pop(uid, None)
        save_users(users)
        logger.info(f"[wiki] 删除账号：{rec.get('username')}（{rec.get('email')}）")
        return RedirectResponse(
            "/admin/cookies?msg=" + quote(f"已删除账号 {rec.get('username')}"),
            status_code=303)
    if action in ("set_editor", "set_guest"):
        role = ROLE_EDITOR if action == "set_editor" else ROLE_GUEST
        if effective_role(rec) == ROLE_OWNER:
            return RedirectResponse(
                "/admin/cookies?err=" + quote("站长账号的角色不能改"),
                status_code=303)
        rec["role"] = role
        if role == ROLE_GUEST:
            rec["cookie"] = {}
        save_users(users)
        logger.info(f"[wiki] 角色调整：{rec.get('username')} → {role}")
        return RedirectResponse("/admin/cookies?msg=" + quote(f"已设为{ROLE_NAMES[role]}"),
                                status_code=303)
    return RedirectResponse("/admin/cookies?err=" + quote("未知操作"), status_code=303)


@app.get("/admin/mail/test")
async def route_admin_mail_test(to: str = "") -> HTMLResponse:
    """站长测试 SMTP 是否能真的把邮件发出去"""
    blocked = _require_owner()
    if blocked:
        return blocked
    to = (to or "").strip()
    if not to:
        return RedirectResponse("/admin/mail?err=" + quote("请先在地址栏加 ?to=你的邮箱"),
                                status_code=303)
    code = new_email_code()
    ok, msg = send_code_email(to, code, "测试")
    smtp = _smtp_config()
    real = bool(smtp.get("host"))
    logger.info(f"[wiki] 测试发信 -> {to}: {msg}")
    head = "SMTP 已配置，已尝试真实投递" if real else "SMTP 未配置，只写了本地 outbox"
    body = f"{head}：{msg}（验证码 {code}）"
    return RedirectResponse("/admin/mail?msg=" + quote(body), status_code=303)


@app.get("/admin/mail", response_class=HTMLResponse)
async def route_admin_mail(req: Request) -> HTMLResponse:
    """站长兜底查看最近的验证码邮件（没配 SMTP 时用）"""
    blocked = _require_owner()
    if blocked:
        return blocked
    smtp = _smtp_config()
    status = ("已配置 SMTP：" + smtp.get("host", "")) if smtp.get("host") else \
             "未配置 SMTP —— 验证码写到 outbox，可在本页查看后转告用户"
    notice = ""
    if req.query_params.get("msg"):
        notice = f'<div class="notice">{_esc(req.query_params["msg"])}</div>'
    if req.query_params.get("err"):
        notice = f'<div class="notice warn">{_esc(req.query_params["err"])}</div>'
    import json as _json
    sample_cfg = _json.dumps({"token": "（原有）", "smtp": {
        "host": "smtp.qq.com", "port": "465", "ssl": "1",
        "user": "you@qq.com", "password": "SMTP授权码", "from": "you@qq.com"}},
        ensure_ascii=False, indent=2)
    inner = f"""
    <div class="admin-head"><h1>邮件</h1><div class="t">{_esc(status)}</div></div>
    {_admin_tabs("mail")}
    {notice}
    <h2 style="font-size:16px;margin:0 0 10px">发信测试</h2>
    <form method="get" action="/admin/mail/test" style="display:flex;gap:10px;max-width:520px">
      <input name="to" type="email" placeholder="收件邮箱，例如 you@qq.com"
        style="flex:1;padding:10px 13px;border:1px solid var(--line);border-radius:9px;
        background:var(--panel);color:var(--ink);font-size:14px;outline:none">
      <button class="btn primary" type="submit">发测试邮件</button>
    </form>
    <p style="font-size:12.5px;color:var(--sub);margin-top:10px">
      配了 SMTP 会真的投递到邮箱；没配就只写本地 outbox（下面能看到）。
    </p>
    <h2 style="font-size:16px;margin:26px 0 10px">最近一封邮件</h2>
    <pre style="white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);
      border-radius:10px;padding:16px;font-size:13px;line-height:1.8">{_esc(latest_mail())}</pre>
    <h2 style="font-size:16px;margin:26px 0 10px">怎么配 SMTP</h2>
    <pre style="white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);
      border-radius:10px;padding:16px;font-size:12.5px;line-height:1.8">{_esc(sample_cfg)}

QQ 邮箱：设置 → 账户 → 开启 IMAP/SMTP → 生成授权码（不是登录密码）。
其它邮箱同理，改 host/port 即可（465 走 SSL，587 走 STARTTLS）。
改完 wiki_data/config.json 后重启机器人。</pre>
    """
    return HTMLResponse(_layout("邮件", inner))


# ============================================================
# 后台路由 / 上传 / 静态媒体
# ============================================================

def _admin_blocked() -> Optional[HTMLResponse]:
    """后台入口：站长（owner）或已获 Cookie 的编辑都能进"""
    user = CUR_USER["v"]
    if can_edit(user):
        return None
    if not user:
        notice = ('<div class="notice warn">请先登录。'
                  '<a href="/login">去登录</a> · <a href="/register">注册</a></div>')
    else:
        notice = ('<div class="notice warn">你的账号还没有后台权限（需要站长发放 Cookie）。'
                  '<a href="/apply">申请 Cookie</a></div>')
    return HTMLResponse(_layout("需要权限", notice + "<h1>没有后台权限</h1>"), status_code=403)


@app.get("/admin", response_class=HTMLResponse)
async def route_admin() -> HTMLResponse:
    return _admin_blocked() or HTMLResponse(view_admin_home())


@app.get("/admin/load", response_class=HTMLResponse)
async def route_admin_load() -> HTMLResponse:
    return _admin_blocked() or HTMLResponse(view_admin_load())


@app.get("/admin/media", response_class=HTMLResponse)
async def route_admin_media(msg: str = "", err: str = "") -> HTMLResponse:
    return _admin_blocked() or HTMLResponse(view_admin_media(msg, err))


@app.get("/admin/pages", response_class=HTMLResponse)
async def route_admin_pages() -> HTMLResponse:
    return _admin_blocked() or HTMLResponse(view_admin_pages())


@app.get("/api/admin/load")
async def route_api_admin_load() -> JSONResponse:
    if not can_edit(CUR_USER["v"]):
        return JSONResponse({"error": "需要登录"}, status_code=403)
    return JSONResponse(server_load())


@app.get("/api/load")
async def route_api_load() -> JSONResponse:
    """公开的轻量负载（只给状态，不暴露细节，方便手机看一眼）"""
    s = server_load()
    return JSONResponse({
        "ok": True,
        "cpu": s["cpu"]["percent"], "mem": s["mem"]["percent"],
        "disk": s["disk"]["percent"], "uptime": s["uptime"],
        "pages": s["pages"], "images": s["uploads"]["count"],
    })


@app.post("/admin/upload")
async def route_admin_upload(request: Request, file: UploadFile = File(...)):
    """图片上传（后台用，返回纯文本结果给 fetch 显示）"""
    if not can_edit(CUR_USER["v"]):
        return PlainTextResponse("❌ 需要先登录", status_code=403)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return PlainTextResponse(
            f"❌ 图片太大（{len(raw) / 1024 / 1024:.1f}MB，上限 {MAX_UPLOAD_MB}MB）",
            status_code=413)
    ok, msg = save_upload(file.filename or "", raw)
    if not ok:
        return PlainTextResponse(f"❌ {msg}", status_code=400)
    return PlainTextResponse(f"✅ 上传成功：{msg}")


@app.post("/api/upload")
async def route_api_upload(request: Request, file: UploadFile = File(...)):
    """给编辑器 AJAX 用的上传接口，返回 JSON"""
    if not can_edit(CUR_USER["v"]):
        return JSONResponse({"ok": False, "error": "需要登录"}, status_code=403)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse({"ok": False,
                             "error": f"图片太大（上限 {MAX_UPLOAD_MB}MB）"}, status_code=413)
    ok, msg = save_upload(file.filename or "", raw)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return JSONResponse({"ok": True, "url": msg})


@app.get("/favicon.svg")
async def route_favicon():
    """站点图标（浏览器标签页左边那个小图）"""
    path = DATA_DIR / "favicon.svg"
    if not path.exists():
        return PlainTextResponse("", status_code=404)
    return FileResponse(str(path), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/media/{name}")
async def route_media(name: str):
    """提供上传的图片（严格限制在 uploads 目录内，防路径穿越）"""
    safe = Path(name).name
    target = UPLOAD_DIR / safe
    try:
        target = target.resolve()
        if UPLOAD_DIR.resolve() not in target.parents or not target.is_file():
            return PlainTextResponse("not found", status_code=404)
    except Exception:
        return PlainTextResponse("not found", status_code=404)
    suffix = target.suffix.lower()
    media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp",
                  ".svg": "image/svg+xml"}.get(suffix, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type,
                        headers={"Cache-Control": "public, max-age=86400"})


# ============================================================
# 初始内容（首次启动时写入，之后不再动）
# ============================================================

SEED_PAGES = {
    "欢迎": {
        "tags": ["站点"],
        "body": """这是群友共建的「星海百科」。

## 怎么用
- 左上角搜索框可以直接搜**标题 / 正文 / 标签**
- 左侧「全部词条 / 标签 / 最近更新」可以按目录翻
- 有编辑口令的同学可以**新建**和**编辑**，每次保存都会留历史版本

## 写点什么好
- 群里反复被问的问题（群规、机器人怎么用、常见错误）
- 技术笔记、教程、踩坑记录
- 有意思的资料、书单、工具清单

## 约定
- 标题用名词短语，例如「电压表」「群规」
- 正文支持 Markdown：二级标题用两个井号，粗体用两个星号，列表用减号
- 灌水会被清理，认准有信息量的内容""",
    },
    "群规": {
        "tags": ["站点", "群务"],
        "body": """## 基本约定
1. 友善讨论，不人身攻击、不阴阳怪气
2. 不刷屏、不重复发同一内容（机器人会自动提醒）
3. 不讨论违法内容，不发广告

## 机器人相关
- 被禁言/黑名单会有提示，有异议找群管理
- 机器人功能见「机器人功能」词条

## 关于本百科
- 有编辑口令就能写，写完大家都能看
- 内容有误直接改，改了会留下历史记录""",
    },
    "机器人功能": {
        "tags": ["机器人"],
        "body": """## 日常
- 签到　每日签到抽星星，连签有加成
- 森林 / 种树 / 浇水　群共享森林玩法
- 农场 / 一键　种菜收菜，一键把待办做完

## 查询
- 天气 城市　天气卡片；台风　查活跃台风
- 本站（星海百科）就是群的公共知识库

## 其他
- 正经　群管理工具（刷屏/黑名单）
- 猜歌 / 猜词 / 海龟汤　小游戏
- AI 助手：群里 @机器人 或私聊直接问，支持联网搜索""",
    },
}


def seed_if_empty() -> None:
    data = load_pages()
    if data.get("pages"):
        return
    now = _now()
    for name, page in SEED_PAGES.items():
        data.setdefault("pages", {})[name] = {
            "body": page["body"], "tags": page["tags"],
            "created": now, "updated": now, "edits": 1, "history": [],
            "author": "system",
        }
    save_pages(data)
    logger.info(f"[wiki] 已写入 {len(SEED_PAGES)} 个初始词条")


# ============================================================
# 启动
# ============================================================

driver = get_driver()


@driver.on_startup
async def _wiki_startup() -> None:
    global CONFIG, EDIT_TOKEN
    import asyncio
    CONFIG = load_config()
    EDIT_TOKEN = str(CONFIG.get("token") or "")
    seed_if_empty()
    pages = len((load_pages().get("pages") or {}))

    async def _serve() -> None:
        config = uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.create_task(_serve())
    logger.info("=" * 62)
    logger.info(f"[wiki] 📚 星海百科已启动：{PUBLIC_URL}（本机 http://127.0.0.1:{WEB_PORT}）")
    logger.info(f"[wiki] 📄 现有词条 {pages} 个　数据：{PAGES_FILE}")
    logger.info(f"[wiki] 🔑 编辑口令：{EDIT_TOKEN}")
    logger.info(f"[wiki] 🔗 登录地址：{PUBLIC_URL}/login?token={EDIT_TOKEN}")
    logger.info("=" * 62)


logger.info("📚 星海百科网站已加载（替代原斗地主网页服务，端口 8081）")
