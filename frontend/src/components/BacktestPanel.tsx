import { useState } from "react";
import { EquityChart } from "./EquityChart";

type BacktestTrade = {
  side: string;
  entry_ts: number;
  entry_px: number;
  exit_ts: number;
  exit_px: number;
  pnl: number;
  fee: number;
  reason: string;
  bars: number;
};

type BacktestSummary = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_pnl: number;
  return_pct: number;
  final_equity: number;
  max_drawdown_pct: number;
  profit_factor: number | null;
  avg_win: number;
  avg_loss: number;
  fees: number;
};

type BacktestResult = {
  inst_id: string;
  bar: string;
  candles: number;
  range: { start: number; end: number };
  summary: BacktestSummary;
  equity: { ts: number; equity: number }[];
  trades: BacktestTrade[];
};

const REASON_TEXT: Record<string, string> = {
  signal: "信号平仓",
  stop: "止损",
  end: "到期强平",
};

const BAR_OPTIONS = ["1m", "5m", "15m", "1H", "4H"];
const LIMIT_OPTIONS = [300, 500, 800, 1000, 1440];

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleString("zh-CN", { hour12: false });
}

/** 回测面板：用历史 K 线复现双均线策略，输出收益、回撤与逐笔交易。 */
export function BacktestPanel({ instIds }: { instIds: string[] }) {
  const [instId, setInstId] = useState(instIds[0] ?? "BTC-USDT-SWAP");
  const [bar, setBar] = useState("1m");
  const [limit, setLimit] = useState(500);
  const [fast, setFast] = useState("9");
  const [slow, setSlow] = useState("21");
  const [atrPeriod, setAtrPeriod] = useState("14");
  const [atrMultiple, setAtrMultiple] = useState("1.5");
  const [size, setSize] = useState("0.01");
  const [cooldown, setCooldown] = useState("3");
  const [trailing, setTrailing] = useState(true);
  const [feeRate, setFeeRate] = useState("0.0005");
  const [slippage, setSlippage] = useState("0");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const run = async () => {
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/v1/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          inst_id: instId,
          bar,
          limit,
          fast: Number(fast),
          slow: Number(slow),
          atr_period: Number(atrPeriod),
          atr_multiple: Number(atrMultiple),
          size,
          cooldown_bars: Number(cooldown),
          trailing,
          fee_rate: Number(feeRate),
          slippage_rate: Number(slippage),
        }),
      });
      const body = (await response.json()) as {
        detail?: { message?: string } | string;
      } & Partial<BacktestResult>;
      if (!response.ok) {
        const detail = body.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message ?? "回测失败");
      }
      setResult(body as BacktestResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : "回测失败");
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="card">
      <h2>策略回测</h2>
      <div className="trade-form">
        <div className="field">
          <label>合约</label>
          <select value={instId} onChange={(event) => setInstId(event.target.value)}>
            {Array.from(new Set([...instIds, instId])).map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>周期</label>
          <select value={bar} onChange={(event) => setBar(event.target.value)}>
            {BAR_OPTIONS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>K 线数量</label>
          <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
            {LIMIT_OPTIONS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>快线</label>
          <input value={fast} onChange={(event) => setFast(event.target.value)} />
        </div>
        <div className="field">
          <label>慢线</label>
          <input value={slow} onChange={(event) => setSlow(event.target.value)} />
        </div>
        <div className="field">
          <label>ATR 周期</label>
          <input value={atrPeriod} onChange={(event) => setAtrPeriod(event.target.value)} />
        </div>
        <div className="field">
          <label>ATR 倍数</label>
          <input value={atrMultiple} onChange={(event) => setAtrMultiple(event.target.value)} />
        </div>
        <div className="field">
          <label>张数</label>
          <input value={size} onChange={(event) => setSize(event.target.value)} />
        </div>
        <div className="field">
          <label>冷却 K 线</label>
          <input value={cooldown} onChange={(event) => setCooldown(event.target.value)} />
        </div>
        <div className="field">
          <label>手续费率</label>
          <input value={feeRate} onChange={(event) => setFeeRate(event.target.value)} />
        </div>
        <div className="field">
          <label>滑点率</label>
          <input value={slippage} onChange={(event) => setSlippage(event.target.value)} />
        </div>
        <div className="field">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={trailing}
              onChange={(event) => setTrailing(event.target.checked)}
            />
            <span>移动止损</span>
          </label>
        </div>
      </div>

      <div className="trade-actions">
        <button className="btn-long" onClick={() => void run()} disabled={loading}>
          {loading ? "回测中..." : "运行回测"}
        </button>
      </div>

      {error && <div className="notice err">{error}</div>}

      {result && (
        <>
          <div className="hint">
            {result.inst_id} · {result.bar} · {result.candles} 根 ·{" "}
            {fmtTime(result.range.start)} ~ {fmtTime(result.range.end)}
          </div>
          <div className="stats">
            <div className="stat">
              <span className="label">净盈亏</span>
              <strong className={result.summary.net_pnl >= 0 ? "up" : "down"}>
                {result.summary.net_pnl.toFixed(2)}
              </strong>
            </div>
            <div className="stat">
              <span className="label">收益率</span>
              <strong className={result.summary.return_pct >= 0 ? "up" : "down"}>
                {result.summary.return_pct.toFixed(2)}%
              </strong>
            </div>
            <div className="stat">
              <span className="label">胜率</span>
              <strong>{result.summary.win_rate.toFixed(1)}%</strong>
            </div>
            <div className="stat">
              <span className="label">交易次数</span>
              <strong>{result.summary.trades}</strong>
            </div>
            <div className="stat">
              <span className="label">最大回撤</span>
              <strong>{result.summary.max_drawdown_pct.toFixed(2)}%</strong>
            </div>
            <div className="stat">
              <span className="label">盈亏比</span>
              <strong>{result.summary.profit_factor == null ? "-" : result.summary.profit_factor.toFixed(2)}</strong>
            </div>
            <div className="stat">
              <span className="label">手续费</span>
              <strong>{result.summary.fees.toFixed(2)}</strong>
            </div>
          </div>

          <EquityChart
            points={result.equity.map((point) => ({ ts: point.ts, eq: String(point.equity) }))}
          />

          {result.trades.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>方向</th>
                  <th>开仓时间</th>
                  <th>开仓价</th>
                  <th>平仓时间</th>
                  <th>平仓价</th>
                  <th>持仓 K 线</th>
                  <th>盈亏</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {result.trades
                  .slice(-30)
                  .reverse()
                  .map((trade, index) => (
                    <tr key={`${trade.entry_ts}-${index}`}>
                      <td className={trade.side === "long" ? "up" : "down"}>
                        {trade.side === "long" ? "多" : "空"}
                      </td>
                      <td>{fmtTime(trade.entry_ts)}</td>
                      <td>{trade.entry_px}</td>
                      <td>{fmtTime(trade.exit_ts)}</td>
                      <td>{trade.exit_px}</td>
                      <td>{trade.bars}</td>
                      <td className={trade.pnl >= 0 ? "up" : "down"}>{trade.pnl.toFixed(2)}</td>
                      <td>{REASON_TEXT[trade.reason] ?? trade.reason}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
