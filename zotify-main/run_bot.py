import os
import sys
from argparse import Namespace
from pathlib import Path
from zotify.bot import main
from zotify.config import Zotify
from zotify.termoutput import Printer, PrintChannel

if __name__ == "__main__":
    # Enable more detailed error output
    sys.tracebacklimit = None
    
    # Hardcode the token for testing
    os.environ['TELEGRAM_BOT_TOKEN'] = '8328270557:AAEBOt16pobxjpLaoW5ZIWGMQC6UxEMaRks'
    
    # Set up config location based on system platform
    system_paths = {
        'win32': str(Path.home() / 'AppData/Roaming/Zotify'),
        'linux': str(Path.home() / '.config/zotify'),
        'darwin': str(Path.home() / 'Library/Application Support/Zotify')
    }
    config_location = system_paths.get(sys.platform, str(Path.cwd() / '.zotify'))
    
    # Initialize Zotify with required arguments
    args = Namespace(
        debug=True,  # Enable debug mode to see what's happening
        config_location=config_location,
        update_config=False,
        no_splash=True,
        codec="mp3",  # Set default codec to mp3
        download_quality="high",  # Set quality to high
        username=None,  # Will use saved credentials if available
        token=None,
        urls=[],
        liked_songs=False,
        followed_artists=False,
        playlist=False,
        search=None,
        file_of_urls=None,
        verify_library=False
    )
    
    print("Starting Zotify Telegram Bot...")
    print("Initializing Zotify configuration...")
    print(f"Using config location: {config_location}")
    
    try:
        # Initialize Zotify configuration
        zotify = Zotify(args)
        
        # Check if we're logged in
        if not zotify.check_premium():
            print("Not logged in to Spotify or not a premium account.")
            print("Please run 'python -m zotify' first to set up your Spotify credentials.")
            sys.exit(1)
            
        print("Zotify initialized successfully!")
        print("Starting Telegram bot...")
        
        # Run the bot
        main()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"Error: {str(e)}")
        if args.debug:
            import traceback
            traceback.print_exc()