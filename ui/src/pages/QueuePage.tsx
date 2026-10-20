import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api } from "../api/client";
import type { ReviewSummary } from "../api/types";
import { label, money } from "../format";

const FILTERS = [
  { value: "awaiting_review", label: "Waiting for review" },
  { value: "posted", label: "Posted" },
  { value: "rejected", label: "Rejected" },
  { value: "all", label: "All" },
];

export default function QueuePage() {
  const [status, setStatus] = useState("awaiting_review");
  const [reload, setReload] = useState(0);
  const [rows, setRows] = useState<ReviewSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.GET("/reviews", { params: { query: { status } } }).then(({ data, error }) => {
      if (cancelled) return;
      if (error || !data) {
        setError("Could not load the review queue. Is the agent running?");
        setRows([]);
        return;
      }
      setError(null);
      setRows(data);
    });
    return () => {
      cancelled = true;
    };
  }, [status, reload]);

  return (
    <section>
      <div className="toolbar">
        <h1>Review queue</h1>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {FILTERS.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
        <button onClick={() => setReload((r) => r + 1)}>Refresh</button>
      </div>

      {error && <p className="error">{error}</p>}

      {rows === null ? (
        <p>Loading…</p>
      ) : rows.length === 0 ? (
        <p className="empty">No orders in this view.</p>
      ) : (
        <table className="grid">
          <thead>
            <tr>
              <th>PO number</th>
              <th>Customer</th>
              <th className="num">Total</th>
              <th>Issues</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.idempotency_key}>
                <td>
                  <Link to={`/reviews/${r.idempotency_key}`}>{r.po_number}</Link>
                </td>
                <td>{r.customer_name}</td>
                <td className="num">{money(r.total_amount, r.currency)}</td>
                <td>
                  {r.issue_codes.map((code, i) => (
                    <span key={`${code}-${i}`} className="chip">
                      {code}
                    </span>
                  ))}
                </td>
                <td>
                  <span className={`status status-${r.status}`}>{label(r.status)}</span>
                </td>
                <td>{new Date(r.updated_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
