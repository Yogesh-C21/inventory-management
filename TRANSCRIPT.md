# Stantech Backend Task — Engineering Session Notes

**Service**: Multi-tenant inventory and order service (FastAPI + SQLAlchemy + SQLite)  
**Scope**: Add `POST /orders/{order_id}/reserve`, review and fix existing production code  
**Time**: ~60 minutes  

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Assumptions Made](#2-assumptions-made)
3. [Scale Model](#3-scale-model)
4. [Problems Identified in the Original Code](#4-problems-identified-in-the-original-code)
5. [Changes Made](#5-changes-made)
6. [Reserve Endpoint — Design Decisions](#6-reserve-endpoint--design-decisions)
7. [Testing — API Calls and Responses](#7-testing--api-calls-and-responses)
8. [Trade-offs](#8-trade-offs)
9. [What Was Deliberately Left Alone](#9-what-was-deliberately-left-alone)
10. [Open Questions and Discussion Points](#10-open-questions-and-discussion-points)

---

## 1. System Overview

The service is a shared-database multi-tenant system. Every HTTP request carries an `X-Tenant-Id` header that identifies which tenant the caller belongs to. All core tables (`warehouses`, `products`, `customers`, `stock_levels`, `orders`, `order_lines`) carry a `tenant_id` column.

**Tenants seeded:**

| Tenant ID | Name               |
|-----------|--------------------|
| 1         | Northwind Traders  |
| 2         | Globex             |

**Warehouses seeded:**

| Warehouse ID | Tenant | Name                          |
|--------------|--------|-------------------------------|
| 1            | 1      | Central (Northwind Traders)   |
| 2            | 1      | Overflow (Northwind Traders)  |
| 3            | 2      | Central (Globex)              |
| 4            | 2      | Overflow (Globex)             |

Each tenant has 40 products (SKUs `NW-1001..NW-1040` and `GX-1001..GX-1040`). Every product has a `StockLevel` row per warehouse, seeded at `quantity_available = 25`. Orders 1–2 belong to Northwind, orders 3–4 to Globex.

---

## 2. Assumptions Made

### Reservation semantics
- "Reserve" means: atomically move quantity from `quantity_available` to `quantity_reserved` for every line on the order at the specified warehouse.
- The order's `status` transitions from `pending` → `reserved`.
- A separate fulfilment step (not in scope) would later move `reserved` → `fulfilled` and decrement `quantity_reserved`.

### Idempotency key
- The fulfilment agent retries on timeout. A retry must be safe.
- Idempotency key: `(order.status == "reserved") AND (order.reserved_warehouse_id == warehouse_id)`.
- If both match, return the current order state immediately — no stock is touched.
- If the order is `reserved` but at a *different* warehouse, that is a conflict (409), not a retry.

### All-or-nothing
- Partial reservation is worse than no reservation — it leaves the order in an inconsistent state and makes fulfilment logic harder.
- All stock checks are done before any writes. If any line fails, the entire request is rejected with no side effects.

### Warehouse ownership
- A tenant must not be able to reserve stock at another tenant's warehouse, even if they know the warehouse ID.
- Warehouse is validated against `tenant_id` before any stock is touched.

### Status machine
- Only `pending` orders can be reserved. Any other status (e.g. `cancelled`, `fulfilled`) returns 409 with a clear message.

---

## 3. Scale Model

This is a low-scale, high-correctness system. The constraints that shaped every decision:

- **SQLite single-writer**: only one write transaction executes at a time. This means `SELECT ... FOR UPDATE` serialises concurrent reservations at the DB level — no distributed locking needed.
- **Flash sales**: many concurrent reservation requests for the same product. The risk is a read-modify-write race where two requests both read `quantity_available = 5`, both pass the check, and both deduct — resulting in negative stock (oversell). `with_for_update()` eliminates this.
- **Fulfilment agent retries**: the agent calls reserve and retries on timeout. Without idempotency, a retry after a successful-but-slow commit would double-deduct stock.
- **No horizontal scaling assumed**: a single SQLite file, single process. If this moved to Postgres with multiple workers, the locking strategy would still be correct (Postgres supports `SELECT FOR UPDATE` natively), but connection pool sizing and lock timeout configuration would need attention.

---

## 4. Problems Identified in the Original Code

### 4.1 N+1 Query Problem — `GET /orders` and `GET /orders/{order_id}`

**What it was**: Both endpoints loaded orders, then for each order accessed `order.customer`, `order.lines`, and for each line `line.product` — all as lazy loads. For a list of 100 orders with 2 lines each, this produced ~300 SQL queries per request.

**Why it matters**: Support reported the order list feels slow. This was the cause.

**Fix**: Added `_order_query()` helper using `joinedload` to eager-load all relationships in a single query (one JOIN per relationship level).

```python
def _order_query(db: Session):
    return db.query(Order).options(
        joinedload(Order.customer),
        joinedload(Order.lines).joinedload(OrderLine.product),
    )
```

---

### 4.2 Missing Tenant Filter on `GET /orders/{order_id}`

**What it was**: The original `get_order` endpoint filtered only by `order_id`, with no `tenant_id` check:

```python
# Original — broken
order = db.query(Order).filter(Order.id == order_id).first()
```

**Why it matters**: Tenant A could fetch Tenant B's order by guessing the integer ID. Order 4 in the seed data contains a note: `"Confidential: 40% negotiated discount, Q4 renewal - do not share externally"`. This was a live data leak.

**Fix**: Added `Order.tenant_id == tenant_id` to the filter.

```python
order = _order_query(db).filter(Order.id == order_id, Order.tenant_id == tenant_id).first()
```

---

### 4.3 Race Condition on `POST /stock/adjust`

**What it was**: The original adjust endpoint did a plain read then write:

```python
# Original — race condition
stock = db.query(StockLevel).filter(...).first()
stock.quantity_available += payload.delta
db.commit()
```

Two concurrent requests could both read the same value, both add their delta, and one write would silently overwrite the other (lost update).

**Why it matters**: Support reported stock was oversold during high-traffic periods. This was one of the two causes (the other being the lack of any locking in the reserve path, which didn't exist yet).

**Fix**: Added `.with_for_update()` to serialise concurrent adjustments.

```python
stock = db.query(StockLevel).filter(...).with_for_update().first()
```

---

### 4.4 No Row-Level Security

**What it was**: Tenant isolation was enforced only where the developer remembered to add a `tenant_id` filter. The missing filter on `get_order` proved this was not reliable.

**Fix**: Added a SQLAlchemy session-level `do_orm_execute` event that automatically injects a `tenant_id` filter on every ORM SELECT against tenant-scoped tables. The explicit filters in endpoints remain as belt-and-suspenders.

```python
_current_tenant: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_tenant", default=None
)

@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(execute_state):
    tenant_id = _current_tenant.get()
    if tenant_id is None or not execute_state.is_select:
        return
    mapper = execute_state.bind_mapper
    if mapper is None:
        return
    table = mapper.persist_selectable
    if table.name not in _TENANT_SCOPED_TABLES or not hasattr(table.c, "tenant_id"):
        return
    execute_state.statement = execute_state.statement.filter(
        table.c.tenant_id == tenant_id
    )
```

The `_current_tenant` ContextVar is set per-request in the `current_tenant_id` dependency, so it is safe under async concurrency.

---

### 4.5 Missing Indexes

**What it was**: No indexes beyond primary keys. Every tenant-scoped query was a full table scan.

**Indexes added:**

| Table          | Index                                              | Reason                                                        |
|----------------|----------------------------------------------------|---------------------------------------------------------------|
| `orders`       | `(tenant_id)`                                      | `GET /orders` filters by tenant                               |
| `stock_levels` | `(warehouse_id, tenant_id)`                        | `GET /stock` filters by warehouse + tenant                    |
| `stock_levels` | `(warehouse_id, tenant_id, product_id)`            | Reserve endpoint locks rows by product within warehouse+tenant |
| `order_lines`  | `(order_id)`                                       | Relationship load from order → lines                          |
| `warehouses`   | `(tenant_id)`                                      | Warehouse ownership check in reserve                          |
| `products`     | `(tenant_id)`                                      | RLS filter on product lookups                                 |
| `customers`    | `(tenant_id)`                                      | RLS filter on customer lookups                                |

---

## 5. Changes Made

### Schema additions

**`StockLevel`**: Added `quantity_reserved: Mapped[int]` (default 0).  
Tracks how much stock is committed to reserved orders but not yet fulfilled. Allows the warehouse UI to distinguish available vs. committed stock without querying orders.

**`Order`**: Added `reserved_warehouse_id: Mapped[Optional[int]]` (nullable FK to `warehouses`).  
Required for idempotency check — the agent may retry with the same warehouse ID, and we need to know which warehouse was already reserved.

### New endpoint

`POST /orders/{order_id}/reserve` — see Section 6.

### Summary of all file changes

| Area                  | Change                                                                 |
|-----------------------|------------------------------------------------------------------------|
| Imports               | Added `contextvars`, `event`, `Index`, `joinedload`                    |
| RLS                   | `_current_tenant` ContextVar + `_apply_tenant_filter` session event    |
| `StockLevel` model    | Added `quantity_reserved` column + composite indexes                   |
| `Order` model         | Added `reserved_warehouse_id` column + `tenant_id` index               |
| `Warehouse/Product/Customer` | Added `index=True` on `tenant_id`                             |
| `OrderLine`           | Added `index=True` on `order_id`                                       |
| `current_tenant_id`   | Now sets `_current_tenant` ContextVar                                  |
| `_order_query()`      | New helper — eager loads customer + lines + products                   |
| `list_orders`         | Uses `_order_query()`                                                  |
| `get_order`           | Uses `_order_query()` + added `tenant_id` filter                       |
| `adjust_stock`        | Added `.with_for_update()`                                             |
| `serialize_stock`     | Added `quantity_reserved` to output                                    |
| `ReserveIn`           | New Pydantic model                                                     |
| `reserve_order`       | New endpoint                                                           |

---

## 6. Reserve Endpoint — Design Decisions

```
POST /orders/{order_id}/reserve
Header: X-Tenant-Id: <int>
Body:   {"warehouse_id": <int>}
```

### Execution flow

1. Load order, assert it belongs to the calling tenant.
2. Idempotency check: if already `reserved` at the same warehouse, return immediately.
3. Status check: reject anything that is not `pending`.
4. Warehouse ownership check: reject if warehouse does not belong to the calling tenant.
5. Collect all `product_id`s from order lines, sort them ascending.
6. Lock all matching `StockLevel` rows with `SELECT ... FOR UPDATE` in sorted `product_id` order.
7. Validate all lines against available stock — no writes yet.
8. If all pass: deduct `quantity_available`, increment `quantity_reserved` for each line.
9. Set `order.status = "reserved"`, `order.reserved_warehouse_id = warehouse_id`.
10. Commit.

### Why sorted lock order?

If two concurrent requests reserve different orders that share products, and each locks rows in arbitrary order, they can deadlock (A holds product 5, waits for product 7; B holds product 7, waits for product 5). Sorting by `product_id` before acquiring locks ensures all transactions acquire locks in the same order, making a cycle impossible.

### Why validate before writing?

Partial reservation is worse than no reservation. If we deduct line 1 and then fail on line 2, we have to roll back — but the rollback path is more complex and error-prone than simply checking everything first. The validate-then-write pattern keeps the happy path and the error path both simple.

### HTTP status codes

| Scenario                                      | Status |
|-----------------------------------------------|--------|
| Success                                       | 200    |
| Idempotent retry (same warehouse, already reserved) | 200 |
| Order not found / warehouse not found         | 404    |
| Order not in `pending` status                 | 409    |
| Insufficient stock for any line               | 409    |
| No stock record for a product at the warehouse | 422   |

---

## 7. Testing — API Calls and Responses

All tests run against a freshly seeded database (`python app.py`). Stock starts at `quantity_available = 25` for all products at all warehouses.

---

### 7.1 Happy Path — Reserve Order 1 at Warehouse 1 (Northwind)

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/1/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 1}'
```

**Response (200):**
```json
{
  "id": 1,
  "customer_name": "Acme Retail",
  "notes": "Standard terms",
  "status": "reserved",
  "lines": [
    {"product_sku": "NW-1001", "product_name": "Component 1", "quantity": 3},
    {"product_sku": "NW-1002", "product_name": "Component 2", "quantity": 2}
  ]
}
```

**Stock verification:**
```bash
curl -s "http://localhost:8000/stock?warehouse_id=1" -H "X-Tenant-Id: 1"
```

NW-1001: `quantity_available: 22, quantity_reserved: 3`  
NW-1002: `quantity_available: 23, quantity_reserved: 2`  
All other products: `quantity_available: 25, quantity_reserved: 0`

---

### 7.2 Idempotency — Retry Same Request

**Request:** Same as 7.1 (order 1, warehouse 1, tenant 1).

**Response (200):** Identical to 7.1. No change to stock levels.

**Verified**: Stock levels unchanged after second call. `quantity_available` still 22 and 23 respectively.

---

### 7.3 Tenant Isolation — Northwind Cannot Read Globex Order

**Request:**
```bash
curl -s http://localhost:8000/orders/3 -H "X-Tenant-Id: 1"
```

**Response (404):**
```json
{"detail": "Order not found"}
```

Order 3 belongs to Globex (tenant 2). Northwind (tenant 1) receives a 404 — not a 403, which would confirm the order exists. This is intentional: leaking existence is also a data leak.

---

### 7.4 Cross-Tenant Warehouse — Northwind Cannot Reserve at Globex Warehouse

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/1/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 3}'
```

Note: Order 1 was already reserved in 7.1. To test this cleanly, reseed and use a different pending order.

**Request (order 2, warehouse 3):**
```bash
curl -s -X POST http://localhost:8000/orders/2/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 3}'
```

**Response (404):**
```json
{"detail": "Warehouse not found"}
```

Warehouse 3 belongs to Globex. Northwind receives a 404 — same reasoning as above.

---

### 7.5 Insufficient Stock

**Setup**: Adjust stock for NW-1003 at warehouse 1 down to 2 (order 2 needs 5).

```bash
# Find stock_level_id for NW-1003 at warehouse 1
curl -s "http://localhost:8000/stock?warehouse_id=1" -H "X-Tenant-Id: 1"
# Adjust down by 23 to leave quantity_available = 2
curl -s -X POST http://localhost:8000/stock/adjust \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"stock_level_id": 3, "delta": -23}'
```

**Reserve attempt:**
```bash
curl -s -X POST http://localhost:8000/orders/2/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 1}'
```

**Response (409):**
```json
{
  "detail": "Insufficient stock for product NW-1003: need 5, have 2"
}
```

**Verified**: Stock levels for all products at warehouse 1 are unchanged — the all-or-nothing guarantee held.

---

### 7.6 Wrong Status — Attempt to Reserve an Already-Reserved Order at a Different Warehouse

**Setup**: Order 1 is `reserved` at warehouse 1 (from 7.1).

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/1/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 2}'
```

**Response (409):**
```json
{
  "detail": "Order is 'reserved', only pending orders can be reserved"
}
```

---

### 7.7 Confidential Order Isolation — Globex Order 4

**Request (Northwind trying to read Globex's confidential order):**
```bash
curl -s http://localhost:8000/orders/4 -H "X-Tenant-Id: 1"
```

**Response (404):**
```json
{"detail": "Order not found"}
```

Order 4 contains `"Confidential: 40% negotiated discount, Q4 renewal - do not share externally"`. This was accessible to any tenant in the original code. Fixed.

---

## 8. Trade-offs

### SQLite `SELECT FOR UPDATE` behaviour

SQLite does not implement row-level locking. `with_for_update()` on SQLite causes SQLAlchemy to emit a `BEGIN IMMEDIATE` transaction, which acquires a write lock on the entire database file for the duration of the transaction. This means:

- Under concurrent reservations, requests serialise at the DB level — correct, but throughput is limited.
- This is acceptable for the stated scale. If this moved to Postgres, `SELECT FOR UPDATE` would give true row-level locking and much better concurrency.

### RLS via session event vs. application-level filters

The session event approach catches ORM SELECTs automatically but does not cover raw `text()` queries or `connection.execute()` calls. The explicit `tenant_id` filters in each endpoint remain in place as a second layer. The result is that `tenant_id` appears twice in some generated SQL (`tenant_id = 1 AND tenant_id = 1`). This is harmless — the query planner eliminates the duplicate predicate — but it is worth noting.

### `quantity_reserved` on `StockLevel`

An alternative is to compute reserved quantity on the fly by joining `orders` and `order_lines`. That approach is always consistent but expensive. Storing it denormalised on `StockLevel` is fast but requires careful maintenance: any code path that changes order status must also update `quantity_reserved`. Currently only the reserve endpoint does this. A future `cancel` or `fulfil` endpoint must decrement it.

### 404 vs. 403 for cross-tenant access

Returning 404 instead of 403 when a tenant accesses another tenant's resource prevents existence leakage. The downside is that a misconfigured client gets a confusing error. This is the standard approach for multi-tenant SaaS and is the right call here.

### No `updated_at` on `Order`

The order model has `created_at` but no `updated_at`. After reservation, there is no timestamp for when the status changed. This makes debugging and audit harder. Not fixed in this session — it is a schema addition that requires a migration and was out of scope.

---

## 9. What Was Deliberately Left Alone

### Authentication

The `X-Tenant-Id` header is caller-supplied with no verification. In production this must come from a verified JWT or session token. Left alone because the task brief explicitly states the header is a simplification for the exercise.

### Input validation on `StockAdjustIn.delta`

`delta` accepts any integer including very large values. There is no upper bound. A malicious or buggy caller could set `quantity_available` to an arbitrarily large number. Not fixed — adding a `Field(ge=-10_000, le=10_000)` constraint would be the right move but was not a stated bug.

### No `cancel` or `fulfil` endpoints

These would need to decrement `quantity_reserved` (cancel) or decrement `quantity_reserved` and leave `quantity_available` unchanged (fulfil). Without them, `quantity_reserved` only ever grows. Left out of scope — the task asked for the reserve endpoint only.

### Seed data structure

The seed creates one `Customer` row per order (high cardinality by design, per the comment in the code). This is unusual but intentional in the seed — left as-is.

### Error handling granularity on `list_stock`

`GET /stock` returns an empty list if the warehouse does not exist or belongs to another tenant. It does not return 404. This is a minor UX issue — a caller cannot distinguish "warehouse exists but is empty" from "warehouse does not exist". Not fixed — it is a behaviour change that could break existing clients.

### Logging and observability

No structured logging, no request IDs, no tracing. In production this would be the first thing to add. Out of scope for this exercise.

---

## 10. Open Questions and Discussion Points

### Architecture

**Q: When does SQLite become the bottleneck?**  
The `BEGIN IMMEDIATE` lock on reserve means all concurrent reservations serialise. For a flash sale with hundreds of requests per second, this becomes a queue. The fix is Postgres with row-level locking, or a queue-based reservation system (reserve requests go into a queue, a worker processes them serially per product). Worth discussing what the actual traffic numbers look like.

**Q: Should `quantity_reserved` live on `StockLevel` or be computed?**  
Denormalised is fast but requires every status-changing endpoint to maintain it. A computed view or a separate `reservations` table (one row per reservation, summed at query time) would be more correct but slower. The right answer depends on read/write ratio and whether the warehouse UI needs real-time reserved counts.

**Q: Should the reserve endpoint be idempotent by order+warehouse, or by a client-supplied idempotency key?**  
The current approach (order+warehouse as the natural key) works for the stated use case. A Stripe-style `Idempotency-Key` header would be more general and would handle cases where the agent retries with different parameters due to a bug. Worth discussing whether the agent is trusted to always retry with the same parameters.

**Q: What happens to `quantity_reserved` when an order is cancelled?**  
Currently nothing — it stays incremented forever. A `POST /orders/{order_id}/cancel` endpoint needs to decrement it. If orders can expire (e.g. unpaid after 24 hours), a background job needs to release reserved stock. Neither exists yet.

### Concurrency

**Q: Deadlock risk with the current sorted-lock approach under Postgres?**  
Sorted lock order prevents deadlocks between two reserve transactions. But a reserve transaction and a concurrent `adjust_stock` transaction could still deadlock if `adjust_stock` acquires its lock in a different order. Under SQLite this cannot happen (whole-file lock). Under Postgres it could. The fix is to ensure `adjust_stock` also acquires its lock in a consistent order relative to reserve — or to use advisory locks.

**Q: What if the fulfilment agent retries with a different warehouse?**  
The current idempotency check is keyed on `(order_id, warehouse_id)`. If the agent retries with a different warehouse (e.g. due to a bug or config change), it gets a 409 because the order is already `reserved`. The agent needs to handle 409 as a terminal error, not a retryable one. This should be documented in the API contract.

### Data model

**Q: Should `reserved_warehouse_id` be on `Order` or on `OrderLine`?**  
Currently the entire order is reserved at one warehouse. If the business ever needs split fulfilment (some lines from warehouse A, some from warehouse B), the model needs to move `reserved_warehouse_id` to `OrderLine`. Worth confirming the business requirement before the model is locked in.

**Q: Is `status` on `Order` the right state machine?**  
Currently: `pending` → `reserved` → (implied) `fulfilled` / `cancelled`. There is no `partially_reserved` state, no `payment_pending` state, no `shipped` state. The status field is a plain string with no enforcement of valid transitions. A proper state machine (even just an Enum + transition validation) would prevent invalid states from being written.

**Q: Should `order_lines` be immutable after creation?**  
There is no endpoint to modify order lines, but there is also no enforcement. If a line is modified after reservation, `quantity_reserved` on `StockLevel` becomes inconsistent. Worth adding a check in any future line-editing endpoint.

### Operational

**Q: How are schema migrations handled?**  
The current code uses `Base.metadata.drop_all` + `create_all` on startup (via `seed()`). This is destructive. In production, Alembic or a similar migration tool is needed. The two new columns (`quantity_reserved`, `reserved_warehouse_id`) would need `ALTER TABLE` migrations with appropriate defaults.

**Q: What is the retry policy of the fulfilment agent?**  
The task brief says it retries on timeout. It does not say how many times, with what backoff, or what it does on a 409. The reserve endpoint is designed to be safe to retry, but the agent's behaviour on non-retryable errors (409, 422) needs to be defined to avoid infinite retry loops.
