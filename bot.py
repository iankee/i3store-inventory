"""Sricreate Inventory Telegram Bot.

Smart bot with AI-powered fuzzy matching for product names.
Listens for messages and intelligently adds/removes stock.
Supports OCR-based outgoing flow for e-commerce shipping labels.
"""

import multiprocessing
try:
    multiprocessing.set_start_method("spawn")
except RuntimeError:
    pass  # already set

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from difflib import get_close_matches

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, UPLOAD_FOLDER
from models import (
    MovementSource,
    MovementType,
    Product,
    SessionLocal,
    StockMovement,
)
from ocr import parse_label

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inv-bot")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Fuzzy matching ───────────────────────────────────────────────

# Common variant words (colors, sizes, types) in Indonesian
VARIANT_WORDS = {
    "hitam", "putih", "merah", "biru", "hijau", "kuning", "ungu", "orange", "oranye",
    "pink", "abu", "abu-abu", "silver", "gold", "emas", "coklat", "cokelat", "cream",
    "navy", "tosca", "maroon", "beige", "mocca", "grafit", "graphite",
    "s", "m", "l", "xl", "xxl", "xxxl", "4xl", "5xl",
    "small", "medium", "large", "xlarge",
    "pro", "lite", "ultra", "max", "plus", "mini", "normal", "regular",
    "4/128", "6/128", "8/128", "8/256", "12/256", "12/512",
    "128gb", "256gb", "512gb", "1tb", "64gb",
}


def extract_variants(text: str) -> set[str]:
    """Extract variant words (color, size, spec) from text."""
    words = set(text.lower().split())
    return words & VARIANT_WORDS


def has_variant_conflict(query: str, product_name: str) -> bool:
    """Check if query has variant words that differ from product name.
    Returns True if user is likely talking about a different variant."""
    q_variants = extract_variants(query)
    p_variants = extract_variants(product_name)

    if not q_variants:
        return False  # no variant specified, safe to match

    # If query has variants that the product doesn't have (or has different ones),
    # this might be a different variant
    if q_variants - p_variants:
        return True  # user specified a variant not in the product

    return False

def find_product_fuzzy(name: str, threshold: float = 0.4) -> list[Product]:
    """Find products with fuzzy name matching. Returns sorted by relevance."""
    db = SessionLocal()
    try:
        all_products = db.query(Product).filter(Product.is_active == 1).all()

        candidates = [(p, p.name.lower()) for p in all_products]
        names = [c[1] for c in candidates]

        query = name.lower().strip()

        # Try exact match first
        for p, pname in candidates:
            if pname == query:
                return [(p, 1.0)]

        # Try substring match
        substring_matches = []
        for p, pname in candidates:
            if query in pname or pname in query:
                substring_matches.append((p, 0.85))

        if substring_matches:
            return substring_matches

        # Try difflib fuzzy match
        matches = get_close_matches(query, names, n=5, cutoff=threshold)
        results = []
        for m in matches:
            idx = names.index(m)
            score = _similarity(query, m)
            results.append((candidates[idx][0], score))

        return sorted(results, key=lambda x: x[1], reverse=True)
    finally:
        db.close()


def _similarity(a: str, b: str) -> float:
    """Simple token-based similarity score."""
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    union = a_tokens | b_tokens
    return len(intersection) / len(union)


# ── Smart parser ─────────────────────────────────────────────────

def parse_smart(text: str) -> tuple[str | None, int | None]:
    """
    Parse natural language into (product_hint, quantity).
    Returns (None, None) if nothing useful found.
    """
    text = text.strip()
    if not text:
        return None, None

    # Strip prefixes
    for prefix in ["tambah ", "tambah:", "add ", "add:", "masuk ", "stock in ", "+"]:
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()

    # Strip quantity suffixes
    text = re.sub(r'\b(\d+)\s*(pcs|item|buah|unit|box|karton|biji|pack|dus)\b', r'\1', text, flags=re.IGNORECASE)

    # Try to extract quantity
    qty = None
    name_hint = None

    # Pattern: "Nama = 25" or "Nama: 25"
    m = re.match(r'^(.+?)\s*[=:]\s*(\d+)\s*$', text)
    if m:
        return m.group(1).strip(), int(m.group(2))

    # Pattern: "25 Nama" — number at start
    m = re.match(r'^(\d+)\s+(.+)$', text)
    if m:
        return m.group(2).strip(), int(m.group(1))

    # Pattern: "Nama 25" — number at end
    m = re.match(r'^(.+)\s+(\d+)\s*$', text)
    if m:
        return m.group(1).strip(), int(m.group(2))

    # Pattern: just a number — quantity only, no name
    m = re.match(r'^\s*(\d+)\s*$', text)
    if m:
        return None, int(m.group(1))

    # No quantity found — treat entire text as product name hint
    if len(text) > 1:
        return text, None

    return None, None


# ── Helpers ──────────────────────────────────────────────────────

async def save_photo(photo_file, filename: str) -> str | None:
    try:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        await photo_file.download_to_drive(filepath)
        return filename
    except Exception as e:
        logger.error(f"Failed to save photo: {e}")
        return None


def compute_file_hash(filepath: str) -> str | None:
    """Compute SHA256 hash of a file for deduplication."""
    try:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()
    except Exception as e:
        logger.error(f"Failed to hash file: {e}")
        return None


def is_duplicate_photo(file_hash: str) -> tuple[bool, str | None]:
    """Check if a photo hash already exists (exact match or pipe-separated).
    Returns (is_duplicate, product_name)."""
    if not file_hash:
        return False, None
    db = SessionLocal()
    try:
        existing = db.query(StockMovement).filter(
            (StockMovement.photo_hash == file_hash) |
            (StockMovement.photo_hash.like(f'{file_hash}|%')) |
            (StockMovement.photo_hash.like(f'%|{file_hash}')) |
            (StockMovement.photo_hash.like(f'%|{file_hash}|%'))
        ).first()
        if existing:
            return True, existing.product.name if existing.product else "?"
        return False, None
    finally:
        db.close()


async def _save_telegram_photo(msg, product_name: str) -> str | None:
    """Save the largest photo from a Telegram message to uploads/."""
    try:
        photo_file = await msg.photo[-1].get_file()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", product_name)[:30]
        filename = f"tg_{ts}_{safe_name}.jpg"
        return await save_photo(photo_file, filename)
    except Exception as e:
        logger.error(f"Failed to save Telegram photo: {e}")
        return None


async def _save_telegram_media(msg, product_name: str) -> str | None:
    """Save photo or video from a Telegram message. Returns filename or None."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", product_name)[:30]

        if msg.photo:
            photo_file = await msg.photo[-1].get_file()
            filename = f"tg_{ts}_{safe_name}.jpg"
            return await save_photo(photo_file, filename)
        elif msg.video:
            video_file = await msg.video.get_file()
            filename = f"tg_{ts}_{safe_name}.mp4"
            return await save_photo(video_file, filename)
        else:
            return None
    except Exception as e:
        logger.error(f"Failed to save media: {e}")
        return None


async def save_and_dedup_media(msg, product_name: str) -> tuple[str | None, str | None, str | None]:
    """Save media, check for duplicates. Returns (filename, hash, duplicate_product_name)."""
    filename = await _save_telegram_media(msg, product_name)
    if not filename:
        return None, None, None

    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file_hash = compute_file_hash(filepath)
    if file_hash:
        is_dup, dup_product = is_duplicate_photo(file_hash)
        if is_dup:
            # Delete the duplicate file
            try:
                os.remove(filepath)
            except Exception:
                pass
            return filename, file_hash, dup_product

    return filename, file_hash, None


def do_stock_in(product: Product, quantity: int, notes: str, photo_path: str | None = None, photo_hash: str | None = None):
    """Execute stock-in transaction. Returns (product, before, after, is_new, movement_id)."""
    db = SessionLocal()
    try:
        existing = db.query(Product).filter(Product.id == product.id).first()
        is_new = False
        if not existing:
            existing = Product(name=product.name, current_stock=0)
            db.add(existing)
            db.flush()
            is_new = True

        before = existing.current_stock
        existing.current_stock += quantity

        movement = StockMovement(
            product_id=existing.id,
            type=MovementType.STOCK_IN,
            source=MovementSource.TELEGRAM,
            quantity=quantity,
            stock_before=before,
            stock_after=existing.current_stock,
            notes=notes,
            photo_path=photo_path,
            photo_hash=photo_hash,
        )
        db.add(movement)
        db.commit()
        return existing, before, existing.current_stock, is_new, movement.id
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def do_stock_out(product: Product, quantity: int, source: MovementSource, notes: str, photo_path: str | None = None, photo_hash: str | None = None):
    """Execute stock-out transaction. Returns (product, before, after, movement_id)."""
    db = SessionLocal()
    try:
        existing = db.query(Product).filter(Product.id == product.id).first()
        if not existing:
            return None, 0, 0, None

        before = existing.current_stock
        if before < quantity:
            return None, before, before, None  # insufficient stock

        existing.current_stock -= quantity

        movement = StockMovement(
            product_id=existing.id,
            type=MovementType.STOCK_OUT,
            source=source,
            quantity=-quantity,
            stock_before=before,
            stock_after=existing.current_stock,
            notes=notes,
            photo_path=photo_path,
            photo_hash=photo_hash,
        )
        db.add(movement)
        db.commit()
        return existing, before, existing.current_stock, movement.id
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()


def _append_photo_to_movement(movement_id: int, photo_path: str):
    """Append an additional photo path + hash to an existing stock movement.
    Saves hash so subsequent dedup checks catch reversed-order uploads."""
    db = SessionLocal()
    try:
        movement = db.query(StockMovement).filter(StockMovement.id == movement_id).first()
        if movement:
            # Append photo path
            existing = movement.photo_path or ""
            movement.photo_path = f"{existing}|{photo_path}" if existing else photo_path

            # Compute and append hash for dedup
            full_path = os.path.join(UPLOAD_FOLDER, photo_path)
            file_hash = compute_file_hash(full_path)
            if file_hash:
                existing_hash = movement.photo_hash or ""
                movement.photo_hash = f"{existing_hash}|{file_hash}" if existing_hash else file_hash

            db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to append photo: {e}")
    finally:
        db.close()


# ── Source name mapping ──────────────────────────────────────────

SOURCE_NAMES = {
    "shopee": "🛒 Shopee",
    "tokopedia": "🛍️ Tokopedia",
    "tiktok": "🎵 TikTok Shop",
    "lazada": "📦 Lazada",
    "bukalapak": "🏪 Bukalapak",
    "blibli": "🔵 Blibli",
}


# ── Outgoing OCR handler ─────────────────────────────────────────

async def _handle_outgoing(msg, caption: str):
    """Handle outgoing stock flow: OCR first photo → deduct stock. Other media appended as evidence."""
    has_media = msg.photo or msg.video
    if not has_media:
        await msg.reply_text("❌ Kirim *foto label pengiriman* untuk barang keluar.\n\nContoh: foto label Shopee/Tokopedia + caption \"keluar\"", parse_mode="Markdown")
        return

    media_group_id = msg.media_group_id
    is_first_in_group = bool(caption and caption.strip())

    # If part of a media group but NOT the first (no caption), skip — 
    # it'll be handled when the first message triggers the group processing
    if media_group_id and not is_first_in_group:
        return

    # Status message
    status_msg = await msg.reply_text("🔍 *Menganalisa label pengiriman...*", parse_mode="Markdown")

    # Save first media with dedup check
    first_media, media_hash, dup_product = await save_and_dedup_media(msg, "outgoing")
    if not first_media:
        await status_msg.edit_text("❌ Gagal menyimpan media.")
        return

    if dup_product:
        await status_msg.edit_text(
            f"⚠️ *Foto sudah pernah dipakai!*\n\n"
            f"Foto ini sudah digunakan sebagai bukti untuk:\n"
            f"📦 *{dup_product}*\n\n"
            "Gunakan foto lain atau kirim manual:\n"
            "`/keluar [platform] [produk] [jumlah]`",
            parse_mode="Markdown",
        )
        return

    full_path = os.path.join(UPLOAD_FOLDER, first_media)

    # Run OCR
    try:
        result = parse_label(full_path)
    except Exception as e:
        logger.error(f"OCR error: {e}")
        await status_msg.edit_text(f"❌ Gagal membaca gambar: {e}")
        return

    platform = result["platform"]
    product_hint = result["product_hint"]
    quantity = result["quantity"]
    raw_text = result["raw_text"]

    if not platform and not product_hint:
        await status_msg.edit_text(
            "❌ *Tidak bisa membaca label.*\n\n"
            "Pastikan foto jelas dan ada nama produk.\n\n"
            "Atau kirim manual:\n"
            "`keluar [platform] [produk] [jumlah]`\n"
            "Contoh: `keluar shopee Kaos Polos 2`",
            parse_mode="Markdown",
        )
        return

    # Map platform to MovementSource
    source_map = {
        "shopee": MovementSource.SHOPEE,
        "tokopedia": MovementSource.TOKOPEDIA,
        "tiktok": MovementSource.SHOPEE,  # fallback
        "lazada": MovementSource.SHOPEE,  # fallback
        "bukalapak": MovementSource.TOKOPEDIA,  # fallback
        "blibli": MovementSource.TOKOPEDIA,  # fallback
    }
    source = source_map.get(platform, MovementSource.WEB)
    source_label = SOURCE_NAMES.get(platform, f"📤 {platform}")

    if product_hint:
        # Fuzzy match the product
        matches = find_product_fuzzy(product_hint, threshold=0.35)

        if matches and (matches[0][1] >= 0.75 or len(matches) == 1):
            # High confidence match — ask confirmation instead of auto-deduct
            best = matches[0][0]
            qty = quantity or 1

            keyboard = [
                [InlineKeyboardButton(
                    f"✅ Ya, keluar {qty}",
                    callback_data=f"confirm_out:{best.id}"
                )],
                [InlineKeyboardButton(
                    f"🔢 Ubah Jumlah",
                    callback_data=f"edit_out_qty:{best.id}"
                ), InlineKeyboardButton(
                    f"📝 Ubah Produk",
                    callback_data=f"edit_out_prod:{best.id}"
                )],
                [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
            ]

            sent = await status_msg.edit_text(
                f"🤔 *Konfirmasi Barang Keluar — OCR* 🤖\n\n"
                f"🏪 {source_label}\n"
                f"📦 {best.name}\n"
                f"🔢 Jumlah: *{qty}*\n"
                f"📊 Stok saat ini: *{best.current_stock}*\n\n"
                "Konfirmasi atau ubah dulu?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            _cache_outgoing(sent.message_id, best.id, best.name, qty,
                          source, source_label, first_media, media_hash, media_group_id,
                          ocr_hint=product_hint)

        elif matches and len(matches) > 1:
            # Multiple matches — ask user
            keyboard = []
            for p, score in matches[:6]:
                keyboard.append([InlineKeyboardButton(
                    f"{p.name} ({p.current_stock}) — {score:.0%}",
                    callback_data=f"out:{p.id}:{quantity or 1}:{platform or 'web'}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

            await status_msg.edit_text(
                f"🤔 *{source_label}* — Produk yang mana?\n\n"
                f"Nama terbaca: `{product_hint}`\n"
                f"Jumlah: *{quantity or '?'}*\n\n"
                "Pilih produk:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            # Cache OCR hint so "Ubah Produk" can filter by it later
            _cache_outgoing(status_msg.message_id, None, None,
                          quantity or 1, source, source_label,
                          first_media, media_hash, media_group_id,
                          ocr_hint=product_hint)
        else:
            # No match at all
            await status_msg.edit_text(
                f"🔍 *{source_label}*\n\n"
                f"Nama terbaca: `{product_hint}`\n"
                f"Tidak cocok dengan produk manapun.\n\n"
                "Kirim ulang dengan nama yang lebih jelas, atau:\n"
                "`keluar [platform] [produk] [jumlah]`",
                parse_mode="Markdown",
            )
    else:
        # Platform detected but no product — ask user
        await status_msg.edit_text(
            f"🏪 *{source_label}* terdeteksi!\n"
            f"Tapi nama produk tidak terbaca.\n\n"
            "Kirim manual:\n"
            f"`keluar {platform} [nama produk]`",
            parse_mode="Markdown",
        )


async def keluar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /keluar command — manual stock-out."""
    msg = update.message
    text = msg.text or msg.caption or ""

    # Parse: /keluar [platform] [product] [qty]
    # Remove command prefix
    text = re.sub(r'^/keluar\s*', '', text, flags=re.IGNORECASE).strip()

    if not text:
        await msg.reply_text(
            "📤 *Barang Keluar — Manual*\n\n"
            "Format: `/keluar [platform] [produk] [jumlah]`\n\n"
            "Contoh:\n"
            "`/keluar shopee Kaos Polos 2`\n"
            "`/keluar tokopedia DJI Neo 1`\n\n"
            "Atau kirim *foto label* + caption \"keluar\"",
            parse_mode="Markdown",
        )
        return

    # Try to extract platform
    platform = None
    platform_keywords = {
        "shopee": "shopee", "tokopedia": "tokopedia", "tokped": "tokopedia",
        "tiktok": "tiktok", "lazada": "lazada", "bukalapak": "bukalapak",
        "blibli": "blibli",
    }
    text_lower = text.lower()
    for kw, plat in platform_keywords.items():
        if text_lower.startswith(kw):
            platform = plat
            text = text[len(kw):].strip()
            break

    if not platform:
        await msg.reply_text(
            "❌ Platform tidak dikenal.\n\n"
            "Gunakan: shopee, tokopedia, tiktok, lazada, dll.\n"
            "Contoh: `/keluar shopee Kaos Polos 2`",
            parse_mode="Markdown",
        )
        return

    source_map = {
        "shopee": MovementSource.SHOPEE,
        "tokopedia": MovementSource.TOKOPEDIA,
        "tiktok": MovementSource.SHOPEE,
        "lazada": MovementSource.SHOPEE,
        "bukalapak": MovementSource.TOKOPEDIA,
        "blibli": MovementSource.TOKOPEDIA,
    }
    source = source_map.get(platform, MovementSource.WEB)
    source_label = SOURCE_NAMES.get(platform, platform)

    # Parse product + quantity
    name_hint, quantity = parse_smart(text)

    if not name_hint and quantity:
        # Only quantity — ask which product
        db = SessionLocal()
        try:
            products = db.query(Product).filter(Product.is_active == 1).order_by(Product.name).all()
        finally:
            db.close()

        if not products:
            await msg.reply_text("📭 Belum ada produk.")
            return

        keyboard = []
        row = []
        for p in products[:12]:
            row.append(InlineKeyboardButton(
                f"{p.name} ({p.current_stock})",
                callback_data=f"out:{p.id}:{quantity}:{platform}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

        await msg.reply_text(
            f"🏪 *{source_label}* — produk yang mana?\nJumlah: *{quantity}*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if not name_hint:
        await msg.reply_text("❌ Masukkan nama produk. Contoh: `/keluar shopee Kaos Polos 2`", parse_mode="Markdown")
        return

    qty = quantity or 1

    # Save photo if present
    photo_path = None
    if msg.photo:
        photo_path = await _save_telegram_photo(msg, name_hint)

    # Fuzzy match
    matches = find_product_fuzzy(name_hint, threshold=0.35)

    if not matches or matches[0][1] < 0.5:
        await msg.reply_text(
            f"🔍 \"{name_hint}\" tidak cocok dengan produk apapun.\n"
            "Coba nama yang lebih spesifik.",
        )
        return

    best = matches[0][0]

    # ── CONFIRMATION STEP ──
    keyboard = [
        [InlineKeyboardButton(
            f"✅ Ya, keluar {qty}",
            callback_data=f"confirm_out:{best.id}"
        )],
        [InlineKeyboardButton(
            f"🔢 Ubah Jumlah",
            callback_data=f"edit_out_qty:{best.id}"
        ), InlineKeyboardButton(
            f"📝 Ubah Produk",
            callback_data=f"edit_out_prod:{best.id}"
        )],
        [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
    ]

    sent = await msg.reply_text(
        f"🤔 *Konfirmasi Barang Keluar*\n\n"
        f"🏪 {source_label}\n"
        f"📦 {best.name}\n"
        f"🔢 Jumlah: *{qty}*\n"
        f"📊 Stok saat ini: *{best.current_stock}*\n\n"
        "Konfirmasi atau ubah dulu?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    _cache_outgoing(sent.message_id, best.id, best.name, qty,
                  source, source_label, photo_path, ocr_hint=name_hint)


# ── Handlers ─────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 *Sricreate Inventory Bot* — AI + OCR Ready! 🤖\n\n"
        "📥 *Stok Masuk:*\n"
        "`Kaos item 25`\n"
        "`Tambah yang merah 10`\n"
        "📸 Foto + caption jumlah\n\n"
        "📤 *Stok Keluar (OCR):*\n"
        "📸 Foto label Shopee/Tokopedia\n"
        "Caption: `keluar`\n\n"
        "📤 *Manual:*\n"
        "`/keluar shopee Kaos Polos 2`\n\n"
        "Perintah:\n"
        "/stock — Lihat stok\n"
        "/cari [nama] — Cari produk\n"
        "/keluar — Barang keluar\n"
        "/help — Bantuan",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Cara Pakai*\n\n"
        "📥 *Stok Masuk:*\n"
        "1️⃣ Kirim pesan bebas — AI akan cari produk terdekat\n"
        "   • `Kaos 25` → langsung masuk\n"
        "   • `Tambah yang item 10 pcs` → dicocokkan\n"
        "   • `25` → akan ditanya produk apa\n"
        "2️⃣ Kalau nama kurang jelas, bot akan tanya\n\n"
        "📤 *Stok Keluar (OCR):*\n"
        "1️⃣ Foto label pengiriman Shopee/Tokopedia\n"
        "2️⃣ Caption: `keluar`\n"
        "3️⃣ AI baca platform, nama produk, & jumlah\n"
        "4️⃣ Auto-kurangi stok!\n\n"
        "📤 *Manual:*\n"
        "`/keluar [platform] [produk] [jumlah]`\n"
        "Contoh: `/keluar shopee Kaos Polos 2`\n\n"
        "📊 Cek stok: /stock\n"
        "🔍 Cari: /cari [kata kunci]",
        parse_mode="Markdown",
    )


async def stock_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.is_active == 1).order_by(Product.name).all()
        if not products:
            await update.message.reply_text("📭 Belum ada produk.")
            return

        lines = ["📊 *Stok Saat Ini*\n"]
        for p in products:
            warn = " ⚠️" if p.current_stock <= p.min_stock else ""
            lines.append(f"• {p.name}: *{p.current_stock}*{warn}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        db.close()


async def search_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search products by keyword."""
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("❌ Gunakan: `/cari [kata kunci]`", parse_mode="Markdown")
        return

    matches = find_product_fuzzy(query, threshold=0.2)
    if not matches:
        await update.message.reply_text(f"🔍 Tidak ada produk cocok dengan \"{query}\"")
        return

    lines = [f"🔍 *Hasil: \"{query}\"*\n"]
    for p, score in matches[:10]:
        warn = " ⚠️" if p.current_stock <= p.min_stock else ""
        lines.append(f"• {p.name}: *{p.current_stock}*{warn} ({score:.0%})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# Track processed media groups: group_id → {'movement_id', 'product_name'}
_processed_groups: dict[str, dict] = {}
# Track users editing outgoing quantity: user_id → {product_id, source, source_label, ...}
_editing_out: dict[int, dict] = {}
# Store photo data by message_id so callbacks can retrieve it
import json as _json
_PHOTO_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photo_cache")
os.makedirs(_PHOTO_CACHE_DIR, exist_ok=True)
# Store outgoing confirmation data by message_id
_OUT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_cache")
os.makedirs(_OUT_CACHE_DIR, exist_ok=True)


def _cache_photo(msg_id: int, photo_path: str | None, photo_hash: str | None):
    """Store photo data keyed by Telegram message_id (survives restarts)."""
    if not photo_path:
        return
    data = _json.dumps({"photo_path": photo_path, "photo_hash": photo_hash})
    with open(os.path.join(_PHOTO_CACHE_DIR, str(msg_id)), "w") as f:
        f.write(data)


def _get_cached_photo(msg_id: int) -> tuple[str | None, str | None]:
    """Retrieve and delete cached photo data."""
    cache_file = os.path.join(_PHOTO_CACHE_DIR, str(msg_id))
    try:
        with open(cache_file) as f:
            data = _json.loads(f.read())
        os.remove(cache_file)
        return data.get("photo_path"), data.get("photo_hash")
    except (FileNotFoundError, _json.JSONDecodeError):
        return None, None


def _cache_outgoing(msg_id: int, product_id: int | None, product_name: str | None,
                    quantity: int, source, source_label: str,
                    photo_path: str | None = None, photo_hash: str | None = None,
                    media_group_id: str | None = None, ocr_hint: str | None = None):
    """Store outgoing stock data keyed by message_id for confirmation callback."""
    data = _json.dumps({
        "product_id": product_id,
        "product_name": product_name,
        "quantity": quantity,
        "source": source.value if hasattr(source, 'value') else source,
        "source_label": source_label,
        "photo_path": photo_path,
        "photo_hash": photo_hash,
        "media_group_id": media_group_id,
        "ocr_hint": ocr_hint,
    })
    with open(os.path.join(_OUT_CACHE_DIR, str(msg_id)), "w") as f:
        f.write(data)


def _get_cached_outgoing(msg_id: int) -> dict | None:
    """Retrieve and delete cached outgoing data."""
    cache_file = os.path.join(_OUT_CACHE_DIR, str(msg_id))
    try:
        with open(cache_file) as f:
            data = _json.loads(f.read())
        os.remove(cache_file)
        return data
    except (FileNotFoundError, _json.JSONDecodeError):
        return None


def _peek_cached_outgoing(msg_id: int) -> dict | None:
    """Read cached outgoing data WITHOUT deleting."""
    cache_file = os.path.join(_OUT_CACHE_DIR, str(msg_id))
    try:
        with open(cache_file) as f:
            return _json.loads(f.read())
    except (FileNotFoundError, _json.JSONDecodeError):
        return None


async def _send_and_cache(msg, photo_path, photo_hash, text, **kwargs):
    """Send a reply and cache photo data keyed by the sent message's ID."""
    sent = await msg.reply_text(text, **kwargs)
    _cache_photo(sent.message_id, photo_path, photo_hash)
    return sent


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart message handler with fuzzy product matching."""
    msg = update.message
    if not msg:
        return

    # DEBUG: dump raw update to file
    try:
        raw = str(msg)
        with open("/home/iankee/inventory/bot_debug.log", "a") as f:
            f.write(f"\n=== {datetime.now(timezone.utc)} ===\n")
            f.write(f"photo={bool(msg.photo)} video={bool(msg.video)} doc={bool(msg.document)}\n")
            f.write(f"caption={bool(msg.caption)} text={bool(msg.text)}\n")
            f.write(f"forward={bool(msg.forward_origin)} reply={bool(msg.reply_to_message)}\n")
            if msg.photo:
                f.write(f"photo sizes: {len(msg.photo)}\n")
            else:
                f.write(f"NO PHOTO in message\n")
            f.write(f"raw: {raw[:500]}\n")
    except Exception as e:
        with open("/home/iankee/inventory/bot_debug_err.log", "a") as f:
            f.write(f"DEBUG ERROR: {e}\n")

    text = msg.caption or msg.text or ""

    # ── Editing outgoing quantity? ──
    if msg.from_user and msg.from_user.id in _editing_out:
        # User is editing quantity — expect a number
        edit_data = _editing_out.pop(msg.from_user.id)
        try:
            new_qty = int(text.strip())
            if new_qty <= 0:
                await msg.reply_text("❌ Jumlah harus lebih dari 0. Kirim ulang angka.")
                _editing_out[msg.from_user.id] = edit_data
                return
        except ValueError:
            await msg.reply_text("❌ Kirim *angka* saja. Contoh: `3`", parse_mode="Markdown")
            _editing_out[msg.from_user.id] = edit_data
            return

        # Re-show confirmation with new quantity
        product_id = edit_data["product_id"]
        product_name = edit_data.get("product_name", "")
        source_val = edit_data.get("source", MovementSource.WEB.value)
        source_label = edit_data.get("source_label", "Web")
        photo_path = edit_data.get("photo_path")
        photo_hash = edit_data.get("photo_hash")
        media_group_id = edit_data.get("media_group_id")
        source = MovementSource(source_val) if isinstance(source_val, int) else MovementSource.WEB

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await msg.reply_text("❌ Produk tidak ditemukan.")
                return

            keyboard = [
                [InlineKeyboardButton(
                    f"✅ Ya, keluar {new_qty}",
                    callback_data=f"confirm_out:{product.id}"
                )],
                [InlineKeyboardButton(
                    f"🔢 Ubah Jumlah",
                    callback_data=f"edit_out_qty:{product.id}"
                ), InlineKeyboardButton(
                    f"📝 Ubah Produk",
                    callback_data=f"edit_out_prod:{product.id}"
                )],
                [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
            ]

            sent = await msg.reply_text(
                f"🤔 *Konfirmasi Barang Keluar*\n\n"
                f"🏪 {source_label}\n"
                f"📦 {product.name}\n"
                f"🔢 Jumlah: *{new_qty}*\n"
                f"📊 Stok saat ini: *{product.current_stock}*\n\n"
                "Konfirmasi atau ubah dulu?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            _cache_outgoing(sent.message_id, product.id, product.name, new_qty,
                          source, source_label, photo_path, photo_hash, media_group_id,
                          ocr_hint=edit_data.get("product_hint"))
        except Exception as e:
            db.rollback()
            await msg.reply_text(f"❌ Gagal: {e}")
        finally:
            db.close()
        return

    # ── Outgoing flow detection ──
    is_outgoing = False
    outgoing_keywords = ["keluar", "out", "barang keluar", "pengiriman"]
    text_lower = text.lower().strip()
    for kw in outgoing_keywords:
        if text_lower.startswith(kw) or text_lower == kw:
            is_outgoing = True
            break

    if is_outgoing and msg.photo:
        await _handle_outgoing(msg, text)
        return

    # ── Media group (multi-photo) handling ──
    media_group_id = msg.media_group_id
    if media_group_id:
        has_caption = bool(msg.caption and msg.caption.strip())
        group_data = _processed_groups.get(media_group_id)

        if group_data and not has_caption:
            # Subsequent media in group — save and append to movement
            media_path = await _save_telegram_media(msg, group_data["product_name"])
            if media_path:
                _append_photo_to_movement(group_data["movement_id"], media_path)
            return

        if has_caption:
            # First photo — process as stock-in, will store group data later
            pass
        else:
            # Photo without caption in a group — skip
            return

    # Parse name hint and quantity
    name_hint, quantity = parse_smart(text)

    # ── No name, no quantity ──
    if name_hint is None and quantity is None:
        await msg.reply_text(
            "❌ Tidak dapat membaca.\n\n"
            "Kirim dengan format:\n"
            "`Nama Barang Jumlah`\n\n"
            "Contoh: `Kaos Polos 25`\n"
            "Atau ketik /help untuk bantuan.",
            parse_mode="Markdown",
        )
        return

    # ── Quantity only (no name) ──
    if name_hint is None and quantity is not None:
        if quantity <= 0:
            await msg.reply_text("❌ Jumlah harus lebih dari 0.")
            return
        # Save photo if present (for callback)
        photo_path = None
        photo_hash = None
        if msg.photo:
            photo_path, photo_hash, dup_product = await save_and_dedup_media(msg, f"qty_{quantity}")
            if dup_product:
                await msg.reply_text(
                    f"⚠️ *Foto sudah pernah dipakai!*\n\n"
                    f"Foto ini sudah digunakan untuk:\n📦 *{dup_product}*\n\n"
                    "Kirim foto lain atau tanpa foto.",
                    parse_mode="Markdown",
                )
                return
        # Ask user which product
        db = SessionLocal()
        try:
            products = db.query(Product).filter(Product.is_active == 1).order_by(Product.name).all()
        finally:
            db.close()

        if not products:
            await msg.reply_text("📭 Belum ada produk. Tambah dulu lewat web dashboard ya:\nhttps://inv.sricreate.com")
            return

        # Show product selection keyboard
        keyboard = []
        row = []
        for p in products[:12]:
            row.append(InlineKeyboardButton(
                f"{p.name} ({p.current_stock})",
                callback_data=f"in:{p.id}:{quantity}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

        await _send_and_cache(msg, photo_path, photo_hash,
            f"🤔 Jumlah: *{quantity}* — produk yang mana?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # ── Name hint present ──
    if quantity is not None and quantity <= 0:
        await msg.reply_text("❌ Jumlah harus lebih dari 0.")
        return

    # Fuzzy match the name
    matches = find_product_fuzzy(name_hint, threshold=0.35)

    # Save photo early so confirm_in callback can use it
    photo_path = None
    photo_hash = None
    photo_token = None
    if msg.photo:
        photo_path, photo_hash, dup_product = await save_and_dedup_media(msg, name_hint)
        logger.info(f"Photo save attempt: path={photo_path}")
        if dup_product:
            await msg.reply_text(
                f"⚠️ *Foto sudah pernah dipakai!*\n\n"
                f"Foto ini sudah digunakan untuk:\n📦 *{dup_product}*\n\n"
                "Kirim foto lain atau tanpa foto.",
                parse_mode="Markdown",
            )
            return

    if not matches:
        # No match at all — create new product or ask
        if quantity is not None:
            # Has name + quantity, create new
            product = Product(name=name_hint, current_stock=0)
            _product, before, after, is_new, movement_id = do_stock_in(
                product, quantity,
                f"Telegram: @{msg.from_user.username or msg.from_user.id}",
                photo_path=photo_path,
                photo_hash=photo_hash,
            )
            # Store for multi-photo groups
            if media_group_id:
                _processed_groups[media_group_id] = {"movement_id": movement_id, "product_name": name_hint}
            new_tag = " 🆕 *BARU*" if is_new else ""
            warn = " ⚠️ Stok menipis!" if after <= product.min_stock else ""
            await msg.reply_text(
                f"✅ *Stok Masuk*{new_tag}\n\n"
                f"📦 {name_hint}\n"
                f"➕ +{quantity}\n"
                f"📊 {before} → *{after}*\n"
                f"{warn}",
                parse_mode="Markdown",
            )
        else:
            # Only name, no quantity — search
            await msg.reply_text(
                f"🔍 \"{name_hint}\" tidak ditemukan.\n"
                "Berapa jumlahnya? Kirim angka.\n"
                "Atau tambah produk baru di web dashboard:\n"
                "https://inv.sricreate.com"
            )
        return

    # Got matches
    best_match, best_score = matches[0]

    if best_score >= 0.75 or len(matches) == 1:
        # High confidence — but check for variant conflict first
        if name_hint and has_variant_conflict(name_hint, best_match.name):
            # User specified different color/size/etc — ask: new product or existing?
            q_variants = extract_variants(name_hint)
            p_variants = extract_variants(best_match.name)
            new_variants = q_variants - p_variants

            if quantity is None:
                quantity = 1

            keyboard = [
                [InlineKeyboardButton(
                    f"🆕 Buat baru: {name_hint} (+{quantity})",
                    callback_data=f"new:{quantity}:{name_hint[:80]}"
                )],
                [InlineKeyboardButton(
                    f"📦 Pakai yang ada: {best_match.name} (+{quantity})",
                    callback_data=f"confirm_in:{best_match.id}:{quantity}"
                )],
                [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
            ]

            variant_str = ", ".join(sorted(new_variants))
            await _send_and_cache(msg, photo_path, photo_hash,
                f"⚠️ *Terdeteksi variasi berbeda!*\n\n"
                f"Kamu kirim: `{name_hint}`\n"
                f"Variasi: *{variant_str}*\n\n"
                f"Produk terdekat: *{best_match.name}*\n"
                f"(variasi: {', '.join(sorted(p_variants)) if p_variants else 'tidak ada'})\n\n"
                "Mau buat produk baru atau pakai yang sudah ada?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        # Found product but no quantity specified
        if quantity is None:
            await msg.reply_text(
                f"🔍 Maksudnya: *{best_match.name}*?\n"
                f"Stok saat ini: *{best_match.current_stock}*\n\n"
                "Berapa jumlahnya? Kirim angka saja.",
                parse_mode="Markdown",
            )
            return

        # ── CONFIRMATION STEP (NEW) ──
        # Instead of auto stock-in, ask for confirmation
        keyboard = [
            [InlineKeyboardButton(
                f"✅ Ya, tambah {quantity}",
                callback_data=f"confirm_in:{best_match.id}:{quantity}"
            )],
            [InlineKeyboardButton(
                f"🆕 Buat produk baru: {name_hint[:40]}",
                callback_data=f"new:{quantity}:{name_hint[:80]}"
            )],
            [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
        ]

        await _send_and_cache(msg, photo_path, photo_hash,
            f"🤔 *Konfirmasi Stok Masuk*\n\n"
            f"📦 {best_match.name}\n"
            f"➕ +{quantity}\n"
            f"📊 Stok saat ini: *{best_match.current_stock}*\n\n"
            "Apakah benar?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    else:
        # Medium/low confidence — ask user to pick
        if quantity is None:
            quantity = 1  # default

        keyboard = []
        for p, score in matches[:5]:
            keyboard.append([InlineKeyboardButton(
                f"{p.name} ({p.current_stock}) — {score:.0%}",
                callback_data=f"confirm_in:{p.id}:{quantity}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"🆕 Buat produk baru: {name_hint[:40]}",
            callback_data=f"new:{quantity}:{name_hint[:80]}"
        )])
        keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

        await _send_and_cache(msg, photo_path, photo_hash,
            f"🤔 Maksudnya yang mana ya?\n"
            f"Jumlah: *{quantity}*\n\n"
            "Pilih produk, atau buat baru:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel":
        await query.edit_message_text("❌ Dibatalkan.")
        return

    # Format: confirm_in:product_id:quantity  OR  in:product_id:quantity  OR  out:...
    if data.startswith("confirm_in:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        product_id = int(parts[1])
        quantity = int(parts[2])

        # Retrieve cached photo by confirmation message ID
        photo_path, photo_hash = _get_cached_photo(query.message.message_id)
        logger.info(f"confirm_in: msg_id={query.message.message_id} photo_path={photo_path}")

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.edit_message_text("❌ Produk tidak ditemukan.")
                return

            before = product.current_stock
            product.current_stock += quantity

            movement = StockMovement(
                product_id=product.id,
                type=MovementType.STOCK_IN,
                source=MovementSource.TELEGRAM,
                quantity=quantity,
                stock_before=before,
                stock_after=product.current_stock,
                notes=f"Telegram: @{query.from_user.username or query.from_user.id}",
                photo_path=photo_path,
                photo_hash=photo_hash,
            )
            db.add(movement)
            db.commit()

            warn = " ⚠️ Stok menipis!" if product.current_stock <= product.min_stock else ""
            await query.edit_message_text(
                f"✅ *Stok Masuk*\n\n"
                f"📦 {product.name}\n"
                f"➕ +{quantity}\n"
                f"📊 {before} → *{product.current_stock}*\n"
                f"{warn}",
                parse_mode="Markdown",
            )
            logger.info(f"Stock-in (confirm): {product.name} +{quantity}")
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()

    elif data.startswith("in:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        product_id = int(parts[1])
        quantity = int(parts[2])

        # Retrieve cached photo by message ID
        photo_path, photo_hash = _get_cached_photo(query.message.message_id)

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.edit_message_text("❌ Produk tidak ditemukan.")
                return

            before = product.current_stock
            product.current_stock += quantity

            movement = StockMovement(
                product_id=product.id,
                type=MovementType.STOCK_IN,
                source=MovementSource.TELEGRAM,
                quantity=quantity,
                stock_before=before,
                stock_after=product.current_stock,
                notes=f"Telegram: @{query.from_user.username or query.from_user.id}",
                photo_path=photo_path,
                photo_hash=photo_hash,
            )
            db.add(movement)
            db.commit()

            warn = " ⚠️ Stok menipis!" if product.current_stock <= product.min_stock else ""
            await query.edit_message_text(
                f"✅ *Stok Masuk*\n\n"
                f"📦 {product.name}\n"
                f"➕ +{quantity}\n"
                f"📊 {before} → *{product.current_stock}*\n"
                f"{warn}",
                parse_mode="Markdown",
            )
            logger.info(f"Stock-in (callback): {product.name} +{quantity}")
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()

    elif data.startswith("new:"):
        # Format: new:quantity:product_name
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        quantity = int(parts[1])
        product_name = parts[2]

        # Retrieve cached photo by message ID
        photo_path, photo_hash = _get_cached_photo(query.message.message_id)

        db = SessionLocal()
        try:
            product = Product(name=product_name, current_stock=0)
            _product, before, after, is_new, movement_id = do_stock_in(
                product, quantity,
                f"Telegram: @{query.from_user.username or query.from_user.id} (varian baru)",
                photo_path=photo_path,
                photo_hash=photo_hash,
            )
            new_tag = " 🆕 *BARU*"
            await query.edit_message_text(
                f"✅ *Stok Masuk — Produk Baru!*{new_tag}\n\n"
                f"📦 {product_name}\n"
                f"➕ +{quantity}\n"
                f"📊 0 → *{after}*\n"
                "Produk varian baru berhasil dibuat!",
                parse_mode="Markdown",
            )
            logger.info(f"New variant created (callback): {product_name} +{quantity}")
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()

    elif data.startswith("out:"):
        parts = data.split(":")
        if len(parts) != 4:
            return
        product_id = int(parts[1])
        quantity = int(parts[2])
        platform = parts[3]

        source_map = {
            "shopee": MovementSource.SHOPEE,
            "tokopedia": MovementSource.TOKOPEDIA,
            "tiktok": MovementSource.SHOPEE,
            "lazada": MovementSource.SHOPEE,
            "bukalapak": MovementSource.TOKOPEDIA,
            "blibli": MovementSource.TOKOPEDIA,
        }
        source = source_map.get(platform, MovementSource.WEB)
        source_label = SOURCE_NAMES.get(platform, platform)

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.edit_message_text("❌ Produk tidak ditemukan.")
                return

            # Show confirmation instead of auto-deduct
            keyboard = [
                [InlineKeyboardButton(
                    f"✅ Ya, keluar {quantity}",
                    callback_data=f"confirm_out:{product.id}"
                )],
                [InlineKeyboardButton(
                    f"🔢 Ubah Jumlah",
                    callback_data=f"edit_out_qty:{product.id}"
                ), InlineKeyboardButton(
                    f"📝 Ubah Produk",
                    callback_data=f"edit_out_prod:{product.id}"
                )],
                [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
            ]

            sent = await query.edit_message_text(
                f"🤔 *Konfirmasi Barang Keluar*\n\n"
                f"🏪 {source_label}\n"
                f"📦 {product.name}\n"
                f"🔢 Jumlah: *{quantity}*\n"
                f"📊 Stok saat ini: *{product.current_stock}*\n\n"
                "Konfirmasi atau ubah dulu?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            # Carry forward OCR hint from multi-match picker
            picker_hint = _peek_cached_outgoing(query.message.message_id)
            ocr_hint = picker_hint.get("ocr_hint") if picker_hint else None
            _cache_outgoing(sent.message_id, product.id, product.name, quantity,
                          source, source_label, ocr_hint=ocr_hint)
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()

    elif data.startswith("confirm_out:"):
        parts = data.split(":")
        if len(parts) != 2:
            return
        product_id = int(parts[1])

        # Retrieve cached outgoing data
        cached = _get_cached_outgoing(query.message.message_id)

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.edit_message_text("❌ Produk tidak ditemukan.")
                return

            # Use cached quantity/source if available, fallback to defaults
            if cached:
                quantity = cached["quantity"]
                source_val = cached["source"]
                source_label = cached["source_label"]
                photo_path = cached.get("photo_path")
                photo_hash = cached.get("photo_hash")
                media_group_id = cached.get("media_group_id")
                # Reconstruct source enum
                source = MovementSource(source_val) if isinstance(source_val, int) else MovementSource.WEB
            else:
                quantity = 1
                source = MovementSource.WEB
                source_label = "Web"
                photo_path = None
                photo_hash = None
                media_group_id = None

            before = product.current_stock
            if before < quantity:
                await query.edit_message_text(
                    f"⚠️ *Stok tidak cukup!*\n\n"
                    f"📦 {product.name}\n📦 Stok: *{before}*\n🔻 Butuh: *{quantity}*",
                    parse_mode="Markdown",
                )
                return

            product.current_stock -= quantity

            movement = StockMovement(
                product_id=product.id,
                type=MovementType.STOCK_OUT,
                source=source,
                quantity=-quantity,
                stock_before=before,
                stock_after=product.current_stock,
                notes=f"Telegram: {source_label} — @{query.from_user.username or query.from_user.id}",
                photo_path=photo_path,
                photo_hash=photo_hash,
            )
            db.add(movement)
            movement_id = movement.id
            db.commit()

            # Store for media group tracking
            if media_group_id:
                _processed_groups[media_group_id] = {"movement_id": movement_id, "product_name": product.name}

            warn = " ⚠️ Stok menipis!" if product.current_stock <= product.min_stock else ""
            await query.edit_message_text(
                f"📤 *Barang Keluar*\n\n"
                f"🏪 {source_label}\n"
                f"📦 {product.name}\n"
                f"➖ -{quantity}\n"
                f"📊 {before} → *{product.current_stock}*\n"
                f"{warn}",
                parse_mode="Markdown",
            )
            logger.info(f"Stock-out (confirm): {product.name} -{quantity} via {source_label}")
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()

    elif data.startswith("edit_out_qty:"):
        # User wants to change quantity — prompt them to type a number
        parts = data.split(":")
        if len(parts) != 2:
            return
        product_id = int(parts[1])

        # Store editing state for this user
        cached = _get_cached_outgoing(query.message.message_id)
        if cached:
            _editing_out[query.from_user.id] = {
                "product_id": product_id,
                "product_name": cached["product_name"],
                "source": cached["source"],
                "source_label": cached["source_label"],
                "photo_path": cached.get("photo_path"),
                "photo_hash": cached.get("photo_hash"),
                "media_group_id": cached.get("media_group_id"),
                "product_hint": cached.get("ocr_hint"),
            }
        else:
            _editing_out[query.from_user.id] = {"product_id": product_id}

        await query.edit_message_text(
            "🔢 *Kirim jumlah baru* (angka saja).\n\n"
            "Contoh: `3`",
            parse_mode="Markdown",
        )

    elif data.startswith("edit_out_prod:"):
        # User wants to change product — show matching products (fuzzy by OCR hint)
        parts = data.split(":")
        if len(parts) != 2:
            return

        # Peek cached OCR hint for fuzzy filtering
        cached = _peek_cached_outgoing(query.message.message_id)
        ocr_hint = cached.get("ocr_hint") if cached else None

        db = SessionLocal()
        try:
            if ocr_hint:
                # Fuzzy match against OCR hint — show closest products
                matches = find_product_fuzzy(ocr_hint, threshold=0.2)
                products = [p for p, _ in matches[:12]]
                hint_text = f" (dari: `{ocr_hint}`)"
            else:
                # No hint — show first 12 products alphabetically
                products = db.query(Product).filter(
                    Product.is_active == 1
                ).order_by(Product.name).limit(12).all()
                hint_text = ""
        finally:
            db.close()

        if not products:
            await query.edit_message_text("📭 Tidak ada produk yang cocok.")
            return

        keyboard = []
        row = []
        for p in products:
            row.append(InlineKeyboardButton(
                f"{p.name} ({p.current_stock})",
                callback_data=f"out_pick:{p.id}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

        await query.edit_message_text(
            f"📝 *Pilih produk yang benar:*{hint_text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    elif data.startswith("out_pick:"):
        # User picked a different product — re-show confirmation with new product
        parts = data.split(":")
        if len(parts) != 2:
            return
        product_id = int(parts[1])

        # Get cached outgoing data (quantity, source, etc.)
        cached = _get_cached_outgoing(query.message.message_id)
        quantity = cached["quantity"] if cached else 1
        source_val = cached["source"] if cached else MovementSource.WEB.value
        source_label = cached["source_label"] if cached else "Web"
        photo_path = cached.get("photo_path") if cached else None
        photo_hash = cached.get("photo_hash") if cached else None
        media_group_id = cached.get("media_group_id") if cached else None
        source = MovementSource(source_val) if isinstance(source_val, int) else MovementSource.WEB

        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                await query.edit_message_text("❌ Produk tidak ditemukan.")
                return

            keyboard = [
                [InlineKeyboardButton(
                    f"✅ Ya, keluar {quantity}",
                    callback_data=f"confirm_out:{product.id}"
                )],
                [InlineKeyboardButton(
                    f"🔢 Ubah Jumlah",
                    callback_data=f"edit_out_qty:{product.id}"
                ), InlineKeyboardButton(
                    f"📝 Ubah Produk",
                    callback_data=f"edit_out_prod:{product.id}"
                )],
                [InlineKeyboardButton("❌ Batal", callback_data="cancel")],
            ]

            sent = await query.edit_message_text(
                f"🤔 *Konfirmasi Barang Keluar*\n\n"
                f"🏪 {source_label}\n"
                f"📦 {product.name}\n"
                f"🔢 Jumlah: *{quantity}*\n"
                f"📊 Stok saat ini: *{product.current_stock}*\n\n"
                "Konfirmasi atau ubah dulu?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            _cache_outgoing(sent.message_id, product.id, product.name, quantity,
                          source, source_label, photo_path, photo_hash, media_group_id,
                          ocr_hint=cached.get("ocr_hint"))
        except Exception as e:
            db.rollback()
            await query.edit_message_text(f"❌ Gagal: {e}")
        finally:
            db.close()


# ── Main ─────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stock", stock_list))
    app.add_handler(CommandHandler("cari", search_product))
    app.add_handler(CommandHandler("keluar", keluar_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | (filters.TEXT & ~filters.COMMAND), handle_message))

    logger.info("🤖 Inventory Bot (AI + OCR) started — polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
