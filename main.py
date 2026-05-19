import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from groq import Groq
from supabase import Client, create_client


SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "edmund_lungis_verify_2024")

PRODUCTS_TABLE = os.environ.get("PRODUCTS_TABLE", "products")
WHATSAPP_USERS_TABLE = os.environ.get("CUSTOMER_PROFILE_TABLE", "whatsapp_users")
WHATSAPP_ORDERS_TABLE = os.environ.get("WHATSAPP_ORDERS_TABLE", "whatsapp_orders")
DEFAULT_WHATSAPP_USERS_TABLE = "whatsapp_users"
AUTH_USERS_TABLE = "users"

GROQ_MODEL = "llama-3.3-70b-versatile"
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
PRODUCTS_PAGE_SIZE = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


GREETING_MESSAGE = """👋 Welcome to *Edmund Lungis*!

I'm your AI shopping assistant for traditional Indian wear. 🎽

Try asking:
• _show white dhoti_
• _cotton lungi under 500_
• _wedding veshti_
• _silk dhoti_
• _daily wear lungi_

What are you looking for? 🛒"""

ORDER_FOLLOW_UP_MESSAGE = """🛒 You can:
• Add products to cart
• View cart
• Remove products
• Checkout anytime

Example:
Add Black Dhoti size Large quantity 2"""

FRIENDLY_RETRY_MESSAGE = "Please try sending that once again 👍"
FRIENDLY_REPHRASE_MESSAGE = "Could you please rephrase that 😊"
FRIENDLY_ERROR_MESSAGE = "Sorry, I couldn't process that properly 😊"
PROFILE_SAVE_RETRY_MESSAGE = (
    "Sorry, I couldn't save your details right now 😊\nPlease try again in a moment."
)
CART_EMPTY_MESSAGE = "🛒 Your cart is empty right now.\n\nYou can continue shopping and add products anytime 😊"
CUSTOMER_INFO_REQUEST_MESSAGE = """😊 Before adding items to your cart, please share:

👤 Name:
📧 Email:
🏠 Address:

Example:
Name: Pradeep
Email: pradeep@gmail.com
Address: 12 Gandhi Street, Chennai"""

ORDER_STATUS_MESSAGES = {
    "confirmed": "✅ Your Edmund Lungis order has been confirmed!",
    "packed": "📦 Your order has been packed and is getting ready for shipment.",
    "shipped": "🚚 Your order has been shipped successfully.",
    "out_for_delivery": "🛵 Your order is out for delivery!",
    "delivered": "🎉 Your order has been delivered successfully.\n\nThank you for shopping with Edmund Lungis 😊",
    "cancelled": "❌ Your order has been cancelled.\n\nPlease contact support for assistance.",
}


product_context = ""
products_list: list[dict] = []
groq_client: Groq | None = None
customer_sessions: dict[str, dict] = {}


SYSTEM_PROMPT = """You are the official AI shopping assistant for "Edmund Lungis", a premium traditional Indian wear brand.

You help customers discover products in a friendly WhatsApp style.

Rules:
1. Only recommend products from the provided PRODUCT CATALOG.
2. Never invent products, prices, sizes, colors, images, or stock.
3. Understand synonyms: veshti = dhoti = vesti, lungi = lungis.
4. Match filters like color, material, category, and budget such as "under 500".
5. Recommend only in-stock products.
6. Return a maximum of 5 products.
7. Keep the tone conversational, premium, concise, and mobile-friendly.
8. Never mention technical details.
9. If nothing matches, suggest nearby alternatives when possible.

Return valid JSON only in this format:
{
  "message": "short intro",
  "products": [
    {
      "id": "uuid",
      "name": "Product name",
      "price": 499,
      "color": "White",
      "material": "Cotton",
      "sizes": "Free Size, Large",
      "availability": "In Stock",
      "image_url": "https://..."
    }
  ],
  "footer": "short footer"
}

If nothing matches:
{
  "message": "Product not available ❌",
  "products": [],
  "footer": "Try searching for lungi, dhoti, or veshti! 🛍️"
}"""


def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_name(name: str) -> str:
    cleaned = normalize_spaces(name)
    cleaned = re.sub(r"^(my name is|i am|i'm|im)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.-")


def normalize_email(email: str) -> str:
    return normalize_spaces(email).lower()


def normalize_address(address: str) -> str:
    cleaned = normalize_spaces(address)
    cleaned = re.sub(r"^(my address is|address is|address)\s*[:,-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(i live in|i am from|i'm from|from)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(?:,?\s*)?(?:mobile|mobile no|mobile number|phone|phone no|phone number|contact)\s*[:.-]?\s*\+?\d[\d\s-]{7,}$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ,")


def is_valid_name(name: str) -> bool:
    return len(name) >= 2 and any(char.isalpha() for char in name)


def is_valid_email(email: str) -> bool:
    return not email or bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email))


def is_valid_address(address: str) -> bool:
    return len(address.strip()) >= 5


def is_greeting_message(message: str) -> bool:
    greetings = {"hi", "hello", "hey", "vanakkam", "hlo", "helo", "hai", "hii", "start"}
    return normalize_lookup(message) in greetings


def is_show_more_message(message: str) -> bool:
    normalized = normalize_lookup(message)
    more_phrases = {
        "show more",
        "more",
        "next",
        "continue",
        "still more",
        "show next",
    }
    if normalized in more_phrases:
        return True
    return normalized.startswith("more ") or normalized.startswith("show more ")


def is_order_intent_message(message: str) -> bool:
    lowered = message.lower()
    keywords = [
        "checkout",
        "place order",
        "order this",
        "buy now",
        "book order",
        "i need this",
        "confirm order",
        "purchase this",
        "confirm cart",
    ]
    if any(keyword in lowered for keyword in keywords):
        return True
    return bool(re.search(r"\bsize\b", lowered) and re.search(r"\b(quantity|qty)\b", lowered))


def is_confirmation_message(message: str) -> bool:
    normalized = normalize_lookup(message)
    return normalized in {"confirm", "yes confirm", "confirm order", "yes", "ok confirm", "proceed"}


def is_change_address_message(message: str) -> bool:
    lowered = message.lower()
    return "change address" in lowered or "update address" in lowered


def is_wishlist_intent_message(message: str) -> bool:
    lowered = message.lower()
    keywords = ["save this", "add to wishlist", "wishlist this product", "wishlist this", "save to wishlist"]
    return any(keyword in lowered for keyword in keywords)


def is_add_to_cart_intent_message(message: str, mobile: str = "") -> bool:
    lowered = message.lower()
    keywords = [
        "add to cart",
        "add this",
        "save this",
        "save this product",
        "cart this",
        "i want this",
        "put this in cart",
        "add it to cart",
    ]
    if any(keyword in lowered for keyword in keywords):
        return True
    if re.search(r"\badd\s+\d+\b", lowered):
        return True
    if re.search(r"\b(size|qty|quantity)\b", lowered) and resolve_product_from_message(message, mobile):
        return True
    return False


def is_view_cart_message(message: str) -> bool:
    normalized = normalize_lookup(message)
    return normalized in {"view cart", "show cart", "my cart", "cart"}


def is_remove_from_cart_message(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in ["remove from cart", "remove item", "delete item", "remove "])


def is_checkout_message(message: str) -> bool:
    normalized = normalize_lookup(message)
    return normalized in {"checkout", "place order", "confirm cart", "confirm order"} or is_order_intent_message(message)


def fetch_active_products(client: Client) -> list[dict]:
    response = client.table(PRODUCTS_TABLE).select("*").eq("is_active", True).execute()
    return response.data or []


def fetch_profile_from_table(client: Client, table_name: str, mobile: str) -> dict | None:
    response = client.table(table_name).select("*").eq("mobile", mobile).limit(1).execute()
    if not response.data:
        return None
    record = response.data[0]
    record["_source_table"] = table_name
    return record


def get_profile_lookup_tables() -> list[str]:
    tables: list[str] = []
    for table_name in [WHATSAPP_USERS_TABLE, DEFAULT_WHATSAPP_USERS_TABLE, AUTH_USERS_TABLE]:
        if table_name and table_name not in tables:
            tables.append(table_name)
    return tables


def fetch_customer_profile(client: Client, mobile: str) -> dict | None:
    for table_name in get_profile_lookup_tables():
        try:
            profile = fetch_profile_from_table(client, table_name, mobile)
            if profile:
                return profile
        except Exception as exc:
            logger.error("Profile lookup failed in %s for %s: %s", table_name, mobile, exc)
    return None


def fetch_whatsapp_user(client: Client, mobile: str) -> dict | None:
    tables: list[str] = []
    for table_name in [WHATSAPP_USERS_TABLE, DEFAULT_WHATSAPP_USERS_TABLE]:
        if table_name and table_name not in tables:
            tables.append(table_name)

    for table_name in tables:
        try:
            profile = fetch_profile_from_table(client, table_name, mobile)
            if profile:
                return profile
        except Exception as exc:
            logger.error("WhatsApp user lookup failed in %s for %s: %s", table_name, mobile, exc)
    return None


def extract_default_address(user: dict | None) -> str:
    if not user:
        return ""

    plain_address = normalize_spaces(user.get("address", ""))
    if plain_address:
        return plain_address

    addresses = user.get("addresses")
    if isinstance(addresses, dict):
        default_block = addresses.get("default", {})
        if isinstance(default_block, dict):
            return normalize_spaces(default_block.get("address", ""))

    return ""


def merge_profile(existing_user: dict | None, updates: dict, mobile: str) -> dict:
    return {
        "name": normalize_name(updates.get("name") or existing_user.get("name", "") if existing_user else updates.get("name", "")),
        "email": normalize_email(updates.get("email") or existing_user.get("email", "") if existing_user else updates.get("email", "")),
        "mobile": mobile,
        "address": normalize_address(updates.get("address") or extract_default_address(existing_user)),
    }


def get_missing_order_fields(profile: dict) -> list[str]:
    missing_fields = []
    if not is_valid_name(profile.get("name", "")):
        missing_fields.append("name")
    if not profile.get("mobile"):
        missing_fields.append("mobile")
    if not is_valid_address(profile.get("address", "")):
        missing_fields.append("address")
    return missing_fields


def get_missing_customer_fields(profile: dict) -> list[str]:
    missing_fields = []
    if not is_valid_name(profile.get("name", "")):
        missing_fields.append("name")
    email = profile.get("email", "")
    if not email or not is_valid_email(email):
        missing_fields.append("email")
    if not is_valid_address(profile.get("address", "")):
        missing_fields.append("address")
    return missing_fields


def extract_mobile_from_text(message: str) -> str:
    match = re.search(
        r"(?:mobile|mobile no|mobile number|phone|phone no|phone number|contact)\s*[:.-]?\s*(\+?\d[\d\s-]{7,})",
        message,
        re.IGNORECASE,
    )
    if not match:
        return ""

    digits_only = re.sub(r"\D", "", match.group(1))
    if digits_only.startswith("91") and len(digits_only) > 10:
        digits_only = digits_only[-10:]
    return digits_only


def extract_profile_details(message: str) -> dict:
    extracted = {"name": "", "email": "", "address": "", "mobile": ""}
    text = message.strip()
    if not text:
        return extracted

    extracted["mobile"] = extract_mobile_from_text(text)

    email_match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", text, re.IGNORECASE)
    if email_match:
        extracted["email"] = normalize_email(email_match.group(1))

    name_match = re.search(r"(?im)^\s*(?:name)\s*:\s*(.+)$", text)
    if not name_match:
        name_match = re.search(r"(?im)\b(?:my name is|i am|i'm|im)\s+([A-Za-z][A-Za-z\s.'-]{1,})", text)
    if name_match:
        extracted["name"] = normalize_name(name_match.group(1).splitlines()[0])

    address_match = re.search(r"(?ims)^\s*(?:address)\s*:\s*(.+)$", text)
    if not address_match:
        address_match = re.search(r"(?im)^\s*(?:i live in|i am from|i'm from|from)\s+(.+)$", text)
    if address_match:
        extracted["address"] = normalize_address(address_match.group(1))

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if not extracted["name"]:
        for line in lines:
            lower_line = line.lower()
            if "@" in line or lower_line.startswith("address") or lower_line.startswith("email"):
                continue
            candidate = normalize_name(line)
            if is_valid_name(candidate):
                extracted["name"] = candidate
                break

    if not extracted["address"]:
        address_lines = []
        for line in lines:
            lower_line = line.lower()
            if lower_line.startswith("name") or lower_line.startswith("email"):
                continue
            if extracted["email"] and extracted["email"] in lower_line:
                continue
            if extracted["name"] and normalize_name(line) == extracted["name"]:
                continue
            address_lines.append(line)
        if address_lines:
            extracted["address"] = normalize_address("\n".join(address_lines))

    return extracted


def build_whatsapp_user_payload(profile: dict, existing_user: dict | None = None) -> dict:
    payload = {
        "mobile": profile["mobile"],
        "name": profile.get("name") or None,
        "email": profile.get("email") or None,
    }

    current_addresses = existing_user.get("addresses", {}) if existing_user else {}
    if not isinstance(current_addresses, dict):
        current_addresses = {}

    if profile.get("address"):
        current_addresses["default"] = {"address": profile["address"]}
        payload["addresses"] = current_addresses

    if "wishlist" in profile:
        payload["wishlist"] = profile["wishlist"]

    if "total_orders" in profile:
        payload["total_orders"] = profile["total_orders"]

    return payload


def build_legacy_user_payload(profile: dict) -> dict:
    payload = {
        "mobile": profile["mobile"],
        "name": profile.get("name") or None,
        "email": profile.get("email") or None,
    }
    if profile.get("address"):
        payload["address"] = profile["address"]
    return payload


def upsert_whatsapp_user(client: Client, profile: dict) -> dict | None:
    existing_user = fetch_whatsapp_user(client, profile["mobile"])
    payload = build_whatsapp_user_payload(profile, existing_user)
    target_table = (existing_user or {}).get("_source_table") or WHATSAPP_USERS_TABLE

    try:
        if existing_user:
            client.table(target_table).update(payload).eq("mobile", profile["mobile"]).execute()
        else:
            client.table(target_table).insert(payload).execute()
        return fetch_whatsapp_user(client, profile["mobile"])
    except Exception as exc:
        logger.error("Primary WhatsApp user upsert failed for %s: %s", profile["mobile"], exc)

    legacy_payload = build_legacy_user_payload(profile)
    try:
        if existing_user:
            client.table(target_table).update(legacy_payload).eq("mobile", profile["mobile"]).execute()
        else:
            client.table(target_table).insert(legacy_payload).execute()
        return fetch_whatsapp_user(client, profile["mobile"])
    except Exception as exc:
        logger.error("Legacy WhatsApp user upsert failed for %s: %s", profile["mobile"], exc)
        raise


def get_product_sizes(product: dict) -> list[str]:
    sizes = product.get("sizes", [])
    if isinstance(sizes, list):
        return [normalize_spaces(size) for size in sizes if normalize_spaces(size)]
    if isinstance(sizes, str):
        return [normalize_spaces(size) for size in sizes.split(",") if normalize_spaces(size)]
    return []


def get_stock_quantity(product: dict) -> int:
    try:
        return int(product.get("stock_quantity") or 0)
    except Exception:
        return 0


def is_in_stock(product: dict) -> bool:
    return get_stock_quantity(product) > 0


def format_products_as_context(products: list[dict]) -> str:
    lines = ["=== EDMUND LUNGIS PRODUCT CATALOG ==="]
    for index, product in enumerate(products, start=1):
        sizes = ", ".join(get_product_sizes(product)) or "Not specified"
        availability = "In Stock" if is_in_stock(product) else "Out of Stock"
        lines.append(
            "\n".join(
                [
                    f"PRODUCT {index}:",
                    f"  ID          : {product.get('id', 'N/A')}",
                    f"  Name        : {product.get('name', 'N/A')}",
                    f"  Category    : {product.get('category', 'N/A')}",
                    f"  Description : {product.get('description', 'N/A')}",
                    f"  Material    : {product.get('material', 'N/A')}",
                    f"  Color       : {product.get('color', 'N/A')}",
                    f"  Price       : Rs.{product.get('price', 'N/A')}",
                    f"  Sizes       : {sizes}",
                    f"  Stock       : {get_stock_quantity(product)}",
                    f"  Availability: {availability}",
                    f"  Image URL   : {product.get('image_url', 'none')}",
                ]
            )
        )
    return "\n\n".join(lines)


def get_conversation_session(mobile: str) -> dict:
    return customer_sessions.setdefault(
        mobile,
        {
            "last_products": [],
            "browse": {
                "search_query": "",
                "matched_product_ids": [],
                "shown_product_ids": [],
                "remaining_product_ids": [],
            },
            "order": None,
            "cart": {
                "awaiting_customer_info": False,
                "pending_item": None,
            },
        },
    )


def remember_products(mobile: str, ai_products: list[dict]) -> None:
    remembered: list[dict] = []
    for item in ai_products:
        product = None
        product_id = str(item.get("id", "") or "").strip()
        product_name = str(item.get("name", "") or "").strip()

        if product_id:
            product = next((candidate for candidate in products_list if str(candidate.get("id")) == product_id), None)
        if not product and product_name:
            product = find_product_by_name(product_name)
        if product:
            remembered.append(product)

    get_conversation_session(mobile)["last_products"] = remembered[:5]


def find_product_by_name(name: str, candidates: list[dict] | None = None) -> dict | None:
    if not name:
        return None

    normalized_query = normalize_lookup(name)
    search_pool = candidates or products_list

    exact_match = next(
        (product for product in search_pool if normalize_lookup(product.get("name", "")) == normalized_query),
        None,
    )
    if exact_match:
        return exact_match

    contains_match = next(
        (
            product
            for product in sorted(search_pool, key=lambda item: len(str(item.get("name", ""))), reverse=True)
            if normalize_lookup(product.get("name", "")) in normalized_query
            or normalized_query in normalize_lookup(product.get("name", ""))
        ),
        None,
    )
    return contains_match


def build_searchable_text(product: dict) -> str:
    parts = [
        product.get("name", ""),
        product.get("category", ""),
        product.get("description", ""),
        product.get("material", ""),
        product.get("color", ""),
        " ".join(get_product_sizes(product)),
    ]
    return normalize_lookup(" ".join(str(part or "") for part in parts))


def extract_budget_filters(message: str) -> tuple[int | None, int | None]:
    lowered = message.lower()

    under_match = re.search(r"\b(?:under|below|less than)\s+(\d+)\b", lowered)
    if under_match:
        return int(under_match.group(1)), None

    between_match = re.search(r"\bbetween\s+(\d+)\s+(?:and|to)\s+(\d+)\b", lowered)
    if between_match:
        first = int(between_match.group(1))
        second = int(between_match.group(2))
        return max(first, second), min(first, second)

    above_match = re.search(r"\b(?:above|over|more than)\s+(\d+)\b", lowered)
    if above_match:
        return None, int(above_match.group(1))

    return None, None


def extract_search_terms(message: str) -> list[str]:
    normalized = normalize_lookup(message)
    normalized = re.sub(r"\b(show|find|need|want|looking|search|for|please|me|some|collection|collections)\b", " ", normalized)
    normalized = re.sub(r"\b(more|next|continue|still)\b", " ", normalized)
    normalized = re.sub(r"\b(under|below|less|than|between|and|to|above|over)\s+\d+\b", " ", normalized)
    normalized = re.sub(r"\b\d+\b", " ", normalized)
    stop_words = {
        "the", "a", "an", "all", "any", "with", "without", "in", "on", "at",
        "from", "of", "my", "your", "our", "this", "that", "these", "those",
    }
    terms = [term for term in normalized.split() if len(term) > 1 and term not in stop_words]
    return terms


def is_simple_product_query(message: str) -> bool:
    normalized = normalize_lookup(message)
    if is_show_more_message(message):
        return True

    product_keywords = {
        "lungi", "lungis", "dhoti", "veshti", "vesti",
        "cotton", "silk", "wedding", "white", "black", "premium", "daily", "wear",
    }
    words = normalized.split()
    if not words:
        return False
    if len(words) <= 6 and any(word in product_keywords for word in words):
        return True
    return any(word in product_keywords for word in words) and any(prefix in normalized for prefix in ["show", "find", "under", "below"])


def product_matches_terms(product: dict, terms: list[str], max_price: int | None, min_price: int | None) -> bool:
    if not is_in_stock(product):
        return False

    price = product.get("price")
    try:
        numeric_price = float(price)
    except Exception:
        numeric_price = None

    if max_price is not None and numeric_price is not None and numeric_price > max_price:
        return False
    if min_price is not None and numeric_price is not None and numeric_price < min_price:
        return False

    searchable_text = build_searchable_text(product)
    synonym_map = {
        "veshti": {"veshti", "vesti", "dhoti"},
        "vesti": {"veshti", "vesti", "dhoti"},
        "dhoti": {"veshti", "vesti", "dhoti"},
        "lungi": {"lungi", "lungis"},
        "lungis": {"lungi", "lungis"},
    }

    for term in terms:
        valid_terms = synonym_map.get(term, {term})
        if not any(valid_term in searchable_text for valid_term in valid_terms):
            return False

    return True


def sort_products_for_browsing(products: list[dict]) -> list[dict]:
    return sorted(
        products,
        key=lambda product: (
            0 if is_in_stock(product) else 1,
            float(product.get("price") or 0),
            str(product.get("name", "")),
        ),
    )


def search_products_direct(query: str) -> list[dict]:
    max_price, min_price = extract_budget_filters(query)
    terms = extract_search_terms(query)

    matched_products = [
        product
        for product in products_list
        if product_matches_terms(product, terms, max_price, min_price)
    ]

    if matched_products:
        return sort_products_for_browsing(matched_products)

    return []


def build_browse_intro(query: str, count: int) -> str:
    if count <= 0:
        return "Product not available ❌"
    if count == 1:
        return f"Here is a matching product for *{query.strip()}* 😊"
    return f"Here are some options for *{query.strip()}* 😊"


def build_no_more_message(query: str) -> str:
    if query:
        return (
            "😔 We've shown all available products for this search.\n\n"
            "Would you like to explore cotton veshti or premium dhoti collections instead? 😊"
        )
    return "😔 Sorry, no more products are available in this category right now."


def reset_browse_session(mobile: str, query: str, matched_products: list[dict]) -> dict:
    session = get_conversation_session(mobile)
    session["browse"] = {
        "search_query": query,
        "matched_product_ids": [str(product.get("id")) for product in matched_products if product.get("id") is not None],
        "shown_product_ids": [],
        "remaining_product_ids": [str(product.get("id")) for product in matched_products if product.get("id") is not None],
    }
    return session["browse"]


def get_browse_session(mobile: str) -> dict:
    session = get_conversation_session(mobile)
    return session.setdefault(
        "browse",
        {
            "search_query": "",
            "matched_product_ids": [],
            "shown_product_ids": [],
            "remaining_product_ids": [],
        },
    )


def get_products_by_ids(product_ids: list[str]) -> list[dict]:
    id_map = {str(product.get("id")): product for product in products_list if product.get("id") is not None}
    return [id_map[product_id] for product_id in product_ids if product_id in id_map]


def get_next_browse_batch(mobile: str) -> tuple[list[dict], dict]:
    browse_session = get_browse_session(mobile)
    remaining_ids = browse_session.get("remaining_product_ids", [])
    next_ids = remaining_ids[:PRODUCTS_PAGE_SIZE]
    browse_session["remaining_product_ids"] = remaining_ids[PRODUCTS_PAGE_SIZE:]
    browse_session["shown_product_ids"] = browse_session.get("shown_product_ids", []) + next_ids
    return get_products_by_ids(next_ids), browse_session


def resolve_product_from_message(message: str, mobile: str) -> dict | None:
    session = get_conversation_session(mobile)
    recent_products = session.get("last_products", [])
    lowered = message.lower()

    if any(token in lowered for token in ["this", "that", "same one", "same product"]) and recent_products:
        return recent_products[-1]

    recent_match = find_product_by_name(message, recent_products)
    if recent_match:
        return recent_match

    return find_product_by_name(message)


def extract_quantity(message: str) -> int | None:
    patterns = [
        r"\bquantity\s*[:=-]?\s*(\d+)\b",
        r"\bqty\s*[:=-]?\s*(\d+)\b",
        r"\b(\d+)\s*(?:pieces|piece|pcs|pc)\b",
        r"\badd\s+(\d+)\b",
        r"^\s*(\d+)\s+[a-z]",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def match_size(product: dict, candidate_size: str) -> str | None:
    cleaned = normalize_lookup(candidate_size)
    for size in get_product_sizes(product):
        if normalize_lookup(size) == cleaned:
            return size
    return None


def extract_size(message: str, product: dict | None = None) -> str | None:
    if product:
        for size in get_product_sizes(product):
            if normalize_lookup(size) in normalize_lookup(message):
                return size

    explicit_match = re.search(r"\bsize\s*[:=-]?\s*([A-Za-z0-9 +_-]+)\b", message, re.IGNORECASE)
    if not explicit_match:
        return None

    candidate = normalize_spaces(explicit_match.group(1))
    if not product:
        return candidate

    return match_size(product, candidate)


def build_product_caption(product: dict) -> str:
    sizes = ", ".join(get_product_sizes(product)) or "Not specified"
    availability = "In Stock" if is_in_stock(product) else "Out of Stock"
    return "\n".join(
        [
            f"🛍️ *{product.get('name', 'Product')}*",
            f"💰 Price: Rs.{product.get('price', 'N/A')}",
            f"🎨 Color: {product.get('color', 'N/A')}",
            f"🧵 Material: {product.get('material', 'N/A')}",
            f"📏 Sizes: {sizes}",
            f"✅ Stock Status: {availability}",
        ]
    )


def build_cart_state() -> dict:
    return {
        "awaiting_customer_info": False,
        "pending_item": None,
    }


def get_cart_state(mobile: str) -> dict:
    session = get_conversation_session(mobile)
    cart_state = session.get("cart")
    if not isinstance(cart_state, dict):
        session["cart"] = build_cart_state()
    else:
        cart_state.setdefault("awaiting_customer_info", False)
        cart_state.setdefault("pending_item", None)
    return session["cart"]


def clear_cart_state(mobile: str) -> None:
    get_conversation_session(mobile)["cart"] = build_cart_state()


def parse_cart_item_entry(entry: str) -> dict | None:
    parts = [normalize_spaces(part) for part in str(entry or "").split("|")]
    if not parts or not parts[0]:
        return None

    quantity = 1
    size = ""

    if len(parts) >= 2 and not parts[1].lower().startswith("qty"):
        size = parts[1]
    if len(parts) >= 2 and parts[1].lower().startswith("qty"):
        quantity_match = re.search(r"(\d+)", parts[1])
        if quantity_match:
            quantity = int(quantity_match.group(1))
    if len(parts) >= 3:
        quantity_match = re.search(r"(\d+)", parts[2])
        if quantity_match:
            quantity = int(quantity_match.group(1))

    if normalize_lookup(size) in {"not specified", "no size", "default"}:
        size = ""

    return {
        "product_name": parts[0],
        "size": size,
        "quantity": quantity,
    }


def format_cart_item_entry(product_name: str, size: str, quantity: int) -> str:
    display_size = normalize_spaces(size) or "Not specified"
    return f"{normalize_spaces(product_name)} | {display_size} | Qty:{int(quantity)}"


def parse_cart_items(wishlist: list[str] | None) -> list[dict]:
    items: list[dict] = []
    for entry in wishlist or []:
        parsed = parse_cart_item_entry(entry)
        if parsed:
            items.append(parsed)
    return items


def serialize_cart_items(items: list[dict]) -> list[str]:
    return [
        format_cart_item_entry(item.get("product_name", ""), item.get("size", ""), int(item.get("quantity") or 1))
        for item in items
        if item.get("product_name")
    ]


def find_matching_cart_index(items: list[dict], product_name: str, size: str = "") -> int | None:
    normalized_product = normalize_lookup(product_name)
    normalized_size = normalize_lookup(size)
    for index, item in enumerate(items):
        if normalize_lookup(item.get("product_name", "")) != normalized_product:
            continue
        if normalized_size and normalize_lookup(item.get("size", "")) != normalized_size:
            continue
        if not normalized_size and normalize_lookup(item.get("size", "")) not in {"", "not specified"}:
            continue
        return index
    return None


def upsert_cart_item(items: list[dict], product_name: str, size: str, quantity: int) -> tuple[list[dict], dict]:
    normalized_size = normalize_spaces(size)
    item_index = find_matching_cart_index(items, product_name, normalized_size)
    if item_index is None:
        new_item = {
            "product_name": normalize_spaces(product_name),
            "size": normalized_size,
            "quantity": int(quantity),
        }
        items.append(new_item)
        return items, new_item

    items[item_index]["quantity"] = int(items[item_index].get("quantity") or 0) + int(quantity)
    return items, items[item_index]


def build_cart_item_summary(item: dict) -> str:
    return "\n".join(
        [
            "🛒 Cart Item:",
            f"• {item.get('product_name', 'Product')}",
            f"• Size: {item.get('size') or 'Not specified'}",
            f"• Quantity: {item.get('quantity')}",
        ]
    )


def build_cart_added_message(item: dict) -> str:
    return "\n".join(
        [
            "✅ Product added to your cart!",
            "",
            build_cart_item_summary(item),
            "",
            "You can continue shopping or type:",
            "• View cart",
            "• Checkout",
            "• Show more products",
        ]
    )


def build_cart_view_message(items: list[dict]) -> str:
    lines = ["🛒 Your Cart:", ""]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{index}. {item.get('product_name', 'Product')}",
                f"   Size: {item.get('size') or 'Not specified'}",
                f"   Qty: {item.get('quantity')}",
                "",
            ]
        )
    lines.extend(
        [
            "You can:",
            "• Remove item",
            "• Continue shopping",
            "• Checkout",
        ]
    )
    return "\n".join(lines).strip()


def build_checkout_success_message(items: list[dict]) -> str:
    lines = ["✅ Your order has been placed successfully!", "", "🧾 Order Summary:"]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{index}. {item.get('product_name', 'Product')}",
                f"   Size: {item.get('size') or 'Not specified'}",
                f"   Qty: {item.get('quantity')}",
            ]
        )
    lines.extend(["", "Thank you for shopping with Edmund Lungis 😊"])
    return "\n".join(lines)


def suggest_alternative_products(product: dict | None = None, query: str = "") -> list[dict]:
    candidates: list[dict] = []

    if product:
        for candidate in products_list:
            if str(candidate.get("id")) == str(product.get("id")):
                continue
            if not is_in_stock(candidate):
                continue
            same_category = normalize_lookup(candidate.get("category", "")) == normalize_lookup(product.get("category", ""))
            same_material = normalize_lookup(candidate.get("material", "")) == normalize_lookup(product.get("material", ""))
            same_color = normalize_lookup(candidate.get("color", "")) == normalize_lookup(product.get("color", ""))
            if same_category or same_material or same_color:
                candidates.append(candidate)
    elif query:
        candidates = search_products_direct(query)

    if not candidates:
        candidates = [candidate for candidate in sort_products_for_browsing(products_list) if is_in_stock(candidate)]

    return candidates[:3]


def build_unavailable_message(product_name: str, alternatives: list[dict]) -> str:
    lines = ["😔 Sorry, this product is currently unavailable."]
    if product_name:
        lines.append(f"Product: {product_name}")
    if alternatives:
        lines.extend(["", "You can try:", *[f"• {item.get('name', 'Product')}" for item in alternatives]])
    return "\n".join(lines)


def validate_cart_selection(selection: dict) -> dict:
    product = selection.get("product")
    if not product:
        return {"ok": False, "reason": "product_missing", "message": "Please mention the product name you want to add 😊"}

    if not is_in_stock(product):
        return {
            "ok": False,
            "reason": "out_of_stock",
            "message": build_unavailable_message(product.get("name", ""), suggest_alternative_products(product=product)),
        }

    sizes = get_product_sizes(product)
    selected_size = normalize_spaces(selection.get("size", ""))
    if sizes and len(sizes) == 1 and not selected_size:
        selected_size = sizes[0]

    if sizes and len(sizes) > 1:
        if not selected_size:
            return {
                "ok": False,
                "reason": "size_missing",
                "message": (
                    f"Please share the size for *{product.get('name', 'this product')}*.\n"
                    f"Available sizes: {', '.join(sizes)}"
                ),
            }
        matched_size = match_size(product, selected_size)
        if not matched_size:
            return {
                "ok": False,
                "reason": "size_unavailable",
                "message": (
                    f"😔 Sorry, that size is unavailable for *{product.get('name', 'this product')}*.\n"
                    f"Available sizes: {', '.join(sizes)}"
                ),
            }
        selected_size = matched_size
    elif selected_size:
        matched_size = match_size(product, selected_size)
        if matched_size:
            selected_size = matched_size

    quantity = int(selection.get("quantity") or 1)
    if quantity < 1:
        return {"ok": False, "reason": "quantity_invalid", "message": "Please share a valid quantity 😊"}

    available_stock = get_stock_quantity(product)
    if quantity > available_stock:
        return {
            "ok": False,
            "reason": "stock_shortage",
            "message": (
                f"😔 Sorry, only {available_stock} piece(s) are available for *{product.get('name', 'this product')}* right now."
            ),
        }

    return {
        "ok": True,
        "product": product,
        "size": selected_size,
        "quantity": quantity,
    }


def extract_cart_selection(message: str, mobile: str) -> dict:
    selection = {"product": None, "size": "", "quantity": None}
    apply_order_details_from_message(selection, message, mobile)
    if not selection.get("quantity"):
        selection["quantity"] = 1
    return selection


def validate_existing_cart_item(item: dict) -> dict:
    product = find_product_by_name(item.get("product_name", ""))
    if not product or not is_in_stock(product):
        return {
            "ok": False,
            "message": build_unavailable_message(item.get("product_name", ""), suggest_alternative_products(query=item.get("product_name", ""))),
        }

    size = item.get("size", "")
    sizes = get_product_sizes(product)
    if sizes:
        if not size and len(sizes) > 1:
            return {
                "ok": False,
                "message": (
                    f"Please update the size for *{item.get('product_name', 'this item')}* before checkout.\n"
                    f"Available sizes: {', '.join(sizes)}"
                ),
            }
        if size and not match_size(product, size):
            return {
                "ok": False,
                "message": (
                    f"😔 Sorry, the size selected for *{item.get('product_name', 'this item')}* is unavailable.\n"
                    f"Available sizes: {', '.join(sizes)}"
                ),
            }
        if not size and len(sizes) == 1:
            size = sizes[0]

    quantity = int(item.get("quantity") or 1)
    available_stock = get_stock_quantity(product)
    if quantity > available_stock:
        return {
            "ok": False,
            "message": (
                f"😔 Sorry, only {available_stock} piece(s) are available for *{item.get('product_name', 'this item')}* right now."
            ),
        }

    return {
        "ok": True,
        "product": product,
        "size": size,
        "quantity": quantity,
        "product_name": product.get("name", item.get("product_name", "")),
    }


def get_remove_target_index(items: list[dict], message: str, mobile: str) -> int | None:
    normalized = normalize_lookup(message)
    if len(items) == 1 and normalized in {"remove", "remove item", "delete item", "remove from cart"}:
        return 0

    product = resolve_product_from_message(message, mobile)
    selected_size = extract_size(message, product) if product else None
    if product:
        for index, item in enumerate(items):
            if normalize_lookup(item.get("product_name", "")) != normalize_lookup(product.get("name", "")):
                continue
            if selected_size and normalize_lookup(item.get("size", "")) != normalize_lookup(selected_size):
                continue
            return index

    candidate_query = re.sub(r"\b(remove|delete|item|from|cart)\b", " ", message, flags=re.IGNORECASE)
    candidate_query = normalize_lookup(candidate_query)
    if candidate_query:
        for index, item in enumerate(items):
            haystack = normalize_lookup(f"{item.get('product_name', '')} {item.get('size', '')}")
            if candidate_query in haystack or haystack in candidate_query:
                return index

    return None


async def add_item_to_cart(from_number: str, whatsapp_user: dict | None, validated_selection: dict) -> dict:
    client = get_supabase_client()
    current_user = whatsapp_user or fetch_whatsapp_user(client, from_number)
    merged_profile = merge_profile(current_user, {}, from_number)
    cart_items = parse_cart_items((current_user or {}).get("wishlist") or [])
    cart_items, saved_item = upsert_cart_item(
        cart_items,
        validated_selection["product"].get("name", ""),
        validated_selection.get("size", ""),
        int(validated_selection.get("quantity") or 1),
    )
    merged_profile["wishlist"] = serialize_cart_items(cart_items)
    if current_user and "total_orders" in current_user:
        merged_profile["total_orders"] = current_user.get("total_orders", 0)

    upsert_whatsapp_user(client, merged_profile)
    get_conversation_session(from_number)["last_products"] = [validated_selection["product"]]
    await send_text_message(from_number, build_cart_added_message(saved_item))
    return {"status": "cart_item_added"}


async def handle_customer_info_collection(from_number: str, user_text: str) -> dict:
    cart_state = get_cart_state(from_number)
    pending_item = cart_state.get("pending_item")
    client = get_supabase_client()
    existing_user = fetch_whatsapp_user(client, from_number)
    extracted_profile = extract_profile_details(user_text)
    merged_profile = merge_profile(existing_user, extracted_profile, from_number)
    missing_fields = get_missing_customer_fields(merged_profile)

    if missing_fields:
        await send_text_message(
            from_number,
            "😊 Please share your name, email, and address in one message.\n\n" + CUSTOMER_INFO_REQUEST_MESSAGE,
        )
        return {"status": "awaiting_customer_info"}

    if existing_user and "wishlist" in existing_user:
        merged_profile["wishlist"] = existing_user.get("wishlist") or []
    if existing_user and "total_orders" in existing_user:
        merged_profile["total_orders"] = existing_user.get("total_orders", 0)

    try:
        saved_user = upsert_whatsapp_user(client, merged_profile)
    except Exception as exc:
        logger.error("Customer profile save failed for %s: %s", from_number, exc)
        await send_text_message(from_number, PROFILE_SAVE_RETRY_MESSAGE)
        return {"status": "customer_profile_save_failed"}

    cart_state["awaiting_customer_info"] = False
    if pending_item:
        cart_state["pending_item"] = None
        return await add_item_to_cart(from_number, saved_user, pending_item)

    clear_cart_state(from_number)
    await send_text_message(from_number, "✅ Your details have been saved successfully 😊")
    return {"status": "customer_profile_saved"}


async def handle_add_to_cart(from_number: str, user_text: str) -> dict:
    selection = extract_cart_selection(user_text, from_number)
    validation = validate_cart_selection(selection)
    if not validation.get("ok"):
        await send_text_message(from_number, validation["message"])
        return {"status": validation.get("reason", "cart_validation_failed")}

    client = get_supabase_client()
    whatsapp_user = fetch_whatsapp_user(client, from_number)
    if not whatsapp_user:
        cart_state = get_cart_state(from_number)
        cart_state["awaiting_customer_info"] = True
        cart_state["pending_item"] = validation
        await send_text_message(from_number, CUSTOMER_INFO_REQUEST_MESSAGE)
        return {"status": "awaiting_customer_info"}

    try:
        return await add_item_to_cart(from_number, whatsapp_user, validation)
    except Exception as exc:
        logger.error("Cart update failed for %s: %s", from_number, exc)
        await send_text_message(from_number, "Sorry 😊 I couldn't update your cart right now. Please try again.")
        return {"status": "cart_update_failed"}


async def handle_view_cart(from_number: str) -> dict:
    client = get_supabase_client()
    whatsapp_user = fetch_whatsapp_user(client, from_number)
    cart_items = parse_cart_items((whatsapp_user or {}).get("wishlist") or [])

    if not cart_items:
        await send_text_message(from_number, CART_EMPTY_MESSAGE)
        return {"status": "cart_empty"}

    await send_text_message(from_number, build_cart_view_message(cart_items))
    return {"status": "cart_view_sent"}


async def handle_remove_from_cart(from_number: str, user_text: str) -> dict:
    client = get_supabase_client()
    whatsapp_user = fetch_whatsapp_user(client, from_number)
    cart_items = parse_cart_items((whatsapp_user or {}).get("wishlist") or [])

    if not cart_items:
        await send_text_message(from_number, CART_EMPTY_MESSAGE)
        return {"status": "cart_empty"}

    item_index = get_remove_target_index(cart_items, user_text, from_number)
    if item_index is None:
        await send_text_message(from_number, "Please mention the product you want to remove from your cart 😊")
        return {"status": "remove_target_missing"}

    cart_items.pop(item_index)
    merged_profile = merge_profile(whatsapp_user, {}, from_number)
    merged_profile["wishlist"] = serialize_cart_items(cart_items)
    if whatsapp_user and "total_orders" in whatsapp_user:
        merged_profile["total_orders"] = whatsapp_user.get("total_orders", 0)

    try:
        upsert_whatsapp_user(client, merged_profile)
        await send_text_message(from_number, "🗑️ Item removed from your cart successfully.")
        return {"status": "cart_item_removed"}
    except Exception as exc:
        logger.error("Cart remove failed for %s: %s", from_number, exc)
        await send_text_message(from_number, "Sorry 😊 I couldn't update your cart right now. Please try again.")
        return {"status": "cart_remove_failed"}


async def handle_checkout(from_number: str) -> dict:
    client = get_supabase_client()
    whatsapp_user = fetch_whatsapp_user(client, from_number)
    cart_items = parse_cart_items((whatsapp_user or {}).get("wishlist") or [])

    if not cart_items:
        await send_text_message(from_number, CART_EMPTY_MESSAGE)
        return {"status": "cart_empty"}

    validated_items = []
    for item in cart_items:
        validation = validate_existing_cart_item(item)
        if not validation.get("ok"):
            await send_text_message(from_number, validation["message"])
            return {"status": "checkout_validation_failed"}
        validated_items.append(validation)

    merged_profile = merge_profile(whatsapp_user, {}, from_number)
    current_total_orders = int((whatsapp_user or {}).get("total_orders") or 0)
    merged_profile["total_orders"] = current_total_orders + 1
    merged_profile["wishlist"] = []

    try:
        upsert_whatsapp_user(client, merged_profile)
        timestamp = utc_now_iso()
        for item in validated_items:
            order_payload = {
                "user_mobile": from_number,
                "customer_name": merged_profile.get("name"),
                "product_name": item.get("product_name"),
                "quantity": int(item.get("quantity") or 1),
                "size": item.get("size") or None,
                "address": merged_profile.get("address"),
                "order_status": "pending",
                "updated_at": timestamp,
            }
            client.table(WHATSAPP_ORDERS_TABLE).insert(order_payload).execute()

        clear_cart_state(from_number)
        await send_text_message(
            from_number,
            build_checkout_success_message(
                [
                    {
                        "product_name": item.get("product_name"),
                        "size": item.get("size"),
                        "quantity": item.get("quantity"),
                    }
                    for item in validated_items
                ]
            ),
        )
        return {"status": "checkout_completed"}
    except Exception as exc:
        logger.error("Checkout failed for %s: %s", from_number, exc)
        await send_text_message(from_number, "Sorry 😊 I couldn't update your cart right now. Please try again.")
        return {"status": "checkout_failed"}


def get_ai_response_json(user_message: str) -> dict:
    global groq_client, product_context

    if not groq_client or not product_context:
        return {
            "message": "Our collection is loading right now. Please try again shortly 😊",
            "products": [],
            "footer": "",
        }

    raw_response = ""
    for attempt in range(2):
        try:
            response = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"PRODUCT CATALOG:\n{product_context}\n\nCUSTOMER QUERY: {user_message}",
                    },
                ],
                max_tokens=1000,
                temperature=0.3,
            )
            raw_response = response.choices[0].message.content.strip()

            if raw_response.startswith("```"):
                raw_response = raw_response.split("```", 2)[1]
                if raw_response.startswith("json"):
                    raw_response = raw_response[4:]
            raw_response = raw_response.strip()

            parsed = json.loads(raw_response)
            parsed["products"] = parsed.get("products", [])[:5]
            return parsed
        except json.JSONDecodeError as exc:
            logger.error("JSON parse error on attempt %s: %s | raw=%s", attempt + 1, exc, raw_response)
        except Exception as exc:
            logger.error("Groq response error on attempt %s: %s", attempt + 1, exc)

    return {
        "message": FRIENDLY_REPHRASE_MESSAGE,
        "products": [],
        "footer": "",
    }


async def send_whatsapp_payload(payload: dict) -> None:
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if response.status_code != 200:
            logger.error("WhatsApp send failed: %s - %s", response.status_code, response.text)


async def send_text_message(to: str, message: str) -> None:
    await send_whatsapp_payload(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": message},
        }
    )


async def send_image_message(to: str, image_url: str, caption: str) -> None:
    await send_whatsapp_payload(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": caption,
            },
        }
    )


async def send_product_batch(
    to: str,
    products: list[dict],
    intro_message: str = "",
    footer_message: str = "",
) -> None:
    if intro_message:
        await send_text_message(to, intro_message)

    for product in products:
        caption = build_product_caption(product)
        image_url = str(product.get("image_url", "") or "").strip()
        if image_url.startswith("http"):
            await send_image_message(to, image_url, caption)
        else:
            await send_text_message(to, caption)

    if footer_message:
        await send_text_message(to, footer_message)

    if products:
        await send_text_message(to, ORDER_FOLLOW_UP_MESSAGE)


async def send_product_responses(to: str, ai_response: dict) -> None:
    intro_message = ai_response.get("message", "")
    products = ai_response.get("products", [])
    footer = ai_response.get("footer", "")

    remember_products(to, products)
    resolved_products: list[dict] = []
    for product_ref in products:
        product = None
        product_id = str(product_ref.get("id", "") or "").strip()
        if product_id:
            product = next((item for item in products_list if str(item.get("id")) == product_id), None)
        if not product:
            product = resolve_product_from_message(product_ref.get("name", ""), to)
        if not product:
            product = product_ref
        resolved_products.append(product)

    await send_product_batch(to, resolved_products, intro_message, footer)


async def handle_paginated_browse(to: str, user_text: str) -> bool:
    try:
        if is_show_more_message(user_text):
            browse_session = get_browse_session(to)
            if not browse_session.get("search_query"):
                await send_text_message(to, "Please tell me what you'd like to explore, like white dhoti or cotton lungi 😊")
                return True

            next_products, browse_session = get_next_browse_batch(to)
            if not next_products:
                await send_text_message(to, build_no_more_message(browse_session.get("search_query", "")))
                return True

            remember_products(
                to,
                [{"id": product.get("id"), "name": product.get("name")} for product in next_products],
            )
            await send_product_batch(
                to,
                next_products,
                f"Here are more options for *{browse_session.get('search_query', '').strip()}* 😊",
            )
            return True

        if not is_simple_product_query(user_text):
            return False

        matched_products = search_products_direct(user_text)
        if not matched_products:
            return False

        reset_browse_session(to, user_text, matched_products)
        next_products, _ = get_next_browse_batch(to)
        remember_products(
            to,
            [{"id": product.get("id"), "name": product.get("name")} for product in next_products],
        )
        await send_product_batch(to, next_products, build_browse_intro(user_text, len(matched_products)))
        return True
    except Exception as exc:
        logger.error("Direct browse failed for %s: %s", to, exc)
        await send_text_message(to, "Sorry 😊 I couldn't fetch products right now. Please try again.")
        return True


async def send_ai_product_response(to: str, user_text: str) -> None:
    ai_response = get_ai_response_json(user_text)
    logger.info("AI response: %s", ai_response)
    await send_product_responses(to, ai_response)


def build_order_state() -> dict:
    return {
        "product": None,
        "size": "",
        "quantity": None,
        "profile_updates": {},
        "awaiting": None,
        "profile_saved": False,
    }


def get_order_state(mobile: str) -> dict:
    session = get_conversation_session(mobile)
    if not session.get("order"):
        session["order"] = build_order_state()
    return session["order"]


def clear_order_state(mobile: str) -> None:
    get_conversation_session(mobile)["order"] = None


def apply_order_details_from_message(order_state: dict, message: str, mobile: str) -> None:
    product = resolve_product_from_message(message, mobile)
    if product:
        order_state["product"] = product

    if order_state.get("product"):
        size = extract_size(message, order_state["product"])
        if size:
            order_state["size"] = size

    quantity = extract_quantity(message)
    if quantity:
        order_state["quantity"] = quantity


def validate_order_selection(order_state: dict) -> str | None:
    product = order_state.get("product")
    if not product:
        return "Please send the product name you would like to order 😊"

    if not is_in_stock(product):
        return f"Sorry, *{product.get('name', 'this product')}* is currently out of stock ❌"

    sizes = get_product_sizes(product)
    if sizes and len(sizes) == 1 and not order_state.get("size"):
        order_state["size"] = sizes[0]

    if sizes and len(sizes) > 1:
        requested_size = order_state.get("size", "")
        if not requested_size:
            return (
                f"Please share the size needed for *{product.get('name', 'this product')}*.\n"
                f"Available sizes: {', '.join(sizes)}"
            )
        if not match_size(product, requested_size):
            return (
                f"That size is not available for *{product.get('name', 'this product')}*.\n"
                f"Available sizes: {', '.join(sizes)}"
            )
        order_state["size"] = match_size(product, requested_size)

    if not order_state.get("quantity"):
        return "Please share the quantity needed 😊"

    if int(order_state["quantity"]) < 1:
        return "Please share a valid quantity 😊"

    if int(order_state["quantity"]) > get_stock_quantity(product):
        return (
            f"Only {get_stock_quantity(product)} piece(s) are available for "
            f"*{product.get('name', 'this product')}* right now."
        )

    return None


def build_delivery_confirmation_message(profile: dict) -> str:
    return "\n".join(
        [
            "Please confirm your delivery details 😊",
            "",
            f"👤 Name: {profile.get('name', '')}",
            f"📍 Address: {profile.get('address', '')}",
            f"📞 Mobile: {profile.get('mobile', '')}",
            "",
            "Reply:",
            "• confirm",
            "OR",
            "• change address",
        ]
    )


def build_order_success_message(order_state: dict) -> str:
    product = order_state["product"]
    return "\n".join(
        [
            "🎉 Your order has been placed successfully!",
            "",
            "🧾 Order Summary:",
            f"• Product: {product.get('name', 'N/A')}",
            f"• Size: {order_state.get('size') or 'Not specified'}",
            f"• Quantity: {order_state.get('quantity')}",
            "",
            "Our team will contact you soon 😊",
        ]
    )


def prepare_wishlist_message(product_name: str, already_saved: bool) -> str:
    if already_saved:
        return f"*{product_name}* is already in your wishlist 😊"
    return f"*{product_name}* has been added to your wishlist ❤️"


async def save_wishlist_item(from_number: str, user: dict | None, user_text: str) -> dict:
    product = resolve_product_from_message(user_text, from_number)
    if not product:
        recent_products = get_conversation_session(from_number).get("last_products", [])
        if len(recent_products) == 1:
            product = recent_products[0]

    if not product:
        await send_text_message(from_number, "Please mention the product name you want to save 😊")
        return {"status": "wishlist_product_missing"}

    client = get_supabase_client()
    whatsapp_user = fetch_whatsapp_user(client, from_number)
    merged_profile = merge_profile(user, {}, from_number)
    wishlist = list((whatsapp_user or {}).get("wishlist") or [])
    product_name = str(product.get("name", "") or "").strip()

    already_saved = product_name in wishlist
    if not already_saved:
        wishlist.append(product_name)

    merged_profile["wishlist"] = wishlist
    if whatsapp_user and "total_orders" in whatsapp_user:
        merged_profile["total_orders"] = whatsapp_user.get("total_orders", 0)

    try:
        upsert_whatsapp_user(client, merged_profile)
        await send_text_message(from_number, prepare_wishlist_message(product_name, already_saved))
        return {"status": "wishlist_saved"}
    except Exception:
        await send_text_message(from_number, PROFILE_SAVE_RETRY_MESSAGE)
        return {"status": "wishlist_save_failed"}


async def finalize_order(from_number: str, user: dict | None, order_state: dict) -> dict:
    client = get_supabase_client()
    merged_profile = merge_profile(user, order_state.get("profile_updates", {}), from_number)

    try:
        whatsapp_user = fetch_whatsapp_user(client, from_number)
        current_total_orders = int((whatsapp_user or {}).get("total_orders") or 0)
        merged_profile["total_orders"] = current_total_orders + 1
        saved_user = upsert_whatsapp_user(client, merged_profile)

        order_payload = {
            "user_mobile": from_number,
            "customer_name": merged_profile.get("name"),
            "product_name": order_state["product"].get("name"),
            "quantity": int(order_state["quantity"]),
            "size": order_state.get("size") or None,
            "address": merged_profile.get("address"),
            "order_status": "pending",
            "updated_at": utc_now_iso(),
        }
        client.table(WHATSAPP_ORDERS_TABLE).insert(order_payload).execute()

        clear_order_state(from_number)
        if saved_user:
            get_conversation_session(from_number)["last_products"] = [order_state["product"]]

        await send_text_message(from_number, build_order_success_message(order_state))
        return {"status": "order_created"}
    except Exception as exc:
        logger.error("Order creation failed for %s: %s", from_number, exc)
        await send_text_message(from_number, FRIENDLY_ERROR_MESSAGE)
        return {"status": "order_creation_failed"}


def get_customer_mobile_for_display(from_number: str, extracted_mobile: str = "") -> str:
    if extracted_mobile:
        return extracted_mobile

    digits_only = re.sub(r"\D", "", from_number or "")
    if digits_only.startswith("91") and len(digits_only) > 10:
        return digits_only[-10:]
    return digits_only or from_number


async def save_profile_before_confirmation(from_number: str, user: dict | None, order_state: dict) -> bool:
    client = get_supabase_client()
    merged_profile = merge_profile(user, order_state.get("profile_updates", {}), from_number)

    try:
        upsert_whatsapp_user(client, merged_profile)
        order_state["profile_saved"] = True
        return True
    except Exception as exc:
        logger.error("Profile pre-save failed for %s: %s", from_number, exc)
        return False


async def handle_order_flow(from_number: str, user: dict | None, user_text: str) -> dict:
    order_state = get_order_state(from_number)

    extracted_profile = extract_profile_details(user_text)
    for field in ["name", "email", "address", "mobile"]:
        if extracted_profile.get(field):
            order_state["profile_updates"][field] = extracted_profile[field]

    if order_state.get("awaiting") == "confirm_details":
        if is_confirmation_message(user_text):
            return await finalize_order(from_number, user, order_state)

        if is_change_address_message(user_text):
            order_state["awaiting"] = "address"
            await send_text_message(from_number, "Please share your delivery address 🚚")
            return {"status": "awaiting_address"}

        if extracted_profile.get("address"):
            order_state["awaiting"] = None
        else:
            await send_text_message(from_number, "Please reply with `confirm` or `change address` 😊")
            return {"status": "awaiting_confirmation"}

    apply_order_details_from_message(order_state, user_text, from_number)

    selection_error = validate_order_selection(order_state)
    if selection_error:
        await send_text_message(from_number, selection_error)
        return {"status": "awaiting_order_details"}

    merged_profile = merge_profile(user, order_state.get("profile_updates", {}), from_number)
    missing_fields = get_missing_order_fields(merged_profile)

    if missing_fields:
        order_state["profile_saved"] = False
        order_state["awaiting"] = missing_fields[0]
        if missing_fields[0] == "name":
            await send_text_message(from_number, "Please share your name 😊")
        elif missing_fields[0] == "address":
            await send_text_message(from_number, "Please share your delivery address 🚚")
        else:
            await send_text_message(from_number, "Please share your details 😊")
        return {"status": f"awaiting_{missing_fields[0]}"}

    if not order_state.get("profile_saved"):
        saved = await save_profile_before_confirmation(from_number, user, order_state)
        if not saved:
            await send_text_message(from_number, PROFILE_SAVE_RETRY_MESSAGE)
            return {"status": "profile_presave_failed"}

    merged_profile["mobile"] = get_customer_mobile_for_display(
        from_number,
        order_state.get("profile_updates", {}).get("mobile", ""),
    )
    order_state["awaiting"] = "confirm_details"
    await send_text_message(from_number, build_delivery_confirmation_message(merged_profile))
    return {"status": "awaiting_confirmation"}


async def send_order_status_notification(order: dict, new_status: str) -> None:
    message = ORDER_STATUS_MESSAGES.get(new_status)
    if message:
        await send_text_message(order["user_mobile"], message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global product_context, products_list, groq_client

    logger.info("Starting Edmund Lungis WhatsApp Assistant")

    if GROQ_API_KEY:
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client initialized")

    try:
        client = get_supabase_client()
        products_list = fetch_active_products(client)
        product_context = format_products_as_context(products_list)
        logger.info("Loaded %s active products", len(products_list))
    except Exception as exc:
        logger.error("Startup error: %s", exc)

    yield
    logger.info("Shutting down")


app = FastAPI(
    title="Edmund Lungis WhatsApp AI Assistant",
    description="WhatsApp commerce concierge for traditional Indian wear",
    version="5.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "Edmund Lungis WhatsApp AI Assistant",
        "version": "5.0.0",
        "products": len(products_list),
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "products_loaded": bool(products_list),
        "groq_ready": groq_client is not None,
    }


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
        logger.info("Incoming webhook: %s", body)

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

        message = messages[0]
        from_number = message.get("from")
        message_type = message.get("type")

        if message_type != "text":
            await send_text_message(
                from_number,
                "Hi! 👋 I can only understand text messages.\nPlease type your query about our traditional wear 🛍️",
            )
            return {"status": "non_text_ignored"}

        user_text = message["text"]["body"].strip()
        logger.info("From %s: %s", from_number, user_text)

        if is_greeting_message(user_text):
            await send_text_message(from_number, GREETING_MESSAGE)
            return {"status": "greeting_sent"}

        cart_state = get_cart_state(from_number)
        if cart_state.get("awaiting_customer_info"):
            return await handle_customer_info_collection(from_number, user_text)

        if is_view_cart_message(user_text):
            return await handle_view_cart(from_number)

        if is_remove_from_cart_message(user_text):
            return await handle_remove_from_cart(from_number, user_text)

        if is_add_to_cart_intent_message(user_text, from_number) or is_wishlist_intent_message(user_text):
            return await handle_add_to_cart(from_number, user_text)

        if is_checkout_message(user_text):
            return await handle_checkout(from_number)

        browse_handled = await handle_paginated_browse(from_number, user_text)
        if browse_handled:
            return {"status": "browse_response_sent"}

        await send_ai_product_response(from_number, user_text)
        return {"status": "product_response_sent"}

    except Exception as exc:
        logger.error("Webhook error: %s", exc)
        if "from_number" in locals() and from_number:
            await send_text_message(from_number, FRIENDLY_ERROR_MESSAGE)
        return {"status": "error"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
