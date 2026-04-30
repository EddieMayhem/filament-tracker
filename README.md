# Filament Tracker / Inventory System

Flask-based inventory management web app for 3D printing filament and supplies, running on a Raspberry Pi.

## Features

- **Multi-tab inventory tracking** — Filaments, Shipping Boxes, Maintenance Products, or any custom category
- **Filament grouping** — entries grouped by brand + color with combined stock counts
- **Per-roll price tracking** — total price split proportionally when pulling from multi-roll entries
- **Pull / FIFO** — always consumes the oldest stock entry first
- **Amazon integration** — scrape product images and link directly to listings
- **Image proxy** — Amazon CDN images proxied through Flask (restricted to Amazon domains)
- **Usage history** — full log of every pull action with timestamp and remaining stock
- **Webhook engine** — fires events on add/update/delete/pull for Discord, Home Assistant, etc.
- **Dynamic tabs** — add, edit, and remove tabs with custom field definitions via the settings modal
- **Dark-themed UI** — clean web interface accessible from any browser on the network
- **Optional API authentication** — token-based auth via `API_TOKEN` env var
- **Security headers** — CSP, X-Frame-Options, X-Content-Type-Options

## Tech Stack

- **Flask** (Python 3)
- **SQLite** with WAL mode (no server needed, database lives at `filaments.db`)
- **Systemd user service** — runs as a user-level service on port 5000
- **Raspberry Pi** (running at `http://AiPi:5000` on the local network)

## Setup

### Prerequisites

```bash
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
# Edit .env with your settings
```

Key settings:
- `SECRET_KEY` — Flask secret key (set to a random value)
- `API_TOKEN` — Set this to require Bearer token auth on all API endpoints (leave empty to disable)
- `AMAZON_DOMAIN` — Amazon domain for image scraping (default: `www.amazon.de`)
- `FLASK_DEBUG` — Set to `true` only for local development

### Run on Raspberry Pi

```bash
cd ~/filament-tracker
pip install -r requirements.txt

# Start the service
systemctl --user daemon-reload
systemctl --user enable filament-tracker
systemctl --user start filament-tracker
```

The app will be available at `http://localhost:5000` (or `http://AiPi:5000` from other machines on the network).

### Access from outside the network

Set up a Cloudflare tunnel or port forward on your router. **Important**: Set `API_TOKEN` in your `.env` if exposing the app to the internet.

### Running tests

```bash
pip install pytest
pytest tests/
```

## Project Structure

```
filament-tracker/
├── app.py              # Flask API server
├── templates/
│   └── index.html     # Single-page UI (dark theme)
├── tests/
│   └── test_app.py    # Test suite
├── filaments.db        # SQLite database (gitignored)
├── webhooks.json      # Webhook configuration (gitignored)
├── requirements.txt   # Python dependencies
├── .env.example       # Example environment config
└── README.md
```

## Webhook Events

The following events fire webhook POST requests to configured endpoints:

- `filament_added` — new filament item added
- `filament_updated` — filament details edited
- `filament_deleted` — filament removed
- `filament_taken` — one unit pulled from stock
- `item_added` — generic item added
- `item_updated` — generic item edited
- `item_deleted` — generic item removed

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
| GET/POST | `/api/filaments` | All filaments / create filament |
| PUT/DELETE | `/api/filaments/<id>` | Update/delete filament |
| POST | `/api/filaments/<id>/take-one` | Pull one unit from stock |
| POST | `/api/filaments/<id>/scrape-image` | Scrape Amazon product image |
| GET | `/api/proxy/image?url=...` | Proxy Amazon images (restricted domains) |
| GET | `/api/usage` | Usage/pull history |
| GET/POST | `/api/webhooks` | Webhook configs / create webhook |
| PUT/DELETE | `/api/webhooks/<id>` | Update/delete webhook |
| POST | `/api/webhooks/<id>/test` | Send test ping to webhook |

### Authentication

If `API_TOKEN` is set, all API endpoints require a Bearer token:

```
Authorization: Bearer your-token-here
```

Or as a query parameter: `?token=your-token-here`
