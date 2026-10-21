import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PurchaseOrder } from "../api/types";
import EditForm from "./EditForm";

const initial: PurchaseOrder = {
  po_number: "B-26/0852",
  po_date: "2026-01-21",
  customer_name: "Salzach Kunststofftechnik GmbH",
  customer_number: "1000107",
  currency: "EUR",
  delivery_date: "2026-02-10",
  ship_to_address: "Münchner Bundesstraße 140, 5020 Salzburg, Austria",
  lines: [
    {
      line_no: 1,
      material_number: null,
      description: "Keilriemen SPZ 1250",
      quantity: "170.5",
      unit: "PC",
      unit_price: "2.00",
      line_total: "341.00",
    },
    {
      line_no: 2,
      material_number: "400120",
      description: "Stretchfolie 500 mm x 300 m, 23 my",
      quantity: "2",
      unit: "ROL",
      unit_price: "10.00",
      line_total: "20.00",
    },
  ],
  total_amount: "361.00",
};

// The cells of a line row, in column order.
const MATERIAL = 0;
const DESCRIPTION = 1;
const QUANTITY = 2;
const PRICE = 4;

function renderForm() {
  const onSave = vi.fn();
  const onCancel = vi.fn();
  render(<EditForm initial={initial} busy={false} onSave={onSave} onCancel={onCancel} />);
  return { onSave, onCancel };
}

/** The table row of a line (row 0 is the header). */
function lineRow(lineNo: number): HTMLElement {
  return screen.getAllByRole("row")[lineNo];
}

function lineInput(lineNo: number, column: number): HTMLInputElement {
  return within(lineRow(lineNo)).getAllByRole("textbox")[column] as HTMLInputElement;
}

function change(input: HTMLElement, value: string) {
  fireEvent.change(input, { target: { value } });
}

function orderTotal(): string {
  return screen.getByText(/^Order total:/).querySelector("strong")!.textContent!;
}

function save() {
  fireEvent.click(screen.getByRole("button", { name: "Save and check again" }));
}

afterEach(cleanup);

describe("EditForm", () => {
  it("renders every line", () => {
    renderForm();

    expect(screen.getAllByRole("row")).toHaveLength(1 + initial.lines.length);
    expect(lineInput(1, DESCRIPTION).value).toBe("Keilriemen SPZ 1250");
    expect(lineInput(1, MATERIAL).value).toBe("");
    expect(lineInput(2, DESCRIPTION).value).toBe("Stretchfolie 500 mm x 300 m, 23 my");
    expect(lineInput(2, MATERIAL).value).toBe("400120");
    expect(orderTotal()).toBe("361,00 EUR");
  });

  it("updates the line total and the order total when a price changes", () => {
    renderForm();

    change(lineInput(1, PRICE), "2.15");

    // 170.5 x 2.15 = 366.575 -> rounded half up to 366.58
    expect(within(lineRow(1)).getByText("366,58 EUR")).toBeTruthy();
    expect(within(lineRow(2)).getByText("20,00 EUR")).toBeTruthy();
    expect(orderTotal()).toBe("386,58 EUR");
  });

  it("saves normalized strings with recalculated totals", () => {
    const { onSave, onCancel } = renderForm();

    change(lineInput(1, QUANTITY), " 170,5 ");
    change(lineInput(1, PRICE), "2,15");
    change(lineInput(1, DESCRIPTION), "  Keilriemen SPZ 1250  ");
    change(lineInput(2, MATERIAL), "  ");
    save();

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({
      ...initial,
      lines: [
        {
          line_no: 1,
          material_number: null,
          description: "Keilriemen SPZ 1250",
          quantity: "170.5",
          unit: "PC",
          unit_price: "2.15",
          line_total: "366.58",
        },
        {
          line_no: 2,
          material_number: null,
          description: "Stretchfolie 500 mm x 300 m, 23 my",
          quantity: "2",
          unit: "ROL",
          unit_price: "10.00",
          line_total: "20.00",
        },
      ],
      total_amount: "386.58",
    });
    expect(onCancel).not.toHaveBeenCalled();
    expect(document.querySelector(".error")).toBeNull();
  });

  it("does not save a line without description", () => {
    const { onSave } = renderForm();

    change(lineInput(2, DESCRIPTION), "   ");
    save();

    expect(screen.getByText(/^Line 2: description, a quantity above 0/)).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("does not save a line with quantity 0", () => {
    const { onSave } = renderForm();

    change(lineInput(1, QUANTITY), "0");
    save();

    expect(screen.getByText(/^Line 1: description, a quantity above 0/)).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("calls onCancel on Cancel", () => {
    const { onSave, onCancel } = renderForm();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });
});
