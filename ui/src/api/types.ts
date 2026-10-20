import type { components } from "./schema";

export type ReviewSummary = components["schemas"]["ReviewSummary"];
export type ReviewDetail = components["schemas"]["ReviewDetail"];
export type Issue = components["schemas"]["Issue"];
export type PurchaseOrder = ReviewDetail["purchase_order"];
export type LineItem = PurchaseOrder["lines"][number];
export type AuditEntry = components["schemas"]["AuditEntry"];
export type User = components["schemas"]["User"];
