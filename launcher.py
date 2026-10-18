#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WIKI.HS 启动器：自动检测依赖，询问是否安装，然后启动。

用法：
    python launcher.py --local    本地编辑台（自动打开浏览器）
    python launcher.py            服务器版
    python launcher.py --en       强制英文
    python launcher.py --zh       强制中文
    python launcher.py --yes      缺少依赖时自动安装，不再询问
"""
import importlib
import locale
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("multipart", "python-multipart"),
]

# ── 中英双语文案 ──
TEXT = {
    "zh": {
        "title_local": "星海百科 · 本地编辑台",
        "title_server": "星海百科 · 服务器版",
        "checking": "正在检查运行环境…",
        "ok": "环境正常",
        "missing": "缺少以下依赖：",
        "ask_install": "要现在自动安装吗？（需要联网，约 1 分钟）",
        "ask_yes_no": "输入 y 安装，直接回车跳过：",
        "installing": "正在安装，请稍等…",
        "install_ok": "依赖安装完成",
        "install_fail": "自动安装失败，请手动执行：",
        "skip": "已跳过安装，但缺少依赖无法启动。",
        "manual": "请手动执行下面的命令后重新双击：",
        "starting_local": "正在启动，稍等几秒浏览器会自动打开…",
        "starting_server": "启动后不要关闭这个窗口，关了网站就停了。",
        "server_addr": "默认地址：http://127.0.0.1:8081",
        "close_hint": "关闭这个窗口 = 停止服务",
        "nopython": "没有找到 Python，请先安装 Python 3.10+",
        "press_enter": "按回车继续…",
    },
    "en": {
        "title_local": "Wiki.HS - Local Editor",
        "title_server": "Wiki.HS - Server",
        "checking": "Checking runtime environment...",
        "ok": "Environment OK",
        "missing": "Missing dependencies:",
        "ask_install": "Install them automatically now? (needs internet, ~1 min)",
        "ask_yes_no": "Type y to install, or press Enter to skip: ",
        "installing": "Installing, please wait...",
        "install_ok": "Dependencies installed",
        "install_fail": "Auto install failed. Please run manually:",
        "skip": "Skipped. Cannot start without dependencies.",
        "manual": "Run the command below, then double-click again:",
        "starting_local": "Starting... the browser will open in a few seconds.",
        "starting_server": "Keep this window open. Closing it stops the site.",
        "server_addr": "Address: http://127.0.0.1:8081",
        "close_hint": "Close this window = stop the service",
        "nopython": "Python not found. Please install Python 3.10+",
        "press_enter": "Press Enter to continue...",
    },
}


def detect_lang() -> str:
    """优先命令行指定，其次看系统语言"""
    if "--en" in sys.argv:
        return "en"
    if "--zh" in sys.argv:
        return "zh"
    try:
        lang = (locale.getdefaultlocale()[0] or "").lower()
        if lang.startswith("zh"):
            return "zh"
        if lang:
            return "en"
    except Exception:
        pass
    # 兜底：看系统代码页
    try:
        if sys.platform == "win32":
            import ctypes
            cp = ctypes.windll.kernel32.GetConsoleOutputCP()
            return "zh" if cp in (936, 54936) else "en"
    except Exception:
        pass
    return "zh"


def missing_packages():
    out = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def pip_install(pkgs, t):
    """尝试用 pip 安装，返回是否成功"""
    print()
    print("  " + t["installing"])
    print()
    cmds = [
        [sys.executable, "-m", "pip", "install", "--upgrade"] + pkgs,
        # 国内网络常见问题：加清华镜像重试
        [sys.executable, "-m", "pip", "install", "--upgrade",
         "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + pkgs,
    ]
    for i, cmd in enumerate(cmds, 1):
        try:
            r = subprocess.call(cmd, cwd=str(HERE))
            if r == 0:
                return True
            if i < len(cmds):
                print()
                print("  ... retrying with mirror")
        except Exception:
            continue
    return False


def main() -> int:
    lang = detect_lang()
    t = TEXT[lang]
    mode = "local" if "--local" in sys.argv else "server"
    title = t["title_local"] if mode == "local" else t["title_server"]

    print("=" * 60)
    print("  " + title)
    print("=" * 60)
    print()
    print("  " + t["checking"])

    miss = missing_packages()
    if miss:
        print()
        print("  " + t["missing"])
        for p in miss:
            print("      - " + p)
        print()
        print("  " + t["ask_install"])
        auto_yes = "--yes" in sys.argv or "-y" in sys.argv
        if auto_yes:
            ans = "y"
        else:
            try:
                ans = input("  " + t["ask_yes_no"]).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
        if ans in ("y", "yes", "是", "好"):
            if pip_install(miss, t):
                # 重新检查
                still = missing_packages()
                if not still:
                    print()
                    print("  " + t["install_ok"])
                    miss = []
                else:
                    miss = still
            else:
                print()
                print("  " + t["install_fail"])
                print("      pip install " + " ".join(miss))
        else:
            print()
            print("  " + t["skip"])
            print()
            print("  " + t["manual"])
            print("      pip install " + " ".join(miss))
            print()
            try:
                input("  " + t["press_enter"])
            except Exception:
                pass
            return 1

    if miss:
        print()
        print("  " + t["manual"])
        print("      pip install " + " ".join(miss))
        print()
        try:
            input("  " + t["press_enter"])
        except Exception:
            pass
        return 1

    print("  " + t["ok"])
    print()
    if mode == "local":
        print("  " + t["starting_local"])
        print("  " + t["close_hint"])
    else:
        print("  " + t["starting_server"])
        print("  " + t["server_addr"])
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
