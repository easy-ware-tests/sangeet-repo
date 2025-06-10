# util.py
# --- Top Imports ---
import base64
import json
import logging
import os
from pathlib import Path
import platform
import random
import re
from functools import lru_cache , wraps
import pytz
import  pytz
import secrets
import smtplib
import stat
import subprocess
import time # Keep for time.time()
from datetime import datetime, timedelta # Keep for datetime objects
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional # Added Optional
import requests
# import redis # REMOVE: Redis client not directly used for primary data store
from dotenv import load_dotenv
from flask import  session  , jsonify  , redirect , url_for , request # Keep Flask parts if used by utils called from routes
from mutagen import File # Keep for metadata extraction
from yt_dlp import YoutubeDL # Keep
from ytmusicapi import YTMusic # Keep

import logging

try:
    from  database.database import get_pg_connection
except ImportError:
    # Fallback for cases where util.py might be run standalone or structure is different
    # This requires database_pg.py to be in Python's path or same directory
    print("Warning: Could not import get_pg_connection using relative import '..database_pg'. Trying direct import.")
    try:
        from database.database import get_pg_connection
    except ImportError as e:
        print(f"FATAL: Could not import get_pg_connection. Ensure database_pg.py is accessible. Error: {e}")
        # Define a dummy function to prevent further import errors, but app will fail
        def get_pg_connection():
            raise ImportError("get_pg_connection is not available due to import issues.")

import psycopg2
import psycopg2.extras # For DictCursor
# --- End PostgreSQL Specific Imports ---


try:
    from helpers import time_helper # Assuming time_helper is independent of DB
    time_sync = time_helper.TimeSync()
except ImportError:
    print("time_helper module not found. Time synchronization features will be limited.")
    class TimeSync: # Basic fallback
        def get_current_time(self): return datetime.now()
        def parse_datetime(self, time_str):
            try: return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S.%f') # Try with microseconds
            except ValueError: return datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S') # Fallback
        def format_time(self, dt_obj, relative=False): # Renamed param
            if not isinstance(dt_obj, datetime): # Handle if it's already a string
                try: dt_obj = self.parse_datetime(str(dt_obj))
                except: return str(dt_obj) # Return as is if parsing fails
            if relative: return "relative time" # Placeholder
            return dt_obj.strftime('%Y-%m-%d %H:%M:%S')
    time_sync = TimeSync()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)




# --- Variables ---
# Ensure the path to .env is correct relative to where util.py is or where the app runs
dotenv_path = os.path.join(os.getcwd(), "configs", "ui" , "config.conf") # Path from original database_pg.py
if not os.path.exists(dotenv_path):
    # Fallback if the above path doesn't exist, maybe try relative to util.py's location
    util_dir = os.path.dirname(os.path.abspath(__file__))
    dotenv_path_alt = os.path.join(util_dir, "..", "configs", "ui", "config.conf") # Adjust if structure is different
    if os.path.exists(dotenv_path_alt):
        dotenv_path = dotenv_path_alt
    else:
        logger.warning(f"config.conf not found at primary or alternative path: {dotenv_path}, {dotenv_path_alt}")
load_dotenv(dotenv_path=dotenv_path)


# DB_PATH REMOVED (SQLite specific)
MUSIC_DIR = os.getenv("MUSIC_PATH", "music") # Ensure this is set and directory exists
if not os.path.isdir(MUSIC_DIR):
    logger.warning(f"MUSIC_DIR '{MUSIC_DIR}' does not exist or is not a directory. Downloads may fail.")
    # os.makedirs(MUSIC_DIR, exist_ok=True) # Optionally create it

FFMPEG_BIN_DIR = os.path.join(os.getcwd(), "ffmpeg", "bin") # For Windows yt-dlp
LOCAL_SONGS_PATHS = os.getenv("LOCAL_SONGS_PATHS", "") # Paths for user's own music files
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587")) # Ensure this is an int

CACHE_DURATION = 3600 # For non-DB caches like API results

# redis_client REMOVED
ytmusic = YTMusic() # Keep for YouTube Music API interactions
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO) # Configure logging in your main app setup

song_cache = {}     # Temporary cache for YTMusic API responses (get_song)
search_cache = {}   # Temporary cache for YTMusic search results
# lyrics_cache REMOVED (now in PostgreSQL)
local_songs = {}    # This will be populated from PostgreSQL by load_local_songs_from_db_util()



def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or 'session_token' not in session:
            return redirect(url_for('auth_ui_server.login', next=request.url)) # Pass next URL for redirection

        conn = None
        try:
            conn = get_pg_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT 1 FROM active_sessions
                    WHERE user_id = %s AND session_token = %s
                    AND expires_at > CURRENT_TIMESTAMP
                """, (session['user_id'], session['session_token']))
                valid_session = cur.fetchone()
            if not valid_session:
                session.clear()
                return redirect(url_for('auth_ui_server.login', error="Session expired or invalid.", next=request.url))
            return f(*args, **kwargs)
        except psycopg2.Error as e:
            logger.error(f"Database error in login_required: {e}")
            session.clear()
            return redirect(url_for('auth_ui_server.login', error="Database error, please try again."))
        finally:
            if conn:
                conn.close()
    return decorated_function



def setup_ytdlp():
    """
    Locates the 'driver' (yt-dlp executable) in the expected path:
    current_working_directory/drivers/yt-dlp/driver (or driver.exe on Windows).
    This function is meant to be called when util.py is loaded to set YTDLP_PATH.
    It relies on an external process/script (like the one run by sangeet_ui_server-1 startup)
    to have already downloaded the driver.
    Returns: tuple: (executable_path: str | None, version_path: str | None)
    """
    logger.info("[util.py setup_ytdlp] Initializing YTDLP_PATH check...")
    current_system = platform.system().lower()
    
    # Define the base name for the local executable
    # This should match how the downloader script saves it.
    local_executable_base_name = "driver" 
    
    local_driver_filename = local_executable_base_name
    if current_system == "windows":
        local_driver_filename = f"{local_executable_base_name}.exe"
    
    try:
        # Construct the path based on the current working directory (cwd)
        # The downloader script also uses cwd/drivers/yt-dlp/
        # It's crucial that os.getcwd() is consistent.
        # Logs indicate the downloader placed it in /sangeet-v4/drivers/yt-dlp/driver
        cwd_path = Path(os.getcwd())
        driver_dir = cwd_path / "drivers" / "yt-dlp"
        executable_path = driver_dir / local_driver_filename

        logger.info(f"[util.py setup_ytdlp] Current Working Directory (os.getcwd()): {cwd_path.resolve()}")
        logger.info(f"[util.py setup_ytdlp] Attempting to locate driver at resolved path: {executable_path.resolve()}")
        logger.info(f"[util.py setup_ytdlp] Looking for filename: {local_driver_filename}")

        if executable_path.exists() and executable_path.is_file():
            resolved_path_str = str(executable_path.resolve())
            logger.info(f"[util.py setup_ytdlp] SUCCESS: Found local driver at: {resolved_path_str}")
            # YTDLP_VERSION_PATH is not strictly needed by download_flac_pg if YTDLP_PATH is set.
            # The version.txt is primarily for the downloader script's update logic.
            return resolved_path_str, None 
        else:
            if not executable_path.exists():
                logger.error(f"[util.py setup_ytdlp] FAILURE: Driver NOT FOUND at expected path: {executable_path.resolve()}")
            elif not executable_path.is_file():
                logger.error(f"[util.py setup_ytdlp] FAILURE: Path exists but is not a file: {executable_path.resolve()}")
            
            logger.error("[util.py setup_ytdlp] YTDLP_PATH will be set to None. FLAC downloads will fail.")
            return None, None

    except Exception as e:
        logger.error(f"[util.py setup_ytdlp] CRITICAL ERROR during driver path check: {e}", exc_info=True)
        return None, None


YTDLP_PATH , YTDLP_VERSION_PATH = setup_ytdlp()


def generate_session_id(): # Unchanged
    """Generate a unique session ID for grouping played songs."""
    return f"session_{int(time.time())}_{secrets.token_hex(4)}" # Added randomness



# # --- PostgreSQL specific utility to load local songs into the global var ---
# def load_local_songs_from_db_util():
#     """
#     Loads songs from PostgreSQL 'local_songs' table into the global `local_songs` dictionary.
#     This is the util.py version, similar to the one in routes.py.
#     """
#     global local_songs # The global dictionary in util.py
#     logger.info("Util: Attempting to load/reload local songs from PostgreSQL...")
#     temp_cache = {}
#     conn = None
#     try:
#         conn = get_pg_connection()
#         with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
#             cur.execute("SELECT id, title, artist, album, path, thumbnail, duration FROM local_songs")
#             rows = cur.fetchall()
#             for row in rows:
#                 if row['path'] and os.path.exists(row['path']): # File existence check
#                     temp_cache[row['id']] = {
#                         "id": row['id'],
#                         "title": row['title'] or "Unknown Title",
#                         "artist": row['artist'] or "Unknown Artist",
#                         "album": row['album'] or "Unknown Album",
#                         "path": row['path'],
#                         "thumbnail": row['thumbnail'], # Assumed to be URL or base64 string
#                         "duration": int(row['duration']) if row['duration'] is not None else 0
#                     }
#                 else:
#                     logger.warning(f"Util: Local song DB entry {row['id']} path {row.get('path')} invalid/missing. Skipping.")
#         local_songs = temp_cache # Atomically update global
#         logger.info(f"Util: Loaded {len(local_songs)} songs from 'local_songs' PG table.")
#     except psycopg2.Error as e:
#         logger.error(f"Util: PostgreSQL error loading local songs: {e}")
#     except Exception as e:
#         logger.error(f"Util: Unexpected error loading local songs: {e}", exc_info=True)
#     finally:
#         if conn:
#             conn.close()
#     return local_songs # Return for immediate use if needed



def load_local_songs_from_db_util():
    """
    Loads songs from PostgreSQL 'local_songs' table into the global `local_songs` dictionary.
    This is the util.py version, similar to the one in routes.py.
    """
    global local_songs 
    logger.info("Util: Attempting to load/reload local songs from PostgreSQL...")
    temp_cache = {}
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: # type: ignore
            cur.execute("SELECT id, title, artist, album, path, thumbnail, duration FROM local_songs")
            rows = cur.fetchall()
            for row in rows:
                if row['path'] and os.path.exists(row['path']): 
                    temp_cache[row['id']] = {
                        "id": row['id'],
                        "title": row['title'] or "Unknown Title",
                        "artist": row['artist'] or "Unknown Artist",
                        "album": row['album'] or "Unknown Album",
                        "path": row['path'],
                        "thumbnail": row['thumbnail'], 
                        "duration": int(row['duration']) if row['duration'] is not None else 0
                    }
                else:
                    logger.warning(f"Util: Local song DB entry {row['id']} path {row.get('path')} invalid/missing. Skipping.")
        local_songs = temp_cache 
        logger.info(f"Util: Loaded {len(local_songs)} songs from 'local_songs' PG table.")
    except psycopg2.Error as e: # type: ignore
        logger.error(f"Util: PostgreSQL error loading local songs: {e}")
    except Exception as e:
        logger.error(f"Util: Unexpected error loading local songs: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
    return local_songs 


load_local_songs_from_db_util()

# def record_song_pg(song_id: str, user_id: int, client_timestamp_str: Optional[str] = None):
#     """
#     Record song play into PostgreSQL 'user_play_queue_history' and update 'user_statistics'.
#     Ensures all datetime operations are timezone-aware (UTC).
#     """
#     conn = None
#     try:
#         # 1. Determine played_at time, ensuring it's timezone-aware (UTC)
#         played_at_dt = datetime.now(pytz.utc) # Default to current server time in UTC

#         if client_timestamp_str:
#             try:
#                 # Clean the timestamp string: remove 'Z' and replace with UTC offset
#                 if client_timestamp_str.endswith('Z'):
#                     client_timestamp_str = client_timestamp_str[:-1] + "+00:00"
                
#                 # Parse the ISO format string. This will be an aware datetime if offset is present.
#                 parsed_client_time = datetime.fromisoformat(client_timestamp_str)
                
#                 # If it's already timezone-aware, convert to UTC.
#                 # If it's naive, assume it was intended to be UTC (log this assumption).
#                 if parsed_client_time.tzinfo is not None and \
#                    parsed_client_time.tzinfo.utcoffset(parsed_client_time) is not None:
#                     played_at_dt = parsed_client_time.astimezone(pytz.utc)
#                     logger.debug(f"Client timestamp {client_timestamp_str} parsed and converted to UTC: {played_at_dt}")
#                 else:
#                     # This case means datetime.fromisoformat() produced a naive datetime
#                     # (e.g., "2023-10-27T10:30:00" without offset)
#                     logger.warning(
#                         f"Client timestamp '{client_timestamp_str}' was parsed as naive datetime. "
#                         f"Localizing to UTC. Ensure client sends timezone-aware timestamps or UTC."
#                     )
#                     played_at_dt = pytz.utc.localize(parsed_client_time)
#             except ValueError as ve:
#                 logger.warning(
#                     f"Invalid client_timestamp format: '{client_timestamp_str}'. Error: {ve}. "
#                     f"Using server's current UTC time: {played_at_dt}"
#                 )
#             except Exception as e_ts: # Catch any other parsing/conversion errors
#                  logger.error(
#                     f"Error processing client_timestamp '{client_timestamp_str}': {e_ts}. "
#                     f"Using server's current UTC time: {played_at_dt}", exc_info=True
#                 )


#         # 2. Fetch song metadata (title, artist, thumbnail)
#         title, artist, thumbnail_url = None, None, None
#         media_info_rs = get_media_info_util(song_id)
#         if media_info_rs:
#             title = media_info_rs.get('title')
#             artist = media_info_rs.get('artist')
#             thumbnail_url = media_info_rs.get('thumbnail')
#         else:
#             logger.warning(f"Could not retrieve media info for song_id: {song_id}. "
#                            f"History record will have nulls for title/artist/thumbnail.")

#         # 3. Database operations
#         conn = get_pg_connection()
#         with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
#             cur.execute("""
#                 SELECT session_id, sequence_number, played_at 
#                 FROM user_play_queue_history
#                 WHERE user_id = %s 
#                 ORDER BY played_at DESC 
#                 LIMIT 1
#             """, (user_id,))
#             last_entry = cur.fetchone()

#             session_id_val = generate_session_id()
#             sequence_number_val = 1

#             if last_entry:
#                 last_played_at_from_db = last_entry['played_at'] # This is the value from DB

#                 # Ensure last_played_at_from_db becomes an aware, UTC datetime
#                 if last_played_at_from_db.tzinfo is None or \
#                    last_played_at_from_db.tzinfo.utcoffset(last_played_at_from_db) is None:
#                     # If naive, assume it's UTC (based on how your DB stores it or server default) and make it aware
#                     logger.warning(
#                         f"Timestamp for last_entry['played_at'] (value: {last_played_at_from_db}) "
#                         f"from database is naive. Assuming it represents UTC and localizing."
#                     )
#                     last_played_at_utc = pytz.utc.localize(last_played_at_from_db)
#                 else:
#                     # If already aware, ensure it's in UTC for consistent comparison
#                     last_played_at_utc = last_played_at_from_db.astimezone(pytz.utc)
                
#                 # Now, played_at_dt is also guaranteed to be UTC aware from earlier logic.
#                 time_difference_seconds = (played_at_dt - last_played_at_utc).total_seconds()
                
#                 if time_difference_seconds >= 0 and time_difference_seconds < 3600: # 1 hour session continuity
#                     session_id_val = last_entry['session_id']
#                     sequence_number_val = last_entry['sequence_number'] + 1
#                 else:
#                     logger.info(f"Starting new play session for user {user_id}. "
#                                 f"Time since last play: {time_difference_seconds:.2f}s.")
#             else:
#                 logger.info(f"First play record for user {user_id} or no previous session found.")

#             cur.execute("""
#                 INSERT INTO user_play_queue_history
#                 (user_id, song_id, session_id, sequence_number, played_at, title, artist, thumbnail)
#                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
#             """, (user_id, song_id, session_id_val, sequence_number_val, played_at_dt, title, artist, thumbnail_url))

#             cur.execute("""
#                 INSERT INTO user_statistics (user_id, total_plays, last_played_at)
#                 VALUES (%s, 1, %s)
#                 ON CONFLICT (user_id) DO UPDATE SET
#                     total_plays = user_statistics.total_plays + 1,
#                     last_played_at = EXCLUDED.last_played_at
#             """, (user_id, played_at_dt))
            
#             conn.commit()
#             logger.info(f"Successfully recorded play for user {user_id}, song {song_id} at {played_at_dt} (UTC). Session: {session_id_val}, Seq: {sequence_number_val}")

#     except psycopg2.Error as db_err:
#         logger.error(f"PostgreSQL Error recording song play for user {user_id}, song {song_id}: {db_err}", exc_info=True)
#         if conn:
#             try:
#                 conn.rollback()
#             except psycopg2.Error as rb_err:
#                 logger.error(f"Error during rollback: {rb_err}")
#     except Exception as e:
#         logger.error(f"General Error recording song play for user {user_id}, song {song_id}: {e}", exc_info=True)
#         if conn: 
#             try:
#                 conn.rollback()
#             except psycopg2.Error as rb_err:
#                 logger.error(f"Error during rollback on general exception: {rb_err}")
#     finally:
#         if conn:
#             conn.close()

def record_song_pg(song_id: str, user_id: int, client_timestamp_str: Optional[str] = None):
    """
    Record song play into PostgreSQL 'user_play_queue_history' and update 'user_statistics'.
    Ensures all datetime operations are timezone-aware (UTC).
    """
    conn = None
    try:
        # 1. Determine played_at time, ensuring it's timezone-aware (UTC)
        played_at_dt = datetime.now(pytz.utc) # Default to current server time in UTC

        if client_timestamp_str:
            try:
                # ... (Your existing, correct timestamp handling code) ...
                if client_timestamp_str.endswith('Z'):
                    client_timestamp_str = client_timestamp_str[:-1] + "+00:00"
                parsed_client_time = datetime.fromisoformat(client_timestamp_str)
                if parsed_client_time.tzinfo is not None and \
                   parsed_client_time.tzinfo.utcoffset(parsed_client_time) is not None:
                    played_at_dt = parsed_client_time.astimezone(pytz.utc)
                else:
                    logger.warning(
                        f"Client timestamp '{client_timestamp_str}' was parsed as naive datetime. "
                        f"Localizing to UTC. Ensure client sends timezone-aware timestamps or UTC."
                    )
                    played_at_dt = pytz.utc.localize(parsed_client_time)
            except Exception as e_ts:
                 logger.error(
                    f"Error processing client_timestamp '{client_timestamp_str}': {e_ts}. "
                    f"Using server's current UTC time: {played_at_dt}", exc_info=True
                )

        # 2. Fetch song metadata
        title, artist, thumbnail_url = None, None, None
        media_info_rs = get_media_info_util(song_id)
        if media_info_rs:
            title = media_info_rs.get('title')
            artist = media_info_rs.get('artist')
            thumbnail_url = media_info_rs.get('thumbnail')
        else:
            logger.warning(f"Could not retrieve media info for song_id: {song_id}. "
                           f"History record will have nulls for title/artist/thumbnail.")

        # 3. Database operations
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT session_id, sequence_number, played_at
                FROM user_play_queue_history
                WHERE user_id = %s
                ORDER BY played_at DESC
                LIMIT 1
            """, (user_id,))
            last_entry = cur.fetchone()

            session_id_val = generate_session_id()
            sequence_number_val = 1

            if last_entry:
                last_played_at_from_db = last_entry['played_at']
                if last_played_at_from_db.tzinfo is None:
                    last_played_at_utc = pytz.utc.localize(last_played_at_from_db)
                else:
                    last_played_at_utc = last_played_at_from_db.astimezone(pytz.utc)

                time_difference_seconds = (played_at_dt - last_played_at_utc).total_seconds()

                if time_difference_seconds >= 0 and time_difference_seconds < 3600:
                    session_id_val = last_entry['session_id']
                    # *** THIS IS THE FIX ***
                    # Check if sequence_number exists before incrementing it.
                    if last_entry['sequence_number'] is not None:
                        sequence_number_val = last_entry['sequence_number'] + 1
                    else:
                        # If it's None, this is the first sequence of the session.
                        sequence_number_val = 1
                else:
                    logger.info(f"Starting new play session for user {user_id}. "
                                f"Time since last play: {time_difference_seconds:.2f}s.")
            else:
                logger.info(f"First play record for user {user_id} or no previous session found.")

            # ... (Your existing, correct database insertion and commit code) ...
            cur.execute("""
                INSERT INTO user_play_queue_history
                (user_id, song_id, session_id, sequence_number, played_at, title, artist, thumbnail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, song_id, session_id_val, sequence_number_val, played_at_dt, title, artist, thumbnail_url))

            cur.execute("""
                INSERT INTO user_statistics (user_id, total_plays, last_played_at)
                VALUES (%s, 1, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    total_plays = user_statistics.total_plays + 1,
                    last_played_at = EXCLUDED.last_played_at
            """, (user_id, played_at_dt))

            conn.commit()
            logger.info(f"Successfully recorded play for user {user_id}, song {song_id} at {played_at_dt} (UTC). Session: {session_id_val}, Seq: {sequence_number_val}")

    except psycopg2.Error as db_err:
        logger.error(f"PostgreSQL Error recording song play for user {user_id}, song {song_id}: {db_err}", exc_info=True)
        if conn: conn.rollback()
    except Exception as e:
        logger.error(f"General Error recording song play for user {user_id}, song {song_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()

def record_download_pg(video_id: str, title: str, artist: str, album: Optional[str], path: str, user_id: int):
    """Store a downloaded track with user association in PostgreSQL 'user_downloads' table."""
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_downloads (user_id, video_id, title, artist, album, path, downloaded_at)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, video_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    artist = EXCLUDED.artist,
                    album = EXCLUDED.album,
                    path = EXCLUDED.path,
                    downloaded_at = CURRENT_TIMESTAMP 
            """, (user_id, video_id, title, artist, album, path))
            # The ON CONFLICT DO UPDATE might be too aggressive if path changes but metadata shouldn't.
            # A simpler ON CONFLICT DO NOTHING might be safer if you only want to record the first download.
            # Or, specify which columns to update more carefully.
            # For now, it updates everything if (user_id, video_id) exists.
            conn.commit()
        logger.info(f"Recorded/Updated download for user {user_id}, video {video_id} in PG.")
    except psycopg2.Error as e:
        logger.error(f"PG Error recording download for user {user_id}, video {video_id}: {e}")
        if conn: conn.rollback()
    except Exception as e:
        logger.error(f"General Error recording download for user {user_id}, video {video_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    finally:
        if conn: conn.close()


def is_potential_video_id(text_id: str) -> bool: # Renamed param, type hint
    """Check if a string might be a YouTube video ID (11 chars, specific charset)."""
    if text_id.startswith("local-"): text_id = text_id[6:] # Strip prefix if present
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{11}', text_id)) # Use fullmatch
def load_local_songs_util():
    """
    Scans specified local directories AND the music download directory for music files,
    populates the `local_songs` global dictionary in util.py,
    and updates the `local_songs` PostgreSQL table.
    This is a more comprehensive version that syncs FS to PG DB and then to in-memory.
    """
    global local_songs 
    
    paths_to_scan = set()
    if LOCAL_SONGS_PATHS:
        for path_str in LOCAL_SONGS_PATHS.split(";"):
            abs_path = os.path.abspath(path_str.strip())
            if os.path.isdir(abs_path): paths_to_scan.add(abs_path)
            else: logger.warning(f"LOCAL_SONGS_PATHS item not a dir: {abs_path}")
    
    music_path_abs_scan = os.path.abspath(MUSIC_DIR) 
    if os.path.isdir(music_path_abs_scan): paths_to_scan.add(music_path_abs_scan)
    else: logger.warning(f"MUSIC_DIR for downloads not found: {music_path_abs_scan}")

    if not paths_to_scan:
        logger.warning("No valid directories to scan for local/downloaded songs.")
        local_songs = {} 
        return {}
    
    logger.info(f"Starting scan of directories for local songs: {list(paths_to_scan)}")
    
    # Updated file extensions
    file_exts = {
        ".mp3", ".flac", ".m4a", ".wav", ".ogg", ".wma", 
        ".aac", ".opus", ".aiff", ".oga", ".alac"
    }
    found_on_fs: Dict[str, Dict[str, Any]] = {} # type: ignore

    for scan_dir in paths_to_scan:
        is_main_download_dir = (scan_dir == music_path_abs_scan)
        logger.info(f"Scanning directory: {scan_dir}") # Verbose logging
        for root, _, files in os.walk(scan_dir):
            logger.info(f"Walking root: {root}, Found {len(files)} files initially.") # Verbose logging
            for fname in files:
                original_fname_for_log = fname # For logging original name
                logger.info(f"Processing filename from os.walk: '{original_fname_for_log}'") # Verbose logging
                try:
                    file_name_str, ext = os.path.splitext(fname)
                    ext_lower = ext.lower()
                    logger.info(f"  Extracted for '{original_fname_for_log}': name='{file_name_str}', ext='{ext}', lower_ext='{ext_lower}'") # Verbose logging

                    if ext_lower not in file_exts:
                        logger.info(f"  Skipping '{original_fname_for_log}': extension '{ext_lower}' not in {file_exts}") # Verbose logging
                        continue
                    logger.info(f"  Keeping '{original_fname_for_log}' for further processing.") # Verbose logging

                    full_path = os.path.abspath(os.path.join(root, fname))
                    base_name_no_ext = os.path.splitext(fname)[0] 

                    song_id_fs = None 
                    if is_main_download_dir and is_potential_video_id(base_name_no_ext):
                        song_id_fs = base_name_no_ext 
                    else: 
                        safe_id_base = re.sub(r'[^\w\-.]+', '_', base_name_no_ext) 
                        song_id_fs = f"local-{safe_id_base}" 
                    
                    title_fs, artist_fs, album_fs, duration_fs, thumbnail_fs = base_name_no_ext, "Unknown Artist", "Unknown Album", 0, "" 
                    try:
                        audio = File(full_path, easy=True)
                        if audio:
                            if audio.info and hasattr(audio.info, 'length'): duration_fs = int(audio.info.length)
                            title_fs = audio.get('title', [title_fs])[0]
                            artist_fs = audio.get('artist', [artist_fs])[0]
                            album_fs = audio.get('album', [album_fs])[0]
                            # Thumbnail logic could be enhanced if needed
                            if hasattr(audio, 'pictures') and audio.pictures: # type: ignore
                                 pic = audio.pictures[0] # type: ignore
                                 thumbnail_fs = f"data:{pic.mime};base64,{base64.b64encode(pic.data).decode('utf-8')}"
                    except Exception as e_meta: 
                        logger.error(f"Metadata error for {full_path} ('{original_fname_for_log}'): {e_meta}")
                    
                    found_on_fs[full_path] = {
                        "id": song_id_fs, "title": title_fs, "artist": artist_fs, "album": album_fs,
                        "path": full_path, "thumbnail": thumbnail_fs, "duration": duration_fs
                    }
                except Exception as e_fname_proc:
                    logger.error(f"  Error processing filename itself '{original_fname_for_log}': {e_fname_proc}", exc_info=True)
                    continue # Skip this file and continue with the next

    conn_lls = None 
    try:
        conn_lls = get_pg_connection()
        with conn_lls.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_lls: # type: ignore
            cur_lls.execute("SELECT id, path FROM local_songs")
            db_songs = {row['path']: row['id'] for row in cur_lls.fetchall()} 

            songs_to_upsert_tuples = [] 
            for path_fs, meta_fs in found_on_fs.items(): 
                songs_to_upsert_tuples.append(
                    (meta_fs['id'], meta_fs['title'], meta_fs['artist'], meta_fs['album'],
                     meta_fs['path'], meta_fs['thumbnail'], meta_fs['duration'])
                )

            if songs_to_upsert_tuples:
                # Using ON CONFLICT (id) as 'id' is PRIMARY KEY.
                # If 'path' should be the unique identifier for conflict, adjust schema and query.
                # Assuming 'id' (derived from filename/YT ID) is the intended stable PK for local songs.
                upsert_sql_on_id = """
                    INSERT INTO local_songs (id, title, artist, album, path, thumbnail, duration)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        artist = EXCLUDED.artist,
                        album = EXCLUDED.album,
                        path = EXCLUDED.path, 
                        thumbnail = EXCLUDED.thumbnail,
                        duration = EXCLUDED.duration;
                """
                psycopg2.extras.execute_batch(cur_lls, upsert_sql_on_id, songs_to_upsert_tuples) # type: ignore
                logger.info(f"Upserted {len(songs_to_upsert_tuples)} songs into PostgreSQL 'local_songs'.")

            db_paths = set(db_songs.keys())
            fs_paths = set(found_on_fs.keys())
            paths_to_delete_from_db = db_paths - fs_paths
            if paths_to_delete_from_db:
                delete_path_tuples = [(p,) for p in paths_to_delete_from_db]
                cur_lls.executemany("DELETE FROM local_songs WHERE path = %s", delete_path_tuples) # type: ignore
                logger.info(f"Removed {len(paths_to_delete_from_db)} songs from 'local_songs' DB not found on filesystem.")
            
            conn_lls.commit()

            cur_lls.execute("SELECT id, title, artist, album, path, thumbnail, duration FROM local_songs")
            reloaded_songs_from_db = {} 
            for row_db_reload in cur_lls.fetchall(): 
                 reloaded_songs_from_db[row_db_reload['id']] = dict(row_db_reload)
            local_songs = reloaded_songs_from_db 
            logger.info(f"Util: Synced and reloaded {len(local_songs)} entries into in-memory local_songs cache from PG.")

    except psycopg2.Error as e_lls_db: # type: ignore
        logger.error(f"Util: PostgreSQL error during local songs sync: {e_lls_db}")
        if conn_lls: conn_lls.rollback()
    except Exception as e_lls_gen: 
        logger.error(f"Util: Unexpected error during local songs sync: {e_lls_gen}", exc_info=True)
        if conn_lls: conn_lls.rollback()
    finally:
        if conn_lls: conn_lls.close()
    return local_songs 

# Call it once when util.py is loaded to populate `local_songs`
load_local_songs_util()


def get_song_info_util(song_id: str) -> Optional[Dict[str, Any]]: # Renamed to avoid conflict
    """Internal util to get song info (local from cache or YT)."""
    global local_songs # Use the util's global
    if song_id.startswith("local-"):
        return local_songs.get(song_id)
    else: # YouTube song
        if song_id in song_cache: # Use temporary API cache
            return song_cache[song_id]
        try:
            info = ytmusic.get_song(song_id)
            if info and info.get("videoDetails"):
                vd = info["videoDetails"]
                processed_info = {
                    "id": song_id, "video_id": song_id,
                    "title": vd.get("title", "Unknown Title"),
                    "artist": vd.get("author", "Unknown Artist"),
                    "album": info.get("album", {}).get("name", "Unknown Album"),
                    "thumbnail": (max(vd.get("thumbnail", {}).get("thumbnails", []), key=lambda x:x.get("width",0))["url"]
                                  if vd.get("thumbnail", {}).get("thumbnails") else f"https://i.ytimg.com/vi/{song_id}/hqdefault.jpg"),
                    "duration": int(vd.get("lengthSeconds", 0))
                }
                song_cache[song_id] = processed_info # Cache it
                return processed_info
            return None
        except Exception as e:
            logger.error(f"Util: Error fetching YT song info for {song_id}: {e}")
            return None


# get_media_info_util: A version of get_media_info for use within util.py to avoid circular imports if routes.py also has one.
def get_media_info_util(media_id: str) -> Optional[Dict[str, Any]]:
    """
    Util-specific version to get media details. 'local-' from util's global `local_songs`, else YouTube.
    """
    global local_songs # Use the util.py's global local_songs dictionary
    if not local_songs: # Attempt to load if empty (e.g., if direct util call before app init)
        logger.warning("Util's local_songs cache empty in get_media_info_util, attempting one-time load.")
        load_local_songs_from_db_util()


    if media_id.startswith("local-"):
        details = local_songs.get(media_id)
        if details:
            # Ensure 'video_id' for consistency if templates expect it
            return {**details, "video_id": media_id}
        else: # Not found in our current local_songs cache
            logger.warning(f"Util: Local song {media_id} not found in util's local_songs cache.")
            return {"id": media_id, "video_id": media_id, "title": "Unknown Local Song (Util)",
                    "artist": "N/A", "thumbnail": "", "duration": 0} # Basic fallback
    else: # Assume YouTube ID
        return get_song_info_util(media_id) # Use the util's YT fetcher


def get_fallback_recommendations_util(): # Renamed
    """Util version of fallback recommendations."""
    # ... (Same logic as fallback_recommendations from routes.py, but returns list of dicts, not jsonify) ...
    # This should call ytmusic API directly.
    try:
        # Try popular music first
        results = ytmusic.search("popular music", filter="songs", limit=5) # type: ignore
        if not results: results = ytmusic.search("trending songs", filter="songs", limit=5) # type: ignore
        if not results: return []

        recs = []
        for track in results: # type: ignore
            if not track.get("videoId"): continue
            recs.append({
                "id": track["videoId"], "title": track["title"],
                "artist": track["artists"][0]["name"] if track.get("artists") else "Unknown",
                "thumbnail": f"https://i.ytimg.com/vi/{track['videoId']}/hqdefault.jpg",
                "duration": track.get("duration_seconds",0)
            })
            if len(recs) >= 5: break
        return recs
    except Exception as e:
        logger.error(f"Util: Fallback recommendations error: {e}")
        return []



def sanitize_filename(s_param: str) -> str: # Renamed param, type hint
    """Remove invalid characters from a string to make it a safe filename component."""
    if not s_param: return 'unknown_track' # Handle empty input
    s_param = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s_param) # Remove control chars too
    s_param = s_param[:150].strip('. ') # Limit length and strip trailing/leading dots/spaces
    # Replace multiple dots with underscore, but try to preserve extension dot if present
    # This is tricky. A simpler approach is often to just replace all dots with underscore
    # if you're not dealing with extensions here, or handle extension separately.
    # For now, a simpler replacement:
    s_param = s_param.replace('.', '_')
    return s_param if s_param else 'sanitized_track' # Ensure not empty after sanitization



# get_download_info_pg: Replaces Redis-based get_download_info
def get_download_info_pg(video_id: str, user_id: int) -> Optional[str]:
    """Return file path from PostgreSQL 'user_downloads' if downloaded by user, else None."""
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT path FROM user_downloads WHERE video_id = %s AND user_id = %s", (video_id, user_id))
            row = cur.fetchone()
            if row and row['path'] and os.path.exists(row['path']):
                return row['path']
            elif row and row['path']: # Exists in DB but not on FS
                logger.warning(f"Download record for {video_id} (user {user_id}) exists in DB but file missing at {row['path']}.")
                # Optionally, delete this stale DB record here.
            return None
    except psycopg2.Error as e:
        logger.error(f"PG Error checking download info for {video_id} (user {user_id}): {e}")
        return None
    finally:
        if conn: conn.close()

# filter_local_songs_pg: Replaces Redis/in-memory local_songs filtering.
def filter_local_songs_pg(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Return local songs from PostgreSQL 'local_songs' table matching the query."""
    qlow = f"%{query.lower()}%" # Prepare for ILIKE
    results = []
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Search title, artist, album. ILIKE is case-insensitive.
            cur.execute("""
                SELECT id, title, artist, album, path, thumbnail, duration
                FROM local_songs
                WHERE LOWER(title) ILIKE %s OR LOWER(artist) ILIKE %s OR LOWER(album) ILIKE %s
                LIMIT %s
            """, (qlow, qlow, qlow, limit))
            results = [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as e:
        logger.error(f"PG Error filtering local songs for query '{query}': {e}")
    finally:
        if conn: conn.close()
    return results

# search_songs (YTMusic search) remains mostly the same, uses its own non-DB song_cache.
# Ensure CACHE_DURATION is used.

def add_recommendation_util(track_data: Dict, recommendations_list: List, seen_songs_set: set, current_song_id: Optional[str] = None, ytmusic_api: Optional[YTMusic] = None): # Renamed params
    """
    Helper to add a track to a recommendations list if valid.
    This is a more generic version for util.py.
    If ytmusic_api is passed, can enrich info, otherwise uses what's in track_data.
    """
    # ... (Keep the logic of your existing add_recommendation function) ...
    # This function processes a track dictionary (usually from YTMusic API)
    # and decides if it's suitable for recommendation.
    # It should return True if added, False otherwise.
    # Ensure it uses the passed parameters correctly.
    # Example structure:
    try:
        video_id_ar = track_data.get("videoId") # Renamed
        if not video_id_ar or video_id_ar in seen_songs_set or video_id_ar == current_song_id:
            return False
        if track_data.get("isAvailable") is False or track_data.get("isPrivate") is True: return False

        title_ar = track_data.get("title", "").strip() # Renamed
        # Artist extraction can be complex from YTMusic results
        artist_ar = "Unknown Artist" # Renamed
        if track_data.get("artists") and isinstance(track_data["artists"], list) and track_data["artists"]:
            artist_ar = track_data["artists"][0].get("name", "Unknown Artist").strip()
        elif track_data.get("artist") and isinstance(track_data["artist"], str) : # Sometimes 'artist' field
            artist_ar = track_data["artist"].strip()
        
        if not title_ar or not artist_ar or artist_ar == "Unknown Artist": return False

        duration_ar = int(track_data.get("duration_seconds", 0)) # Renamed
        # if duration_ar < 30 or duration_ar > 1800: return False # Example filter

        album_ar = (track_data.get("album", {}) or {}).get("name", "") # Renamed
        thumbnail_ar = get_best_thumbnail_util(track_data.get("thumbnails", [])) or f"https://i.ytimg.com/vi/{video_id_ar}/hqdefault.jpg" # Renamed

        recommendations_list.append({
            "id": video_id_ar, "title": title_ar, "artist": artist_ar, "album": album_ar,
            "thumbnail": thumbnail_ar, "duration": duration_ar
        })
        seen_songs_set.add(video_id_ar)
        return True
    except Exception as e_ar: # Renamed
        logger.warning(f"Util: Error processing track for recommendation: {track_data.get('videoId', 'N/A')}, Error: {e_ar}")
        return False

def get_best_thumbnail_util(thumbnails_list: List) -> str: # Renamed param
    """Util version of get_best_thumbnail."""
    if not thumbnails_list or not isinstance(thumbnails_list, list): return ""
    try:
        # Sort by height * width (area) to find largest, then by height if area is same
        sorted_thumbs = sorted(
            thumbnails_list,
            key=lambda x: (x.get('height', 0) * x.get('width', 0), x.get('height', 0)),
            reverse=True
        )
        if sorted_thumbs:
            thumb_url = sorted_thumbs[0].get('url', '')
            return f"https:{thumb_url}" if thumb_url.startswith('//') else thumb_url
        return ""
    except Exception: return "" # Catch any error during processing


def cleanup_expired_sessions_pg():
    """Remove expired sessions from PostgreSQL 'active_sessions' table."""
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
            deleted_count = cur.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired sessions from PG.")
    except psycopg2.Error as e:
        logger.error(f"PG Error cleaning up expired sessions: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

# fetch_image remains the same (uses requests).

def record_listen_start_pg(user_id: int, song_id: str, title: str, artist: str, session_id: str, duration: Optional[int] = 0) -> Optional[int]:
    """Record listen start in PostgreSQL 'listening_history', returns new listen_id."""
    conn = None
    try:
        # Assuming client sends UTC or it's converted before this point. PG TIMESTAMPTZ handles it.
        started_at_time = datetime.now(pytz.utc) if time_sync.get_current_time().tzinfo is None else time_sync.get_current_time()


        song_id_clean = str(song_id).strip() # Renamed
        title_clean = str(title).strip() # Renamed
        artist_clean = str(artist).strip() # Renamed
        session_id_clean = str(session_id).strip() # Renamed
        duration_clean = int(duration or 0) # Renamed

        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                INSERT INTO listening_history (user_id, song_id, title, artist, session_id, started_at, duration)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (user_id, song_id_clean, title_clean, artist_clean, session_id_clean, started_at_time, duration_clean))
            listen_id_new = cur.fetchone()['id'] # Renamed
            conn.commit()
            logger.info(f"Recorded listen start in PG for user {user_id}, song {song_id_clean}, listen_id {listen_id_new}.")
            return listen_id_new
    except psycopg2.Error as e:
        logger.error(f"PG Error recording listen start for user {user_id}: {e}")
        if conn: conn.rollback()
    except Exception as e_gen:
        logger.error(f"General error recording listen start for user {user_id}: {e_gen}", exc_info=True)
        if conn: conn.rollback()
    finally:
        if conn: conn.close()
    return None # Indicate failure

def get_overview_stats_pg(user_id: int) -> Dict[str, Any]:
    """
    Fetches overview statistics for a user from the listening_history.
    Ensures all expected keys are present with default numeric values.
    """
    conn = None
    # Initialize with keys expected by the frontend
    stats = {
        "total_time": 0,             # Changed from total_time_seconds
        "total_songs": 0,            # Changed from total_songs_played
        "unique_artists": 0,         # Changed from unique_artists_listened
        "average_daily": 0.0         # Changed from average_daily_plays
    }
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Total listening time
            cur.execute("SELECT COALESCE(SUM(listened_duration), 0) as total_time_val FROM listening_history WHERE user_id = %s", (user_id,)) # Use a different alias
            row = cur.fetchone()
            stats["total_time"] = int(row['total_time_val']) if row and row['total_time_val'] is not None else 0
            
            # Total songs played (count of entries in listening_history)
            cur.execute("SELECT COUNT(*) as total_songs_val FROM listening_history WHERE user_id = %s", (user_id,)) # Use a different alias
            row = cur.fetchone()
            stats["total_songs"] = int(row['total_songs_val']) if row and row['total_songs_val'] is not None else 0

            # Unique artists (ensure artist is not null or empty)
            cur.execute("SELECT COUNT(DISTINCT artist) as unique_artists_val FROM listening_history WHERE user_id = %s AND artist IS NOT NULL AND artist <> ''", (user_id,)) # Use a different alias
            row = cur.fetchone()
            stats["unique_artists"] = int(row['unique_artists_val']) if row and row['unique_artists_val'] is not None else 0

            # Average daily plays
            cur.execute("SELECT MIN(started_at) as first_listen FROM listening_history WHERE user_id = %s", (user_id,))
            first_listen_row = cur.fetchone()
            avg_daily_calc = 0.0 
            if first_listen_row and first_listen_row['first_listen']:
                first_listen_dt = first_listen_row['first_listen'] 
                now_aware = datetime.now(pytz.utc)
                
                first_listen_dt_aware = first_listen_dt
                if first_listen_dt.tzinfo is None or first_listen_dt.tzinfo.utcoffset(first_listen_dt) is None:
                    logger.warning(f"Insights: first_listen_dt '{first_listen_dt}' from DB is naive. Localizing to UTC.")
                    first_listen_dt_aware = pytz.utc.localize(first_listen_dt)
                elif first_listen_dt.tzinfo != pytz.utc:
                     first_listen_dt_aware = first_listen_dt.astimezone(pytz.utc)

                days_active = (now_aware - first_listen_dt_aware).days
                
                if days_active > 0:
                    if stats["total_songs"] > 0: 
                         avg_daily_calc = stats["total_songs"] / days_active
                elif stats["total_songs"] > 0: 
                    avg_daily_calc = float(stats["total_songs"]) 
            
            stats["average_daily"] = round(avg_daily_calc, 2)
        
        logger.debug(f"Overview stats for user {user_id}: {stats}")
        return stats
    except psycopg2.Error as e:
        logger.error(f"PG Error in get_overview_stats_pg for user {user_id}: {e}", exc_info=True)
        # Return stats with default 0 values on error
        stats_on_error = { "total_time": 0, "total_songs": 0, "unique_artists": 0, "average_daily": 0.0 }
        return stats_on_error
    except Exception as e_gen:
        logger.error(f"General Error in get_overview_stats_pg for user {user_id}: {e_gen}", exc_info=True)
        # Return stats with default 0 values on error
        stats_on_error_gen = { "total_time": 0, "total_songs": 0, "unique_artists": 0, "average_daily": 0.0 }
        return stats_on_error_gen
    finally:
        if conn: conn.close()

def get_recent_activity_pg(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Fetches recent listening activity for a user."""
    conn = None
    activities = []
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT song_id, title, artist, started_at, listened_duration, completion_rate
                FROM listening_history 
                WHERE user_id = %s 
                ORDER BY started_at DESC 
                LIMIT %s
            """, (user_id, limit))
            for row in cur.fetchall():
                started_at_dt = row['started_at'] # TIMESTAMPTZ from PG is aware UTC
                
                activities.append({
                    "song_id": row['song_id'],
                    "title": row['title'] or "Unknown Title", 
                    "artist": row['artist'] or "Unknown Artist",
                    "started_at_iso": started_at_dt.isoformat(), 
                    "started_at_formatted": time_sync.format_time(started_at_dt),
                    "started_at_relative": time_sync.format_time(started_at_dt, relative=True),
                    "listened_duration_seconds": int(row['listened_duration'] or 0),
                    # Ensure completion_rate is float and then multiply for percentage
                    "completion_percentage": round(float(row['completion_rate'] or 0.0) * 100, 2)
                })
        return activities
    except psycopg2.Error as e:
        logger.error(f"PG Error in get_recent_activity_pg for user {user_id}: {e}", exc_info=True)
        return []
    except Exception as e_gen:
        logger.error(f"General Error in get_recent_activity_pg for user {user_id}: {e_gen}", exc_info=True)
        return []
    finally:
        if conn: conn.close()
# In utils/util.py

def get_top_artists_pg(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Fetches top listened artists for a user."""
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT artist, 
                       COUNT(*) as plays, 
                       COALESCE(SUM(listened_duration), 0) as total_time_seconds_val
                FROM listening_history 
                WHERE user_id = %s AND artist IS NOT NULL AND artist <> ''
                GROUP BY artist 
                ORDER BY plays DESC, total_time_seconds_val DESC 
                LIMIT %s
            """, (user_id, limit))
            
            results = []
            for row in cur.fetchall():
                results.append({
                    'name': row['artist'],  # Changed from 'artist' to 'name'
                    'plays': row['plays'],
                    'time': row['total_time_seconds_val']  # Changed from 'total_time_seconds' to 'time'
                })
            return results
    except psycopg2.Error as e:
        logger.error(f"PG Error in get_top_artists_pg for user {user_id}: {e}", exc_info=True)
        return []
    except Exception as e_gen:
        logger.error(f"General Error in get_top_artists_pg for user {user_id}: {e_gen}", exc_info=True)
        return []
    finally:
        if conn: conn.close()

def get_listening_patterns_pg(user_id: int) -> Dict[str, Any]:
    """Fetches hourly and daily listening patterns for a user."""
    conn = None
    hourly_pattern = {f"{h:02d}": 0 for h in range(24)} 
    # Use full day names as keys for frontend, map DOW (0=Sun) to these names
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    daily_pattern = {day_name: 0 for day_name in day_names}
    
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Hourly pattern (EXTRACT(HOUR FROM timestamptz) is UTC hour)
            cur.execute("""
                SELECT EXTRACT(HOUR FROM started_at AT TIME ZONE 'UTC') as hour_of_day, 
                       COUNT(*) as plays
                FROM listening_history 
                WHERE user_id = %s AND started_at IS NOT NULL
                GROUP BY hour_of_day 
                ORDER BY hour_of_day
            """, (user_id,))
            for row in cur.fetchall(): 
                hour_key = f"{int(row['hour_of_day']):02d}"
                if hour_key in hourly_pattern:
                    hourly_pattern[hour_key] = int(row['plays'])

            # Daily pattern (EXTRACT(DOW FROM timestamptz): 0 for Sunday, ..., 6 for Saturday)
            cur.execute("""
                SELECT EXTRACT(DOW FROM started_at AT TIME ZONE 'UTC') as day_of_week, 
                       COUNT(*) as plays
                FROM listening_history 
                WHERE user_id = %s AND started_at IS NOT NULL
                GROUP BY day_of_week 
                ORDER BY day_of_week
            """, (user_id,))
            for row in cur.fetchall(): 
                day_index = int(row['day_of_week'])
                if 0 <= day_index < len(day_names):
                    daily_pattern[day_names[day_index]] = int(row['plays'])
        return {"hourly": hourly_pattern, "daily": daily_pattern}
    except psycopg2.Error as e:
        logger.error(f"PG Error in get_listening_patterns_pg for user {user_id}: {e}", exc_info=True)
        return {"hourly": hourly_pattern, "daily": daily_pattern} # Return defaults
    except Exception as e_gen:
        logger.error(f"General Error in get_listening_patterns_pg for user {user_id}: {e_gen}", exc_info=True)
        return {"hourly": hourly_pattern, "daily": daily_pattern} # Return defaults
    finally:
        if conn: conn.close()

def get_completion_rates_pg(user_id: int) -> Dict[str, Any]:
    """Fetches song completion rate statistics for a user."""
    conn = None
    completion_stats = {'full': 0, 'partial': 0, 'skip': 0, 'unknown': 0} 
    avg_completion = 0.0
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Distribution based on 'listen_type'
            cur.execute("""
                SELECT listen_type, COUNT(*) as count 
                FROM listening_history
                WHERE user_id = %s AND listen_type IS NOT NULL 
                GROUP BY listen_type
            """, (user_id,))
            for row in cur.fetchall():
                if row['listen_type'] in completion_stats:
                    completion_stats[row['listen_type']] = int(row['count'])
                else:
                    completion_stats['unknown'] += int(row['count'])
            
            # Average completion_rate (where duration > 0 to avoid division by zero issues if rate was calculated based on 0 duration)
            cur.execute("""
                SELECT COALESCE(AVG(completion_rate), 0.0) as avg_comp
                FROM listening_history 
                WHERE user_id = %s AND completion_rate IS NOT NULL AND duration > 0 
            """, (user_id,))
            avg_comp_row = cur.fetchone()
            if avg_comp_row and avg_comp_row['avg_comp'] is not None: 
                avg_completion = round(float(avg_comp_row['avg_comp']) * 100, 2) 
        return {"completion_distribution": completion_stats, "average_completion_percentage": avg_completion}
    except psycopg2.Error as e:
        logger.error(f"PG Error in get_completion_rates_pg for user {user_id}: {e}", exc_info=True)
        return {"completion_distribution": completion_stats, "average_completion_percentage": avg_completion}
    except Exception as e_gen:
        logger.error(f"General Error in get_completion_rates_pg for user {user_id}: {e_gen}", exc_info=True)
        return {"completion_distribution": completion_stats, "average_completion_percentage": avg_completion}
    finally:
        if conn: conn.close()

def record_listen_end_pg(listen_id: int, listened_duration: int):
    """
    Records the end of a song listen, updates listened duration, completion rate,
    listen type, and updates total listened time in user_statistics.
    """
    conn = None
    try:
        ended_at_time = datetime.now(pytz.utc) # Current time in UTC
        listened_duration_clean = int(listened_duration or 0)

        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Fetch original duration and user_id for the given listen_id
            cur.execute("SELECT duration, user_id FROM listening_history WHERE id = %s", (listen_id,))
            history_entry = cur.fetchone()
            if not history_entry:
                logger.error(f"Cannot record listen_end: listen_id {listen_id} not found.")
                return

            original_duration = int(history_entry['duration'] or 0)
            history_user_id = history_entry['user_id'] 
            
            completion_rate_val = 0.0 
            if original_duration > 0: # Avoid division by zero
                completion_rate_val = min(1.0, max(0.0, listened_duration_clean / original_duration))
            
            # Determine listen_type based on completion rate
            listen_type_val = 'partial' # Default
            if completion_rate_val >= 0.85 : listen_type_val = 'full' 
            elif completion_rate_val <= 0.20 and original_duration > 0 : listen_type_val = 'skip'
            elif original_duration == 0 and listened_duration_clean > 10 : listen_type_val = 'partial' # Listened for a bit, duration unknown
            elif original_duration == 0 : listen_type_val = 'unknown' # Duration unknown, minimal listen

            # Update the listening_history record
            cur.execute("""
                UPDATE listening_history SET
                    ended_at = %s, 
                    listened_duration = %s, 
                    completion_rate = %s, 
                    listen_type = %s
                WHERE id = %s
            """, (ended_at_time, listened_duration_clean, completion_rate_val, listen_type_val, listen_id))
            
            # Update user_statistics: total_listened_time
            if history_user_id is not None:
                cur.execute("""
                    INSERT INTO user_statistics (user_id, total_listened_time)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        total_listened_time = COALESCE(user_statistics.total_listened_time, 0) + EXCLUDED.total_listened_time
                """, (history_user_id, listened_duration_clean))
                logger.debug(f"Updated total_listened_time for user {history_user_id} by {listened_duration_clean}s.")
            else:
                logger.warning(f"Could not update user_statistics for listen_id {listen_id} as user_id was not found in listening_history.")

            conn.commit()
            logger.info(f"Recorded listen end in PG for listen_id {listen_id}. Listened: {listened_duration_clean}s, Type: {listen_type_val}, Rate: {completion_rate_val:.2f}")
    except psycopg2.Error as e:
        logger.error(f"PG Error recording listen end for listen_id {listen_id}: {e}", exc_info=True)
        if conn: conn.rollback()
    except Exception as e_gen:
        logger.error(f"General error recording listen end for listen_id {listen_id}: {e_gen}", exc_info=True)
        if conn: conn.rollback()
    finally:
        if conn: conn.close()



def generate_otp(): # Unchanged
    """Generate a 6-digit OTP."""
    return ''.join([str(secrets.randbelow(10)) for _ in range(6)])

def store_otp_pg(email: str, otp: str, purpose: str):
    """Store OTP in PostgreSQL 'pending_otps' table."""
    conn = None
    try:
        # OTPs typically expire relatively quickly
        expires_at_otp = datetime.now(pytz.utc) + timedelta(minutes=10) # Renamed, use UTC
        conn = get_pg_connection()
        with conn.cursor() as cur:
            # Clear existing OTPs for this email and purpose to avoid confusion
            cur.execute("DELETE FROM pending_otps WHERE email = %s AND purpose = %s", (email, purpose))
            cur.execute("""
                INSERT INTO pending_otps (email, otp, purpose, expires_at)
                VALUES (%s, %s, %s, %s)
            """, (email, otp, purpose, expires_at_otp))
            conn.commit()
        logger.info(f"Stored OTP for {email} (purpose: {purpose}) in PG.")
    except psycopg2.Error as e:
        logger.error(f"PG Error storing OTP for {email}: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

def verify_otp_pg(email: str, otp_provided: str, purpose: str) -> bool: # Renamed param
    """Verify OTP from PostgreSQL 'pending_otps' and delete if valid."""
    conn = None
    valid_otp = False # Renamed
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Fetch the latest non-expired OTP for the given email and purpose
            cur.execute("""
                SELECT otp FROM pending_otps
                WHERE email = %s AND purpose = %s AND expires_at > CURRENT_TIMESTAMP
                ORDER BY created_at DESC LIMIT 1
            """, (email, purpose))
            row = cur.fetchone()
            if row and row['otp'] == otp_provided:
                valid_otp = True
                # Delete all OTPs for this email/purpose once verified to prevent reuse
                cur.execute("DELETE FROM pending_otps WHERE email = %s AND purpose = %s", (email, purpose))
            conn.commit()
        if valid_otp: logger.info(f"OTP verified successfully for {email} (purpose: {purpose}).")
        else: logger.warning(f"OTP verification failed for {email} (purpose: {purpose}).")
        return valid_otp
    except psycopg2.Error as e:
        logger.error(f"PG Error verifying OTP for {email}: {e}")
        if conn: conn.rollback() # Rollback if delete failed after potential check
    finally:
        if conn: conn.close()
    return False # Default to false on error


# download_default_songs: Needs to use download_flac_init_pg
def download_default_songs_pg():
    default_song_ids = ["dQw4w9WgXcQ"] # Example
    logger.info(f"Checking/Downloading default songs: {default_song_ids}")
    for song_id in default_song_ids:
        flac_path = os.path.join(MUSIC_DIR, f"{song_id}.flac")
        if not os.path.exists(flac_path):
            logger.info(f"Default song {song_id} not found locally, attempting download...")
            download_flac_init_pg(song_id) # Use PG-aware init downloader
        else:
            logger.info(f"Default song {song_id} already exists at {flac_path}.")


def send_email(to_email, subject, body_html): # Renamed param, Unchanged logic
    """Send HTML email using SMTP."""
    if not all([SMTP_USER, SMTP_PASSWORD, SMTP_HOST, SMTP_PORT]):
        logger.error("SMTP settings not configured. Cannot send email.")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body_html, 'html', 'utf-8')) # Specify utf-8

        # Choose SMTP_SSL for port 465, otherwise standard SMTP with starttls
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            server.starttls() # Secure the connection
        
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info(f"Email sent successfully to {to_email} with subject '{subject}'.")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP Authentication Error. Check SMTP_USER and SMTP_PASSWORD.")
    except smtplib.SMTPServerDisconnected:
        logger.error("SMTP Server Disconnected. Check SMTP_HOST and SMTP_PORT or network.")
    except smtplib.SMTPConnectError:
         logger.error(f"SMTP Connection Error. Could not connect to {SMTP_HOST}:{SMTP_PORT}.")
    except Exception as e_email: # Renamed
        logger.error(f"Failed to send email to {to_email}: {e_email}", exc_info=True)
    return False


# --- FLAC Downloaders (Core logic updated to use PG for recording download) ---
def download_flac_pg(video_id: str, user_id: Optional[int]) -> Optional[str]: # user_id can be None for init
    """
    Download song using yt-dlp (executable or module), record in PG's 'user_downloads'.
    Returns path to FLAC file or None.
    """
    if not YTDLP_PATH:
        logger.error("YTDLP_PATH not configured. Cannot download FLAC.")
        return None
        
    flac_path_dl = os.path.join(MUSIC_DIR, f"{video_id}.flac") # Renamed
    if os.path.exists(flac_path_dl):
        logger.info(f"FLAC for {video_id} already exists at {flac_path_dl}.")
        # Ensure it's recorded for this user if user_id is provided
        if user_id:
            # This might re-fetch metadata if not already in DB for this user.
            # A lighter "ensure_recorded" might be better if path exists.
            try:
                # Use a helper to get metadata without re-downloading if file exists
                # This is a bit simplified; ideally, metadata is fetched once.
                info_meta = ytmusic.get_song(video_id) # type: ignore
                title_meta = info_meta.get("videoDetails", {}).get("title", "Unknown Title") # type: ignore
                artist_meta = info_meta.get("videoDetails", {}).get("author", "Unknown Artist") # type: ignore
                album_meta = info_meta.get("album", {}).get("name") # type: ignore
                record_download_pg(video_id, title_meta, artist_meta, album_meta, flac_path_dl, user_id)
            except Exception as e_meta_rec:
                logger.error(f"Error recording existing download {video_id} for user {user_id}: {e_meta_rec}")
        return flac_path_dl

    logger.info(f"Attempting download for: {video_id} (User: {user_id if user_id else 'Init/System'})")
    yt_music_url_dl = f"https://music.youtube.com/watch?v={video_id}" # Renamed

    # Using the module approach as primary, as it's generally more robust if yt-dlp is installed as lib.
    # The executable path can be passed to YoutubeDL if preferred via `youtube_dl_path` option.
    try:
       
        return download_with_module_pg(video_id, user_id, yt_music_url_dl, flac_path_dl)

    except Exception as e_dl_primary: # Renamed
        logger.warning(f"Primary download method (module) failed for {video_id}: {e_dl_primary}. Trying executable.")
        try:
                    return download_with_executable_pg(video_id, user_id, yt_music_url_dl, flac_path_dl)
            
        except Exception as e_dl_fallback: # Renamed
            logger.error(f"All download methods failed for {video_id}: {e_dl_fallback}", exc_info=True)
            return None


def download_flac_init_pg(video_id: str) -> Optional[str]:
    """Version of download_flac_pg for initialization (user_id is None)."""
    return download_flac_pg(video_id, None)


def download_with_executable_pg(video_id: str, user_id: Optional[int], url: str, flac_path: str) -> Optional[str]:
    """Helper: Download using yt-dlp executable, record in PG."""
    if not YTDLP_PATH or not os.path.exists(YTDLP_PATH):
        raise Exception(f"yt-dlp executable not found at {YTDLP_PATH}")

    # Metadata extraction with yt-dlp executable
    # Using --dump-json is more reliable for structured metadata
    meta_proc = subprocess.run( # Renamed
        [YTDLP_PATH, "--quiet", "--no-warnings", "--dump-json", url],
        capture_output=True, text=True, encoding='utf-8', check=False # check=False, handle error below
    )
    if meta_proc.returncode != 0 or not meta_proc.stdout:
        logger.error(f"yt-dlp metadata exec failed for {url}. Stderr: {meta_proc.stderr}")
        # Fallback to ytmusicapi for metadata if exec fails
        try:
            info_api = ytmusic.get_song(video_id) # type: ignore
            title, artist, album = info_api.get("videoDetails", {}).get("title"), info_api.get("videoDetails", {}).get("author"), info_api.get("album", {}).get("name") # type: ignore
        except Exception as api_err:
            logger.error(f"YTMusic API metadata fallback also failed for {video_id}: {api_err}")
            title, artist, album = "Unknown Title", "Unknown Artist", None # Provide defaults
    else:
        try:
            info_json = json.loads(meta_proc.stdout)
            title = info_json.get('title', 'Unknown Title')
            artist = info_json.get('artist') or info_json.get('uploader', 'Unknown Artist') # Fallback to uploader
            album = info_json.get('album')
        except json.JSONDecodeError:
            logger.error(f"Failed to parse yt-dlp JSON output for {url}. Using fallbacks.")
            title, artist, album = "Unknown Title (Parse Error)", "Unknown Artist", None


    # Download command
    command = [
        YTDLP_PATH, "--quiet", "--no-warnings", "-x", "--audio-format", "flac",
        "--audio-quality", "0", "--embed-metadata", "--embed-thumbnail",
        "-o", os.path.join(MUSIC_DIR, f"{video_id}.%(ext)s"), # Ensure output name is just video_id
    ]
    if platform.system().lower() == "windows" and FFMPEG_BIN_DIR and os.path.isdir(FFMPEG_BIN_DIR):
        command.extend(["--ffmpeg-location", FFMPEG_BIN_DIR])
    command.append(url)

    logger.info(f"Executing yt-dlp command: {' '.join(command)}")
    dl_proc = subprocess.run(command, check=False) # Renamed, check=False

    if dl_proc.returncode == 0 and os.path.exists(flac_path):
        if user_id is not None: # Only record if user_id is provided (not for init)
            record_download_pg(video_id, title or "N/A", artist or "N/A", album, flac_path, user_id)
            load_local_songs_util() # Rescan/reload to include the new download
        logger.info(f"Exec DL success: {video_id} to {flac_path}")
        return flac_path
    else:
        logger.error(f"yt-dlp executable download failed for {video_id}. Code: {dl_proc.returncode}. Stderr: {dl_proc.stderr if hasattr(dl_proc, 'stderr') else 'N/A'}")
        # Attempt to clean up partial file if it exists but failed
        if os.path.exists(flac_path) and dl_proc.returncode != 0:
            try: os.remove(flac_path)
            except OSError: logger.warning(f"Could not remove partially downloaded file: {flac_path}")
        raise Exception(f"yt-dlp executable download failed (Code {dl_proc.returncode})")


def download_with_module_pg(video_id: str, user_id: Optional[int], url: str, flac_path: str) -> Optional[str]:
    """Helper: Download using yt-dlp Python module, record in PG."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(MUSIC_DIR, f"{video_id}.%(ext)s"), # Output name is just video_id
        'postprocessors': [{
            'key': 'FFmpegExtractAudio', 'preferredcodec': 'flac', 'preferredquality': '0',
        }, {
            'key': 'FFmpegMetadata', 'add_metadata': True,
        }, {
            'key': 'EmbedThumbnail', 'already_have_thumbnail': False,
        }],
        'writethumbnail': True, 'embedthumbnail': True, 'addmetadata': True,
        'prefer_ffmpeg': True, 'keepvideo': False, 'clean_infojson': True,
        'quiet': True, 'no_warnings': True, 'retries': 5, 'fragment_retries': 5,
        # 'ffmpeg_location': FFMPEG_BIN_DIR if platform.system().lower() == "windows" and FFMPEG_BIN_DIR and os.path.isdir(FFMPEG_BIN_DIR) else None
        # Filter out None for ffmpeg_location if not set/valid
    }
    if platform.system().lower() == "windows" and FFMPEG_BIN_DIR and os.path.isdir(FFMPEG_BIN_DIR):
        ydl_opts['ffmpeg_location'] = FFMPEG_BIN_DIR
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False) # Get info first
            title = info.get('title', 'Unknown Title')
            artist = info.get('artist') or info.get('uploader', 'Unknown Artist')
            album = info.get('album')
            
            logger.info(f"Starting module download for {video_id} ({title})")
            ydl.download([url]) # Perform download

            if os.path.exists(flac_path):
                if user_id is not None: # Only record if user_id is provided
                    record_download_pg(video_id, title, artist, album, flac_path, user_id)
                    load_local_songs_util() # Rescan/reload
                logger.info(f"Module DL success: {video_id} to {flac_path}")
                return flac_path
            else: # File not found after download attempt
                logger.error(f"yt-dlp module download: File not found at {flac_path} after processing {video_id}.")
                raise Exception("yt-dlp module download: Output file not found.")
    except Exception as e_mod_dl: # Renamed
        logger.error(f"yt-dlp module download error for {video_id}: {e_mod_dl}", exc_info=True)
        # Attempt to clean up partial file
        if os.path.exists(flac_path):
            try: os.remove(flac_path)
            except OSError: logger.warning(f"Could not remove partially downloaded file (module): {flac_path}")
        raise # Re-raise the exception to be caught by the caller (download_flac_pg)

@lru_cache(maxsize=100)
def search_songs(query: str):
    """YTMusic search (songs) with deduplication, cached for 1 hour."""
    now_ts = datetime.now().timestamp()
    if query in search_cache:
        old_data, old_ts = search_cache[query]
        if now_ts - old_ts < CACHE_DURATION:
            return old_data

    try:
        raw = ytmusic.search(query, filter="songs")
        seen_titles = set()  # Track seen title+artist combinations
        results = []

        for item in raw:
            vid = item.get("videoId")
            if not vid:
                continue

            artist = "Unknown Artist"
            if item.get("artists"):
                artist = item["artists"][0].get("name", "Unknown Artist")

            title = item.get("title", "Unknown")
            title_artist = (title.lower(), artist.lower())

            # Skip if we've seen this title+artist combination
            if title_artist in seen_titles:
                continue

            dur = item.get("duration_seconds", 0)
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

            results.append({
                "id": vid,
                "title": title,
                "artist": artist,
                "album": "",
                "duration": dur,
                "thumbnail": thumb
            })

            seen_titles.add(title_artist)

        search_cache[query] = (results, now_ts)
        return results

    except Exception as e:
        logger.error(f"search_songs error: {e}")
        return []

def fallback_recommendations():
    """Simplified fallback using search instead of unavailable methods."""
    try:
        categories = [
            "top hits",
            "popular music",
            "trending songs",
            "new releases",
            "viral hits"
        ]

        recommendations = []
        seen_songs = set()

        # Try 2 random categories
        selected_cats = random.sample(categories, 2)

        for query in selected_cats:
            results = ytmusic.search(query, filter="songs", limit=10)
            if results:
                selected = random.sample(results, min(3, len(results)))
                for track in selected:
                    add_recommendation(track, recommendations, seen_songs)
                    if len(recommendations) >= 5:
                        break

        random.shuffle(recommendations)
        return jsonify(recommendations[:5])

    except Exception as e:
        logger.error(f"Fallback recommendations error: {e}")
        return jsonify([])


def get_local_song_recommendations(local_song_id):
    """Get recommendations for local songs using title/artist search."""
    try:
        local_meta = local_songs.get(local_song_id)
        if not local_meta:
            return fallback_recommendations()

        recommendations = []
        seen_songs = set()

        # Search using song title and artist
        query = f"{local_meta['title']} {local_meta['artist']}"
        search_results = ytmusic.search(query, filter="songs", limit=15)

        for track in search_results:
            if add_recommendation(track, recommendations, seen_songs):
                if len(recommendations) >= 5:
                    break

        # If we need more, add some popular songs
        if len(recommendations) < 5:
            popular = ytmusic.search("popular music", filter="songs", limit=10)
            for track in popular:
                if add_recommendation(track, recommendations, seen_songs):
                    if len(recommendations) >= 5:
                        break

        random.shuffle(recommendations)
        return jsonify(recommendations[:5])

    except Exception as e:
        logger.error(f"Local recommendations error: {e}")
        return fallback_recommendations()
def add_recommendation(track, recommendations, seen_songs, current_song_id=None, ytmusic_api: Optional[YTMusic] = None): # Add ytmusic_api here
    """Enhanced track processing with better validation.
    This version acts as a wrapper to add_recommendation_util, passing along the ytmusic_api.
    """
    # Call the utility function that actually contains the logic and accepts ytmusic_api
    return add_recommendation_util(
        track_data=track,
        recommendations_list=recommendations,
        seen_songs_set=seen_songs,
        current_song_id=current_song_id,
        ytmusic_api=ytmusic_api # Pass it through
    )

# --- Initialize local songs cache on module load ---
# This is important so that `local_songs` is populated when other functions in this module are called.
if __name__ != "__main__": # Avoid running on direct script execution if any
    logger.info("Util.py loaded, initializing local songs cache from database...")
    load_local_songs_util()
 



def process_description(description):
    """Process and clean artist description"""
    if isinstance(description, list):
        description = ' '.join(description)
    return description.strip() or 'No description available'



def process_genres(artist_data):
    """Extract and process genres"""
    try:
        if 'genres' not in artist_data:
            return []

        if isinstance(artist_data['genres'], list):
            return artist_data['genres']
        elif artist_data['genres']:
            return [artist_data['genres']]
        return []
    except:
        return []

@login_required
def get_artist_stats(artist_data):
    """Extract all artist statistics"""
    try:
        # Get monthly listeners with fallbacks
        monthly_listeners = get_monthly_listeners(artist_data)

        # Extract and format stats
        stats = {
            'subscribers': safe_format_count(artist_data.get('subscribers', '0')),
            'views': safe_format_count(artist_data.get('views', '0')),
            'monthlyListeners': safe_format_count(monthly_listeners)
        }

        # Try to get additional stats if available
        if 'stats' in artist_data and isinstance(artist_data['stats'], dict):
            extra_stats = artist_data['stats']
            if 'totalPlays' in extra_stats:
                stats['totalPlays'] = safe_format_count(extra_stats['totalPlays'])
            if 'avgDailyPlays' in extra_stats:
                stats['avgDailyPlays'] = safe_format_count(extra_stats['avgDailyPlays'])

        return stats
    except Exception as e:
        logger.warning(f"Error processing stats: {e}")
        return {
            'subscribers': '0',
            'views': '0',
            'monthlyListeners': '0'
        }

def get_monthly_listeners(artist_data):
    """Extract monthly listeners with multiple fallback methods"""
    try:
        # Try direct stats object first
        if 'stats' in artist_data:
            stats = artist_data['stats']
            if isinstance(stats, dict):
                for key in ['monthlyListeners', 'monthly_listeners', 'listeners']:
                    if key in stats and stats[key]:
                        return stats[key]

        # Try top level fields
        for key in ['monthlyListeners', 'monthly_listeners', 'listeners']:
            if key in artist_data and artist_data[key]:
                return artist_data[key]

        # Get from subscriptionButton if available
        if 'subscriptionButton' in artist_data:
            sub_text = artist_data['subscriptionButton'].get('text', '')
            if isinstance(sub_text, str):
                match = re.search(r'(\d[\d,.]*[KMB]?)\s*(?:monthly listeners|listeners)',
                                sub_text, re.IGNORECASE)
                if match:
                    return match.group(1)

        # Try to extract from header or subscription count
        return (artist_data.get('header', {}).get('subscriberCount') or
                artist_data.get('subscribers', '0'))

    except Exception as e:
        logger.warning(f"Error extracting monthly listeners: {e}")
        return '0'
def get_best_thumbnail(thumbnails):
    """Get highest quality thumbnail URL"""
    try:
        if not thumbnails or not isinstance(thumbnails, list):
            return ''
        thumb = thumbnails[-1].get('url', '')
        if thumb.startswith('//'):
            return f"https:{thumb}"
        return thumb
    except:
        return ''
def process_top_songs(artist_data):
    """Process and extract top songs information"""
    top_songs = []
    try:
        songs_data = artist_data.get('songs', [])
        if not isinstance(songs_data, list):
            return []

        for song in songs_data[:10]:  # Limit to top 10
            if not isinstance(song, dict):
                continue

            try:
                song_info = {
                    'title': song.get('title', 'Unknown'),
                    'videoId': song.get('videoId', ''),
                    'plays': safe_format_count(song.get('plays', '0')),
                    'duration': song.get('duration', ''),
                    'thumbnail': get_best_thumbnail(song.get('thumbnails', [])),
                    'album': (song.get('album', {}) or {}).get('name', ''),
                    'year': song.get('year', '')
                }

                if song_info['videoId']:  # Only add if we have a valid video ID
                    top_songs.append(song_info)
            except Exception as e:
                logger.warning(f"Error processing song: {e}")
                continue

    except Exception as e:
        logger.warning(f"Error processing top songs: {e}")

    return top_songs

def process_artist_links(artist_data, artist_id):
    """Process and extract all artist links"""
    links = {
        'youtube': f"https://music.youtube.com/channel/{artist_id}" if artist_id else None,
        'official': artist_data.get('officialWebsite')
    }

    # Add social media links if available
    try:
        if 'links' in artist_data:
            for link in artist_data['links']:
                if isinstance(link, dict):
                    link_type = link.get('type', '').lower()
                    if link_type in ['instagram', 'twitter', 'facebook']:
                        links[link_type] = link.get('url', '')
    except Exception as e:
        logger.warning(f"Error processing social links: {e}")

    return links

def extract_video_id(url):
    """Extract video ID from various YouTube/YouTube Music URL formats"""
    try:
        # Common YouTube URL patterns
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|music\.youtube\.com/watch\?v=|music\.youtube\.com/playlist\?list=)([a-zA-Z0-9_-]{11})',
            r'(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
            r'(?:youtube\.com/playlist\?list=)([a-zA-Z0-9_-]{34})',
            r'(?:music\.youtube\.com/browse/)([a-zA-Z0-9_-]{11})'
        ]

        # Try each pattern
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None
    except:
        return None

def extract_year(artist_data):
    """Extract artist's formation/debut year"""
    try:
        # Try direct year field
        if 'yearFormed' in artist_data:
            return str(artist_data['yearFormed'])

        # Try years active
        if 'years_active' in artist_data:
            years = artist_data['years_active']
            if isinstance(years, list) and years:
                return str(years[0])

        # Try to find year in description
        if 'description' in artist_data:
            desc = str(artist_data['description'])
            match = re.search(r'\b(19|20)\d{2}\b', desc)
            if match:
                return match.group(0)

        return None
    except Exception as e:
        logger.warning(f"Error extracting year: {e}")
        return None
# Inside utils.util.py

def safe_format_count(count):
    """Safely format numerical counts with K/M/B suffixes"""
    try:
        if not count or str(count).strip().lower() in ['0', '', 'none', 'null']:
            return '0'

        # Clean the string: remove commas, spaces, and any trailing non-digit characters (like 'views')
        count_str = str(count).replace(',', '').replace(' ', '')
        # Remove trailing non-numeric characters (e.g., "views", "listeners")
        count_str = re.sub(r'[^\d.]+$', '', count_str, flags=re.IGNORECASE)


        # Handle if count is already formatted (e.g., "1.2M")
        if any(suffix in count_str.upper() for suffix in ['K', 'M', 'B']):
            # Attempt to further process if it's like "1.2M listeners" and only "1.2M" remains
            # If it's just "1.2M", this will pass. If conversion fails, it falls to except.
            pass # Let it try to convert directly or fall to the formatting logic

        num = float(count_str) # This should now work if count_str is purely numeric

        if num >= 1_000_000_000:
            return f"{num/1_000_000_000:.1f}B"
        if num >= 1_000_000:
            return f"{num/1_000_000:.1f}M"
        if num >= 1_000:
            return f"{num/1_000:.1f}K"
        return str(int(num))
    except ValueError: # Catch error if conversion still fails
        logger.warning(f"Could not parse count value after cleaning: '{count_str}' (original: '{count}')")
        # Return the cleaned string or original string if parsing fails, or a default
        return str(count_str) if count_str else '0' # Or return original: str(count)
    except Exception as e:
        logger.warning(f"Error formatting count {count}: {e}")
        return str(count)
def filter_local_songs(query: str):
    """Return deduplicated local songs with title/artist matching the query."""
    qlow = query.lower()
    seen_titles = set()  # Track seen title+artist combinations
    out = []

    for sid, meta in local_songs.items():
        title_artist = (meta["title"].lower(), meta["artist"].lower())

        if (qlow in meta["title"].lower() or qlow in meta["artist"].lower()) and \
           title_artist not in seen_titles:
            out.append(meta)
            seen_titles.add(title_artist)

    return out

def get_fallback_tracks(seen_songs):
    """Get fallback tracks from trending/popular songs or charts."""
    try:
        fallback_tracks = []

        # Try trending
        trending = ytmusic.get_trending_music()
        if trending:
            for track in trending:
                add_recommendation(track, fallback_tracks, seen_songs)

        # If still need more, use charts
        if len(fallback_tracks) < 5:
            charts = ytmusic.get_charts()
            if charts and "items" in charts:
                for track in charts["items"]:
                    add_recommendation(track, fallback_tracks, seen_songs)
        return fallback_tracks
    except Exception as e:
        logger.warning(f"Fallback tracks error: {e}")
        return []