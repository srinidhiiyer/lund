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
# Falls back to SOURCE_CHANNEL for backward compatibility.
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
# 2. Duplicate prevention (two layers)
#    a) Exact hash  — byte-for-byte duplicates
#    b) Fuzzy hash  — same content with minor emoji/spacing differences
# ─────────────────────────────────────────────
_seen_exact: set[str] = set()
_seen_fuzzy: set[str] = set()
_dedup_lock = asyncio.Lock()   # prevents race when 2 sources fire at once

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
                    _seen_exact.add(parts[0])   # legacy format
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
    """Strips emoji/punctuation/case so near-identical messages collide."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)   # keep only word chars
    t = " ".join(t.split())                              # collapse whitespace
    return hashlib.sha256(t.encode("utf-8")).hexdigest()

def _is_duplicate(text: str) -> tuple[bool, str, str]:
    eh = _exact_hash(text)
    fh = _fuzzy_hash(text)
    return (eh in _seen_exact or fh in _seen_fuzzy), eh, fh

# ─────────────────────────────────────────────
# 3. Filter — only toss messages pass through
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
# 4. Client + event handler
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

@client.on(events.NewMessage(chats=SOURCE_CHANS))
async def handle_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text    = message.text or ""
    text    = text.replace("*", "") 
    source  = getattr(event.chat, "username", None) or str(event.chat_id)

    log.debug("[%s] Received: %s", source, text[:80])

    # ── Filter ────────────────────────────────
    if not is_toss_message(text):
        return

# ── Dedup — fast pre-check without lock, then confirm under lock ──
    eh = _exact_hash(text)
    fh = _fuzzy_hash(text)

    if eh in _seen_exact or fh in _seen_fuzzy:
        log.info("[%s] Duplicate skipped.", source)
        return

    async with _dedup_lock:
        # Re-check inside lock in case another source just claimed it
        if eh in _seen_exact or fh in _seen_fuzzy:
            log.info("[%s] Duplicate skipped (race).", source)
            return
        _persist_hashes(eh, fh)

    # ── Send raw text — no formatting changes, no "Forwarded from" ──
    try:
        # Re-send with the original Telegram formatting entities (bold/italic/etc.)
        # so the message looks exactly the same as the source, just without
        # any "Forwarded from" header.
        await client.send_message(
            entity=TARGET_CHAN,
            message=text,
            formatting_entities=message.entities,  # preserves original bold/italic/etc.
        )

        log.info(
            "✅ [%s] Sent at %s",
            source,
            datetime.utcnow().isoformat(timespec="seconds"),
        )

    except Exception as exc:
        log.error("❌ [%s] Send error: %s", source, exc)

# ─────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────
async def main() -> None:
    if not all([API_ID, API_HASH, PHONE, SOURCE_CHANS, TARGET_CHAN]):
        log.critical(
            "Missing config. Need API_ID, API_HASH, PHONE, "
            "SOURCE_CHANNELS (or SOURCE_CHANNEL), TARGET_CHANNEL in .env"
        )
        return

    _load_seen_cache()

    log.info("🚀 Starting…")
    log.info("Sources (%d): %s", len(SOURCE_CHANS), ", ".join(SOURCE_CHANS))
    log.info("Target : %s", TARGET_CHAN)

    await client.start(phone=PHONE)
    await client.get_dialogs()   # ensures all channels are cached

    log.info("✅ Logged in. Listening on %d source(s)…", len(SOURCE_CHANS))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
