import { useState } from "react";
import { type Status, useLiveSocket } from "./useLiveSocket";

export type OrderStatus =
  | "pending_new"
  | "new"
  | "partially_filled"
  | "filled"
  | "pending_cancel"
  | "canceled"
  | "rejected"
  | "expired";

export type Order = {
  cl_ord_id: string;
  trader: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  price: number;
  tif: string;
  status: OrderStatus;
  order_id: string;
  cum_qty: number;
  avg_px: number;
  leaves: number;
  text: string;
  created: string;
  updated: string;
  source?: "app" | "electronx"; // electronx: entered on the ElectronX screen, seen through the drop copy
  exchange_user?: string; // the trader's ElectronX id, on orders from the ElectronX screen
};

export const LIVE: OrderStatus[] = ["pending_new", "new", "partially_filled", "pending_cancel"];

/** The orders this user may see, from the api's /ws/orders WebSocket, kept by ClOrdID. */
export function useOrders(
  token: string,
  onUnauthorized: () => void,
): { orders: Record<string, Order>; status: Status } {
  const [orders, setOrders] = useState<Record<string, Order>>({});
  const status = useLiveSocket(
    "/ws/orders",
    token,
    (msg) => {
      if (msg.type === "order") {
        const order = msg.data as Order;
        setOrders((prev) => ({ ...prev, [order.cl_ord_id]: order }));
      }
    },
    onUnauthorized,
  );
  return { orders, status };
}
