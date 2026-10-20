export function money(value: string | number, currency: string): string {
  const amount = Number(value).toLocaleString("de-AT", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${amount} ${currency}`;
}

export function label(status: string): string {
  return status.replace(/_/g, " ");
}
