import { useCallback, useEffect, useState } from "react";

type PendingOrder = {
  ordId: string;
  instId: string;
  side: string;
  posSide?: string;
  ordType: string;
  px: string;
  sz: string;
  state: string;
};

type AlgoOrder = {
  algoId: string;
  instId: string;
  side: string;
  posSide?: string;
  ordType: string;
  sz: string;
  slTriggerPx?: string;
  tpTriggerPx?: string;
  callbackSpread?: string;
  callbackRatio?: string;
  state: string;
};

const ORDER_TYPE_TEXT: Record<string, string> = {
  limit: "限价",
  market: "市价",
  post_only: "只做 Maker",
  fok: "FOK",
  ioc: "IOC",
  conditional: "条件止损",
  move_order_stop: "移动止损",
  trigger: "计划委托",
  oco: "OCO",
};

function sideText(side: string, posSide?: string): string {
  const action = side === "buy" ? "买" : "卖";
  const pos = posSide === "long" ? "多" : posSide === "short" ? "空" : "";
  return pos ? `${action}/${pos}` : action;
}

function algoPrice(order: AlgoOrder): string {
  if (order.ordType === "move_order_stop") {
    if (order.callbackSpread) return `价差 ${order.callbackSpread}`;
    if (order.callbackRatio) return `比例 ${Number(order.callbackRatio) * 100}%`;
  }
  const parts: string[] = [];
  if (order.slTriggerPx) parts.push(`损 ${order.slTriggerPx}`);
  if (order.tpTriggerPx) parts.push(`盈 ${order.tpTriggerPx}`);
  return parts.join(" / ") || "-";
}

/** 挂单面板：普通委托 + 条件单/移动止损，支持撤单。 */
export function OrdersPanel() {
  const [orders, setOrders] = useState<PendingOrder[]>([]);
  const [algos, setAlgos] = useState<AlgoOrder[]>([]);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [pendingRes, algoRes] = await Promise.all([
        fetch("/api/v1/trade/orders/pending"),
        fetch("/api/v1/trade/algo-orders"),
      ]);
      if (!pendingRes.ok || !algoRes.ok) {
        setError("挂单加载失败（可能未配置凭据）");
        return;
      }
      const pending = (await pendingRes.json()) as { orders?: PendingOrder[] };
      const algo = (await algoRes.json()) as { orders?: AlgoOrder[] };
      setOrders(pending.orders ?? []);
      setAlgos(algo.orders ?? []);
      setError("");
    } catch {
      setError("挂单加载失败");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load]);

  const cancel = async (kind: "order" | "algo", id: string, instId: string) => {
    if (busyId) return;
    if (!window.confirm(`确认撤销 ${instId} 的这张${kind === "algo" ? "条件单" : "委托"}？`)) return;
    setBusyId(id);
    setError("");
    try {
      const url = kind === "algo" ? `/api/v1/trade/algo-orders/${id}/cancel` : `/api/v1/trade/orders/${id}/cancel`;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ inst_id: instId }),
      });
      if (!res.ok) {
        const body = (await res.json()) as { detail?: { message?: string } | string };
        const detail = body.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message ?? "撤单失败");
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤单失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="card">
      <h2>挂单</h2>
      {error && <div className="notice err">{error}</div>}
      {orders.length === 0 && algos.length === 0 && !error && <div className="label">当前无挂单。</div>}
      {orders.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>合约</th>
              <th>方向</th>
              <th>类型</th>
              <th>价格</th>
              <th>张数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.ordId}>
                <td>{order.instId}</td>
                <td className={order.side === "buy" ? "up" : "down"}>{sideText(order.side, order.posSide)}</td>
                <td>{ORDER_TYPE_TEXT[order.ordType] ?? order.ordType}</td>
                <td>{order.px || "市价"}</td>
                <td>{order.sz}</td>
                <td>
                  <button
                    className="btn-ghost"
                    disabled={busyId === order.ordId}
                    onClick={() => void cancel("order", order.ordId, order.instId)}
                  >
                    撤单
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {algos.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>合约</th>
              <th>方向</th>
              <th>类型</th>
              <th>触发条件</th>
              <th>张数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {algos.map((order) => (
              <tr key={order.algoId}>
                <td>{order.instId}</td>
                <td className={order.side === "buy" ? "up" : "down"}>{sideText(order.side, order.posSide)}</td>
                <td>{ORDER_TYPE_TEXT[order.ordType] ?? order.ordType}</td>
                <td>{algoPrice(order)}</td>
                <td>{order.sz}</td>
                <td>
                  <button
                    className="btn-ghost"
                    disabled={busyId === order.algoId}
                    onClick={() => void cancel("algo", order.algoId, order.instId)}
                  >
                    撤单
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
