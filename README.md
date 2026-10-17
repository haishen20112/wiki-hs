# WIKI.HS

给一群人用的知识库网站。把群规、教程、反复被问的问题写下来，谁都能查。

[English](README.en.md)

## 功能

- Markdown 词条，支持搜索、标签、历史版本
- 后台：仪表盘、服务器负载、媒体库、词条管理、Cookie 审核、邮件
- 账号：注册、邮箱验证码、人机验证。三级角色：站长、编辑、访客
- 图片上传，单张 5MB，按内容判断类型而不是看扩展名
- 删除的词条进回收站
- 深色模式跟随系统

## 安装

Linux 服务器：

```bash
git clone https://github.com/haishen20112/wiki-hs.git
cd wiki-hs
sudo bash deploy.sh
```

脚本会装好依赖、注册 systemd 服务，结束时打印访问地址和站长口令。

可选参数：

```bash
sudo bash deploy.sh --port 9000                  # 换端口
sudo bash deploy.sh --domain wiki.example.com    # 用 Caddy 自动配 HTTPS
sudo bash deploy.sh --uninstall                  # 停止服务，数据备份到 /root
```

打开注册页创建第一个账号，它会自动成为站长。

也可以放进机器人的 `src/plugins/wiki/` 目录当插件用，重启即可。

## 配置

`wiki_data/config.json`：

```json
{
  "token": "站长口令，首次启动自动生成",
  "port": 8081,
  "owner": "",
  "smtp": {
    "host": "smtp.qq.com",
    "port": "465",
    "ssl": "1",
    "user": "you@qq.com",
    "password": "SMTP 授权码",
    "from": "WIKI <you@qq.com>"
  }
}
```

`owner` 留空表示第一个通过验证的账号自动成为站长。

不配 SMTP 也能用，验证码会写到 `wiki_data/mail_outbox/`，在后台邮件页可以看。

## 目录

```
wiki.py            全部代码
deploy.sh          服务器安装脚本
sync_release.sh    打包发布脚本
wiki_data/         配置、词条、账号、图片
```

备份直接拷走 `wiki_data/`。

## 依赖

Python 3.10+，`fastapi`、`uvicorn`、`python-multipart`。数据是 JSON 文件，没有构建步骤。

## 许可

MIT。随便用，随便改，请勿转售。
