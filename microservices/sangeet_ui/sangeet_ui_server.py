from flask import (
    Flask , render_template , session , redirect , jsonify , render_template_string , url_for , request
)
import os
import time # Retained for other uses if any, or can be removed if not
import threading # Added for directory monitor
from utils import setup_dlp
import secrets # Retained

from ytmusicapi import YTMusic

from database import database # For init_postgres_db
from routes import download_server_proxy

# Import directory monitor
from directory_monitor import start_monitoring # Added

def operation_main():
    database.init_postgres_db()

# Mutagen imports are removed as get_song_metadata is removed.
# from mutagen.mp3 import MP3
# from mutagen.mp4 import MP4
# from mutagen.easyid3 import EasyID3
# from mutagen.flac import FLAC

operation_main() # Initializes database, including local_songs table with genre

from interconnect.config import config
from routes import cdn
import logging
# from keygen import keygen # Retained if used by other parts
from functools import wraps # Retained

import psycopg2 # Retained if used directly, else can be reviewed
from database.database import get_pg_connection # Retained if used directly

# from var_templates import var_templates # Retained if used
from routes import auth
from routes import sangeet_home
from routes import sangeet_all
# from utils import util # Retained if used

server = Flask("sangeet_ui_server_main")

#register all blueprints
server.register_blueprint(cdn.bp)
server.register_blueprint(auth.bp)
server.register_blueprint(sangeet_all.bp)
server.register_blueprint(download_server_proxy.bp)
server.register_blueprint(sangeet_home.server)

#initialize ytmusic
ytmusic = YTMusic() # Retained

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


env_data_01 = config.get_env_data(os.path.join(os.getcwd() , "configs" , "ui" , "config.conf"))

# Securely load secret key
try:
    with open("key.txt" , "r") as md:
        key = md.read().strip() # Use strip to remove potential newlines
    if not key:
        logger.warning("key.txt is empty. Generating a new secret key.")
        key = secrets.token_hex(32)
        with open("key.txt", "w") as md_write:
            md_write.write(key)
    server.secret_key = key
except FileNotFoundError:
    logger.warning("key.txt not found. Generating a new secret key and saving to key.txt.")
    key = secrets.token_hex(32)
    try:
        with open("key.txt", "w") as md_write:
            md_write.write(key)
        server.secret_key = key
    except IOError as e:
        logger.error(f"Could not write key.txt: {e}. Session security will be compromised.")
        server.secret_key = secrets.token_hex(32) # Fallback to in-memory key for this session


sangeet_music_path_env = env_data_01.MUSIC_PATH
sangeet_local_songs_path_env = env_data_01.LOCAL_SONGS_PATH

raw_paths = []
if sangeet_music_path_env:
    raw_paths.extend(sangeet_music_path_env.split(';'))
if sangeet_local_songs_path_env:
    raw_paths.extend(sangeet_local_songs_path_env.split(';'))

unique_cleaned_paths = sorted(list(set([p.strip() for p in raw_paths if p.strip()])))

if not unique_cleaned_paths:
    logger.warning("No music directories configured via MUSIC_PATH or LOCAL_SONGS_PATH. Using default: ['/music', '/local_songs']")
    CONFIGURED_MUSIC_DIRS = ["/music", "/local_songs"] 
else:
    CONFIGURED_MUSIC_DIRS = unique_cleaned_paths
    logger.info(f"Configured music directories to scan: {CONFIGURED_MUSIC_DIRS}")

def task():

    # SCAN_INTERVAL and related old scanning functions are removed.


    # Database initialization is already called via operation_main() at the top.

    logger.info("Starting Sangeet UI Server...")

    # Start the directory monitor in a background thread
    logger.info(f"Preparing to start directory monitoring for: {CONFIGURED_MUSIC_DIRS}")
    monitor_thread = threading.Thread(
        target=start_monitoring, 
        args=(CONFIGURED_MUSIC_DIRS,), 
        daemon=True  # Daemon thread will exit when the main program exits
    )
    monitor_thread.start()
    logger.info("Directory monitoring thread has been initiated.")
    

  

if __name__ == "__main__":
  
   task()
   server.run(port=80 , host="localhost")