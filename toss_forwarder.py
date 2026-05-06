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

# Comma-separated list of source channels in .env:
#   SOURCE_CHANNELS=@chan1,@chan2,@chan3,@chan4,@chan5
_raw_sources = os.getenv("SOURCE_CHANNELS", os.getenv("SOURCE_CHANNEL", ""))
SOURCE_CHANS: list[str] = [s.strip() for s in _raw_sources.split(",") if s.strip()]

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
_seen_exact: set[str] = set()
_seen_fuzzy: set[str] = set()
_dedup_lock = asyncio.Lock()

SEEN_CACHE_FILE = Path("seen_hashes.txt")

def _load_seen_cache() -> None:
    if SEEN_CACHE_FILE.exists():
        with open(SEEN_CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(":", 1)
                if len(parts) == 2:
                    kind, h = parts
                    (_seen_exact if kind == "e" else _seen_fuzzy).add(h)
                else:
                    _seen_exact.add(parts[0])
        log.info("Loaded %d exact + %d fuzzy hashes.", len(_seen_exact), len(_seen_fuzzy))

def _persist_hashes(exact: str, fuzzy: str) -> None:
    _seen_exact.add(exact)
    _seen_fuzzy.add(fuzzy)
    with open(SEEN_CACHE_FILE, "a", encoding="utf-8") as f:
        f.write(f"e:{exact}\n")
        f.write(f"f:{fuzzy}\n")

def _exact_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _fuzzy_hash(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = " ".join(t.split())
    return hashlib.sha256(t.encode("utf-8")).hexdigest()

# ─────────────────────────────────────────────
# 3. Filter
# ─────────────────────────────────────────────
_TOSS_PHRASE = re.compile(r"WON\s+THE\s+TOSS\s+AND\s+(DECIDED|CHOSE|OPTED|ELECTED)\s+TO", re.IGNORECASE)
_DECISION    = re.compile(r"\b(BAT|BOWL)\b", re.IGNORECASE)
_ENDING      = re.compile(r"[✔✅✓☑]")

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
# 4. Handler (registered dynamically in main)
# ─────────────────────────────────────────────
async def handle_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text    = (message.text or "").replace("*", "")
    source  = getattr(event.chat, "username", None) or str(event.chat_id)

    log.debug("[%s] Received: %s", source, text[:80])

    if not is_toss_message(text):
        return

    # Fast pre-check without lock
    eh = _exact_hash(text)
    fh = _fuzzy_hash(text)

    if eh in _seen_exact or fh in _seen_fuzzy:
        log.info("[%s] Duplicate skipped.", source)
        return

    # Confirm + claim under lock
    async with _dedup_lock:
        if eh in _seen_exact or fh in _seen_fuzzy:
            log.info("[%s] Duplicate skipped (race).", source)
            return
        _persist_hashes(eh, fh)

    try:
        await client.send_message(
            entity=TARGET_CHAN,
            message=text,
            formatting_entities=message.entities,
        )
        log.info("✅ [%s] Sent at %s", source, datetime.utcnow().isoformat(timespec="seconds"))

    except Exception as exc:
        log.error("❌ [%s] Send error: %s", source, exc)

# ─────────────────────────────────────────────
# 5. Client
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ─────────────────────────────────────────────
# 6. Main
# ─────────────────────────────────────────────
async def main() -> None:
    if not all([API_ID, API_HASH, PHONE, SOURCE_CHANS, TARGET_CHAN]):
        log.critical("Missing config in .env")
        return

    _load_seen_cache()

    log.info("🚀 Starting…")
    log.info("Sources (%d): %s", len(SOURCE_CHANS), ", ".join(SOURCE_CHANS))
    log.info("Target : %s", TARGET_CHAN)

    await client.start(phone=PHONE)
    await client.get_dialogs()

    # Resolve every source channel explicitly so Telethon
    # actively listens to ALL of them, not just the first
    resolved = []
    for ch in SOURCE_CHANS:
        try:
            entity = await client.get_entity(ch)
            resolved.append(entity)
            log.info("✅ Listening to: %s", ch)
        except Exception as e:
            log.error("❌ Could not resolve %s: %s", ch, e)

    if not resolved:
        log.critical("No source channels resolved. Check your SOURCE_CHANNELS in .env")
        return

    # Register handler AFTER login with fully resolved entities
    client.add_event_handler(
        handle_new_message,
        events.NewMessage(chats=resolved)
    )

    log.info("🟢 Watching %d source(s). Whichever posts first wins.", len(resolved))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
