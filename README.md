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

## Windows 用户（最简单）

包里有两个 `.bat`，**双击就能用**：

| 文件 | 作用 |
| --- | --- |
| **启动本地版.bat** | 双击后在浏览器里编辑内容，导出静态站，一键发到 GitHub Pages。**不需要服务器** |
| **启动服务器版.bat** | 双击后启动完整网站，浏览器访问 `http://127.0.0.1:8081` |

### 会自动检测依赖

第一次双击时，启动器会**自动检查**有没有装 `fastapi` / `uvicorn` / `python-multipart`：

- **装了** → 直接启动
- **没装** → 提示缺什么，问你要不要自动安装（输入 `y` 回车即可）
- **装失败** → 给出可直接复制的 `pip install` 命令

```
  缺少以下依赖：
      - fastapi
      - uvicorn
      - python-multipart

  要现在自动安装吗？（需要联网，约 1 分钟）
  输入 y 安装，直接回车跳过：
```

### 中英文自动切换

启动器会**自动识别系统语言**：

- 中文系统 → 中文提示
- 英文系统 → 英文提示
- 想强制指定：`python launcher.py --local --zh` 或 `--en`
- 想跳过询问直接装：`python launcher.py --local --yes`

> 中文提示由 `launcher.py` 输出，`.bat` 本身是纯 ASCII，
> 不会出现乱码或"不是内部或外部命令"的报错。

> 如果双击后窗口一闪而过，说明没装 Python，或安装时没勾选 "Add Python to PATH"。

## 本地桌面版（推荐给下载者）

不想租服务器？在自己电脑上跑本地编辑台：

```bash
python wiki.py --local
```

会自动打开浏览器，进入本地编辑台。三步搞定：

1. **编辑内容** —— 新建 / 修改词条
2. **导出静态站** —— 生成纯静态 HTML
3. **推送 GitHub Pages** —— 填仓库地址，一键发布

全程不需要服务器、不需要域名、不需要邮箱配置。
编辑台只监听 `127.0.0.1`，外网访问不到。

只想导出不上传：

```bash
python wiki.py --export ./my-wiki
```

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

## 界面模式

`wiki_data/config.json` 里的 `ui_mode` 可以选两套：

| 模式 | 说明 | 适用 |
| --- | --- | --- |
| `lite`（默认） | 纯内联 CSS，零外部请求，断网可用，CSP 最严 | 内网、轻量部署 |
| `standard` | 引入 Tailwind CSS（CDN），视觉更现代 | 有公网、追求观感 |

改完重启生效。页脚会显示当前模式，方便确认。

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
