# Stantech Backend Task — Full Engineering Session

**Service**: Multi-tenant inventory and order service (FastAPI + SQLAlchemy + SQLite)  
**Task**: Add `POST /orders/{order_id}/reserve` · Review and fix existing production code  
**Duration**: ~60 minutes  
**Tools used**: Amazon Q (IDE), curl, SQLite CLI  

---

## Table of Contents

1. [Starting Point — Reading the Codebase](#1-starting-point--reading-the-codebase)
2. [Bugs Found in the Original Code](#2-bugs-found-in-the-original-code)
3. [Assumptions and Design Decisions](#3-assumptions-and-design-decisions)
4. [Scale Model](#4-scale-model)
5. [Changes Made — With Rationale](#5-changes-made--with-rationale)
6. [Reserve Endpoint — Full Design](#6-reserve-endpoint--full-design)
7. [Test Suite — Every API Call and Response](#7-test-suite--every-api-call-and-response)
8. [Trade-offs Accepted](#8-trade-offs-accepted)
9. [Deliberately Left Alone](#9-deliberately-left-alone)
10. [Open for Discussion](#10-open-for-discussion)

---

## 1. Starting Point — Reading the Codebase

The service was read top to bottom before touching anything. Key observations noted immediately:

**Data model:**
- Shared database, all tenant data co-located, isolated only by `tenant_id` columns
- Tables: `tenants`, `warehouses`, `products`, `customers`, `stock_levels`, `orders`, `order_lines`
- `StockLevel` tracks `quantity_available` per product per warehouse per tenant
- `Order` has `status` (plain string, default `"pending"`) and `lines` (list of product + quantity)

**Seeded data:**

| Tenant ID | Name              |
|-----------|-------------------|
| 1         | Northwind Traders |
| 2         | Globex            |

| Warehouse ID | Tenant | Name                         |
|--------------|--------|------------------------------|
| 1            | 1      | Central (Northwind Traders)  |
| 2            | 1      | Overflow (Northwind Traders) |
| 3            | 2      | Central (Globex)             |
| 4            | 2      | Overflow (Globex)            |

- Each tenant: 40 products, 2 warehouses, every product stocked at every warehouse at `quantity_available = 25`
- Orders 1–2: Northwind. Orders 3–4: Globex
- Order 4 note: `"Confidential: 40% negotiated discount, Q4 renewal - do not share externally"` — planted deliberately to test tenant isolation

**Existing endpoints:**
- `GET /orders` — list all orders for tenant
- `GET /orders/{order_id}` — get single order
- `GET /stock?warehouse_id=` — list stock levels at a warehouse
- `POST /stock/adjust` — adjust `quantity_available` by a delta

**First read flags (before any changes):**
1. `GET /orders/{order_id}` — no `tenant_id` filter. Any tenant can read any order by ID.
2. `GET /orders` — lazy-loads `customer`, `lines`, and `line.product` per row. N+1 at scale.
3. `POST /stock/adjust` — plain read then write, no lock. Lost update under concurrency.
4. No indexes beyond primary keys.
5. No systematic tenant isolation — enforced only where the developer remembered.

---

## 2. Bugs Found in the Original Code

### Bug 1 — Data Leak: Missing Tenant Filter on `GET /orders/{order_id}`

**Severity**: Critical  
**Original code:**
```python
order = db.query(Order).filter(Order.id == order_id).first()
```

Any authenticated tenant could fetch any other tenant's order by guessing an integer ID. Order 4 (Globex) contained a confidential commercial note. This was a live data leak in the original code.

**Fix:**
```python
order = _order_query(db).filter(Order.id == order_id, Order.tenant_id == tenant_id).first()
```

---

### Bug 2 — Oversell: Race Condition on `POST /stock/adjust`

**Severity**: Critical  
**Original code:**
```python
stock = db.query(StockLevel).filter(...).first()
stock.quantity_available += payload.delta
db.commit()
```

Two concurrent requests both read `quantity_available = 10`. Both pass the `>= 0` check. Both write back `10 + delta`. One write silently overwrites the other — a lost update. This was the direct cause of the reported oversells during high-traffic periods.

**Fix:**
```python
stock = db.query(StockLevel).filter(...).with_for_update().first()
```

---

### Bug 3 — Performance: N+1 Queries on Order Endpoints

**Severity**: High  
**What happened**: Both `GET /orders` and `GET /orders/{order_id}` used SQLAlchemy's default lazy loading. For each order, accessing `order.customer` fired one query, `order.lines` fired one query, and each `line.product` fired one query. For 100 orders with 2 lines each: ~300 SQL queries per request.

This was the direct cause of the reported slow order list.

**Fix**: Added `_order_query()` helper with `joinedload`:
```python
def _order_query(db: Session):
    return db.query(Order).options(
        joinedload(Order.customer),
        joinedload(Order.lines).joinedload(OrderLine.product),
    )
```
Both endpoints now use this helper. The entire result set loads in 3 queries regardless of order count.

---

### Bug 4 — Systemic: No Row-Level Security

**Severity**: High  
**What happened**: Tenant isolation was enforced only where the developer explicitly added a `tenant_id` filter. Bug 1 proved this was not reliable — one missed filter and data leaks. With more endpoints added over time, the probability of another miss increases.

**Fix**: Added a SQLAlchemy session-level event that automatically injects `tenant_id` into every ORM SELECT against tenant-scoped tables:

```python
_current_tenant: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_tenant", default=None
)

_TENANT_SCOPED_TABLES = frozenset([
    "warehouses", "products", "customers", "stock_levels", "orders", "order_lines",
])

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

`_current_tenant` is a `ContextVar` — set per-request in the `current_tenant_id` dependency, safe under async concurrency. Explicit `tenant_id` filters in endpoints remain as belt-and-suspenders. The result is `tenant_id = 1 AND tenant_id = 1` in some queries — harmless, the query planner eliminates the duplicate predicate.

---

### Bug 5 — Performance: No Indexes

**Severity**: Medium  
**What happened**: Every tenant-scoped query was a full table scan. At seed scale (100 orders, 160 stock rows) this is invisible. Under real load it degrades linearly.

**Indexes added:**

| Table          | Index columns                          | Covers                                                    |
|----------------|----------------------------------------|-----------------------------------------------------------|
| `orders`       | `(tenant_id)`                          | `GET /orders` tenant filter                               |
| `stock_levels` | `(warehouse_id, tenant_id)`            | `GET /stock` warehouse + tenant filter                    |
| `stock_levels` | `(warehouse_id, tenant_id, product_id)`| Reserve endpoint — lock rows by product within warehouse  |
| `order_lines`  | `(order_id)`                           | Relationship load order → lines                           |
| `warehouses`   | `(tenant_id)`                          | Warehouse ownership check in reserve                      |
| `products`     | `(tenant_id)`                          | RLS filter on product lookups                             |
| `customers`    | `(tenant_id)`                          | RLS filter on customer lookups                            |

---

## 3. Assumptions and Design Decisions

### What "reserve" means

Reserve atomically moves quantity from `quantity_available` to `quantity_reserved` for every line on the order at the specified warehouse. The order status transitions `pending` → `reserved`. A separate fulfilment step (out of scope) would later move `reserved` → `fulfilled` and decrement `quantity_reserved`.

This was a deliberate choice over alternatives:
- **Soft lock only (no stock deduction)**: simpler, but doesn't prevent oversell — two reservations could both "soft lock" the same stock.
- **Hard deduct with no reserved tracking**: simpler schema, but the warehouse UI loses visibility into committed vs. available stock.
- **Separate reservations table**: more normalised, but adds a join on every stock query. Overkill at this scale.

### Idempotency key

The fulfilment agent retries on timeout. The idempotency key is `(order.status == "reserved") AND (order.reserved_warehouse_id == warehouse_id)`. If both match, return the current order state immediately — no stock is touched, no error is raised.

If the order is `reserved` but at a *different* warehouse, that is a conflict (409), not a retry. The agent should treat 409 as terminal.

### All-or-nothing

All stock checks run before any writes. If any line fails, the entire request is rejected with no side effects. Partial reservation is worse than no reservation — it leaves the order in an inconsistent state and makes downstream fulfilment logic harder to reason about.

### Warehouse ownership

A tenant must not be able to reserve stock at another tenant's warehouse even if they know the warehouse ID. The warehouse is validated against `tenant_id` before any stock is touched. Returns 404 (not 403) to avoid confirming the warehouse exists.

### Status transitions

Only `pending` orders can be reserved. Any other status returns 409 with a message that includes the current status. This prevents double-reservation and makes the state machine explicit even without a formal Enum.

### 404 vs. 403 for cross-tenant access

Returning 404 instead of 403 when a tenant accesses another tenant's resource prevents existence leakage. A 403 would confirm the resource exists. 404 is the standard approach for multi-tenant SaaS.

---

## 4. Scale Model

Every decision was made with this scale model in mind:

- **Single SQLite file, single process** — no distributed systems, no horizontal scaling
- **SQLite single-writer** — `with_for_update()` causes SQLAlchemy to emit `BEGIN IMMEDIATE`, which acquires a write lock on the entire database file. All concurrent write transactions serialise at the DB level. This is correct and sufficient at this scale.
- **Flash sales** — many concurrent reservation requests for the same product. The risk is a read-modify-write race: two requests both read `quantity_available = 5`, both pass the check, both deduct — resulting in negative stock. `with_for_update()` eliminates this by serialising the transactions.
- **Fulfilment agent retries** — the agent retries on timeout. Without idempotency, a retry after a successful-but-slow commit double-deducts stock. The idempotency check handles this.

**If this moved to Postgres with multiple workers**: the locking strategy remains correct — Postgres supports `SELECT FOR UPDATE` with true row-level locking, which would give better concurrency than SQLite's file-level lock. Connection pool sizing and lock timeout configuration would need attention.

---

## 5. Changes Made — With Rationale

### Schema additions

**`StockLevel.quantity_reserved`** (Integer, default 0)  
Tracks stock committed to reserved orders but not yet fulfilled. Allows the warehouse UI to show available vs. committed without querying orders. Denormalised — must be maintained by every status-changing endpoint.

**`Order.reserved_warehouse_id`** (nullable FK → `warehouses`)  
Required for the idempotency check. Without it, there is no way to know which warehouse was already reserved when the agent retries.

### Full change log

| Area                         | Change                                                              |
|------------------------------|---------------------------------------------------------------------|
| Imports                      | Added `contextvars`, `event`, `Index`, `joinedload`                 |
| RLS                          | `_current_tenant` ContextVar + `_apply_tenant_filter` session event |
| `StockLevel`                 | Added `quantity_reserved` + composite indexes                       |
| `Order`                      | Added `reserved_warehouse_id` + `tenant_id` index                  |
| `Warehouse`, `Product`, `Customer` | Added `index=True` on `tenant_id`                           |
| `OrderLine`                  | Added `index=True` on `order_id`                                    |
| `current_tenant_id`          | Now sets `_current_tenant` ContextVar                               |
| `_order_query()`             | New helper — eager-loads customer + lines + products                |
| `list_orders`                | Uses `_order_query()`                                               |
| `get_order`                  | Uses `_order_query()` + added `tenant_id` filter                    |
| `adjust_stock`               | Added `.with_for_update()`                                          |
| `serialize_stock`            | Added `quantity_reserved` to output                                 |
| `ReserveIn`                  | New Pydantic model                                                  |
| `reserve_order`              | New endpoint                                                        |

---

## 6. Reserve Endpoint — Full Design

```
POST /orders/{order_id}/reserve
Header: X-Tenant-Id: <int>
Body:   {"warehouse_id": <int>}
```

### Execution flow

```
1.  Load order → 404 if not found or wrong tenant
2.  Idempotency check → 200 early return if already reserved at same warehouse
3.  Status check → 409 if not "pending"
4.  Warehouse ownership check → 404 if warehouse not found or wrong tenant
5.  Collect product_ids from order lines, sort ascending
6.  SELECT ... FOR UPDATE on StockLevel rows in sorted product_id order
7.  Validate all lines against available stock (no writes yet)
8.  If all pass: deduct quantity_available, increment quantity_reserved per line
9.  Set order.status = "reserved", order.reserved_warehouse_id = warehouse_id
10. Commit
```

### Why sorted lock order prevents deadlocks

If two concurrent transactions lock rows in arbitrary order, a cycle is possible:

```
T1: locks product_5, waits for product_7
T2: locks product_7, waits for product_5
→ deadlock
```

Sorting by `product_id` before acquiring locks ensures all transactions acquire locks in the same global order. A cycle becomes impossible.

### Why validate before writing

If we deduct line 1 and then fail on line 2, we must roll back. The rollback path is more complex and error-prone than simply checking everything first. Validate-then-write keeps both the happy path and the error path simple, and makes the all-or-nothing guarantee trivially correct.

### HTTP status codes

| Scenario                                           | Status |
|----------------------------------------------------|--------|
| Success                                            | 200    |
| Idempotent retry — same warehouse, already reserved | 200   |
| Order not found or wrong tenant                    | 404    |
| Warehouse not found or wrong tenant                | 404    |
| Order not in `pending` status                      | 409    |
| Insufficient stock for any line                    | 409    |
| No stock record for a product at the warehouse     | 422    |

---

## 7. Test Suite — Every API Call and Response

All tests run against a freshly seeded database. Stock starts at `quantity_available = 25`, `quantity_reserved = 0` for all products at all warehouses.

---

### Test 1 — Happy Path

Reserve order 1 (Northwind, 2 lines: NW-1001 × 3, NW-1002 × 2) at warehouse 1.

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/1/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 1}'
```

**Response — 200:**
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

| Product  | quantity_available | quantity_reserved |
|----------|--------------------|-------------------|
| NW-1001  | 22                 | 3                 |
| NW-1002  | 23                 | 2                 |
| All others | 25               | 0                 |

✅ Stock correctly deducted and reserved.

---

### Test 2 — Idempotency

Exact same request as Test 1 (order 1, warehouse 1, tenant 1).

**Response — 200:** Identical to Test 1.

**Stock verification:** Unchanged. `NW-1001` still 22/3, `NW-1002` still 23/2.

✅ Retry is safe. No double-deduction.

---

### Test 3 — Tenant Isolation: Cross-Tenant Order Read

Northwind (tenant 1) attempts to read Globex's order 3.

**Request:**
```bash
curl -s http://localhost:8000/orders/3 -H "X-Tenant-Id: 1"
```

**Response — 404:**
```json
{"detail": "Order not found"}
```

✅ 404, not 403. Existence is not leaked.

---

### Test 4 — Tenant Isolation: Cross-Tenant Warehouse

Northwind (tenant 1) attempts to reserve order 2 at Globex's warehouse 3.

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/2/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 3}'
```

**Response — 404:**
```json
{"detail": "Warehouse not found"}
```

✅ Warehouse 3 belongs to Globex. Northwind cannot use it. Existence not leaked.

---

### Test 5 — Insufficient Stock (All-or-Nothing)

Order 2 has one line: NW-1003 × 5. Adjust NW-1003 at warehouse 1 down to 2.

**Setup:**
```bash
# Identify stock_level_id for NW-1003 at warehouse 1 from GET /stock response
curl -s -X POST http://localhost:8000/stock/adjust \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"stock_level_id": 3, "delta": -23}'
# NW-1003 now: quantity_available = 2
```

**Reserve attempt:**
```bash
curl -s -X POST http://localhost:8000/orders/2/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 1}'
```

**Response — 409:**
```json
{
  "detail": "Insufficient stock for product NW-1003: need 5, have 2"
}
```

**Stock verification:** All products at warehouse 1 unchanged. No partial deduction occurred.

✅ All-or-nothing guarantee confirmed.

---

### Test 6 — Wrong Status: Re-reserve at Different Warehouse

Order 1 is already `reserved` at warehouse 1 (from Test 1). Attempt to reserve at warehouse 2.

**Request:**
```bash
curl -s -X POST http://localhost:8000/orders/1/reserve \
  -H "X-Tenant-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 2}'
```

**Response — 409:**
```json
{
  "detail": "Order is 'reserved', only pending orders can be reserved"
}
```

✅ Status machine enforced. Cannot re-reserve at a different warehouse.

---

### Test 7 — Confidential Order Isolation

Northwind (tenant 1) attempts to read Globex's order 4, which contains a confidential commercial note.

**Request:**
```bash
curl -s http://localhost:8000/orders/4 -H "X-Tenant-Id: 1"
```

**Response — 404:**
```json
{"detail": "Order not found"}
```

In the original code, this returned the full order including the confidential note. Fixed.

✅ Data leak closed.

---

## 8. Trade-offs Accepted

### SQLite `with_for_update()` serialises the entire database

SQLite has no row-level locking. `with_for_update()` causes SQLAlchemy to emit `BEGIN IMMEDIATE`, which locks the entire database file for the transaction duration. Under concurrent reservations, all requests queue behind each other. This is correct but limits throughput.

Accepted because: the scale model is single-process SQLite. If this moves to Postgres, `SELECT FOR UPDATE` gives true row-level locking and the throughput concern goes away.

### RLS event does not cover raw SQL

The `do_orm_execute` event intercepts ORM queries only. Raw `text()` queries or `connection.execute()` calls bypass it. The explicit `tenant_id` filters in each endpoint remain as a second layer. Some queries emit `tenant_id = 1 AND tenant_id = 1` — harmless, but worth knowing.

### `quantity_reserved` is denormalised

Storing reserved quantity on `StockLevel` is fast but requires every status-changing endpoint to maintain it. Currently only the reserve endpoint does. A future `cancel` or `fulfil` endpoint must decrement it, or the number drifts. The alternative — computing it from orders at query time — is always consistent but expensive.

### No `updated_at` on `Order`

The order model has `created_at` but no `updated_at`. After reservation, there is no timestamp for when the status changed. Noted but not fixed — it requires a schema migration and was out of scope.

### `GET /stock` returns empty list for non-existent warehouse

A caller cannot distinguish "warehouse exists but is empty" from "warehouse does not exist or belongs to another tenant". Not fixed — changing this to a 404 is a breaking change for existing clients.

---

## 9. Deliberately Left Alone

**Authentication**: `X-Tenant-Id` is caller-supplied with no verification. In production this must come from a verified JWT or session token. Left alone — the task brief explicitly states the header is a simplification for the exercise.

**`delta` bounds on `StockAdjustIn`**: No upper or lower bound beyond the `>= 0` check after application. A `Field(ge=-10_000, le=10_000)` constraint would be the right addition. Not a stated bug, not touched.

**`cancel` and `fulfil` endpoints**: These would need to decrement `quantity_reserved`. Without them, `quantity_reserved` only ever grows. Out of scope — the task asked for the reserve endpoint only.

**Seed structure**: One `Customer` row per order (high cardinality). Unusual but intentional per the comment in the code. Left as-is.

**Logging and observability**: No structured logging, no request IDs, no tracing. First thing to add in production. Out of scope for this exercise.

**Alembic / migrations**: The code uses `drop_all` + `create_all` on startup. Destructive. In production, Alembic is needed. Not introduced here — it would change the project structure significantly and was not part of the task.

---

## 10. Open for Discussion

### Architecture

**When does SQLite become the bottleneck?**  
`BEGIN IMMEDIATE` means all concurrent reservations serialise. For a flash sale with hundreds of requests per second, this becomes a queue. The fix is Postgres with row-level locking, or a queue-based reservation system where reserve requests are processed serially per product by a worker. Worth knowing the actual peak RPS before deciding.

**Should `quantity_reserved` live on `StockLevel` or be computed?**  
Denormalised is fast but requires maintenance discipline. A computed view or a separate `reservations` table (one row per reservation, summed at query time) is more correct but slower. The right answer depends on read/write ratio and whether the warehouse UI needs real-time reserved counts.

**Natural idempotency key vs. client-supplied `Idempotency-Key` header?**  
The current key is `(order_id, warehouse_id)`. A Stripe-style `Idempotency-Key` header would be more general and would handle cases where the agent retries with different parameters due to a bug. Worth discussing whether the agent is trusted to always retry with the same parameters.

**What happens to `quantity_reserved` when an order is cancelled?**  
Currently nothing — it stays incremented. A `POST /orders/{order_id}/cancel` endpoint needs to decrement it. If orders can expire (e.g. unpaid after 24 hours), a background job needs to release reserved stock. Neither exists yet.

### Concurrency

**Deadlock risk between `reserve` and `adjust_stock` under Postgres?**  
Sorted lock order prevents deadlocks between two reserve transactions. But a reserve transaction and a concurrent `adjust_stock` transaction could still deadlock if `adjust_stock` acquires its lock in a different order. Under SQLite this cannot happen (whole-file lock). Under Postgres it could. The fix is to ensure `adjust_stock` also acquires its lock in a consistent order, or to use advisory locks.

**What if the fulfilment agent retries with a different warehouse?**  
It gets a 409 because the order is already `reserved`. The agent must treat 409 as terminal, not retryable. This needs to be in the API contract — it is not currently documented anywhere.

### Data model

**Should `reserved_warehouse_id` be on `Order` or on `OrderLine`?**  
Currently the entire order is reserved at one warehouse. If the business ever needs split fulfilment (some lines from warehouse A, some from warehouse B), `reserved_warehouse_id` needs to move to `OrderLine`. Worth confirming the business requirement before this model is locked in.

**Is `status` on `Order` the right state machine?**  
Currently `pending` → `reserved` → (implied) `fulfilled` / `cancelled`. No `partially_reserved`, no `payment_pending`, no `shipped`. The field is a plain string with no enforcement of valid transitions. A proper state machine — even just an Enum + transition guard — would prevent invalid states from being written.

**Should `order_lines` be immutable after creation?**  
There is no endpoint to modify lines, but there is also no enforcement. If a line is modified after reservation, `quantity_reserved` on `StockLevel` becomes inconsistent. Any future line-editing endpoint needs to handle this.

### Operational

**Schema migrations?**  
`drop_all` + `create_all` on startup is destructive. In production, Alembic is needed. The two new columns (`quantity_reserved`, `reserved_warehouse_id`) would need `ALTER TABLE` migrations with appropriate defaults for existing rows.

**Fulfilment agent retry policy?**  
The task brief says it retries on timeout. It does not say how many times, with what backoff, or what it does on a 409. The reserve endpoint is safe to retry, but the agent's behaviour on non-retryable errors (409, 422) needs to be defined to avoid infinite retry loops.
