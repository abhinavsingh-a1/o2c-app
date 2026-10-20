import { useState } from "react";

import type { PurchaseOrder } from "../api/types";
import { isDecimal, lineTotal, normalize, sumMoney } from "../decimal";
import { money } from "../format";

type LineDraft = {
  line_no: number;
  material_number: string;
  description: string;
  quantity: string;
  unit: string;
  unit_price: string;
};

type Props = {
  initial: PurchaseOrder;
  busy: boolean;
  onCancel: () => void;
  onSave: (po: PurchaseOrder) => void;
};

export default function EditForm({ initial, busy, onCancel, onSave }: Props) {
  const [customerName, setCustomerName] = useState(initial.customer_name);
  const [customerNumber, setCustomerNumber] = useState(initial.customer_number ?? "");
  const [poDate, setPoDate] = useState(initial.po_date);
  const [deliveryDate, setDeliveryDate] = useState(initial.delivery_date ?? "");
  const [shipTo, setShipTo] = useState(initial.ship_to_address);
  const [lines, setLines] = useState<LineDraft[]>(
    initial.lines.map((l) => ({
      line_no: l.line_no,
      material_number: l.material_number ?? "",
      description: l.description,
      quantity: String(l.quantity),
      unit: l.unit,
      unit_price: String(l.unit_price),
    })),
  );
  const [error, setError] = useState<string | null>(null);

  const totals = lines.map((l) => lineTotal(l.quantity, l.unit_price));
  const orderTotal = sumMoney(totals);

  function update(index: number, field: keyof LineDraft, value: string) {
    setLines((current) => current.map((l, i) => (i === index ? { ...l, [field]: value } : l)));
  }

  function save() {
    const bad = lines.find(
      (l) => !l.description.trim() || !isDecimal(l.quantity) || !isDecimal(l.unit_price) ||
        Number(normalize(l.quantity)) <= 0,
    );
    if (bad) {
      setError(`Line ${bad.line_no}: description, a quantity above 0 and a valid price are required.`);
      return;
    }
    if (!customerName.trim() || !shipTo.trim() || !poDate) {
      setError("Customer, PO date and ship-to address are required.");
      return;
    }
    setError(null);
    onSave({
      ...initial,
      customer_name: customerName.trim(),
      customer_number: customerNumber.trim() || null,
      po_date: poDate,
      delivery_date: deliveryDate || null,
      ship_to_address: shipTo.trim(),
      lines: lines.map((l, i) => ({
        line_no: l.line_no,
        material_number: l.material_number.trim() || null,
        description: l.description.trim(),
        quantity: normalize(l.quantity),
        unit: l.unit.trim(),
        unit_price: normalize(l.unit_price),
        line_total: totals[i],
      })),
      total_amount: orderTotal,
    });
  }

  return (
    <div className="panel">
      <h2>Edit order</h2>
      <div className="form-grid">
        <label>
          Customer
          <input value={customerName} onChange={(e) => setCustomerName(e.target.value)} />
        </label>
        <label>
          Customer number
          <input value={customerNumber} onChange={(e) => setCustomerNumber(e.target.value)} />
        </label>
        <label>
          PO date
          <input type="date" value={poDate} onChange={(e) => setPoDate(e.target.value)} />
        </label>
        <label>
          Delivery date
          <input type="date" value={deliveryDate} onChange={(e) => setDeliveryDate(e.target.value)} />
        </label>
        <label className="wide">
          Ship to
          <input value={shipTo} onChange={(e) => setShipTo(e.target.value)} />
        </label>
      </div>

      <table className="grid edit-table">
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
          {lines.map((l, i) => (
            <tr key={l.line_no}>
              <td>{l.line_no}</td>
              <td>
                <input value={l.material_number} onChange={(e) => update(i, "material_number", e.target.value)} />
              </td>
              <td>
                <input value={l.description} onChange={(e) => update(i, "description", e.target.value)} />
              </td>
              <td>
                <input className="num" value={l.quantity} onChange={(e) => update(i, "quantity", e.target.value)} />
              </td>
              <td>
                <input value={l.unit} onChange={(e) => update(i, "unit", e.target.value)} />
              </td>
              <td>
                <input className="num" value={l.unit_price} onChange={(e) => update(i, "unit_price", e.target.value)} />
              </td>
              <td className="num">{money(totals[i], initial.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="total-line">
        Order total: <strong>{money(orderTotal, initial.currency)}</strong>
      </p>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button className="primary" disabled={busy} onClick={save}>
          Save and check again
        </button>
        <button disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
