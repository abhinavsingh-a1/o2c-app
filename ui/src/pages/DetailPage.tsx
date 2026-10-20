import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import { api } from "../api/client";
import type { ReviewDetail } from "../api/types";
import { label, money } from "../format";

export default function DetailPage() {
  const { key = "" } = useParams();
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .GET("/reviews/{idempotency_key}", { params: { path: { idempotency_key: key } } })
      .then(({ data, error }) => {
        if (cancelled) return;
        if (error || !data) setError("This order was not found.");
        else setDetail(data);
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    api
      .GET("/reviews/{idempotency_key}/document", {
        params: { path: { idempotency_key: key } },
        parseAs: "blob",
      })
      .then(({ data }) => {
        if (cancelled || !data) return;
        url = URL.createObjectURL(data);
        setPdfUrl(url);
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [key]);

  if (error) {
    return (
      <p className="error">
        {error} <Link to="/">Back to the queue</Link>
      </p>
    );
  }
  if (!detail) return <p>Loading…</p>;

  const po = detail.purchase_order;
  const flaggedLines = new Set(
    detail.issues.map((issue) => issue.line_no).filter((n): n is number => n != null),
  );

  return (
    <section>
      <p>
        <Link to="/">← Back to the queue</Link>
      </p>
      <div className="toolbar">
        <h1>{po.po_number}</h1>
        <span className={`status status-${detail.status}`}>{label(detail.status)}</span>
      </div>

      <div className="split">
        <div className="pdf">
          {pdfUrl ? (
            <iframe title="Purchase order PDF" src={pdfUrl} />
          ) : (
            <p>Loading document…</p>
          )}
        </div>

        <div className="facts">
          {detail.explanation && (
            <div className="explanation">
              <h2>Why this order needs you</h2>
              <p>{detail.explanation}</p>
            </div>
          )}

          {detail.issues.length > 0 && (
            <>
              <h2>Issues</h2>
              <ul className="issues">
                {detail.issues.map((issue, i) => (
                  <li key={i}>
                    <span className="chip">{issue.code}</span> {issue.message}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h2>Order</h2>
          <dl className="fields">
            <dt>Customer</dt>
            <dd>
              {po.customer_name} {detail.customer_number ? `(${detail.customer_number})` : ""}
            </dd>
            <dt>PO date</dt>
            <dd>{po.po_date}</dd>
            <dt>Delivery date</dt>
            <dd>{po.delivery_date ?? "—"}</dd>
            <dt>Ship to</dt>
            <dd>{po.ship_to_address}</dd>
            <dt>Total</dt>
            <dd>{money(po.total_amount, po.currency)}</dd>
            {detail.sales_order_number && (
              <>
                <dt>Sales order</dt>
                <dd>{detail.sales_order_number}</dd>
              </>
            )}
          </dl>

          <h2>Lines</h2>
          <table className="grid">
            <thead>
              <tr>
                <th>#</th>
                <th>Material</th>
                <th>Description</th>
                <th className="num">Qty</th>
                <th>Unit</th>
                <th className="num">Price</th>
                <th className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {po.lines.map((line) => (
                <tr key={line.line_no} className={flaggedLines.has(line.line_no) ? "flagged" : ""}>
                  <td>{line.line_no}</td>
                  <td>{line.material_number ?? "—"}</td>
                  <td>{line.description}</td>
                  <td className="num">{line.quantity}</td>
                  <td>{line.unit}</td>
                  <td className="num">{money(line.unit_price, po.currency)}</td>
                  <td className="num">{money(line.line_total, po.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
