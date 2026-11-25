# AutoTrade

基于 OKX v5 官方接口的 USDT 本位永续合约自动化交易系统（双向持仓模式）。

当前进度：**P0 - 接入验证**（脚手架、OKX 客户端、冒烟测试、最小前端）。

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
│   │   ├── api/routes/       # HTTP 路由（health、交易所自检）
│   │   ├── core/config.py    # 配置（从 backend/.env 读取）
│   │   ├── db/session.py     # SQLAlchemy 引擎与会话
│   │   ├── exchange/         # OKX 客户端、签名、限频
│   │   └── main.py           # FastAPI 入口
│   ├── tests/                # 签名/Mock/公共接口/私有接口测试
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vite + React + TS
├── docker-compose.yml
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

## 配置说明（backend/.env）

| 变量 | 说明 |
|---|---|
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` | OKX 模拟盘 API 凭据，**不要提交到 git** |
| `OKX_SIMULATED` | `true` 模拟盘（默认），`false` 实盘 |
| `OKX_PROXY` | 本地代理，如 `http://127.0.0.1:7890`，留空直连 |
| `OKX_RESOLVE_IP` | DNS 被污染时的兜底固定 IP，见下方说明 |
| `DATABASE_URL` | 默认 `sqlite:///./autotrade.db` |

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
- [ ] P1 WebSocket 行情与私有频道
- [ ] P2 交易执行引擎（双向持仓）
- [ ] P3 策略引擎 + 风控熔断
- [ ] P4 完整可视化界面
- [ ] P5 回测与部署文档
