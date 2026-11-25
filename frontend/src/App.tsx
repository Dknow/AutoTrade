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

type StreamMessage = {
  type: string;
  data?: unknown;
  market?: { candles?: string[][]; ticker?: Ticker };
  account?: { enabled?: boolean; positions?: Position[] };
};

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

export default function App() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastTimeRef = useRef<number>(0);

  const [ticker, setTicker] = useState<Ticker | null>(null);
  const [connected, setConnected] = useState(false);
  const [accountEnabled, setAccountEnabled] = useState(false);
  const [positions, setPositions] = useState<Position[]>([]);

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

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let pingTimer: number | undefined;
    let disposed = false;

    const handleMessage = (message: StreamMessage) => {
      if (message.type === "snapshot") {
        const rows = message.market?.candles ?? [];
        const data = rows.map(toCandle).sort((a, b) => a.time - b.time);
        seriesRef.current?.setData(data);
        lastTimeRef.current = data.length ? Number(data[data.length - 1].time) : 0;
        if (message.market?.ticker) setTicker(message.market.ticker);
        setAccountEnabled(Boolean(message.account?.enabled));
        setPositions(message.account?.positions ?? []);
        return;
      }
      if (message.type === "ticker") {
        setTicker(message.data as Ticker);
        return;
      }
      if (message.type === "candle") {
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

  return (
    <div className="container">
      <header className="topbar">
        <div>
          <h1>BTC-USDT-SWAP</h1>
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
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
