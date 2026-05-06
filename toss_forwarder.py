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

# The source you want to prioritise if multiple arrive at the same time
# Set this in .env as: PRIORITY_SOURCE=kingtossline111
PRIORITY_SOURCE = os.getenv("PRIORITY_SOURCE", "").lower().strip("@")

# Comma-separated source channels in .env:
#   SOURCE_CHANNELS=@chan1,@chan2,@chan3,@chan4,@chan5
_raw_sources = os.getenv("SOURCE_CHANNELS", os.getenv("SOURCE_CHANNEL", ""))
SOURCE_CHANS: list[str] = [s.strip() for s in _raw_sources.split(",") if s.strip()]

# Max characters allowed for a valid single-line toss message
# Example valid msg: "🇦🇫 DUBAI ROYAL 🇦🇫 WON THE TOSS AND DECIDED TO BOWL ✔️✔️" (~60 chars)
# Bulk/promo messages are much longer and will be blocked
MAX_MSG_LEN = 100

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
_dedup_lock  = asyncio.Lock()

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
    """
    Strip all emoji and non-ASCII first, then remove punctuation.
    So '🇦🇫 SPEEN GHAR 🇦🇫 WON THE TOSS... BAT' and
       'SPEEN GHAR WON THE TOSS... BAT' produce the same hash.
    """
    t = text.lower()
    t = t.encode("ascii", "ignore").decode("ascii")   # drop all emoji/unicode
    t = re.sub(r"[^a-z0-9\s]", " ", t)               # keep only alphanum
    t = " ".join(t.split())                            # collapse spaces
    return hashlib.sha256(t.encode("utf-8")).hexdigest()

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

    # Block long messages — bulk summaries, promos, spam
    if len(text.strip()) > MAX_MSG_LEN:
        log.debug("Blocked long message (%d chars)", len(text.strip()))
        return False

    normalised = " ".join(text.split())

    # Block if more than one toss result in the message
    if len(_TOSS_PHRASE.findall(normalised)) > 1:
        log.debug("Blocked multi-toss message.")
        return False

    has_phrase   = bool(_TOSS_PHRASE.search(normalised))
    has_decision = bool(_DECISION.search(normalised))
    has_tick     = bool(_ENDING.search(text))
    has_first    = bool(re.search(r"\b(BAT|BOWL)\s+FIRST\b", normalised, re.IGNORECASE))

    return has_phrase and has_decision and (has_tick or has_first)

# ─────────────────────────────────────────────
# 4. Priority queue
#    Each toss is held for 2 seconds.
#    If PRIORITY_SOURCE arrives in that window → it wins.
#    Otherwise the first source that arrived wins.
# ─────────────────────────────────────────────
_pending: dict = {}
_pending_lock = asyncio.Lock()

async def _flush_pending(fh: str) -> None:
    """Wait 2s then send the best available source for this toss."""
    await asyncio.sleep(2)

    async with _pending_lock:
        entry = _pending.pop(fh, None)

    if not entry:
        return

    chosen = entry.get("priority") or entry.get("first")
    if not chosen:
        return

    text, entities, source = chosen
    try:
        await client.send_message(
            entity=TARGET_CHAN,
            message=text,
            formatting_entities=entities,
        )
        log.info(
            "✅ [%s] Sent at %s",
            source,
            datetime.utcnow().isoformat(timespec="seconds"),
        )
    except Exception as exc:
        log.error("❌ [%s] Send error: %s", source, exc)

# ─────────────────────────────────────────────
# 5. Client
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ─────────────────────────────────────────────
# 6. Handler
# ─────────────────────────────────────────────
async def handle_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text    = (message.text or "").replace("*", "")
    source  = (getattr(event.chat, "username", None) or str(event.chat_id)).lower().strip("@")

    log.debug("[%s] Received: %s", source, text[:80])

    if not is_toss_message(text):
        return

    eh = _exact_hash(text)
    fh = _fuzzy_hash(text)

    async with _pending_lock:
        # Already seen → skip
        if eh in _seen_exact or fh in _seen_fuzzy:
            log.info("[%s] Duplicate skipped.", source)
            return

        # Claim hashes immediately so other sources are blocked
        _persist_hashes(eh, fh)

        is_priority = bool(PRIORITY_SOURCE and PRIORITY_SOURCE in source)

        if fh not in _pending:
            # First arrival for this toss
            _pending[fh] = {
                "first":    (text, message.entities, source),
                "priority": (text, message.entities, source) if is_priority else None,
            }
            # Start 2s window
            asyncio.create_task(_flush_pending(fh))
            log.info("[%s] Queued. Waiting 2s for priority source…", source)
        else:
            # Another source arrived within the 2s window
            if is_priority and not _pending[fh].get("priority"):
                _pending[fh]["priority"] = (text, message.entities, source)
                log.info("[%s] Priority source arrived — will use this.", source)
            else:
                log.info("[%s] Secondary source — ignored.", source)

# ─────────────────────────────────────────────
# 7. Main
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
    log.info("Sources     : %s", ", ".join(SOURCE_CHANS))
    log.info("Target      : %s", TARGET_CHAN)
    log.info("Priority src: %s", PRIORITY_SOURCE or "none set")
    log.info("Max msg len : %d chars", MAX_MSG_LEN)

    await client.start(phone=PHONE)

    # Fetch dialogs so Telethon caches all channels
    await client.get_dialogs(limit=200)
    await asyncio.sleep(2)

    # Resolve every source channel explicitly
    resolved = []
    for ch in SOURCE_CHANS:
        try:
            entity = await client.get_entity(ch)
            resolved.append(entity)
            log.info("✅ Listening to: %s", ch)
        except Exception as e:
            log.error("❌ Could not resolve %s: %s", ch, e)

    if not resolved:
        log.critical("No source channels resolved. Check SOURCE_CHANNELS in .env")
        return

    # Register handler after login with resolved entities
    client.add_event_handler(
        handle_new_message,
        events.NewMessage(chats=resolved)
    )

    log.info("🟢 Watching %d source(s). Whichever posts first wins (priority: %s).",
             len(resolved), PRIORITY_SOURCE or "none")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
