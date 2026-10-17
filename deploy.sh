#!/usr/bin/env bash
# ==============================================================
#  星海百科 WIKI.HS · 一键部署脚本
#
#  用法（在解压后的 WIKI.HS 目录里执行）：
#      bash deploy.sh                              # 默认端口 8081
#      bash deploy.sh --port 9000                  # 自定义端口
#      bash deploy.sh --domain wiki.example.com    # 顺带配 HTTPS（需域名已解析到本机）
#      bash deploy.sh --uninstall                  # 卸载
#
#  脚本做四件事：
#    1. 装系统依赖（python3 / venv / pip）
#    2. 建独立运行环境，装 fastapi + uvicorn
#    3. 注册 systemd 服务：开机自启、崩溃自动重启
#    4. 打印访问地址与站长口令
# ==============================================================

set -u

G="\033[32m"; Y="\033[33m"; R="\033[31m"; B="\033[36m"; N="\033[0m"
ok()   { printf "%b\n" "${G}✅ $*${N}"; }
warn() { printf "%b\n" "${Y}⚠️  $*${N}"; }
err()  { printf "%b\n" "${R}❌ $*${N}"; }
info() { printf "%b\n" "${B}➜  $*${N}"; }

PORT=8081
DOMAIN=""
UNINSTALL=0
SERVICE="wiki-hs"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)      PORT="$2"; shift 2 ;;
    --domain)    DOMAIN="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *) err "未知参数：$1"; exit 1 ;;
  esac
done

INSTALL_DIR="/opt/wiki-hs"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"

if [ "$UNINSTALL" = "1" ]; then
  info "卸载星海百科…"
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload 2>/dev/null || true
  if [ -d "$INSTALL_DIR" ]; then
    BACKUP="/root/wiki-hs-data-$(date +%Y%m%d%H%M).tar.gz"
    tar czf "$BACKUP" -C "$INSTALL_DIR" wiki_data 2>/dev/null && warn "数据已备份到 $BACKUP"
    rm -rf "$INSTALL_DIR"
  fi
  ok "已卸载（数据备份保留在 /root/ 下）"
  exit 0
fi

echo ""
echo "=================================================="
echo "  星海百科 WIKI.HS · 一键部署"
echo "=================================================="
info "源码目录：$APP_DIR"
info "部署位置：$INSTALL_DIR"
info "服务端口：$PORT"
[ -n "$DOMAIN" ] && info "绑定域名：$DOMAIN"
echo ""

if [ "$(id -u)" != "0" ]; then
  err "需要 root 权限，请用：sudo bash deploy.sh"
  exit 1
fi

info "检查系统依赖…"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq python3 python3-venv python3-pip curl tar >/dev/null 2>&1 || {
    err "安装系统依赖失败，请手动执行：apt update && apt install -y python3 python3-venv python3-pip"
    exit 1
  }
fi
ok "系统依赖就绪：$(python3 --version 2>&1)"

if [ "$APP_DIR" != "$INSTALL_DIR" ]; then
  info "安装文件到 $INSTALL_DIR …"
  mkdir -p "$INSTALL_DIR"
  if [ -d "$INSTALL_DIR/wiki_data" ]; then
    warn "检测到已有数据，保留不动（升级模式）"
    cp -f "$APP_DIR/wiki.py" "$INSTALL_DIR/wiki.py"
    cp -f "$APP_DIR/deploy.sh" "$INSTALL_DIR/deploy.sh" 2>/dev/null || true
    if [ -f "$APP_DIR/wiki_data/favicon.svg" ]; then
      cp -f "$APP_DIR/wiki_data/favicon.svg" "$INSTALL_DIR/wiki_data/favicon.svg"
    fi
  else
    cp -r "$APP_DIR"/. "$INSTALL_DIR"/
  fi
fi
mkdir -p "$INSTALL_DIR/wiki_data/uploads"
ok "文件就位"

if [ ! -d "$VENV_DIR" ]; then
  info "创建 Python 运行环境…"
  python3 -m venv "$VENV_DIR" || { err "创建 venv 失败"; exit 1; }
fi
info "安装依赖（fastapi / uvicorn / python-multipart）…"
"$VENV_DIR/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV_DIR/bin/pip" install -q "fastapi>=0.100" "uvicorn>=0.23" "python-multipart>=0.0.6" >/dev/null 2>&1; then
  warn "官方源慢，切换清华镜像重试…"
  "$VENV_DIR/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
      "fastapi>=0.100" "uvicorn>=0.23" "python-multipart>=0.0.6" || { err "安装依赖失败"; exit 1; }
fi
ok "依赖安装完成"

# ---------- 5. 独立启动器（不依赖 NoneBot）----------
RUNNER_PY="$INSTALL_DIR/standalone_runner.py"
cat > "$RUNNER_PY" <<'PYEOF'
"""独立运行器：不依赖 NoneBot 框架，直接把 wiki 跑起来"""
import logging
import sys
import types
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%m-%d %H:%M:%S")
_log = logging.getLogger("wiki")

# wiki.py 只用到 nonebot 的 get_driver() 和 logger，这里给一个最小替身
logger_mod = types.ModuleType("nonebot.log")
logger_mod.logger = _log
nonebot_mod = types.ModuleType("nonebot")


class _FakeDriver:
    def __init__(self):
        self._startup = []

    def on_startup(self, fn):
        self._startup.append(fn)
        return fn


_driver = _FakeDriver()
nonebot_mod.get_driver = lambda: _driver
nonebot_mod.logger = _log
sys.modules["nonebot"] = nonebot_mod
sys.modules["nonebot.log"] = logger_mod

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
import importlib.util

spec = importlib.util.spec_from_file_location("wiki", here / "wiki.py")
wiki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiki)

import asyncio


async def _boot():
    for fn in getattr(_driver, "_startup", []):
        r = fn()
        if asyncio.iscoroutine(r):
            await r


asyncio.run(_boot())
_log.info("星海百科已启动，端口 %s", wiki.WEB_PORT)

import uvicorn

try:
    uvicorn.run(wiki.app, host=wiki.WEB_HOST, port=wiki.WEB_PORT, log_level="warning")
except OSError:
    _log.info("端口已被内部服务占用，沿用已启动的实例")

PYEOF
chmod +x "$RUNNER_PY"
ok "独立启动器已生成"

# ---------- 6. 端口写入配置 + wiki.py 支持读端口 ----------
SETPORT_PY="$INSTALL_DIR/.set_port.py"
cat > "$SETPORT_PY" <<'PYEOF'
import json, sys
from pathlib import Path
cfg_path, port = Path(sys.argv[1]), int(sys.argv[2])
cfg = {}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
cfg["port"] = port
cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("端口已写入配置：", port)

PYEOF
python3 "$SETPORT_PY" "$INSTALL_DIR/wiki_data/config.json" "$PORT" && rm -f "$SETPORT_PY"

python3 - "$INSTALL_DIR/wiki.py" <<'PYEOF'
import sys
from pathlib import Path
p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
if "_CFG_PORT" not in s:
    s = s.replace(
        'WEB_HOST = "0.0.0.0"',
        '_CFG_PORT = 8081\n'
        'try:\n'
        '    import json as _j\n'
        '    _cfg0 = _j.loads((Path(__file__).resolve().parent / "wiki_data" / "config.json")\n'
        '                     .read_text(encoding="utf-8"))\n'
        '    _CFG_PORT = int(_cfg0.get("port") or _CFG_PORT)\n'
        'except Exception:\n'
        '    pass\n\n'
        'WEB_HOST = "0.0.0.0"', 1)
    s = s.replace("WEB_PORT = 8081", "WEB_PORT = _CFG_PORT", 1)
    p.write_text(s, encoding="utf-8")
    print("wiki.py 已支持从配置文件读端口")
PYEOF

# ---------- 7. systemd 服务 ----------
info "注册系统服务…"
cat > "$SERVICE_FILE" <<SVCEOF
[Unit]
Description=Wiki.HS (星海百科) 群知识库网站
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_DIR/bin/python $RUNNER_PY
Restart=always
RestartSec=3
StandardOutput=append:$INSTALL_DIR/wiki.log
StandardError=append:$INSTALL_DIR/wiki.log
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 4

if systemctl is-active --quiet "$SERVICE"; then
  ok "服务已启动（$SERVICE）"
else
  err "服务启动失败，最近的日志："
  journalctl -u "$SERVICE" -n 25 --no-pager || true
  echo "----"
  tail -20 "$INSTALL_DIR/wiki.log" 2>/dev/null || true
  exit 1
fi

# ---------- 8. 可选 HTTPS ----------
if [ -n "$DOMAIN" ]; then
  if ! command -v caddy >/dev/null 2>&1; then
    info "安装 Caddy（自动 HTTPS）…"
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null 2>&1 || true
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list 2>/dev/null || true
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq caddy >/dev/null 2>&1 || true
  fi
  if command -v caddy >/dev/null 2>&1; then
    printf "%s {\n    reverse_proxy 127.0.0.1:%s\n}\n" "$DOMAIN" "$PORT" > /etc/caddy/Caddyfile
    systemctl restart caddy
    ok "HTTPS 已配置：https://$DOMAIN"
  else
    warn "Caddy 安装失败，跳过 HTTPS（可自行用 Nginx 反代 127.0.0.1:$PORT）"
  fi
fi

# ---------- 9. 汇总 ----------
IP=$(curl -s -m 5 ifconfig.me 2>/dev/null || echo "你的服务器IP")
TOKEN=$(python3 -c "
import json
try:
    print(json.load(open('$INSTALL_DIR/wiki_data/config.json')).get('token',''))
except Exception:
    print('')
" 2>/dev/null)

echo ""
echo "=================================================="
ok "部署完成！"
echo "=================================================="
echo ""
echo "  访问地址：http://$IP:$PORT"
[ -n "$DOMAIN" ] && echo "  HTTPS ：https://$DOMAIN"
echo "  注册页　：http://$IP:$PORT/register"
echo "  站长口令：$TOKEN"
echo "  快捷登录：http://$IP:$PORT/login?token=$TOKEN"
echo ""
echo "  [!] 记得去云服务器控制台【安全组】放行 TCP $PORT，否则外网打不开"
echo ""
echo "  ── 常用命令 ──"
echo "  查看状态： systemctl status $SERVICE"
echo "  查看日志： tail -f $INSTALL_DIR/wiki.log"
echo "  重启服务： systemctl restart $SERVICE"
echo "  停止服务： systemctl stop $SERVICE"
echo "  卸载服务： sudo bash deploy.sh --uninstall"
echo ""
echo "  ── 下一步 ──"
echo "  1. 打开【注册页】注册第一个账号（验证邮箱）"
echo "  2. 第一个验证通过的账号自动成为站长"
echo "  3. 想让验证码自动发邮件，在后台「邮件」页看配置说明"
echo "     配置文件：$INSTALL_DIR/wiki_data/config.json"
echo ""
