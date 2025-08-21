# bot.py
# Requires: python-telegram-bot>=20, ffmpy, Pillow, music_tag, tqdm, tabulate[widechars],
#           pkce, protobuf, pwinput, and your Zotify package (with the modules you shared).

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any, Callable

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# --- Zotify internals (used directly; no subprocess) ---
from argparse import Namespace
from librespot.audio.decoders import AudioQuality
from zotify.app import download_from_urls
from zotify.config import Zotify
from zotify.const import (
    TRACK, ALBUM, ARTIST, PLAYLIST, EPISODE, SHOW,
    TRACKS, ALBUMS, ARTISTS, PLAYLISTS, ITEMS, EXPLICIT, NAME, ID, OWNER, DISPLAY_NAME,
    SEARCH_URL
)
from zotify.utils import regex_input_for_urls

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("tg-zotify-bot")

# ---------------- Regex ----------------
SPOTIFY_URL_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:(track|album|playlist|artist|episode|show))/[A-Za-z0-9]+(?:\?.*)?$"
)

# ---------------- Per-chat queues ----------------
CHAT_QUEUES: Dict[int, asyncio.Queue] = {}
CHAT_WORKERS: set[int] = set()

async def _enqueue_chat_task(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_coro_factory: Callable[[], asyncio.Future]) -> int:
    """Enqueue a coroutine factory per chat. Returns queue position (1-based)."""
    if chat_id not in CHAT_QUEUES:
        CHAT_QUEUES[chat_id] = asyncio.Queue()
    queue: asyncio.Queue = CHAT_QUEUES[chat_id]
    await queue.put(task_coro_factory)
    position = queue.qsize()

    if chat_id not in CHAT_WORKERS:
        CHAT_WORKERS.add(chat_id)
        context.application.create_task(_chat_worker(chat_id))

    return position

async def _chat_worker(chat_id: int):
    queue = CHAT_QUEUES.get(chat_id)
    if not queue:
        CHAT_WORKERS.discard(chat_id)
        return
    try:
        while not queue.empty():
            coro_factory: Callable[[], asyncio.Future] = await queue.get()
            try:
                await coro_factory()
            except Exception:
                log.exception("Chat worker task failed")
            finally:
                queue.task_done()
    finally:
        CHAT_WORKERS.discard(chat_id)
        CHAT_QUEUES.pop(chat_id, None)

# ---------------- Zotify bootstrap helpers ----------------
def _build_args_for_zotify(
    urls: List[str],
    root_path: str,
    *,
    download_lyrics: bool = True,
    embed_lyrics: bool = True,
    codec: str = "mp3",
    quality: str = "auto",
    no_splash: bool = True,
    debug: bool = False
) -> Namespace:
    """
    Build an argparse.Namespace equivalent to running Zotify CLI with the same options.
    """
    ns = Namespace()
    # Positional-like
    ns.urls = urls

    # Top-level flags
    ns.config_location = None
    ns.username = None
    ns.token = None
    ns.no_splash = no_splash
    ns.debug = debug
    ns.update_config = False

    # Exclusive modes (we use URLs or search only)
    ns.liked_songs = False
    ns.followed_artists = False
    ns.playlist = False
    ns.search = None
    ns.file_of_urls = None
    ns.verify_library = False

    # Config values of interest
    ns.root_path = root_path                 # ROOT_PATH
    ns.download_format = codec               # --codec
    ns.download_quality = quality            # --download-quality
    ns.download_lyrics = str(download_lyrics)
    ns.md_save_lyrics = str(embed_lyrics)
    return ns


def _ensure_zotify_session(any_root_path: Path) -> None:
    """
    Make sure Zotify global session is initialized once. If credentials are not saved yet,
    the first run will prompt an OAuth URL in your server logs/console.
    """
    if getattr(Zotify, "SESSION", None) is not None:
        return
    args = _build_args_for_zotify(urls=[], root_path=str(any_root_path))
    Zotify(args)  # initializes config + logs in (or reuses saved credentials)


def _collect_new_files(root_dir: Path, since_ts: float) -> Tuple[List[Path], List[Path]]:
    """Return (audio_files, lyric_files) written/updated after since_ts."""
    audio_exts = {".mp3", ".ogg", ".opus", ".m4a", ".aac", ".flac", ".wav"}
    audio_files: List[Path] = []
    lyric_files: List[Path] = []

    for p in root_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < since_ts:
            continue
        if p.suffix.lower() in audio_exts:
            audio_files.append(p)
        elif p.suffix.lower() == ".lrc":
            lyric_files.append(p)

    audio_files.sort(key=lambda x: x.stat().st_mtime)
    lyric_files.sort(key=lambda x: x.stat().st_mtime)
    return audio_files, lyric_files


async def _run_zotify_download(urls: List[str], root_dir: Path) -> Tuple[List[Path], List[Path]]:
    """
    Initialize Zotify and run downloads off the event loop.
    Returns (audio_files, lyric_files) created/updated during this run.
    """
    root_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    def _work():
        args = _build_args_for_zotify(
            urls=urls,
            root_path=str(root_dir),
            download_lyrics=True,
            embed_lyrics=True,
            codec="mp3",
            quality="auto",
            no_splash=True,
            debug=False,
        )

        # Ensure single login; reuse session on subsequent commands
        if getattr(Zotify, "SESSION", None) is None:
            Zotify(args)
        else:
            Zotify.CONFIG.load(args)

        # Set runtime download quality similar to CLI
        quality_options = {
            'auto': AudioQuality.VERY_HIGH if Zotify.check_premium() else AudioQuality.HIGH,
            'normal': AudioQuality.NORMAL,
            'high': AudioQuality.HIGH,
            'very_high': AudioQuality.VERY_HIGH,
        }
        Zotify.DOWNLOAD_QUALITY = quality_options[Zotify.CONFIG.get_download_quality()]

        # Directly download URLs without re-running full client/login
        download_from_urls(urls)

    await asyncio.to_thread(_work)
    return _collect_new_files(root_dir, start)


# ---------------- Existing file fallback ----------------
def _norm_name_for_match(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())

def _find_existing_track_file(track_id: str, root_dir: Path) -> Tuple[Path | None, Path | None]:
    """Search .song_ids under root_dir to find an existing audio for track_id, and best matching .lrc."""
    try:
        for song_ids in root_dir.rglob('.song_ids'):
            try:
                with open(song_ids, 'r', encoding='utf-8') as f:
                    for line in f:
                        parts = line.strip().split('\t')
                        if not parts:
                            continue
                        if parts[0] == track_id and len(parts) >= 5:
                            filename = parts[4]
                            audio_path = song_ids.parent / filename
                            if audio_path.exists():
                                # Try to find matching lrc in same directory
                                stem_norm = _norm_name_for_match(audio_path.stem)
                                lrc_path = None
                                for lrc in song_ids.parent.glob('*.lrc'):
                                    if _norm_name_for_match(lrc.stem) == stem_norm:
                                        lrc_path = lrc
                                        break
                                return audio_path, lrc_path
            except Exception:
                continue
    except Exception:
        pass
    return None, None

async def _send_existing_if_available(url: str, root_dir: Path, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If the requested track already exists, send it immediately. Returns True if sent."""
    track_id, album_id, playlist_id, episode_id, show_id, artist_id = regex_input_for_urls(url)
    if track_id:
        audio_path, lrc_path = await asyncio.to_thread(_find_existing_track_file, track_id, root_dir)
        if audio_path and audio_path.exists():
            try:
                await context.bot.send_audio(chat_id=chat_id, audio=audio_path.open('rb'), caption=f"{audio_path.stem}")
                if lrc_path and lrc_path.exists():
                    await context.bot.send_document(chat_id=chat_id, document=lrc_path.open('rb'), caption="Lyrics (.lrc)")
                return True
            except Exception:
                log.exception("Failed sending existing file")
    return False

# ---------------- Zotify search (API) ----------------
def _zotify_search_api(query: str, limit: int = 12, types: List[str] | None = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Uses Zotify.invoke_url_with_params to search Spotify-like results via the configured session.
    Returns a dict: {"tracks": [...], "albums": [...], "artists": [...], "playlists": [...]}
    Each list contains simplified dicts with id, name, subtitle and type.
    """
    if types is None:
        types = [TRACK, ALBUM, ARTIST, PLAYLIST]

    # Ensure Zotify session exists (so we have auth headers)
    _ensure_zotify_session(Path("downloads") / "bootstrap")

    params = {
        "limit": str(limit),
        "offset": "0",
        "q": query,
        "type": ",".join(types)
    }

    resp = Zotify.invoke_url_with_params(SEARCH_URL, **params)

    results = {
        "tracks": [],
        "albums": [],
        "artists": [],
        "playlists": []
    }

    if TRACK in types:
        tracks = resp.get(TRACKS, {}).get(ITEMS, [])
        for t in tracks:
            explicit = " [E]" if t.get(EXPLICIT) else ""
            name = f"{t.get(NAME, '')}{explicit}"
            artists = ", ".join(a.get(NAME, "") for a in t.get(ARTISTS, []))
            results["tracks"].append({
                "type": TRACK,
                "id": t.get(ID),
                "name": name,
                "subtitle": artists
            })

    if ALBUM in types:
        albums = resp.get(ALBUMS, {}).get(ITEMS, [])
        for a in albums:
            artists = ", ".join(ar.get(NAME, "") for ar in a.get(ARTISTS, []))
            results["albums"].append({
                "type": ALBUM,
                "id": a.get(ID),
                "name": a.get(NAME, ""),
                "subtitle": artists
            })

    if ARTIST in types:
        artists = resp.get(ARTISTS, {}).get(ITEMS, [])
        for ar in artists:
            results["artists"].append({
                "type": ARTIST,
                "id": ar.get(ID),
                "name": ar.get(NAME, ""),
                "subtitle": "Artist"
            })

    if PLAYLIST in types:
        playlists = resp.get(PLAYLISTS, {}).get(ITEMS, [])
        for pl in playlists:
            owner = pl.get(OWNER, {}).get(DISPLAY_NAME, "")
            results["playlists"].append({
                "type": PLAYLIST,
                "id": pl.get(ID),
                "name": pl.get(NAME, ""),
                "subtitle": f"by {owner}" if owner else "Playlist"
            })

    return results


# ---------------- UI helpers ----------------
_TYPE_EMOJI = {
    TRACK: "🎵",
    ALBUM: "💿",
    ARTIST: "👤",
    PLAYLIST: "🎼",
}

def _build_search_keyboard(results: Dict[str, List[Dict[str, Any]]]) -> InlineKeyboardMarkup:
    """
    Create an inline keyboard with up to ~12 results (tracks first, then albums, artists, playlists).
    Each row is one button: "<emoji> Name — Subtitle" which triggers a callback to download.
    """
    rows: List[List[InlineKeyboardButton]] = []

    # Order: tracks -> albums -> artists -> playlists (cap total ~12)
    all_items: List[Dict[str, Any]] = []
    for key in ("tracks", "albums", "artists", "playlists"):
        all_items.extend(results.get(key, []))

    all_items = all_items[:12]
    for item in all_items:
        t = item["type"]
        emoji = _TYPE_EMOJI.get(t, "🎧")
        text = f"{emoji} {item['name']}"
        if item.get("subtitle"):
            text += f" — {item['subtitle']}"
        # callback_data must be short; include only what's necessary
        cb = f"get:{t}:{item['id']}"
        rows.append([InlineKeyboardButton(text=text[:64], callback_data=cb)])

    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([[InlineKeyboardButton("No results", callback_data="noop")]])


def _spotify_url_for(type_: str, id_: str) -> str:
    return f"https://open.spotify.com/{type_}/{id_}"


async def _send_audio_and_lyrics(chat_id: int, context: ContextTypes.DEFAULT_TYPE, audio_files: List[Path], lyric_files: List[Path]) -> int:
    """Send audio files (and matching .lrc if present). Returns count sent."""
    def _norm(name: str) -> str:
        # Normalize stems so underscores vs hyphens and spaces match
        return _norm_name_for_match(name)

    lrc_map = { _norm(p.stem): p for p in lyric_files }
    sent = 0
    for audio in audio_files:
        try:
            await context.bot.send_audio(chat_id=chat_id, audio=audio.open("rb"), caption=f"{audio.stem}")
            sent += 1
            norm_stem = _norm(audio.stem)
            lrc_path = lrc_map.get(norm_stem)
            if lrc_path is None:
                # Fallback: same directory, similar normalized name
                candidates = [p for p in lyric_files if p.parent == audio.parent and _norm(p.stem) == norm_stem]
                lrc_path = candidates[0] if candidates else None
            if lrc_path is not None:
                await context.bot.send_document(chat_id=chat_id, document=lrc_path.open("rb"), caption="Lyrics (.lrc)")
        except Exception as e:
            log.warning(f"Failed to send {audio}: {e}")
    return sent

# ---------------- Bot handlers ----------------

HELP_TEXT = """🎵 Maxxiey Bot

Use me to search and download Spotify content.

Commands:
• /dl <spotify_link> — download track/album/playlist/artist/episode/show
• /search <query> — inline results with buttons; tap to download

Notes:
• Files are MP3 by default; synced lyrics (.lrc) are sent when available.
• Large albums/playlists will take time and arrive as they finish."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Send a Spotify link, e.g. /dl https://open.spotify.com/track/…")
        return

    url = context.args[0].strip()
    if not SPOTIFY_URL_RE.match(url):
        await update.message.reply_text("Please send a valid Spotify link (track/album/playlist/artist/episode/show).")
        return

    chat_id = update.effective_chat.id
    root_dir = Path("downloads") / str(chat_id)

    async def job():
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        await update.message.reply_text("Working on it… 🎧 This may take a while for large items.")

        try:
            audio_files, lyric_files = await _run_zotify_download([url], root_dir)
        except Exception as e:
            log.exception("Download failed")
            # Fallback: if already exists, send it instead of failing
            if await _send_existing_if_available(url, root_dir, chat_id, context):
                await update.message.reply_text("Found in your library ✅ Sent existing file.")
                return
            await update.message.reply_text(f"Download failed: {e}")
            return

        if not audio_files:
            # Likely skipped as existing; try to send from library immediately
            if await _send_existing_if_available(url, root_dir, chat_id, context):
                await update.message.reply_text("Found in your library ✅ Sent existing file.")
                return
            await update.message.reply_text("I couldn't find any audio files after downloading. Try another link?")
            return

        sent = await _send_audio_and_lyrics(chat_id, context, audio_files, lyric_files)
        await update.message.reply_text(f"Done ✅ Sent {sent} file(s).")

    # Enqueue per-chat to avoid overlaps; inform if queued behind others
    position = await _enqueue_chat_task(context, chat_id, job)
    if position > 1:
        await update.message.reply_text(f"Queued ⏳ Position {position}. I'll start as soon as previous task finishes.")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <query>\nExample: /search never gonna give you up")
        return

    query = " ".join(context.args).strip()
    try:
        results = _zotify_search_api(query, limit=12)
    except Exception as e:
        log.exception("Search failed")
        await update.message.reply_text(f"Search failed: {e}")
        return

    kb = _build_search_keyboard(results)
    await update.message.reply_text(
        f"Search results for: {query}\nTap an item to download.",
        reply_markup=kb
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button taps: 'get:<type>:<id>' -> download that selection."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if data == "noop":
        return

    try:
        action, type_, id_ = data.split(":", 2)
    except Exception:
        await query.edit_message_text("Sorry, I didn't understand that button.")
        return

    if action != "get":
        await query.edit_message_text("Unknown action.")
        return

    # Build URL for Zotify based on type & id
    url = _spotify_url_for(type_, id_)
    chat_id = query.message.chat.id
    root_dir = Path("downloads") / str(chat_id)

    async def job():
        await query.edit_message_text(f"⏬ Downloading i'll send it here when done: {type_} …")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        try:
            audio_files, lyric_files = await _run_zotify_download([url], root_dir)
        except Exception as e:
            log.exception("Selection download failed")
            # Fallback to existing
            if await _send_existing_if_available(url, root_dir, chat_id, context):
                await context.bot.send_message(chat_id=chat_id, text="Found in your library ✅ Sent existing file.")
                return
            await query.edit_message_text(f"Download failed: {e}")
            return

        if not audio_files:
            # Try sending existing file
            if await _send_existing_if_available(url, root_dir, chat_id, context):
                await context.bot.send_message(chat_id=chat_id, text="Found in your library ✅ Sent existing file.")
                return
            await query.edit_message_text("No audio files were produced for that item.")
            return

        await query.edit_message_text("Sending files…")
        sent = await _send_audio_and_lyrics(chat_id, context, audio_files, lyric_files)
        await context.bot.send_message(chat_id=chat_id, text=f"Done ✅ Sent {sent} file(s).")

    position = await _enqueue_chat_task(context, chat_id, job)
    if position > 1:
        await context.bot.send_message(chat_id=chat_id, text=f"Queued ⏳ Position {position}. I'll start after the current task.")


# ---------------- Main ----------------

def main():
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable not set. "
            "Get your token from @BotFather and export it like:\n"
            "  export BOT_TOKEN='123456:ABC-DEF...'"
        )

    # Initialize Zotify session once at startup so subsequent commands skip login
    _ensure_zotify_session(Path("downloads") / "bootstrap")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dl", dl))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Bot is up. Press Ctrl+C to stop.")
    # Use default polling interval for snappy idle responsiveness
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()