# Filament Tracker / Inventory System

Flask-based inventory management web app for 3D printing filament and supplies, running on a Raspberry Pi.

## Features

- **Multi-tab inventory tracking** — Filaments, Shipping Boxes, Maintenance Products, or any custom category
- **Filament grouping** — entries grouped by brand + color with combined stock counts
- **Per-roll price tracking** — total price split proportionally when pulling from multi-roll entries
- **Pull / FIFO** — always consumes the oldest stock entry first
- **Amazon integration** — scrape product images and link directly to listings
- **Image proxy** — Amazon CDN images proxied through Flask to avoid CORS blocking
- **Usage history** — full log of every pull action with timestamp and remaining stock
- **Webhook engine** — fires events on add/update/delete/pull for Discord, Home Assistant, etc.
- **Dynamic tabs** — add, edit, and remove tabs with custom field definitions via the ⚙️ settings modal
- **Dark-themed UI** — clean web interface accessible from any browser on the network

## Tech Stack

- **Flask** (Python 3)
- **SQLite** (no server needed, database lives at `filaments.db`)
- **Systemd user service** — runs as a user-level service on port 5000
- **raspberry Pi** (running at `http://AiPi:5000` on the local network)

## Setup

### Run on Raspberry Pi

```bash
cd ~/filament-tracker
pip install flask

# Start the service
systemctl --user daemon-reload
systemctl --user enable filament-tracker
systemctl --user start filament-tracker
```

The app will be available at `http://localhost:5000` (or `http://AiPi:5000` from other machines on the network).

### Access from outside the network

Set up a Cloudflare tunnel or port forward on your router to reach it from the internet.

## Project Structure

```
filament-tracker/
├── app.py              # Flask API server
├── templates/
│   └── index.html     # Single-page UI (dark theme)
├── filaments.db        # SQLite database (gitignored)
├── webhooks.json      # Webhook configuration (gitignored)
└── README.md
```

## Webhook Events

The following events fire webhook POST requests to configured endpoints:

- `filament_added` — new inventory item added
- `filament_updated` — item details edited
- `filament_deleted` — item removed
- `filament_taken` — one unit pulled from stock

Edit `webhooks.json` to configure URLs and enabled events.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/inventory` | All tabs with items grouped |
| GET | `/api/tabs` | All tab definitions |
| POST | `/api/tabs` | Create a new tab |
| PUT | `/api/tabs/<id>` | Update a tab |
| DELETE | `/api/tabs/<id>` | Delete a tab |
| GET/POST | `/api/items` | All items / create item |
| PUT/DELETE | `/api/items/<id>` | Update/delete item |
| POST | `/api/tabs/<tab_id>/items/<item_id>/take-one` | Pull one unit |
| GET | `/api/usage` | Full usage/pull history |
| POST | `/api/filaments/<id>/scrape-image` | Scrape Amazon product image |
| GET | `/api/proxy/image?url=...` | Proxy Amazon images |
| GET | `/api/webhooks` | Webhook configs |
| POST | `/api/webhooks` | Create webhook |
| PUT/DELETE | `/api/webhooks/<id>` | Update/delete webhook |
