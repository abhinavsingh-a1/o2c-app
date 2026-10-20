// Money maths in whole units, never with floating-point numbers.
// Quantities are scaled to thousandths, prices to cents.

export function normalize(value: string): string {
  return value.trim().replace(",", ".");
}

export function isDecimal(value: string): boolean {
  return /^\d+(\.\d+)?$/.test(normalize(value));
}

function toUnits(value: string, scale: number): number {
  return Math.round(Number(normalize(value)) * scale);
}

/** quantity × unit price, rounded half-up to cents (same rule as the agent). */
export function lineTotal(quantity: string, unitPrice: string): string {
  if (!isDecimal(quantity) || !isDecimal(unitPrice)) return "0.00";
  const product = toUnits(quantity, 1000) * toUnits(unitPrice, 100); // in 1/100000
  const cents = Math.floor((product + 500) / 1000);
  return (cents / 100).toFixed(2);
}

export function sumMoney(values: string[]): string {
  const cents = values.reduce((sum, v) => sum + Math.round(Number(v) * 100), 0);
  return (cents / 100).toFixed(2);
}
