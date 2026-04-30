import sqlite3
import os
import json
import re
import uuid
import base64
import threading
import logging
from urllib.parse import urlparse
from datetime import datetime, date
from functools import wraps

import urllib.request
import urllib.error

from flask import Flask, render_template, request, jsonify, abort, Response, g

# ─── Configuration ──────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())

DB = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "filaments.db"))
WEBHOOKS_FILE = os.environ.get("WEBHOOKS_FILE", os.path.join(os.path.dirname(__file__), "webhooks.json"))
API_TOKEN = os.environ.get("API_TOKEN", "")  # Set this to require auth
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))
AMAZON_DOMAIN = os.environ.get("AMAZON_DOMAIN", "www.amazon.de")

# Branding
BRAND_NAME = os.environ.get("BRAND_NAME", "Inventory System")
BRAND_ICON = os.environ.get("BRAND_ICON", "📦")
BRAND_TAGLINE = os.environ.get("BRAND_TAGLINE", "3D print shop inventory management")
BRAND_ACCENT = os.environ.get("BRAND_ACCENT", "#00d4ff")
BRAND_ACCENT2 = os.environ.get("BRAND_ACCENT2", "#ff6b35")

# Allowed URL schemes and domains for proxy/webhooks
ALLOWED_IMAGE_DOMAINS = {"m.media-amazon.com", "images-na.ssl-images-amazon.com",
                         "images-eu.ssl-images-amazon.com", "ws-na.amazon-adsystem.com",
                         "ecx.images-amazon.com", "images-fe.ssl-images-amazon.com"}
ALLOWED_WEBHOOK_SCHEMES = {"http", "https"}
BLOCKED_WEBHOOK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254",
                         "[::1]", "metadata.google.internal"}

# ─── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─── Security headers ───────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none';"
    )
    return response

# ─── Authentication ─────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_TOKEN:
            return f(*args, **kwargs)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            token = request.args.get("token", "")
        if token != API_TOKEN:
            abort(401, "Unauthorized")
        return f(*args, **kwargs)
    return decorated

# ─── Database ───────────────────────────────────────────────────────────────────

def get_db():
    """Get a request-scoped database connection."""
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def get_standalone_db():
    """Get a standalone connection for init/background use."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_standalone_db() as db:
        # ── filaments table ────────────────────────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS filaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL,
                color TEXT NOT NULL,
                weight_grams INTEGER DEFAULT 1000,
                total_price REAL NOT NULL,
                quantity INTEGER DEFAULT 1 NOT NULL,
                status TEXT DEFAULT 'full',
                purchase_date DATE,
                reorder_level INTEGER DEFAULT 200,
                reorder_date DATE,
                notes TEXT,
                amazon_url TEXT,
                image_url TEXT,
                tab_id INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── usage_log ──────────────────────────────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filament_id INTEGER NOT NULL,
                taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                remaining INTEGER NOT NULL,
                FOREIGN KEY (filament_id) REFERENCES filaments(id) ON DELETE CASCADE
            )
        """)

        # ── tabs ────────────────────────────────────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS tabs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                icon TEXT DEFAULT '📦',
                color TEXT DEFAULT '#00d4ff',
                tab_type TEXT DEFAULT 'filament',
                fields TEXT DEFAULT '[]',
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── items (generic inventory for non-filament tabs) ────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tab_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER DEFAULT 1 NOT NULL,
                total_price REAL DEFAULT 0,
                status TEXT DEFAULT 'full',
                purchase_date DATE,
                reorder_level INTEGER DEFAULT 5,
                reorder_date DATE,
                notes TEXT,
                extra_data TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tab_id) REFERENCES tabs(id) ON DELETE CASCADE
            )
        """)

        # ── Migrations: add columns safely ─────────────────────────────────────
        migrations = [
            ("filaments", "amazon_url", "TEXT", None),
            ("filaments", "image_url", "TEXT", None),
            ("filaments", "quantity", "INTEGER", 1),
            ("filaments", "total_price", "REAL", None),
            ("filaments", "tab_id", "INTEGER", 1),
        ]
        for table, col, coltype, default in migrations:
            try:
                stmt = f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"
                if default is not None:
                    stmt += f" DEFAULT {default}"
                db.execute(stmt)
                db.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # ── Indexes ────────────────────────────────────────────────────────────
        db.execute("CREATE INDEX IF NOT EXISTS idx_filaments_tab_id ON filaments(tab_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_items_tab_id ON items(tab_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_filament_id ON usage_log(filament_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_filaments_brand_color ON filaments(brand, color)")
        db.commit()

        # ── settings (key-value store for branding etc.) ───────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        db.commit()

        # Seed default Filaments tab if tabs table is empty
        row = db.execute("SELECT COUNT(*) as c FROM tabs").fetchone()
        if row["c"] == 0:
            FILAMENT_FIELDS = json.dumps([
                {"key": "brand",        "label": "Brand",        "type": "text",   "required": True},
                {"key": "color",        "label": "Color",        "type": "text",   "required": True},
                {"key": "weight_grams","label": "Weight (g)",   "type": "number", "required": False},
                {"key": "total_price", "label": "Total Price (€)", "type": "number","required": True},
                {"key": "status",      "label": "Status",       "type": "select", "options": ["full","low","used","empty"], "required": False},
                {"key": "purchase_date","label": "Purchase Date","type": "date",   "required": False},
                {"key": "reorder_level","label": "Reorder Level (g)","type": "number","required": False},
                {"key": "reorder_date","label": "Reorder Date", "type": "date",   "required": False},
                {"key": "amazon_url",  "label": "Amazon URL",   "type": "url",    "required": False},
                {"key": "image_url",    "label": "Image URL",   "type": "url",    "required": False},
                {"key": "notes",        "label": "Notes",        "type": "textarea","required": False},
            ])
            db.execute("""
                INSERT INTO tabs (name, slug, icon, color, tab_type, fields, sort_order)
                VALUES ('Filaments', 'filaments', '🧵', '#00d4ff', 'filament', ?, 0)
            """, (FILAMENT_FIELDS,))
            db.commit()
            db.execute("UPDATE filaments SET tab_id = 1 WHERE tab_id IS NULL OR tab_id = 0")
            db.commit()

# ─── URL Validation ─────────────────────────────────────────────────────────────

def is_valid_image_url(url):
    """Validate that a URL points to an allowed image host."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.hostname not in ALLOWED_IMAGE_DOMAINS:
            return False
        return True
    except Exception:
        return False

def is_valid_webhook_url(url):
    """Validate that a webhook URL is safe (no SSRF)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ALLOWED_WEBHOOK_SCHEMES:
            return False
        hostname = parsed.hostname or ""
        if hostname in BLOCKED_WEBHOOK_HOSTS:
            return False
        # Block private IP ranges
        if hostname.startswith("10.") or hostname.startswith("192.168."):
            return False
        if hostname.startswith("172."):
            parts = hostname.split(".")
            if len(parts) >= 2:
                try:
                    second = int(parts[1])
                    if 16 <= second <= 31:
                        return False
                except ValueError:
                    pass
        return True
    except Exception:
        return False

# ─── Webhooks ───────────────────────────────────────────────────────────────────

_webhooks_lock = threading.Lock()

def load_webhooks():
    with _webhooks_lock:
        if os.path.exists(WEBHOOKS_FILE):
            try:
                with open(WEBHOOKS_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                logger.error("Failed to load webhooks.json")
                return []
    return []

def save_webhooks(webhooks):
    with _webhooks_lock:
        with open(WEBHOOKS_FILE, "w") as f:
            json.dump(webhooks, f, indent=2)

def fire_webhook(event, payload):
    """Fire webhooks asynchronously in background threads."""
    webhooks = load_webhooks()
    for wh in webhooks:
        if wh.get("enabled") and event in wh.get("events", []):
            if not is_valid_webhook_url(wh["url"]):
                logger.warning(f"Skipping unsafe webhook URL: {wh['url']}")
                continue
            t = threading.Thread(target=_send_webhook, args=(wh["url"], event, payload), daemon=True)
            t.start()

def _send_webhook(url, event, payload):
    try:
        data = json.dumps({"event": event, "payload": payload}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": f"{BRAND_NAME.replace(' ', '')}/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        logger.info(f"Webhook fired: {event} -> {url}")
    except Exception as e:
        logger.warning(f"Webhook failed for {url}: {e}")

# ─── Amazon helpers ─────────────────────────────────────────────────────────────

def extract_asin(url):
    patterns = [r'/dp/([A-Z0-9]{10})', r'/gp/product/([A-Z0-9]{10})', r'/product/([A-Z0-9]{10})']
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def scrape_amazon_image(url):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        for pat in [r'"hiRes"\s*:\s*"([^"]+)"', r'"large"\s*:\s*"([^"]+)"',
                    r'"landingImageUrl"\s*:\s*"([^"]+)"']:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        imgs = re.findall(r'(https://[a-z0-9.\-]+/images/I/[^\"]+\.(jpg|jpeg|png|webp))', html)
        for img in imgs:
            if "._SY" not in img[0] and "._SL150" not in img[0]:
                return img[0]
        if imgs:
            return imgs[0][0]
    except Exception as e:
        logger.warning(f"Amazon scrape failed for {url}: {e}")
    return None

# ─── Helpers ────────────────────────────────────────────────────────────────────

def auto_status(quantity):
    """Derive status from quantity."""
    if quantity == 0:
        return "empty"
    elif quantity == 1:
        return "low"
    return "full"

def safe_int(val, default=0):
    """Safely convert to int."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def safe_float(val, default=0.0):
    """Safely convert to float."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def get_json_or_400():
    """Get JSON body or abort with 400."""
    data = request.get_json(silent=True)
    if data is None:
        abort(400, "Invalid or missing JSON body")
    return data

def guess_per_item_price(item):
    """Derive per-item price for display from total_price / quantity."""
    qty = item.get("quantity") or 1
    total = item.get("total_price") or 0
    return round(total / qty, 2) if qty > 0 else total

def get_tab_fields(tab):
    """Parse the JSON fields schema from a tab row."""
    try:
        return json.loads(tab["fields"]) if tab["fields"] else []
    except (json.JSONDecodeError, TypeError):
        return []

def item_to_display(item, tab_fields):
    """Convert an item dict + tab fields to a display-friendly dict."""
    d = dict(item)
    d["_per_item_price"] = guess_per_item_price(d)
    if item.get("extra_data"):
        try:
            extra = json.loads(item["extra_data"]) if isinstance(item["extra_data"], str) else item["extra_data"]
            d.update(extra)
        except (json.JSONDecodeError, TypeError):
            pass
    return d

# ─── Init ───────────────────────────────────────────────────────────────────────

init_db()

# ─── Root / ───────────────────────────────────────────────────────────────────────

def get_branding():
    """Get branding settings, falling back to env vars / defaults."""
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'brand_%'").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    return {
        "brand_name": stored.get("brand_name", BRAND_NAME),
        "brand_icon": stored.get("brand_icon", BRAND_ICON),
        "brand_tagline": stored.get("brand_tagline", BRAND_TAGLINE),
        "brand_accent": stored.get("brand_accent", BRAND_ACCENT),
        "brand_accent2": stored.get("brand_accent2", BRAND_ACCENT2),
        "brand_logo": stored.get("brand_logo", ""),
    }

@app.route("/")
def index():
    b = get_branding()
    return render_template("index.html",
                           brand_name=b["brand_name"],
                           brand_icon=b["brand_icon"],
                           brand_tagline=b["brand_tagline"],
                           brand_accent=b["brand_accent"],
                           brand_accent2=b["brand_accent2"],
                           brand_logo=b["brand_logo"])

@app.route("/api/branding", methods=["GET"])
@require_auth
def get_branding_api():
    return jsonify(get_branding())

@app.route("/api/branding", methods=["PUT"])
@require_auth
def update_branding():
    data = request.get_json(force=True)
    db = get_db()
    allowed = ("brand_name", "brand_icon", "brand_tagline", "brand_accent", "brand_accent2")
    for key in allowed:
        if key in data:
            val = str(data[key]).strip()[:200]
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    db.commit()
    return jsonify(get_branding())

@app.route("/api/branding/logo", methods=["POST"])
@require_auth
def upload_logo():
    """Accept a logo image upload (stored as base64 data URI in settings)."""
    if "logo" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["logo"]
    if not f.filename:
        return jsonify({"error": "No file selected"}), 400
    # Validate content type
    allowed_types = {"image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp"}
    if f.content_type not in allowed_types:
        return jsonify({"error": f"Invalid image type: {f.content_type}"}), 400
    # Limit size to 256KB
    data = f.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        return jsonify({"error": "Logo must be under 256KB"}), 400
    data_uri = f"data:{f.content_type};base64,{base64.b64encode(data).decode()}"
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("brand_logo", data_uri))
    db.commit()
    return jsonify({"brand_logo": data_uri})

@app.route("/api/branding/logo", methods=["DELETE"])
@require_auth
def delete_logo():
    db = get_db()
    db.execute("DELETE FROM settings WHERE key = 'brand_logo'")
    db.commit()
    return jsonify({"ok": True})

# ─── Tab CRUD ───────────────────────────────────────────────────────────────────

@app.route("/api/tabs", methods=["GET"])
@require_auth
def list_tabs():
    db = get_db()
    rows = db.execute("SELECT * FROM tabs ORDER BY sort_order, id").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tabs", methods=["POST"])
@require_auth
def add_tab():
    data = get_json_or_400()
    if not data.get("name"):
        abort(400, "name required")
    slug = data.get("slug") or re.sub(r'[^a-z0-9]+', '-', data["name"].lower()).strip('-')
    fields = data.get("fields", [])
    if isinstance(fields, list):
        fields = json.dumps(fields)

    db = get_db()
    max_ord = db.execute("SELECT COALESCE(MAX(sort_order), -1) as m FROM tabs").fetchone()["m"]
    cur = db.execute("""
        INSERT INTO tabs (name, slug, icon, color, tab_type, fields, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["name"], slug,
        data.get("icon", "📦"),
        data.get("color", "#00d4ff"),
        data.get("tab_type", "custom"),
        fields,
        max_ord + 1
    ))
    db.commit()
    row = db.execute("SELECT * FROM tabs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201

@app.route("/api/tabs/<int:id>", methods=["PUT"])
@require_auth
def update_tab(id):
    data = get_json_or_400()
    db = get_db()

    # Check existence first
    row = db.execute("SELECT * FROM tabs WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "tab not found")

    sets, vals = [], []
    for f in ["name", "slug", "icon", "color", "tab_type", "fields", "sort_order"]:
        if f in data:
            val = data[f]
            if f == "fields" and isinstance(val, list):
                val = json.dumps(val)
            sets.append(f"{f} = ?")
            vals.append(val)
    if not sets:
        abort(400, "nothing to update")
    vals.append(id)
    db.execute(f"UPDATE tabs SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    row = db.execute("SELECT * FROM tabs WHERE id = ?", (id,)).fetchone()
    return jsonify(dict(row))

@app.route("/api/tabs/<int:id>", methods=["DELETE"])
@require_auth
def delete_tab(id):
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM tabs").fetchone()["c"]
    if count <= 1:
        abort(400, "Cannot delete the last tab")
    db.execute("DELETE FROM items WHERE tab_id = ?", (id,))
    db.execute("DELETE FROM filaments WHERE tab_id = ?", (id,))
    cur = db.execute("DELETE FROM tabs WHERE id = ?", (id,))
    db.commit()
    if cur.rowcount == 0:
        abort(404, "tab not found")
    return "", 204

# ─── Items (generic non-filament inventory) ───────────────────────────────────

ITEM_KNOWN_FIELDS = {"tab_id", "name", "quantity", "total_price", "status",
                     "purchase_date", "reorder_level", "reorder_date", "notes"}

@app.route("/api/items", methods=["GET"])
@require_auth
def list_items():
    tab_id = request.args.get("tab_id", type=int)
    db = get_db()
    if tab_id:
        rows = db.execute("SELECT * FROM items WHERE tab_id = ? ORDER BY created_at DESC", (tab_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/items", methods=["POST"])
@require_auth
def add_item():
    data = get_json_or_400()
    tab_id = data.get("tab_id")
    if not tab_id:
        abort(400, "tab_id required")

    name = data.get("name") or (data.get("brand", "") + " " + data.get("color", "")).strip()
    if not name:
        abort(400, "name required (or brand+color)")

    extra = {k: v for k, v in data.items() if k not in ITEM_KNOWN_FIELDS}

    quantity = safe_int(data.get("quantity", 1), 1)
    total_price = safe_float(data.get("total_price", 0))
    status = data.get("status") or auto_status(quantity)

    db = get_db()
    cur = db.execute("""
        INSERT INTO items (tab_id, name, quantity, total_price, status,
            purchase_date, reorder_level, reorder_date, notes, extra_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tab_id, name, quantity, total_price, status,
        data.get("purchase_date", date.today().isoformat()),
        safe_int(data.get("reorder_level", 5), 5),
        data.get("reorder_date"),
        data.get("notes", ""),
        json.dumps(extra)
    ))
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()

    fire_webhook("item_added", dict(row))
    return jsonify(dict(row)), 201

@app.route("/api/items/<int:id>", methods=["PUT"])
@require_auth
def update_item(id):
    data = get_json_or_400()
    db = get_db()

    # Check existence
    existing_row = db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    if not existing_row:
        abort(404, "item not found")

    sets, vals = [], []
    for f in ITEM_KNOWN_FIELDS:
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data[f])

    # extra_data: merge with existing
    extra_keys = {k for k in data.keys() if k not in ITEM_KNOWN_FIELDS}
    if "extra_data" in data:
        sets.append("extra_data = ?")
        vals.append(json.dumps(data["extra_data"]))
    elif extra_keys:
        existing = {}
        if existing_row["extra_data"]:
            try:
                existing = json.loads(existing_row["extra_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        existing.update({k: data[k] for k in extra_keys})
        sets.append("extra_data = ?")
        vals.append(json.dumps(existing))

    if not sets:
        abort(400, "nothing to update")
    vals.append(id)
    db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    row = db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    fire_webhook("item_updated", dict(row))
    return jsonify(dict(row))

@app.route("/api/items/<int:id>", methods=["DELETE"])
@require_auth
def delete_item(id):
    db = get_db()
    cur = db.execute("DELETE FROM items WHERE id = ?", (id,))
    db.commit()
    if cur.rowcount == 0:
        abort(404, "item not found")
    fire_webhook("item_deleted", {"id": id})
    return "", 204

@app.route("/api/items/<int:id>/take-one", methods=["POST"])
@require_auth
def take_one_item(id):
    """Decrement quantity of an item by 1. Marks status=used when quantity reaches 0."""
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "item not found")
    row = dict(row)
    qty = row["quantity"] or 1
    if qty <= 1:
        db.execute("UPDATE items SET quantity = 0, status = 'used' WHERE id = ?", (id,))
    else:
        db.execute("UPDATE items SET quantity = quantity - 1 WHERE id = ?", (id,))
        if row["status"] in ("full", "low"):
            new_status = "low" if qty - 1 <= 5 else "full"
            db.execute("UPDATE items SET status = ? WHERE id = ?", (new_status, id))
    db.commit()
    fire_webhook("item_updated", dict(db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()))
    return "", 204

# ─── All inventory (unified view for a tab) ────────────────────────────────────

@app.route("/api/inventory", methods=["GET"])
@require_auth
def get_inventory():
    """Return all inventory (filaments + items) grouped by tab."""
    tab_id = request.args.get("tab_id", type=int)
    db = get_db()
    tabs_rows = db.execute("SELECT * FROM tabs ORDER BY sort_order, id").fetchall()
    tab_list = [dict(t) for t in tabs_rows]

    if tab_id:
        tab_list = [t for t in tab_list if t["id"] == tab_id]

    result = {}
    for tab in tab_list:
        t = dict(tab)
        fields = get_tab_fields(tab)
        t["fields"] = fields
        field_keys = {f["key"] for f in fields}
        # Only use filaments table if tab_type is filament AND it has brand+color fields
        if tab["tab_type"] == "filament" and "brand" in field_keys and "color" in field_keys:
            rows = db.execute(
                "SELECT * FROM filaments WHERE tab_id = ? ORDER BY created_at DESC",
                (tab["id"],)
            ).fetchall()
            t["items"] = [dict(r) for r in rows]
        else:
            rows = db.execute(
                "SELECT * FROM items WHERE tab_id = ? ORDER BY created_at DESC",
                (tab["id"],)
            ).fetchall()
            t["items"] = [item_to_display(dict(r), fields) for r in rows]
        result[tab["slug"]] = t

    return jsonify(result)

# ─── Filament endpoints (tab-aware, backwards compat) ─────────────────────────

@app.route("/api/filaments", methods=["GET"])
@require_auth
def list_filaments():
    tab_id = request.args.get("tab_id", type=int)
    db = get_db()
    if tab_id:
        rows = db.execute("SELECT * FROM filaments WHERE tab_id = ? ORDER BY created_at DESC", (tab_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM filaments ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/filaments", methods=["POST"])
@require_auth
def add_filament():
    data = get_json_or_400()
    required = ["brand", "color", "total_price"]
    if not all(k in data for k in required):
        abort(400, "brand, color, total_price required")

    amazon_url = data.get("amazon_url", "")
    image_url = data.get("image_url", "")
    if amazon_url and not image_url:
        asin = extract_asin(amazon_url)
        if asin:
            scraped = scrape_amazon_image(f"https://{AMAZON_DOMAIN}/dp/{asin}")
            if scraped:
                image_url = scraped

    tab_id = safe_int(data.get("tab_id", 1), 1)
    quantity = safe_int(data.get("quantity", 1), 1)
    total_price = safe_float(data.get("total_price", 0))
    if not total_price:
        abort(400, "total_price required")
    status = data.get("status") or auto_status(quantity)

    db = get_db()
    cur = db.execute("""
        INSERT INTO filaments (brand, color, weight_grams, total_price, quantity, status,
            purchase_date, reorder_level, reorder_date, notes, amazon_url, image_url, tab_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["brand"], data["color"],
        safe_int(data.get("weight_grams", 1000), 1000),
        total_price, quantity, status,
        data.get("purchase_date", date.today().isoformat()),
        safe_int(data.get("reorder_level", 200), 200),
        data.get("reorder_date"),
        data.get("notes", ""),
        amazon_url, image_url, tab_id
    ))
    db.commit()
    row = db.execute("SELECT * FROM filaments WHERE id = ?", (cur.lastrowid,)).fetchone()

    filament = dict(row)
    fire_webhook("filament_added", filament)
    logger.info(f"Filament added: {filament['brand']} {filament['color']} (id={filament['id']})")
    return jsonify(filament), 201

@app.route("/api/filaments/<int:id>", methods=["PUT"])
@require_auth
def update_filament(id):
    data = get_json_or_400()
    db = get_db()

    # Check existence first
    existing = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    if not existing:
        abort(404, "not found")

    fields = ["brand","color","weight_grams","total_price","quantity","status",
              "purchase_date","reorder_level","reorder_date","notes","amazon_url","image_url","tab_id"]
    sets, vals = [], []
    for f in fields:
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data[f])
    if not sets:
        abort(400, "no valid fields")
    vals.append(id)
    db.execute(f"UPDATE filaments SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    filament = dict(row)
    fire_webhook("filament_updated", filament)
    return jsonify(filament)

@app.route("/api/filaments/<int:id>", methods=["DELETE"])
@require_auth
def delete_filament(id):
    db = get_db()
    cur = db.execute("DELETE FROM filaments WHERE id = ?", (id,))
    db.commit()
    if cur.rowcount == 0:
        abort(404, "not found")
    fire_webhook("filament_deleted", {"id": id})
    return "", 204

@app.route("/api/filaments/<int:id>/take-one", methods=["POST"])
@require_auth
def take_one(id):
    """Clone this filament entry as a 'used' spool, decrement quantity.
    Price is split proportionally. Uses a single connection with BEGIN IMMEDIATE for safety.
    """
    db = get_db()
    # Use BEGIN IMMEDIATE to prevent race conditions
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
        if not row:
            db.execute("ROLLBACK")
            abort(404, "not found")

        filament = dict(row)
        qty = filament.get("quantity") or 1
        total_price = filament.get("total_price") or 0
        per_roll_price = total_price / qty if qty > 0 else total_price
        remaining_qty = max(0, qty - 1)
        remaining_price = per_roll_price * remaining_qty

        if qty == 1:
            db.execute("UPDATE filaments SET status = 'used', quantity = 1 WHERE id = ?", (id,))
            updated_id = id
        else:
            db.execute("UPDATE filaments SET quantity = ?, total_price = ?, status = ? WHERE id = ?",
                       (remaining_qty, remaining_price, auto_status(remaining_qty), id))
            cur = db.execute("""
                INSERT INTO filaments (brand, color, weight_grams, total_price, quantity, status,
                    purchase_date, reorder_level, reorder_date, notes, amazon_url, image_url, tab_id)
                VALUES (?, ?, ?, ?, 1, 'used', ?, ?, ?, ?, ?, ?, ?)
            """, (
                filament["brand"], filament["color"],
                filament.get("weight_grams", 1000), per_roll_price,
                filament.get("purchase_date"),
                filament.get("reorder_level", 200),
                filament.get("reorder_date"),
                filament.get("notes", ""),
                filament.get("amazon_url", ""),
                filament.get("image_url", ""),
                filament.get("tab_id", 1),
            ))
            updated_id = cur.lastrowid

        db.execute("INSERT INTO usage_log (filament_id, remaining) VALUES (?, ?)", (id, remaining_qty))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    updated = db.execute("SELECT * FROM filaments WHERE id = ?", (updated_id,)).fetchone()
    fire_webhook("filament_taken", dict(updated))
    logger.info(f"Filament taken: id={id}, remaining={remaining_qty}")
    return jsonify({
        "original_id": id, "taken_id": updated_id,
        "remaining": remaining_qty, "per_roll_price": round(per_roll_price, 4),
        "filament": dict(updated)
    })

@app.route("/api/filaments/<int:id>/scrape-image", methods=["POST"])
@require_auth
def scrape_filament_image(id):
    db = get_db()
    row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "not found")
    filament = dict(row)
    if not filament.get("amazon_url"):
        abort(400, "no amazon_url set")
    asin = extract_asin(filament["amazon_url"])
    if not asin:
        abort(400, "could not extract ASIN")
    new_url = scrape_amazon_image(f"https://{AMAZON_DOMAIN}/dp/{asin}")
    if not new_url:
        abort(502, "no image found on Amazon page")
    db.execute("UPDATE filaments SET image_url = ? WHERE id = ?", (new_url, id))
    db.commit()
    return jsonify({"image_url": new_url})

# ─── Usage log ──────────────────────────────────────────────────────────────────

@app.route("/api/usage", methods=["GET"])
@require_auth
def get_all_usage():
    db = get_db()
    rows = db.execute("""
        SELECT ul.*, f.brand, f.color
        FROM usage_log ul
        JOIN filaments f ON f.id = ul.filament_id
        ORDER BY ul.taken_at DESC LIMIT 50
    """).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Image proxy (restricted to allowed domains) ──────────────────────────────

# Simple in-memory cache for proxied images
_image_cache = {}
_IMAGE_CACHE_MAX = 100

@app.route("/api/proxy/image")
@require_auth
def proxy_image():
    url = request.args.get("url")
    if not url:
        abort(400, "url param required")
    if not is_valid_image_url(url):
        abort(403, "URL not allowed. Only Amazon image CDN domains are permitted.")

    # Check cache
    if url in _image_cache:
        data, content_type = _image_cache[url]
        return Response(data, mimetype=content_type)

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/jpeg,image/png,image/webp,*/*",
            "Referer": f"https://{AMAZON_DOMAIN}/",
        }, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            # Cache it (evict oldest if full)
            if len(_image_cache) >= _IMAGE_CACHE_MAX:
                _image_cache.pop(next(iter(_image_cache)))
            _image_cache[url] = (data, content_type)
            return Response(data, mimetype=content_type)
    except urllib.error.HTTPError as e:
        abort(502, f"Upstream returned {e.code}")
    except Exception as e:
        abort(502, str(e))

# ─── Webhook CRUD ───────────────────────────────────────────────────────────────

@app.route("/api/webhooks", methods=["GET"])
@require_auth
def get_webhooks():
    return jsonify(load_webhooks())

@app.route("/api/webhooks", methods=["POST"])
@require_auth
def add_webhook():
    data = get_json_or_400()
    url = data.get("url", "")
    if not url:
        abort(400, "url required")
    if not is_valid_webhook_url(url):
        abort(400, "Invalid webhook URL. Must be HTTPS and not point to private/internal addresses.")

    webhooks = load_webhooks()
    wh = {
        "id": str(uuid.uuid4()),
        "url": url,
        "name": data.get("name", "Webhook"),
        "events": data.get("events", ["filament_added","filament_updated","filament_deleted"]),
        "enabled": True
    }
    webhooks.append(wh)
    save_webhooks(webhooks)
    return jsonify(wh), 201

@app.route("/api/webhooks/<wh_id>", methods=["PUT"])
@require_auth
def update_webhook(wh_id):
    data = get_json_or_400()
    webhooks = load_webhooks()
    for wh in webhooks:
        if wh["id"] == wh_id:
            if "url" in data:
                if not is_valid_webhook_url(data["url"]):
                    abort(400, "Invalid webhook URL")
                wh["url"] = data["url"]
            if "name" in data:
                wh["name"] = data["name"]
            if "events" in data:
                wh["events"] = data["events"]
            if "enabled" in data:
                wh["enabled"] = data["enabled"]
            save_webhooks(webhooks)
            return jsonify(wh)
    abort(404, "webhook not found")

@app.route("/api/webhooks/<wh_id>", methods=["DELETE"])
@require_auth
def delete_webhook(wh_id):
    webhooks = load_webhooks()
    before = len(webhooks)
    webhooks = [w for w in webhooks if w["id"] != wh_id]
    if len(webhooks) == before:
        abort(404, "webhook not found")
    save_webhooks(webhooks)
    return "", 204

@app.route("/api/webhooks/<wh_id>/test", methods=["POST"])
@require_auth
def test_webhook(wh_id):
    webhooks = load_webhooks()
    wh = next((w for w in webhooks if w["id"] == wh_id), None)
    if not wh:
        abort(404, "webhook not found")
    if not is_valid_webhook_url(wh["url"]):
        return jsonify({"ok": False, "error": "URL blocked by security policy"}), 400
    try:
        payload = json.dumps({"event": "test", "message": f"{BRAND_NAME} test ping"}).encode()
        req = urllib.request.Request(wh["url"], data=payload, headers={
            "Content-Type": "application/json", "User-Agent": f"{BRAND_NAME.replace(' ', '')}/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return jsonify({"ok": True, "status": resp.status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
