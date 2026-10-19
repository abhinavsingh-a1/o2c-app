# ADR-003: Events and Kafka topics

Date: 2026-10-19
Status: Accepted

## Context
Services must hand work to each other without losing it when one crashes,
and every step must be traceable for audit.

## Decision
| Topic | Producer | Consumer | Retention |
|---|---|---|---|
| docs.received | intake | classification | 7 days |
| docs.classified | classification | extraction | 7 days |
| orders.extracted | extraction | agent | 7 days |
| orders.validated | agent | (audit) | 7 days |
| orders.exceptions | agent | review UI, ServiceNow | 30 days |
| orders.posted | agent | CRM (Zapier) | 30 days |
| orders.dlq | any consumer | DLQ replay tool | 30 days |

- Message key = idempotency key of the document, so all events of one PO
  land in the same partition and stay in order.
- 3 partitions per topic; replication 1 locally, 3 in AWS.
- Delivery is at-least-once: a message can arrive twice, so every consumer
  must be idempotent (processed_events table).
- After N failed attempts a message goes to orders.dlq with error headers,
  so one bad message never blocks the rest.

## Consequences
- Services are decoupled and can be redeployed independently.
- Events can be replayed after a bug fix.
- More moving parts than direct HTTP calls; consumer lag must be monitored.
