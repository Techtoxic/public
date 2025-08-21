import os
import shutil
import subprocess
import os
import logging
import sys
from pathlib import Path
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ChatAction
from zotify.utils import regex_input_for_urls
from zotify.config import CONFIG_VALUES
from zotify.const import *

logger = logging.getLogger(__name__)

class ZotifyBot:
    def __init__(self, token: str):
        self.application = Application.builder().token(token).build()
        self.setup_handlers()
        self.config = CONFIG_VALUES
        self.base_path = os.path.expanduser(self.config[ROOT_PATH]['default'])
        self.progress_messages = {}
    
    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("download", self.download_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Welcome to Zotify Bot! 🎵\n\n"
            "Commands:\n"
            "/download <spotify_url> - Download a track\n"
            "/help - Show this help message"
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /help command."""
        help_text = """
🎵 Welcome to Zotify Bot!

Available commands:
/start - Start the bot
/download <spotify_url> - Download music from Spotify
/help - Show this help message

Examples:
/download https://open.spotify.com/track/4HnKmBEIKEUSJp7r6OWR20
        """
        await update.message.reply_text(help_text)
        if message_id in self.progress_messages:
            try:
                await self.progress_messages[message_id].edit_text(f"⏳ {progress}")
            except:
                pass

    async def download_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /download command."""
        if not context.args:
            await update.message.reply_text("Please provide a Spotify URL. Example: /download <spotify_url>")
            return

        url = context.args[0]
        result = regex_input_for_urls(url)
        if not result or len(result) < 1:
            await update.message.reply_text("Invalid Spotify track URL!")
            return

        logger.info(f"Starting download for: {url}")
        await update.message.reply_text(f"🔍 Starting download for: {url}")
        
        # Create a unique download directory
        download_dir = Path(self.base_path) / f"downloads/{update.effective_user.id}_{update.message.message_id}"
        download_dir.mkdir(parents=True, exist_ok=True)
        
        # Send initial status
        import logging
        logger = logging.getLogger(__name__)
        status_message = await update.message.reply_text("🎵 Downloading track using zotify...", quote=True)
        
        try:
            # Try multiple approaches to find zotify
            zotify_cmd = None
            
            # Check if zotify command exists
            try:
                result = subprocess.run(["zotify", "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    zotify_cmd = "zotify"
                    logger.info("Using 'zotify' command")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            
            # Try python module approach
            if not zotify_cmd:
                zotify_cmd = [sys.executable, "-m", "zotify"]
                logger.info("Using 'python -m zotify' approach")
            
            cmd = [zotify_cmd, "--codec", "mp3", "--force-premium", url] if isinstance(zotify_cmd, str) else zotify_cmd + ["--codec", "mp3", "--force-premium", url]
            
            logger.info(f"Running command: {' '.join(cmd)}")
            logger.info(f"Working directory: {download_dir}")
            
            import time
            import sys
            import threading
            
            # Update status to show we're actually doing something
            await context.bot.edit_message_text(
                text=f"⚙️ Starting download process...",
                chat_id=update.effective_chat.id,
                message_id=status_message.message_id
            )
            
            process = subprocess.Popen(
                cmd,
                cwd=str(download_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # Stream output and monitor progress
            last_update_time = 0
            download_completed = False
            output_buffer = []
            
            logger.info("Starting subprocess monitoring...")
            
            # Read output with timeout handling
            while True:
                try:
                    line = process.stdout.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    
                    line = line.strip()
                    if line:
                        output_buffer.append(line)
                        logger.info(f"DOWNLOAD: {line}")
                        
                        # Update status message periodically (every 2 seconds for faster feedback)
                        current_time = time.time()
                        
                        progress_keywords = ["100%", "completed", "saved", "download", "converted", "finished"]
                        update_keywords = ["downloading", "progress", "converting", "getting", "processing"]
                        
                        if any(keyword in line.lower() for keyword in progress_keywords):
                            download_completed = True
                        
                        if any(keyword in line.lower() for keyword in update_keywords) or True:  # Show all output
                            if current_time - last_update_time >= 2:
                                try:
                                    truncated_line = line[:100] + "..." if len(line) > 100 else line
                                    await context.bot.edit_message_text(
                                        text=f"🔄 {truncated_line}",
                                        chat_id=update.effective_chat.id,
                                        message_id=status_message.message_id
                                    )
                                except Exception as e:
                                    logger.warning(f"Could not update status message: {e}")
                                last_update_time = current_time
                                
                except Exception as e:
                    logger.error(f"Error reading process output: {e}")
                    break
            
            logger.info("Subprocess completed, checking return code...")
            process.wait(timeout=30)  # Reduced timeout for faster error detection
            
            returncode = process.returncode
            logger.info(f"Process returned with code: {returncode}")
            
            if returncode != 0 or not download_completed:
                error_details = f"Process returned code: {returncode}\n"
                if output_buffer:
                    error_details += "Recent output:\n" + "\n".join(output_buffer[-10:])
                
                logger.error(f"Download failed: {error_details}")
                await context.bot.edit_message_text(
                    text=f"❌ Download failed (Code: {returncode})\n\nView console for details.",
                    chat_id=update.effective_chat.id,
                    message_id=status_message.message_id
                )
                raise RuntimeError(error_details)
            
            # Find the downloaded MP3 file
            downloaded_files = list(download_dir.glob("**/*.mp3"))
            if not downloaded_files:
                downloaded_files = list(download_dir.glob("*"))
                logger.error(f"No MP3 files found. Contents: {list(download_dir.iterdir())}")
                raise FileNotFoundError("No music files were downloaded")
            
            # Get the most recently downloaded file
            downloaded_file = max(downloaded_files, key=lambda x: x.stat().st_mtime)
            logger.info(f"Found downloaded file: {downloaded_file}")
            
            # Update status to success
            try:
                await context.bot.edit_message_text(
                    text="✅ Download completed! Sending file...",
                    chat_id=update.effective_chat.id,
                    message_id=status_message.message_id
                )
            except Exception:
                pass  # Ignore if edit fails
            
            # Send the file
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_AUDIO)
            
            with open(downloaded_file, 'rb') as audio_file:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_file,
                    reply_to_message_id=update.message.message_id,
                    title=downloaded_file.stem,
                    timeout=120
                )
            
            logger.info(f"Successfully sent file {downloaded_file.name}")
            await status_message.delete()
            
        except Exception as e:
            logger.error(f"Download failed: {str(e)}", exc_info=True)
            error_msg = f"❌ Download failed: {str(e)}"
            
            try:
                await context.bot.edit_message_text(
                    text=error_msg,
                    chat_id=update.effective_chat.id,
                    message_id=status_message.message_id
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=error_msg,
                    reply_to_message_id=update.message.message_id
                )
        
        finally:
            # Clean up the download directory
            if download_dir.exists():
                shutil.rmtree(download_dir)
                logger.info(f"Clean up complete: {download_dir}")
    
    def run(self):
        self.application.run_polling()

def main():
    # Get bot token from environment variable
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set!")
        return
    
    bot = ZotifyBot(token)
    bot.run()

if __name__ == "__main__":
    main()