#!/usr/bin/env python3
"""
🎓 ربات سفارش پروژه دانشگاهی v2
پنل ادمین + پنل مشتری + باشگاه مشتریان + کیف پول + کد معرف + اعلان‌های شخصی‌سازی
"""

import os, json, hmac, hashlib, logging, threading, uuid, sys, subprocess, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import requests
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton,
                      BotCommand, BotCommandScopeDefault)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

# ─── config ─────────────────────────────────────────────
load_dotenv()
BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)
DB_FILE = BASE_DIR / "database.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
PORT = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT", "5000"))
ZP_MERCHANT = os.getenv("ZARINPAL_MERCHANT_ID", "")
ZP_SANDBOX = os.getenv("ZARINPAL_SANDBOX", "true").lower() == "true"
ZP_API = "https://sandbox.zarinpal.com/pg/v4/payment/" if ZP_SANDBOX else "https://api.zarinpal.com/pg/v4/payment/"
ZP_START = "https://sandbox.zarinpal.com/pg/StartPay/" if ZP_SANDBOX else "https://www.zarinpal.com/pg/StartPay/"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── database ───────────────────────────────────────────
def db(): 
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return _default_db()

def _default_db():
    return {
        "categories": [],
        "orders": [],
        "files": [],
        "payments": [],
        "wallets": [],
        "referrals": [],
        "settings": {
            "advance_percent": 50, "min_advance": 100000, "currency": "تومان",
            "support_phone": "", "support_telegram": "",
            "referral_discount_percent": 10, "referral_reward": 50000,
            "payment_methods": [
                {"name": "زرین‌پال", "url": "zarinpal", "active": True}
            ]
        },
        "notifications": {
            "pending": {"to_user": "✅ سفارش شما با موفقیت ثبت شد و در انتظار بررسی است.", "to_admin": "📬 سفارش جدید ثبت شد!"},
            "paid_advance": {"to_user": "💰 پیش‌پرداخت شما تأیید شد. پروژه شروع شد!", "to_admin": ""},
            "in_progress": {"to_user": "🔄 پروژه شما در حال انجام است.", "to_admin": ""},
            "completed": {"to_user": "🎉 پروژه شما تکمیل شد! لطفاً پرداخت نهایی را انجام دهید.", "to_admin": ""},
            "paid_final": {"to_user": "💵 تسویه نهایی انجام شد. از اعتماد شما متشکریم!", "to_admin": ""},
            "cancelled": {"to_user": "❌ سفارش شما لغو شد. لطفاً با پشتیبانی تماس بگیرید.", "to_admin": ""},
        }
    }

def save(d): 
    with open(DB_FILE, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=2)

def uid(): return str(uuid.uuid4())[:10]

# ─── category crud ──────────────────────────────────────
def cat_add(name, desc, price, advance=None):
    d = db(); s = d["settings"]
    c = {"id": uid(), "name": name, "description": desc, "price": int(price),
         "advance_percent": advance or s["advance_percent"], "created_at": now(), "active": True}
    d["categories"].append(c); save(d); return c

def cat_get(cid):
    for c in db()["categories"]:
        if c["id"] == cid: return c
    return None

def cat_all(active_only=True):
    cats = db()["categories"]
    return [c for c in cats if c.get("active", True)] if active_only else cats

def cat_update(cid, **kw):
    d = db()
    for c in d["categories"]:
        if c["id"] == cid: c.update(kw); save(d); return c
    return None

def cat_delete(cid): return cat_update(cid, active=False)

# ─── orders ─────────────────────────────────────────────
def order_create(uid_, uname, fname, cid, desc):
    cat = cat_get(cid)
    if not cat: return None
    adv = int(cat["price"] * cat["advance_percent"] / 100)
    d = db()
    o = {"id": uid(), "user_id": uid_, "username": uname or "", "first_name": fname or "",
         "category_id": cid, "category_name": cat["name"], "description": desc,
         "price": cat["price"], "advance_amount": adv, "final_amount": cat["price"] - adv,
         "status": "pending", "created_at": now(), "updated_at": now()}
    d["orders"].append(o); save(d); return o

def order_get(oid):
    for o in db()["orders"]:
        if o["id"] == oid: return o
    return None

def order_all(status=None, uid_=None):
    orders = db()["orders"]
    if status: orders = [o for o in orders if o["status"] == status]
    if uid_: orders = [o for o in orders if o["user_id"] == uid_]
    return sorted(orders, key=lambda x: x["created_at"], reverse=True)

def order_update(oid, **kw):
    d = db()
    for o in d["orders"]: 
        if o["id"] == oid: o.update(kw); o["updated_at"] = now(); save(d); return o
    return None

# ─── files ──────────────────────────────────────────────
def file_add(oid, fname, fpath, tgid):
    d = db()
    f = {"id": uid(), "order_id": oid, "filename": fname, "file_path": fpath, "telegram_file_id": tgid, "uploaded_at": now()}
    d["files"].append(f); save(d); return f

def file_get(oid):
    return [f for f in db()["files"] if f["order_id"] == oid]

# ─── payments ───────────────────────────────────────────
def pay_create(oid, amount, ptype):
    d = db()
    p = {"id": uid(), "order_id": oid, "amount": amount, "payment_type": ptype,
         "authority": "", "ref_id": "", "status": "pending", "created_at": now(), "admin_approved": None}
    d["payments"].append(p); save(d); return p

def pay_get(pid):
    for p in db()["payments"]:
        if p["id"] == pid: return p
    return None

def pay_update(pid, **kw):
    d = db()
    for p in d["payments"]: 
        if p["id"] == pid: p.update(kw); save(d); return p
    return None

def pay_by_order(oid):
    return [p for p in db()["payments"] if p["order_id"] == oid]

# ─── wallets ────────────────────────────────────────────
def wallet_get(uid_):
    for w in db()["wallets"]:
        if w["user_id"] == uid_: return w
    return None

def wallet_create(uid_):
    d = db()
    w = {"id": uid(), "user_id": uid_, "balance": 0, "transactions": [], "created_at": now()}
    d["wallets"].append(w); save(d); return w

def wallet_ensure(uid_):
    w = wallet_get(uid_)
    if not w: w = wallet_create(uid_)
    return w

def wallet_credit(uid_, amount, desc=""):
    d = db()
    for w in d["wallets"]:
        if w["user_id"] == uid_:
            w["balance"] += amount
            w["transactions"].append({"type": "credit", "amount": amount, "description": desc, "date": now()})
            save(d); return w
    return None

def wallet_debit(uid_, amount, desc=""):
    d = db()
    for w in d["wallets"]:
        if w["user_id"] == uid_:
            w["balance"] -= amount
            w["transactions"].append({"type": "debit", "amount": amount, "description": desc, "date": now()})
            save(d); return w
    return None

# ─── referrals ──────────────────────────────────────────
def ref_create(uid_, code=None):
    d = db()
    code = code or f"REF{uid_}{uid()[:4]}"
    r = {"id": uid(), "user_id": uid_, "code": code.upper(), "invited_count": 0, "total_earned": 0, "created_at": now()}
    d["referrals"].append(r); save(d); return r

def ref_get_by_user(uid_):
    for r in db()["referrals"]:
        if r["user_id"] == uid_: return r
    return None

def ref_get_by_code(code):
    for r in db()["referrals"]:
        if r["code"].upper() == code.upper(): return r
    return None

def ref_record_invite(ref_uid, invited_uid):
    d = db()
    for r in d["referrals"]:
        if r["user_id"] == ref_uid:
            r["invited_count"] += 1
            reward = d["settings"].get("referral_reward", 50000)
            r["total_earned"] += reward
            wallet_credit(ref_uid, reward, f"پاداش دعوت کاربر {invited_uid}")
            save(d)
            return r
    return None

# ─── settings ───────────────────────────────────────────
def settings_get(): return db()["settings"]
def settings_update(**kw):
    d = db(); d["settings"].update(kw); save(d); return d["settings"]

def notif_get(): return db()["notifications"]
def notif_update(data):
    d = db(); d["notifications"] = data; save(d); return d["notifications"]

# ─── dashboard ──────────────────────────────────────────
def dashboard():
    d = db(); orders = d["orders"]; pays = d["payments"]
    return {
        "total_orders": len(orders),
        "pending": len([o for o in orders if o["status"]=="pending"]),
        "in_progress": len([o for o in orders if o["status"] in ("paid_advance","in_progress")]),
        "completed": len([o for o in orders if o["status"]=="completed"]),
        "cancelled": len([o for o in orders if o["status"]=="cancelled"]),
        "total_earned": sum(p["amount"] for p in pays if p["status"]=="paid"),
        "pending_payments": sum(p["amount"] for p in pays if p["status"]=="pending"),
        "categories": len(d["categories"]),
        "total_users": len(set(o["user_id"] for o in orders)),
        "pending_approval_payments": len([p for p in pays if p["status"]=="pending" and p.get("admin_approved") is None]),
    }

# ─── customer dashboard ─────────────────────────────────
def customer_dashboard(uid_):
    orders = order_all(uid_=uid_)
    w = wallet_ensure(uid_)
    ref = ref_get_by_user(uid_)
    return {
        "orders_count": len(orders),
        "active_orders": len([o for o in orders if o["status"] in ("paid_advance","in_progress")]),
        "completed_orders": len([o for o in orders if o["status"] in ("completed","paid_final")]),
        "wallet_balance": w["balance"],
        "referral_code": ref["code"] if ref else None,
        "referral_count": ref["invited_count"] if ref else 0,
        "referral_earned": ref["total_earned"] if ref else 0,
    }

# ─── helpers ────────────────────────────────────────────
def now(): return datetime.now().isoformat()

def escape(s): 
    if not s: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

# ─── public url detection ──────────────────────────────
def detect_url():
    for key in ["WEBAPP_URL","RENDER_EXTERNAL_URL"]:
        v = os.getenv(key,"")
        if v: return v
    rd = os.getenv("RAILWAY_PUBLIC_DOMAIN","")
    if rd: return f"https://{rd}"
    try:
        r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        for t in r.json().get("tunnels",[]):
            if t.get("proto")=="https" and f":{PORT}" in t.get("config",{}).get("addr",""):
                return t["public_url"]
    except: pass
    return ""

# ─── payment gateway ────────────────────────────────────
def zp_request(amount, desc, callback_url):
    try:
        r = requests.post(ZP_API+"request.json", json={
            "merchant_id": ZP_MERCHANT, "amount": amount, "description": desc, "callback_url": callback_url
        }, headers={"Content-Type":"application/json","Accept":"application/json"}, timeout=15)
        data = r.json()
        if data.get("data") and data["data"].get("authority"):
            return True, data["data"]["authority"], f"{ZP_START}{data['data']['authority']}"
        return False, str(data.get("errors","خطا")), None
    except Exception as e:
        return False, str(e), None

def zp_verify(authority, amount):
    try:
        r = requests.post(ZP_API+"verify.json", json={
            "merchant_id": ZP_MERCHANT, "amount": amount, "authority": authority
        }, headers={"Content-Type":"application/json","Accept":"application/json"}, timeout=15)
        data = r.json()
        if data.get("data") and data["data"].get("ref_id"):
            return True, data["data"]["ref_id"]
        return False, str(data.get("errors",""))
    except Exception as e:
        return False, str(e)

# ─── flask app ──────────────────────────────────────────
flask_app = Flask(__name__)

def admin_check():
    init = request.headers.get("X-Telegram-Init-Data") or request.args.get("initData","")
    if init:
        try:
            parsed = parse_qs(init)
            d = {k:v[0] for k,v in parsed.items()}
            h = d.pop("hash","")
            check = "\n".join(f"{k}={v}" for k,v in sorted(d.items()))
            sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            if hmac.new(sk, check.encode(), hashlib.sha256).hexdigest() == h:
                user = json.loads(d.get("user","{}"))
                if user.get("id") in ADMIN_IDS: return True, user
        except: pass
    ak = request.args.get("admin_key","")
    if ak and ak == BOT_TOKEN[:16]: return True, None
    return False, None

def user_check():
    init = request.headers.get("X-Telegram-Init-Data") or request.args.get("initData","")
    if init:
        try:
            parsed = parse_qs(init)
            d = {k:v[0] for k,v in parsed.items()}
            h = d.pop("hash","")
            check = "\n".join(f"{k}={v}" for k,v in sorted(d.items()))
            sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            if hmac.new(sk, check.encode(), hashlib.sha256).hexdigest() == h:
                return True, json.loads(d.get("user","{}"))
        except: pass
    return False, None

# ─── panels ─────────────────────────────────────────────
@flask_app.route("/admin")
def admin_panel(): return render_template("admin.html")

@flask_app.route("/panel")
def customer_panel(): return render_template("customer.html")

# ─── auth api ───────────────────────────────────────────
@flask_app.route("/api/auth/me")
def auth_me():
    ok, u = user_check()
    if not ok: ok2, u = admin_check()
    if not ok and not ok2: return jsonify({"error":"unauthorized"}), 403
    is_admin = False
    if u and u.get("id") in ADMIN_IDS: is_admin = True
    return jsonify({"user": u, "is_admin": is_admin})

# ─── admin api ──────────────────────────────────────────
@flask_app.route("/api/admin/dashboard")
def api_dashboard():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(dashboard())

@flask_app.route("/api/admin/categories")
def api_categories():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(db()["categories"])

@flask_app.route("/api/admin/categories", methods=["POST"])
def api_cat_create():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    if not data.get("name") or not data.get("price"): return jsonify({"error":"name & price required"}), 400
    c = cat_add(data["name"].strip(), data.get("description","").strip(), int(data["price"]), data.get("advance_percent"))
    return jsonify(c)

@flask_app.route("/api/admin/categories/<cid>", methods=["PUT"])
def api_cat_update(cid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    allowed = ["name","description","price","advance_percent","active"]
    c = cat_update(cid, **{k:v for k,v in data.items() if k in allowed})
    return jsonify(c) if c else (jsonify({"error":"not found"}), 404)

@flask_app.route("/api/admin/categories/<cid>", methods=["DELETE"])
def api_cat_delete(cid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    cat_delete(cid); return jsonify({"ok":True})

@flask_app.route("/api/admin/orders")
def api_orders():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    status = request.args.get("status")
    orders = order_all(status=status)
    for o in orders: o["files"] = file_get(o["id"]); o["payments"] = pay_by_order(o["id"])
    return jsonify(orders)

@flask_app.route("/api/admin/orders/<oid>")
def api_order_detail(oid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    o = order_get(oid)
    if not o: return jsonify({"error":"not found"}), 404
    o["files"] = file_get(oid); o["payments"] = pay_by_order(oid)
    return jsonify(o)

@flask_app.route("/api/admin/orders/<oid>/status", methods=["PUT"])
def api_order_status(oid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    ns = data.get("status")
    if ns not in ["pending","paid_advance","in_progress","completed","paid_final","cancelled"]:
        return jsonify({"error":"invalid status"}), 400
    o = order_update(oid, status=ns)
    if not o: return jsonify({"error":"not found"}), 404
    # notify user
    n = notif_get().get(ns, {})
    if n.get("to_user"):
        _notify_user(o["user_id"], n["to_user"], oid, ns)
    return jsonify(o)

@flask_app.route("/api/admin/payments")
def api_payments():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(db()["payments"])

@flask_app.route("/api/admin/payments/<pid>/approve", methods=["PUT"])
def api_payment_approve(pid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    p = pay_update(pid, admin_approved=True, status="paid")
    if not p: return jsonify({"error":"not found"}), 404
    o = order_get(p["order_id"])
    if o:
        if p["payment_type"]=="advance": order_update(o["id"], status="paid_advance")
        else: order_update(o["id"], status="paid_final")
    return jsonify(p)

@flask_app.route("/api/admin/payments/<pid>/reject", methods=["PUT"])
def api_payment_reject(pid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    p = pay_update(pid, admin_approved=False, status="failed")
    return jsonify(p) if p else (jsonify({"error":"not found"}), 404)

@flask_app.route("/api/admin/settings")
def api_settings():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(settings_get())

@flask_app.route("/api/admin/settings", methods=["PUT"])
def api_settings_update():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    allowed = ["advance_percent","min_advance","currency","support_phone","support_telegram",
               "referral_discount_percent","referral_reward","payment_methods"]
    s = settings_update(**{k:v for k,v in data.items() if k in allowed})
    return jsonify(s)

@flask_app.route("/api/admin/notifications")
def api_notifications():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(notif_get())

@flask_app.route("/api/admin/notifications", methods=["PUT"])
def api_notifications_update():
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    s = notif_update(data); return jsonify(s)

@flask_app.route("/api/admin/files/<fid>")
def api_file_download(fid):
    ok, _ = admin_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    for f in db()["files"]:
        if f["id"]==fid:
            p = Path(f["file_path"])
            if p.exists(): return send_from_directory(str(p.parent), p.name, download_name=f["filename"])
    return jsonify({"error":"not found"}), 404

# ─── customer api ───────────────────────────────────────
@flask_app.route("/api/customer/dashboard")
def api_cust_dash():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(customer_dashboard(u["id"]))

@flask_app.route("/api/customer/categories")
def api_cust_categories():
    ok, _ = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    return jsonify(cat_all(True))

@flask_app.route("/api/customer/orders")
def api_cust_orders():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    orders = order_all(uid_=u["id"])
    for o in orders: o["files"] = file_get(o["id"]); o["payments"] = pay_by_order(o["id"])
    return jsonify(orders)

@flask_app.route("/api/customer/orders", methods=["POST"])
def api_cust_order_create():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    cid = data.get("category_id"); desc = data.get("description","")
    if not cid: return jsonify({"error":"category_id required"}), 400
    o = order_create(u["id"], u.get("username",""), u.get("first_name",""), cid, desc)
    if not o: return jsonify({"error":"category not found"}), 404
    # notify admin
    for aid in ADMIN_IDS:
        n = notif_get().get("pending",{})
        if n.get("to_admin"):
            _notify_user(aid, n["to_admin"].replace("{user}", u.get("first_name","کاربر")).replace("{oid}", o["id"]), o["id"], "pending")
    return jsonify(o)

@flask_app.route("/api/customer/orders/<oid>")
def api_cust_order_detail(oid):
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error":"not found"}), 404
    o["files"] = file_get(oid); o["payments"] = pay_by_order(oid)
    return jsonify(o)

@flask_app.route("/api/customer/orders/<oid>/ref_code", methods=["POST"])
def api_cust_order_apply_ref(oid):
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error":"not found"}), 404
    data = request.json or {}
    code = data.get("code","").strip().upper()
    ref = ref_get_by_code(code)
    if not ref or ref["user_id"] == u["id"]: return jsonify({"error":"کد معرف نامعتبر است"}), 400
    discount_pct = settings_get().get("referral_discount_percent", 10)
    discount = int(o["price"] * discount_pct / 100)
    o["discount"] = discount
    o["referral_code"] = code
    o["referral_discount_applied"] = True
    order_update(oid, discount=discount, referral_code=code, referral_discount_applied=True)
    return jsonify({"discount": discount, "new_advance": int((o["price"]-discount) * o.get("advance_amount",0)/o["price"] if o["price"] else 0) if False else int((o["price"] - discount) * (cat_get(o["category_id"]) or {"advance_percent":50})["advance_percent"] / 100)})

@flask_app.route("/api/customer/wallet")
def api_cust_wallet():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    w = wallet_ensure(u["id"])
    ref = ref_get_by_user(u["id"])
    return jsonify({"wallet": w, "referral": ref})

@flask_app.route("/api/customer/referral")
def api_cust_referral():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    ref = ref_get_by_user(u["id"])
    if not ref: ref = ref_create(u["id"])
    return jsonify(ref)

@flask_app.route("/api/customer/payments", methods=["POST"])
def api_cust_pay_create():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    oid = data.get("order_id"); ptype = data.get("type","advance")
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error":"not found"}), 404
    amount = o["advance_amount"] if ptype=="advance" else o["final_amount"]
    if o.get("discount"): amount = max(0, amount - o["discount"])
    p = pay_create(oid, amount, ptype)
    cb = f"{PUBLIC_URL}/payment/callback/{p['id']}"
    ok2, auth, purl = zp_request(amount, f"{'پیش‌پرداخت' if ptype=='advance' else 'پرداخت نهایی'} #{oid}", cb)
    if ok2 and purl:
        pay_update(p["id"], authority=auth)
        return jsonify({"payment_url": purl, "payment_id": p["id"], "amount": amount})
    return jsonify({"error": auth}), 500

@flask_app.route("/api/customer/referral/apply", methods=["POST"])
def api_cust_referral_apply():
    ok, u = user_check()
    if not ok: return jsonify({"error":"unauthorized"}), 403
    data = request.json or {}
    code = data.get("code","").strip().upper()
    ref = ref_get_by_code(code)
    if not ref: return jsonify({"error":"کد معرف نامعتبر است"}), 400
    if ref["user_id"] == u["id"]: return jsonify({"error":"نمی‌توانید از کد خودتان استفاده کنید"}), 400
    # Check if already used a referral
    for o in order_all(uid_=u["id"]):
        if o.get("referral_code"): return jsonify({"error":"شما قبلاً از کد معرف استفاده کرده‌اید"}), 400
    ref_record_invite(ref["user_id"], u["id"])
    pct = settings_get().get("referral_discount_percent", 10)
    return jsonify({"discount_percent": pct, "message": f"کد معرف تأیید شد! {pct}% تخفیف در سفارش بعدی"})

# ─── payment callback ───────────────────────────────────
@flask_app.route("/payment/callback/<pid>")
def pay_callback(pid):
    authority = request.args.get("Authority","")
    status = request.args.get("Status","")
    p = pay_get(pid)
    if not p: return _pay_html(False, None, None, error="تراکنش یافت نشد")
    o = order_get(p["order_id"])
    if status != "OK":
        pay_update(pid, status="failed")
        return _pay_html(False, p, o, error="پرداخت لغو شد")
    ok, ref = zp_verify(authority, p["amount"])
    if ok:
        pay_update(pid, ref_id=str(ref), status="paid")
        # notify admin
        for aid in ADMIN_IDS:
            _notify_user(aid, f"💳 پرداخت جدید!\nسفارش #{o['id']}\nمبلغ: {p['amount']:,} تومان\nنوع: {p['payment_type']}", o["id"], "pending")
        return _pay_html(True, p, o, ref_id=ref)
    else:
        pay_update(pid, status="failed")
        return _pay_html(False, p, o, error=str(ref))

def _pay_html(success, p, o, ref_id=None, error=None):
    c, icon, title = ("#22c55e","✓","پرداخت موفق") if success else ("#ef4444","✕","پرداخت ناموفق")
    detail = f"<p>کد پیگیری: {ref_id}</p>" if success else f"<p>{error}</p>"
    bot = os.getenv("BOT_USERNAME","")
    amount = f"{p['amount']:,}" if p else "-"
    oid = o["id"] if o else "-"
    oname = o["category_name"] if o else "-"
    return f"""<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{{margin:0;padding:0;box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,Tahoma,sans-serif;background:#0a0a0a;color:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.card{{background:#141414;border-radius:20px;padding:32px 24px;max-width:360px;width:100%;text-align:center;border:1px solid #222}}
.icon{{width:64px;height:64px;border-radius:50%;background:{c}20;display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:28px;color:{c}}}
h2{{font-size:18px;font-weight:600;margin-bottom:16px;color:{c}}}
p{{font-size:13px;color:#888;margin:6px 0;line-height:1.6}}
.row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:13px}}
.row span:first-child{{color:#666}}.row span:last-child{{color:#ddd;font-weight:500}}
.btn{{display:inline-block;padding:12px 32px;background:#fff;color:#000;text-decoration:none;border-radius:12px;margin-top:20px;font-weight:600;font-size:14px}}
.btn:active{{opacity:.8}}</style></head><body>
<div class="card"><div class="icon">{icon}</div><h2>{title}</h2>{detail}
<div class="row"><span>مبلغ</span><span>{amount} تومان</span></div>
<div class="row"><span>سفارش</span><span>#{oid} - {oname}</span></div>
<a class="btn" href="https://t.me/{bot}">بازگشت به ربات</a></div></body></html>"""

# ─── notification helper ────────────────────────────────
def _notify_user(uid_, msg, oid, status):
    try:
        import asyncio
        async def _send():
            app_bot = Application.builder().token(BOT_TOKEN).build()
            kb = None
            if status == "completed":
                o = order_get(oid)
                if o:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"💳 پرداخت نهایی ({o['final_amount']:,} تومان)", web_app=WebAppInfo(url=f"{PUBLIC_URL}/panel?order={oid}"))
                    ]])
            try:
                await app_bot.bot.send_message(chat_id=uid_, text=msg, reply_markup=kb)
            except: pass
            await app_bot.shutdown()
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        loop.run_until_complete(_send()); loop.close()
    except Exception as e:
        log.error(f"notify error: {e}")

# ─── telegram bot ───────────────────────────────────────
user_sessions = {}
MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 ثبت سفارش جدید")],
    [KeyboardButton("📂 سفارش‌های من"), KeyboardButton("👥 باشگاه مشتریان")],
    [KeyboardButton("💰 کیف پول"), KeyboardButton("📞 پشتیبانی")],
], resize_keyboard=True)
PUBLIC_URL = ""

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_sessions.pop(u.id, None)
    # ensure wallet & referral
    wallet_ensure(u.id)
    if not ref_get_by_user(u.id): ref_create(u.id)

    if u.id in ADMIN_IDS and PUBLIC_URL:
        try:
            await ctx.bot.set_chat_menu_button(chat_id=u.id,
                menu_button=MenuButtonWebApp(text="⚙️ پنل مدیریت", web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin")))
        except: pass

    await update.message.reply_text(
        f"👋 سلام {escape(u.first_name)}!\n\nبه ربات سفارش پروژه دانشگاهی خوش آمدی.\nبرای شروع روی «ثبت سفارش جدید» کلیک کن.",
        reply_markup=MAIN_KB)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text; u = update.effective_user
    if t == "📋 ثبت سفارش جدید": return await order_start(update, ctx)
    if t == "📂 سفارش‌های من": return await my_orders(update, ctx)
    if t == "👥 باشگاه مشتریان": return await club(update, ctx)
    if t == "💰 کیف پول": return await wallet_cmd(update, ctx)
    if t == "📞 پشتیبانی": return await support_cmd(update, ctx)
    s = user_sessions.get(u.id, {})
    if s.get("state") == "entering_desc": return await order_desc(update, ctx)
    await update.message.reply_text("از دکمه‌های زیر استفاده کن 👇", reply_markup=MAIN_KB)

async def order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cats = cat_all(True)
    if not cats: 
        await update.message.reply_text("⚠️ دسته‌بندی فعالی وجود ندارد.", reply_markup=MAIN_KB); return
    user_sessions[update.effective_user.id] = {"state": "selecting_category"}
    kb = [[InlineKeyboardButton(f"{c['name']} — {c['price']:,} تومان", callback_data=f"cat_{c['id']}")] for c in cats]
    kb.append([InlineKeyboardButton("انصراف", callback_data="cancel")])
    await update.message.reply_text("🎯 نوع پروژه را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

async def cat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = q.from_user
    if q.data == "cancel": user_sessions.pop(u.id,None); await q.edit_message_text("لغو شد."); return
    cid = q.data.replace("cat_",""); cat = cat_get(cid)
    if not cat: await q.edit_message_text("⚠️ دسته‌بندی نامعتبر."); return
    user_sessions[u.id] = {"state":"entering_desc","category_id":cid}
    adv = int(cat["price"]*cat["advance_percent"]/100)
    await q.edit_message_text(f"📌 {cat['name']}\n💰 قیمت: {cat['price']:,} تومان\n💳 پیش‌پرداخت: {adv:,} تومان\n\n📝 توضیحات پروژه را بنویسید:")

async def order_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; s = user_sessions.pop(u.id, {})
    if s.get("state")!="entering_desc": return
    desc = update.message.text; o = order_create(u.id, u.username, u.first_name, s["category_id"], desc)
    if not o: await update.message.reply_text("⚠️ خطا."); return
    await update.message.reply_text(f"✅ سفارش #{o['id']} ثبت شد!\n\nحالا فایل‌های مدارک را بفرست (عکس، PDF، Word و...)\nبعدش روی «اتمام» کلیک کن.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ اتمام آپلود", callback_data=f"finish_{o['id']}")],
            [InlineKeyboardButton("⏭ رد کردن", callback_data=f"skip_{o['id']}")]]))
    # notify admin
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid, f"📬 سفارش جدید #{o['id']}\nاز: {escape(u.first_name)} (@{u.username or '---'})\nدسته: {o['category_name']}\nمبلغ: {o['price']:,} تومان")
        except: pass

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; msg = update.message
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video or msg.voice
    if not file: return
    fname = getattr(file, 'file_name', None) or f"file_{file.file_id[:8]}"
    try:
        tf = await ctx.bot.get_file(file.file_id)
        sp = UPLOAD_FOLDER / f"{uid()}_{fname}"
        await tf.download_to_drive(sp)
        orders = order_all(uid_=u.id)
        if not orders: await msg.reply_text("⚠️ اول سفارش ثبت کن."); return
        oid = orders[0]["id"]
        file_add(oid, fname, str(sp), file.file_id)
        await msg.reply_text(f"✅ «{escape(fname)}» آپلود شد.\nفایل بعدی یا «اتمام».")
    except Exception as e: 
        await msg.reply_text("⚠️ خطا در آپلود.")

async def finish_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = q.from_user
    data = q.data; oid = data.replace("finish_","").replace("skip_","")
    o = order_get(oid)
    if not o or o["user_id"] != u.id: await q.edit_message_text("⚠️ سفارش یافت نشد."); return
    files = file_get(oid)
    if data.startswith("finish_") and files:
        await q.edit_message_text("✅ فایل‌ها دریافت شد!")
    else:
        await q.edit_message_text("✅ اطلاعات ثبت شد.")
    kb = [[InlineKeyboardButton(f"💳 پرداخت پیش‌پرداخت ({o['advance_amount']:,} تومان)", web_app=WebAppInfo(url=f"{PUBLIC_URL}/panel?order={oid}"))]]
    await ctx.bot.send_message(u.id, f"📋 سفارش #{oid}\n💰 مبلغ: {o['price']:,} تومان\n💳 پیش‌پرداخت: {o['advance_amount']:,} تومان\n\nبرای پرداخت روی دکمه زیر کلیک کن:", reply_markup=InlineKeyboardMarkup(kb))

async def my_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; orders = order_all(uid_=u.id)
    if not orders: await update.message.reply_text("📂 سفارشی نداری.", reply_markup=MAIN_KB); return
    smap = {"pending":"⏳ در انتظار","paid_advance":"💰 در حال انجام","in_progress":"🔄 در حال انجام","completed":"✅ تکمیل","paid_final":"💵 تسویه","cancelled":"❌ لغو"}
    txt = "📂 سفارش‌های شما:\n\n"
    for o in orders[:8]:
        txt += f"▸ #{o['id']} — {o['category_name']}\n   {smap.get(o['status'],o['status'])} | {o['price']:,} تومان\n\n"
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

async def club(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; ref = ref_get_by_user(u.id)
    if not ref: ref = ref_create(u.id)
    s = settings_get()
    txt = (f"👥 باشگاه مشتریان\n\n"
           f"🔗 کد معرف شما: <code>{ref['code']}</code>\n"
           f"👤 تعداد دعوت: {ref['invited_count']} نفر\n"
           f"💰 پاداش دریافتی: {ref['total_earned']:,} تومان\n\n"
           f"🎁 دعوت هر نفر: {s.get('referral_reward',50000):,} تومان\n"
           f"🎫 تخفیف برای دعوت‌شونده: {s.get('referral_discount_percent',10)}%\n\n"
           f"📋 برای دریافت تخفیف، کد معرف را هنگام ثبت سفارش وارد کنید.")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=MAIN_KB)

async def wallet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; w = wallet_ensure(u.id)
    txt = f"💰 کیف پول\n\nموجودی: {w['balance']:,} تومان\n\n📋 تراکنش‌های اخیر:\n"
    for t in w.get("transactions",[])[-5:][::-1]:
        sign = "+" if t["type"]=="credit" else "-"; color = "🟢" if t["type"]=="credit" else "🔴"
        txt += f"{color} {sign}{t['amount']:,} — {t.get('description','')}\n"
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

async def support_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = settings_get(); txt = "📞 پشتیبانی:\n"
    if s.get("support_phone"): txt += f"📱 {s['support_phone']}\n"
    if s.get("support_telegram"): txt += f"💬 @{s['support_telegram']}\n"
    if not s.get("support_phone") and not s.get("support_telegram"): txt += "از همین ربات پیام دهید."
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

# ─── seed ───────────────────────────────────────────────
def seed():
    d = db()
    if not d["categories"]:
        for name, desc, price in [
            ("مقاله و تحقیق","نگارش مقاله، تحقیق کلاسی، پروپوزال",500000),
            ("برنامه‌نویسی","پایتون، جاوا، C++، وب، اندروید",1500000),
            ("پاورپوینت","اسلایدهای حرفه‌ای و قالب اختصاصی",300000),
            ("حل تمرین","حل تمرین‌های درسی و مسائل",200000),
            ("طراحی گرافیک","پوستر، لوگو، اینفوگرافیک",400000),
        ]: cat_add(name, desc, price)
        log.info("demo categories seeded.")

# ─── main ───────────────────────────────────────────────
def run_flask(): flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    global PUBLIC_URL
    if not BOT_TOKEN or len(BOT_TOKEN) < 20: 
        print("❌ BOT_TOKEN را در .env تنظیم کن!"); sys.exit(1)
    
    seed()
    PUBLIC_URL = detect_url()
    if not PUBLIC_URL:
        print("⚠️ URL عمومی پیدا نشد. پنل‌ها در دسترس نیستند.")
        print("   روی Render.com دپلوی کن یا ngrok نصب کن.")
        PUBLIC_URL = f"http://localhost:{PORT}"

    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)
    
    # try get bot username
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
        if r.json().get("ok"): os.environ["BOT_USERNAME"] = r.json()["result"]["username"]
    except: pass

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cancel$"))
    app.add_handler(CallbackQueryHandler(finish_callback, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(finish_callback, pattern="^skip_"))
    app.add_handler(MessageHandler(filters.Document.ALL|filters.PHOTO|filters.VIDEO|filters.VOICE, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Use webhook on cloud platforms
    if PUBLIC_URL.startswith("https://") and (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")):
        wh = f"{PUBLIC_URL}/webhook"
        log.info(f"webhook → {wh}")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path="webhook", webhook_url=wh)
    else:
        log.info(f"polling... admin: {PUBLIC_URL}/admin | panel: {PUBLIC_URL}/panel")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
