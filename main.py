#!/usr/bin/env python3
"""🎓 ربات سفارش پروژه دانشگاهی v3 — PTB 21.3 + Flask"""

import os, json, hmac, hashlib, logging, threading, uuid, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import requests
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      MenuButtonWebApp, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

load_dotenv()
BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / "uploads"; UPLOAD_FOLDER.mkdir(exist_ok=True)
DB_FILE = BASE_DIR / "database.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
PORT = int(os.getenv("PORT", "5000"))
ZP_MERCHANT = os.getenv("ZARINPAL_MERCHANT_ID", "")
ZP_SANDBOX = os.getenv("ZARINPAL_SANDBOX", "true").lower() == "true"
ZP_API = "https://sandbox.zarinpal.com/pg/v4/payment/" if ZP_SANDBOX else "https://api.zarinpal.com/pg/v4/payment/"
ZP_START = "https://sandbox.zarinpal.com/pg/StartPay/" if ZP_SANDBOX else "https://www.zarinpal.com/pg/StartPay/"

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ═══════════════ DATABASE ═══════════════
def _db():
    if DB_FILE.exists():
        with open(DB_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return {
        "categories": [], "orders": [], "files": [], "payments": [], "wallets": [], "referrals": [],
        "settings": {
            "advance_percent": 50, "min_advance": 100000, "currency": "تومان",
            "support_phone": "", "support_telegram": "",
            "referral_discount_percent": 10, "referral_reward": 50000,
            "payment_methods": [{"name": "زرین‌پال", "url": "zarinpal", "active": True}]
        },
        "notifications": {
            "pending": {"to_user": "✅ سفارش شما با موفقیت ثبت شد.", "to_admin": "📬 سفارش جدید ثبت شد!"},
            "paid_advance": {"to_user": "💰 پیش‌پرداخت تأیید شد. پروژه شروع شد!", "to_admin": ""},
            "in_progress": {"to_user": "🔄 پروژه در حال انجام است.", "to_admin": ""},
            "completed": {"to_user": "🎉 پروژه تکمیل شد! لطفاً پرداخت نهایی را انجام دهید.", "to_admin": ""},
            "paid_final": {"to_user": "💵 تسویه انجام شد. متشکریم!", "to_admin": ""},
            "cancelled": {"to_user": "❌ سفارش لغو شد.", "to_admin": ""},
        }
    }

def _save(d): 
    with open(DB_FILE, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=2)

_uid = lambda: str(uuid.uuid4())[:10]
_now = lambda: datetime.now().isoformat()

# ═══════════════ CRUD ═══════════════
def cat_add(n, d, p, a=None):
    db = _db(); s = db["settings"]
    c = {"id": _uid(), "name": n, "description": d, "price": int(p),
         "advance_percent": a or s["advance_percent"], "created_at": _now(), "active": True}
    db["categories"].append(c); _save(db); return c

def cat_get(cid):
    for c in _db()["categories"]:
        if c["id"] == cid: return c
    return None

def cat_all(active_only=True):
    cats = _db()["categories"]
    return [c for c in cats if c.get("active", True)] if active_only else cats

def cat_update(cid, **kw):
    db = _db()
    for c in db["categories"]:
        if c["id"] == cid: c.update(kw); _save(db); return c
    return None

cat_delete = lambda cid: cat_update(cid, active=False)

def order_create(uid_, uname, fname, cid, desc):
    cat = cat_get(cid)
    if not cat: return None
    adv = int(cat["price"] * cat["advance_percent"] / 100)
    db = _db()
    o = {"id": _uid(), "user_id": uid_, "username": uname or "", "first_name": fname or "",
         "category_id": cid, "category_name": cat["name"], "description": desc,
         "price": cat["price"], "advance_amount": adv, "final_amount": cat["price"] - adv,
         "status": "pending", "created_at": _now(), "updated_at": _now()}
    db["orders"].append(o); _save(db); return o

def order_get(oid):
    for o in _db()["orders"]:
        if o["id"] == oid: return o
    return None

def order_all(status=None, uid_=None):
    orders = _db()["orders"]
    if status: orders = [o for o in orders if o["status"] == status]
    if uid_: orders = [o for o in orders if o["user_id"] == uid_]
    return sorted(orders, key=lambda x: x["created_at"], reverse=True)

def order_update(oid, **kw):
    db = _db()
    for o in db["orders"]:
        if o["id"] == oid: o.update(kw); o["updated_at"] = _now(); _save(db); return o
    return None

def file_add(oid, fn, fp, tgid):
    db = _db()
    f = {"id": _uid(), "order_id": oid, "filename": fn, "file_path": fp, "telegram_file_id": tgid, "uploaded_at": _now()}
    db["files"].append(f); _save(db); return f

file_get = lambda oid: [f for f in _db()["files"] if f["order_id"] == oid]

def pay_create(oid, amt, pt):
    db = _db()
    p = {"id": _uid(), "order_id": oid, "amount": amt, "payment_type": pt,
         "authority": "", "ref_id": "", "status": "pending", "created_at": _now(), "admin_approved": None}
    db["payments"].append(p); _save(db); return p

def pay_get(pid):
    for p in _db()["payments"]:
        if p["id"] == pid: return p
    return None

def pay_update(pid, **kw):
    db = _db()
    for p in db["payments"]:
        if p["id"] == pid: p.update(kw); _save(db); return p
    return None

pay_by_order = lambda oid: [p for p in _db()["payments"] if p["order_id"] == oid]

def wallet_get(uid_):
    for w in _db()["wallets"]:
        if w["user_id"] == uid_: return w
    return None

def wallet_create(uid_):
    db = _db()
    w = {"id": _uid(), "user_id": uid_, "balance": 0, "transactions": [], "created_at": _now()}
    db["wallets"].append(w); _save(db); return w

def wallet_ensure(uid_):
    w = wallet_get(uid_)
    return w if w else wallet_create(uid_)

def wallet_credit(uid_, amt, desc=""):
    db = _db()
    for w in db["wallets"]:
        if w["user_id"] == uid_:
            w["balance"] += amt
            w["transactions"].append({"type": "credit", "amount": amt, "description": desc, "date": _now()})
            _save(db); return w
    return None

def ref_create(uid_, code=None):
    db = _db()
    code = code or f"REF{uid_}{_uid()[:4]}"
    r = {"id": _uid(), "user_id": uid_, "code": code.upper(), "invited_count": 0, "total_earned": 0, "created_at": _now()}
    db["referrals"].append(r); _save(db); return r

def ref_get_by_user(uid_):
    for r in _db()["referrals"]:
        if r["user_id"] == uid_: return r
    return None

def ref_get_by_code(code):
    for r in _db()["referrals"]:
        if r["code"].upper() == code.upper(): return r
    return None

def ref_record_invite(ref_uid, inv_uid):
    db = _db()
    for r in db["referrals"]:
        if r["user_id"] == ref_uid:
            r["invited_count"] += 1
            reward = db["settings"].get("referral_reward", 50000)
            r["total_earned"] += reward
            wallet_credit(ref_uid, reward, f"پاداش دعوت کاربر {inv_uid}")
            _save(db); return r
    return None

settings_get = lambda: _db()["settings"]
def settings_update(**kw):
    db = _db(); db["settings"].update(kw); _save(db); return db["settings"]

notif_get = lambda: _db()["notifications"]
def notif_update(data):
    db = _db(); db["notifications"] = data; _save(db); return db["notifications"]

def dashboard():
    db = _db(); oo = db["orders"]; pp = db["payments"]
    return {
        "total_orders": len(oo),
        "pending": len([o for o in oo if o["status"] == "pending"]),
        "in_progress": len([o for o in oo if o["status"] in ("paid_advance", "in_progress")]),
        "completed": len([o for o in oo if o["status"] == "completed"]),
        "cancelled": len([o for o in oo if o["status"] == "cancelled"]),
        "total_earned": sum(p["amount"] for p in pp if p["status"] == "paid"),
        "pending_payments": sum(p["amount"] for p in pp if p["status"] == "pending"),
        "categories": len(db["categories"]),
        "total_users": len(set(o["user_id"] for o in oo)),
        "pending_approval_payments": len([p for p in pp if p["status"] == "pending" and p.get("admin_approved") is None]),
    }

def customer_dashboard(uid_):
    oo = order_all(uid_=uid_)
    w = wallet_ensure(uid_); ref = ref_get_by_user(uid_)
    return {
        "orders_count": len(oo),
        "active_orders": len([o for o in oo if o["status"] in ("paid_advance", "in_progress")]),
        "completed_orders": len([o for o in oo if o["status"] in ("completed", "paid_final")]),
        "wallet_balance": w["balance"],
        "referral_code": ref["code"] if ref else None,
        "referral_count": ref["invited_count"] if ref else 0,
        "referral_earned": ref["total_earned"] if ref else 0,
    }

def esc(s):
    if not s: return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ═══════════════ PUBLIC URL ═══════════════
def detect_url():
    for k in ["WEBAPP_URL", "RENDER_EXTERNAL_URL"]:
        v = os.getenv(k, "")
        if v: return v
    v = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
    return f"https://{v}" if v else ""

# ═══════════════ PAYMENT ═══════════════
def zp_request(amount, desc, cb_url):
    try:
        r = requests.post(ZP_API + "request.json", json={
            "merchant_id": ZP_MERCHANT, "amount": amount, "description": desc, "callback_url": cb_url
        }, headers={"Content-Type": "application/json"}, timeout=15)
        d = r.json()
        if d.get("data") and d["data"].get("authority"):
            return True, d["data"]["authority"], f"{ZP_START}{d['data']['authority']}"
        return False, str(d.get("errors", "خطا")), None
    except Exception as e:
        return False, str(e), None

def zp_verify(auth, amount):
    try:
        r = requests.post(ZP_API + "verify.json", json={
            "merchant_id": ZP_MERCHANT, "amount": amount, "authority": auth
        }, headers={"Content-Type": "application/json"}, timeout=15)
        d = r.json()
        if d.get("data") and d["data"].get("ref_id"):
            return True, d["data"]["ref_id"]
        return False, str(d.get("errors", ""))
    except Exception as e:
        return False, str(e)

# ═══════════════ FLASK ═══════════════
flask_app = Flask(__name__)
PUBLIC_URL = ""

def _auth(admin_only=False):
    init = request.headers.get("X-Telegram-Init-Data") or request.args.get("initData", "")
    if init:
        try:
            p = parse_qs(init); d = {k: v[0] for k, v in p.items()}; h = d.pop("hash", "")
            check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
            sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            if hmac.new(sk, check.encode(), hashlib.sha256).hexdigest() == h:
                u = json.loads(d.get("user", "{}"))
                if admin_only and u.get("id") not in ADMIN_IDS: return None
                return u
        except: pass
    ak = request.args.get("admin_key", "")
    if ak and ak == BOT_TOKEN[:16]:
        return {"id": ADMIN_IDS[0] if ADMIN_IDS else 0, "is_admin": True}
    return None

# ── pages ──
@flask_app.route("/")
def health(): return "OK"

@flask_app.route("/admin")
def admin_page(): return render_template("admin.html")

@flask_app.route("/panel")
def cust_page(): return render_template("customer.html")

# ── admin API ──
def _admin_only(f):
    def w(*a, **kw):
        if not _auth(admin_only=True): return jsonify({"error": "unauthorized"}), 403
        return f(*a, **kw)
    w.__name__ = f.__name__; return w

@flask_app.route("/api/admin/dashboard")
@_admin_only
def api_dashboard(): return jsonify(dashboard())

@flask_app.route("/api/admin/categories")
@_admin_only
def api_cats(): return jsonify(_db()["categories"])

@flask_app.route("/api/admin/categories", methods=["POST"])
@_admin_only
def api_cat_create():
    d = request.json or {}
    if not d.get("name") or not d.get("price"): return jsonify({"error": "name & price required"}), 400
    return jsonify(cat_add(d["name"].strip(), d.get("description", "").strip(), int(d["price"]), d.get("advance_percent")))

@flask_app.route("/api/admin/categories/<cid>", methods=["PUT"])
@_admin_only
def api_cat_update(cid):
    d = request.json or {}
    c = cat_update(cid, **{k: v for k, v in d.items() if k in ["name", "description", "price", "advance_percent", "active"]})
    return jsonify(c) if c else (jsonify({"error": "not found"}), 404)

@flask_app.route("/api/admin/categories/<cid>", methods=["DELETE"])
@_admin_only
def api_cat_delete(cid): cat_delete(cid); return jsonify({"ok": True})

@flask_app.route("/api/admin/orders")
@_admin_only
def api_orders():
    oo = order_all(status=request.args.get("status"))
    for o in oo: o["files"] = file_get(o["id"]); o["payments"] = pay_by_order(o["id"])
    return jsonify(oo)

@flask_app.route("/api/admin/orders/<oid>")
@_admin_only
def api_order_detail(oid):
    o = order_get(oid)
    if not o: return jsonify({"error": "not found"}), 404
    o["files"] = file_get(oid); o["payments"] = pay_by_order(oid)
    return jsonify(o)

@flask_app.route("/api/admin/orders/<oid>/status", methods=["PUT"])
@_admin_only
def api_order_status(oid):
    ns = (request.json or {}).get("status")
    if ns not in ["pending", "paid_advance", "in_progress", "completed", "paid_final", "cancelled"]:
        return jsonify({"error": "invalid status"}), 400
    o = order_update(oid, status=ns)
    if not o: return jsonify({"error": "not found"}), 404
    _notify_user(o["user_id"], notif_get().get(ns, {}).get("to_user", ""))
    return jsonify(o)

@flask_app.route("/api/admin/payments")
@_admin_only
def api_payments(): return jsonify(_db()["payments"])

@flask_app.route("/api/admin/payments/<pid>/approve", methods=["PUT"])
@_admin_only
def api_pay_approve(pid):
    p = pay_update(pid, admin_approved=True, status="paid")
    if not p: return jsonify({"error": "not found"}), 404
    o = order_get(p["order_id"])
    if o: order_update(o["id"], status="paid_advance" if p["payment_type"] == "advance" else "paid_final")
    return jsonify(p)

@flask_app.route("/api/admin/payments/<pid>/reject", methods=["PUT"])
@_admin_only
def api_pay_reject(pid):
    p = pay_update(pid, admin_approved=False, status="failed")
    return jsonify(p) if p else (jsonify({"error": "not found"}), 404)

@flask_app.route("/api/admin/settings")
@_admin_only
def api_settings(): return jsonify(settings_get())

@flask_app.route("/api/admin/settings", methods=["PUT"])
@_admin_only
def api_settings_upd():
    d = request.json or {}
    ok = ["advance_percent", "min_advance", "currency", "support_phone", "support_telegram", "referral_discount_percent", "referral_reward", "payment_methods"]
    return jsonify(settings_update(**{k: v for k, v in d.items() if k in ok}))

@flask_app.route("/api/admin/notifications")
@_admin_only
def api_notifs(): return jsonify(notif_get())

@flask_app.route("/api/admin/notifications", methods=["PUT"])
@_admin_only
def api_notifs_upd(): return jsonify(notif_update(request.json or {}))

@flask_app.route("/api/admin/files/<fid>")
@_admin_only
def api_file_dl(fid):
    for f in _db()["files"]:
        if f["id"] == fid:
            p = Path(f["file_path"])
            if p.exists(): return send_from_directory(str(p.parent), p.name, download_name=f["filename"])
    return jsonify({"error": "not found"}), 404

# ── customer API ──
def _user_only(f):
    def w(*a, **kw):
        u = _auth(admin_only=False)
        if not u: return jsonify({"error": "unauthorized"}), 403
        return f(u, *a, **kw)
    w.__name__ = f.__name__; return w

@flask_app.route("/api/customer/dashboard")
@_user_only
def api_cust_dash(u): return jsonify(customer_dashboard(u["id"]))

@flask_app.route("/api/customer/categories")
@_user_only
def api_cust_cats(u): return jsonify(cat_all(True))

@flask_app.route("/api/customer/orders")
@_user_only
def api_cust_orders(u):
    oo = order_all(uid_=u["id"])
    for o in oo: o["files"] = file_get(o["id"]); o["payments"] = pay_by_order(o["id"])
    return jsonify(oo)

@flask_app.route("/api/customer/orders", methods=["POST"])
@_user_only
def api_cust_order_create(u):
    d = request.json or {}
    cid, desc = d.get("category_id"), d.get("description", "")
    if not cid: return jsonify({"error": "category_id required"}), 400
    o = order_create(u["id"], u.get("username", ""), u.get("first_name", ""), cid, desc)
    if not o: return jsonify({"error": "category not found"}), 404
    for aid in ADMIN_IDS:
        _notify_user(aid, f"📬 سفارش جدید #{o['id']}\nاز: {esc(u.get('first_name','کاربر'))}\nمبلغ: {o['price']:,} تومان")
    return jsonify(o)

@flask_app.route("/api/customer/orders/<oid>")
@_user_only
def api_cust_order_detail(u, oid):
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error": "not found"}), 404
    o["files"] = file_get(oid); o["payments"] = pay_by_order(oid)
    return jsonify(o)

@flask_app.route("/api/customer/orders/<oid>/ref_code", methods=["POST"])
@_user_only
def api_cust_order_ref(u, oid):
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error": "not found"}), 404
    code = (request.json or {}).get("code", "").strip().upper()
    ref = ref_get_by_code(code)
    if not ref or ref["user_id"] == u["id"]: return jsonify({"error": "کد معرف نامعتبر"}), 400
    pct = settings_get().get("referral_discount_percent", 10)
    discount = int(o["price"] * pct / 100)
    order_update(oid, discount=discount, referral_code=code, referral_discount_applied=True)
    return jsonify({"discount": discount})

@flask_app.route("/api/customer/wallet")
@_user_only
def api_cust_wallet(u):
    w = wallet_ensure(u["id"]); ref = ref_get_by_user(u["id"])
    return jsonify({"wallet": w, "referral": ref})

@flask_app.route("/api/customer/payments", methods=["POST"])
@_user_only
def api_cust_pay_create(u):
    d = request.json or {}
    oid, pt = d.get("order_id"), d.get("type", "advance")
    o = order_get(oid)
    if not o or o["user_id"] != u["id"]: return jsonify({"error": "not found"}), 404
    amt = o["advance_amount"] if pt == "advance" else o["final_amount"]
    if o.get("discount"): amt = max(0, amt - o["discount"])
    p = pay_create(oid, amt, pt)
    ok, auth, purl = zp_request(amt, f"{'پیش‌پرداخت' if pt == 'advance' else 'پرداخت نهایی'} #{oid}", f"{PUBLIC_URL}/payment/callback/{p['id']}")
    if ok and purl:
        pay_update(p["id"], authority=auth)
        return jsonify({"payment_url": purl, "payment_id": p["id"], "amount": amt})
    return jsonify({"error": auth}), 500

@flask_app.route("/api/customer/referral/apply", methods=["POST"])
@_user_only
def api_cust_ref_apply(u):
    code = (request.json or {}).get("code", "").strip().upper()
    ref = ref_get_by_code(code)
    if not ref: return jsonify({"error": "کد معرف نامعتبر"}), 400
    if ref["user_id"] == u["id"]: return jsonify({"error": "کد خودتان!"}), 400
    for o in order_all(uid_=u["id"]):
        if o.get("referral_code"): return jsonify({"error": "قبلاً استفاده کرده‌اید"}), 400
    ref_record_invite(ref["user_id"], u["id"])
    return jsonify({"discount_percent": settings_get().get("referral_discount_percent", 10), "message": "کد معرف تأیید شد!"})

# ── payment callback ──
@flask_app.route("/payment/callback/<pid>")
def pay_callback(pid):
    auth = request.args.get("Authority", ""); st = request.args.get("Status", "")
    p = pay_get(pid)
    if not p: return _pay_html(False, error="تراکنش یافت نشد")
    o = order_get(p["order_id"])
    if st != "OK":
        pay_update(pid, status="failed")
        return _pay_html(False, p, o, error="پرداخت لغو شد")
    ok, ref = zp_verify(auth, p["amount"])
    if ok:
        pay_update(pid, ref_id=str(ref), status="paid")
        for aid in ADMIN_IDS:
            _notify_user(aid, f"💳 پرداخت جدید!\nسفارش #{o['id'] if o else '-'}\nمبلغ: {p['amount']:,} تومان")
        return _pay_html(True, p, o, ref_id=ref)
    pay_update(pid, status="failed")
    return _pay_html(False, p, o, error=str(ref))

def _pay_html(success, p=None, o=None, ref_id=None, error=None):
    c = "#22c55e" if success else "#ef4444"; icon = "✓" if success else "✕"; title = "پرداخت موفق" if success else "پرداخت ناموفق"
    detail = f"<p>کد پیگیری: {ref_id}</p>" if success else f"<p>{error}</p>"
    bot = os.getenv("BOT_USERNAME", ""); amt = f"{p['amount']:,}" if p else "-"; oid = o["id"] if o else "-"; oname = o["category_name"] if o else "-"
    return f"""<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{{margin:0;padding:0}}body{{font-family:-apple-system,Tahoma;background:#0a0a0a;color:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.card{{background:#141414;border-radius:20px;padding:32px 24px;max-width:360px;width:100%;text-align:center;border:1px solid #222}}
.icon{{width:64px;height:64px;border-radius:50%;background:{c}20;display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:28px;color:{c}}}
h2{{font-size:18px;font-weight:600;margin-bottom:16px;color:{c}}}p{{font-size:13px;color:#888;margin:6px 0}}
.row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:13px}}
.row span:first-child{{color:#666}}.row span:last-child{{color:#ddd;font-weight:500}}
.btn{{display:inline-block;padding:12px 32px;background:#fff;color:#000;text-decoration:none;border-radius:12px;margin-top:20px;font-weight:600;font-size:14px}}</style></head><body>
<div class="card"><div class="icon">{icon}</div><h2>{title}</h2>{detail}
<div class="row"><span>مبلغ</span><span>{amt} تومان</span></div>
<div class="row"><span>سفارش</span><span>#{oid} - {oname}</span></div>
<a class="btn" href="https://t.me/{bot}">بازگشت به ربات</a></div></body></html>"""

# ═══════════════ TELEGRAM BOT ═══════════════
user_sessions = {}
MAIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 ثبت سفارش جدید")],
    [KeyboardButton("📂 سفارش‌های من"), KeyboardButton("👥 باشگاه مشتریان")],
    [KeyboardButton("💰 کیف پول"), KeyboardButton("📞 پشتیبانی")],
], resize_keyboard=True)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_sessions.pop(u.id, None)
    wallet_ensure(u.id)
    if not ref_get_by_user(u.id): ref_create(u.id)
    if u.id in ADMIN_IDS and PUBLIC_URL:
        try:
            await ctx.bot.set_chat_menu_button(chat_id=u.id,
                menu_button=MenuButtonWebApp(text="⚙️ پنل مدیریت", web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin")))
        except: pass
    await update.message.reply_text(
        f"👋 سلام {esc(u.first_name)}!\n\nبه ربات سفارش پروژه دانشگاهی خوش آمدی.\nبرای شروع روی «ثبت سفارش جدید» کلیک کن.",
        reply_markup=MAIN_KB)

async def handle_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    if msg.document or msg.photo or msg.video or msg.voice:
        return await handle_file(update, ctx)
    if not msg.text: return
    t = msg.text; u = update.effective_user
    handlers = {
        "📋 ثبت سفارش جدید": order_start, "📂 سفارش‌های من": my_orders,
        "👥 باشگاه مشتریان": club, "💰 کیف پول": wallet_cmd, "📞 پشتیبانی": support_cmd
    }
    if t in handlers: return await handlers[t](update, ctx)
    s = user_sessions.get(u.id, {})
    if s.get("state") == "entering_desc": return await order_desc(update, ctx)
    await msg.reply_text("از دکمه‌های زیر استفاده کن 👇", reply_markup=MAIN_KB)

async def order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cats = cat_all(True)
    if not cats: await update.message.reply_text("⚠️ دسته‌بندی فعالی وجود ندارد.", reply_markup=MAIN_KB); return
    user_sessions[update.effective_user.id] = {"state": "selecting_category"}
    kb = [[InlineKeyboardButton(f"{c['name']} — {c['price']:,} تومان", callback_data=f"cat_{c['id']}")] for c in cats]
    kb.append([InlineKeyboardButton("انصراف", callback_data="cancel")])
    await update.message.reply_text("🎯 نوع پروژه را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

async def cat_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = q.from_user
    if q.data == "cancel": user_sessions.pop(u.id, None); await q.edit_message_text("لغو شد."); return
    cid = q.data.replace("cat_", ""); cat = cat_get(cid)
    if not cat: await q.edit_message_text("⚠️ دسته‌بندی نامعتبر."); return
    user_sessions[u.id] = {"state": "entering_desc", "category_id": cid}
    adv = int(cat["price"] * cat["advance_percent"] / 100)
    await q.edit_message_text(f"📌 {cat['name']}\n💰 قیمت: {cat['price']:,} تومان\n💳 پیش‌پرداخت: {adv:,} تومان\n\n📝 توضیحات پروژه را بنویسید:")

async def order_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; s = user_sessions.pop(u.id, {})
    if s.get("state") != "entering_desc": return
    o = order_create(u.id, u.username, u.first_name, s["category_id"], update.message.text)
    if not o: await update.message.reply_text("⚠️ خطا."); return
    await update.message.reply_text(f"✅ سفارش #{o['id']} ثبت شد!\n\nحالا فایل‌های مدارک را بفرست (عکس، PDF، Word و...)\nبعدش روی «اتمام» کلیک کن.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ اتمام آپلود", callback_data=f"finish_{o['id']}")],
            [InlineKeyboardButton("⏭ رد کردن", callback_data=f"skip_{o['id']}")]]))
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid, f"📬 سفارش جدید #{o['id']}\nاز: {esc(u.first_name)} (@{u.username or '---'})\nدسته: {o['category_name']}\nمبلغ: {o['price']:,} تومان")
        except: pass

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; msg = update.message
    file = msg.document or (msg.photo[-1] if msg.photo else None) or msg.video or msg.voice
    if not file: return
    fname = getattr(file, 'file_name', None) or f"file_{file.file_id[:8]}"
    try:
        tf = await ctx.bot.get_file(file.file_id)
        sp = UPLOAD_FOLDER / f"{_uid()}_{fname}"
        await tf.download_to_drive(sp)
        oo = order_all(uid_=u.id)
        if not oo: await msg.reply_text("⚠️ اول سفارش ثبت کن."); return
        file_add(oo[0]["id"], fname, str(sp), file.file_id)
        await msg.reply_text(f"✅ «{esc(fname)}» آپلود شد.\nفایل بعدی یا «اتمام».")
    except Exception as e:
        await msg.reply_text("⚠️ خطا در آپلود.")

async def finish_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = q.from_user
    data = q.data; oid = data.replace("finish_", "").replace("skip_", "")
    o = order_get(oid)
    if not o or o["user_id"] != u.id: await q.edit_message_text("⚠️ سفارش یافت نشد."); return
    await q.edit_message_text("✅ فایل‌ها دریافت شد!" if data.startswith("finish_") else "✅ اطلاعات ثبت شد.")
    kb = [[InlineKeyboardButton(f"💳 پرداخت پیش‌پرداخت ({o['advance_amount']:,} تومان)", web_app=WebAppInfo(url=f"{PUBLIC_URL}/panel?order={oid}"))]]
    await ctx.bot.send_message(u.id, f"📋 سفارش #{oid}\n💰 مبلغ: {o['price']:,} تومان\n💳 پیش‌پرداخت: {o['advance_amount']:,} تومان\n\nبرای پرداخت روی دکمه زیر کلیک کن:", reply_markup=InlineKeyboardMarkup(kb))

async def my_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; oo = order_all(uid_=u.id)
    if not oo: await update.message.reply_text("📂 سفارشی نداری.", reply_markup=MAIN_KB); return
    sm = {"pending": "⏳ در انتظار", "paid_advance": "💰 در حال انجام", "in_progress": "🔄 در حال انجام",
          "completed": "✅ تکمیل", "paid_final": "💵 تسویه", "cancelled": "❌ لغو"}
    txt = "📂 سفارش‌های شما:\n\n"
    for o in oo[:8]: txt += f"▸ #{o['id']} — {o['category_name']}\n   {sm.get(o['status'], o['status'])} | {o['price']:,} تومان\n\n"
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

async def club(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; ref = ref_get_by_user(u.id) or ref_create(u.id); s = settings_get()
    txt = (f"👥 باشگاه مشتریان\n\n🔗 کد معرف شما: <code>{ref['code']}</code>\n👤 تعداد دعوت: {ref['invited_count']} نفر\n"
           f"💰 پاداش دریافتی: {ref['total_earned']:,} تومان\n\n🎁 دعوت هر نفر: {s.get('referral_reward', 50000):,} تومان\n"
           f"🎫 تخفیف برای دعوت‌شونده: {s.get('referral_discount_percent', 10)}%\n\n📋 کد معرف را هنگام ثبت سفارش وارد کنید.")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=MAIN_KB)

async def wallet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; w = wallet_ensure(u.id)
    txt = f"💰 کیف پول\n\nموجودی: {w['balance']:,} تومان\n\n📋 تراکنش‌های اخیر:\n"
    for t in w.get("transactions", [])[-5:][::-1]:
        sign, color = ("+", "🟢") if t["type"] == "credit" else ("-", "🔴")
        txt += f"{color} {sign}{t['amount']:,} — {t.get('description', '')}\n"
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

async def support_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = settings_get(); txt = "📞 پشتیبانی:\n"
    if s.get("support_phone"): txt += f"📱 {s['support_phone']}\n"
    if s.get("support_telegram"): txt += f"💬 @{s['support_telegram']}\n"
    if not s.get("support_phone") and not s.get("support_telegram"): txt += "از همین ربات پیام دهید."
    await update.message.reply_text(txt, reply_markup=MAIN_KB)

# ═══════════════ NOTIFICATION ═══════════════
def _notify_user(uid_, msg):
    """Send a message to a Telegram user (non-blocking)."""
    def _run():
        import asyncio
        async def _send():
            app_bot = Application.builder().token(BOT_TOKEN).build()
            try: await app_bot.bot.send_message(chat_id=uid_, text=msg)
            except: pass
            await app_bot.shutdown()
        try:
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(_send()); loop.close()
        except: pass
    threading.Thread(target=_run, daemon=True).start()

# ═══════════════ SEED ═══════════════
def seed():
    d = _db()
    if not d["categories"]:
        for n, de, p in [("مقاله و تحقیق", "نگارش مقاله، تحقیق کلاسی، پروپوزال", 500000),
                          ("برنامه‌نویسی", "پایتون، جاوا، C++، وب، اندروید", 1500000),
                          ("پاورپوینت", "اسلایدهای حرفه‌ای و قالب اختصاصی", 300000),
                          ("حل تمرین", "حل تمرین‌های درسی و مسائل", 200000),
                          ("طراحی گرافیک", "پوستر، لوگو، اینفوگرافیک", 400000)]:
            cat_add(n, de, p)
        log.info("demo categories seeded.")

# ═══════════════ MAIN ═══════════════
def run_flask():
    """Run Flask in daemon thread."""
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def main():
    """Build and run the bot with polling. Flask runs in a background thread."""
    seed()
    global PUBLIC_URL
    PUBLIC_URL = detect_url()
    if not PUBLIC_URL:
        PUBLIC_URL = f"http://localhost:{PORT}"
    log.info(f"PUBLIC_URL = {PUBLIC_URL}")

    # Get bot username
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
        if r.json().get("ok"):
            os.environ["BOT_USERNAME"] = r.json()["result"]["username"]
            log.info(f"bot: @{os.environ['BOT_USERNAME']}")
    except: pass

    # Build bot
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(cat_callback, pattern="^cancel$"))
    app.add_handler(CallbackQueryHandler(finish_callback, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(finish_callback, pattern="^skip_"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all))

    # Start Flask in daemon thread
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(1.5)
    log.info(f"🌐 Flask on port {PORT} | {PUBLIC_URL}/admin | {PUBLIC_URL}/panel")

    # Run bot polling (blocking - runs forever in main thread)
    log.info("🤖 Bot polling starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
