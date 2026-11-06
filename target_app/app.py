"""Disposable e-commerce target app for PolicyReplay (synthetic data only).

BUG_PROFILE=correct : enforces P-01 (refund > 10000 INR needs manager approval)
BUG_PROFILE=buggy   : planted bug, skips the approval check
Same code, same image; only the env flag differs.
Threshold semantics: strictly greater than 10000 needs approval; exactly 10000 does not.
P-02 planted bug (buggy only): POST /users/{id}/role lets any staff role change roles (UI shows the
form to everyone; only the server check is missing). Every attempt is recorded in role_changes
with the actor's role AT THE TIME (a self-promotion would otherwise hide the actor).
P-05 planted bug (buggy only): GET /orders/{id} skips the ownership check (direct-URL access).
Every order page view is recorded in order_views with the HTTP status actually returned.
"""
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

THRESHOLD = 10000
BASE = Path(__file__).parent

SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, role TEXT);
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, total INTEGER, status TEXT);
CREATE TABLE refunds(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, amount INTEGER,
                     issued_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, amount INTEGER,
                       requested_by INTEGER, approved_by INTEGER, status TEXT DEFAULT 'pending');
CREATE TABLE role_changes(id INTEGER PRIMARY KEY AUTOINCREMENT, target_user_id INTEGER,
                         old_role TEXT, new_role TEXT, changed_by INTEGER, actor_role TEXT,
                         applied INTEGER, changed_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE order_views(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_id INTEGER,
                         status INTEGER, viewed_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
ROLES = ["support", "manager", "admin", "customer"]
SEED_USERS = [(1, "support1", "support"), (2, "manager1", "manager"), (3, "admin1", "admin"),
              (4, "alice", "customer"), (5, "bob", "customer")]
SEED_ORDERS = [(1001, 4, 20000, "delivered"), (1002, 5, 8000, "delivered")]


def create_app(profile=None, db_path=None):
    profile = profile or os.environ.get("BUG_PROFILE", "correct")
    db_path = db_path or os.environ.get("DB_PATH", "/tmp/app.db")
    if profile not in ("correct", "buggy"):
        raise ValueError("BUG_PROFILE must be 'correct' or 'buggy'")
    Path(db_path).unlink(missing_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO users VALUES(?,?,?)", SEED_USERS)
    con.executemany("INSERT INTO orders VALUES(?,?,?,?)", SEED_ORDERS)
    con.commit()

    app = FastAPI(title="Target Shop")
    tpl = Jinja2Templates(directory=str(BASE / "templates"))

    def me(request):
        name = request.cookies.get("user")
        return con.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone() if name else None

    def order_page(request, user, order_id, flash=None, kind=None, status=200, enforce_owner=True):
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            return HTMLResponse('<p data-testid="error">Not found</p>', status_code=404)
        if enforce_owner and user["role"] == "customer" and order["customer_id"] != user["id"]:
            return HTMLResponse('<p data-testid="error">Forbidden</p>', status_code=403)
        refunds = con.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,)).fetchall()
        approvals = con.execute("SELECT * FROM approvals WHERE order_id=?", (order_id,)).fetchall()
        return tpl.TemplateResponse(request, "order.html", dict(
            user=user, order=order, refunds=refunds, approvals=approvals,
            flash=flash, kind=kind), status_code=status)

    @app.get("/health")
    def health():
        return {"ok": True, "profile": profile}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return tpl.TemplateResponse(request, "login.html", dict(user=None, error=None))

    @app.post("/login")
    def login(request: Request, username: str = Form(...)):
        u = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            return tpl.TemplateResponse(request, "login.html",
                                        dict(user=None, error="Unknown user"), status_code=401)
        r = RedirectResponse("/orders", status_code=303)
        r.set_cookie("user", username)
        return r

    @app.get("/orders", response_class=HTMLResponse)
    def orders(request: Request):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] == "customer":
            rows = con.execute("SELECT * FROM orders WHERE customer_id=?", (u["id"],)).fetchall()
        else:
            rows = con.execute("SELECT * FROM orders").fetchall()
        return tpl.TemplateResponse(request, "orders.html", dict(user=u, orders=rows))

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def order(request: Request, order_id: int):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        # P-05 planted bug: the buggy build does not enforce ownership on this route.
        resp = order_page(request, u, order_id, enforce_owner=(profile == "correct"))
        if con.execute("SELECT 1 FROM orders WHERE id=?", (order_id,)).fetchone():
            con.execute("INSERT INTO order_views(user_id, order_id, status) VALUES(?,?,?)",
                        (u["id"], order_id, resp.status_code))
            con.commit()
        return resp

    @app.post("/orders/{order_id}/refund", response_class=HTMLResponse)
    def refund(request: Request, order_id: int, amount: int = Form(...)):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] not in ("support", "manager", "admin"):
            return order_page(request, u, order_id, "Not allowed", "error", 403)
        o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or amount <= 0 or amount > o["total"]:
            return order_page(request, u, order_id, "Invalid refund amount", "error", 400)
        if profile == "correct" and amount > THRESHOLD:
            ok = con.execute("SELECT 1 FROM approvals WHERE order_id=? AND status='approved' "
                             "AND amount>=?", (order_id, amount)).fetchone()
            if not ok:
                return order_page(request, u, order_id,
                                  "Manager approval required for refunds above 10000", "error", 403)
        con.execute("INSERT INTO refunds(order_id, amount, issued_by) VALUES(?,?,?)",
                    (order_id, amount, u["id"]))
        con.commit()
        return order_page(request, u, order_id, f"Refund of {amount} issued", "success")

    @app.post("/orders/{order_id}/approval-request", response_class=HTMLResponse)
    def approval_request(request: Request, order_id: int, amount: int = Form(...)):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] != "support":
            return order_page(request, u, order_id, "Only support can request approval", "error", 403)
        con.execute("INSERT INTO approvals(order_id, amount, requested_by) VALUES(?,?,?)",
                    (order_id, amount, u["id"]))
        con.commit()
        return order_page(request, u, order_id, "Approval requested", "success")

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals(request: Request):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] != "manager":
            return HTMLResponse("Forbidden", status_code=403)
        rows = con.execute("SELECT * FROM approvals").fetchall()
        return tpl.TemplateResponse(request, "approvals.html", dict(user=u, approvals=rows))

    @app.post("/approvals/{approval_id}/approve")
    def approve(request: Request, approval_id: int):
        u = me(request)
        if not u or u["role"] != "manager":
            return HTMLResponse("Forbidden", status_code=403)
        con.execute("UPDATE approvals SET status='approved', approved_by=? WHERE id=?",
                    (u["id"], approval_id))
        con.commit()
        return RedirectResponse("/approvals", status_code=303)


    def users_page(request, user, flash=None, kind=None, status=200):
        rows = con.execute("SELECT * FROM users ORDER BY id").fetchall()
        return tpl.TemplateResponse(request, "users.html", dict(
            user=user, users=rows, roles=ROLES, flash=flash, kind=kind), status_code=status)

    @app.get("/users", response_class=HTMLResponse)
    def users(request: Request):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] == "customer":
            return HTMLResponse('<p data-testid="error">Forbidden</p>', status_code=403)
        return users_page(request, u)

    @app.post("/users/{user_id}/role", response_class=HTMLResponse)
    def change_role(request: Request, user_id: int, role: str = Form(...)):
        u = me(request)
        if not u:
            return RedirectResponse("/login", status_code=303)
        if u["role"] == "customer":
            return HTMLResponse('<p data-testid="error">Forbidden</p>', status_code=403)
        target = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            return HTMLResponse('<p data-testid="error">Not found</p>', status_code=404)
        if role not in ROLES:
            return users_page(request, u, "Unknown role", "error", 400)
        # P-02 planted bug: the buggy build lets any staff user change roles.
        allowed = (u["role"] == "admin") if profile == "correct" else True
        con.execute("INSERT INTO role_changes(target_user_id, old_role, new_role, changed_by, "
                    "actor_role, applied) VALUES(?,?,?,?,?,?)",
                    (user_id, target["role"], role, u["id"], u["role"], 1 if allowed else 0))
        if allowed:
            con.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            con.commit()
            return users_page(request, me(request), f"Role updated to {role}", "success")
        con.commit()
        return users_page(request, u, "Only administrators can change roles", "error", 403)

    return app


app = create_app() if os.environ.get("PR_AUTOSTART", "1") == "1" else None
