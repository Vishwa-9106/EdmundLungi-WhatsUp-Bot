# pip install groq supabase fastapi uvicorn python-dotenv twilio httpx

import os
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from supabase import create_client, Client
from groq import Groq

SUPABASE_URL           = os.environ.get("SUPABASE_URL")
SUPABASE_KEY           = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY           = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN         = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID      = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN           = os.environ.get("VERIFY_TOKEN", "edmund_lungis_verify_2024")

GROQ_MODEL = "llama-3.3-70b-versatile"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

product_context = ""
groq_client     = None

def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_active_products(client: Client) -> list[dict]:
    response = client.table("products").select("*").eq("is_active", True).execute()
    return response.data

def format_products_as_context(products: list[dict]) -> str:
    lines = ["=== EDMUND LUNGIS PRODUCT CATALOG ===\n"]
    for i, p in enumerate(products, 1):
        sizes = p.get("sizes", [])
        sizes_str = ", ".join(sizes) if isinstance(sizes, list) and sizes else "Not specified"
        stock_qty    = p.get("stock_quantity", 0) or 0
        availability = "In Stock" if stock_qty > 0 else "Out of Stock"
        price        = p.get("price", "N/A")
        orig_price   = p.get("original_price", "N/A")
        lines.append(f"""
PRODUCT {i}:
  Name        : {p.get('name', 'N/A')}
  Category    : {p.get('category', 'N/A')}
  Description : {p.get('description', 'N/A')}
  Material    : {p.get('material', 'N/A')}
  Color       : {p.get('color', 'N/A')}
  Price       : ₹{price}
  Orig Price  : ₹{orig_price}
  Sizes       : {sizes_str}
  Stock       : {stock_qty} units
  Availability: {availability}
  Product ID  : {p.get('id', 'N/A')}
""")
    return "\n".join(lines)

SYSTEM_PROMPT = """You are an AI ecommerce assistant for "Edmund Lungis", a traditional Indian clothing store.
You specialize in: lungi, dhoti, veshti, vesti, traditional wear.

RULES:
1. ONLY recommend products from the PRODUCT CATALOG provided. Never invent products.
2. Understand synonyms: veshti = dhoti = vesti, white dhoti = traditional dhoti, lungi = lungis
3. Filter by budget when mentioned (e.g., "under 500" means price <= 500)
4. Only recommend products where Availability = "In Stock"
5. Always show: product name, price (₹), material, color, sizes
6. Keep responses SHORT and WhatsApp-friendly
7. Use emojis for readability
8. If NO matching product found reply: "Product not available ❌"
9. Suggest alternatives if exact match unavailable
10. End every reply with: "Reply with your query to explore more! 🛍️"

FORMAT each product like:
🛒 *Product Name*
💰 Price: ₹XXX
🎨 Color: XXX
🧵 Material: XXX
📏 Sizes: XXX
✅ Status: In Stock
"""

def get_ai_response(user_message: str) -> str:
    global product_context, groq_client
    if not product_context:
        return "⚠️ Product catalog not loaded. Please try again shortly."
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"PRODUCT CATALOG:\n{product_context}\n\nCUSTOMER QUERY: {user_message}"}
            ],
            max_tokens=500,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return "Sorry, I couldn't process your request. Please try again! 🙏"

def build_twiml_response(message: str) -> str:
    message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{message}</Message>
</Response>"""

import httpx

async def send_meta_whatsapp_message(to: str, message: str):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if response.status_code != 200:
            logger.error(f"Meta API error: {response.status_code} - {response.text}")
        else:
            logger.info(f"Message sent to {to}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global product_context, groq_client
    logger.info("Starting Edmund Lungis WhatsApp Assistant...")
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq client initialized")
    try:
        client   = get_supabase_client()
        products = fetch_active_products(client)
        logger.info(f"Fetched {len(products)} active products from Supabase")
        product_context = format_products_as_context(products)
        logger.info(f"Product context built ({len(product_context)} chars)")
        logger.info("Assistant ready!")
    except Exception as e:
        logger.error(f"Startup error: {e}")
    yield
    logger.info("Shutting down...")

app = FastAPI(
    title="Edmund Lungis WhatsApp AI Assistant",
    description="Traditional Indian wear chatbot via Meta WhatsApp",
    version="3.0.0",
    lifespan=lifespan,
)

@app.get("/")
async def root():
    return {
        "status"  : "running",
        "service" : "Edmund Lungis WhatsApp AI Assistant",
        "version" : "3.0.0",
        "products": "loaded" if product_context else "not loaded",
    }

@app.get("/health")
async def health():
    return {
        "status"          : "healthy",
        "products_loaded" : bool(product_context),
        "groq_ready"      : groq_client is not None,
    }

# ── Meta Webhook Verification (GET) ──────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully!")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Forbidden", status_code=403)

# ── Meta Webhook Receive Messages (POST) ─────────────────────
@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
        logger.info(f"Incoming webhook: {body}")

        entry = body.get("entry", [])
        if not entry:
            return {"status": "no_entry"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "no_changes"}

        value = changes[0].get("value", {})

        if "statuses" in value:
            return {"status": "status_update_ignored"}

        messages = value.get("messages", [])
        if not messages:
            return {"status": "no_messages"}

        message      = messages[0]
        from_number  = message.get("from")
        message_type = message.get("type")

        if message_type != "text":
            await send_meta_whatsapp_message(
                from_number,
                "Hi! 👋 I can only understand text messages.\nPlease type your query about our traditional wear! 🛍️"
            )
            return {"status": "non_text_ignored"}

        user_text = message["text"]["body"].strip()
        logger.info(f"From: {from_number} | Message: {user_text}")

        greetings = ["hi", "hello", "hey", "start", "helo", "vanakkam", "hai"]
        if user_text.lower() in greetings:
            reply = (
                "👋 Welcome to *Edmund Lungis*!\n\n"
                "I'm your AI shopping assistant for traditional Indian wear. 🎽\n\n"
                "Try asking:\n"
                "• _show white dhoti_\n"
                "• _cotton lungi under 500_\n"
                "• _wedding veshti_\n"
                "• _silk dhoti_\n"
                "• _daily wear lungi_\n\n"
                "What are you looking for? 🛒"
            )
        else:
            reply = get_ai_response(user_text)

        await send_meta_whatsapp_message(from_number, reply)
        return {"status": "message_processed"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"status": "error", "detail": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)