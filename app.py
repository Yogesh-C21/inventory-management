"""
Stantech - Backend Working Task
A small multi-tenant inventory and order service. See README.md for your task.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:8000/docs

Each request identifies its tenant via the X-Tenant-Id header.
(In production this would come from the authenticated session; a header keeps
this exercise simple.)
"""
import contextvars
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, event, ForeignKey, Index, String, Integer, DateTime, func, text
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker, joinedload
)

engine = create_engine(
    "sqlite:///./inventory.db", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)

# ---------------------------------------------------------------------------
# RLS simulation
# SQLite has no native Row-Level Security. We enforce tenant isolation at the
# SQLAlchemy session layer: every ORM SELECT against a tenant-scoped table
# automatically gets tenant_id injected, so a missing filter in application
# code cannot leak cross-tenant rows.
# ---------------------------------------------------------------------------
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


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    sku: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String)


class StockLevel(Base):
    __tablename__ = "stock_levels"
    __table_args__ = (
        # Hot path: list stock for a warehouse scoped to a tenant
        Index("ix_stock_warehouse_tenant", "warehouse_id", "tenant_id"),
        # Hot path: reserve endpoint looks up by product within warehouse+tenant
        Index("ix_stock_warehouse_tenant_product", "warehouse_id", "tenant_id", "product_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity_available: Mapped[int] = mapped_column(Integer, default=0)
    quantity_reserved: Mapped[int] = mapped_column(Integer, default=0)
    warehouse: Mapped["Warehouse"] = relationship()
    product: Mapped["Product"] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_tenant_id", "tenant_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    notes: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending")
    reserved_warehouse_id: Mapped[Optional[int]] = mapped_column(ForeignKey("warehouses.id"), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    customer: Mapped["Customer"] = relationship()
    lines: Mapped[List["OrderLine"]] = relationship()


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    product: Mapped["Product"] = relationship()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_tenant_id(x_tenant_id: int = Header(...)) -> int:
    _current_tenant.set(x_tenant_id)
    return x_tenant_id


app = FastAPI(title="Stantech Inventory")


def serialize_order(o: Order) -> dict:
    return {
        "id": o.id,
        "customer_name": o.customer.name,
        "notes": o.notes,
        "status": o.status,
        "lines": [
            {
                "product_sku": ln.product.sku,
                "product_name": ln.product.name,
                "quantity": ln.quantity,
            }
            for ln in o.lines
        ],
    }


def serialize_stock(s: StockLevel) -> dict:
    return {
        "id": s.id,
        "warehouse": s.warehouse.name,
        "product_sku": s.product.sku,
        "quantity_available": s.quantity_available,
        "quantity_reserved": s.quantity_reserved,
    }


def _order_query(db: Session):
    """Eager-loads all relationships needed by serialize_order in one query."""
    return db.query(Order).options(
        joinedload(Order.customer),
        joinedload(Order.lines).joinedload(OrderLine.product),
    )


@app.get("/orders")
def list_orders(
    db: Session = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    # Explicit filter is belt-and-suspenders on top of the RLS session event.
    orders = _order_query(db).filter(Order.tenant_id == tenant_id).all()
    return [serialize_order(o) for o in orders]


@app.get("/orders/{order_id}")
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    order = _order_query(db).filter(Order.id == order_id, Order.tenant_id == tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return serialize_order(order)


@app.get("/stock")
def list_stock(
    warehouse_id: int,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    levels = (
        db.query(StockLevel)
        .filter(
            StockLevel.warehouse_id == warehouse_id,
            StockLevel.tenant_id == tenant_id,
        )
        .all()
    )
    return [serialize_stock(s) for s in levels]


class StockAdjustIn(BaseModel):
    stock_level_id: int
    delta: int


@app.post("/stock/adjust")
def adjust_stock(
    payload: StockAdjustIn,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """Adjust the quantity available for a single stock level.

    Used by the warehouse UI and by our fulfilment agent.
    """
    stock = (
        db.query(StockLevel)
        .filter(
            StockLevel.id == payload.stock_level_id,
            StockLevel.tenant_id == tenant_id,
        )
        .with_for_update()
        .first()
    )
    if not stock:
        raise HTTPException(status_code=404, detail="Stock level not found")

    new_quantity = stock.quantity_available + payload.delta
    if new_quantity < 0:
        raise HTTPException(status_code=400, detail="Insufficient stock")
    stock.quantity_available = new_quantity
    db.commit()
    return serialize_stock(stock)


class ReserveIn(BaseModel):
    warehouse_id: int


@app.post("/orders/{order_id}/reserve")
def reserve_order(
    order_id: int,
    payload: ReserveIn,
    db: Session = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """
    Reserve stock at a warehouse for every line on the order.
    Idempotent: re-calling with the same warehouse on an already-reserved order
    returns the current state without touching stock.
    All-or-nothing: if any line cannot be satisfied the whole request is rejected
    and no stock is touched.
    """
    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.tenant_id == tenant_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Idempotency: already reserved at this warehouse — return without side effects.
    if order.status == "reserved" and order.reserved_warehouse_id == payload.warehouse_id:
        return serialize_order(order)

    if order.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Order is '{order.status}', only pending orders can be reserved",
        )

    # Verify the warehouse belongs to this tenant (prevents cross-tenant warehouse use).
    warehouse = (
        db.query(Warehouse)
        .filter(Warehouse.id == payload.warehouse_id, Warehouse.tenant_id == tenant_id)
        .first()
    )
    if not warehouse:
        raise HTTPException(status_code=404, detail="Warehouse not found")

    product_ids = sorted({ln.product_id for ln in order.lines})

    # Lock stock rows in deterministic order to prevent deadlocks under concurrency.
    # with_for_update() maps to SELECT ... FOR UPDATE; SQLite serialises at the
    # connection level so this also prevents the read-modify-write race.
    stock_map = {
        s.product_id: s
        for s in db.query(StockLevel)
        .filter(
            StockLevel.warehouse_id == payload.warehouse_id,
            StockLevel.tenant_id == tenant_id,
            StockLevel.product_id.in_(product_ids),
        )
        .order_by(StockLevel.product_id)
        .with_for_update()
        .all()
    }

    # Validate all lines before touching anything (all-or-nothing).
    for line in order.lines:
        stock = stock_map.get(line.product_id)
        if not stock:
            raise HTTPException(
                status_code=422,
                detail=f"No stock record for product {line.product_id} at warehouse {payload.warehouse_id}",
            )
        if stock.quantity_available < line.quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for product {stock.product.sku}: "
                       f"need {line.quantity}, have {stock.quantity_available}",
            )

    # All checks passed — deduct atomically.
    for line in order.lines:
        stock = stock_map[line.product_id]
        stock.quantity_available -= line.quantity
        stock.quantity_reserved += line.quantity

    order.status = "reserved"
    order.reserved_warehouse_id = payload.warehouse_id
    db.commit()
    db.refresh(order)
    return serialize_order(order)


def seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()

    northwind = Tenant(name="Northwind Traders")
    globex = Tenant(name="Globex")
    db.add_all([northwind, globex])
    db.flush()

    warehouses = {}
    for t in (northwind, globex):
        for wname in ("Central", "Overflow"):
            w = Warehouse(tenant_id=t.id, name=f"{wname} ({t.name})")
            db.add(w)
            db.flush()
            warehouses.setdefault(t.id, []).append(w)

    products = {}
    for t in (northwind, globex):
        for i in range(1, 41):
            p = Product(
                tenant_id=t.id,
                sku=f"{'NW' if t.id == northwind.id else 'GX'}-{1000 + i}",
                name=f"Component {i}",
            )
            db.add(p)
            db.flush()
            products.setdefault(t.id, []).append(p)

    for t in (northwind, globex):
        for w in warehouses[t.id]:
            for p in products[t.id]:
                db.add(
                    StockLevel(
                        tenant_id=t.id,
                        warehouse_id=w.id,
                        product_id=p.id,
                        quantity_available=25,
                    )
                )
    db.flush()

    # One distinct customer row per order (high cardinality on purpose).
    def make_order(tenant, customer, notes, items):
        c = Customer(tenant_id=tenant.id, name=customer)
        db.add(c)
        db.flush()
        o = Order(tenant_id=tenant.id, customer_id=c.id, notes=notes)
        db.add(o)
        db.flush()
        for prod, qty in items:
            db.add(OrderLine(order_id=o.id, product_id=prod.id, quantity=qty))
        return o

    # Named orders (ids 1-4). Order id 4 belongs to Globex and is sensitive.
    make_order(northwind, "Acme Retail", "Standard terms",
               [(products[northwind.id][0], 3), (products[northwind.id][1], 2)])
    make_order(northwind, "Bluebird Stores", "Ship in one consignment",
               [(products[northwind.id][2], 5)])
    make_order(globex, "Initech", "Standard terms",
               [(products[globex.id][0], 4), (products[globex.id][3], 1)])
    make_order(globex, "Umbrella Group",
               "Confidential: 40% negotiated discount, Q4 renewal - do not share externally",
               [(products[globex.id][1], 8), (products[globex.id][4], 6)])

    # Filler so the list endpoint returns many rows, each with its own lines.
    for i in range(60):
        make_order(northwind, f"Northwind customer {i + 1}", "",
                   [(products[northwind.id][i % 40], (i % 4) + 1),
                    (products[northwind.id][(i + 17) % 40], (i % 3) + 1)])
    for i in range(40):
        make_order(globex, f"Globex customer {i + 1}", "",
                   [(products[globex.id][i % 40], (i % 4) + 1),
                    (products[globex.id][(i + 13) % 40], (i % 3) + 1)])

    db.commit()
    db.close()


if __name__ == "__main__":
    seed()
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
