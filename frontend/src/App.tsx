import { useEffect, useRef, useState } from "react";
import {
  ColorType,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";

type Ticker = {
  inst_id: string;
  last: string;
  bid: string;
  ask: string;
  open_24h: string;
  high_24h: string;
  low_24h: string;
  vol_24h: string;
  ts: string;
};

type Position = {
  instId: string;
  posSide: string;
  pos: string;
  avgPx: string;
  upl: string;
  lever: string;
  mgnMode: string;
};

type InstrumentSpec = {
  minSz: string;
  lotSz: string;
  tickSz: string;
  maxLever: string;
};

type StreamMessage = {
  type: string;
  inst_id?: string;
  data?: unknown;
  market?: {
    inst_id?: string;
    inst_ids?: string[];
    candles?: string[][];
    ticker?: Ticker;
    tickers?: Record<string, Ticker>;
  };
  account?: { enabled?: boolean; positions?: Position[] };
};

type Notice = { ok: boolean; text: string };

type StrategyRecord = { at: string; event: string; detail: string; ok: boolean };

type StrategyConfigView = {
  inst_id: string;
  fast: number;
  slow: number;
  atr_period: number;
  atr_multiple: number;
  size: string;
  lever: number;
  td_mode: string;
  cooldown_bars: number;
  trailing: boolean;
};

type StrategyState = {
  running: boolean;
  inst_id: string;
  bar: string;
  config: StrategyConfigView;
  last_signal: string | null;
  last_error: string;
  stop_price: string | null;
  stop_spread: string | null;
  stop_algo_ids: string[];
  records: StrategyRecord[];
};

type RiskState = {
  tripped: boolean;
  trip_reason: string;
  tripped_at: string;
  peak_equity: string | null;
  equity: string | null;
  drawdown_pct: string;
  max_drawdown_pct: string;
  max_size: string;
  max_notional: string;
};

type ConfigForm = {
  instId: string;
  fast: string;
  slow: string;
  atrMultiple: string;
  size: string;
  lever: string;
  cooldown: string;
  trailing: boolean;
};

const DEFAULT_INST = "BTC-USDT-SWAP";
const WS_URL = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/api/v1/ws/stream`;

function toCandle(row: string[]): CandlestickData<UTCTimestamp> {
  return {
    time: Math.floor(Number(row[0]) / 1000) as UTCTimestamp,
    open: Number(row[1]),
    high: Number(row[2]),
    low: Number(row[3]),
    close: Number(row[4]),
  };
}

function fmt(value: string | undefined, digits = 2): string {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "-";
}

async function postJson<T>(path: string, body: unknown, method = "POST"): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = (data as { detail?: { message?: string; code?: string } }).detail;
    throw new Error(detail?.message ?? detail?.code ?? `请求失败 (HTTP ${response.status})`);
  }
  return data as T;
}

export default function App() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastTimeRef = useRef<number>(0);

  const [instId, setInstId] = useState(DEFAULT_INST);
  const [instIds, setInstIds] = useState<string[]>([DEFAULT_INST]);
  const [tickers, setTickers] = useState<Record<string, Ticker>>({});
  const instIdRef = useRef(instId);

  const [connected, setConnected] = useState(false);
  const [accountEnabled, setAccountEnabled] = useState(false);
  const [positions, setPositions] = useState<Position[]>([]);

  const [specs, setSpecs] = useState<Record<string, InstrumentSpec>>({});
  const [ordType, setOrdType] = useState<"limit" | "market">("limit");
  const [price, setPrice] = useState("");
  const [size, setSize] = useState("");
  const [lever, setLever] = useState("10");
  const [tp, setTp] = useState("");
  const [sl, setSl] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const [strategy, setStrategy] = useState<StrategyState | null>(null);
  const [risk, setRisk] = useState<RiskState | null>(null);
  const [strategyBusy, setStrategyBusy] = useState(false);
  const [strategyNotice, setStrategyNotice] = useState<Notice | null>(null);
  const [configForm, setConfigForm] = useState<ConfigForm>({
    instId: DEFAULT_INST,
    fast: "9",
    slow: "21",
    atrMultiple: "1.5",
    size: "0.01",
    lever: "10",
    cooldown: "3",
    trailing: true,
  });
  const configInitialized = useRef(false);

  const ticker = tickers[instId] ?? null;
  const spec = specs[instId] ?? null;

  const loadStrategy = async () => {
    const [strategyData, riskData] = await Promise.all([
      fetch("/api/v1/strategy").then((res) => res.json()),
      fetch("/api/v1/risk").then((res) => res.json()),
    ]);
    setStrategy(strategyData as StrategyState);
    setRisk(riskData as RiskState);
  };

  useEffect(() => {
    loadStrategy().catch(() => undefined);
    const timer = window.setInterval(() => {
      loadStrategy().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    instIdRef.current = instId;
  }, [instId]);

  useEffect(() => {
    if (strategy && !configInitialized.current) {
      configInitialized.current = true;
      setConfigForm({
        instId: strategy.config.inst_id,
        fast: String(strategy.config.fast),
        slow: String(strategy.config.slow),
        atrMultiple: String(strategy.config.atr_multiple),
        size: strategy.config.size,
        lever: String(strategy.config.lever),
        cooldown: String(strategy.config.cooldown_bars),
        trailing: strategy.config.trailing,
      });
    }
  }, [strategy]);

  useEffect(() => {
    fetch("/api/v1/trade/instruments")
      .then((res) => res.json())
      .then((data: { specs?: Record<string, InstrumentSpec> }) => {
        setSpecs(data.specs ?? {});
      })
      .catch(() => setNotice({ ok: false, text: "合约规格加载失败，交易功能暂不可用" }));
  }, []);

  useEffect(() => {
    const item = specs[instId];
    setSize(item?.minSz ?? "");
  }, [instId, specs]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      layout: { background: { type: ColorType.Solid, color: "#161b22" }, textColor: "#8b949e" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      width: container.clientWidth,
      height: 420,
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#30363d" },
      rightPriceScale: { borderColor: "#30363d" },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => chart.applyOptions({ width: container.clientWidth });
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // 切换标的时重新拉取历史 K 线，避免图表与所选合约不一致
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/v1/market/candles?instId=${encodeURIComponent(instId)}&bar=1m&limit=200`)
      .then((res) => res.json())
      .then((data: { candles?: string[][] }) => {
        if (cancelled) return;
        const candles = (data.candles ?? []).map(toCandle).sort((a, b) => a.time - b.time);
        seriesRef.current?.setData(candles);
        lastTimeRef.current = candles.length ? Number(candles[candles.length - 1].time) : 0;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [instId]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let pingTimer: number | undefined;
    let disposed = false;

    const handleMessage = (message: StreamMessage) => {
      if (message.type === "snapshot") {
        const ids = message.market?.inst_ids ?? [];
        if (ids.length > 0) setInstIds(ids);
        const incoming = message.market?.tickers ?? {};
        setTickers((prev) => ({ ...prev, ...incoming }));
        if (message.market?.ticker?.inst_id) {
          const first = message.market.ticker;
          setTickers((prev) => ({ ...prev, [first.inst_id]: first }));
        }
        // 快照里的 K 线只属于默认标的，切到别的标的时交给 REST 加载
        if (message.market?.inst_id === instIdRef.current) {
          const rows = message.market?.candles ?? [];
          const data = rows.map(toCandle).sort((a, b) => a.time - b.time);
          seriesRef.current?.setData(data);
          lastTimeRef.current = data.length ? Number(data[data.length - 1].time) : 0;
        }
        setAccountEnabled(Boolean(message.account?.enabled));
        setPositions(message.account?.positions ?? []);
        return;
      }
      if (message.type === "ticker") {
        const target = message.inst_id;
        if (target) {
          const data = message.data as Ticker;
          setTickers((prev) => ({ ...prev, [target]: data }));
        }
        return;
      }
      if (message.type === "candle") {
        if (message.inst_id && message.inst_id !== instIdRef.current) return;
        for (const row of (message.data as string[][]) ?? []) {
          const candle = toCandle(row);
          const time = Number(candle.time);
          if (time >= lastTimeRef.current) {
            seriesRef.current?.update(candle);
            lastTimeRef.current = time;
          }
        }
        return;
      }
      if (message.type === "positions") {
        setPositions((message.data as Position[]) ?? []);
      }
    };

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      socket = ws;
      ws.onopen = () => {
        setConnected(true);
        pingTimer = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 15000);
      };
      ws.onmessage = (event) => {
        try {
          handleMessage(JSON.parse(event.data) as StreamMessage);
        } catch {
          // 忽略无法解析的消息
        }
      };
      ws.onclose = () => {
        setConnected(false);
        window.clearInterval(pingTimer);
        if (!disposed) reconnectTimer = window.setTimeout(connect, 3000);
      };
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(reconnectTimer);
      window.clearInterval(pingTimer);
      socket?.close();
    };
  }, []);

  const changePct = ticker && ticker.open_24h
    ? ((Number(ticker.last) - Number(ticker.open_24h)) / Number(ticker.open_24h)) * 100
    : 0;

  const openPosition = async (posSide: "long" | "short") => {
    if (busy) return;
    const quantity = Number(size);
    if (!Number.isFinite(quantity) || quantity <= 0) {
      setNotice({ ok: false, text: "请填写有效的下单张数" });
      return;
    }
    const limitPrice = Number(price || ticker?.last || 0);
    if (ordType === "limit" && limitPrice <= 0) {
      setNotice({ ok: false, text: "限价单需要有效价格" });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await postJson("/trade/orders", {
        inst_id: instId,
        pos_side: posSide,
        size: quantity,
        ord_type: ordType,
        price: ordType === "limit" ? limitPrice : null,
        lever: Number(lever) > 0 ? Number(lever) : null,
        tp_trigger_px: tp ? Number(tp) : null,
        sl_trigger_px: sl ? Number(sl) : null,
      });
      setNotice({ ok: true, text: `${posSide === "long" ? "开多" : "开空"} ${quantity} 张已提交` });
    } catch (error) {
      setNotice({ ok: false, text: (error as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const closePosition = async (targetInstId: string, posSide: string) => {
    if (busy) return;
    if (!window.confirm(`确认平掉 ${targetInstId} 的${posSide === "long" ? "多" : "空"}仓？`)) return;
    setBusy(true);
    setNotice(null);
    try {
      await postJson("/trade/positions/close", {
        inst_id: targetInstId,
        pos_side: posSide,
        ord_type: "market",
      });
      setNotice({ ok: true, text: "平仓已提交" });
    } catch (error) {
      setNotice({ ok: false, text: (error as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const startStrategy = async () => {
    if (strategyBusy) return;
    setStrategyBusy(true);
    setStrategyNotice(null);
    try {
      setStrategy(await postJson<StrategyState>("/strategy/start", {}));
      setStrategyNotice({ ok: true, text: "策略已启动" });
    } catch (error) {
      setStrategyNotice({ ok: false, text: (error as Error).message });
    } finally {
      setStrategyBusy(false);
    }
  };

  const stopStrategy = async () => {
    if (strategyBusy) return;
    setStrategyBusy(true);
    setStrategyNotice(null);
    try {
      setStrategy(await postJson<StrategyState>("/strategy/stop", {}));
      setStrategyNotice({ ok: true, text: "策略已停止" });
    } catch (error) {
      setStrategyNotice({ ok: false, text: (error as Error).message });
    } finally {
      setStrategyBusy(false);
    }
  };

  const saveConfig = async () => {
    if (strategyBusy) return;
    setStrategyBusy(true);
    setStrategyNotice(null);
    try {
      const updated = await postJson<StrategyState>(
        "/strategy/config",
        {
          inst_id: configForm.instId.trim().toUpperCase(),
          fast: Number(configForm.fast),
          slow: Number(configForm.slow),
          atr_multiple: Number(configForm.atrMultiple),
          size: Number(configForm.size),
          lever: Number(configForm.lever),
          cooldown_bars: Number(configForm.cooldown),
          trailing: configForm.trailing,
        },
        "PATCH",
      );
      setStrategy(updated);
      setStrategyNotice({ ok: true, text: "参数已保存" });
    } catch (error) {
      setStrategyNotice({ ok: false, text: (error as Error).message });
    } finally {
      setStrategyBusy(false);
    }
  };

  const killSwitch = async () => {
    if (!window.confirm("Kill Switch：停止策略并市价全平所有持仓，确认执行？")) return;
    setStrategyBusy(true);
    setStrategyNotice(null);
    try {
      await postJson("/risk/kill-switch", { close_all: true, reason: "前端手动触发" });
      await loadStrategy();
      setStrategyNotice({ ok: true, text: "Kill Switch 已执行" });
    } catch (error) {
      setStrategyNotice({ ok: false, text: (error as Error).message });
    } finally {
      setStrategyBusy(false);
    }
  };

  const resetRisk = async () => {
    setStrategyBusy(true);
    setStrategyNotice(null);
    try {
      setRisk(await postJson<RiskState>("/risk/reset", {}));
      setStrategyNotice({ ok: true, text: "熔断已复位" });
    } catch (error) {
      setStrategyNotice({ ok: false, text: (error as Error).message });
    } finally {
      setStrategyBusy(false);
    }
  };

  return (
    <div className="container">
      <header className="topbar">
        <div>
          <div className="inst-switch">
            <select value={instId} onChange={(event) => setInstId(event.target.value)}>
              {Array.from(new Set([...instIds, strategy?.inst_id, DEFAULT_INST].filter(Boolean) as string[])).map(
                (item) => (
                  <option value={item} key={item}>
                    {item}
                  </option>
                ),
              )}
            </select>
            <h1>{instId}</h1>
          </div>
          <div className="subtitle">USDT 本位永续 · 双向持仓 · 1m K 线</div>
        </div>
        <div className="price-block">
          <div className={`price ${changePct >= 0 ? "up" : "down"}`}>{fmt(ticker?.last, 1)}</div>
          <div className={`change ${changePct >= 0 ? "up" : "down"}`}>
            {changePct >= 0 ? "+" : ""}
            {changePct.toFixed(2)}%
          </div>
        </div>
        <div className="stats">
          <div className="stat">
            <span className="label">24h 高</span>
            <span>{fmt(ticker?.high_24h, 1)}</span>
          </div>
          <div className="stat">
            <span className="label">24h 低</span>
            <span>{fmt(ticker?.low_24h, 1)}</span>
          </div>
          <div className="stat">
            <span className="label">买一 / 卖一</span>
            <span>
              {fmt(ticker?.bid, 1)} / {fmt(ticker?.ask, 1)}
            </span>
          </div>
        </div>
        <span className={`tag ${connected ? "ok" : "err"}`}>{connected ? "实时已连接" : "连接中断"}</span>
      </header>

      <section className="card chart-card">
        <div ref={containerRef} className="chart" />
      </section>

      <section className="card trade-card">
        <h2>下单</h2>
        <div className="trade-form">
          <div className="field">
            <label>类型</label>
            <select value={ordType} onChange={(event) => setOrdType(event.target.value as "limit" | "market")}>
              <option value="limit">限价</option>
              <option value="market">市价</option>
            </select>
          </div>
          <div className="field">
            <label>价格 (USDT)</label>
            <input
              type="number"
              value={price}
              disabled={ordType === "market"}
              placeholder={ticker?.last ? fmt(ticker.last, 1) : "最新价"}
              onChange={(event) => setPrice(event.target.value)}
            />
          </div>
          <div className="field">
            <label>张数</label>
            <input
              type="number"
              value={size}
              step={spec?.lotSz ?? "0.01"}
              min={spec?.minSz ?? "0.01"}
              onChange={(event) => setSize(event.target.value)}
            />
          </div>
          <div className="field">
            <label>杠杆</label>
            <input
              type="number"
              value={lever}
              min="1"
              max={spec?.maxLever ?? "100"}
              onChange={(event) => setLever(event.target.value)}
            />
          </div>
          <div className="field">
            <label>止盈价</label>
            <input type="number" value={tp} placeholder="可选" onChange={(event) => setTp(event.target.value)} />
          </div>
          <div className="field">
            <label>止损价</label>
            <input type="number" value={sl} placeholder="可选" onChange={(event) => setSl(event.target.value)} />
          </div>
        </div>
        <div className="trade-actions">
          <button className="btn-long" disabled={busy || !accountEnabled} onClick={() => openPosition("long")}>
            开多 / 买入
          </button>
          <button className="btn-short" disabled={busy || !accountEnabled} onClick={() => openPosition("short")}>
            开空 / 卖出
          </button>
          <span className="label hint">
            最小 {spec?.minSz ?? "-"} 张 · 步长 {spec?.lotSz ?? "-"} · 价格精度 {spec?.tickSz ?? "-"}
          </span>
        </div>
        {notice && <div className={notice.ok ? "notice ok" : "notice err"}>{notice.text}</div>}
        {!accountEnabled && <div className="label">未配置 OKX 凭据，无法下单。</div>}
      </section>

      <section className="card strategy-card">
        <h2>策略 · 双均线 + ATR 止损</h2>
        <div className="status-row">
          <span className={`tag ${strategy?.running ? "ok" : "warn"}`}>
            {strategy?.running ? "运行中" : "已停止"}
          </span>
          <span className="label">
            信号：{strategy?.last_signal === "long" ? "多" : strategy?.last_signal === "short" ? "空" : "无"}
          </span>
          <span className="label">周期：{strategy?.bar ?? "-"}</span>
          <span className="label">标的：{strategy?.inst_id ?? "-"}</span>
          <span className="label">
            止损：
            {strategy?.config.trailing
              ? strategy?.stop_spread
                ? `移动止损 · 回调价差 ${strategy.stop_spread}（交易所维护）`
                : "未挂单"
              : strategy?.stop_price
                ? `固定止损 ${strategy.stop_price}`
                : "未挂单"}
          </span>
          {strategy?.last_error && <span className="label err">最近错误：{strategy.last_error}</span>}
        </div>
        <div className="trade-form">
          <div className="field">
            <label>交易标的</label>
            <input
              type="text"
              value={configForm.instId}
              disabled={strategy?.running}
              placeholder="如 ETH-USDT-SWAP"
              onChange={(event) => setConfigForm({ ...configForm, instId: event.target.value })}
            />
          </div>
          <div className="field">
            <label>快线</label>
            <input
              type="number"
              value={configForm.fast}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, fast: event.target.value })}
            />
          </div>
          <div className="field">
            <label>慢线</label>
            <input
              type="number"
              value={configForm.slow}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, slow: event.target.value })}
            />
          </div>
          <div className="field">
            <label>ATR 倍数</label>
            <input
              type="number"
              step="0.1"
              value={configForm.atrMultiple}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, atrMultiple: event.target.value })}
            />
          </div>
          <div className="field">
            <label>下单张数</label>
            <input
              type="number"
              value={configForm.size}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, size: event.target.value })}
            />
          </div>
          <div className="field">
            <label>杠杆</label>
            <input
              type="number"
              value={configForm.lever}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, lever: event.target.value })}
            />
          </div>
          <div className="field">
            <label>冷却 K 线</label>
            <input
              type="number"
              value={configForm.cooldown}
              disabled={strategy?.running}
              onChange={(event) => setConfigForm({ ...configForm, cooldown: event.target.value })}
            />
          </div>
          <div className="field">
            <label>移动止损</label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={configForm.trailing}
                disabled={strategy?.running}
                onChange={(event) => setConfigForm({ ...configForm, trailing: event.target.checked })}
              />
              <span>{configForm.trailing ? "开启：交易所移动止损（ATR 价差）" : "关闭：固定条件止损"}</span>
            </label>
          </div>
        </div>
        <div className="trade-actions">
          <button onClick={startStrategy} disabled={strategyBusy || strategy?.running}>
            启动策略
          </button>
          <button className="btn-ghost" onClick={stopStrategy} disabled={strategyBusy || !strategy?.running}>
            停止策略
          </button>
          <button className="btn-ghost" onClick={saveConfig} disabled={strategyBusy || strategy?.running}>
            保存参数
          </button>
        </div>
        {strategyNotice && (
          <div className={strategyNotice.ok ? "notice ok" : "notice err"}>{strategyNotice.text}</div>
        )}
        {strategy && strategy.records.length > 0 && (
          <div className="records">
            {strategy.records.slice(0, 12).map((record, index) => (
              <div className={`record ${record.ok ? "" : "err"}`} key={`${record.at}-${index}`}>
                <span className="label">{record.at.slice(11, 19)}</span>
                <span className="event">{record.event}</span>
                <span>{record.detail}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <h2>风控</h2>
        <div className="risk-grid">
          <div className="stat">
            <span className="label">当前权益</span>
            <span>{risk?.equity ?? "-"} USDT</span>
          </div>
          <div className="stat">
            <span className="label">峰值权益</span>
            <span>{risk?.peak_equity ?? "-"} USDT</span>
          </div>
          <div className="stat">
            <span className="label">当前回撤</span>
            <span className={Number(risk?.drawdown_pct ?? 0) >= Number(risk?.max_drawdown_pct ?? 0) ? "down" : ""}>
              {risk?.drawdown_pct ?? "-"}% / 阈值 {risk?.max_drawdown_pct ?? "-"}%
            </span>
          </div>
          <div className="stat">
            <span className="label">单笔上限</span>
            <span>
              {risk?.max_size ?? "-"} 张 / {risk?.max_notional ?? "-"} USDT
            </span>
          </div>
        </div>
        {risk?.tripped && <div className="notice err">已熔断：{risk.trip_reason}</div>}
        <div className="trade-actions">
          <button className="btn-short" onClick={killSwitch} disabled={strategyBusy}>
            Kill Switch（停策略 + 全平）
          </button>
          <button className="btn-ghost" onClick={resetRisk} disabled={strategyBusy}>
            复位熔断
          </button>
        </div>
      </section>

      <section className="card">
        <h2>持仓</h2>
        {!accountEnabled && <div className="label">未配置 OKX 凭据，私有频道未启动。</div>}
        {accountEnabled && positions.length === 0 && <div className="label">当前无持仓。</div>}
        {positions.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>合约</th>
                <th>方向</th>
                <th>张数</th>
                <th>均价</th>
                <th>杠杆</th>
                <th>未实现盈亏</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <tr key={`${position.instId}-${position.posSide}`}>
                  <td>{position.instId}</td>
                  <td className={position.posSide === "long" ? "up" : "down"}>
                    {position.posSide === "long" ? "多" : position.posSide === "short" ? "空" : position.posSide}
                  </td>
                  <td>{position.pos}</td>
                  <td>{position.avgPx}</td>
                  <td>{position.lever}x</td>
                  <td className={Number(position.upl) >= 0 ? "up" : "down"}>{fmt(position.upl)}</td>
                  <td>
                    <button
                      className="btn-ghost"
                      disabled={busy}
                      onClick={() => closePosition(position.instId, position.posSide)}
                    >
                      市价全平
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
