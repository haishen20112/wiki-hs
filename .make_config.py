import json, secrets, sys
"""生成带随机口令的干净配置（发布用）"""
cfg = {
    "token": secrets.token_urlsafe(12),
    "port": 8081,
    "owner": "",
    "smtp": {"host": "", "port": "465", "ssl": "1",
             "user": "", "password": "", "from": ""},
    "_说明": "首次启动自动生成随机 token 并打印在日志里；"
             "smtp 填好才能真发邮件；owner 留空 = 第一个验证的账号自动当站长",
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("已生成干净配置")
