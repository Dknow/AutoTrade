import { useEffect, useState } from "react";

type Health = {
  status: string;
  app: string;
  env: string;
  simulated: boolean;
  okx_credentials_configured: boolean;
};

type SelfCheck = {
  reachable: boolean;
  base_url: string;
  simulated: boolean;
  time_offset_ms: number;
  credentials_configured: boolean;
  account?: {
    uid?: string;
    acct_lv?: string;
    pos_mode?: string;
    mgn_mode?: string;
  };
};

function Tag({ tone, children }: { tone: "ok" | "warn" | "err"; children: string }) {
  return <span className={`tag ${tone}`}>{children}</span>;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [selfCheck, setSelfCheck] = useState<SelfCheck | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/v1/health")
      .then((resp) => resp.json())
      .then(setHealth)
      .catch((err: unknown) => setError(String(err)));
  }, []);

  async function runSelfCheck() {
    setLoading(true);
    setError(null);
    setSelfCheck(null);
    try {
      const resp = await fetch("/api/v1/exchange/selfcheck");
      const body = await resp.json();
      if (!resp.ok) {
        setError(body?.detail?.message ?? "自检失败");
        return;
      }
      setSelfCheck(body as SelfCheck);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="container">
      <h1>AutoTrade</h1>
      <div className="subtitle">OKX 合约自动化交易系统 · P0 接入验证</div>

      <div className="grid">
        <section className="card">
          <h2>服务状态</h2>
          {health ? (
            <>
              <div className="row">
                <span className="label">状态</span>
                <Tag tone={health.status === "ok" ? "ok" : "err"}>{health.status}</Tag>
              </div>
              <div className="row">
                <span className="label">环境</span>
                <span>{health.env}</span>
              </div>
              <div className="row">
                <span className="label">交易环境</span>
                <Tag tone={health.simulated ? "ok" : "err"}>{health.simulated ? "模拟盘" : "实盘"}</Tag>
              </div>
              <div className="row">
                <span className="label">API 凭据</span>
                <Tag tone={health.okx_credentials_configured ? "ok" : "warn"}>
                  {health.okx_credentials_configured ? "已配置" : "未配置"}
                </Tag>
              </div>
            </>
          ) : (
            <div className="label">加载中…</div>
          )}
        </section>

        <section className="card">
          <h2>OKX 连接自检</h2>
          <button onClick={runSelfCheck} disabled={loading}>
            {loading ? "检测中…" : "开始自检"}
          </button>
          {selfCheck && (
            <div style={{ marginTop: 16 }}>
              <div className="row">
                <span className="label">连通性</span>
                <Tag tone={selfCheck.reachable ? "ok" : "err"}>
                  {selfCheck.reachable ? "可达" : "不可达"}
                </Tag>
              </div>
              <div className="row">
                <span className="label">时钟偏移</span>
                <span>{selfCheck.time_offset_ms} ms</span>
              </div>
              <div className="row">
                <span className="label">接口地址</span>
                <span>{selfCheck.base_url}</span>
              </div>
              {selfCheck.account && (
                <>
                  <div className="row">
                    <span className="label">账户等级</span>
                    <span>{selfCheck.account.acct_lv ?? "-"}</span>
                  </div>
                  <div className="row">
                    <span className="label">持仓模式</span>
                    <span>{selfCheck.account.pos_mode ?? "-"}</span>
                  </div>
                </>
              )}
            </div>
          )}
          {error && <div className="error">{error}</div>}
        </section>
      </div>

      <div style={{ marginTop: 24 }}>
        <pre>{JSON.stringify({ health, selfCheck }, null, 2)}</pre>
      </div>
    </div>
  );
}
