# 完整部署：域名 + nginx

This is the recommended way to put the app where other people can reach it. It matches the README section [完整 / 对外部署走域名 + nginx](README.md#vps-domain).

The [localhost / IP path](README.md#simplest-deploy) is for learning. Uvicorn there has **no login screen**. Schwab OAuth only stores API tokens in `data/`. After those tokens exist, anyone who can open the URL can use the dashboard, including the switch that sets `DRY_RUN=false`. Do not publish port 8765 on a public IP. This file keeps the app on `127.0.0.1` and puts TLS, a firewall, and a password in front.

Do one local dry-run first (`DRY_RUN=true`, one **模拟** row in Run Log). Read the [免责声明](README.md#disclaimer) in the README. Replace every `example.com` below with your domain. [`.env.example`](.env.example) lists every variable. Secrets stay in the server’s `.env`. Do not commit that file.

What you end up with:

- DNS for `example.com` points at the VPS
- nginx terminates Let’s Encrypt TLS and proxies `/plan/` to the app
- HTTP Basic Auth on that site (strongly recommended; the app has no login of its own)
- uvicorn listens only on `127.0.0.1:8765` (firewall does not open 8765)
- `APP_ROOT_PATH=/plan`, so the site is `https://example.com/plan/` and the OAuth callback is `https://example.com/plan/callback`
- `DRY_RUN=true` until you choose otherwise

Python **3.11+** (`python3 --version`). Commands are for Debian or Ubuntu. On another distro, use the same paths and the matching package names.

## Callback checklist / 回调必须一致

These three strings are the same, including `https`, the host, and `/plan`, with no trailing slash:

```text
Schwab Developer Portal → Callback URL
SCHWAB_CALLBACK_URL in .env
https://example.com/plan/callback
```

Also:

- `APP_ROOT_PATH=/plan` (that is why the path contains `/plan`)
- `APP_PUBLIC_BASE_URL=https://example.com` (origin only, no path)
- `ALLOWED_HOSTS` includes `example.com` (add `www.example.com` if the browser can open the app on www)
- `DRY_RUN=true`

A portal callback of `https://127.0.0.1:…` will not complete OAuth on the server. Update the portal when you leave the laptop.

## 1. DNS / 域名

Create an **A** record (and **AAAA** if you have IPv6) for `example.com` to the VPS address. Add `www` only if you will serve it. Wait until this works from your laptop:

```bash
dig +short example.com
```

The answer is the VPS address. Let’s Encrypt will fail while DNS is still empty.

## 2. Packages / 安装软件

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx apache2-utils python3-venv
python3 --version   # 3.11 or newer
```

## 3. App on the server / 放上应用

Clone this repository into `/opt/schwab-dca`, then install it. The service user comes in the next step; install as the account you SSH in with.

```bash
sudo mkdir -p /opt/schwab-dca
sudo chown "$USER":"$USER" /opt/schwab-dca
git clone <this-repo-url> /opt/schwab-dca
cd /opt/schwab-dca
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` so these lines match your domain. Keep the two Schwab secrets from the developer portal:

```bash
SCHWAB_APP_KEY=paste_app_key_here
SCHWAB_APP_SECRET=paste_app_secret_here
SCHWAB_CALLBACK_URL=https://example.com/plan/callback
APP_PUBLIC_BASE_URL=https://example.com
ALLOWED_HOSTS=example.com,www.example.com
DASHBOARD_URL=
APP_ROOT_PATH=/plan
APP_HOST=127.0.0.1
APP_PORT=8765
DRY_RUN=true
```

`DASHBOARD_URL` empty means notification links use `https://example.com/plan/`. `ALLOWED_HOSTS` is the browser `Host` CSRF will accept. Use `*.example.com` only when other subdomains should open the app.

Lock the file down before changing its owner:

```bash
chmod 600 /opt/schwab-dca/.env
```

## 4. Schwab portal / 改回调

In the [Schwab Developer Portal](https://developer.schwab.com/), set this app’s **Callback URL** to:

```text
https://example.com/plan/callback
```

That string matches `SCHWAB_CALLBACK_URL` and `APP_ROOT_PATH=/plan`. Save it before the first OAuth login on the server. The laptop’s `https://127.0.0.1:8765/plan/callback` registration is a different URL.

## 5. systemd / 开机自启

The process stays on loopback. It does not serve TLS itself.

```bash
sudo useradd --system --home /opt/schwab-dca --shell /usr/sbin/nologin schwab-dca
sudo chown -R schwab-dca:schwab-dca /opt/schwab-dca
sudo tee /etc/systemd/system/schwab-dca.service >/dev/null <<'EOF'
[Unit]
Description=Schwab DCA
After=network.target

[Service]
Type=simple
User=schwab-dca
Group=schwab-dca
WorkingDirectory=/opt/schwab-dca
EnvironmentFile=/opt/schwab-dca/.env
ExecStart=/opt/schwab-dca/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now schwab-dca
sudo systemctl status schwab-dca --no-pager
curl -fsS http://127.0.0.1:8765/plan/health
```

Health looks like `{"ok": true, "dry_run": true, "root": "/plan"}`. Logs:

```bash
journalctl -u schwab-dca -n 40 --no-pager
```

Look for `callback_host_allowed=True` and `dashboard=https://example.com/plan/`. If the host was rejected, fix `ALLOWED_HOSTS` and `sudo systemctl restart schwab-dca`.

`data/` (SQLite and the Fernet key for Schwab tokens) now belongs to `schwab-dca`. Back it up. OAuth done on your laptop is a different `data/` directory; log in again in the browser after the site is up.

The app also reads `/opt/schwab-dca/.env` itself. Keep that file and `EnvironmentFile` the same. Do not put secrets in `ExecStart`.

## 6. Firewall / 防火墙

Open SSH, HTTP, and HTTPS. Leave **8765** closed to the internet. uvicorn is already bound to `127.0.0.1`.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
ss -ltnp | grep 8765
```

`ss` should show `127.0.0.1:8765`, not `0.0.0.0:8765`. If the VPS has a cloud security group (vendor console), allow inbound 22, 80, and 443 there too, and do not publish 8765.

Port 80 has to be reachable for the Let’s Encrypt challenge in the next step.

## 7. Certificate / Let’s Encrypt

A port-80 site first, so certbot can answer the challenge. Then certbot attaches the certificate.

```bash
sudo tee /etc/nginx/sites-available/schwab-dca >/dev/null <<'EOF'
server {
    listen 80;
    server_name example.com www.example.com;
}
EOF
sudo ln -sf /etc/nginx/sites-available/schwab-dca /etc/nginx/sites-enabled/schwab-dca
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d example.com -d www.example.com
```

Use your email when certbot asks. Renewal is a timer on Ubuntu: `systemctl status certbot.timer`. Drop `-d www.example.com` when you have no www record, and drop `www.example.com` from `server_name` and from `ALLOWED_HOSTS`.

Certbot writes `ssl_certificate` lines under `/etc/letsencrypt/live/example.com/`. Keep those lines when you edit the file in the next step.

## 8. nginx reverse proxy / 反代

Replace the site file with the block below. Keep the certificate paths certbot created if they differ. `proxy_pass` has **no** trailing path, so `/plan/...` reaches uvicorn unchanged.

`/plan/t/stream` is server-sent events. That location turns buffering off.

The two `auth_basic` lines are optional and commented. Enable them in step 9 if you want a password on the UI.

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://example.com$request_uri;
}

server {
    listen 443 ssl;
    server_name www.example.com;
    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    return 301 https://example.com$request_uri;
}

server {
    listen 443 ssl;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    # Basic Auth — turn this on in step 9 before sharing the URL.
    # The app has no login screen; nginx is the only page password.
    # auth_basic "Schwab DCA";
    # auth_basic_user_file /etc/nginx/schwab-dca.htpasswd;

    location /plan/t/stream {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }

    location /plan {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Connection "";
    }
}
```

`Host` is the public name (`ALLOWED_HOSTS` checks it). `X-Forwarded-Proto` is what marks the CSRF cookie `Secure`.

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -fsSI https://example.com/plan/ | head -n 15
```

You want an HTTP response from the app (or `401` after Basic Auth is on), not a connection error.

## 9. Basic Auth / 强烈建议加上密码

Schwab OAuth links the brokerage and stores tokens on this server. It does not ask for a password on each visit. With no Basic Auth, anyone who can open `https://example.com/plan/` can use the dashboard once those tokens exist, including turning off DRY-RUN and placing orders.

Turn these lines on before you send anyone the URL. Skip them only when some other control already limits who can reach the host.

```bash
sudo htpasswd -c /etc/nginx/schwab-dca.htpasswd yourname
sudo chmod 640 /etc/nginx/schwab-dca.htpasswd
sudo chown root:www-data /etc/nginx/schwab-dca.htpasswd
```

Uncomment the two `auth_basic` lines in the `example.com` `443` server, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

The password file stays out of the repo (`.htpasswd*` is gitignored). Skip this step only when something else already limits who can reach the host.

## 10. Open the UI / 打开并再模拟一次

1. Open `https://example.com/plan/`. The banner says **安全模式 / DRY-RUN**.
2. **OAuth Login**. Schwab redirects to `https://example.com/plan/callback`. The dashboard shows **Linked**. This is a new token store on the server.
3. **新建计划**. One symbol, **每次股数** `1`, **Save**.
4. **立即运行**, confirm the DRY-RUN dialog.
5. **Run Log** shows a new row with the **模拟** badge.

Leave `DRY_RUN=true` until you have read the cash gate in the README ([现金监控](README.md#cash-monitoring--现金监控margin-safe)). The corner switch writes `DRY_RUN=false` and the next run is a real market order. Live mode spends cash, not margin buying power.

## Done when / 核对

- `https://example.com/plan/health` is ok (via nginx, or `curl` on the server to `127.0.0.1`)
- Portal Callback URL, `SCHWAB_CALLBACK_URL`, and `https://example.com/plan/callback` are the same string
- `ALLOWED_HOSTS` contains `example.com`
- `ss` shows `127.0.0.1:8765` only
- Run Log on the server has a **模拟** row
- `DRY_RUN=true`
- nginx asks for the Basic Auth password before the dashboard (unless another control already does that job)
