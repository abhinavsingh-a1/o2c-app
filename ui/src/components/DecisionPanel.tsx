import { useState } from "react";

import { api } from "../api/client";
import type { PurchaseOrder } from "../api/types";
import EditForm from "./EditForm";

type Props = {
  idempotencyKey: string;
  purchaseOrder: PurchaseOrder;
  onDecided: (message: string) => void;
};

type Action = "approve" | "reject" | "edit";

export default function DecisionPanel({ idempotencyKey, purchaseOrder, onDecided }: Props) {
  const [comment, setComment] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(action: Action, editedPo?: PurchaseOrder) {
    if (action === "reject" && !comment.trim()) {
      setError("Please give a reason for rejecting this order.");
      return;
    }
    if (action === "approve" && !window.confirm("Post this order to the ERP exactly as it is?")) {
      return;
    }
    setBusy(true);
    setError(null);
    const { data, error, response } = await api.POST("/reviews/{idempotency_key}", {
      params: { path: { idempotency_key: idempotencyKey } },
      body: { action, comment: comment.trim() || null, edited_po: editedPo ?? null },
    });
    setBusy(false);
    if (response.status === 409) {
      onDecided("This order has already been decided (maybe in another tab).");
      return;
    }
    if (error || !data) {
      setError("The decision could not be saved. Please try again.");
      return;
    }
    setEditing(false);
    if (data.status === "posted") onDecided(`Posted as sales order ${data.sales_order_number}.`);
    else if (data.status === "rejected") onDecided("The order was rejected.");
    else onDecided("The corrected order still has issues. Please review it again.");
  }

  return (
    <div className="panel">
      <h2>Your decision</h2>
      <div className="form-grid">
        <label className="wide">
          Comment (required for reject)
          <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} />
        </label>
      </div>
      {error && <p className="error">{error}</p>}

      {editing ? (
        <EditForm
          initial={purchaseOrder}
          busy={busy}
          onCancel={() => setEditing(false)}
          onSave={(po) => submit("edit", po)}
        />
      ) : (
        <div className="actions">
          <button className="primary" disabled={busy} onClick={() => submit("approve")}>
            Approve
          </button>
          <button disabled={busy} onClick={() => setEditing(true)}>
            Edit order
          </button>
          <button className="danger" disabled={busy} onClick={() => submit("reject")}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
