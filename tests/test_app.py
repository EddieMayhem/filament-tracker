"""Basic tests for the Filament Tracker app."""
import json
import os
import tempfile
import pytest

# Use a temp file DB so all connections share the same database
_test_db = os.path.join(tempfile.gettempdir(), "test_filament_tracker.db")
os.environ["DATABASE_PATH"] = _test_db
os.environ["WEBHOOKS_FILE"] = os.path.join(tempfile.gettempdir(), "test_webhooks.json")
os.environ["API_TOKEN"] = ""

from app import app, init_db


@pytest.fixture(autouse=True)
def client():
    app.config["TESTING"] = True
    # Drop and recreate tables
    import sqlite3
    conn = sqlite3.connect(_test_db)
    conn.execute("DROP TABLE IF EXISTS usage_log")
    conn.execute("DROP TABLE IF EXISTS items")
    conn.execute("DROP TABLE IF EXISTS filaments")
    conn.execute("DROP TABLE IF EXISTS tabs")
    conn.commit()
    conn.close()
    wh_path = os.environ["WEBHOOKS_FILE"]
    if os.path.exists(wh_path):
        os.remove(wh_path)
    init_db()
    with app.test_client() as client:
        yield client


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Inventory System" in resp.data


def test_list_tabs(client):
    resp = client.get("/api/tabs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) >= 1
    assert data[0]["slug"] == "filaments"


def test_create_tab(client):
    resp = client.post("/api/tabs", json={"name": "Test Tab", "tab_type": "custom"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "Test Tab"
    assert data["slug"] == "test-tab"


def test_add_filament(client):
    resp = client.post("/api/filaments", json={
        "brand": "Prusament", "color": "Galaxy Black",
        "total_price": 25.99, "quantity": 2
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["brand"] == "Prusament"
    assert data["quantity"] == 2
    assert data["status"] == "full"


def test_add_filament_missing_fields(client):
    resp = client.post("/api/filaments", json={"brand": "Test"})
    assert resp.status_code == 400


def test_take_one(client):
    # Add a filament with qty 3
    resp = client.post("/api/filaments", json={
        "brand": "PETG", "color": "White",
        "total_price": 60.0, "quantity": 3
    })
    fid = resp.get_json()["id"]

    # Take one
    resp = client.post(f"/api/filaments/{fid}/take-one")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["remaining"] == 2
    assert data["per_roll_price"] == 20.0


def test_take_one_last(client):
    resp = client.post("/api/filaments", json={
        "brand": "PLA", "color": "Red",
        "total_price": 20.0, "quantity": 1
    })
    fid = resp.get_json()["id"]

    resp = client.post(f"/api/filaments/{fid}/take-one")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filament"]["status"] == "used"


def test_delete_filament(client):
    resp = client.post("/api/filaments", json={
        "brand": "ABS", "color": "Blue", "total_price": 15.0
    })
    fid = resp.get_json()["id"]
    resp = client.delete(f"/api/filaments/{fid}")
    assert resp.status_code == 204


def test_add_item(client):
    # Create a custom tab first
    resp = client.post("/api/tabs", json={"name": "Boxes", "tab_type": "custom"})
    tab_id = resp.get_json()["id"]

    resp = client.post("/api/items", json={
        "tab_id": tab_id, "name": "Small Box",
        "quantity": 50, "total_price": 25.0
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "Small Box"
    assert data["status"] == "full"


def test_inventory_endpoint(client):
    resp = client.get("/api/inventory")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "filaments" in data


def test_proxy_rejects_invalid_url(client):
    resp = client.get("/api/proxy/image?url=http://localhost:8080/secret")
    assert resp.status_code == 403


def test_proxy_rejects_missing_url(client):
    resp = client.get("/api/proxy/image")
    assert resp.status_code == 400


def test_webhook_crud(client):
    # Create
    resp = client.post("/api/webhooks", json={
        "url": "https://discord.com/api/webhooks/123/abc",
        "name": "Test"
    })
    assert resp.status_code == 201
    wh_id = resp.get_json()["id"]

    # List
    resp = client.get("/api/webhooks")
    assert resp.status_code == 200
    assert len(resp.get_json()) == 1

    # Update
    resp = client.put(f"/api/webhooks/{wh_id}", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Updated"

    # Delete
    resp = client.delete(f"/api/webhooks/{wh_id}")
    assert resp.status_code == 204


def test_webhook_rejects_private_url(client):
    resp = client.post("/api/webhooks", json={
        "url": "http://169.254.169.254/metadata",
        "name": "Evil"
    })
    assert resp.status_code == 400


def test_invalid_json_body(client):
    resp = client.post("/api/filaments",
                       data="not json",
                       content_type="application/json")
    assert resp.status_code == 400


def test_security_headers(client):
    resp = client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in resp.headers
