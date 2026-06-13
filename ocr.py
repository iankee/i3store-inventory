"""OCR module for reading e-commerce shipping labels.

Uses AI vision API (OpenRouter) for accurate reading of labels,
with tesseract as local fallback.
"""

import base64
import json
import logging
import os
import re
from typing import Optional

import pytesseract
import requests
from PIL import Image, ImageEnhance, ImageFilter

logger = logging.getLogger("inv-ocr")

# ── OpenRouter Vision API ────────────────────────────────────────

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "google/gemma-3-27b-it"  # cheap vision model: $0.10/$0.30 per M tokens

OCR_PROMPT = """Extract these fields from this e-commerce shipping label image:

1. platform: Which platform? (shopee / tokopedia / tiktok / lazada / bukalapak / blibli / unknown)
2. product_name: The product name exactly as written on the label
3. quantity: The quantity number (just the number, e.g. 1, 2, 3)

Reply ONLY with a JSON object, no other text:
{"platform": "...", "product_name": "...", "quantity": N}"""


def _image_to_base64(image_path: str) -> str:
    """Convert image to base64 data URL."""
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")

    # Determine MIME type
    ext = os.path.splitext(image_path)[1].lower()
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
    return f"data:image/{mime};base64,{data}"


def parse_label_ai(image_path: str) -> dict:
    """Use AI vision model to parse shipping label. Much more accurate than tesseract."""
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set, skipping AI OCR")
        return {"platform": None, "product_hint": None, "quantity": None, "raw_text": ""}

    try:
        data_url = _image_to_base64(image_path)

        payload = {
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 200,
            "temperature": 0,
        }

        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://inv.sricreate.com",
                "X-Title": "Sricreate Inventory",
            },
            json=payload,
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(f"OpenRouter API error: {resp.status_code} {resp.text[:300]}")
            return {"platform": None, "product_hint": None, "quantity": None, "raw_text": ""}

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        logger.info(f"AI OCR response: {content[:300]}")

        # Parse JSON from response
        # Handle potential markdown code blocks
        content = re.sub(r"```(?:json)?\s*", "", content).strip()
        parsed = json.loads(content)

        return {
            "platform": parsed.get("platform", "").lower(),
            "product_hint": parsed.get("product_name", "").strip(),
            "quantity": int(parsed.get("quantity", 0)) or None,
            "raw_text": content,
        }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI OCR response: {e}")
        return {"platform": None, "product_hint": None, "quantity": None, "raw_text": ""}
    except Exception as e:
        logger.error(f"AI OCR error: {e}")
        return {"platform": None, "product_hint": None, "quantity": None, "raw_text": ""}

# ── Image preprocessing ──────────────────────────────────────────

def preprocess(image_path: str) -> Image.Image:
    """Enhance image for better OCR: grayscale, contrast, sharpen, resize if too small."""
    img = Image.open(image_path)

    # Convert to RGB if RGBA
    if img.mode == "RGBA":
        img = img.convert("RGB")

    # Resize if too small (min 1000px wide)
    w, h = img.size
    if w < 1000:
        ratio = 1000 / w
        img = img.resize((1000, int(h * ratio)), Image.LANCZOS)

    # Grayscale
    img = img.convert("L")

    # Increase contrast
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)

    # Sharpen
    img = img.filter(ImageFilter.SHARPEN)

    return img


def extract_text(image_path: str, lang: str = "ind+eng") -> str:
    """Extract text from image using Tesseract OCR."""
    try:
        img = preprocess(image_path)
        text = pytesseract.image_to_string(img, lang=lang, config="--psm 6")
        return text.strip()
    except Exception as e:
        logger.error(f"OCR failed: {e}")
        return ""


# ── Platform detection ───────────────────────────────────────────

PLATFORM_PATTERNS = [
    (r"(?i)\bshopee\b", "shopee"),
    (r"(?i)\bspx\b", "shopee"),
    (r"(?i)shopee\s*xpress", "shopee"),
    (r"(?i)shopee\s*food", "shopee"),
    (r"(?i)\btokopedia\b", "tokopedia"),
    (r"(?i)\btokped\b", "tokopedia"),
    (r"(?i)tiktok\s*shop", "tiktok"),
    (r"(?i)\btiktok\b", "tiktok"),
    (r"(?i)\blazada\b", "lazada"),
    (r"(?i)\bbukalapak\b", "bukalapak"),
    (r"(?i)\bblibli\b", "blibli"),
]


def detect_platform(text: str) -> Optional[str]:
    """Detect e-commerce platform from OCR text."""
    for pattern, platform in PLATFORM_PATTERNS:
        if re.search(pattern, text):
            return platform
    return None


# ── Quantity extraction ──────────────────────────────────────────

QTY_PATTERNS = [
    # Common OCR errors in quantity labels
    # Qty → Oty/0ty/aty/Qtv/Jumtah/Juml4h
    r"(?i)(?:[qoa0][\s]*t[vwy]|qty|jumlah|jumtah|juml[4a]h|kuantitas)\s*[:=]?\s*(\d+)",
    r"(?i)(\d+)\s*(?:pcs|item|buah|unit|box|pack|barang|pes)\b",
    r"(?i)x\s*(\d+)",
    # Standalone digit that might be quantity (1-999)
    r"(?<!\d)(\d{1,3})(?!\d)",
]


def extract_quantity(text: str) -> Optional[int]:
    """Extract quantity from OCR text."""
    # Phase 1: Labeled patterns (high confidence)
    labeled_patterns = [
        r"(?i)(?:[qoa0][\s]*t[vwy]|qty|jumlah|jumtah|juml[4a]h|kuantitas)\s*[:=]?\s*(\d+)",
    ]
    for pattern in labeled_patterns:
        m = re.search(pattern, text)
        if m:
            qty = int(m.group(1))
            if 1 <= qty <= 10000:
                return qty

    # Phase 2: Context patterns
    context_patterns = [
        r"(?i)(\d+)\s*(?:pcs|item|buah|unit|box|pack|barang|pes)\b",
        r"(?i)x\s*(\d+)",
    ]
    for pattern in context_patterns:
        m = re.search(pattern, text)
        if m:
            qty = int(m.group(1))
            if 1 <= qty <= 10000:
                return qty

    # Phase 3: Fallback — look for lone 1-3 digit number
    # Filter out: years (202x, 19xx), phone numbers, prices (with .000)
    all_nums = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text)
    for num_str in all_nums:
        n = int(num_str)
        if 1 <= n <= 999 and not (1900 <= n <= 2099) and n != 62:
            return n

    return None


# ── Product name extraction ──────────────────────────────────────

# Known noise words from shipping labels to filter out
NOISE_WORDS = {
    "pengirim", "penerima", "alamat", "no", "telp", "telepon",
    "berat", "kg", "gram", "gr", "cod", "non", "regular",
    "same", "day", "instant", "express", "standard", "hemat",
    "asuransi", "pengiriman", "ekspedisi", "kurir", "resi",
    "invoice", "order", "total", "bayar", "harga", "rp",
    "dari", "ke", "dikirim", "tanggal", "waktu", "jam",
    "note", "catatan", "packing", "kayu", "bubble", "wrap",
    "jne", "jnt", "sicepat", "anteraja", "ninja", "pos",
    "spx", "gosend", "grab", "shopee", "tokopedia",
}


def extract_product_hint(text: str) -> Optional[str]:
    """Try to find the product name from OCR text.

    Strategy: look for the longest non-noise line that looks like a product name.
    """
    lines = text.split("\n")
    candidates = []

    for line in lines:
        line = line.strip()
        if len(line) < 4 or len(line) > 100:
            continue

        # Skip lines that are mostly numbers or noise
        words = set(line.lower().split())
        if words & NOISE_WORDS:
            continue

        # Skip lines that look like addresses or phone numbers
        if re.search(r"\d{3}[-.\s]?\d{3}[-.\s]?\d{4}", line):  # phone
            continue
        if re.search(r"(?i)(rt|rw|desa|kel|kec|kab|prov|kode\s*pos|jalan|jl\.)", line):
            continue

        # Prefer lines with product-like patterns
        score = len(line)
        # Bonus for lines after "Barang" or "Produk" or "Item"
        candidates.append((score, line))

    if not candidates:
        return None

    # Return the longest candidate
    candidates.sort(key=lambda x: x[0], reverse=True)
    hint = candidates[0][1].strip()

    # Clean up common prefixes (handle OCR losing colons)
    for prefix in ["barang", "produk", "item", "nama barang", "nama produk"]:
        if hint.lower().startswith(prefix):
            # Remove prefix + optional colon/space
            hint = hint[len(prefix):].lstrip(": ").strip()

    return hint


# ── Main parse function ──────────────────────────────────────────

def parse_label(image_path: str) -> dict:
    """Parse shipping label / order screenshot.

    Uses AI vision API first (accurate), falls back to tesseract (free).

    Returns:
        {
            "platform": "shopee" | "tokopedia" | None,
            "product_hint": "Kaos Polos Hitam" | None,
            "quantity": 1 | None,
            "raw_text": "full OCR output",
        }
    """
    # Try AI vision first
    result = parse_label_ai(image_path)

    if result["platform"] or result["product_hint"]:
        # AI gave us something — use it
        logger.info(f"AI OCR: platform={result['platform']}, hint={result['product_hint']}, qty={result['quantity']}")
        return result

    # Fallback to tesseract
    logger.info("AI OCR failed, falling back to tesseract...")
    text = extract_text(image_path)
    if not text:
        return {"platform": None, "product_hint": None, "quantity": None, "raw_text": ""}

    platform = detect_platform(text)
    quantity = extract_quantity(text)
    product_hint = extract_product_hint(text)

    logger.info(
        f"Tesseract result — platform={platform}, hint={product_hint}, qty={quantity}"
    )

    return {
        "platform": platform,
        "product_hint": product_hint,
        "quantity": quantity,
        "raw_text": text,
    }
