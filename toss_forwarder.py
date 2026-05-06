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

API_ID          = int(os.getenv("API_ID", "0"))
API_HASH        = os.getenv("API_HASH", "")
PHONE           = os.getenv("PHONE", "")
TARGET_CHAN     = os.getenv("TARGET_CHANNEL", "")
SESSION_NAME    = os.getenv("SESSION_NAME", "toss_session")
PRIORITY_SOURCE = os.getenv("PRIORITY_SOURCE", "").lower().strip("@")

_raw_sources = os.getenv("SOURCE_CHANNELS", os.getenv("SOURCE_CHANNEL", ""))
SOURCE_CHANS: list[str] = [s.strip() for s in _raw_sources.split(",") if s.strip()]

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
_seen_lock  = asyncio.Lock()

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
    """
    Extract a dedup key using ONLY team name + bat/bowl decision.
    Strips all emoji, flags, punctuation so that:
      '🇦🇫 SPEEN GHAR 🇦🇫 WON THE TOSS AND DECIDED TO BAT ✔️'
      '🏏 SPEEN GHAR 🏏 WON THE TOSS AND OPTED TO BAT FIRST ✓✓'
    Both produce the same key: 'speenghar|bat'
    """
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
    decision = decision_match.group(1)

    return f"{team}|{decision}"

# ─────────────────────────────────────────────
# 3. Output formatter
# ─────────────────────────────────────────────
def _format_output(original_text: str) -> str:
    """
    Always outputs:
      "🇹🇼 FIRE DRAGONS 🇹🇼" WON THE TOSS AND DECIDED TO BAT ✔️✔️

    - Team name extracted from source (with original emoji/flags)
    - Decision extracted from source (BAT or BOWL)
    - Everything else is fixed format — DECIDED TO, ✔️✔️
    """
    # Extract team name (everything before WON THE TOSS) from original
    team_match = re.search(r"^(.*?)\s+WON\s+THE\s+TOSS", original_text, re.IGNORECASE)
    if not team_match:
        return original_text  # fallback
    team_part = team_match.group(1).strip()

    # Extract decision (BAT or BOWL)
    decision_match = re.search(r"\b(BAT|BOWL)\b", original_text, re.IGNORECASE)
    if not decision_match:
        return original_text  # fallback
    decision = decision_match.group(1).upper()

    return f'"{team_part}" WON THE TOSS AND DECIDED TO {decision} ✔️✔️'

# ─────────────────────────────────────────────
# 4. Filter
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

    # Block long messages (bulk summaries / promos)
    if len(text.strip()) > MAX_MSG_LEN:
        log.debug("Blocked: too long (%d chars)", len(text.strip()))
        return False

    normalised = " ".join(text.split())

    # Block if more than one toss result
    if len(_TOSS_PHRASE.findall(normalised)) > 1:
        log.debug("Blocked: multiple toss results.")
        return False

    has_phrase   = bool(_TOSS_PHRASE.search(normalised))
    has_decision = bool(_DECISION.search(normalised))
    has_tick     = bool(_ENDING.search(text))
    has_first    = bool(re.search(r"\b(BAT|BOWL)\s+FIRST\b", normalised, re.IGNORECASE))

    return has_phrase and has_decision and (has_tick or has_first)

# ─────────────────────────────────────────────
# 5. Priority queue
#    Hold each toss for 2s.
#    If PRIORITY_SOURCE arrives in window → it wins.
#    Otherwise first-arrived wins.
#    Only ONE message ever sent per toss.
# ─────────────────────────────────────────────
_pending: dict = {}
_pending_lock = asyncio.Lock()

async def _flush_pending(toss_key: str) -> None:
    await asyncio.sleep(2)

    async with _pending_lock:
        entry = _pending.pop(toss_key, None)

    if not entry:
        return

    chosen = entry.get("priority") or entry.get("first")
    if not chosen:
        return

    original_text, source = chosen

    # Always format into clean fixed output
    formatted = _format_output(original_text)

    try:
        await client.send_message(
            entity=TARGET_CHAN,
            message=formatted,
        )
        log.info("✅ [%s] Sent: %s", source, formatted)
    except Exception as exc:
        log.error("❌ [%s] Send error: %s", source, exc)

# ─────────────────────────────────────────────
# 6. Client
# ─────────────────────────────────────────────
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ─────────────────────────────────────────────
# 7. Handler
# ─────────────────────────────────────────────
async def handle_new_message(event: events.NewMessage.Event) -> None:
    message = event.message
    text    = (message.text or "").replace("*", "")
    source  = (getattr(event.chat, "username", None) or str(event.chat_id)).lower().strip("@")

    log.debug("[%s] Received: %s", source, text[:80])

    if not is_toss_message(text):
        return

    toss_key = _toss_key(text)
    if not toss_key:
        log.debug("[%s] Could not extract toss key, skipping.", source)
        return

    log.debug("[%s] Toss key: %s", source, toss_key)

    async with _pending_lock:
        # Already seen this toss (team + decision) → skip
        if toss_key in _seen_keys:
            log.info("[%s] Duplicate skipped (key: %s).", source, toss_key)
            return

        is_priority = bool(PRIORITY_SOURCE and PRIORITY_SOURCE in source)

        if toss_key not in _pending:
            # First arrival — claim key and start 2s window
            _persist_key(toss_key)
            _pending[toss_key] = {
                "first":    (text, source),
                "priority": (text, source) if is_priority else None,
            }
            asyncio.create_task(_flush_pending(toss_key))
            log.info("[%s] Queued '%s'. Waiting 2s for priority source…", source, toss_key)
        else:
            # Arrived within the 2s window
            if is_priority and not _pending[toss_key].get("priority"):
                _pending[toss_key]["priority"] = (text, source)
                log.info("[%s] Priority source arrived for '%s'.", source, toss_key)
            else:
                log.info("[%s] Secondary source for '%s' — ignored.", source, toss_key)

# ─────────────────────────────────────────────
# 8. Main
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
    await client.get_dialogs(limit=200)
    await asyncio.sleep(2)

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

    client.add_event_handler(
        handle_new_message,
        events.NewMessage(chats=resolved)
    )

    log.info("🟢 Watching %d source(s). Priority: %s", len(resolved), PRIORITY_SOURCE or "none")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
