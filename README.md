# Edmund Lungis Bot

FastAPI-based WhatsApp shopping assistant for Edmund Lungis. It connects WhatsApp Cloud API, Groq, and Supabase to help customers browse traditional Indian wear, add items to cart, save profile details, and place orders through chat.

## Features

- WhatsApp webhook endpoint for inbound customer messages
- AI-assisted product discovery using Groq
- Direct product browsing with filters like color, material, and budget
- Cart flow with add, view, remove, and checkout actions
- Customer profile capture for name, email, mobile, and address
- Order persistence in Supabase
- Automatic WhatsApp order status notifications with delivery logs
- Product replies with image + caption when an image URL is available
- Health and root endpoints for deployment checks

## Tech Stack

- Python
- FastAPI
- Uvicorn
- Groq
- Supabase
- HTTPX

## Project Files

- [main.py](/D:/website/edmund-lungis-bot/main.py) - main FastAPI app and WhatsApp bot logic
- [requirements.txt](/D:/website/edmund-lungis-bot/requirements.txt) - Python dependencies
- [Procfile](/D:/website/edmund-lungis-bot/Procfile) - process definition for deployment platforms
- [whatsapp_users.sql](/D:/website/edmund-lungis-bot/whatsapp_users.sql) - Supabase table setup for WhatsApp users and orders

## Requirements

- Python 3.10+
- A Supabase project
- A Groq API key
- A Meta WhatsApp Cloud API app with webhook access

## Environment Variables

Set these before starting the app:

```bash
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_SECRET_KEY=
SUPABASE_KEY=
GROQ_API_KEY=
WHATSAPP_TOKEN=
WHATSAPP_PHONE_ID=
VERIFY_TOKEN=edmund_lungis_verify_2024
PRODUCTS_TABLE=products
CUSTOMER_PROFILE_TABLE=whatsapp_users
WHATSAPP_ORDERS_TABLE=whatsapp_orders
WHATSAPP_NOTIFICATION_LOGS_TABLE=whatsapp_notification_logs
DEFAULT_COUNTRY_CODE=91
ORDER_STATUS_POLL_INTERVAL_SECONDS=2
ORDER_STATUS_POLL_BATCH_SIZE=50
ORDER_STATUS_MAX_RETRIES=3
ORDER_STATUS_RETRY_DELAY_SECONDS=30
ORDER_STATUS_BACKFILL_ON_START=false
```

### Notes

- `SUPABASE_SERVICE_ROLE_KEY` is strongly recommended for server-side inserts and updates.
- If `SUPABASE_SERVICE_ROLE_KEY` is not set, the app falls back to `SUPABASE_KEY`, but writes may fail because of RLS.
- `SUPABASE_SECRET_KEY` is also accepted as an alias for `SUPABASE_SERVICE_ROLE_KEY`.
- `VERIFY_TOKEN` must match the value configured in the Meta webhook settings.

## Database Setup

Run the SQL in [whatsapp_users.sql](/D:/website/edmund-lungis-bot/whatsapp_users.sql) in your Supabase SQL editor.

This creates:

- `whatsapp_users`
- `whatsapp_orders`
- `whatsapp_notification_logs`

The app also expects a products table, `products` by default, with active rows filtered by `is_active = true`.

The bot reads product fields such as:

- `id`
- `name`
- `price`
- `color`
- `material`
- `image_url`
- `is_active`

It also uses product size and stock-related fields when available.

## Install

```bash
pip install -r requirements.txt
```

## Run Locally

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

You can also run:

```bash
python main.py
```

## Deployment

The project includes this Procfile command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

This is suitable for platforms that inject `PORT` automatically.

## API Endpoints

- `GET /` - service status, version, and loaded product count
- `GET /health` - health check and readiness info
- `GET /webhook` - Meta webhook verification endpoint
- `POST /webhook` - receives incoming WhatsApp messages

## WhatsApp Flow

The bot can handle:

- greetings like `hi` or `hello`
- product queries like `show white dhoti`
- budget searches like `cotton lungi under 500`
- pagination requests like `show more`
- cart actions like `add black dhoti size large quantity 2`
- cart review and removal
- checkout and profile collection

## How It Works

1. On startup, the app loads active products from Supabase.
2. Incoming WhatsApp messages hit `POST /webhook`.
3. The app detects whether the message is a greeting, browse request, cart action, or checkout flow.
4. Product search is handled either by direct matching logic or by Groq using a constrained catalog prompt.
5. Responses are sent back through the WhatsApp Cloud API.
6. User profiles and orders are stored in Supabase.
7. A background watcher polls `whatsapp_orders.updated_at`, sends the right WhatsApp template when `order_status` changes, and records the result in `whatsapp_notification_logs`.

## Important Behavior

- Only text messages are processed.
- Duplicate WhatsApp message IDs are ignored in memory during runtime.
- Duplicate order status notifications are blocked at the database level with a unique `(order_id, order_status)` log key.
- Product replies are capped at 5 items.
- Cart and order session state is stored in memory, so restarting the app clears active conversation state.

## Next Improvements

- Add `.env` loading if you want local environment file support
- Add tests for webhook, cart, and checkout flows
- Document the expected products table schema in more detail
- Persist conversation session state outside memory if needed
