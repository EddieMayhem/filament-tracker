import sqlite3
import os
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, abort, Response
from html import escape as html_escape

app = Flask(__name__)
DB = os.path.join(os.path.dirname(__file__), "filaments.db")
WEBHOOKS_FILE = os.path.join(os.path.dirname(__file__), "webhooks.json")

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
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

        # Add columns that might not exist
        for col, coltype, default in [
            ("amazon_url", "TEXT", None),
            ("image_url", "TEXT", None),
            ("quantity", "INTEGER", 1),
            ("total_price", "REAL", None),
            ("tab_id", "INTEGER", 1),
        ]:
            try:
                if default is None:
                    db.execute(f"ALTER TABLE filaments ADD COLUMN {col} {coltype}")
                else:
                    db.execute(f"ALTER TABLE filaments ADD COLUMN {col} {coltype} DEFAULT {default}")
                db.commit()
            except Exception:
                pass

        # Add items table columns (in case of partial create)
        for col, coltype, default in [
            ("tab_id", "INTEGER", None),
            ("name", "TEXT", None),
            ("quantity", "INTEGER", 1),
            ("total_price", "REAL", 0),
            ("status", "TEXT", 'full'),
            ("purchase_date", "DATE", None),
            ("reorder_level", "INTEGER", 5),
            ("reorder_date", "DATE", None),
            ("notes", "TEXT", None),
            ("extra_data", "TEXT", '{}'),
        ]:
            try:
                db.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype}" +
                           (f" DEFAULT {default}" if default is not None else ""))
                db.commit()
            except Exception:
                pass

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
            # Link existing filaments to tab 1
            db.execute("UPDATE filaments SET tab_id = 1 WHERE tab_id IS NULL OR tab_id = 0")
            db.commit()

def load_webhooks():
    if os.path.exists(WEBHOOKS_FILE):
        with open(WEBHOOKS_FILE) as f:
            return json.load(f)
    return []

def save_webhooks(webhooks):
    with open(WEBHOOKS_FILE, "w") as f:
        json.dump(webhooks, f, indent=2)

def fire_webhook(event, payload):
    webhooks = load_webhooks()
    for wh in webhooks:
        if wh.get("enabled") and event in wh.get("events", []):
            try:
                data = json.dumps({"event": event, "payload": payload}).encode()
                req = urllib.request.Request(
                    wh["url"], data=data,
                    headers={"Content-Type": "application/json", "User-Agent": "InventorySystem/1.0"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception as e:
                app.logger.warning(f"Webhook failed for {wh['url']}: {e}")

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
        app.logger.warning(f"Amazon scrape failed for {url}: {e}")
    return None

init_db()

# ─── Helpers ────────────────────────────────────────────────────────────────────

def guess_per_item_price(item):
    """Derive per-item price for display from total_price / quantity."""
    qty = item.get("quantity") or 1
    total = item.get("total_price") or 0
    return round(total / qty, 2) if qty > 0 else total

def get_tab_fields(tab):
    """Parse the JSON fields schema from a tab row."""
    try:
        return json.loads(tab["fields"]) if tab["fields"] else []
    except Exception:
        return []

def item_to_display(item, tab_fields):
    """Convert an item dict + tab fields to a display-friendly dict."""
    d = dict(item)
    d["_per_item_price"] = guess_per_item_price(d)
    if item.get("extra_data"):
        try:
            extra = json.loads(item["extra_data"]) if isinstance(item["extra_data"], str) else item["extra_data"]
            d.update(extra)
        except Exception:
            pass
    return d

# ─── Root / ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ─── Tab CRUD ───────────────────────────────────────────────────────────────────

@app.route("/api/tabs", methods=["GET"])
def list_tabs():
    with get_db() as db:
        rows = db.execute("SELECT * FROM tabs ORDER BY sort_order, id").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tabs", methods=["POST"])
def add_tab():
    data = request.get_json()
    if not data.get("name"):
        abort(400, "name required")
    slug = data.get("slug") or re.sub(r'[^a-z0-9]+', '-', data["name"].lower())
    fields = data.get("fields", [])
    if isinstance(fields, list):
        fields = json.dumps(fields)

    with get_db() as db:
        # auto-assign sort_order
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
def update_tab(id):
    data = request.get_json()
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
    with get_db() as db:
        db.execute(f"UPDATE tabs SET {', '.join(sets)} WHERE id = ?", vals)
        db.commit()
        row = db.execute("SELECT * FROM tabs WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "tab not found")
    return jsonify(dict(row))

@app.route("/api/tabs/<int:id>", methods=["DELETE"])
def delete_tab(id):
    with get_db() as db:
        # Can't delete the last tab
        count = db.execute("SELECT COUNT(*) as c FROM tabs").fetchone()["c"]
        if count <= 1:
            abort(400, "Cannot delete the last tab")
        # Delete tab's items (for non-filament tabs) and filaments
        db.execute("DELETE FROM items WHERE tab_id = ?", (id,))
        db.execute("DELETE FROM filaments WHERE tab_id = ?", (id,))
        cur = db.execute("DELETE FROM tabs WHERE id = ?", (id,))
        db.commit()
    if cur.rowcount == 0:
        abort(404, "tab not found")
    return "", 204

# ─── Items (generic non-filament inventory) ───────────────────────────────────

@app.route("/api/items", methods=["GET"])
def list_items():
    tab_id = request.args.get("tab_id", type=int)
    with get_db() as db:
        if tab_id:
            rows = db.execute("SELECT * FROM items WHERE tab_id = ? ORDER BY created_at DESC", (tab_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/items", methods=["POST"])
def add_item():
    data = request.get_json()
    tab_id = data.get("tab_id")
    if not tab_id:
        abort(400, "tab_id required")

    name = data.get("name") or (data.get("brand", "") + " " + data.get("color", "")).strip()
    if not name:
        abort(400, "name required (or brand+color)")

    extra = {k: v for k, v in data.items()
             if k not in ["tab_id","name","quantity","total_price","status",
                          "purchase_date","reorder_level","reorder_date","notes"]}

    quantity = int(data.get("quantity", 1))
    total_price = float(data.get("total_price") or 0)
    status = data.get("status")
    if status is None:
        status = "empty" if quantity == 0 else ("low" if quantity == 1 else "full")

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO items (tab_id, name, quantity, total_price, status,
                purchase_date, reorder_level, reorder_date, notes, extra_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tab_id, name, quantity, total_price, status,
            data.get("purchase_date", date.today().isoformat()),
            data.get("reorder_level", 5),
            data.get("reorder_date"),
            data.get("notes", ""),
            json.dumps(extra)
        ))
        db.commit()
        row = db.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()

    fire_webhook("item_added", dict(row))
    return jsonify(dict(row)), 201

@app.route("/api/items/<int:id>", methods=["PUT"])
def update_item(id):
    data = request.get_json()
    sets, vals = [], []
    for f in ["tab_id","name","quantity","total_price","status",
              "purchase_date","reorder_level","reorder_date","notes"]:
        if f in data:
            sets.append(f"{f} = ?")
            vals.append(data[f])
    # extra_data: merge with existing
    if "extra_data" in data:
        sets.append("extra_data = ?")
        vals.append(json.dumps(data["extra_data"]))
    elif any(k not in ["tab_id","name","quantity","total_price","status",
                       "purchase_date","reorder_level","reorder_date","notes"]
             for k in data.keys()):
        # Merge extra fields into existing extra_data
        with get_db() as db:
            row = db.execute("SELECT extra_data FROM items WHERE id = ?", (id,)).fetchone()
        existing = {}
        if row and row["extra_data"]:
            try:
                existing = json.loads(row["extra_data"])
            except Exception:
                pass
        existing.update({k: v for k, v in data.items()
                        if k not in ["tab_id","name","quantity","total_price","status",
                                     "purchase_date","reorder_level","reorder_date","notes"]})
        sets.append("extra_data = ?")
        vals.append(json.dumps(existing))

    if not sets:
        abort(400, "nothing to update")
    vals.append(id)
    with get_db() as db:
        db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", vals)
        db.commit()
        row = db.execute("SELECT * FROM items WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "item not found")
    fire_webhook("item_updated", dict(row))
    return jsonify(dict(row))

@app.route("/api/items/<int:id>", methods=["DELETE"])
def delete_item(id):
    with get_db() as db:
        cur = db.execute("DELETE FROM items WHERE id = ?", (id,))
        db.commit()
    if cur.rowcount == 0:
        abort(404, "item not found")
    fire_webhook("item_deleted", {"id": id})
    return "", 204

# ─── All inventory (unified view for a tab) ────────────────────────────────────

@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    """Return all inventory (filaments + items) grouped by tab."""
    tab_id = request.args.get("tab_id", type=int)
    with get_db() as db:
        tabs = db.execute("SELECT * FROM tabs ORDER BY sort_order, id").fetchall()
        tab_list = [dict(t) for t in tabs]

        if tab_id:
            tab_list = [t for t in tab_list if t["id"] == tab_id]

        result = {}
        for tab in tab_list:
            t = dict(tab)
            t["fields"] = get_tab_fields(tab)
            if tab["tab_type"] == "filament":
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
                t["items"] = [item_to_display(dict(r), get_tab_fields(tab)) for r in rows]
            result[tab["slug"]] = t

        return jsonify(result)

# ─── Filament endpoints (tab-aware, backwards compat) ─────────────────────────

@app.route("/api/filaments", methods=["GET"])
def list_filaments():
    tab_id = request.args.get("tab_id", type=int)
    with get_db() as db:
        if tab_id:
            rows = db.execute("SELECT * FROM filaments WHERE tab_id = ? ORDER BY created_at DESC", (tab_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM filaments ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/filaments", methods=["POST"])
def add_filament():
    data = request.get_json()
    required = ["brand", "color", "total_price"]
    if not all(k in data for k in required):
        abort(400, "brand, color, total_price required")

    amazon_url = data.get("amazon_url", "")
    image_url = data.get("image_url", "")
    if amazon_url and not image_url:
        asin = extract_asin(amazon_url)
        if asin:
            scraped = scrape_amazon_image(f"https://www.amazon.de/dp/{asin}")
            if scraped:
                image_url = scraped

    tab_id = data.get("tab_id", 1)
    quantity = int(data.get("quantity", 1))
    total_price = float(data.get("total_price") or 0)
    if not total_price:
        abort(400, "total_price required")
    status = data.get("status")
    if status is None:
        status = "empty" if quantity == 0 else ("low" if quantity == 1 else "full")

    with get_db() as db:
        cur = db.execute("""
            INSERT INTO filaments (brand, color, weight_grams, total_price, quantity, status,
                purchase_date, reorder_level, reorder_date, notes, amazon_url, image_url, tab_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["brand"], data["color"],
            data.get("weight_grams", 1000),
            total_price, quantity, status,
            data.get("purchase_date", date.today().isoformat()),
            data.get("reorder_level", 200),
            data.get("reorder_date"),
            data.get("notes", ""),
            amazon_url, image_url, tab_id
        ))
        db.commit()
        row = db.execute("SELECT * FROM filaments WHERE id = ?", (cur.lastrowid,)).fetchone()

    filament = dict(row)
    fire_webhook("filament_added", filament)
    return jsonify(filament), 201

@app.route("/api/filaments/<int:id>", methods=["PUT"])
def update_filament(id):
    data = request.get_json()
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
    with get_db() as db:
        db.execute(f"UPDATE filaments SET {', '.join(sets)} WHERE id = ?", vals)
        db.commit()
        row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "not found")
    filament = dict(row)
    fire_webhook("filament_updated", filament)
    return jsonify(filament)

@app.route("/api/filaments/<int:id>", methods=["DELETE"])
def delete_filament(id):
    with get_db() as db:
        cur = db.execute("DELETE FROM filaments WHERE id = ?", (id,))
        db.commit()
    if cur.rowcount == 0:
        abort(404, "not found")
    fire_webhook("filament_deleted", {"id": id})
    return "", 204

@app.route("/api/filaments/<int:id>/take-one", methods=["POST"])
def take_one(id):
    """Clone this filament entry as a 'used' spool, decrement quantity.
    Price is split proportionally. Only works for tab_type='filament' entries.
    """
    with get_db() as db:
        row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "not found")

    filament = dict(row)
    qty = filament.get("quantity") or 1
    total_price = filament.get("total_price") or 0
    per_roll_price = total_price / qty if qty > 0 else total_price
    remaining_qty = max(0, qty - 1)
    remaining_price = per_roll_price * remaining_qty

    with get_db() as db:
        if qty == 1:
            db.execute("UPDATE filaments SET status = 'used', quantity = 1 WHERE id = ?", (id,))
            updated_id = id
        else:
            db.execute("UPDATE filaments SET quantity = ?, total_price = ? WHERE id = ?",
                       (remaining_qty, remaining_price, id))
            if remaining_qty == 0:
                db.execute("UPDATE filaments SET status = 'empty' WHERE id = ?", (id,))
            elif remaining_qty == 1:
                db.execute("UPDATE filaments SET status = 'low' WHERE id = ?", (id,))
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
        db.commit()
        updated = db.execute("SELECT * FROM filaments WHERE id = ?", (updated_id,)).fetchone()

    fire_webhook("filament_taken", dict(updated))
    return jsonify({
        "original_id": id, "taken_id": updated_id,
        "remaining": remaining_qty, "per_roll_price": round(per_roll_price, 4),
        "filament": dict(updated)
    })

@app.route("/api/filaments/<int:id>/scrape-image", methods=["POST"])
def scrape_filament_image(id):
    with get_db() as db:
        row = db.execute("SELECT * FROM filaments WHERE id = ?", (id,)).fetchone()
    if not row:
        abort(404, "not found")
    filament = dict(row)
    if not filament.get("amazon_url"):
        abort(400, "no amazon_url set")
    asin = extract_asin(filament["amazon_url"])
    if not asin:
        abort(400, "could not extract ASIN")
    new_url = scrape_amazon_image(f"https://www.amazon.de/dp/{asin}")
    if not new_url:
        abort(502, "no image found on Amazon page")
    with get_db() as db:
        db.execute("UPDATE filaments SET image_url = ? WHERE id = ?", (new_url, id))
        db.commit()
    return jsonify({"image_url": new_url})

# ─── Usage log ──────────────────────────────────────────────────────────────────

@app.route("/api/usage", methods=["GET"])
def get_all_usage():
    with get_db() as db:
        rows = db.execute("""
            SELECT ul.*, f.brand, f.color
            FROM usage_log ul
            JOIN filaments f ON f.id = ul.filament_id
            ORDER BY ul.taken_at DESC LIMIT 50
        """).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Image proxy ───────────────────────────────────────────────────────────────

@app.route("/api/proxy/image")
def proxy_image():
    url = request.args.get("url")
    if not url:
        abort(400, "url param required")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "image/jpeg,image/png,image/webp,*/*",
            "Referer": "https://www.amazon.de/",
        }, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return Response(resp.read(), mimetype=resp.headers.get("Content-Type", "image/jpeg"))
    except urllib.error.HTTPError as e:
        abort(502, f"Upstream returned {e.code}")
    except Exception as e:
        abort(502, str(e))

# ─── Webhook CRUD ───────────────────────────────────────────────────────────────

@app.route("/api/webhooks", methods=["GET"])
def get_webhooks():
    return jsonify(load_webhooks())

@app.route("/api/webhooks", methods=["POST"])
def add_webhook():
    data = request.get_json()
    if not data.get("url"):
        abort(400, "url required")
    webhooks = load_webhooks()
    wh = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S"),
        "url": data["url"],
        "name": data.get("name", "Webhook"),
        "events": data.get("events", ["filament_added","filament_updated","filament_deleted"]),
        "enabled": True
    }
    webhooks.append(wh)
    save_webhooks(webhooks)
    return jsonify(wh), 201

@app.route("/api/webhooks/<wh_id>", methods=["PUT"])
def update_webhook(wh_id):
    data = request.get_json()
    webhooks = load_webhooks()
    for wh in webhooks:
        if wh["id"] == wh_id:
            if "url" in data: wh["url"] = data["url"]
            if "name" in data: wh["name"] = data["name"]
            if "events" in data: wh["events"] = data["events"]
            if "enabled" in data: wh["enabled"] = data["enabled"]
            save_webhooks(webhooks)
            return jsonify(wh)
    abort(404, "webhook not found")

@app.route("/api/webhooks/<wh_id>", methods=["DELETE"])
def delete_webhook(wh_id):
    webhooks = load_webhooks()
    before = len(webhooks)
    webhooks = [w for w in webhooks if w["id"] != wh_id]
    if len(webhooks) == before:
        abort(404, "webhook not found")
    save_webhooks(webhooks)
    return "", 204

@app.route("/api/webhooks/<wh_id>/test", methods=["POST"])
def test_webhook(wh_id):
    webhooks = load_webhooks()
    wh = next((w for w in webhooks if w["id"] == wh_id), None)
    if not wh:
        abort(404, "webhook not found")
    try:
        payload = json.dumps({"event": "test", "message": "Inventory System test ping"}).encode()
        req = urllib.request.Request(wh["url"], data=payload, headers={
            "Content-Type": "application/json", "User-Agent": "InventorySystem/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return jsonify({"ok": True, "status": resp.status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
