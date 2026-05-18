# ============================================================
# EDMUND LUNGIS - WhatsApp AI Assistant WITH IMAGES
# Meta WhatsApp API + Groq + Supabase
# ============================================================

import os
import json
import re
import logging
from uuid import uuid4
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
CUSTOMER_PROFILE_TABLE = os.environ.get("CUSTOMER_PROFILE_TABLE", "whatsapp_users")
AUTH_USERS_TABLE = "users"

GROQ_MODEL       = "llama-3.3-70b-versatile"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE
# ============================================================

product_context  = ""
products_list    = []   # Full product list with image_url
onboarding_sessions = {}

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

FRIENDLY_RETRY_MESSAGE = "Please try sending that once again 👍"
FRIENDLY_REPHRASE_MESSAGE = "Could you please rephrase that 😊"
REGISTRATION_SAVE_RETRY_MESSAGE = (
    "Sorry, we couldn't complete profile setup right now 😊\n"
    "Please try again in a moment."
)
REGISTRATION_RETRYING_MESSAGE = (
    "Your details were received 👍\n"
    "We are retrying the registration process."
)

ONBOARDING_BULK_PROMPT = (
    "Welcome to Edmund Lungis 😊\n\n"
    "Before we continue, please share the following details in one message:\n\n"
    "👤 Name:\n"
    "📧 Email:\n"
    "🏠 Address:\n\n"
    "Example:\n\n"
    "Name: Rajesh Kumar\n"
    "Email: rajesh@gmail.com\n"
    "Address: 12 Gandhi Street, Chennai - 600001"
)

# ============================================================
# SUPABASE
# ============================================================

def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_active_products(client: Client) -> list[dict]:
    response = client.table("products").select("*").eq("is_active", True).execute()
    return response.data


def fetch_profile_from_table(client: Client, table_name: str, mobile: str) -> dict | None:
    response = client.table(table_name).select("*").eq("mobile", mobile).limit(1).execute()
    if not response.data:
        return None
    record = response.data[0]
    record["_source_table"] = table_name
    return record


def fetch_customer_profile(client: Client, mobile: str) -> dict | None:
    primary_tables = [AUTH_USERS_TABLE]
    if CUSTOMER_PROFILE_TABLE != AUTH_USERS_TABLE:
        primary_tables.append(CUSTOMER_PROFILE_TABLE)

    for table_name in primary_tables:
        try:
            record = fetch_profile_from_table(client, table_name, mobile)
            if record:
                return record
        except Exception as e:
            logger.error("Profile lookup failed in table %s for %s: %s", table_name, mobile, e)
    return None


def validate_profile(profile: dict) -> list[str]:
    errors = []
    name = str(profile.get("name", "") or "").strip()
    email = str(profile.get("email", "") or "").strip()
    mobile = str(profile.get("mobile", "") or "").strip()
    address = str(profile.get("address", "") or "").strip()

    if not is_valid_name(name):
        errors.append("invalid_name")
    if not is_valid_email(email):
        errors.append("invalid_email")
    if not mobile:
        errors.append("missing_mobile")
    if not is_valid_address(address):
        errors.append("missing_address")
    return errors


def build_users_table_payload(profile: dict) -> dict:
    return {
        "name": profile["name"],
        "email": profile["email"],
        "mobile": profile["mobile"],
        "addresses": {
            "default": {
                "address": profile["address"],
            }
        },
        "role": "user",
        "no_of_orders": 0,
        "wishlist": [],
    }


def build_users_update_payload(profile: dict) -> dict:
    return {
        "name": profile["name"],
        "email": profile["email"],
        "mobile": profile["mobile"],
        "addresses": {
            "default": {
                "address": profile["address"],
            }
        },
    }


def build_whatsapp_users_payload(profile: dict, generated_uuid: str) -> dict:
    return {
        "id": generated_uuid,
        "name": profile["name"],
        "email": profile["email"],
        "mobile": profile["mobile"],
        "address": profile["address"],
    }


def log_profile_save_attempt(profile: dict, payload: dict, generated_uuid: str | None, target_table: str) -> None:
    logger.info(
        "Preparing profile save | table=%s | extracted_name=%s | extracted_email=%s | extracted_mobile=%s | extracted_address=%s | generated_uuid=%s | final_insert_payload=%s",
        target_table,
        profile.get("name"),
        profile.get("email"),
        profile.get("mobile"),
        profile.get("address"),
        generated_uuid,
        payload,
    )


def insert_customer_profile(client: Client, profile: dict) -> None:
    generated_uuid = str(uuid4())
    if CUSTOMER_PROFILE_TABLE == AUTH_USERS_TABLE:
        raise ValueError("Direct insert into public.users is disabled for WhatsApp-only customers.")

    payload = build_whatsapp_users_payload(profile, generated_uuid)
    log_profile_save_attempt(profile, payload, generated_uuid, CUSTOMER_PROFILE_TABLE)
    client.table(CUSTOMER_PROFILE_TABLE).insert(payload).execute()


def update_customer_profile(client: Client, source_table: str, mobile: str, profile: dict) -> None:
    if source_table == AUTH_USERS_TABLE:
        payload = build_users_update_payload(profile)
    else:
        payload = {
            "name": profile["name"],
            "email": profile["email"],
            "mobile": profile["mobile"],
            "address": profile["address"],
        }
    log_profile_save_attempt(profile, payload, None, source_table)
    client.table(source_table).update(payload).eq("mobile", mobile).execute()


def extract_default_address(user: dict | None) -> str:
    if not user:
        return ""
    plain_address = str(user.get("address", "") or "").strip()
    if plain_address:
        return plain_address
    addresses = user.get("addresses")
    if isinstance(addresses, dict):
        default_address = addresses.get("default", {})
        if isinstance(default_address, dict):
            return str(default_address.get("address", "") or "").strip()
    return ""


def get_missing_profile_fields(user: dict | None) -> list[str]:
    if not user:
        return ["name", "email", "address"]

    missing_fields = []
    if not str(user.get("name", "") or "").strip():
        missing_fields.append("name")
    if not str(user.get("email", "") or "").strip():
        missing_fields.append("email")
    if not extract_default_address(user):
        missing_fields.append("address")
    return missing_fields


def is_valid_name(name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", name).strip()
    return len(cleaned) >= 2 and any(char.isalpha() for char in cleaned)


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email.strip()))


def is_greeting_message(message: str) -> bool:
    greetings = {"hi", "hello", "hey", "start", "helo", "vanakkam", "hai", "hii"}
    return message.strip().lower() in greetings


def normalize_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    cleaned = re.sub(r"^(my name is|i am|i'm|im)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.-")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_address(address: str) -> str:
    cleaned = address.strip()
    cleaned = re.sub(r"^(my address is|address is|address)\s*[:,-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(i live in|i am from|i'm from|from)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_valid_address(address: str) -> bool:
    return len(address.strip()) >= 5


def build_session_progress(profile: dict) -> dict:
    name_collected = is_valid_name(str(profile.get("name", "") or "").strip())
    email_collected = is_valid_email(str(profile.get("email", "") or "").strip())
    address_collected = is_valid_address(str(profile.get("address", "") or "").strip())
    return {
        "name_collected": name_collected,
        "email_collected": email_collected,
        "address_collected": address_collected,
        "profile_completed": name_collected and email_collected and address_collected,
    }


def refresh_session_state(session: dict) -> None:
    progress = build_session_progress(session["profile"])
    session.update(progress)


def extract_profile_details(message: str) -> dict:
    extracted = {"name": "", "email": "", "address": ""}
    text = message.strip()
    if not text:
        return extracted

    email_match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", text, re.IGNORECASE)
    if email_match:
        extracted["email"] = normalize_email(email_match.group(1))

    name_match = re.search(r"(?im)^\s*(?:👤\s*)?name\s*:\s*(.+)$", text)
    if not name_match:
        name_match = re.search(r"(?im)\b(?:my name is|i am|i'm|im)\s+([A-Za-z][A-Za-z\s.'-]{1,})", text)
    if name_match:
        candidate = name_match.group(1).splitlines()[0]
        extracted["name"] = normalize_name(candidate)

    address_match = re.search(r"(?ims)^\s*(?:🏠\s*)?address\s*:\s*(.+)$", text)
    if not address_match:
        address_match = re.search(r"(?im)^\s*(?:i live in|i am from|i'm from|from)\s+(.+)$", text)
    if address_match:
        extracted["address"] = normalize_address(address_match.group(1))

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not extracted["name"]:
        for line in lines:
            lower_line = line.lower()
            if "@" in line:
                continue
            if any(keyword in lower_line for keyword in ["address", "email", "mail id", "mail", "i live in", "from"]):
                continue
            candidate = normalize_name(line)
            if is_valid_name(candidate):
                extracted["name"] = candidate
                break

    if not extracted["address"]:
        address_lines = []
        for line in lines:
            if extracted["email"] and extracted["email"] in line.lower():
                continue
            normalized_line = normalize_name(line)
            if extracted["name"] and normalized_line == extracted["name"]:
                continue
            lower_line = line.lower()
            if lower_line.startswith("name:") or lower_line.startswith("email:"):
                continue
            if "mail id" in lower_line:
                continue
            address_lines.append(line)

        if address_lines:
            extracted["address"] = normalize_address("\n".join(address_lines))

    return extracted


def get_onboarding_prompt(field: str, is_new_user: bool = False) -> str:
    if field == "name":
        if is_new_user:
            return ONBOARDING_BULK_PROMPT
        return "Please share your full name 😊"
    if field == "email":
        return "Thank you 👍\nPlease share your email address."
    return "Please share your delivery address for future orders 🚚"


def build_registration_completion_message(name: str = "") -> str:
    customer_name = str(name or "").strip()
    greeting = f"Thank you {customer_name} 😊" if customer_name else "Thank you 😊"
    return (
        f"{greeting}\n"
        "Your profile has been created successfully.\n\n"
        "You can now explore our premium collections 🛍️"
    )


def create_onboarding_session(user: dict | None, mobile: str, pending_query: str | None) -> dict:
    missing_fields = get_missing_profile_fields(user)
    profile = {
        "name": str(user.get("name", "") or "").strip() if user else "",
        "email": str(user.get("email", "") or "").strip() if user else "",
        "address": extract_default_address(user),
        "mobile": mobile,
    }
    session = {
        "mobile": mobile,
        "user_id": user.get("id") if user else None,
        "source_table": user.get("_source_table") if user else CUSTOMER_PROFILE_TABLE,
        "is_new_user": user is None,
        "pending_query": pending_query,
        "missing_fields": missing_fields,
        "current_step_index": 0,
        "profile": profile,
        "bulk_collection_pending": user is None and missing_fields == ["name", "email", "address"],
        "name_collected": False,
        "email_collected": False,
        "address_collected": False,
        "profile_completed": False,
    }
    refresh_session_state(session)
    onboarding_sessions[mobile] = session
    return session


def get_current_onboarding_field(session: dict) -> str | None:
    missing_fields = session.get("missing_fields", [])
    index = session.get("current_step_index", 0)
    if index >= len(missing_fields):
        return None
    return missing_fields[index]


def advance_onboarding_session(session: dict) -> str | None:
    session["current_step_index"] = session.get("current_step_index", 0) + 1
    return get_current_onboarding_field(session)


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
    raw = ""
    for attempt in range(2):
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
            logger.error(f"JSON parse error on attempt {attempt + 1}: {e} | Raw: {raw}")
            if attempt == 1:
                return {
                    "message": FRIENDLY_REPHRASE_MESSAGE,
                    "products": [],
                    "footer": ""
                }
        except Exception as e:
            logger.error(f"Groq API error on attempt {attempt + 1}: {e}")
            if attempt == 1:
                return {
                    "message": "Sorry, I couldn't process that properly 😊",
                    "products": [],
                    "footer": ""
                }

    return {
        "message": FRIENDLY_RETRY_MESSAGE,
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


async def send_ai_product_response(to: str, user_text: str):
    ai_response = get_ai_response_json(user_text)
    logger.info(f"AI Response: {ai_response}")
    await send_product_responses(to, ai_response)


async def complete_registration(session: dict) -> bool:
    client = get_supabase_client()
    profile = session["profile"]
    validation_errors = validate_profile(profile)
    if validation_errors:
        logger.error("Profile validation failed for %s: %s | profile=%s", session["mobile"], validation_errors, profile)
        return False
    for attempt in range(2):
        try:
            existing_user = fetch_customer_profile(client, session["mobile"])
            if existing_user:
                session["user_id"] = existing_user.get("id")
                session["source_table"] = existing_user.get("_source_table") or AUTH_USERS_TABLE
                update_customer_profile(
                    client,
                    session["source_table"],
                    session["mobile"],
                    profile,
                )
            else:
                insert_customer_profile(client, profile)
            onboarding_sessions.pop(session["mobile"], None)
            return True
        except Exception as e:
            logger.error(
                "Registration save failed for %s on attempt %s: %s | profile=%s",
                session["mobile"],
                attempt + 1,
                e,
                profile,
            )
    return False


async def process_onboarding_step(from_number: str, user_text: str, session: dict) -> dict:
    cleaned_text = user_text.strip()

    if session.get("bulk_collection_pending"):
        extracted = extract_profile_details(cleaned_text)
        if extracted["name"]:
            session["profile"]["name"] = extracted["name"]
        if extracted["email"]:
            session["profile"]["email"] = extracted["email"]
        if extracted["address"]:
            session["profile"]["address"] = extracted["address"]

        refresh_session_state(session)
        if session.get("profile_completed"):
            session["bulk_collection_pending"] = False
            session["missing_fields"] = []
            session["current_step_index"] = 0
        else:
            missing_fields = []
            if not session.get("name_collected"):
                missing_fields.append("name")
            if not session.get("email_collected"):
                missing_fields.append("email")
            if not session.get("address_collected"):
                missing_fields.append("address")
            session["bulk_collection_pending"] = False
            session["missing_fields"] = missing_fields
            session["current_step_index"] = 0
            await send_text_message(from_number, get_onboarding_prompt(missing_fields[0]))
            return {"status": f"awaiting_{missing_fields[0]}"}
    else:
        current_field = get_current_onboarding_field(session)

        if current_field == "name":
            normalized_name = normalize_name(cleaned_text)
            if not is_valid_name(normalized_name):
                await send_text_message(from_number, "Please share your full name 😊")
                return {"status": "awaiting_name"}
            session["profile"]["name"] = normalized_name
        elif current_field == "email":
            normalized_email = normalize_email(cleaned_text)
            if not is_valid_email(normalized_email):
                await send_text_message(from_number, "Please share a valid email address 😊")
                return {"status": "awaiting_email"}
            session["profile"]["email"] = normalized_email
        elif current_field == "address":
            normalized_address = normalize_address(cleaned_text)
            if not is_valid_address(normalized_address):
                await send_text_message(from_number, "Please share your delivery address 😊")
                return {"status": "awaiting_address"}
            session["profile"]["address"] = normalized_address

        refresh_session_state(session)
        next_field = advance_onboarding_session(session)
        if next_field:
            await send_text_message(from_number, get_onboarding_prompt(next_field))
            return {"status": f"awaiting_{next_field}"}

    registration_saved = await complete_registration(session)
    if not registration_saved:
        await send_text_message(from_number, REGISTRATION_SAVE_RETRY_MESSAGE)
        return {"status": "registration_save_retry_needed"}

    await send_text_message(
        from_number,
        build_registration_completion_message(session["profile"].get("name", "")),
    )

    pending_query = session.get("pending_query")
    if pending_query:
        await send_ai_product_response(from_number, pending_query)
        return {"status": "registration_completed_and_query_processed"}

    return {"status": "registration_completed"}


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

        client = get_supabase_client()
        user = fetch_customer_profile(client, from_number)
        pending_session = onboarding_sessions.get(from_number)

        if pending_session:
            return await process_onboarding_step(from_number, user_text, pending_session)

        greeting_message = is_greeting_message(user_text)
        pending_query = None if greeting_message else user_text

        if user is None:
            create_onboarding_session(None, from_number, pending_query)
            await send_text_message(from_number, ONBOARDING_BULK_PROMPT)
            return {"status": "new_user_onboarding_started"}

        missing_fields = get_missing_profile_fields(user)
        if missing_fields:
            session = create_onboarding_session(user, from_number, pending_query)
            first_field = get_current_onboarding_field(session)
            await send_text_message(from_number, get_onboarding_prompt(first_field))
            return {"status": "existing_user_profile_completion_started"}

        customer_name = str(user.get("name", "") or "").strip() or "there"
        if greeting_message:
            await send_text_message(from_number, f"Welcome back {customer_name} 😊\nHow can I help you today?")
            return {"status": "welcome_back_sent"}

        await send_ai_product_response(from_number, user_text)
        return {"status": "message_processed"}

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        if "from_number" in locals() and from_number:
            await send_text_message(from_number, "Sorry, I couldn't process that properly 😊")
        return {"status": "error", "detail": str(e)}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
