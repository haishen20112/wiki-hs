# WIKI.HS（星海百科）

> 一个开箱即用的群知识库网站 —— 群规、教程、FAQ 的归处。
> **完全免费 · 公益项目 · 可自由二创**

---

## ✨ 特性

- **服务端渲染**：零前端依赖、零构建步骤，断网也能开
- **Markdown 词条**：标题 / 粗体 / 列表 / 代码块 / 链接
- **全文搜索**：标题 + 正文 + 标签，标签分类、最近更新
- **历史版本**：每次保存自动留档，随时回看
- **回收站**：删掉的词条可恢复，最多保留 100 条
- **图片上传**：png/jpg/gif/webp/svg，默认 5MB，校验文件头防伪装
- **账号体系**：注册 + 邮箱验证码 + 人机验证，密码 PBKDF2 加盐
- **三级权限**：站长（owner）/ 编辑（editor）/ 访客（guest）
- **Cookie 分发**：访客申请 → 站长审批 → 拿到 Cookie 才能编辑
- **后台管理**：仪表盘 / 服务器负载 / 媒体库 / 词条管理 / 邮件
- **深色模式**：自动跟随系统，手机端自适应

---

## 🚀 快速开始

### 方式一：一键部署（推荐）

Linux 服务器（Ubuntu / Debian / CentOS）：

```bash
git clone https://github.com/你的用户名/wiki-hs.git
cd wiki-hs
sudo bash deploy.sh
```

约 1 分钟装完，自动注册 systemd 服务、开机自启、崩溃自动重启。

可选参数：

```bash
sudo bash deploy.sh --port 9000                  # 自定义端口
sudo bash deploy.sh --domain wiki.example.com    # 自动配置 HTTPS
sudo bash deploy.sh --uninstall                  # 卸载（先备份数据）
```

### 方式二：作为机器人插件

1. 把本仓库放进 `机器人/src/plugins/wiki/`
2. 重启机器人，日志会打印地址与站长口令
3. 打开 `/register` 注册第一个账号 → **自动成为站长**

---

## 📸 截图

> 首页 / 词条页 / 后台负载 / 注册页

<!-- 上传截图后替换这里 -->

---

## 🔧 配置

`wiki_data/config.json`：

```json
{
  "token": "站长快捷登录口令（首次启动自动生成）",
  "port": 8081,
  "owner": "",
  "smtp": {
    "host": "smtp.qq.com",
    "port": "465",
    "ssl": "1",
    "user": "你的邮箱@qq.com",
    "password": "SMTP授权码（不是登录密码）",
    "from": "星海百科 <你的邮箱@qq.com>"
  }
}
```

- `owner` 留空 = 第一个验证邮箱的账号自动当站长
- `smtp` 不填也能用，验证码会落到 `wiki_data/mail_outbox/`，站长在后台「邮件」页查看

**QQ 邮箱**：设置 → 账户 → 开启 IMAP/SMTP → 生成授权码

---

## 📁 目录结构

```
.
├── wiki.py                 全部功能（单文件，约 110KB）
├── deploy.sh               一键部署脚本
├── README.md
├── LICENSE
└── wiki_data/
    ├── config.json         配置（口令首次启动自动生成）
    ├── pages.json          词条（核心数据）
    ├── users.json          账号（含密码哈希）
    ├── media.json          图片索引
    ├── deleted_pages.json  回收站
    ├── favicon.svg         站点图标
    └── uploads/            上传的图片
```

**备份**：拷走整个 `wiki_data/` 目录即可。

---

## 🛠 技术栈

| 项目 | 说明 |
| --- | --- |
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI + Uvicorn |
| 前端 | 服务端渲染 HTML + 少量原生 JS（无构建） |
| 存储 | JSON 文件（可读可备份，不锁数据库） |
| 邮件 | 标准库 smtplib（不装第三方邮件包） |
| 负载监控 | 直接读 /proc（不依赖 psutil） |

运行时依赖只有三个：`fastapi`、`uvicorn`、`python-multipart`。

---

## ❓ 常见问题

<details>
<summary><b>网页打开是空白 / 403？</b></summary>

`/admin` 需要登录；先访问 `/`。
</details>

<details>
<summary><b>注册后登不进去？</b></summary>

要先过邮箱验证。没配 SMTP 就去后台「邮件」页看验证码。
</details>

<details>
<summary><b>忘了站长口令？</b></summary>

看 `wiki_data/config.json` 里的 `token`。
</details>

<details>
<summary><b>端口被占用？</b></summary>

`sudo bash deploy.sh --port 9000`，或改 `wiki.py` 顶部 `WEB_PORT`。
</details>

<details>
<summary><b>想改成自己的名字和配色？</b></summary>

`SITE_NAME`、`SITE_SUB` 在 `wiki.py` 顶部；配色在 `CSS` 的 `:root`。
</details>

---

## 📄 许可

MIT License —— 可自用、可修改、可二创，**禁止转售**。

---

## 💚 关于

用爱发电。不锁功能、不藏后门、不加广告。

如果帮到你了，给个 Star 或转给需要的朋友就够啦。
