# Stantech - Backend Working Task

Thanks for taking the time to do this. It should take **45-60 minutes**. We are
not looking for a polished production system in an hour - we are looking at how
you think, what you notice, and what you decide to do about it.

## The service

`app.py` is a small **multi-tenant inventory and order service** (FastAPI +
SQLAlchemy + SQLite). Several customer companies ("tenants") share the same
database. Every request identifies its tenant through the `X-Tenant-Id` header.

Run it:

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:8000/docs to try the endpoints. Running `app.py`
reseeds the database with two tenants, some warehouses, products, stock levels
and around a hundred orders.

## Your task

**1. Add a reserve-stock endpoint.**

```
POST /orders/{order_id}/reserve
Body: {"warehouse_id": <int>}
```

It should reserve stock at the given warehouse for every line on the order, and
return the updated order. Decide yourself what "reserve" should do to the data
and what should happen when it cannot be satisfied - and be ready to explain why.

Two things worth knowing about how this endpoint is used:

- Our **fulfilment agent** calls it autonomously, and **retries on timeout**.
- During flash sales, many reservations for the same product arrive at once.

**2. Treat the rest of the file as production code you own.**

This service is live and we are on call for it. Review `app.py` the way you
would review a teammate's pull request before it ships, and fix what you think
genuinely needs fixing. You do not need to fix everything - use your judgement
about what matters, and tell us what you deliberately left alone.

One piece of context from support: **a few customers have reported that the
order list feels slow**, and separately, **we have twice oversold stock during
high-traffic periods** and had to cancel customer orders afterwards.

## Using AI tools

**Please use AI tools** (Claude, ChatGPT, Cursor, Copilot, whatever you normally
use). This is how we work, and we want to see how you work with them.

## What to send back

1. **Your code** - the modified `app.py` plus anything else you added.
2. **Your complete, unedited AI transcript(s).** Export the whole session, not a
   summary or a screenshot. If you used more than one tool, send all of them. If
   your tool cannot export, copy and paste the full conversation into a text
   file.
3. **A short note** (a few lines is fine) covering what you changed, what you
   found, and anything you chose not to do and why.

Please send these as a zip, or as a link to a **private** repository.

A note on the transcript: we read it as carefully as the code. We are not
checking whether you used AI - we expect you to. We are interested in how you
direct it, what you question, and what you verify.

Polish matters less than judgement.
