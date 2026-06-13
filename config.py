import os

# Database
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'inventory.db')}"

# Auth
SECRET_KEY = os.getenv("INV_SECRET_KEY", "change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480  # 8 jam

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv("INV_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = [
    u.strip()
    for u in os.getenv("INV_ALLOWED_USERS", "").split(",")
    if u.strip()
]

# Upload folder for receipt photos
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

# Server
HOST = "0.0.0.0"
PORT = int(os.getenv("INV_PORT", "5002"))
