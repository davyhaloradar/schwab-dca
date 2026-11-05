# Schwab DCA · 定投助手 (v1)

Local Python web app for **Charles Schwab personal Trader API** dollar-cost averaging (DCA / 定投).

默认 **DRY_RUN=true**：只记录拟下单 JSON，不提交真实订单。

两条路：学习用 [本机或 IP 直接打开](#simplest-deploy)（默认没有登录页）。完整、对外部署走 [域名 + nginx](#vps-domain)，步骤在 [DEPLOY.md](DEPLOY.md)。

<a id="disclaimer"></a>

## 免责声明 / Security & risk disclaimer

**你自己评估安全风险和交易风险，并自负后果。** 这不是投资、税务或交易建议。没有担保。许可证是 [MIT](LICENSE)。

**Not financial advice.** You alone judge whether this is safe to run and whether any order is appropriate. You bear the consequences. There is no warranty. See [LICENSE](LICENSE).

运行本程序，就是把 Schwab 的 App Key、App Secret 和 OAuth token 放在你控制的那台机器上（`.env` 和 `data/`）。这台机器或这个网页一旦被别人控制，对方可以通过 API 替你查账户、下单。

**以本仓库实际调用的接口为准：** 行情（报价、K 线）以及账户与订单（持仓、余额、市价买卖、撤单）。代码里没有 ACH 入金、出金或电汇。没有通过本接口做出入金或转账到外部银行；最坏是持仓被乱下单破坏（卖掉持仓、买入证券、撤掉未成交订单）。那仍然足以毁掉账户里的资产。开发者门户里实际批准的权限以你的 Schwab 应用为准，请自行核对；上面这句话描述的是这个仓库会调用的接口，不是对 Schwab 全部产品的承诺。

**API surface used here:** market data (quotes, price history) and trading (positions, balances, place and cancel orders). This codebase does not call ACH deposit, withdrawal, or wire endpoints. A compromise of this path does not, by itself, send cash out to an external bank. The worst case through these calls is the portfolio being traded against you. That can still wipe out what the account holds. Confirm the scopes on your own Schwab app; a token can do whatever Schwab granted it, which may be wider than this README.

---

## Features / 功能

| Area | Status |
|------|--------|
| OAuth authorize + callback + encrypted token store | ✅ |
| Auto-refresh access token near expiry | ✅ (needs live tokens) |
| Dashboard: auth, accounts, plans | ✅ |
| DCA plans CRUD + APScheduler (ET) | ✅ |
| Dry-run order logging | ✅ |
| Live MARKET BUY (whole shares) | ✅ code path; needs `DRY_RUN=false` + OAuth |
| Run log | ✅ |
| Quant `Strategy` plugin stub (`NoopStrategy`) | ✅ stub |
| 5 分钟 VWAP 做T 监控（模拟，图表 + 成交表） | ✅ `/plan/t`，不下单 |

---

<a id="simplest-deploy"></a>

## 最简单：本机或 IP 直接打开 / Simplest: localhost or by IP

Start uvicorn and open the UI on **this computer** or, if you must, at `https://YOUR_IP:8765/plan/`. Python **3.11+**. Commands assume a checkout of this repository and a Unix shell (macOS, Linux, or WSL).

学习请只用 localhost，或一台不在公网入口上的内网地址。不要把裸的公网 IP:8765 暴露到互联网上：这条路径默认没有登录页，也没有 nginx Basic Auth。对外部署走下面的 [域名 + nginx](#vps-domain)。

### 这条路径没有登录页 / No login screen here

本程序的网页不是 Schwab 的登录门户，也不是多用户系统。

今天实际存在的门只有两种：

- **Schwab OAuth**（页面上的 **OAuth Login**）向 Schwab 要一次 API token，加密后存在这台机器的 `data/`。它不负责「每次打开页面都要先登录」。
- **HTTP Basic Auth** 只出现在你按 [DEPLOY.md](DEPLOY.md) 给 nginx 配了密码的时候。直接跑 uvicorn 时没有这一层。

所以：直接用 IP 或 localhost 打开时，**默认没有登录框**。谁能访问这个 URL，谁就能打开控制台。OAuth 完成、token 已经在这台机器上之后，来者可以当作你来用：看账户、建计划、点右上角把 `DRY_RUN` 改成 false、再下单。token 还没有的时候，页面仍打得开，但还不能替你向 Schwab 下单。

自签证书只加密浏览器到这台机器的连接，不决定谁可以打开页面。公网 IP 上如果没有 TLS、也没有 Basic Auth，这就是把带交易 token 的控制台放在网上。不要这样做。

### 1. 安装 / Install

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Leave this terminal open; later commands use the same activated venv.

### 2. 选地址 / Pick the URL you will open

`APP_ROOT_PATH` defaults to `/plan`, so the page is `/plan/` and the OAuth callback is `/plan/callback`.

| 你怎么打开 | 绑定 | 浏览器 | 门户 Callback 与 `SCHWAB_CALLBACK_URL` |
|------------|------|--------|----------------------------------------|
| 同一台电脑（推荐） | `--host 127.0.0.1` | `https://127.0.0.1:8765/plan/` | `https://127.0.0.1:8765/plan/callback` |
| 内网另一台设备 | `--host 0.0.0.0` | `https://YOUR_IP:8765/plan/` | `https://YOUR_IP:8765/plan/callback` |

`YOUR_IP` 是这台机器的地址，例如 `192.168.1.20`。门户、`.env`、浏览器三处必须是同一个字符串，带 `https` 和 `/plan`，末尾不要斜杠。用 IP 打开时，不要把回调留在 `127.0.0.1`，否则别的设备过不了 OAuth，表单也会被 CSRF 拒绝。

只想先看页面、先不做 OAuth 时，可以不配证书，用普通 HTTP：`http://127.0.0.1:8765/plan/`。Schwab 的回调必须是 https，所以真正连上账户仍要走下面的自签证书。

### 3. 登记 Callback / Register the callback

在 [Schwab Developer Portal](https://developer.schwab.com/) 创建应用，把 **Callback URL** 设成上表里你选的那一行。复制 App Key 和 App Secret，只放进 `.env`（该文件已被 gitignore）。

### 4. 填写 .env / Fill in `.env`

`.env.example` 已经是本机模拟的默认值。`cp` 之后改密钥；若你用的是 `YOUR_IP`，把回调改成那一行。`APP_PUBLIC_BASE_URL` 和 `ALLOWED_HOSTS` 留空即可：空的允许列表会放行 `localhost`、`127.0.0.1`，以及回调 URL 里的主机（也就是你的 IP）。

同一台电脑：

```bash
SCHWAB_APP_KEY=paste_app_key_here
SCHWAB_APP_SECRET=paste_app_secret_here
SCHWAB_CALLBACK_URL=https://127.0.0.1:8765/plan/callback
APP_PUBLIC_BASE_URL=
ALLOWED_HOSTS=
APP_ROOT_PATH=/plan
APP_HOST=127.0.0.1
APP_PORT=8765
DRY_RUN=true
```

改用 IP 时，只把回调换成 `https://YOUR_IP:8765/plan/callback`。显式写 `ALLOWED_HOSTS` 的话，里面要有这个 IP。

### 5. 启动 / Start uvicorn

先看页面（无 OAuth）可以纯 HTTP，只监听本机：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

要做 OAuth，用自签证书。证书在 `data/` 里，已被 gitignore。把 `IP` 换成 `127.0.0.1` 或你的 `YOUR_IP`：

```bash
mkdir -p data/certs
openssl req -x509 -newkey rsa:2048 \
  -keyout data/certs/key.pem \
  -out data/certs/cert.pem \
  -days 825 -nodes \
  -subj "/CN=127.0.0.1" \
  -addext "subjectAltName=IP:127.0.0.1"
uvicorn app.main:app --host 127.0.0.1 --port 8765 \
  --ssl-keyfile data/certs/key.pem \
  --ssl-certfile data/certs/cert.pem
```

内网用 IP 时，`-addext` 写成 `subjectAltName=IP:YOUR_IP`，启动改为 `--host 0.0.0.0`。浏览器会提示证书不受信任，只在你自己的设备上继续即可。`openssl` 不认识 `-addext` 时，换当前版本的 OpenSSL，或用 [mkcert](https://github.com/FiloSottile/mkcert) 给同一个 IP 签证书，再把文件传给同样的 `--ssl-*` 参数。

公网机器不要用 `--host 0.0.0.0` 把 8765 直接暴露出去。那种部署用 [DEPLOY.md](DEPLOY.md)：域名、Let’s Encrypt、nginx，应用只听 `127.0.0.1`。

### 6. 打开页面并确认模拟 / Open the UI and confirm dry-run

打开你在第 2 步选的地址，例如：

```text
https://127.0.0.1:8765/plan/
https://YOUR_IP:8765/plan/
```

横幅必须是 **安全模式 / DRY-RUN**。不要点右上角的开关。那个开关会把 `.env` 里的 `DRY_RUN` 写成 `false`，之后就是真订单。这里没有登录页挡住这个开关。

### 7. 连接 OAuth / Connect OAuth

Click **OAuth Login** or **Connect Schwab OAuth**. Approve access at Schwab. The browser returns to the callback you registered (`https://127.0.0.1:8765/plan/callback` or `https://YOUR_IP:8765/plan/callback`), then the dashboard. Auth status should show **Linked**. From here, anyone who can open this URL can use those tokens.

Tokens are encrypted in `data/` (SQLite + a Fernet key). Keep that directory. The refresh token lasts about **7 days**. When the dashboard says the refresh expired, use OAuth Login again.

### 8. 建一个计划并跑一次 / Create one plan and run it

1. Click **新建计划** (or **+ New Plan**).
2. Keep the defaults if you want the shortest path: name `Daily ETF DCA`, symbol `QQQM`, **每次股数 / Shares per symbol** `1`. Schwab equity market orders are **whole shares**. `1` is one share; you do not need a dollar amount when the share count is at least 1.
3. With OAuth linked, the form pre-fills the first account hash and locks it on save. Click **Save**.
4. On the dashboard card, click **立即运行**. The confirm dialog says this is DRY-RUN and will not place a real order. Confirm.
5. The app sends you to **Run Log**.

The top-right control stays on dry-run through all of this.

### 9. 在 Run Log 里核对 / Check the Run Log

On **Run Log** (`/plan/runs`) the new row has the **模拟** badge. The dashboard block **最近运行 / Recent runs** shows the same badge. That row is the dry run. Schwab did not receive an order.

A failed quote in dry-run is still a simulated row, sized with a placeholder price of **$100** marked `PLACEHOLDER_PRICE`. After a linked account, a normal run uses the Schwab quote and still does not submit.

学习到这里可以停。`DRY_RUN=true` 留在 `.env` 里。不要为了「方便」把 8765 映射到公网。

### 常见坑 / Pitfalls

- **没有登录页。** 本机或 IP 直连不会弹出密码框。OAuth 只保存 API token。token 在 `data/` 里之后，谁能打开 URL，谁就能操作，包括关掉 DRY-RUN。
- **不要裸奔公网 IP。** 学习用 `127.0.0.1` 或内网。公网入口用域名 + nginx + TLS，并加上 Basic Auth，见 [DEPLOY.md](DEPLOY.md)。
- **HTTPS callback.** 门户是 `https://…`。纯 HTTP 进程，或回调少了 `/plan`，OAuth 完不成。本机是 `https://127.0.0.1:8765/plan/callback`；用 IP 打开则是 `https://YOUR_IP:8765/plan/callback`。
- **自签警告。** 用上面的 `openssl` 证书时浏览器会报警。只在你自己的设备上继续。
- **Refresh token 大约 7 天。** 控制台显示 refresh 过期后，再点一次 OAuth Login。删掉 `data/` 会同时丢掉 token 和加密密钥。
- **只能整股。** 股票市价单是股数 `quantity`，没有按美元下单的字段。股数 ≥ 1 时用股数；股数为 0 时用 `floor(金额 / 价格)`。价格高于金额就会得到 0 股并跳过该标的。
- **实盘开关。** 右上角和 `DRY_RUN=false` 会让「立即运行」和定时任务提交真市价买单。先读 [现金监控 / Cash monitoring](#cash-monitoring--现金监控margin-safe)。实盘花的是现金（`cashAvailableForTrading`，其次 `cashBalance`，再次 `availableFundsNonMarginableTrade`），不用融资 buying power。读不到现金且 `DCA_REQUIRE_CASH_READ=true` 时跳过下单。默认上限：每标的 5 股，单笔名义 $2000。
- **允许的主机。** 本机或 IP 直连时把 `ALLOWED_HOSTS` 和 `APP_PUBLIC_BASE_URL` 留空，让程序从回调 URL 推出主机。写成别的域名不会放行 `YOUR_IP`。

### Callback URL 对照 / Callback URL matrix

| Setup | `APP_ROOT_PATH` | Open | `SCHWAB_CALLBACK_URL` (portal must match) |
|-------|-----------------|------|---------------------------------------------|
| Localhost (learning) | `/plan` | `https://127.0.0.1:8765/plan/` | `https://127.0.0.1:8765/plan/callback` |
| This machine by IP | `/plan` | `https://YOUR_IP:8765/plan/` | `https://YOUR_IP:8765/plan/callback` |
| App at site root | empty | `https://127.0.0.1:8765/` | `https://127.0.0.1:8765/callback` |
| Domain + nginx | `/plan` | `https://example.com/plan/` | `https://example.com/plan/callback` |

<a id="smallest-production"></a>
<a id="vps-domain"></a>

## 完整 / 对外部署走域名 + nginx

不要用公网 IP:8765 当正式入口。完整部署是：**自己的域名 + nginx + Let’s Encrypt + 防火墙**，应用只听 `127.0.0.1:8765`，`APP_ROOT_PATH=/plan`。强烈建议同时打开 nginx Basic Auth——这是目前唯一能挡住「打开网址就能用控制台」的一层，程序本身没有登录页。

逐步命令（DNS、certbot、nginx、systemd、Basic Auth、防火墙，以及门户回调必须等于 `https://example.com/plan/callback`）都在 **[DEPLOY.md](DEPLOY.md)**。下面只是地图；`example.com` 换成你的域名。

1. Finish the local dry-run once, so the Schwab app key works.
2. On the server, clone this repo, create the venv, `pip install -r requirements.txt`, and `cp .env.example .env`. Do not commit `.env`.
3. Set `.env` to your public host (`example.com` is a placeholder):

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

`APP_PUBLIC_BASE_URL` is the origin only, no `/plan`. Empty `DASHBOARD_URL` makes email links `https://example.com/plan/`. `ALLOWED_HOSTS` is the browser host CSRF will accept. Add `*.example.com` only if you serve other subdomains.

4. In the Schwab portal, set Callback URL to `https://example.com/plan/callback`. The `/plan` segment has to match `APP_ROOT_PATH=/plan`.
5. Follow [DEPLOY.md](DEPLOY.md) for Let’s Encrypt, the nginx `/plan` server block (`Host` and `X-Forwarded-Proto`), the firewall (22/80/443 only; do not publish 8765), and Basic Auth. Turn Basic Auth on before you share the URL.
6. Install the systemd unit in [DEPLOY.md](DEPLOY.md) so uvicorn stays on `127.0.0.1:8765`. On the server, `curl -fsS http://127.0.0.1:8765/plan/health` returns `{"ok": true, ...}`.
7. Open `https://example.com/plan/`. Banner says DRY-RUN. **OAuth Login**, **新建计划**, **立即运行**. **Run Log** shows **模拟**.
8. Leave `DRY_RUN=true` until the cash gate in [现金监控](#cash-monitoring--现金监控margin-safe) is clear. The corner switch is what turns on live market orders.

Startup logs include `callback_host_allowed` and `dashboard`. If the public host was rejected, set `ALLOWED_HOSTS` to that hostname and restart.

---

## Order sizing API note

Schwab Trader API equity **MARKET** orders require **whole-share `quantity`**. There is **no** dollar-amount field for equity market buys.

This app: `quantity = floor(allocated_usd / last_price)`.

Quotes: `GET https://api.schwabapi.com/marketdata/v1/quotes?symbols=…`.  
If quotes fail and `DRY_RUN=true`, uses placeholder **$100** clearly marked `PLACEHOLDER_PRICE`. Live mode refuses without a real quote.

Example order body:

```json
{
  "orderType": "MARKET",
  "session": "NORMAL",
  "duration": "DAY",
  "orderStrategyType": "SINGLE",
  "orderLegCollection": [{
    "instruction": "BUY",
    "quantity": 2,
    "instrument": { "symbol": "VOO", "assetType": "EQUITY" }
  }]
}
```

---

## Quant plugin stub

```python
from app.quant import Strategy, StrategyContext, OrderIntent, NoopStrategy

class Strategy:
    def evaluate(self, ctx: StrategyContext) -> list[OrderIntent]: ...
```

UI note:「规则策略插件位，后续接入」— DCA engine currently drives buys; `NoopStrategy` returns `[]`.

---


---

## Cash monitoring / 现金监控（margin-safe）

This account may have **margin**. DCA **never** uses margin buying power.

**Cash fields used** (in order, from `currentBalances` → `projectedBalances` → `initialBalances`):

1. `cashAvailableForTrading` — preferred
2. `cashBalance` — settled + unsettled cash (negative → treated as $0 spendable)
3. `availableFundsNonMarginableTrade` — fallback

**Never used as the cash gate:** `buyingPower`, `marginBuyingPower`, `dayTradingBuyingPower`, `availableFunds`, `liquidationValue`.

Behavior:

- **Dashboard**: shows cash + estimated remaining runs per plan; warns when remaining ≤ `DCA_CASH_WARN_RUNS` (default 3).
- **LIVE execute / scheduled**: if cash &lt; planned notional → skip all orders, log `skipped_insufficient_cash`, notify. If cash cannot be read and `DCA_REQUIRE_CASH_READ=true` → `skipped_cash_unknown`.
- **Dry-run**: still simulates but annotates cash shortfall.
- **Due tick**: debounced `low_cash_warning` notify when remaining runs ≤ threshold (`DCA_CASH_WARN_COOLDOWN_SEC`, default 6h).

Env:

- `DCA_CASH_WARN_RUNS=3`
- `DCA_CASH_WARN_COOLDOWN_SEC=21600`
- `DCA_REQUIRE_CASH_READ=true`

## 5 分钟 VWAP 做T / intraday monitor

页面：`/plan/t`（导航「做T」）。规则与 2026-10-30 的 **5 分钟** VWAP 均值回归回测一致（不是更密的 1 分钟版本）：

- 默认标的 **QQQM**、**AAPL**（页面上可改，最多 6 个）
- 入场：价格低于当日 VWAP 达到该标的阈值（QQQM 约 **31.3 bps**，AAPL 约 **48.6 bps**；其它已回测标的用各自阈值，未回测的沿用 QQQM）
- 出场：回到 VWAP（5 bps 内也算）；止损约为入场距离的 2 倍
- 只做多，每个标的最多 1 笔未平仓，美东 **15:50** 平仓后当天不再开新仓
- 名义默认 **$2000**，整股；价格高于名义金额则跳过

**做T v1 只模拟成交，不向 Schwab 提交订单。** 即使把右上角定投开关切到 LIVE，本页也不会下单。`T_LIVE_ORDERS` 目前会被忽略。

- 刚收盘的那根 K 线：用报价 **mid**，没有 mid 则用最新价
- 补记更早的已收盘 K 线：用触发价（没有当时的报价）
- 成交写入 SQLite（`t_fills`），刷新页面不会丢
- 行情：已连接 Schwab 时用 Market Data 5 分钟 K 线，否则用 Yahoo Finance 备用
- 图表经 **SSE** `/plan/t/stream` 推送（失败则页面改为轮询 `/plan/t/state`）。图上是 5 分钟 K 线、当日 VWAP，以及随 VWAP 走动的买入/出场/止损曲线（没有贯穿全图的水平参考线），加上模拟买卖标记。入场/出场/止损 bps 也在图下参数表。**实时** 跟今天；**回看** 的范围和 **指定天** 在同一个下拉里。选 **指定天** 才出现交易日选择，图是那一天。选 **近1周 / 近1月 / 近3月 / 近6月 / 近1年** 会先把该窗口的 5 分钟 K 线拉进本地缓存（Schwab 优先，Yahoo 回补；5 分钟源大约只有 60 个自然日，更长范围只回补能拿到的部分并标明，不编造 K 线），再把窗口内所有交易日连成一张图。成交标记仅来自已保存的模拟记录，不会为过去的日子补写新成交。图表库放在本站 `/plan/static/`（不依赖 jsDelivr，国内可打开）

本地试：

```bash
# Same TLS server as the simplest-deploy section, or plain HTTP if you only need this page:
uvicorn app.main:app --host 127.0.0.1 --port 8765
# 打开 http://127.0.0.1:8765/plan/t
# 若按上面的 HTTPS 启动，则打开 https://127.0.0.1:8765/plan/t
# 保持 DRY_RUN=true（做T 在 v1 无论该开关如何都不会下单）
# 点「启动监控」—— 交易时段内会补记今日已收盘 5 分钟信号，并继续跟着行情更新
# 「清空今日模拟」只删今天全部标的；「清空此标的全部模拟」删当前图表标的的所有交易日
```

非交易时段会画出最近一个交易日的 5 分钟图和 VWAP，但**不会**为那一天补写模拟成交。

```bash
python scripts/test_vwap_mr.py
```

## Project layout

```
.
  app/
    main.py
    config.py
    db.py
    crypto_util.py
    routers/   auth, plans, runs
    services/  schwab_client, dca, scheduler
    quant/     Strategy stub
    templates/ Jinja2
    static/
  data/        SQLite + Fernet key (gitignored)
  .env.example
  DEPLOY.md
  LICENSE
  requirements.txt
```

---

## Env vars (`.env.example`)

`.env.example` is the source of truth. Common ones:

- `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET`
- `SCHWAB_CALLBACK_URL=https://127.0.0.1:8765/plan/callback` (local default; production uses your public HTTPS URL)
- `APP_PUBLIC_BASE_URL` — public origin, no path; empty derives from the callback URL
- `DASHBOARD_URL` — optional email link; empty derives from the public origin plus `APP_ROOT_PATH`
- `ALLOWED_HOSTS` — comma-separated CSRF / redirect hosts; empty derives localhost, `127.0.0.1`, and the callback host (apex and `www`)
- `APP_HOST` / `APP_PORT` / `APP_ROOT_PATH=/plan`
- `DRY_RUN=true`
