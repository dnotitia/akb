---
type: table
title: orders
description: One row per completed order.
resource: akb://acme-analytics/coll/tables/table/orders
tags:
- sales
- orders
timestamp: "2026-06-13T14:30:00+00:00"
sql_name: vt_orders
row_count: 1200000
---

One row per completed order.

# Schema

| Column | Type | Description |
| --- | --- | --- |
| order_id | uuid | Unique identifier for the order |
| customer_id | uuid | References the customer |
| total | numeric | Order total in minor units |
| created_at | timestamptz | When the order completed |

# Rows

1200000 rows (data lives in AKB; this concept carries the schema, not the rows).
