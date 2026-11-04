# Postmortem: incident #61, exception spike on orders (+2 more)

| | |
|---|---|
| Outcome | Resolved by Sherlog: roll back orders to its previous version, recovery verified |
| Impact | 2m 02s (08:06:49 to 08:08:51 UTC) |
| Requests affected | 307 of 2,783 shop requests failed (11.0%) |
| Services | orders, gateway, payments |
| Detect / mitigate / recover | 0 s / 52 s / 2m 02s (from the first anomaly, then from detection) |
| Seen before | incident #30 (orders), fixed by rollback |
| Model usage | 4 calls, 18,962 tokens, $0.0000 |

## Summary

Sherlog detected the incident 0 s after the first anomaly (orders: log spike "Unhandled KeyError while handling POST /orders" (26/min)). Sherlog ran "roll back orders to its previous version" (requested by e2e-operator-1793779597) and verified that the services were back to baseline 70 s later. Service was back to normal after 2m 02s in total.

## Root cause

No confident diagnosis was reached. The graph heuristic pointed at **orders** (orders is the deepest anomalous service: its callers gateway are failing while its own dependencies look healthy; first anomaly at 2026-11-04T08:06:49.111675+00:00.).

## Timeline (UTC)

| Time | Event |
|---|---|
| 08:06:49 | First anomaly: orders: log spike "Unhandled KeyError while handling POST /orders" (26/min) |
| 08:06:49 | Incident detected |
| 08:07:34 | Evidence funnel: 14,465 log lines -> 3 templates, 1 exceptions, 3 traces -> ~2,867 tokens |
| 08:07:34 | Seen before: incident #30 (orders), fixed by rollback |
| 08:07:34 | Investigation started (fake models) |
| 08:07:34 | Fake triage: the graph heuristic points at orders. |
| 08:07:34 | Root cause: orders (unknown, confidence 0.00). 18,962 tokens, $0.0000 |
| 08:07:34 | Escalated to a human: the investigator recommends it (root cause orders / unknown, confidence 0.00) |
| 08:07:37 | e2e-operator-1793779597 asked Sherlog to roll back orders to its previous version |
| 08:07:37 | Executing: roll back orders to its previous version |
| 08:07:41 | Done: roll back orders to its previous version. Verifying recovery of gateway, orders (up to 180 s) |
| 08:09:21 | Verified: back to baseline 70 s after the action |
| 08:09:21 | Resolved by Sherlog: roll back orders to its previous version |

The investigator made 3 tool calls; the full trail is in the incident timeline.

## Follow-up actions

- [ ] Prioritize the permanent fix: this happened before, in incident #30 (orders), fixed by rollback
- [ ] Confirm the root cause by hand and record it
