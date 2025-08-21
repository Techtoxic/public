import os
import sys
from pathlib import Path

# Add parent directory to path to import zotify
sys.path.append(str(Path(__file__).parent.parent))

from zotify.bot import ZotifyBot
from zotify.config import Zotify

def main():
    # Get bot token from environment variable
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set!")
        print("Please set it with: set TELEGRAM_BOT_TOKEN=your_bot_token_here (Windows)")
        print("or: export TELEGRAM_BOT_TOKEN=your_bot_token_here (Linux/MacOS)")
        return
    
    # Initialize Zotify configuration
    Zotify.load_config()
    
    # Create and run the bot
    print("Starting Zotify Telegram Bot...")
    bot = ZotifyBot(token)
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"Error running bot: {e}")

if __name__ == "__main__":
    main()