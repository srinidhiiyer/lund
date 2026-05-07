import asyncio
import logging
import re
import hashlib
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
TARGET_CHAN  = os.getenv("TARGET_CHANNEL", "")
SESSION_NAME = os.getenv("SESSION_NAME", "toss_session")
SOURCE_CHAN  = os.getenv("SOURCE_CHANNEL", "")

MAX_MSG_LEN = 100

# ─────────────────────────────────────────────
# 1. Logging
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
_seen_keys: set[str] = set()
SEEN_CACHE_FILE = Path("seen_hashes.txt")

def _load_seen_cache() -> None:
    if SEEN_CACHE_FILE.exists():
        with open(SEEN_CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                h = line.strip()
                if h:
                    _seen_keys.add(h)
        log.info("Loaded %d toss keys from cache.", len(_seen_keys))

def _persist_key(key: str) -> None:
    _seen_keys.add(key)
    with open(SEEN_CACHE_FILE, "a", encoding="utf-8") as f:
        f.write(key + "\n")

def _toss_key(text: str) -> str | None:
    """Team name + decision as dedup key, all emoji/punctuation stripped."""
    clean = text.encode("ascii", "ignore").decode("ascii")
    clean = clean.lower()
    clean = re.sub(r"[^a-z0-9\s]", " ", clean)
    clean = " ".join(clean.split())

    match = re.search(r"^(.*?)\s+won\s+the\s+toss", clean)
    if not match:
        return None
    team = re.sub(r"\s+", "", match.group(1))

    decision_match = re.search(r"\b(bat|bowl)\b", clean)
    if not decision_match:
        return None

    return f"{team}|{decision_match.group(1)}"

# ─────────────────────────────────────────────
# 3. Filter
# ─────────────────────────────────────────────
_TOSS_PHRASE = re.compile(
    r"WON\s+THE\s+TOSS\s+AND\s+(DECIDED|CHOSE|OPTED?|ELECTED|CALLED)\s+TO",
    re.IGNORECASE
)
_DECISION = re.compile(r"\b(BAT|BOWL)\b", re.IGNORECASE)
_ENDING   = re.compile(r"[✔✅✓☑]")

def is_toss_message(text: str) -> bool:
    if not text:
        return False

    if len(text.strip()) > MAX_MSG_LEN:
        log.debug("Blocked: too long (%d chars)", len(text.strip()))
        return False

    normalised = " ".join(text.split())

    if len(_TOSS_PHRASE.findall(normalised)) > 1:
        log.debug("Blocked: multiple toss results.")
        return False

    has_phrase   = bool(_TOSS_PHRASE.search(normalised))
    has_decision = bool(_DECISION.search(normalised))
    has_tick     = bool(_ENDING.search(text))
    has_first    = bool(re.search(r"\b(BAT|BOWL)\s+FIRST\b", normalised, re.IGNORECASE))

    return has_phrase and has_decision and (has_tick or has_first)

# ─────────────────────────────────────────────
# 4. Format output — bold, clean text
# ─────────────────────────────────────────────
def _format_output(text: str) -> str:
    """Strip asterisks, wrap entire message in bold HTML."""
    clean = text.strip().replace("*", "")
    clean = " ".join(clean.split())
    return f"<b>{clean}</b>"

# ─────────────────────────────────────────────
# 5. Client
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ─────────────────────────────────────────────
# 6. Handler
# ─────────────────────────────────────────────
async def handle_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text    = message.text or ""

    log.debug("Received: %s", text[:80])

    if not is_toss_message(text):
        return

    toss_key = _toss_key(text)
    if not toss_key:
        log.debug("Could not extract toss key, skipping.")
        return

    if toss_key in _seen_keys:
        log.info("Duplicate skipped (key: %s).", toss_key)
        return

    _persist_key(toss_key)

    formatted = _format_output(text)

    try:
        await client.send_message(
            entity=TARGET_CHAN,
            message=formatted,
            parse_mode="html",
        )
        log.info("✅ Sent at %s: %s", datetime.now().isoformat(timespec="seconds"), formatted)
    except Exception as exc:
        log.error("❌ Send error: %s", exc)

# ─────────────────────────────────────────────
# 7. Main
# ─────────────────────────────────────────────
async def main() -> None:
    if not all([API_ID, API_HASH, PHONE, SOURCE_CHAN, TARGET_CHAN]):
        log.critical("Missing config. Need API_ID, API_HASH, PHONE, SOURCE_CHANNEL, TARGET_CHANNEL in .env")
        return

    _load_seen_cache()

    log.info("🚀 Starting…")
    log.info("Source : %s", SOURCE_CHAN)
    log.info("Target : %s", TARGET_CHAN)

    await client.start(phone=PHONE)
    await client.get_dialogs(limit=200)
    await asyncio.sleep(2)

    try:
        source_entity = await client.get_entity(SOURCE_CHAN)
        log.info("✅ Listening to: %s", SOURCE_CHAN)
    except Exception as e:
        log.critical("❌ Could not resolve source channel %s: %s", SOURCE_CHAN, e)
        return

    client.add_event_handler(
        handle_new_message,
        events.NewMessage(chats=[source_entity])
    )

    log.info("🟢 Ready. Waiting for toss messages…")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
