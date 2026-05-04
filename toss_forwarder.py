import asyncio
import logging
import re
import hashlib
import random
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events
from dotenv import load_dotenv
import os

# ─────────────────────────────────────────────
# 0. Load environment variables
# ─────────────────────────────────────────────
load_dotenv()

API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "")
PHONE        = os.getenv("PHONE", "")
SOURCE_CHAN  = os.getenv("SOURCE_CHANNEL", "")
TARGET_CHAN  = os.getenv("TARGET_CHANNEL", "")
SESSION_NAME = os.getenv("SESSION_NAME", "toss_session")

# ─────────────────────────────────────────────
# 1. Logging setup
# ─────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "forwarder.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 2. Duplicate prevention
# ─────────────────────────────────────────────
_seen_hashes: set[str] = set()
SEEN_CACHE_FILE = Path("seen_hashes.txt")

def _load_seen_cache():
    if SEEN_CACHE_FILE.exists():
        with open(SEEN_CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                _seen_hashes.add(line.strip())
        log.info("Loaded %d seen hashes.", len(_seen_hashes))

def _save_hash(h: str):
    _seen_hashes.add(h)
    with open(SEEN_CACHE_FILE, "a", encoding="utf-8") as f:
        f.write(h + "\n")

def _message_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────
# 3. Filter patterns (FIXED)
# ─────────────────────────────────────────────
_TOSS_PHRASE = re.compile(r"WON\s+THE\s+TOSS\s+AND\s+DECIDED\s+TO", re.IGNORECASE)
_DECISION = re.compile(r"\b(BAT|BOWL)\b", re.IGNORECASE)

# 🔥 FIX: allow ticks anywhere (not just end)
_ENDING = re.compile(r"[✔✅✓☑]")

def is_toss_message(text: str) -> bool:
    if not text:
        return False

    normalised = " ".join(text.split())

    return bool(
        _TOSS_PHRASE.search(normalised)
        and _DECISION.search(normalised)
        and _ENDING.search(text)
    )

# ─────────────────────────────────────────────
# 4. Emoji styling (MODIFIED ONLY HERE)
# ─────────────────────────────────────────────
def format_message(text: str) -> str:
    text_clean = text.strip()

    # Remove all * (fix broken formatting)
    text_clean = text_clean.replace("*", "")

    # Random replacement for DECIDED
    choices = ["OPT", "WANT", "PICKED", "ELECT", "CHOOSE"]
    replacement = random.choice(choices)
    text_clean = re.sub(r"\bDECIDED\b", replacement, text_clean, flags=re.IGNORECASE)

    # Add FIRST after BAT or BOWL if missing
    text_clean = re.sub(r"\bBAT\b(?!\s+FIRST)", "BAT FIRST", text_clean, flags=re.IGNORECASE)
    text_clean = re.sub(r"\bBOWL\b(?!\s+FIRST)", "BOWL FIRST", text_clean, flags=re.IGNORECASE)

    # Replace ending ticks with ✅✅
    text_clean = re.sub(r"(✔️|✔|✓|☑|✅)+\s*$", "✅✅", text_clean)

    # Clean spacing
    text_clean = " ".join(text_clean.split())

    # 5. Make text BOLD
    return f"<b>{text_clean}</b>"

# ─────────────────────────────────────────────
# 5. Client
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@client.on(events.NewMessage(chats=SOURCE_CHAN))
async def handle_new_message(event):
    message = event.message
    text = message.text or ""

    log.debug("Received: %s", text)

    # Filter
    if not is_toss_message(text):
        return

    # Duplicate check
    h = _message_hash(text)
    if h in _seen_hashes:
        log.info("Duplicate skipped.")
        return

    try:
        new_msg = format_message(text)

        # ✅ Copy-paste (not forward)
        await client.send_message(entity=TARGET_CHAN, message=new_msg, parse_mode="html")

        _save_hash(h)

        log.info(
            "✅ Sent at %s | %s",
            datetime.utcnow().isoformat(timespec="seconds"),
            new_msg,
        )

    except Exception as e:
        log.error("❌ Error: %s", e)

# ─────────────────────────────────────────────
# 6. Main
# ─────────────────────────────────────────────
async def main():
    if not all([API_ID, API_HASH, PHONE, SOURCE_CHAN, TARGET_CHAN]):
        log.critical("Missing config in .env")
        return

    _load_seen_cache()

    log.info("🚀 Starting...")
    log.info("Source: %s", SOURCE_CHAN)
    log.info("Target: %s", TARGET_CHAN)

    await client.start(phone=PHONE)

    # 🔥 FIX: load dialogs (important for Railway)
    await client.get_dialogs()

    log.info("✅ Logged in. Listening...")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())