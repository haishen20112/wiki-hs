#!/usr/bin/env bash
# 发布同步脚本（项目维护者用）
#   bash sync_release.sh                 只同步站点 zip
#   bash sync_release.sh --github        同时推 GitHub
#   bash sync_release.sh --version 1.2   指定版本号
set -u

G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"
ok()   { printf "%b\n" "${G}✅ $*${N}"; }
warn() { printf "%b\n" "${Y}⚠️  $*${N}"; }
err()  { printf "%b\n" "${R}❌ $*${N}"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_DIR="${SITE_DIR:-/home/ubuntu/Rapvebot}"
PUSH_GIT=0
VERSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    --github)  PUSH_GIT=1; shift ;;
    --version) VERSION="$2"; shift 2 ;;
    *) err "未知参数：$1"; exit 1 ;;
  esac
done

cd "$REPO_DIR" || exit 1
[ -z "$VERSION" ] && VERSION=$(grep -oE "^## \[[0-9.]+\]" CHANGELOG.md | head -1 | tr -dc "0-9.")
[ -z "$VERSION" ] && VERSION="1.1"
ok "发布版本：$VERSION"

# ---------- 1. 打包 ----------
STAGE="$REPO_DIR/.release/WIKI.HS"
rm -rf "$REPO_DIR/.release"
mkdir -p "$STAGE/wiki_data/uploads"
cp wiki.py deploy.sh "$STAGE/"
cp wiki_data/pages.json wiki_data/favicon.svg "$STAGE/wiki_data/"
touch "$STAGE/wiki_data/uploads/.gitkeep"

python3 "$REPO_DIR/.make_config.py" "$STAGE/wiki_data/config.json"

ZIP="$REPO_DIR/.release/WIKI.HS.zip"
python3 - "$STAGE" "$ZIP" <<PYEOF
import sys, zipfile
from pathlib import Path
stage, out = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            z.write(f, f.relative_to(stage.parent))
print("打包完成：%.1f KB" % (out.stat().st_size / 1024))
PYEOF
ok "已生成 WIKI.HS.zip"

# ---------- 2. 同步到站点 ----------
if [ -d "$SITE_DIR/src/plugins" ]; then
  cp -f wiki.py "$SITE_DIR/src/plugins/wiki.py"
  cp -f "$ZIP" "$SITE_DIR/WIKI.HS.zip"
  ok "已同步代码与 zip 到站点目录"
  if [ -d "$SITE_DIR/.venv" ]; then
    ( cd "$SITE_DIR" && .venv/bin/python -m py_compile src/plugins/wiki.py ) \
      && ok "站点代码编译检查通过" \
      || err "站点代码编译失败，请检查"
  fi
else
  warn "未找到站点目录 $SITE_DIR，跳过站点同步"
fi

# ---------- 3. GitHub ----------
if [ "$PUSH_GIT" = "1" ]; then
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    err "当前目录不是 git 仓库，请先 git init 并配置 remote"
    exit 1
  fi
  git add -A
  git commit -m "release: v$VERSION" || warn "没有需要提交的改动"
  git tag -f "v$VERSION"
  git push origin HEAD
  git push -f origin "v$VERSION"
  ok "已推送到 GitHub（tag v$VERSION）"
  echo ""
  echo "接下来可手动创建 Release 上传 .release/WIKI.HS.zip："
  echo "  https://github.com/<你的用户名>/<仓库名>/releases/new"
fi

echo ""
ok "同步完成 · 版本 $VERSION"
ok "zip 位置：$ZIP"
