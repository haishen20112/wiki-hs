# WIKI.HS

A small wiki site for a group of people. Write down the rules, the how-tos, the answers to the questions that keep coming back. Free and open.

## What it does

- Markdown pages with search, tags, and edit history
- Admin panel: dashboard, server load, media library, page manager, cookie review, mail
- Accounts: register, email code, captcha. Three roles: owner, editor, guest
- Image upload up to 5 MB. File type is checked by content, not extension
- Deleted pages go to a recycle bin
- Dark mode follows the system setting

## Install

On a Linux server:

```bash
git clone https://github.com/haishen20112/wiki-hs.git
cd wiki-hs
sudo bash deploy.sh
```

The script installs dependencies, sets up a systemd service, and prints the URL and the admin token when it finishes.

Options:

```bash
sudo bash deploy.sh --port 9000                  # different port
sudo bash deploy.sh --domain wiki.example.com    # adds HTTPS via Caddy
sudo bash deploy.sh --uninstall                  # stops the service, backs up data to /root
```

Open the register page and create the first account. It becomes the owner automatically.

To run it as a bot plugin instead, drop the repo into `src/plugins/wiki/` and restart the bot.

## Configuration

`wiki_data/config.json`:

```json
{
  "token": "admin token, generated on first start",
  "port": 8081,
  "owner": "",
  "smtp": {
    "host": "smtp.qq.com",
    "port": "465",
    "ssl": "1",
    "user": "you@qq.com",
    "password": "smtp code",
    "from": "WIKI <you@qq.com>"
  }
}
```

Leaving `owner` empty means the first verified account becomes the owner.

Without SMTP, verification codes go to `wiki_data/mail_outbox/` and can be read from the admin mail page.

## Layout

```
wiki.py            everything lives here
deploy.sh          server installer
sync_release.sh    build and publish tool
wiki_data/         config, pages, accounts, uploads
```

Back up by copying `wiki_data/`.

## Requirements

Python 3.10+, `fastapi`, `uvicorn`, `python-multipart`. Storage is plain JSON. No build step.

## License

MIT. Use it, change it, share it. Please do not resell it.
