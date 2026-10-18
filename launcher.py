#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WIKI.HS 启动器：检查依赖后启动，给 Windows 双击用。

用法：
    python launcher.py --local    本地编辑台（默认打开浏览器）
    python launcher.py            服务器版
"""
import importlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("multipart", "python-multipart"),
]


def missing_packages():
    """检查依赖，返回缺的包名列表"""
    out = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def main() -> int:
    mode = "local" if "--local" in sys.argv else "server"
    title = "星海百科 · 本地编辑台" if mode == "local" else "星海百科 · 服务器版"

    print("=" * 60)
    print("  " + title)
    print("=" * 60)

    miss = missing_packages()
    if miss:
        print()
        print("  [缺少依赖] 请先安装：")
        print()
        print("      pip install " + " ".join(miss))
        print()
        print("  装完再双击一次本文件即可。")
        print()
        return 1

    print()
    if mode == "local":
        print("  正在启动，稍等几秒浏览器会自动打开...")
        print("  关闭这个窗口 = 停止服务")
    else:
        print("  启动后不要关闭这个窗口，关了网站就停了。")
        print("  默认地址：http://127.0.0.1:8081")
    print()
    print("=" * 60)
    print()

    args = [sys.executable, str(HERE / "wiki.py")]
    if mode == "local":
        args.append("--local")
    try:
        return subprocess.call(args, cwd=str(HERE))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
