import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { AuditEntry } from "../api/types";
import { label } from "../format";

type Props = { idempotencyKey: string; reload: number };

export default function History({ idempotencyKey, reload }: Props) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    api
      .GET("/reviews/{idempotency_key}/audit", {
        params: { path: { idempotency_key: idempotencyKey } },
      })
      .then(({ data }) => {
        if (!cancelled && data) setEntries(data);
      });
    return () => {
      cancelled = true;
    };
  }, [idempotencyKey, reload]);

  if (entries.length === 0) return null;

  return (
    <>
      <h2>Decision history</h2>
      <table className="grid">
        <thead>
          <tr>
            <th>When</th>
            <th>Who</th>
            <th>Action</th>
            <th>Result</th>
            <th>Comment</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}>
              <td>{new Date(e.decided_at).toLocaleString()}</td>
              <td>{e.reviewer_name}</td>
              <td>{e.action}</td>
              <td>
                {label(e.result_status)}
                {e.sales_order_number ? ` (${e.sales_order_number})` : ""}
              </td>
              <td>{e.comment ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
