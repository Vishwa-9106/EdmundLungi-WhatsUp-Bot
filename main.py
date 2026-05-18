# ============================================================
# EDMUND LUNGIS - WhatsApp AI Assistant WITH IMAGES
# Meta WhatsApp API + Groq + Supabase
# ============================================================

import os
import json
import logging
import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from supabase import create_client, Client
from groq import Groq

# ============================================================
# CONFIGURATION
# ============================================================

SUPABASE_URL      = os.environ.get("SUPABASE_URL")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN    = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN      = os.environ.get("VERIFY_TOKEN", "edmund_lungis_verify_2024")

GROQ_MODEL       = "llama-3.3-70b-versatile"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE
# ============================================================

product_context  = ""
products_list    = []   # Full product list with image_url

ORDER_FOLLOW_UP_MESSAGE = (
    "🛒 To place an order, please send:\n"
    "• Product name\n"
    "• Size needed\n"
    "• Quantity\n\n"
    "Example:\n"
    "Black Dhoti with Gold Border\n"
    "Size: Large\n"
    "Quantity: 2\n\n"
    "Our team will assist you further 😊"
)
groq_client      = None

# ============================================================
# SUPABASE
# ============================================================

def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_active_products(client: Client) -> list[dict]:
    response = client.table("products").select("*").eq("is_active", True).execute()
    return response.data


def format_products_as_context(products: list[dict]) -> str:
    lines = ["=== EDMUND LUNGIS PRODUCT CATALOG ===\n"]
    for i, p in enumerate(products, 1):
        sizes     = p.get("sizes", [])
        sizes_str = ", ".join(sizes) if isinstance(sizes, list) and sizes else "Not specified"
        stock_qty    = p.get("stock_quantity", 0) or 0
        availability = "In Stock" if stock_qty > 0 else "Out of Stock"
        lines.append(f"""
PRODUCT {i}:
  ID          : {p.get('id', 'N/A')}
  Name        : {p.get('name', 'N/A')}
  Category    : {p.get('category', 'N/A')}
  Description : {p.get('description', 'N/A')}
  Material    : {p.get('material', 'N/A')}
  Color       : {p.get('color', 'N/A')}
  Price       : ₹{p.get('price', 'N/A')}
  Orig Price  : ₹{p.get('original_price', 'N/A')}
  Sizes       : {sizes_str}
  Stock       : {stock_qty} units
  Availability: {availability}
  Image URL   : {p.get('image_url', 'none')}
""")
    return "\n".join(lines)


# ============================================================
# SYSTEM PROMPT
# ============================================================

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
10. Show maximum 3 products per response to keep it clean
11. End every reply with: "Reply with your query to explore more! 🛍️"

VERY IMPORTANT - RESPONSE FORMAT:
You MUST respond in valid JSON format like this:

{
  "message": "Your friendly intro text here",
  "products": [
    {
      "id": "product-uuid-here",
      "name": "Product Name",
      "price": 199,
      "color": "White",
      "material": "Cotton",
      "sizes": "Free Size, Large",
      "availability": "In Stock",
      "image_url": "https://..."
    }
  ],
  "footer": "Reply with your query to explore more! 🛍️"
}

If no products found:
{
  "message": "Product not available ❌",
  "products": [],
  "footer": "Try searching for lungi, dhoti, or veshti! 🛍️"
}

ONLY return valid JSON. No extra text outside JSON.
"""


# ============================================================
# GROQ - Get AI Response as JSON
# ============================================================

def get_ai_response_json(user_message: str) -> dict:
    global product_context, groq_client
    if not product_context:
        return {
            "message": "⚠️ Product catalog not loaded. Please try again shortly.",
            "products": [],
            "footer": ""
        }
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"PRODUCT CATALOG:\n{product_context}\n\nCUSTOMER QUERY: {user_message}"
                }
            ],
            max_tokens=1000,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()

        # Clean JSON if wrapped in markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e} | Raw: {raw}")
        return {
            "message": "Sorry, I couldn't process your request. Please try again! 🙏",
            "products": [],
            "footer": ""
        }
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return {
            "message": "Sorry, I couldn't process your request. Please try again! 🙏",
            "products": [],
            "footer": ""
        }


# ============================================================
# META WHATSAPP API - Send Functions
# ============================================================

async def send_text_message(to: str, message: str):
    """Send a plain text message."""
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
        resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"Text send error: {resp.status_code} - {resp.text}")
        else:
            logger.info(f"Text sent to {to}")


async def send_image_message(to: str, image_url: str, caption: str):
    """Send an image message with caption."""
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption,
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"Image send error: {resp.status_code} - {resp.text}")
        else:
            logger.info(f"Image sent to {to} | URL: {image_url}")


async def send_product_responses(to: str, ai_response: dict):
    """
    Send intro message first, then each product as image + caption.
    """
    message  = ai_response.get("message", "")
    products = ai_response.get("products", [])
    footer   = ai_response.get("footer", "")

    # Step 1: Send intro text message
    if message:
        await send_text_message(to, message)

    # Step 2: Send each product as image with caption
    if products:
        for product in products:
            name         = product.get("name", "N/A")
            price        = product.get("price", "N/A")
            color        = product.get("color", "N/A")
            material     = product.get("material", "N/A")
            sizes        = product.get("sizes", "N/A")
            availability = product.get("availability", "In Stock")
            image_url    = product.get("image_url", "")

            # Build caption for the image
            caption = (
                f"🛒 *{name}*\n"
                f"💰 Price: ₹{price}\n"
                f"🎨 Color: {color}\n"
                f"🧵 Material: {material}\n"
                f"📏 Sizes: {sizes}\n"
                f"✅ Status: {availability}"
            )

            if image_url and image_url != "none" and image_url.startswith("http"):
                # Send image with caption
                await send_image_message(to, image_url, caption)
            else:
                # No image available — send text only
                await send_text_message(to, caption)

    # Step 3: Send footer message
    if footer:
        await send_text_message(to, footer)

    # Step 4: Send one order follow-up after the full product listing
    if products:
        await send_text_message(to, ORDER_FOLLOW_UP_MESSAGE)


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global product_context, products_list, groq_client

    logger.info("Starting Edmund Lungis WhatsApp Assistant...")
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq client initialized")

    try:
        client        = get_supabase_client()
        products_list = fetch_active_products(client)
        logger.info(f"Fetched {len(products_list)} active products from Supabase")
        product_context = format_products_as_context(products_list)
        logger.info(f"Product context built ({len(product_context)} chars)")
        logger.info("Assistant ready!")
    except Exception as e:
        logger.error(f"Startup error: {e}")

    yield
    logger.info("Shutting down...")


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Edmund Lungis WhatsApp AI Assistant",
    description="Traditional Indian wear chatbot with images via Meta WhatsApp",
    version="4.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "status"  : "running",
        "service" : "Edmund Lungis WhatsApp AI Assistant",
        "version" : "4.0.0",
        "products": f"{len(products_list)} loaded" if products_list else "not loaded",
    }


@app.get("/health")
async def health():
    return {
        "status"          : "healthy",
        "products_loaded" : bool(product_context),
        "groq_ready"      : groq_client is not None,
    }


# ── Webhook Verification (GET) ────────────────────────────────
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


# ── Webhook Receive Messages (POST) ──────────────────────────
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

        logger.info(f"From: {from_number} | Type: {message_type}")

        # Handle non-text messages
        if message_type != "text":
            await send_text_message(
                from_number,
                "Hi! 👋 I can only understand text messages.\nPlease type your query about our traditional wear! 🛍️"
            )
            return {"status": "non_text_ignored"}

        user_text = message["text"]["body"].strip()
        logger.info(f"Message: {user_text}")

        # Handle greetings
        greetings = ["hi", "hello", "hey", "start", "helo", "vanakkam", "hai", "hii"]
        if user_text.lower() in greetings:
            welcome = (
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
            await send_text_message(from_number, welcome)
            return {"status": "greeting_sent"}

        # Get AI response with product data
        ai_response = get_ai_response_json(user_text)
        logger.info(f"AI Response: {ai_response}")

        # Send products with images
        await send_product_responses(from_number, ai_response)

        return {"status": "message_processed"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        await send_text_message(
            from_number if 'from_number' in locals() else "",
            "Sorry, something went wrong. Please try again! 🙏"
        )
        return {"status": "error", "detail": str(e)}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
