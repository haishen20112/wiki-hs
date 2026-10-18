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

import hashlib
import hmac
import html
import json
import os
import random
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse,
                               PlainTextResponse, RedirectResponse)
import uvicorn

try:
    from nonebot import get_driver
    from nonebot.log import logger
except ImportError:                       # 本地桌面版不需要 NoneBot
    import logging as _logging

    class _LocalDriver:
        def on_startup(self, fn):
            return fn

    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%m-%d %H:%M:%S")
    logger = _logging.getLogger("wiki")
    _local_driver = _LocalDriver()

    def get_driver():
        return _local_driver

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
CUR_SESSION: Dict[str, Any] = {"v": ""}   # 当前请求的会话 token


# ============================================================
# Markdown-lite（够用就行：标题/粗体/斜体/代码/列表/链接/引用）
# ============================================================


# ============================================================
# 安全：SVG 消毒、上传限流、审计日志
# ============================================================

# SVG 里可能藏脚本，上传前剥掉危险构造（保留正常图形）
_SVG_DANGER_TAGS = ("script", "iframe", "object", "embed", "handler",
                    "listener", "set", "animate", "foreignobject", "use")
_SVG_DANGER_ATTR = re.compile(
    r'\s+on[a-zA-Z]+\s*=\s*(?:"[^"]*"|' + "'" + r"[^']*" + "'" + r"|[^\s>]+)", re.I)
_SVG_JS_URL = re.compile(
    r'(?:href|xlink:href|src|action)\s*=\s*["' + "'" + r']?\s*'
    r'(?:javascript|data:text/html|vbscript):[^"\'>]*', re.I)
_SVG_TAG_BLOCK = re.compile(
    r"<(?:script|iframe|object|embed|foreignObject|animate|set|handler|listener)\b"
    r"[^>]*>.*?</(?:script|iframe|object|embed|foreignObject|animate|set|handler|listener)\s*>",
    re.I | re.S)
_SVG_TAG_SELF = re.compile(
    r"<(?:script|iframe|object|embed|foreignObject|animate|set|handler|listener)\b[^>]*/?>",
    re.I)


def sanitize_svg(raw: bytes) -> Optional[bytes]:
    """清洗 SVG：去掉脚本、事件属性、javascript: 链接。

    返回清洗后的字节；若清洗后不含 <svg 则认为不是有效 SVG，返回 None。
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    if "<svg" not in text.lower():
        return None
    # 1) 删整段危险标签
    text = _SVG_TAG_BLOCK.sub("", text)
    # 2) 删自闭合危险标签
    text = _SVG_TAG_SELF.sub("", text)
    # 3) 删 on* 事件属性
    text = _SVG_DANGER_ATTR.sub("", text)
    # 4) 删 javascript:/data:text/html: 链接
    text = _SVG_JS_URL.sub("", text)
    # 5) 去掉 XML 处理指令里的外部实体（XXE 防护）
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<!ENTITY[^>]*>", "", text, flags=re.I)
    if "<svg" not in text.lower():
        return None
    return text.encode("utf-8")


# ── 上传限流：同一 IP 每小时上限，防止塞满磁盘 ──
UPLOAD_LOG: Dict[str, List[float]] = {}
UPLOAD_PER_HOUR = 30
UPLOAD_MAX_FILES = 2000          # 总量上限，超过后拒绝新上传


def upload_allowed(ip: str) -> Tuple[bool, str]:
    """检查是否还能上传"""
    # 总量保护
    try:
        existing = len(list(UPLOAD_DIR.glob("*")))
    except Exception:
        existing = 0
    if existing >= UPLOAD_MAX_FILES:
        return False, f"媒体库已达上限（{UPLOAD_MAX_FILES} 张），请联系站长清理"
    if not ip:
        return True, ""
    now = _now()
    hist = [t for t in UPLOAD_LOG.get(ip, []) if now - t < 3600]
    if len(hist) >= UPLOAD_PER_HOUR:
        wait = int(3600 - (now - hist[0]))
        return False, f"上传太频繁，请 {max(1, wait // 60)} 分钟后再试"
    hist.append(now)
    UPLOAD_LOG[ip] = hist
    return True, ""


# ── 审计日志（jsonl 追加，保留 14 天）──
AUDIT_FILE = DATA_DIR / "audit.jsonl"
AUDIT_KEEP_DAYS = 14
AUDIT_LOCK = threading.Lock()


def audit(action: str, target: str = "", detail: str = "") -> None:
    """记一条操作日志：谁、在哪、做了什么"""
    try:
        user = CUR_USER["v"] or {}
        row = {
            "t": _now(),
            "who": str(user.get("username") or user.get("uid") or "匿名")[:40],
            "role": effective_role(user) if user else "guest",
            "action": str(action)[:40],
            "target": str(target)[:120],
            "detail": str(detail)[:200],
        }
        with AUDIT_LOCK:
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[wiki] 写审计日志失败: {e}")


def load_audit(days: int = AUDIT_KEEP_DAYS) -> List[Dict[str, Any]]:
    """读最近 N 天审计记录，顺便清理过期"""
    if not AUDIT_FILE.exists():
        return []
    cutoff = _now() - days * 86400
    keep: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    try:
        for line in AUDIT_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if float(r.get("t", 0)) >= cutoff:
                keep.append(r)
                rows.append(r)
    except Exception as e:
        logger.warning(f"[wiki] 读审计日志失败: {e}")
        return []
    # 有过期记录就重写一次，控制文件大小
    if len(keep) < 1 or (AUDIT_FILE.stat().st_size > 512 * 1024):
        try:
            AUDIT_FILE.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                encoding="utf-8")
        except Exception:
            pass
    return list(reversed(rows))



# ============================================================
# UI 模式：lite（默认）/ standard
#   lite     纯内联 CSS，零外部请求，断网可用，CSP 最严
#   standard 引入 Tailwind CSS（CDN），视觉更现代，需联网
# 在 wiki_data/config.json 里设置 "ui_mode": "standard" 即可切换
# ============================================================
TAILWIND_CDN = "https://cdn.tailwindcss.com"


def ui_mode() -> str:
    mode = str(load_config().get("ui_mode") or "lite").strip().lower()
    return "standard" if mode in ("standard", "std", "tailwind", "pro") else "lite"


def is_standard_ui() -> bool:
    return ui_mode() == "standard"


def tailwind_head() -> str:
    """standard 模式注入 Tailwind；lite 模式返回空串"""
    if not is_standard_ui():
        return ""
    return (
        f'<script src="{TAILWIND_CDN}"></script>'
        "<script>"
        "if(window.tailwind){tailwind.config={darkMode:'media',theme:{extend:{"
        "colors:{brand:'#2c6e5b',brand2:'#3b7ea1',warm:'#c9703c'},"
        "borderRadius:{card:'0.9rem'},"
        "boxShadow:{glow:'0 10px 40px rgba(95,199,158,.35)'}"
        "}}};}"
        "</script>"
    )


def csp_policy() -> str:
    """按 UI 模式生成 CSP：standard 模式放行 Tailwind CDN"""
    parts = [
        "default-src 'self'",
        "img-src 'self' data: blob:",
        "style-src 'self' 'unsafe-inline'",
        "form-action 'self'",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
    if is_standard_ui():
        # Tailwind CDN 需要执行外部脚本，并动态注入样式
        parts.append(f"script-src 'self' 'unsafe-inline' {TAILWIND_CDN}")
        parts.append(f"style-src 'self' 'unsafe-inline' {TAILWIND_CDN}")
    else:
        parts.append("script-src 'self' 'unsafe-inline'")
    return "; ".join(parts)


def ui_badge() -> str:
    """页脚显示当前模式，方便确认切换是否生效"""
    return "标准版" if is_standard_ui() else "轻量版"


# ── 敏感文件权限 ──
def tighten_perms() -> None:
    """把含隐私数据的文件设为 600"""
    for p in (USERS_FILE, SESSION_FILE, CONFIG_FILE, AUDIT_FILE, QA_FILE):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except Exception:
            pass



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
  /* 底：暖白，比纯白柔和，长时间看不刺眼 */
  --bg:#fbfaf8; --bg2:#f5f3ef;
  --panel:#ffffff; --panel2:#faf9f7;
  --ink:#1c2430; --ink2:#39434f; --sub:#79838f; --faint:#a8b0b9;
  --line:#e8e5df; --line2:#d8d4cc;
  /* 主色：墨绿，稳；强调色：暖橙，只用于点缀 */
  --accent:#2c6e5b; --accent-ink:#1f5445; --accent-soft:#e6f0ec;
  --accent2:#3b7ea1; --accent2-soft:#e5eff5;
  --warm:#c9703c; --warm-soft:#fbeee5;
  --ok:#2c6e5b; --warn:#c9963c; --bad:#b8493c;
  --soft:#f2f0eb;
  --code:#232b33;
  --shadow:0 1px 2px rgba(28,36,48,.04), 0 4px 14px rgba(28,36,48,.05);
  --shadow-lg:0 2px 6px rgba(28,36,48,.06), 0 12px 34px rgba(28,36,48,.09);
  --radius:12px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#151a1f; --bg2:#11161a;
    --panel:#1c2228; --panel2:#202730;
    --ink:#e8edf3; --ink2:#c9d3dd; --sub:#8e9aa7; --faint:#65717e;
    --line:#2a323b; --line2:#38424d;
    --accent:#5fc79e; --accent-ink:#8fe0bf; --accent-soft:#1e2f29;
    --accent2:#6fb3d9; --accent2-soft:#1c2b35;
    --warm:#e0915a; --warm-soft:#33251b;
    --ok:#5fc79e; --warn:#e0b45a; --bad:#e0776a;
    --soft:#232b34;
    --code:#0e1317;
    --shadow:0 1px 2px rgba(0,0,0,.3), 0 4px 14px rgba(0,0,0,.24);
    --shadow-lg:0 2px 8px rgba(0,0,0,.35), 0 14px 40px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink); line-height:1.72;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",sans-serif;
  font-size:15.5px;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  text-rendering:optimizeLegibility;
}
::selection{background:var(--accent-soft);color:var(--accent-ink)}
a{color:var(--accent2);text-decoration:none;transition:color .15s}
a:hover{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}

.layout{display:flex;min-height:100vh}

/* ── 侧栏 ── */
.side{
  width:256px;flex:0 0 256px;
  background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%);
  border-right:1px solid var(--line);
  padding:24px 18px;position:sticky;top:0;height:100vh;overflow:auto;
  scrollbar-width:thin;
}
.side::-webkit-scrollbar{width:6px}
.side::-webkit-scrollbar-thumb{background:var(--line2);border-radius:3px}
.logo{
  display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800;
  letter-spacing:1.5px;color:var(--ink);
}
.logo-img{
  width:32px;height:32px;border-radius:9px;display:block;flex-shrink:0;
  box-shadow:var(--shadow);
}
.logo span{
  background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
}
.sub{font-size:12px;color:var(--sub);margin-top:6px;letter-spacing:.3px}

.search{margin:20px 0 12px;display:flex;gap:7px}
.search input{
  flex:1;min-width:0;padding:9px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--bg);color:var(--ink);
  font-size:13.5px;outline:none;transition:border-color .15s,box-shadow .15s;
}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search input::placeholder{color:var(--faint)}
.search button{
  padding:9px 14px;border:1px solid var(--accent);background:var(--accent);
  color:#fff;border-radius:9px;font-size:13.5px;cursor:pointer;
  transition:background .15s,transform .1s;
}
.search button:hover{background:var(--accent-ink)}
.search button:active{transform:scale(.97)}

.nav{margin-top:16px;display:flex;flex-direction:column;gap:2px}
.nav a{
  display:block;padding:8px 11px;border-radius:9px;color:var(--ink2);
  font-size:14px;transition:background .15s,color .15s;
  position:relative;
}
.nav a:hover{background:var(--soft);color:var(--ink)}
.nav a.on{
  background:var(--accent-soft);color:var(--accent-ink);font-weight:600;
}
.nav a.on::before{
  content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:3px;height:16px;border-radius:2px;background:var(--accent);
}
.side h3{
  font-size:11px;color:var(--faint);letter-spacing:1.4px;margin:24px 0 8px;
  font-weight:600;text-transform:uppercase;
}
.side ul{list-style:none;margin:0;padding:0}
.side ul li a{
  display:block;padding:5px 9px;border-radius:7px;font-size:13px;color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:background .15s,color .15s;
}
.side ul li a:hover{background:var(--soft);color:var(--ink)}

/* ── 主区 ── */
.main{flex:1;min-width:0;padding:34px 44px 70px;max-width:1020px}
.crumb{font-size:12.5px;color:var(--faint);margin-bottom:12px}
.crumb a{color:var(--sub)}
h1{
  font-size:32px;line-height:1.28;margin:0 0 8px;letter-spacing:.3px;
  font-weight:800;color:var(--ink);
}
.meta{
  display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--sub);
  padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:26px;
  align-items:center;
}
.tag{
  display:inline-block;padding:2px 10px;border-radius:20px;
  background:var(--accent2-soft);color:var(--accent2);font-size:12px;
  transition:background .15s,color .15s;
}
.tag:hover{background:var(--accent2);color:#fff}

/* ── 正文排版 ── */
.article{font-size:16px;color:var(--ink2)}
.article h2{
  font-size:22px;margin:34px 0 14px;padding-left:13px;
  border-left:4px solid var(--accent);color:var(--ink);font-weight:700;
  line-height:1.4;
}
.article h3{font-size:18px;margin:26px 0 11px;color:var(--ink);font-weight:700}
.article p{margin:0 0 14px}
.article ul,.article ol{margin:0 0 15px;padding-left:26px}
.article li{margin:4px 0}
.article li::marker{color:var(--accent)}
.article blockquote{
  margin:16px 0;padding:12px 18px;border-left:3px solid var(--accent);
  color:var(--sub);background:var(--accent-soft);border-radius:0 10px 10px 0;
}
.article code{
  background:var(--soft);padding:2px 7px;border-radius:5px;font-size:.9em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:var(--accent-ink);border:1px solid var(--line);
}
.article pre.code{
  background:var(--code);color:#e6edf3;padding:16px 18px;border-radius:11px;
  overflow:auto;font-size:13.5px;line-height:1.65;box-shadow:var(--shadow);
}
.article pre.code code{background:none;padding:0;color:inherit;border:none}
.article hr{border:none;border-top:1px solid var(--line);margin:30px 0}
.article img{max-width:100%;border-radius:10px;box-shadow:var(--shadow)}

/* ── 卡片 ── */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px;display:block;box-shadow:var(--shadow);
  transition:border-color .18s,transform .18s,box-shadow .18s;
}
.card:hover{
  border-color:var(--accent);text-decoration:none;
  transform:translateY(-2px);box-shadow:var(--shadow-lg);
}
.card .t{font-size:15.5px;font-weight:700;color:var(--ink);margin-bottom:7px}
.card .d{
  font-size:13px;color:var(--sub);line-height:1.65;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.empty{color:var(--sub);text-align:center;padding:60px 0;font-size:15px}

/* ── 按钮 ── */
.btn{
  display:inline-block;padding:9px 18px;border-radius:9px;border:1px solid var(--line2);
  background:var(--panel);color:var(--ink);font-size:14px;cursor:pointer;
  transition:background .15s,border-color .15s,transform .1s,box-shadow .15s;
  font-family:inherit;
}
.btn:hover{
  text-decoration:none;border-color:var(--accent);color:var(--accent);
  box-shadow:var(--shadow);
}
.btn:active{transform:scale(.98)}
.btn.primary{
  background:var(--accent);border-color:var(--accent);color:#fff;
  font-weight:600;
}
.btn.primary:hover{background:var(--accent-ink);border-color:var(--accent-ink);color:#fff}

/* ── 表单 ── */
.form{max-width:820px}
.form label{display:block;font-size:13px;color:var(--sub);margin:18px 0 7px;font-weight:500}
.form input[type=text],.form textarea{
  width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;
  background:var(--panel);color:var(--ink);font-size:15px;font-family:inherit;
  outline:none;transition:border-color .15s,box-shadow .15s;
}
.form textarea{
  min-height:380px;line-height:1.8;resize:vertical;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px;
}
.form input:focus,.form textarea:focus{
  border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);
}
.actions{margin-top:22px;display:flex;gap:10px;align-items:center}

/* ── 提示 ── */
.notice{
  padding:12px 16px;border-radius:10px;margin-bottom:20px;font-size:14px;
  background:var(--accent-soft);color:var(--accent-ink);
  border:1px solid var(--line);border-left:3px solid var(--accent);
}
.notice.warn{
  background:var(--warm-soft);color:var(--warm);
  border-left-color:var(--warm);
}

.foot{
  margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
  font-size:12px;color:var(--faint);display:flex;justify-content:space-between;
  gap:10px;flex-wrap:wrap;
}
.hist .row{
  padding:10px 0;border-bottom:1px dashed var(--line);
  font-size:13.5px;color:var(--sub);
}

/* ── 问答（折叠）── */
.qa{
  margin-top:36px;border:1px solid var(--line);border-radius:var(--radius);
  background:var(--panel);box-shadow:var(--shadow);overflow:hidden;
}
.qa>summary{
  cursor:pointer;padding:14px 18px;font-size:14.5px;font-weight:600;
  color:var(--ink);list-style:none;transition:background .15s;
  display:flex;align-items:center;gap:8px;
}
.qa>summary::-webkit-details-marker{display:none}
.qa>summary::after{
  content:"展开";margin-left:auto;font-size:12px;color:var(--sub);font-weight:400;
}
.qa[open]>summary::after{content:"收起"}
.qa>summary:hover{background:var(--soft)}
.qa-list{padding:0 18px;border-top:1px solid var(--line)}
.qa-item{border-bottom:1px dashed var(--line)}
.qa-item:last-child{border-bottom:none}
.qa-item>summary{
  cursor:pointer;padding:13px 0;font-size:14px;color:var(--ink2);list-style:none;
  display:flex;align-items:flex-start;gap:10px;line-height:1.6;
}
.qa-item>summary::-webkit-details-marker{display:none}
.qa-item>summary::before{
  content:"▸";color:var(--accent);font-size:12px;flex-shrink:0;margin-top:3px;
  transition:transform .15s;
}
.qa-item[open]>summary::before{transform:rotate(90deg)}
.qa-q{flex:1;font-weight:600;color:var(--ink);word-break:break-word}
.qa-ok,.qa-wait{
  font-size:11px;padding:1px 8px;border-radius:10px;flex-shrink:0;margin-top:2px;
}
.qa-ok{background:var(--accent-soft);color:var(--accent-ink)}
.qa-wait{background:var(--warm-soft);color:var(--warm)}
.qa-meta{
  display:flex;align-items:center;gap:6px;font-size:12px;color:var(--faint);
  padding:0 0 10px 22px;flex-wrap:wrap;
}
.qa-meta b{color:var(--sub);font-weight:500}
.qa-a{
  margin:0 0 12px 22px;padding:11px 14px;background:var(--panel2);
  border-left:3px solid var(--accent);border-radius:0 9px 9px 0;font-size:13.5px;
}
.qa-a-empty{color:var(--sub);border-left-color:var(--line2)}
.qa-a b{color:var(--ink);font-size:13px}
.qa-a-t{font-size:11.5px;color:var(--faint);margin-left:8px}
.qa-a-b{margin-top:5px;color:var(--ink2);line-height:1.7;white-space:pre-wrap;
  word-break:break-word}
.qa-form{padding:0 0 13px 22px}
.qa-form textarea{
  width:100%;min-height:68px;padding:9px 11px;border:1px solid var(--line);
  border-radius:8px;background:var(--bg);color:var(--ink);font-size:13.5px;
  font-family:inherit;resize:vertical;outline:none;line-height:1.6;
}
.qa-form textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.qa-ask{padding:14px 18px;border-top:1px solid var(--line);background:var(--panel2)}
.qa-ask textarea{
  width:100%;min-height:70px;padding:10px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--bg);color:var(--ink);font-size:14px;
  font-family:inherit;resize:vertical;outline:none;line-height:1.6;
}
.qa-ask textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
/* 表格在窄屏可横向滚动 */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:6px}
.table-wrap table{min-width:560px}

@media (max-width:820px){
  .layout{flex-direction:column}
  .side{
    width:auto;flex:none;height:auto;position:static;border-right:none;
    border-bottom:1px solid var(--line);padding:18px 16px;
  }
  .main{padding:24px 18px 56px}
  .side ul{display:flex;flex-wrap:wrap;gap:4px}
  h1{font-size:26px}
  .article{font-size:15.5px}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none !important;animation:none !important}
}
"""


ADMIN_CSS = """
.admin-head{display:flex;align-items:baseline;gap:14px;margin-bottom:8px}
.admin-head h1{font-size:28px;margin:0}
.admin-head .t{font-size:12.5px;color:var(--sub)}

.tabs{
  display:flex;gap:4px;margin:18px 0 26px;border-bottom:1px solid var(--line);
  padding-bottom:0;flex-wrap:wrap;
}
.tabs a{
  padding:9px 15px;border-radius:9px 9px 0 0;font-size:14px;color:var(--sub);
  transition:background .15s,color .15s;border-bottom:2px solid transparent;
}
.tabs a.on{
  background:var(--accent-soft);color:var(--accent-ink);font-weight:600;
  border-bottom-color:var(--accent);
}
.tabs a:hover{text-decoration:none;color:var(--ink);background:var(--soft)}

.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}
.metric{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px 20px;box-shadow:var(--shadow);transition:box-shadow .18s;
}
.metric:hover{box-shadow:var(--shadow-lg)}
.metric .k{font-size:12.5px;color:var(--sub);margin-bottom:9px;font-weight:500}
.metric .v{font-size:27px;font-weight:800;line-height:1.1;color:var(--ink)}
.metric .u{font-size:13px;font-weight:400;color:var(--sub);margin-left:3px}
.metric .s{font-size:12px;color:var(--faint);margin-top:8px}
.bar{
  height:8px;border-radius:5px;background:var(--soft);overflow:hidden;margin-top:12px;
}
.bar i{display:block;height:100%;border-radius:5px;transition:width .45s ease}
.bar i.ok{background:linear-gradient(90deg,var(--accent),var(--accent2))}
.bar i.warn{background:linear-gradient(90deg,#d9a441,#c9963c)}
.bar i.bad{background:linear-gradient(90deg,#c9564a,#b8493c)}

table.t{width:100%;border-collapse:collapse;font-size:13.5px}
table.t th{
  text-align:left;font-weight:600;color:var(--sub);font-size:12px;
  padding:10px 12px;border-bottom:1px solid var(--line2);
  text-transform:uppercase;letter-spacing:.5px;
}
table.t td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
table.t tbody tr{transition:background .12s}
table.t tbody tr:hover td{background:var(--panel2)}
.cmdblock{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:16px;font-size:12.5px;line-height:1.9;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

.thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(156px,1fr));gap:14px}
.thumb{
  background:var(--panel);border:1px solid var(--line);border-radius:11px;
  overflow:hidden;box-shadow:var(--shadow);transition:box-shadow .18s,transform .18s;
}
.thumb:hover{box-shadow:var(--shadow-lg);transform:translateY(-1px)}
.thumb img{
  width:100%;height:108px;object-fit:cover;display:block;background:var(--soft);
}
.thumb .m{padding:9px 11px;font-size:11.5px;color:var(--sub);line-height:1.55}
.thumb .m b{
  display:block;color:var(--ink);font-size:12px;margin-bottom:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}

.drop{
  border:2px dashed var(--line2);border-radius:var(--radius);padding:30px;
  text-align:center;color:var(--sub);font-size:14px;cursor:pointer;
  transition:border-color .18s,background .18s,color .18s;background:var(--panel);
}
.drop:hover,.drop.over{
  border-color:var(--accent);color:var(--accent-ink);background:var(--accent-soft);
}
.drop b{color:var(--ink)}
#pv{margin-top:16px;display:flex;gap:10px;flex-wrap:wrap}
#pv img{height:86px;border-radius:9px;border:1px solid var(--line);box-shadow:var(--shadow)}
"""



def _js(text: str) -> str:
    """转义成可安全放进 onclick/onsubmit 字符串字面量的文本"""
    return (str(text or "").replace("\\", "\\\\").replace("'", "\\'")
            .replace('"', "&quot;").replace("<", "\\x3c").replace(">", "\\x3e")
            .replace("\n", "\\n").replace("\r", ""))

PAGE_JS = """
<script>
(function () {
  // 提交时禁用按钮，防重复点击
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.dataset.busy === '1') return;
    var btn = form.querySelector('button[type=submit], button:not([type])');
    if (!btn || btn.dataset.noBusy) return;
    form.dataset.busy = '1';
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = '处理中…';
    setTimeout(function () {
      btn.disabled = false;
      btn.textContent = old;
      form.dataset.busy = '';
    }, 8000);
  });

  // 有未保存内容时离开页面给提醒
  var dirty = false;
  document.querySelectorAll('form textarea, form input[type=text]').forEach(function (el) {
    el.addEventListener('input', function () { dirty = true; });
  });
  document.querySelectorAll('form').forEach(function (f) {
    f.addEventListener('submit', function () { dirty = false; });
  });
  window.addEventListener('beforeunload', function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });
})();
</script>
"""

def _csrf_field() -> str:
    """渲染成隐藏表单域；没登录时也给一个，登录后再校验"""
    try:
        tok = csrf_token()
    except Exception:
        tok = ""
    return f'<input type="hidden" name="csrf" value="{tok}">'


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
        signed += ' · <a href="/account">账号安全</a>'
    else:
        signed = '<a href="/login">登录</a> · <a href="/register">注册</a>'

    _html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{SITE_SUB}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
{tailwind_head()}
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
      <span>{SITE_NAME} · 群友共建 · <a href="/privacy">隐私说明</a> · {ui_badge()}</span>
      <span>{datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
    </div>
  </main>
</div>
{PAGE_JS}
</body></html>"""
    return _html.replace("{CSRF_FIELD}", _csrf_field()).replace("{PAGE_JS}", PAGE_JS)


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



# ============================================================
# Q&A（词条下方问答，折叠展示）
# ============================================================
QA_FILE = DATA_DIR / "qa.json"
QA_Q_MAX = 300        # 问题长度上限
QA_A_MAX = 1000       # 回答长度上限
QA_MAX_PER_PAGE = 200  # 单页最多保留多少个问题


def load_qa() -> Dict[str, Any]:
    if not QA_FILE.exists():
        return {"items": {}}
    try:
        data = json.loads(QA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"items": {}}
        data.setdefault("items", {})
        return data
    except Exception as e:
        logger.warning(f"[wiki] 读取问答失败: {e}")
        return {"items": {}}


def save_qa(data: Dict[str, Any]) -> None:
    try:
        tmp = QA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(QA_FILE)
    except Exception as e:
        logger.error(f"[wiki] 保存问答失败: {e}")


def page_qa(name: str) -> List[Dict[str, Any]]:
    """取某个词条下的问答，新的在前"""
    items = (load_qa().get("items") or {}).get(name) or []
    return sorted(items, key=lambda q: -float(q.get("time", 0)))


def ask_question(name: str, question: str, user: Dict[str, Any]) -> Tuple[bool, str]:
    question = (question or "").strip()
    if not question:
        return False, "问题不能为空"
    if len(question) > QA_Q_MAX:
        return False, f"问题太长（上限 {QA_Q_MAX} 字）"
    data = load_qa()
    items = data["items"].setdefault(name, [])
    items.append({
        "id": secrets.token_hex(6),
        "q": question,
        "a": "",
        "asker": str(user.get("username") or "匿名")[:40],
        "asker_uid": user.get("uid", ""),
        "answerer": "",
        "answerer_uid": "",
        "time": _now(),
        "answered": 0,
    })
    if len(items) > QA_MAX_PER_PAGE:
        data["items"][name] = items[-QA_MAX_PER_PAGE:]
    save_qa(data)
    return True, ""


def answer_question(name: str, qid: str, answer: str,
                    user: Dict[str, Any]) -> Tuple[bool, str]:
    """回答一个问题（编辑及以上；提问者本人可补充）"""
    answer = (answer or "").strip()
    if not answer:
        return False, "回答不能为空"
    if len(answer) > QA_A_MAX:
        return False, f"回答太长（上限 {QA_A_MAX} 字）"
    if not can_edit(user):
        return False, "只有编辑或站长可以回答"
    data = load_qa()
    items = data["items"].get(name) or []
    for q in items:
        if q.get("id") == qid:
            q["a"] = answer
            q["answerer"] = str(user.get("username") or "匿名")[:40]
            q["answerer_uid"] = user.get("uid", "")
            q["answered"] = _now()
            save_qa(data)
            return True, ""
    return False, "问题不存在"


def delete_question(name: str, qid: str, user: Dict[str, Any]) -> bool:
    """提问者本人可删；编辑/站长可删任何"""
    data = load_qa()
    items = data["items"].get(name) or []
    keep = []
    removed = False
    for q in items:
        if q.get("id") == qid and (q.get("asker_uid") == user.get("uid")
                                   or can_edit(user)):
            removed = True
            continue
        keep.append(q)
    if removed:
        data["items"][name] = keep
        save_qa(data)
    return removed


def _qa_block(name: str) -> str:
    """词条底部的折叠问答区：问题折叠，点开看回答"""
    user = CUR_USER["v"]
    items = page_qa(name)
    total = len(items)
    answered = sum(1 for q in items if q.get("a"))
    rows = ""
    for q in items:
        qid = _esc(q.get("id"))
        ts = datetime.fromtimestamp(float(q.get("time", 0))).strftime("%m-%d %H:%M")
        has_a = bool(q.get("a"))
        badge = ('<span class="qa-ok">已回答</span>' if has_a
                 else '<span class="qa-wait">待回答</span>')
        # 删除按钮
        can_del = user and (q.get("asker_uid") == user.get("uid") or can_edit(user))
        del_btn = ""
        if can_del:
            del_btn = (f'<form method="post" action="/qa/delete" style="display:inline">'
                       f'{{CSRF_FIELD}}'
                       f'<input type="hidden" name="page" value="{_esc(name)}">'
                       f'<input type="hidden" name="qid" value="{qid}">'
                       f'<button class="mini no" style="padding:2px 8px;font-size:11.5px">'
                       f'删除</button></form>')
        # 回答内容
        if has_a:
            ans = (f'<div class="qa-a"><b>{_esc(q.get("answerer"))}</b>'
                   f'<span class="qa-a-t">回答于 '
                   f'{datetime.fromtimestamp(float(q.get("answered") or 0)).strftime("%m-%d %H:%M")}</span>'
                   f'<div class="qa-a-b">{_esc(q.get("a"))}</div></div>')
        else:
            ans = '<div class="qa-a qa-a-empty">还没有人回答</div>'
        # 回答表单（编辑以上可见）
        form = ""
        if user and can_edit(user):
            form = (f'<form method="post" action="/qa/answer" class="qa-form">{{CSRF_FIELD}}'
                    f'<input type="hidden" name="page" value="{_esc(name)}">'
                    f'<input type="hidden" name="qid" value="{qid}">'
                    f'<textarea name="answer" required maxlength="{QA_A_MAX}" '
                    f'placeholder="写下你的回答…">{_esc(q.get("a") or "")}</textarea>'
                    f'<div style="margin-top:6px"><button class="mini ok" type="submit">'
                    f'{"更新回答" if has_a else "回答"}</button></div></form>')
        rows += (f'<details class="qa-item">'
                 f'<summary><span class="qa-q">{_esc(q.get("q"))}</span>'
                 f'{badge}</summary>'
                 f'<div class="qa-meta">提问：<b>{_esc(q.get("asker"))}</b> · {ts}'
                 f'<span style="margin-left:auto">{del_btn}</span></div>'
                 f'{ans}{form}</details>')
    # 提问表单
    if user:
        ask_form = (f'<form method="post" action="/qa" class="qa-ask">{{CSRF_FIELD}}'
                    f'<input type="hidden" name="page" value="{_esc(name)}">'
                    f'<textarea name="question" required maxlength="{QA_Q_MAX}" '
                    f'placeholder="有疑问？在这里提问（{QA_Q_MAX} 字以内）"></textarea>'
                    f'<div style="margin-top:8px"><button class="btn primary" type="submit">'
                    f'提问</button></div></form>')
    else:
        ask_form = ('<div class="empty" style="padding:18px 0;font-size:13.5px">'
                    '<a href="/login">登录</a> 后可以提问</div>')
    empty = ('<div class="empty" style="padding:18px 0;font-size:13.5px">'
             '还没有提问，有问题就来问</div>')
    return f"""
    <details class="qa">
      <summary>❓ 问答（{total}）{f' · 已答 {answered}' if answered else ''}</summary>
      <div class="qa-list">{rows or empty}</div>
      {ask_form}
    </details>"""



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
                     f'onsubmit="return confirm(\'确定删除「{_js(name)}」？删除后可在回收站恢复\')">{{CSRF_FIELD}}'
                     f'<input type="hidden" name="name" value="{_esc(name)}">'
                     f'<button class="btn" style="color:#b4573c;border-color:#e0b9ad">'
                     f'删除</button></form>')
    inner = (f'<h1>{_esc(name)}</h1>'
             f'<div class="meta"><span>🕒 {updated}</span>'
             f'<span>✏️ {int(page.get("edits", 0))} 次编辑</span>{tags}</div>'
             f'<article class="article">{body_html or "<p style=color:var(--sub)>（还没有正文）</p>"}</article>'
             f'<div class="actions" style="margin-top:30px">{edit_btn}</div>'
             f'{_qa_block(name)}')
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
      {{CSRF_FIELD}}
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
      {{CSRF_FIELD}}
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
    # SVG 是 XML，可能内嵌脚本 —— 落盘前强制消毒
    if ext == ".svg":
        cleaned = sanitize_svg(content)
        if not cleaned:
            return False, "SVG 内容不合法（清洗后不是有效图形）"
        content = cleaned
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
             ("/admin/cookies", "Cookie 分发", "cookies"),
             ("/admin/audit", "操作日志", "audit"),
             ("/admin/static", "静态导出", "static"), ("/admin/mail", "邮件", "mail")]
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
        f'onsubmit="return confirm(\'删除这张图片？\')">{{CSRF_FIELD}}'
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
        onsubmit="return confirm('确定清空媒体库？所有上传图片都会被删除')">{{CSRF_FIELD}}
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
        f'onsubmit="return confirm(\'确定删除词条「{_js(n)}」？\\n删除后不可恢复（建议先导出备份）\')">{{CSRF_FIELD}}'
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
        onsubmit="return confirm('确定清空所有词条？会先放进回收站')">{{CSRF_FIELD}}
        <button class="mini no">清空所有词条</button>
      </form>
    </div>
    <table class="t"><thead><tr><th>标题</th><th>标签</th><th>字数</th>
    <th>编辑</th><th>更新时间</th><th>操作</th></tr></thead>
    <tbody>{rows or '<tr><td colspan=6 style="color:var(--sub)">还没有词条</td></tr>'}</tbody></table>
    """
    return _layout("词条管理", inner)



def view_admin_audit() -> str:
    """操作日志：谁在什么时候做了什么"""
    rows = load_audit(AUDIT_KEEP_DAYS)
    trs = ""
    for r in rows[:300]:
        ts = datetime.fromtimestamp(float(r.get("t", 0))).strftime("%m-%d %H:%M:%S")
        role = ROLE_NAMES.get(str(r.get("role")), str(r.get("role")))
        trs += (f'<tr><td style="white-space:nowrap;color:var(--sub)">{_esc(ts)}</td>'
                f'<td>{_esc(r.get("who"))}<span class="badge {r.get("role")}">{_esc(role)}</span></td>'
                f'<td><b>{_esc(r.get("action"))}</b></td>'
                f'<td>{_esc(r.get("target") or "-")}</td>'
                f'<td style="color:var(--sub);font-size:12.5px">{_esc(r.get("detail") or "")}</td></tr>')
    inner = f"""
    <div class="admin-head"><h1>操作日志</h1>
      <div class="t">最近 {AUDIT_KEEP_DAYS} 天 · {len(rows)} 条</div></div>
    {_admin_tabs("audit")}
    <div class="notice">记录登录、编辑、删除、审批等关键操作，保留 {AUDIT_KEEP_DAYS} 天后自动清理。</div>
    <div class="table-wrap">
    <table class="t"><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>备注</th></tr></thead>
    <tbody>{trs or '<tr><td colspan=5 style="color:var(--sub)">还没有记录</td></tr>'}</tbody></table>
    </div>
    """
    return _layout("操作日志", inner)


def view_account() -> str:
    """账号安全页：改密码、看登录设备、踢掉可疑会话"""
    user = CUR_USER["v"]
    if not user:
        return view_login(err="请先登录")
    uid = user.get("uid", "")
    devices = user_sessions(uid)
    drows = ""
    for d in devices:
        ts = datetime.fromtimestamp(float(d["time"])).strftime("%m-%d %H:%M")
        ua = (d.get("ua") or "未知设备")[:70]
        me = ('<span class="badge editor">当前</span>' if d.get("current") else "")
        act = "" if d.get("current") else (
            f'<form method="post" action="/account/kick" style="display:inline">{{CSRF_FIELD}}'
            f'<input type="hidden" name="token" value="{_esc(d["token"])}">'
            f'<button class="mini no">下线</button></form>')
        drows += (f'<tr><td>{_esc(ua)}{me}</td><td>{_esc(d.get("ip") or "-")}</td>'
                  f'<td>{ts}</td><td>{act}</td></tr>')
    inner = f"""
    <div class="auth" style="max-width:640px">
      <h1>账号安全</h1>
      <div class="sub2">{_esc(user.get("username"))} · {ROLE_NAMES.get(effective_role(user), "")}</div>

      <h2 style="font-size:16px;margin:26px 0 10px">修改密码</h2>
      <form method="post" action="/account/password">{{CSRF_FIELD}}
        <div class="field"><label>当前密码</label>
          <input name="old" type="password" required autocomplete="current-password"></div>
        <div class="field"><label>新密码（至少 8 位，不能纯数字）</label>
          <input name="new" type="password" required minlength="8" autocomplete="new-password"></div>
        <div class="field"><label>确认新密码</label>
          <input name="new2" type="password" required minlength="8" autocomplete="new-password"></div>
        <button type="submit">更新密码</button>
      </form>

      <h2 style="font-size:16px;margin:32px 0 10px">登录设备</h2>
      <div class="table-wrap">
      <table class="t"><thead><tr><th>设备</th><th>IP</th><th>登录时间</th><th></th></tr></thead>
      <tbody>{drows or '<tr><td colspan=4 style="color:var(--sub)">没有记录</td></tr>'}</tbody></table>
      </div>
      <div class="foot2" style="text-align:left;margin-top:16px">
        <form method="post" action="/account/kickall"
          onsubmit="return confirm('确定下线其他所有设备？')">{{CSRF_FIELD}}
          <button class="mini no" type="submit">下线其他所有设备</button>
        </form>
      </div>
      <div class="foot2"><a href="/">返回首页</a></div>
    </div>"""
    return _layout("账号安全", inner)


def view_privacy() -> str:
    """隐私说明：收集什么、存哪里、谁能看"""
    inner = f"""
    <div style="max-width:680px">
      <h1>隐私说明</h1>
      <div class="meta"><span>最后更新：2026-10-18</span></div>
      <article class="article">
        <h2>我们收集什么</h2>
        <ul>
          <li><b>注册信息</b>：用户名、邮箱、密码哈希（不可逆）</li>
          <li><b>登录记录</b>：登录时间、IP 地址、浏览器标识</li>
          <li><b>操作日志</b>：谁在什么时候编辑、删除了什么，保留 {AUDIT_KEEP_DAYS} 天</li>
          <li><b>上传内容</b>：你上传的图片，以及原始文件名</li>
        </ul>

        <h2>数据存在哪里</h2>
        <p>全部存放在本站服务器的 <code>wiki_data/</code> 目录下的 JSON 文件中，
        不上传到任何第三方服务。含隐私的文件权限为 600（仅服务器管理员可读）。</p>

        <h2>谁能看到</h2>
        <ul>
          <li><b>公开可见</b>：词条正文、标签、编辑次数、上传的图片</li>
          <li><b>仅站长可见</b>：你的邮箱、登录 IP、操作日志</li>
          <li><b>任何人都看不到</b>：你的密码（只存哈希，无法还原）</li>
        </ul>

        <h2>我们不做什么</h2>
        <ul>
          <li>不投放广告，不接入第三方统计</li>
          <li>不把数据卖给或分享给任何第三方</li>
          <li>不用你的邮箱发营销邮件</li>
        </ul>

        <h2>你的权利</h2>
        <ul>
          <li><b>删除账号</b>：联系站长，会连同你的会话和登录记录一起清除</li>
          <li><b>导出内容</b>：词条内容可随时复制走</li>
          <li><b>修改密码</b>：登录后在 <a href="/account">账号安全</a> 页自行修改</li>
        </ul>

        <h2>Cookie 用途</h2>
        <p>只用于保持登录状态（<code>wiki_token</code>），不做追踪，不跨站共享。</p>
      </article>
    </div>"""
    return _layout("隐私说明", inner)



# ============================================================
# 本地 WebUI（编辑 + 导出 + 推送）
# ============================================================
LOCAL_CSS = """
.local-wrap{max-width:1000px;margin:0 auto;padding:28px 22px 60px}
.local-head{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.local-head h1{font-size:26px;margin:0}
.local-badge{font-size:11.5px;background:var(--accent-soft);color:var(--accent-ink);
  padding:3px 10px;border-radius:20px}
.local-sub{font-size:13px;color:var(--sub);margin:6px 0 26px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;
  padding:20px 22px;margin-bottom:16px;box-shadow:var(--shadow)}
.panel h2{font-size:15px;margin:0 0 14px;display:flex;align-items:center;gap:8px}
.panel h2 .n{width:22px;height:22px;border-radius:7px;background:var(--accent-soft);
  color:var(--accent-ink);display:grid;place-items:center;font-size:12px;font-weight:700}
.panel p{font-size:13.5px;color:var(--sub);line-height:1.85;margin:0 0 10px}
.panel code{background:var(--soft);padding:2px 8px;border-radius:6px;
  font-size:12.5px;color:var(--accent-ink);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row input[type=text]{flex:1;min-width:240px;padding:10px 13px;border:1px solid var(--line);
  border-radius:9px;background:var(--bg);color:var(--ink);font-size:14px;outline:none}
.row input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.result{margin-top:12px;padding:11px 14px;border-radius:9px;font-size:13.5px;line-height:1.7}
.result.ok{background:var(--accent-soft);color:var(--accent-ink);
  border-left:3px solid var(--accent)}
.result.err{background:var(--warm-soft);color:var(--warm);border-left:3px solid var(--warm)}
.stats{display:flex;gap:22px;font-size:13px;color:var(--sub);margin-top:12px;flex-wrap:wrap}
.stats b{color:var(--ink);font-size:15px}
.steps{counter-reset:s;list-style:none;padding:0;margin:0}
.steps li{counter-increment:s;position:relative;padding:0 0 12px 30px;
  font-size:13.5px;color:var(--sub);line-height:1.8}
.steps li::before{content:counter(s);position:absolute;left:0;top:1px;
  width:20px;height:20px;border-radius:50%;background:var(--soft);
  color:var(--accent-ink);font-size:11.5px;display:grid;place-items:center;font-weight:700}
.mini-list{display:flex;flex-direction:column;gap:2px}
.mini-list a{display:flex;justify-content:space-between;padding:9px 12px;
  border-radius:8px;font-size:14px;color:var(--ink2)}
.mini-list a:hover{background:var(--soft);color:var(--accent);text-decoration:none}
.mini-list a span{font-size:12px;color:var(--faint)}
"""


def view_local(msg: str = "", err: str = "") -> str:
    """本地编辑台：可以编辑词条、导出静态站、推送 GitHub"""
    pages = load_pages().get("pages") or {}
    total = len(pages)
    stat = {}
    try:
        stat = export_stats()
    except Exception:
        stat = {}
    rows = ""
    for n, p in sorted(pages.items()):
        upd = datetime.fromtimestamp(float(p.get("updated", 0))).strftime("%m-%d %H:%M")
        rows += (f'<a href="/edit/{quote(n)}">{_esc(n)}'
                 f'<span>{int(p.get("edits", 0))} 次编辑 · {upd}</span></a>')
    notice = f'<div class="result ok">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="result err">{_esc(err)}</div>' if err else ""
    cfg = load_config()
    repo = str(cfg.get("github_repo") or "")
    branch = str(cfg.get("github_branch") or "gh-pages")
    pages_url = str(cfg.get("pages_url") or "")
    inner = f"""
    <style>{LOCAL_CSS}</style>
    <div class="local-wrap">
      <div class="local-head">
        <h1>{SITE_NAME}</h1>
        <span class="local-badge">本地编辑台</span>
      </div>
      <div class="local-sub">在你自己的电脑上编辑内容，导出成静态站，再一键发布到 GitHub Pages</div>
      {notice}{warn}

      <div class="panel">
        <h2><span class="n">1</span>编辑内容</h2>
        <p>共 <b>{total}</b> 个词条。点下面的词条可以修改，也可以新建。</p>
        <div class="row" style="margin-bottom:14px">
          <a class="btn primary" href="/new">＋ 新建词条</a>
          <a class="btn" href="/">预览站点</a>
        </div>
        <div class="mini-list">{rows or "<p>还没有词条</p>"}</div>
      </div>

      <div class="panel">
        <h2><span class="n">2</span>导出静态站</h2>
        <p>把当前内容生成纯静态 HTML，存到 <code>wiki_data/static/</code>。</p>
        <form method="post" action="/local/export">{{CSRF_FIELD}}
          <button class="btn primary" type="submit">导出静态站</button>
        </form>
        {f'<div class="stats"><span><b>{stat.get("pages", 0)}</b> 个词条</span>' if stat else ""}
        {f'<span><b>{stat.get("files", 0)}</b> 个文件</span>' if stat else ""}
        {f'<span><b>{stat.get("size_kb", 0)}</b> KB</span></div>' if stat else ""}
      </div>

      <div class="panel">
        <h2><span class="n">3</span>发布到 GitHub Pages</h2>
        <p>填你的仓库地址，点推送就会把静态站推到 <code>{branch}</code> 分支。
          需要本机已配置好 git 凭据（SSH key 或 token）。</p>
        <form method="post" action="/local/push">{{CSRF_FIELD}}
          <div class="row" style="margin-bottom:10px">
            <input type="text" name="repo" value="{_esc(repo)}"
                   placeholder="git@github.com:你的用户名/仓库名.git">
          </div>
          <div class="row" style="margin-bottom:12px">
            <input type="text" name="branch" value="{_esc(branch)}"
                   placeholder="gh-pages" style="max-width:200px">
            <input type="text" name="pages_url" value="{_esc(pages_url)}"
                   placeholder="发布后的网址（可选）">
          </div>
          <button class="btn primary" type="submit">推送并发布</button>
        </form>
        {f'<p style="margin-top:14px">发布地址：<a href="{pages_url}" target="_blank">{pages_url}</a></p>' if pages_url else ""}
      </div>

      <div class="panel">
        <h2><span class="n">4</span>首次使用要做的事</h2>
        <ol class="steps">
          <li>去 GitHub 新建一个仓库（可以是私有仓库，Pages 需要公开）</li>
          <li>仓库设置 → Pages → Source 选 <code>gh-pages</code> 分支</li>
          <li>回到这里填上仓库地址，点「推送并发布」</li>
          <li>等 1 分钟，访问 <code>https://你的用户名.github.io/仓库名/</code></li>
        </ol>
        <p style="margin-top:12px">之后再改内容，只要重复「导出 → 推送」两步。</p>
      </div>
    </div>"""
    return _layout("本地编辑台", inner)

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
.auth .hint{font-size:12px;color:var(--faint);margin-top:6px;line-height:1.5}
.auth .capbox{margin-bottom:18px}
.auth .capbox>label{display:block;font-size:12.5px;color:var(--sub);margin-bottom:7px}
.auth .caprow{display:flex;align-items:stretch;gap:9px}
.auth .caprow .q{
  flex:1;background:linear-gradient(135deg,var(--accent-soft),var(--accent2-soft));
  border:1px solid var(--line);border-radius:10px;padding:12px 14px;
  font-size:15px;font-weight:700;color:var(--accent-ink);text-align:center;
  display:flex;align-items:center;justify-content:center;
  font-family:ui-monospace,Menlo,Consolas,monospace;
  letter-spacing:.5px;line-height:1.4;
}
.auth .caprow input{
  width:112px;text-align:center;font-size:16px;font-weight:600;
  padding:11px 8px;border:1px solid var(--line);border-radius:10px;
  background:var(--panel);color:var(--ink);outline:none;
  transition:border-color .15s,box-shadow .15s;
}
.auth .caprow input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
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


def view_register(msg: str = "", err: str = "", ip: str = "") -> str:
    q, tok = new_captcha(ip)
    if not q:
        err = err or "请求太频繁，请稍等一分钟再试"
    notice = f'<div class="notice">{_esc(msg)}</div>' if msg else ""
    warn = f'<div class="notice warn">{_esc(err)}</div>' if err else ""
    # 常见邮箱域名，给浏览器一个提示列表
    prov_hint = "、".join(sorted({v["name"] for v in MAIL_PROVIDERS.values()}))[:120]
    inner = f"""
    <div class="auth">
      <h1>注册账号</h1>
      <div class="sub2">注册后可申请 Cookie，拿到 Cookie 才能编辑百科</div>
      {notice}{warn}
      <form method="post" action="/register">{{CSRF_FIELD}}
        <div class="field">
          <label>用户名</label>
          <input name="username" required minlength="3" maxlength="20"
                 autocomplete="username" placeholder="3~20 位，字母/数字/下划线/中文">
        </div>
        <div class="field">
          <label>邮箱</label>
          <input name="email" type="email" required autocomplete="email"
                 placeholder="用于接收验证码">
          <div class="hint">支持 QQ、163、126、Gmail、Outlook 等常见邮箱</div>
        </div>
        <div class="field">
          <label>密码</label>
          <input name="password" type="password" required minlength="8"
                 autocomplete="new-password" placeholder="至少 8 位，不能是纯数字">
        </div>
        <div class="field">
          <label>确认密码</label>
          <input name="password2" type="password" required minlength="8"
                 autocomplete="new-password" placeholder="再输一次">
        </div>
        <div class="capbox">
          <label>人机验证</label>
          <div class="caprow">
            <div class="q">{_esc(q) or "请求过于频繁"}</div>
            <input name="captcha" required placeholder="答案" autocomplete="off">
            <input type="hidden" name="ctoken" value="{_esc(tok)}">
          </div>
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
      <form method="post" action="/login" autocomplete="on">{{CSRF_FIELD}}
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
      <form method="post" action="/login-cookie">{{CSRF_FIELD}}
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
        <form method="post" action="/apply">{{CSRF_FIELD}}
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
                 f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
                 f'<input type="hidden" name="uid" value="{_esc(it["uid"])}">'
                 f'<input type="hidden" name="action" value="approve">'
                 f'<button class="mini ok">批准</button></form>'
                 f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
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
            acts = (f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
                    f'<input type="hidden" name="uid" value="{uid_v}">'
                    f'<input type="hidden" name="action" value="grant">'
                    f'<button class="mini ok">直接发放</button></form>'
                    f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
                    f'<input type="hidden" name="uid" value="{uid_v}">'
                    f'<input type="hidden" name="action" value="revoke">'
                    f'<button class="mini no">收回</button></form>')
            if role != ROLE_EDITOR:
                acts += (f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
                         f'<input type="hidden" name="uid" value="{uid_v}">'
                         f'<input type="hidden" name="action" value="set_editor">'
                         f'<button class="mini">设为编辑</button></form>')
            if role != ROLE_GUEST:
                acts += (f'<form method="post" action="/admin/cookie">{{CSRF_FIELD}}'
                         f'<input type="hidden" name="uid" value="{uid_v}">'
                         f'<input type="hidden" name="action" value="set_guest">'
                         f'<button class="mini">降为访客</button></form>')
            acts += (f'<form method="post" action="/admin/cookie" '
                     f'onsubmit="return confirm(\'删除账号 {_js(u.get("username"))}？'
                     f'该账号会话会立即失效，不可恢复\')">{{CSRF_FIELD}}'
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
# 静态导出（给 GitHub Pages / 任意静态托管用）
# ============================================================
# 纯静态站点没有服务端，所以：
#   * 词条预渲染成 /p/<name>/index.html
#   * 搜索改成前端脚本 + 预生成的 search-index.json
#   * 登录/编辑/上传/问答等动态功能不出现在导出站
#   * 全部相对链接，放子目录（user.github.io/wiki/）也能用
#
# 内存友好：逐条渲染、立即写盘、不缓存整站 HTML。
# 峰值内存约等于「单个最大词条的正文」，数千词条也不会爆。

STATIC_SEARCH_MAX = 500       # 搜索索引最多收录多少词条
STATIC_PAGE_CHUNK = 50       # 每处理多少条落一次盘（控制内存）


STATIC_CSS = """
.side-tip{margin-top:22px;font-size:11.5px;color:var(--faint);
  border-top:1px solid var(--line);padding-top:12px;line-height:1.6}
.search-result{padding:14px 0;border-bottom:1px solid var(--line)}
.search-result:last-child{border-bottom:none}
.search-result a{font-size:17px;font-weight:650;color:var(--ink)}
.search-result a:hover{color:var(--accent)}
.search-result .ex{margin-top:5px;font-size:13.5px;color:var(--sub);line-height:1.7}
.search-result mark{background:var(--accent-soft);color:var(--accent-ink);
  padding:1px 3px;border-radius:3px}
#no-hit{color:var(--sub);text-align:center;padding:50px 0}
.search-big{width:100%;padding:13px 16px;border:1px solid var(--line);
  border-radius:10px;background:var(--panel);color:var(--ink);font-size:15px;
  outline:none;margin:16px 0 20px;box-sizing:border-box}
.search-big:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search-results{margin-top:6px}
.hist-row{display:flex;justify-content:space-between;gap:10px;padding:11px 0;
  border-bottom:1px dashed var(--line);font-size:14px}
.hist-row a{color:var(--ink)}.hist-row a:hover{color:var(--accent)}
.hist-row span{font-size:12.5px;color:var(--faint);white-space:nowrap}
.tag-group{margin-bottom:26px}
.tag-group h2{font-size:16.5px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
.tag-count{font-size:11.5px;background:var(--soft);color:var(--sub);
  padding:1px 8px;border-radius:10px;font-weight:400}
.tag-links{display:flex;flex-wrap:wrap;gap:8px}
.tag-links a{font-size:13.5px;padding:5px 12px;border-radius:8px;
  background:var(--panel);border:1px solid var(--line);color:var(--ink2)}
.tag-links a:hover{border-color:var(--accent);color:var(--accent)}
"""


def _safe_dirname(name: str) -> str:
    """词条名转成安全的目录名：保留中文，去掉路径分隔符和控制字符"""
    raw = str(name or "").strip()
    raw = raw.replace("/", "_").replace("\\", "_").replace("\x00", "")
    raw = re.sub(r'[<>:"|?*]', "_", raw)
    raw = raw.strip(". ") or "untitled"
    return raw[:120]

def _static_layout(title: str, inner: str, crumb: str = "",
                   depth: int = 0) -> str:
    """静态站布局：相对链接、无动态功能入口"""
    up = "../" * depth
    total = len((load_pages().get("pages") or {}))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} · {SITE_NAME}</title>
<meta name="description" content="{SITE_SUB}">
<link rel="icon" type="image/svg+xml" href="{up}favicon.svg">
<style>{CSS}{STATIC_CSS}</style>
</head><body>
<div class="layout">
  <aside class="side">
    <div class="logo"><img src="{up}favicon.svg" alt="" class="logo-img">星海<span>百科</span></div>
    <div class="sub">{SITE_SUB} · {total} 个词条</div>
    <form class="search" action="{up}search/index.html" method="get" onsubmit="return staticSearch(this)">
      <input name="q" placeholder="搜索词条…" autocomplete="off">
      <button type="submit">搜</button>
    </form>
    <nav class="nav">
      <a href="{up}index.html">首页</a>
      <a href="{up}all/index.html">全部词条</a>
      <a href="{up}tags/index.html">标签</a>
      <a href="{up}recent/index.html">最近更新</a>
    </nav>
    <div class="side-tip">静态只读版<br>由 WIKI.HS 导出</div>
  </aside>
  <main class="main">
    {f'<div class="crumb">{crumb}</div>' if crumb else ''}
    {inner}
    <div class="foot">
      <span>{SITE_NAME} · 静态导出</span>
      <span>{datetime.now().strftime("%Y-%m-%d")}</span>
    </div>
  </main>
</div>
<script src="{up}search.js"></script>
</body></html>"""


def _static_card(name: str, page: Dict[str, Any], depth: int = 0) -> str:
    """静态卡片（相对链接）"""
    title = _esc(name)
    text = re.sub(r"\s+", " ", str(page.get("body") or ""))
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|`|>|\[|\]", "", text).strip()[:110]
    return (f'<a class="card" href="{("../" * depth)}p/{quote(name)}/index.html">'
            f'<div class="t">{title}</div>'
            f'<div class="d">{_esc(text)}</div></a>')


def _static_article(name: str, page: Dict[str, Any]) -> str:
    """词条正文（不含编辑按钮和问答）"""
    tags = "".join(f'<a class="tag" href="../../tags/index.html">{_esc(t)}</a>'
                   for t in (page.get("tags") or []))
    updated = datetime.fromtimestamp(
        float(page.get("updated", _now()))).strftime("%Y-%m-%d %H:%M")
    body_html = md_to_html(page.get("body") or "")
    return (f'<h1>{_esc(name)}</h1>'
            f'<div class="meta"><span>🕒 {updated}</span>'
            f'<span>✏️ {int(page.get("edits", 0))} 次编辑</span>{tags}</div>'
            f'<article class="article">{body_html or "<p>（还没有正文）</p>"}</article>')



def export_stats() -> Dict[str, Any]:
    """看上次导出的统计（没导出返回空）"""
    d = DATA_DIR / "static"
    if not d.exists():
        return {}
    files = [f for f in d.rglob("*") if f.is_file()]
    pages = len(list((d / "p").iterdir())) if (d / "p").exists() else 0
    return {"pages": pages, "files": len(files),
            "size_kb": round(sum(f.stat().st_size for f in files) / 1024, 1)}

def export_static(out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把当前内容导出成纯静态站点

    内存策略：逐条渲染 → 立即写盘 → 不保留 HTML 引用，
    峰值内存只与「最大单篇正文」相关，不随词条总数线性增长。
    """
    target = Path(out_dir) if out_dir else (DATA_DIR / "static")
    if target.exists():
        import shutil
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    pages = load_pages().get("pages") or {}
    total = len(pages)
    files = 0

    def write(rel: str, html: str) -> None:
        nonlocal files
        f = target / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(html, encoding="utf-8")
        files += 1

    # ── 首页 ──
    if pages:
        recent = sorted(pages.items(),
                        key=lambda kv: -float(kv[1].get("updated", 0)))[:12]
        cards = "".join(_static_card(n, p) for n, p in recent)
        home = (f'<h1>{SITE_NAME}</h1>'
                f'<div class="meta"><span>{total} 个词条</span>'
                f'<span>{SITE_SUB}</span></div>'
                f'<div class="cards">{cards}</div>')
    else:
        home = '<h1>还没有词条</h1><div class="empty">导出时数据是空的</div>'
    write("index.html", _static_layout("首页", home))

    # ── 词条（逐条写，不累积）──
    for i, (name, page) in enumerate(pages.items(), 1):
        write(f"p/{_safe_dirname(name)}/index.html",
              _static_layout(name, _static_article(name, page),
                             crumb=f'<a href="../../index.html">首页</a> / {_esc(name)}',
                             depth=2))
        # 每 N 条回收一次引用，避免大字典长期驻留
        if i % STATIC_PAGE_CHUNK == 0:
            page = None  # noqa

    # ── 全部词条 ──
    all_cards = "".join(_static_card(n, p) for n, p in sorted(pages.items()))
    write("all/index.html", _static_layout(
        "全部词条",
        f'<h1>全部词条</h1><div class="meta"><span>{total} 个</span></div>'
        f'<div class="cards">{all_cards or "<div class=empty>暂无词条</div>"}</div>',
        depth=1))
    del all_cards

    # ── 标签 ──
    tag_map: Dict[str, List[str]] = {}
    for n, p in pages.items():
        for t in (p.get("tags") or []):
            tag_map.setdefault(t, []).append(n)
    tag_html = ""
    for t in sorted(tag_map):
        links = "".join(f'<a href="../../p/{quote(n)}/index.html">{_esc(n)}</a>'
                        for n in sorted(tag_map[t]))
        tag_html += (f'<div class="tag-group"><h2>{_esc(t)}'
                     f'<span class="tag-count">{len(tag_map[t])}</span></h2>'
                     f'<div class="tag-links">{links}</div></div>')
    write("tags/index.html", _static_layout(
        "标签", f'<h1>标签</h1><div class="meta"><span>{len(tag_map)} 个标签</span></div>'
        f'{tag_html or "<div class=empty>还没有标签</div>"}', depth=1))
    del tag_html, tag_map

    # ── 最近更新 ──
    recent = sorted(pages.items(), key=lambda kv: -float(kv[1].get("updated", 0)))[:100]
    rows = "".join(
        f'<div class="hist-row"><a href="../../p/{quote(n)}/index.html">{_esc(n)}</a>'
        f'<span>{datetime.fromtimestamp(float(p.get("updated", 0))).strftime("%Y-%m-%d %H:%M")}</span></div>'
        for n, p in recent)
    write("recent/index.html", _static_layout(
        "最近更新", f'<h1>最近更新</h1><div class="recent-list">{rows}</div>', depth=1))
    del rows, recent

    # ── 搜索索引 ──
    index: List[Dict[str, str]] = []
    for n, p in list(pages.items())[:STATIC_SEARCH_MAX]:
        body = re.sub(r"\s+", " ", str(p.get("body") or ""))
        body = re.sub(r"[#*`>\[\]]", "", body).strip()[:150]
        index.append({"t": n, "u": f"../p/{quote(n)}/index.html",
                      "b": body, "g": " ".join(p.get("tags") or [])})
    (target / "search-index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    files += 1
    del index

    write("search/index.html", _static_layout(
        "搜索",
        '<h1>搜索</h1>'
        '<div class="meta"><span>在标题、摘要、标签里找</span></div>'
        '<input id="sq" class="search-big" placeholder="输入关键词…" autocomplete="off">'
        '<div id="sr" class="search-results"></div>'
        '<div id="no-hit" style="display:none">没有找到相关词条</div>',
        depth=1))

    # ── 搜索脚本 ──
    (target / "search.js").write_text(STATIC_SEARCH_JS, encoding="utf-8")
    files += 1

    # ── 图标 ──
    fav = DATA_DIR / "favicon.svg"
    if fav.exists():
        (target / "favicon.svg").write_bytes(fav.read_bytes())
        files += 1

    size_kb = round(sum(f.stat().st_size for f in target.rglob("*")
                        if f.is_file()) / 1024, 1)
    return {"dir": str(target), "pages": total, "files": files, "size_kb": size_kb}
STATIC_SEARCH_JS = """
(function () {
  window.staticSearch = function (form) {
    var q = (form.q.value || "").trim();
    if (!q) { return false; }
    var depth = /\\/p\\//.test(location.pathname) ? "../../" : "../";
    location.href = depth + "search/index.html?q=" + encodeURIComponent(q);
    return false;
  };

  var input = document.getElementById("sq");
  if (!input) { return; }
  var box = document.getElementById("sr");
  var none = document.getElementById("no-hit");
  var data = [];

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function highlight(text, q) {
    var low = text.toLowerCase();
    var out = "", pos = 0, at = low.indexOf(q);
    var guard = 0;
    while (at >= 0 && out.length < 500 && guard < 30) {
      out += text.slice(pos, at) + "<mark>" + text.slice(at, at + q.length) + "</mark>";
      pos = at + q.length;
      at = low.indexOf(q, pos);
      guard++;
    }
    return out + text.slice(pos);
  }

  function render() {
    var q = (input.value || "").trim().toLowerCase();
    if (!q) { box.innerHTML = ""; none.style.display = "none"; return; }
    var html = "", hits = 0;
    for (var i = 0; i < data.length && hits < 40; i++) {
      var it = data[i];
      if ((it.t + " " + it.b + " " + it.g).toLowerCase().indexOf(q) >= 0) {
        hits++;
        html += '<div class="search-result"><a href="' + it.u + '">' +
                esc(it.t) + '</a><div class="ex">' + highlight(esc(it.b), q) +
                "</div></div>";
      }
    }
    none.style.display = hits ? "none" : "block";
    box.innerHTML = html;
  }

  fetch("../search-index.json")
    .then(function (r) { return r.json(); })
    .then(function (d) { data = d; render(); })
    .catch(function () { none.style.display = "block"; });

  input.addEventListener("input", render);
  var p = new URLSearchParams(location.search);
  if (p.get("q")) { input.value = p.get("q"); }
  render();
})();
"""



# ============================================================
# 本地桌面版（下载者在自己电脑上跑）
# ============================================================
# 用法：
#   python wiki.py --local            启动本地 WebUI（自动开浏览器）
#   python wiki.py --local --port 8080
#   python wiki.py --export 输出目录    只在命令行导出静态站
#
# 本地模式特点：
#   * 不需要 NoneBot，不需要邮箱、不需要账号体系
#   * 只能用本机访问（127.0.0.1），不对外暴露
#   * 编辑完点「导出」生成静态站，再点「推送」发到你的 GitHub Pages

LOCAL_HOST = "127.0.0.1"


def is_local_mode() -> bool:
    """当前是不是本地桌面版"""
    return bool(os.environ.get("WIKI_LOCAL"))


def local_owner() -> Dict[str, Any]:
    """本地模式下的伪站长（无需登录）"""
    return {"uid": "local", "username": "本机用户", "role": ROLE_OWNER,
            "email": "", "local": True}


def push_static_to_github(repo: str, branch: str = "gh-pages",
                          message: str = "") -> Tuple[bool, str]:
    """把导出的静态站推送到 GitHub（用本机已配置的 git 凭据）

    repo 形如 git@github.com:user/repo.git 或 https://github.com/user/repo.git
    """
    import shutil
    import subprocess
    import tempfile

    src = DATA_DIR / "static"
    if not src.exists():
        return False, "还没有导出，请先点「导出静态站」"
    if not repo.strip():
        return False, "请填写仓库地址"

    work = Path(tempfile.mkdtemp(prefix="wiki-push-"))
    try:
        def run(*args, **kw):
            return subprocess.run(args, cwd=str(work), capture_output=True,
                                  text=True, timeout=kw.pop("timeout", 120))

        run("git", "init", "-q")
        run("git", "config", "user.name", "wiki-hs")
        run("git", "config", "user.email", "wiki-hs@local")
        # 复制静态站
        for f in src.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src)
                dst = work / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
        (work / ".nojekyll").write_text("", encoding="utf-8")
        run("git", "add", "-A")
        r = run("git", "commit", "-q", "-m",
                message or f"docs: 更新静态站 {datetime.now():%Y-%m-%d %H:%M}")
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return False, f"提交失败：{(r.stderr or r.stdout)[:200]}"
        run("git", "branch", "-M", branch)
        run("git", "remote", "add", "origin", repo.strip())
        r = run("git", "push", "-f", "origin", branch, timeout=180)
        if r.returncode != 0:
            err = (r.stderr or r.stdout)[:300]
            return False, f"推送失败：{err}"
        return True, f"已推送到 {branch} 分支"
    except subprocess.TimeoutExpired:
        return False, "推送超时，请检查网络或仓库地址"
    except Exception as e:
        return False, f"出错：{str(e)[:200]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)



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



def _chmod600(path: Path) -> None:
    """含隐私的文件只给所有者读写"""
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except Exception:
        pass


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
    _chmod600(USERS_FILE)


def load_sessions() -> Dict[str, Any]:
    data = _load_json(SESSION_FILE, {"sessions": {}})
    data.setdefault("sessions", {})
    return data


def save_sessions(data: Dict[str, Any]) -> None:
    _save_json(SESSION_FILE, data)
    _chmod600(SESSION_FILE)


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


# 常见邮箱服务商的 SMTP 参数，用户只需填账号和授权码
MAIL_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "qq.com": {"host": "smtp.qq.com", "port": 465, "ssl": True, "name": "QQ邮箱"},
    "foxmail.com": {"host": "smtp.qq.com", "port": 465, "ssl": True, "name": "Foxmail"},
    "vip.qq.com": {"host": "smtp.qq.com", "port": 465, "ssl": True, "name": "QQ邮箱VIP"},
    "163.com": {"host": "smtp.163.com", "port": 465, "ssl": True, "name": "网易163"},
    "126.com": {"host": "smtp.126.com", "port": 465, "ssl": True, "name": "网易126"},
    "yeah.net": {"host": "smtp.yeah.net", "port": 465, "ssl": True, "name": "网易yeah"},
    "sina.com": {"host": "smtp.sina.com", "port": 465, "ssl": True, "name": "新浪邮箱"},
    "sohu.com": {"host": "smtp.sohu.com", "port": 465, "ssl": True, "name": "搜狐邮箱"},
    "aliyun.com": {"host": "smtp.aliyun.com", "port": 465, "ssl": True, "name": "阿里云邮箱"},
    "gmail.com": {"host": "smtp.gmail.com", "port": 465, "ssl": True, "name": "Gmail"},
    "outlook.com": {"host": "smtp.office365.com", "port": 587, "ssl": False, "name": "Outlook"},
    "hotmail.com": {"host": "smtp.office365.com", "port": 587, "ssl": False, "name": "Hotmail"},
    "live.com": {"host": "smtp.office365.com", "port": 587, "ssl": False, "name": "Live"},
    "yahoo.com": {"host": "smtp.mail.yahoo.com", "port": 465, "ssl": True, "name": "Yahoo"},
    "icloud.com": {"host": "smtp.mail.me.com", "port": 587, "ssl": False, "name": "iCloud"},
    "139.com": {"host": "smtp.139.com", "port": 465, "ssl": True, "name": "移动139"},
    "189.cn": {"host": "smtp.189.cn", "port": 465, "ssl": True, "name": "电信189"},
    "wo.cn": {"host": "smtp.wo.cn", "port": 465, "ssl": True, "name": "联通沃邮箱"},
}


def mail_provider(email: str) -> Optional[Dict[str, Any]]:
    """根据邮箱域名推断 SMTP 服务商"""
    domain = (email or "").split("@")[-1].lower().strip()
    if not domain:
        return None
    if domain in MAIL_PROVIDERS:
        return MAIL_PROVIDERS[domain]
    # 企业邮箱：尝试 mail.<domain> 作为 SMTP
    return None


def _smtp_config() -> Dict[str, str]:
    """取 SMTP 配置；缺 host 时按 from/user 的域名自动推断"""
    cfg = {k: str(v) for k, v in (load_config().get("smtp") or {}).items()}
    if not cfg.get("host"):
        guess = mail_provider(cfg.get("from") or cfg.get("user") or "")
        if guess:
            cfg["host"] = guess["host"]
            cfg["port"] = cfg.get("port") or str(guess["port"])
            cfg["ssl"] = cfg.get("ssl") or ("1" if guess["ssl"] else "0")
            cfg["_provider"] = guess["name"]
    return cfg


def smtp_status() -> str:
    """给后台显示的发信状态描述"""
    cfg = _smtp_config()
    if not cfg.get("host"):
        return "未配置（验证码写在本地 outbox，可在后台查看）"
    name = cfg.get("_provider") or cfg.get("host")
    return f"{name} · {cfg.get('host')}:{cfg.get('port')}"


def render_code_mail(to: str, code: str, username: str) -> str:
    return (f"【{SITE_NAME}】注册验证码\n\n"
            f"{username} 你好，你的验证码是：{code}\n"
            f"有效期 {CODE_TTL // 60} 分钟，请勿转告他人。\n\n"
            f"如果不是你本人操作，忽略本邮件即可。\n"
            f"\n--\n{SITE_NAME} {PUBLIC_URL}")


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
            err = str(e)[:200]
            logger.warning(f"[wiki] 发邮件失败: {err}")
            _MAIL.update({"last": _now(), "error": err})
            # 失败也落 outbox，保证用户不会卡在验证页
            try:
                fn = MAIL_DIR / f"{int(_now())}_{secrets.token_hex(3)}.txt"
                fn.write_text(f"To: {to}\n失败原因: {err}\n\n{text}\n", encoding="utf-8")
            except Exception:
                pass
            return True, f"发信失败（{err[:40]}），已写入本地 outbox，请联系站长"
    try:
        fn = MAIL_DIR / f"{int(_now())}_{secrets.token_hex(3)}.txt"
        fn.write_text(f"To: {to}\n\n{text}\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"[wiki] 写 outbox 失败: {e}")
    return True, "验证码已生成（未配置 SMTP，可让站长在后台「邮件」里查看）"


def new_email_code() -> str:
    return "".join(random.choice("0123456789") for _ in range(CODE_LEN))


# ── 人机验证 ──
# 三道题随机出，答案只在服务端内存里，token 一次性、5 分钟过期。
# 另加「同 IP 每分钟最多出 20 题」的限制，防脚本刷题。
CAPTCHA_ISSUE: Dict[str, List[float]] = {}
CAPTCHA_ISSUE_PER_MIN = 20

CN_DIGITS = "零一二三四五六七八九"


def _num_cn(n: int) -> str:
    """小数字转中文，让机器视觉识别更难，人眼反而更简单"""
    if n < 10:
        return CN_DIGITS[n]
    if n < 20:
        return "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    return CN_DIGITS[n // 10] + "十" + (CN_DIGITS[n % 10] if n % 10 else "")


def new_captcha(ip: str = "") -> Tuple[str, str]:
    """出一道验证题，返回 (题干, token)

    题型随机：
      1. 算术：中文数字运算，例如「十二 加 七 = ?」
      2. 填空：给出数列规律，例如「3, 6, 9, 12, ?」
      3. 文字：从一串字符里找第 N 个，例如「甲7乙9丙2，第3个是什么」
    """
    # 同 IP 出题限流
    if ip:
        now = _now()
        hist = [t for t in CAPTCHA_ISSUE.get(ip, []) if now - t < 60]
        if len(hist) >= CAPTCHA_ISSUE_PER_MIN:
            return "", ""
        hist.append(now)
        CAPTCHA_ISSUE[ip] = hist

    kind = random.choice(["arith", "arith", "series", "find"])
    if kind == "arith":
        op = random.choice(["+", "-", "×"])
        if op == "×":
            a, b = random.randint(2, 9), random.randint(2, 9)
            ans = a * b
            q = f"{_num_cn(a)} 乘 {_num_cn(b)} = ?"
        else:
            a, b = random.randint(6, 20), random.randint(2, 12)
            if op == "-":
                a, b = max(a, b), min(a, b)
                ans = a - b
                q = f"{_num_cn(a)} 减 {_num_cn(b)} = ?"
            else:
                ans = a + b
                q = f"{_num_cn(a)} 加 {_num_cn(b)} = ?"
    elif kind == "series":
        start = random.randint(1, 6)
        step = random.randint(2, 5)
        nums = [start + step * i for i in range(4)]
        ans = nums[3]
        q = "，".join(str(x) for x in nums[:3]) + "，?  （每次加 " + str(step) + "）"
    else:
        pool = "甲乙丙丁戊己庚辛壬癸"
        letters = random.sample(pool, 4)
        digits = [random.randint(1, 9) for _ in range(4)]
        seq = "".join(f"{letters[i]}{digits[i]}" for i in range(4))
        pos = random.randint(1, 4)
        ans = digits[pos - 1]
        q = f"{seq} 中第 {_num_cn(pos)} 个数字是几？"

    token = secrets.token_urlsafe(12)
    _CAPTCHA[token] = {"ans": str(ans), "time": _now(), "kind": kind}
    # 清理过期题，避免内存无限增长
    for k in [k for k, v in _CAPTCHA.items() if _now() - v["time"] > CAPTCHA_TTL]:
        _CAPTCHA.pop(k, None)
    return q, token


def check_captcha(token: str, answer: str) -> Tuple[bool, str]:
    item = _CAPTCHA.pop((token or "").strip(), None)
    if not item:
        return False, "验证已过期，请刷新重试"
    if _now() - item["time"] > CAPTCHA_TTL:
        return False, "验证已过期，请刷新重试"
    got = str(answer or "").strip().replace(" ", "")
    # 允许填阿拉伯数字，也允许填中文数字
    norm = got
    if norm and all(ch in CN_DIGITS + "十" for ch in norm):
        # 中文数字转回阿拉伯数字
        try:
            if "十" in norm:
                left, _, right = norm.partition("十")
                val = (CN_DIGITS.index(left) if left else 1) * 10
                val += CN_DIGITS.index(right) if right else 0
                norm = str(val)
            else:
                norm = str(CN_DIGITS.index(norm))
        except Exception:
            pass
    if norm != str(item["ans"]):
        return False, "答案不对，再试一次"
    return True, ""


SESSION_MAX_PER_USER = 3      # 同一账号最多保留几个登录设备


def new_session(user: Dict[str, Any], ip: str = "", ua: str = "") -> str:
    """签发会话。

    * 会话固定防护：每次登录都生成全新 token；
    * 设备数限制：超过 SESSION_MAX_PER_USER 时踢掉最旧的；
    * 过期会话顺带清理，避免文件无限增长。
    """
    token = secrets.token_urlsafe(32)
    uid = user["uid"]
    with _LOCK:
        data = load_sessions()
        sessions = data.get("sessions") or {}
        # 清理过期
        cutoff = _now() - COOKIE_MAX_AGE
        sessions = {k: v for k, v in sessions.items()
                    if float(v.get("time", 0)) > cutoff}
        # 同一账号设备数上限：保留最新的 N-1 个，再加当前这个
        mine = sorted([(k, v) for k, v in sessions.items() if v.get("uid") == uid],
                      key=lambda kv: -float(kv[1].get("time", 0)))
        for k, _v in mine[SESSION_MAX_PER_USER - 1:]:
            sessions.pop(k, None)
        sessions[token] = {"uid": uid,
                           "username": user.get("username", ""),
                           "time": _now(),
                           "ip": (ip or "")[:64],
                           "ua": (ua or "")[:200]}
        data["sessions"] = sessions
        save_sessions(data)
    return token


def user_sessions(uid: str) -> List[Dict[str, Any]]:
    """列出某账号当前登录的设备"""
    out: List[Dict[str, Any]] = []
    for tok, v in (load_sessions().get("sessions") or {}).items():
        if v.get("uid") != uid:
            continue
        out.append({"token": tok,
                    "time": float(v.get("time", 0)),
                    "ip": v.get("ip", ""),
                    "ua": v.get("ua", ""),
                    "current": tok == (CUR_SESSION.get("v") or "")})
    return sorted(out, key=lambda x: -x["time"])


def kill_session(token: str) -> None:
    with _LOCK:
        data = load_sessions()
        data.get("sessions", {}).pop(token, None)
        save_sessions(data)


def kill_user_sessions(uid: str, keep: str = "") -> int:
    """踢掉某账号所有会话（可保留一个），返回踢掉的数量"""
    with _LOCK:
        data = load_sessions()
        sessions = data.get("sessions") or {}
        victims = [k for k, v in sessions.items()
                   if v.get("uid") == uid and k != keep]
        for k in victims:
            sessions.pop(k, None)
        data["sessions"] = sessions
        save_sessions(data)
        return len(victims)


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
        audit("批准 Cookie", rec.get("username", ""))
        return True, f"已批准，Cookie：{cookie}"
    rec["cookie_request"] = {**req, "status": "rejected", "time": _now()}
    save_users(users)
    logger.info(f"[wiki] Cookie 被拒绝：{rec.get('username')}")
    audit("拒绝 Cookie", rec.get("username", ""))
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


def _is_https(req: Request) -> bool:
    """判断当前请求是否走 HTTPS（决定 cookie 要不要加 secure）"""
    proto = req.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return req.url.scheme == "https"


# 全局安全响应头（CSP 按 UI 模式动态生成，见 csp_policy()）
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """每个请求解析一次身份：会话 Cookie → 用户；同时兼容旧口令"""
    token = request.cookies.get(COOKIE_NAME, "")
    CUR_SESSION["v"] = token
    # 本地模式：无需登录，直接是站长
    if is_local_mode():
        IS_EDITOR["v"] = True
        CUR_USER["v"] = local_owner()
        resp = await call_next(request)
        resp.headers.update(SECURITY_HEADERS)
        resp.headers["Content-Security-Policy"] = csp_policy()
        return resp
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
            resp.headers["Content-Security-Policy"] = csp_policy()
            if _is_https(request):
                resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return resp
    IS_EDITOR["v"] = False
    CUR_USER["v"] = user
    resp = await call_next(request)
    resp.headers.update(SECURITY_HEADERS)
    resp.headers["Content-Security-Policy"] = csp_policy()
    # 只在 HTTPS 下加 HSTS，避免 HTTP 环境把自己锁死
    if _is_https(request):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp



# ── CSRF ──
# 站点内所有 POST 表单都带一个隐藏字段，值从会话派生。
# 校验失败直接拒绝，防止第三方页面替用户提交。
def csrf_token() -> str:
    """由站长口令 + 当前会话派生，同一会话内稳定"""
    user = CUR_USER["v"] or {}
    if is_local_mode():
        return "local-mode"
    seed = str(user.get("uid") or "") + "|" + str(current_token() or "")
    return hmac.new(seed.encode("utf-8"), b"csrf", hashlib.sha256).hexdigest()[:32]


def csrf_ok(form_token: str) -> bool:
    expect = csrf_token()
    if not expect:
        return False
    return hmac.compare_digest(str(form_token or ""), expect)


def _csrf_guard(form_token: str) -> Optional[HTMLResponse]:
    """已登录用户的 POST 一律校验 CSRF；登录/注册阶段还没有会话，跳过"""
    # 本地桌面版只监听 127.0.0.1，不存在跨站风险，直接放行
    if is_local_mode():
        return None
    user = CUR_USER["v"]
    if not user:
        return None
    if csrf_ok(form_token):
        return None
    return HTMLResponse(
        _layout("请求被拒绝",
                '<div class="notice warn">页面已过期或来源不合法，请刷新后重试。</div>'),
        status_code=403)


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
    if is_local_mode():
        return RedirectResponse("/local", status_code=303)
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
                         body: str = Form(...), csrf: str = Form("")) -> HTMLResponse:
    blocked = _csrf_guard(csrf) or _require_editor()
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
    audit("新建词条", title)
    return RedirectResponse("/p/" + quote(title), status_code=303)


@app.get("/edit/{name}", response_class=HTMLResponse)
async def route_edit(name: str) -> HTMLResponse:
    blocked = _require_editor()
    if blocked:
        return blocked
    return HTMLResponse(view_edit(unquote(name)))


@app.post("/edit/{name}", response_class=HTMLResponse)
async def route_edit_post(name: str, tags: str = Form(""),
                          body: str = Form(...), csrf: str = Form("")) -> HTMLResponse:
    blocked = _csrf_guard(csrf) or _require_editor()
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
    audit("编辑词条", name)
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
    """发布包下载页（深色毛玻璃风格，零外部依赖）"""
    p = release_zip()
    size = (p.stat().st_size / 1024) if p else 0
    md5 = ""
    if p is not None:
        try:
            md5 = hashlib.md5(p.read_bytes()).hexdigest()
        except Exception:
            md5 = ""

    css = """
    <style>
    body.dl-page{
      margin:0;padding:0;background:#08090c;color:#e8eaed;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
        "Hiragino Sans GB","Microsoft YaHei",sans-serif;
      -webkit-font-smoothing:antialiased;min-height:100vh;
    }
    /* 背景光晕 */
    body.dl-page::before,body.dl-page::after{
      content:"";position:fixed;border-radius:50%;filter:blur(120px);
      opacity:.5;pointer-events:none;z-index:0;
    }
    body.dl-page::before{
      width:640px;height:640px;top:-260px;left:-180px;
      background:radial-gradient(circle,#2c6e5b 0%,transparent 70%);
    }
    body.dl-page::after{
      width:560px;height:560px;bottom:-240px;right:-160px;
      background:radial-gradient(circle,#3b7ea1 0%,transparent 70%);
    }
    .dl-wrap{
      position:relative;z-index:1;max-width:760px;margin:0 auto;
      padding:72px 24px 80px;
    }
    /* 毛玻璃卡片 */
    .glass{
      background:rgba(255,255,255,.045);
      border:1px solid rgba(255,255,255,.09);
      border-radius:20px;
      backdrop-filter:blur(20px) saturate(160%);
      -webkit-backdrop-filter:blur(20px) saturate(160%);
      box-shadow:0 8px 32px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.06);
    }
    .hero{text-align:center;padding:48px 36px 40px}
    .eyebrow{
      display:inline-flex;align-items:center;gap:7px;
      font-size:12px;letter-spacing:.5px;color:#7ee0b0;
      background:rgba(95,199,158,.1);border:1px solid rgba(95,199,158,.22);
      padding:5px 13px;border-radius:20px;margin-bottom:22px;
    }
    .eyebrow::before{
      content:"";width:6px;height:6px;border-radius:50%;
      background:#5fc79e;box-shadow:0 0 8px #5fc79e;
    }
    .hero h1{
      font-size:46px;line-height:1.1;margin:0 0 14px;font-weight:800;
      letter-spacing:-1.2px;
      background:linear-gradient(180deg,#ffffff 30%,#9aa4b2 100%);
      -webkit-background-clip:text;background-clip:text;color:transparent;
    }
    .hero .tagline{
      font-size:16px;color:#8b95a3;margin:0 0 34px;line-height:1.6;
    }
    /* 主下载按钮：大、圆角、悬浮发光 */
    .dl-btn{
      display:inline-flex;align-items:center;justify-content:center;gap:11px;
      padding:19px 52px;border-radius:999px;
      background:linear-gradient(135deg,#2c6e5b 0%,#3b8c72 55%,#3b7ea1 100%);
      color:#fff;font-size:17.5px;font-weight:700;letter-spacing:.2px;
      text-decoration:none;border:1px solid rgba(255,255,255,.14);
      box-shadow:0 6px 26px rgba(44,110,91,.42);
      transition:transform .22s cubic-bezier(.34,1.56,.64,1),
                 box-shadow .22s ease,filter .22s ease;
      position:relative;overflow:hidden;
    }
    .dl-btn::after{
      content:"";position:absolute;inset:0;
      background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.28) 50%,transparent 70%);
      transform:translateX(-120%);transition:transform .6s ease;
    }
    .dl-btn:hover{
      transform:translateY(-3px) scale(1.028);
      box-shadow:0 14px 44px rgba(95,199,158,.55),0 0 0 1px rgba(95,199,158,.4),
                 0 0 60px rgba(95,199,158,.28);
      filter:brightness(1.08);
      text-decoration:none;color:#fff;
    }
    .dl-btn:hover::after{transform:translateX(120%)}
    .dl-btn:active{transform:translateY(-1px) scale(.99)}
    .dl-btn svg{width:19px;height:19px;flex-shrink:0}
    .dl-meta{
      margin-top:20px;font-size:12.5px;color:#6e7887;
      display:flex;gap:18px;justify-content:center;flex-wrap:wrap;
    }
    .dl-meta code{
      background:rgba(255,255,255,.06);padding:2px 8px;border-radius:6px;
      color:#9aa4b2;font-size:11.5px;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }
    /* 信息卡片 */
    .cards{display:grid;gap:16px;margin-top:22px}
    .card-dl{padding:24px 26px}
    .card-dl h2{
      font-size:14px;margin:0 0 14px;color:#c9d3dd;font-weight:650;
      display:flex;align-items:center;gap:9px;
    }
    .card-dl h2 .ico{
      width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
      background:rgba(95,199,158,.12);border:1px solid rgba(95,199,158,.2);
      font-size:13px;flex-shrink:0;
    }
    .card-dl p,.card-dl li{font-size:13.5px;color:#8b95a3;line-height:1.9;margin:0}
    .card-dl ul{margin:0;padding-left:0;list-style:none}
    .card-dl li{padding-left:18px;position:relative}
    .card-dl li::before{
      content:"";position:absolute;left:0;top:12px;width:5px;height:5px;
      border-radius:50%;background:rgba(95,199,158,.6);
    }
    .card-dl code{
      background:rgba(255,255,255,.07);padding:2.5px 9px;border-radius:6px;
      color:#7ee0b0;font-size:12.5px;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    }
    .cmd{
      display:block;margin:10px 0 0;padding:13px 16px;border-radius:11px;
      background:rgba(0,0,0,.42);border:1px solid rgba(255,255,255,.07);
      color:#7ee0b0;font-size:13px;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      overflow-x:auto;white-space:nowrap;
    }
    .foot-dl{
      text-align:center;margin-top:34px;font-size:12.5px;color:#5c6673;
      line-height:1.9;
    }
    .foot-dl a{color:#7ee0b0;text-decoration:none}
    .foot-dl a:hover{text-decoration:underline}
    @media (max-width:640px){
      .dl-wrap{padding:44px 16px 60px}
      .hero{padding:36px 22px 32px}
      .hero h1{font-size:33px;letter-spacing:-.8px}
      .hero .tagline{font-size:14.5px}
      .dl-btn{padding:16px 36px;font-size:16px;width:100%}
      .card-dl{padding:20px}
    }
    @media (prefers-reduced-motion:reduce){
      .dl-btn,.dl-btn::after{transition:none}
    }
    </style>
    """

    dl_icon = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
               'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/>'
               '<path d="M4 19h16"/></svg>')

    # standard 模式：用 Tailwind 类名重写，视觉更接近 Vercel
    if is_standard_ui():
        tw = f"""
        <div class="relative min-h-screen overflow-hidden font-sans">
          <div class="pointer-events-none absolute -top-64 -left-40 h-[640px] w-[640px]
               rounded-full bg-emerald-600/25 blur-[130px]"></div>
          <div class="pointer-events-none absolute -bottom-56 -right-32 h-[560px] w-[560px]
               rounded-full bg-sky-600/25 blur-[130px]"></div>

          <div class="relative z-10 mx-auto max-w-3xl px-6 py-20">
            <div class="rounded-[22px] border border-white/10 bg-white/[0.045]
                 px-9 py-12 text-center shadow-2xl backdrop-blur-xl backdrop-saturate-150">
              <span class="inline-flex items-center gap-2 rounded-full border border-emerald-400/25
                    bg-emerald-400/10 px-3.5 py-1 text-[12px] tracking-wide text-emerald-300">
                <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_#34d399]"></span>
                公益免费 · MIT 许可
              </span>
              <h1 class="mt-6 bg-gradient-to-b from-white to-slate-400 bg-clip-text
                   text-[46px] font-extrabold leading-none tracking-tight text-transparent">
                WIKI.HS</h1>
              <p class="mx-auto mt-4 max-w-md text-[16px] leading-relaxed text-slate-400">
                给一群人用的知识库网站<br>把群规、教程、反复被问的问题写下来，谁都能查</p>

              <a href="/download/WIKI.HS.zip"
                 class="group mt-9 inline-flex items-center justify-center gap-3 rounded-full
                        border border-white/15 bg-gradient-to-r from-emerald-700 via-emerald-600
                        to-sky-700 px-14 py-5 text-[17.5px] font-bold text-white
                        shadow-[0_6px_26px_rgba(44,110,91,.42)]
                        transition duration-200 hover:-translate-y-0.5 hover:scale-[1.025]
                        hover:shadow-[0_14px_44px_rgba(95,199,158,.55)]">
                {dl_icon}<span>下载 WIKI.HS.zip</span></a>

              <div class="mt-5 flex flex-wrap justify-center gap-4 text-[12.5px] text-slate-500">
                <span>版本 1.21</span><span>{size:.0f} KB</span>
                <span>MD5 <code class="rounded bg-white/[0.07] px-2 py-0.5 text-slate-400">
                  {md5[:16] if md5 else "-"}</code></span>
              </div>
            </div>

            <div class="mt-5 grid gap-4">
              <div class="rounded-[18px] border border-white/10 bg-white/[0.045] px-6 py-6
                   shadow-xl backdrop-blur-xl">
                <h2 class="mb-3.5 flex items-center gap-2.5 text-sm font-semibold text-slate-200">
                  <span class="grid h-7 w-7 place-items-center rounded-lg border
                        border-emerald-400/20 bg-emerald-400/10 text-[13px]">⚡</span>一键部署</h2>
                <p class="text-[13.5px] leading-relaxed text-slate-400">
                  Linux 服务器，约 1 分钟装完，自动注册 systemd 服务、开机自启。</p>
                <code class="mt-3 block overflow-x-auto whitespace-nowrap rounded-xl
                      border border-white/[0.07] bg-black/40 px-4 py-3
                      font-mono text-[13px] text-emerald-300">sudo bash deploy.sh</code>
              </div>

              <div class="rounded-[18px] border border-white/10 bg-white/[0.045] px-6 py-6
                   shadow-xl backdrop-blur-xl">
                <h2 class="mb-3.5 flex items-center gap-2.5 text-sm font-semibold text-slate-200">
                  <span class="grid h-7 w-7 place-items-center rounded-lg border
                        border-emerald-400/20 bg-emerald-400/10 text-[13px]">🧩</span>装进现有机器人</h2>
                <ul class="space-y-1.5 text-[13.5px] leading-relaxed text-slate-400">
                  <li>解压，把 WIKI.HS 里所有文件放进 src/plugins/wiki/</li>
                  <li>重启机器人，看日志打印的地址和口令</li>
                  <li>打开 /register 注册第一个账号 → 自动成为站长</li>
                </ul>
              </div>

              <div class="rounded-[18px] border border-white/10 bg-white/[0.045] px-6 py-6
                   shadow-xl backdrop-blur-xl">
                <h2 class="mb-3.5 flex items-center gap-2.5 text-sm font-semibold text-slate-200">
                  <span class="grid h-7 w-7 place-items-center rounded-lg border
                        border-emerald-400/20 bg-emerald-400/10 text-[13px]">✨</span>包含什么</h2>
                <ul class="space-y-1.5 text-[13.5px] leading-relaxed text-slate-400">
                  <li>服务端渲染 WIKI，零前端依赖、断网可用</li>
                  <li>后台：仪表盘 / 服务器负载 / 媒体库 / 词条管理 / Cookie 分发 / 邮件</li>
                  <li>账号体系：注册 + 邮箱验证码 + 人机验证，三级权限</li>
                  <li>图片上传（5MB 上限，按文件头校验类型）</li>
                  <li>删除有回收站，误删可恢复</li>
                  <li>操作日志、账号安全页、隐私说明</li>
                </ul>
              </div>
            </div>

            <p class="mt-9 text-center text-[12.5px] leading-relaxed text-slate-600">
              用爱发电 · 觉得有用的话，转给需要的朋友就够了<br>
              <a href="/" class="text-emerald-400 no-underline hover:underline">返回首页</a></p>
          </div>
        </div>"""
        html = _layout("下载", tw)
        html = html.replace("</head>", """
        <style>.layout{display:block}.side{display:none}.main{padding:0;max-width:none}
        .foot{display:none}body{background:#08090c;color:#e8eaed}</style></head>""", 1)
        return HTMLResponse(html)

    inner = f"""
    {css}
    <div class="dl-wrap">
      <div class="glass hero">
        <div class="eyebrow">公益免费 · MIT 许可</div>
        <h1>WIKI.HS</h1>
        <p class="tagline">给一群人用的知识库网站<br>把群规、教程、反复被问的问题写下来，谁都能查</p>
        <a class="dl-btn" href="/download/WIKI.HS.zip">{dl_icon}
          <span>下载 WIKI.HS.zip</span></a>
        <div class="dl-meta">
          <span>版本 1.21</span>
          <span>{size:.0f} KB</span>
          <span>MD5 <code>{md5[:16] if md5 else "-"}</code></span>
        </div>
      </div>

      <div class="cards">
        <div class="glass card-dl">
          <h2><span class="ico">⚡</span>一键部署</h2>
          <p>Linux 服务器，约 1 分钟装完，自动注册 systemd 服务、开机自启。</p>
          <span class="cmd">sudo bash deploy.sh</span>
        </div>

        <div class="glass card-dl">
          <h2><span class="ico">🧩</span>装进现有机器人</h2>
          <ul>
            <li>解压，把 <code>WIKI.HS</code> 里所有文件放进 <code>src/plugins/wiki/</code></li>
            <li>重启机器人，看日志打印的地址和口令</li>
            <li>打开 <code>/register</code> 注册第一个账号 → 自动成为站长</li>
          </ul>
        </div>

        <div class="glass card-dl">
          <h2><span class="ico">✨</span>包含什么</h2>
          <ul>
            <li>服务端渲染 WIKI，零前端依赖、断网可用</li>
            <li>后台：仪表盘 / 服务器负载 / 媒体库 / 词条管理 / Cookie 分发 / 邮件</li>
            <li>账号体系：注册 + 邮箱验证码 + 人机验证，三级权限</li>
            <li>图片上传（5MB 上限，按文件头校验类型）</li>
            <li>删除有回收站，误删可恢复</li>
            <li>操作日志、账号安全页、隐私说明</li>
          </ul>
        </div>
      </div>

      <div class="foot-dl">
        用爱发电 · 觉得有用的话，转给需要的朋友就够了<br>
        <a href="/">返回首页</a>
      </div>
    </div>"""

    html = _layout("下载", inner)
    # 下载页用独立深色主题，覆盖全局配色与侧栏
    override = """
    <style>
    .layout{display:block}
    .side{display:none}
    .main{padding:0;max-width:none}
    .foot{display:none}
    </style>"""
    html = html.replace("</head>", override + "</head>", 1)
    html = html.replace('<body>', '<body class="dl-page">', 1)
    return HTMLResponse(html)


@app.get("/admin/static", response_class=HTMLResponse)
async def route_admin_static() -> HTMLResponse:
    """静态导出：生成可直接部署到 GitHub Pages 的站点"""
    blocked = _require_owner()
    if blocked:
        return blocked
    info = None
    err = ""
    try:
        info = export_static()
        audit("静态导出", info["dir"],
              f"{info['pages']} 个词条，{info['files']} 个文件")
    except Exception as e:
        err = str(e)[:200]
        logger.warning(f"[wiki] 静态导出失败: {e}")
    body = ""
    if info:
        body = (f'<div class="notice">导出完成：{info["pages"]} 个词条，'
                f'{info["files"]} 个文件，共 {info["size_kb"]} KB<br>'
                f'目录：<code>{_esc(info["dir"])}</code></div>')
    if err:
        body += f'<div class="notice warn">导出失败：{_esc(err)}</div>'
    inner = f"""
    <div class="admin-head"><h1>静态导出</h1>
      <div class="t">生成可部署到 GitHub Pages 的纯静态站点</div></div>
    {_admin_tabs("static")}
    {body}
    <div class="notice">
      静态版是<strong>只读</strong>的：没有登录、编辑、上传和问答，
      但可以在任何静态托管上免费运行（GitHub Pages / Cloudflare Pages / Netlify）。
    </div>
    <div style="display:flex;gap:10px;margin:20px 0">
      <form method="post" action="/admin/static/run">{{CSRF_FIELD}}
        <button class="btn primary" type="submit">重新导出</button></form>
      <a class="btn" href="/admin/static/download">下载静态站 zip</a>
    </div>
    <h2 style="font-size:16px;margin:26px 0 10px">部署到 GitHub Pages</h2>
    <pre class="cmdblock">1. 点上面的「下载静态站 zip」并解压
2. 在 GitHub 新建仓库，把解压内容推上去
3. Settings → Pages → Source 选 main 分支 / root
4. 等 1 分钟，访问 https://用户名.github.io/仓库名/</pre>
    <h2 style="font-size:16px;margin:26px 0 10px">命令行导出</h2>
    <p style="font-size:13.5px;color:var(--sub);line-height:1.9">
      服务器上也可以直接跑（不影响线上服务）：<br>
      <code>python -c "import wiki; wiki.export_static('/tmp/static')"</code></p>
    """
    return HTMLResponse(_layout("静态导出", inner))


@app.post("/admin/static/run")
async def route_admin_static_run(csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    if not is_owner(CUR_USER["v"]):
        return RedirectResponse("/admin/static?err=" + quote("只有站长能导出"),
                                status_code=303)
    return RedirectResponse("/admin/static", status_code=303)


@app.get("/admin/static/download")
async def route_admin_static_download():
    """把导出目录打包成 zip 下载"""
    if not is_owner(CUR_USER["v"]):
        return PlainTextResponse("需要站长权限", status_code=403)
    src = DATA_DIR / "static"
    if not src.exists():
        return PlainTextResponse("还没有导出，请先点「重新导出」", status_code=404)
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition":
                                      'attachment; filename="wiki-static.zip"'})


# ============================================================
# 本地模式路由（只有 --local 启动时才挂载）
# ============================================================

@app.get("/local", response_class=HTMLResponse)
async def route_local(msg: str = "", err: str = "") -> HTMLResponse:
    return HTMLResponse(view_local(msg, err))


@app.post("/local/export", response_class=HTMLResponse)
async def route_local_export(csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    try:
        info = export_static()
        return RedirectResponse(
            "/local?msg=" + quote(
                f"导出成功：{info['pages']} 个词条，{info['files']} 个文件，{info['size_kb']} KB"),
            status_code=303)
    except Exception as e:
        return RedirectResponse("/local?err=" + quote(f"导出失败：{e}"), status_code=303)


@app.post("/local/push", response_class=HTMLResponse)
async def route_local_push(repo: str = Form(""), branch: str = Form("gh-pages"),
                           pages_url: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    # 记住配置，下次不用再填
    try:
        cfg = load_config()
        cfg["github_repo"] = repo.strip()
        cfg["github_branch"] = (branch or "gh-pages").strip()
        cfg["pages_url"] = pages_url.strip()
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        logger.warning(f"[wiki] 保存 GitHub 配置失败: {e}")
    ok, msg = push_static_to_github(repo, branch or "gh-pages")
    if ok:
        return RedirectResponse("/local?msg=" + quote(msg), status_code=303)
    return RedirectResponse("/local?err=" + quote(msg), status_code=303)


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
async def route_register(req: Request, msg: str = "", err: str = "") -> HTMLResponse:
    ip = (req.client.host if req.client else "") or ""
    return HTMLResponse(view_register(msg, err, ip))


@app.post("/register", response_class=HTMLResponse)
async def route_register_post(req: Request, username: str = Form(""),
                              email: str = Form(""), password: str = Form(""),
                              password2: str = Form(""), captcha: str = Form(""),
                              ctoken: str = Form("")):
    ip = (req.client.host if req.client else "") or ""
    username = (username or "").strip()
    email = (email or "").strip().lower()
    # 1) 人机验证
    ok_cap, why_cap = check_captcha(ctoken, captcha)
    if not ok_cap:
        return HTMLResponse(view_register(err=why_cap, ip=ip), status_code=400)
    # 2) 字段校验
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{3,20}", username):
        return HTMLResponse(view_register(err="用户名 3~20 位，字母/数字/下划线/中文", ip=ip), status_code=400)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return HTMLResponse(view_register(err="邮箱格式不对", ip=ip), status_code=400)
    # 已知服务商给出更明确的操作提示；未知域名也允许，用户可让站长手动配 SMTP
    prov = mail_provider(email)
    prov_name = (prov or {}).get("name") or "该邮箱"
    problem = password_problem(password)
    if problem:
        return HTMLResponse(view_register(err=problem, ip=ip), status_code=400)
    if password != password2:
        return HTMLResponse(view_register(err="两次密码不一致", ip=ip), status_code=400)
    # 防用户枚举：不直接说"已被注册"，统一成模糊提示
    if find_user(username) or find_user(email):
        return HTMLResponse(
            view_register(err="用户名或邮箱不可用，换一个试试（如果这是你的账号，请直接登录）",
                          ip=ip), status_code=400)

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
      <form method="post" action="/verify">{{CSRF_FIELD}}
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
        audit("登录失败", (account or "")[:40], f"IP {ip}")
        return HTMLResponse(view_login(err="用户名或密码不对"), status_code=400)
    login_ok(ip)
    if user.get("status") == "banned":
        return HTMLResponse(view_login(err="账号已被封禁"), status_code=403)
    if not user.get("email_verified"):
        return RedirectResponse(f"/verify?uid={user['uid']}&err=" + quote("请先验证邮箱"),
                                status_code=303)
    token = new_session(user, ip=ip,
                        ua=req.headers.get("user-agent", ""))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax", secure=_is_https(req))
    logger.info(f"[wiki] 登录：{user.get('username')}")
    audit("登录成功", user.get("username", ""), f"IP {ip}")
    return resp


@app.post("/login-cookie", response_class=HTMLResponse)
async def route_login_cookie_post(req: Request, cookie: str = Form("")):
    user = live_cookie((cookie or "").strip())
    if not user:
        return HTMLResponse(view_login(err="Cookie 无效或已被收回"), status_code=400)
    ip = (req.client.host if req.client else "") or ""
    token = new_session(user, ip=ip, ua=req.headers.get("user-agent", ""))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax", secure=_is_https(req))
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
async def route_apply_post(reason: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
async def route_admin_page_delete(name: str = Form(""), csrf: str = Form("")):
    """删除词条（站长或编辑）"""
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
    audit("删除词条", name)
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
                 f'<td class="row-actions"><form method="post" action="/admin/pages/restore">{{CSRF_FIELD}}'
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
async def route_admin_page_restore(name: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
    audit("恢复词条", name)
    return RedirectResponse("/admin/pages/deleted?msg=" + quote(f"已恢复「{name}」"),
                            status_code=303)


@app.post("/admin/media/delete")
async def route_admin_media_delete(name: str = Form(""), csrf: str = Form("")):
    """删除上传的图片（同时清掉媒体索引）"""
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
    audit("删除图片", safe)
    msg = f"已删除图片 {safe}" if removed else f"图片 {safe} 不存在（已清理索引）"
    return RedirectResponse("/admin/media?msg=" + quote(msg), status_code=303)


@app.post("/admin/pages/clear")
async def route_admin_pages_clear(csrf: str = Form("")):
    """危险操作：清空所有词条（仅站长，会先备份到回收站）"""
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
    audit("清空词条", f"{count} 个")
    return RedirectResponse("/admin/pages?msg=" + quote(f"已清空 {count} 个词条（可在回收站恢复）"),
                            status_code=303)


@app.post("/admin/media/clear")
async def route_admin_media_clear(csrf: str = Form("")):
    """清空媒体库（仅站长）"""
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
    audit("清空媒体库", f"{n} 张")
    return RedirectResponse("/admin/media?msg=" + quote(f"已清空媒体库（{n} 张）"),
                            status_code=303)


@app.post("/admin/cookie", response_class=HTMLResponse)
async def route_admin_cookie_action(uid: str = Form(""), action: str = Form(""),
                                    csrf: str = Form("")) -> HTMLResponse:
    """站长操作：approve/reject 申请、grant 直接发、revoke 收回"""
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
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
        audit("发放 Cookie", rec.get("username", ""))
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
        audit("收回 Cookie", rec.get("username", ""))
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
        audit("删除账号", rec.get("username", ""))
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


@app.get("/admin/audit", response_class=HTMLResponse)
async def route_admin_audit() -> HTMLResponse:
    blocked = _admin_blocked()
    return blocked or HTMLResponse(view_admin_audit())


@app.get("/account", response_class=HTMLResponse)
async def route_account() -> HTMLResponse:
    return HTMLResponse(view_account())


@app.post("/account/password", response_class=HTMLResponse)
async def route_account_password(old: str = Form(""), new: str = Form(""),
                                 new2: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not verify_password(old, user):
        audit("改密码失败", user.get("username", ""), "当前密码错误")
        return HTMLResponse(view_account().replace(
            "<h1>账号安全</h1>",
            '<h1>账号安全</h1><div class="notice warn">当前密码不对</div>'), status_code=400)
    problem = password_problem(new)
    if problem:
        return HTMLResponse(view_account().replace(
            "<h1>账号安全</h1>",
            f'<h1>账号安全</h1><div class="notice warn">{_esc(problem)}</div>'),
            status_code=400)
    if new != new2:
        return HTMLResponse(view_account().replace(
            "<h1>账号安全</h1>",
            '<h1>账号安全</h1><div class="notice warn">两次输入的新密码不一致</div>'),
            status_code=400)
    users = load_users()
    rec = users["users"].get(user.get("uid", ""))
    if not rec:
        return RedirectResponse("/login", status_code=303)
    pw, salt = hash_password(new)
    rec["password"] = pw
    rec["salt"] = salt
    rec["password_changed"] = _now()
    save_users(users)
    # 改密码后踢掉其他所有设备，只保留当前会话
    killed = kill_user_sessions(rec["uid"], keep=CUR_SESSION.get("v") or "")
    audit("修改密码", rec.get("username", ""), f"下线 {killed} 个其他设备")
    logger.info(f"[wiki] 修改密码：{rec.get('username')}")
    return HTMLResponse(view_account().replace(
        "<h1>账号安全</h1>",
        '<h1>账号安全</h1><div class="notice">密码已更新，其他设备已下线</div>'))


@app.post("/account/kick", response_class=HTMLResponse)
async def route_account_kick(token: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    # 只能踢自己的会话
    for v in (load_sessions().get("sessions") or {}).values():
        pass
    sessions = load_sessions().get("sessions") or {}
    target = sessions.get(token) or {}
    if target.get("uid") == user.get("uid") and token != CUR_SESSION.get("v"):
        kill_session(token)
        audit("下线设备", user.get("username", ""))
    return RedirectResponse("/account", status_code=303)


@app.post("/account/kickall", response_class=HTMLResponse)
async def route_account_kickall(csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    killed = kill_user_sessions(user.get("uid", ""), keep=CUR_SESSION.get("v") or "")
    audit("下线其他设备", user.get("username", ""), f"{killed} 个")
    return RedirectResponse("/account", status_code=303)


@app.post("/qa", response_class=HTMLResponse)
async def route_qa_ask(page: str = Form(""), question: str = Form(""),
                       csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not (load_pages().get("pages") or {}).get(page):
        return RedirectResponse("/", status_code=303)
    ok, msg = ask_question(page, question, user)
    if ok:
        audit("提问", page, question[:60])
    return RedirectResponse("/p/" + quote(page) + "#qa", status_code=303)


@app.post("/qa/answer", response_class=HTMLResponse)
async def route_qa_answer(page: str = Form(""), qid: str = Form(""),
                          answer: str = Form(""), csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    ok, _msg = answer_question(page, qid, answer, user)
    if ok:
        audit("回答问题", page, answer[:60])
    return RedirectResponse("/p/" + quote(page) + "#qa", status_code=303)


@app.post("/qa/delete", response_class=HTMLResponse)
async def route_qa_delete(page: str = Form(""), qid: str = Form(""),
                          csrf: str = Form("")):
    blocked = _csrf_guard(csrf)
    if blocked:
        return blocked
    user = CUR_USER["v"]
    if not user:
        return RedirectResponse("/login", status_code=303)
    if delete_question(page, qid, user):
        audit("删除问答", page)
    return RedirectResponse("/p/" + quote(page) + "#qa", status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
async def route_privacy() -> HTMLResponse:
    return HTMLResponse(view_privacy())


@app.get("/robots.txt")
async def route_robots() -> PlainTextResponse:
    """默认不索引后台与个人页面，但公开词条允许收录"""
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /verify",
        "Disallow: /account",
        "Disallow: /apply",
        "Disallow: /api/",
        "Disallow: /media/",
        "Allow: /",
        "",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


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
    ip = (request.client.host if request.client else "") or ""
    allow, why = upload_allowed(ip)
    if not allow:
        return PlainTextResponse(f"❌ {why}", status_code=429)
    ok, msg = save_upload(file.filename or "", raw)
    if not ok:
        return PlainTextResponse(f"❌ {msg}", status_code=400)
    audit("上传图片", msg, f"{len(raw)} 字节")
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
    ip = (request.client.host if request.client else "") or ""
    allow, why = upload_allowed(ip)
    if not allow:
        return JSONResponse({"ok": False, "error": why}, status_code=429)
    ok, msg = save_upload(file.filename or "", raw)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    audit("上传图片", msg, f"{len(raw)} 字节")
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
    headers = {
        "Cache-Control": "public, max-age=86400",
        # 即使消毒漏了，这个 CSP 也让 SVG 里的脚本无法执行
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    }
    if suffix == ".svg":
        headers["Content-Disposition"] = "inline; filename=" + target.name
    return FileResponse(str(target), media_type=media_type, headers=headers)


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

# 判断运行环境：
#   * 作为 NoneBot 插件被加载 → 用真 driver
#   * 直接 python wiki.py 运行 → 独立模式，不需要 NoneBot
#   * --local → 本地桌面版，同样独立
def _has_nonebot_runtime() -> bool:
    try:
        import nonebot
        nonebot.get_driver()
        return True
    except Exception:
        return False


_IS_PLUGIN = _has_nonebot_runtime() and not is_local_mode()
_STANDALONE = not _IS_PLUGIN


class _NoopDriver:
    """独立运行时的占位 driver"""

    def __init__(self):
        self._startup = []

    def on_startup(self, fn):
        self._startup.append(fn)
        return fn


if _IS_PLUGIN:
    driver = get_driver()
else:
    driver = _NoopDriver()


@driver.on_startup
async def _wiki_startup() -> None:
    global CONFIG, EDIT_TOKEN
    import asyncio
    CONFIG = load_config()
    EDIT_TOKEN = str(CONFIG.get("token") or "")
    seed_if_empty()
    tighten_perms()
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




# ============================================================
# 独立运行入口（下载者本地用）
# ============================================================

def run_standalone(port: int = 0) -> None:
    """独立运行服务器版（不需要 NoneBot）"""
    import asyncio

    global CONFIG, EDIT_TOKEN
    CONFIG = load_config()
    EDIT_TOKEN = str(CONFIG.get("token") or "")
    seed_if_empty()
    tighten_perms()

    use_port = port or int(CONFIG.get("port") or WEB_PORT)
    url = f"http://127.0.0.1:{use_port}"
    print("=" * 60)
    print("  星海百科 · 服务器版")
    print("=" * 60)
    print(f"  地址：{url}")
    print(f"  后台：{url}/login?token={EDIT_TOKEN}")
    print(f"  数据：{DATA_DIR}")
    print("  按 Ctrl+C 停止")
    print("=" * 60)

    async def _serve() -> None:
        cfg = uvicorn.Config(app, host=WEB_HOST, port=use_port, log_level="info")
        await uvicorn.Server(cfg).serve()

    asyncio.run(_serve())


def run_local(port: int = 8080, open_browser: bool = True) -> None:
    """本地桌面版：只监听 127.0.0.1，不对外暴露"""
    import asyncio
    import webbrowser

    os.environ["WIKI_LOCAL"] = "1"
    global CONFIG, EDIT_TOKEN
    CONFIG = load_config()
    EDIT_TOKEN = str(CONFIG.get("token") or "")
    seed_if_empty()
    tighten_perms()

    url = f"http://{LOCAL_HOST}:{port}/local"
    print("=" * 60)
    print("  星海百科 · 本地编辑台")
    print("=" * 60)
    print(f"  地址：{url}")
    print(f"  数据：{DATA_DIR}")
    print("  按 Ctrl+C 停止")
    print("=" * 60)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    class _LocalDriver:
        """本地模式不需要 NoneBot，给启动钩子一个最小替身"""

    async def _serve() -> None:
        cfg = uvicorn.Config(app, host=LOCAL_HOST, port=port, log_level="warning")
        await uvicorn.Server(cfg).serve()

    asyncio.run(_serve())


def _cli() -> None:
    """命令行入口：python wiki.py --local / --export 目录"""
    import sys
    args = sys.argv[1:]
    if not args:
        # 无参数 = 启动服务器版（独立运行，不需要 NoneBot）
        run_standalone()
        return
    if "--help" in args or "-h" in args:
        print("用法：")
        print("  python wiki.py                      启动服务器版（默认端口 8081）")
        print("  python wiki.py --local              启动本地编辑台（推荐新手）")
        print("  python wiki.py --local --port 8080  指定端口")
        print("  python wiki.py --export 输出目录     只导出静态站")
        return
    if "--export" in args:
        i = args.index("--export")
        out = Path(args[i + 1]) if len(args) > i + 1 else (DATA_DIR / "static")
        info = export_static(out)
        print(f"导出完成：{info['pages']} 个词条，{info['files']} 个文件，"
              f"{info['size_kb']} KB → {info['dir']}")
        return
    if "--local" in args:
        port = 8080
        if "--port" in args:
            j = args.index("--port")
            if len(args) > j + 1:
                try:
                    port = int(args[j + 1])
                except ValueError:
                    pass
        run_local(port=port, open_browser=("--no-browser" not in args))
        return
    print("未知参数，用 --help 查看用法")


if __name__ == "__main__":
    _cli()

logger.info("📚 星海百科网站已加载（替代原斗地主网页服务，端口 8081）")
