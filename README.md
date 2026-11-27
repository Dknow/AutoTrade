# AutoTrade

基于 OKX v5 官方接口的 USDT 本位永续合约自动化交易系统（双向持仓模式）。

当前进度：**P6 - 部署（Docker Compose、Nginx 静态托管、裸机与开机自启说明）已完成**。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / httpx / SQLAlchemy + SQLite |
| 前端 | React 18 + TypeScript + Vite |
| 交易所 | OKX v5 REST（自研签名客户端，无第三方 SDK） |
| 测试 | pytest + pytest-asyncio |

 ## 目录结构

```
AutoTrade/
├── backend/
│   ├── app/
│   │   ├── api/routes/       # HTTP 路由（health、market、trade）
│   │   ├── core/config.py    # 配置（从 backend/.env 读取）
│   │   ├── db/session.py     # SQLAlchemy 引擎与会话
│   │   ├── exchange/         # OKX 客户端、签名、限频、WS
│   │   ├── services/         # 行情、账户、合约规格、交易、策略、风控
│   │   └── main.py           # FastAPI 入口
│   ├── tests/                # 签名/Mock/公共接口/私有接口测试
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vite + React + TS（含生产 Dockerfile 与 nginx.conf）
├── backend/Dockerfile        # 后端生产镜像
├── docker-compose.yml        # 一键部署（后端 + Nginx 静态托管）
└── README.md
```

## 快速开始

### 1. 后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env    # 然后填入 OKX 模拟盘凭据
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

访问 <http://127.0.0.1:8000/docs> 查看接口文档。

### 2. 前端

```powershell
cd frontend
npm install
npm run dev
```

访问 <http://localhost:5173>。

### 3. 测试

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -v
```

- 离线测试（签名、Mock 请求头）始终执行；
- 公共接口测试在 OKX 不可达时自动跳过；
- 私有接口测试在未配置凭据时自动跳过。

## 接口一览

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/health` | 服务健康状态 |
| `GET /api/v1/exchange/selfcheck` | OKX 连通性、时钟偏移、账户模式 |
| `GET /api/v1/market/snapshot` | 行情快照（默认标的 ticker、`inst_ids`/`tickers` 多标的、最近 200 根 K 线） |
| `GET /api/v1/market/candles?instId=BTC-USDT-SWAP&bar=1m&limit=200` | 指定标的历史 K 线（`instId` 省略时用默认标的） |
| `WS /api/v1/ws/stream` | 实时推送：`snapshot` / `ticker` / `candle` / `positions` / `orders` |
| `GET /api/v1/trade/instruments` | USDT 本位永续合约规格（面值/步长/精度/最大杠杆） |
| `POST /api/v1/trade/instruments/refresh` | 刷新合约规格缓存 |
| `POST /api/v1/trade/orders` | 开仓（`pos_side=long/short`，可附带止盈止损） |
| `POST /api/v1/trade/orders/{ord_id}/cancel` | 撤单 |
| `POST /api/v1/trade/orders/{ord_id}/amend` | 改单（改量或改价） |
| `POST /api/v1/trade/positions/close` | 平仓（`size` 留空为市价全平，`autoCxl` 自动撤止盈止损） |
| `POST /api/v1/trade/leverage` | 设置杠杆（双向持仓需带 `pos_side`） |
| `GET /api/v1/trade/orders/pending` | 当前未成交挂单 |
| `GET /api/v1/trade/algo-orders` | 条件单 + 移动止损挂单（支持 `instId` 过滤） |
| `POST /api/v1/trade/algo-orders/{algo_id}/cancel` | 撤销条件单/移动止损 |
| `GET /api/v1/account/equity-history?limit=300` | 账户净值历史（升序 `[{ts, eq}]`，每分钟一条快照） |
| `POST /api/v1/backtest/run` | 运行策略回测（拉取历史 K 线，返回指标、净值曲线与逐笔交易） |
| `GET /api/v1/strategy` | 策略状态、参数与最近 50 条决策日志 |
| `POST /api/v1/strategy/start` / `stop` | 启动 / 停止策略 |
| `PATCH /api/v1/strategy/config` | 修改策略参数（运行中返回 409） |
| `GET /api/v1/risk` | 风控状态：权益、峰值、回撤、限额、熔断标志 |
| `POST /api/v1/risk/kill-switch` | 熔断：停止策略，可选市价全平所有持仓 |
| `POST /api/v1/risk/reset` | 人工复位熔断 |

### 策略逻辑

- 仅使用**已收盘** K 线，EMA 快线上穿慢线开多、下穿开空；
- 一次只持一边仓位，反向信号先平旧仓再开新仓；
- 止损交给**交易所原生移动止损** `move_order_stop`：`callbackSpread = ATR × 倍数`（绝对价差），
  由 OKX 跟踪开仓后的最优价自动上移/下移触发价，策略不再逐根 K 线撤旧挂新；
- 关闭 `STRATEGY_TRAILING` 时改为挂固定条件止损单 `conditional`（`slTriggerPx`）；
- 注意：回调价差在开仓时确定，后续 ATR 变化不会重算；部分平仓不会自动撤销该保护单，
  全平走 `close-position` 的 `autoCxl=true` 会自动撤销；
- 冷却 N 根 K 线内不重复开仓，抑制震荡打脸；
- 开仓前必须通过风控：单笔张数上限、名义价值上限、回撤熔断；
- 策略参数与最近信号持久化到 SQLite（`strategy_state` 表），重启后自动恢复；
  不恢复运行状态与止损单号，避免与交易所真实挂单不一致。

### 多标的

- `MARKET_INST_IDS` 决定启动时订阅的合约列表（默认 BTC/ETH/SOL 三个），首个为默认标的；
- 策略可随时切换标的，若目标标的不在订阅列表中会自动动态订阅并预热规格；
- 前端顶部下拉可切换图表与手动下单标的，策略卡单独选择策略标的。

私有频道（持仓、订单、账户）只在配置 OKX 凭据后启动，否则自动跳过。

### 可视化界面

- K 线图：叠加策略 EMA 快慢线（蓝=快线、粉=慢线），并在对应 K 线上标注开多/开空/平仓标记；
  仅当图表标的与策略标的一致时叠加，避免误导；
- 净值曲线：账户 `totalEq` 每分钟落库一条（`equity_snapshots` 表），前端每 30s 刷新；
  快照只增不删，长期运行需自行归档；
- 挂单面板：普通委托与条件单/移动止损分表展示，支持单个撤单（10s 轮询）；
- 策略日志：支持 全部 / 交易 / 止损 / 告警 筛选。

### 回测说明

- 数据：复用 OKX 公共 K 线接口，用 `after` 向历史分页（单页 300 根，最多 1440 根）；
- 成交假设：第 i 根收盘算出信号，第 i+1 根**开盘价**成交，杜绝未来函数；
- 同一根 K 线内止损优先于开仓，止损后不在同根反手；反向信号先平旧仓再开新仓；
- 移动止损：开仓时用 `ATR × 倍数` 固定回调价差，之后跟踪最高/最低价单向移动；
  关闭移动止损则止损价固定在开仓价 ± 价差；
- 成本：手续费按名义价值双边收取（默认 taker 0.05%），滑点按成交价百分比、单边不利方向计算；
- 回测结束时仍有持仓，按最后一根收盘价强制平仓；
- 限制：不含资金费率、强平与穿仓；`/market/candles` 最多回溯 1440 根，更长周期请把 `bar` 放大。

### 双向持仓下单规则

| 动作 | side | posSide | 附加 |
|---|---|---|---|
| 开多 | `buy` | `long` | — |
| 开空 | `sell` | `short` | — |
| 平多 | `sell` | `long` | `reduceOnly=true` |
| 平空 | `buy` | `short` | `reduceOnly=true` |

止盈止损通过 `attachAlgoOrds` 数组下发（顶层 `tpTriggerPx` 已下线，会返回 54070）。

## 配置说明（backend/.env）

| 变量 | 说明 |
|---|---|
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` | OKX 模拟盘 API 凭据，**不要提交到 git** |
| `OKX_SIMULATED` | `true` 模拟盘（默认），`false` 实盘 |
| `OKX_PROXY` | 本地代理，如 `http://127.0.0.1:7890`，留空直连 |
| `OKX_RESOLVE_IP` | DNS 被污染时的兜底固定 IP，见下方说明 |
| `DATABASE_URL` | 默认 `sqlite:///./autotrade.db` |
| `MARKET_INST_IDS` | 订阅的合约列表，逗号分隔，首个为默认标的 |
| `MARKET_BAR` | 行情周期，默认 `1m` |
| `STRATEGY_TRAILING` | 是否开启移动止损，默认 `true` |

### 关于 OKX 域名无法解析

部分网络环境下 `www.okx.com` 会被解析到 `169.254.x.x`（DNS 污染），表现为
`getaddrinfo failed`。两种解法：

1. 配置代理：`OKX_PROXY=http://127.0.0.1:7890`；
2. 配置固定 IP：`OKX_RESOLVE_IP=<可用 IP>`。客户端会在 transport 层把 TCP 目标
   换成该 IP，而 Host 头与 TLS SNI 仍使用 `www.okx.com`，证书校验不受影响。

获取可用 IP 的方法（在有正常 DNS 的环境执行）：

```powershell
Resolve-DnsName www.okx.com -Type A | Select-Object -ExpandProperty IPAddress
```

IP 可能会变化，失效后重新获取即可。

## 部署

### 方式一：Docker Compose（推荐）

前置：Docker Engine 24+ 或 Docker Desktop（自带 Compose v2）。

```powershell
Copy-Item backend\.env.example backend\.env   # 填入 OKX 凭据，OKX_SIMULATED=true
docker compose up -d --build
```

打开 <http://localhost:8080>。后端容器只映射到 `127.0.0.1:8000`（本机调试），前端由 Nginx 托管并同源反代 `/api`（含 WebSocket）。

自检：

```powershell
curl.exe http://127.0.0.1:8000/api/v1/health
curl.exe http://127.0.0.1:8000/api/v1/exchange/selfcheck
```

拓扑：`autotrade-frontend`(8080→80) → `autotrade-backend`(8000)。SQLite 落在命名卷 `autotrade-data`，重建容器不丢数据；后端以非 root 用户运行，并带容器级健康检查。

### 方式二：裸机

后端：

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端（构建静态文件后交给 Nginx）：

```powershell
cd frontend
npm ci
npm run build        # 产物在 dist/
```

Nginx 可直接复用 [frontend/nginx.conf](frontend/nginx.conf)，只需把 `proxy_pass http://backend:8000` 改成 `http://127.0.0.1:8000`。

开机自启：

- Linux：Compose 方式执行 `systemctl enable docker`；裸机方式写 systemd unit 跑 uvicorn；
- Windows：用 [NSSM](https://nssm.cc/) 注册服务，或用「任务计划程序」开机运行上面的 uvicorn 命令。

### 数据备份与升级

```powershell
# 备份（卷名默认是 <仓库目录名>_autotrade-data，可用 docker volume ls 确认）
docker run --rm -v autotrade_autotrade-data:/data -v ${PWD}:/backup alpine tar czf /backup/autotrade-backup.tar.gz -C /data .

# 升级
git pull
docker compose up -d --build
```

注意：SQLite 只允许单实例写入，不要同时起多个后端容器；恢复备份属于全量替换，先停服务并确认目标数据。

### 部署检查清单

- [ ] `backend/.env` 已填好，且 `OKX_SIMULATED=true`；
- [ ] 后端端口未暴露到公网（仅 127.0.0.1 或内网）；
- [ ] `/api/v1/exchange/selfcheck` 返回账户与 `posMode=long_short_mode`；
- [ ] 公网访问已通过反向代理启用 HTTPS（前端才能用 `wss`）；
- [ ] `restart: unless-stopped` 与 Docker 开机自启已生效；
- [ ] 数据库已纳入定期备份。

## 安全约定

1. `.env` 已在 `.gitignore` 中，密钥只允许存在于本地；
2. 默认锁定模拟盘（`OKX_SIMULATED=true`），切换实盘必须人工确认；
3. 模拟盘 Key 与实盘 Key 是两套，禁止混用；
4. 任何删除/全量替换数据的操作必须先人工确认。

## 提交规范

提交前先阅读本文件，按 Conventional Commits 编写中文提交信息：

```
<type>(<scope>): <简述>

<可选正文：变更原因与影响>
```

| type | 含义 |
|---|---|
| feat | 新功能 |
| fix | 修复缺陷 |
| refactor | 重构（不改变行为） |
| test | 测试相关 |
| docs | 文档 |
| chore | 构建/依赖/杂项 |

示例：

```
feat(exchange): 新增 OKX v5 客户端与模拟盘自检接口
```

提交前必须通过：

```powershell
cd backend; .\.venv\Scripts\python.exe -m pytest
cd frontend; npm run build
```

## 路线图

- [x] P0 脚手架 + OKX 客户端 + 冒烟测试
- [x] P1 WebSocket 行情与私有频道（模拟盘凭据已验证）
- [x] P2 交易执行引擎（双向持仓，开多→止盈止损→全平闭环已验证）
- [x] P3 策略与风控（双均线 + ATR 止损、限额、回撤熔断、Kill Switch）
- [x] P3.1 策略增强（多标的行情、移动止损、策略状态持久化）
- [x] P4 完整可视化界面（EMA 叠加与交易标记、净值曲线、挂单面板、日志筛选）
- [x] P5 回测引擎（历史 K 线回测、胜率/回撤/盈亏比、逐笔交易、前端回测面板）
- [x] P6 部署文档（Docker、前端静态托管、开机自启）
