from flask import (


 Blueprint  , redirect , url_for , session , request
 , jsonify , Response , abort , render_template , render_template_string , make_response
 , send_file
)


from ytmusicapi import YTMusic
import secrets
from concurrent.futures import ThreadPoolExecutor
import os
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import logging
import psycopg2
from utils import util
from dotenv import  load_dotenv
from database.database import get_pg_connection
import psycopg2.extras # For DictCursor
import yt_dlp
from functools import wraps , lru_cache




#-------------------------------------------------------------------------


from thefuzz import fuzz
import concurrent.futures
from llm import recommender as llm_recommender # Assuming this is updated if it interacts with DB
import logging
import random
import re
import requests

from bs4 import BeautifulSoup
from functools import wraps, partial, lru_cache
from urllib.parse import urlparse, parse_qs
import yt_dlp
from ytmusicapi import YTMusic
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials





load_dotenv(dotenv_path=os.path.join(os.getcwd() , "configs" , "ui" , "config.conf"))


#---------------------------------------------------------------------------------
ytmusic = YTMusic()


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)



bp = Blueprint('sangeet_all', __name__)
ytmusic = YTMusic() # Keep
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) # Configure as needed

SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')

CACHE_DURATION = 3600 # Retained for potential non-DB caching
local_songs_data_cache = {}
search_cache = {} # Retained for potential non-DB temporary caching
song_cache = {}   # Retained for potential non-DB temporary caching (e.g., ytmusic API results)
# Lyrics are now persistently cached in PostgreSQL table lyrics_cache

SERVER_DOMAIN = os.getenv('SANGEET_BACKEND', f'http://127.0.0.1:{os.getenv("PORT")}')
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
MUSIC_DOWNLOAD_PATH = os.getenv("MUSIC_PATH", "music") # Standardize usage of music download path


if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET:
    logger.warning("Spotify API credentials (SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET) not found.")
    sp = None
else:
    try:
        auth_manager = SpotifyClientCredentials(client_id=SPOTIPY_CLIENT_ID, client_secret=SPOTIPY_CLIENT_SECRET)
        sp = spotipy.Spotify(auth_manager=auth_manager)
        logger.info("Spotipy client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Spotipy client: {e}")
        sp = None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_authed'):
            # For API calls, return 403 Forbidden or 401 Unauthorized
            if request.path.startswith('/api/admin'):
                 return jsonify({"error": "Admin authentication required"}), 401
            # For regular page loads, redirect to the password entry
            return redirect(url_for('sangeet_all.view_admin_issues'))
        return f(*args, **kwargs)
    return decorated_function

def load_local_songs_from_db():
    """Load songs from PostgreSQL 'local_songs' table into the global cache."""
    global local_songs_data_cache
    # Consider adding a timestamp check to avoid reloading too frequently if this is called often
    # For now, it reloads every time it's called.
    
    logger.info("Attempting to load local songs from PostgreSQL...")
    temp_cache = {}
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, title, artist, album, path, thumbnail, duration FROM local_songs")
            rows = cur.fetchall()
            for row in rows:
                # Basic validation: ensure path exists if it's a local file
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
                    logger.warning(f"Local song DB entry {row['id']} has missing/invalid path: {row.get('path')}. Skipping.")
        local_songs_data_cache = temp_cache # Atomically update the global cache
        logger.info(f"Loaded {len(local_songs_data_cache)} songs from PostgreSQL 'local_songs' table.")
    except psycopg2.Error as e:
        logger.error(f"Error loading local songs from PostgreSQL: {e}")
    except Exception as e: # Catch other potential errors like int conversion
        logger.error(f"Unexpected error in load_local_songs_from_db: {e}")
    finally:
        if conn:
            conn.close()
    return local_songs_data_cache # Return the cache for immediate use if needed

local_songs = {}

def load_local_songs_from_postgres():
    """Load songs from the PostgreSQL 'local_songs' table into the local_songs dictionary."""
    global local_songs
    local_songs = {}
    conn = None
    try:
        conn = get_pg_connection()
        if not conn:
            logger.error("Failed to get database connection.")
            return local_songs # Return empty if connection failed

        c = conn.cursor()
        # Select all columns from the local_songs table
        c.execute("SELECT id, title, artist, album, path, thumbnail, duration FROM local_songs")
        songs_data = c.fetchall()

        required_keys = ["id", "title", "artist", "album", "path", "thumbnail", "duration"]
        column_names = [desc[0] for desc in c.description] # Get column names from cursor

        for row in songs_data:
            song_details = dict(zip(column_names, row))

            # Validate that the path exists, similar to the original function's logic
            if "path" not in song_details or not song_details["path"] or not os.path.exists(song_details["path"]):
                logger.warning(f"Path '{song_details.get('path', 'N/A')}' for song ID '{song_details.get('id', 'N/A')}' does not exist. Skipping.")
                # Optionally, you might want to delete or flag such entries in the database
                # For now, we just skip loading them into memory.
                continue

            # Ensure all required keys are present after fetching (though SQL SELECT should guarantee this if columns exist)
            # and perform type conversion if necessary, e.g., duration to int.
            try:
                local_songs[song_details["id"]] = {
                    "id": str(song_details["id"]), # Ensure ID is string if it's not already
                    "title": song_details.get("title"),
                    "artist": song_details.get("artist"),
                    "album": song_details.get("album"),
                    "path": song_details["path"],
                    "thumbnail": song_details.get("thumbnail"),
                    "duration": int(song_details["duration"]) if song_details.get("duration") is not None else 0
                }
            except (ValueError, TypeError) as e:
                logger.error(f"Error processing song data for ID {song_details.get('id')}: {e}. Skipping.")
                continue
            except KeyError as e:
                logger.error(f"Missing expected key {e} in song data for ID {song_details.get('id')}. Skipping.")
                continue


        logger.info(f"Loaded {len(local_songs)} songs from PostgreSQL.")

    except psycopg2.Error as e:
        logger.error(f"Error fetching songs from PostgreSQL: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        if conn:
            if 'c' in locals() and c:
                c.close()
            conn.close()
    return local_songs

def get_best_thumbnail(thumbnails):
    if not thumbnails: return ""
    sorted_thumbs = sorted(thumbnails, key=lambda x: x.get('height', 0) * x.get('width', 0), reverse=True)
    return sorted_thumbs[0].get('url', '')

def get_thumbnail_for_search_results(song_id_param, skip_db_lookup=False):
    """
    Fetches a thumbnail for a given song ID.
    If skip_db_lookup is False, it first tries to find a specific thumbnail
    in the 'local_songs' PostgreSQL table.
    Falls back to a generic YouTube thumbnail URL or a local placeholder.
    The 'skip_db_lookup' defaults to False, meaning DB lookup is preferred.
    """
    # Ensure song_id_param is a string and not None
    if not isinstance(song_id_param, str):
        logger.warning(f"get_thumbnail_for_search_results called with non-string song_id: {song_id_param}. Using default.")
        # Return a very generic default if song_id is invalid
        try:
            return url_for('static', filename='assets/img/defaults/default_thumb.png', _external=False)
        except RuntimeError: # If url_for is not available in this context (e.g. app not fully set up)
            return '/static/assets/img/defaults/default_thumb.png'


    if not skip_db_lookup:
        conn = None
        try:
            conn = get_pg_connection() # Sourced from your database.database module
            if conn:
                with conn.cursor() as c:
                    # Check 'local_songs' table for a specific thumbnail
                    c.execute("SELECT thumbnail FROM local_songs WHERE id = %s", (song_id_param,))
                    result = c.fetchone()
                    if result and result[0] and result[0].strip():
                        logger.debug(f"Found DB thumbnail for '{song_id_param}': {result[0]}")
                        return result[0] # Return DB thumbnail
                    else:
                        logger.debug(f"No specific thumbnail in DB for '{song_id_param}'. Will use fallback.")
            else:
                logger.warning(f"Failed to get DB connection in get_thumbnail_for_search_results for '{song_id_param}'.")
        except psycopg2.Error as e:
            logger.error(f"DB error in get_thumbnail_for_search_results for '{song_id_param}': {e}")
        except Exception as e:
            logger.error(f"Unexpected error during DB lookup in get_thumbnail_for_search_results for '{song_id_param}': {e}")
        finally:
            if conn:
                conn.close()

    # Fallback logic:
    # If song_id_param starts with "local-", it's a local file.
    if song_id_param.startswith("local-"):
        logger.debug(f"Using local placeholder thumbnail for local ID: {song_id_param}")
        try:
            # Ensure this static path is correct for your Flask app static files
            return url_for('static', filename='assets/img/defaults/default_local_thumb.png', _external=False)
        except RuntimeError: # If url_for is not available (e.g. app context issue)
            return '/static/assets/img/defaults/default_local_thumb.png' # Hardcoded fallback path
    else:
        # For non-local IDs (assumed YouTube video IDs or similar that can use YT's thumbnail pattern)
        # This is also the fallback if song_id_param was not found in the local_songs table during DB lookup.
        # Remove "local-" prefix if it was part of an ID that erroneously didn't get a DB thumbnail
        # and is now falling back to YT style (though ideally, IDs are clean).
        effective_video_id = song_id_param.split(':')[-1] # Simplistic way to clean common prefixes if any, otherwise uses original
        
        if not effective_video_id: # Handle empty string case after potential split
             logger.warning(f"Cannot generate fallback thumbnail for empty effective_video_id from original '{song_id_param}'.")
             try:
                 return url_for('static', filename='assets/img/defaults/default_thumb.png', _external=False)
             except RuntimeError:
                 return '/static/assets/img/defaults/default_thumb.png'

        logger.debug(f"Using YouTube-style fallback thumbnail for ID: {effective_video_id}")
        return f"https://i.ytimg.com/vi/{effective_video_id}/hqdefault.jpg"


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



def extract_video_info(url, ydl_opts):
    """Extract single video information"""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                return {
                    "id": info.get('id', ''),
                    "title": info.get('title', 'Unknown'),
                    "artist": info.get('artist', info.get('uploader', 'Unknown Artist')),
                    "album": info.get('album', ''),
                    "duration": int(info.get('duration', 0)),
                    "thumbnail": get_best_thumbnail(info.get('thumbnails', [])),
                }
    except Exception as e:
        logger.error(f"Error extracting video info: {e}")
    return None



@lru_cache(maxsize=1) # LRU cache might still be useful for the combined list
def get_default_songs():
    """Generate a cached list of default songs for empty queries. Uses PG-loaded songs."""
    current_local_songs = load_local_songs_from_db() # Ensures fresh data for the cache generation
    combined = []
    seen_ids = set()

    for song_id, song_data in current_local_songs.items():
        if song_id not in seen_ids:
            combined.append(song_data) # song_data is already a dict
            seen_ids.add(song_id)
    # You might want to add some truly "default" (e.g., hardcoded popular) songs if local_songs is empty
    return combined


# --- Song Info (YouTube API based, unchanged from original) ---
def get_video_info(video_id_param): # Renamed param
    try:
        song_info_yt = ytmusic.get_song(video_id_param) # Renamed var
        details = song_info_yt.get("videoDetails", {})
        title = details.get("title", "Unknown Title")
        artist = details.get("author", "Unknown Artist")
        thumbnails_list = details.get("thumbnail", {}).get("thumbnails", []) # Renamed var
        thumbnail = (max(thumbnails_list, key=lambda x: x.get("width", 0))["url"]
                     if thumbnails_list else f"https://i.ytimg.com/vi/{video_id_param}/maxresdefault.jpg")
        return {"title": title, "artist": artist, "thumbnail": thumbnail, "video_id": video_id_param, "id": video_id_param, "duration": int(details.get("lengthSeconds", 0))}
    except Exception: # Broad exception for API failures
        logger.warning(f"YTMusic API failed for {video_id_param}. Falling back to page scrape.")
        url = f"https://www.youtube.com/watch?v={video_id_param}"
        headers = {'User-Agent': 'Mozilla/5.0'} # Basic user agent
        try:
            response = requests.get(url, headers=headers, timeout=5) # Added timeout
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                og_title = soup.find("meta", property="og:title")
                title_scrape = og_title["content"] if og_title else "Unknown Title (Scraped)" # Renamed var
                # Artist is hard to get reliably from scrape
                return {"title": title_scrape, "artist": "Unknown Artist",
                        "thumbnail": f"https://i.ytimg.com/vi/{video_id_param}/maxresdefault.jpg",
                        "video_id": video_id_param, "id": video_id_param, "duration": 0} # Duration unknown from scrape
        except requests.RequestException as req_err:
            logger.error(f"Request failed during scrape for {video_id_param}: {req_err}")
    # Final fallback if all else fails
    return {"title": "Unknown Title", "artist": "Unknown Artist",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id_param}/maxresdefault.jpg",
            "video_id": video_id_param, "id": video_id_param, "duration": 0}

# --- Unified Media Info (PostgreSQL Ready for local part) ---
def get_media_info(media_id_param): # Renamed param
    """
    Returns media details. For 'local-' IDs, uses PG-loaded cache. Otherwise, YouTube.
    """
    current_local_songs = local_songs_data_cache # Use the global cache
    if not current_local_songs: # If cache is empty, try loading it
        logger.info("local_songs_data_cache is empty in get_media_info, attempting reload.")
        current_local_songs = load_local_songs_from_db()


    if media_id_param.startswith("local-"):
        details = current_local_songs.get(media_id_param)
        if details:
            # Ensure consistent structure, 'video_id' might be expected by some templates/JS
            details["video_id"] = media_id_param # Already has 'id' from DB loading
            return details
        else: # If not in cache, it means it's not in DB or path was invalid
            logger.warning(f"Local song {media_id_param} not found in loaded cache.")
            return {"id": media_id_param, "video_id": media_id_param, "title": "Unknown Local Title",
                    "artist": "Unknown Artist", "thumbnail": "", "duration": 0}
    else:
        # For non-local, assume it's a YouTube ID
        return get_video_info(media_id_param)




def extract_playlist_info(url, max_workers=4):
    try:
        ydl_opts = {'quiet': True, 'extract_flat': True, 'force_generic_extractor': False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and 'entries' in info:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    extract_func = partial(extract_video_info, ydl_opts=ydl_opts)
                    video_urls = [entry['url'] if 'url' in entry else f"https://youtube.com/watch?v={entry['id']}"
                                  for entry in info['entries'] if entry]
                    results = list(executor.map(extract_func, video_urls))
                    return [r for r in results if r]
            return []
    except Exception as e:
        logger.error(f"Error extracting playlist info: {e}")
        return []
    






@bp.route('/getproject-info')
def get_project_info():
    version_file = os.path.join(os.getcwd(), "version-info", "version.md")
    try:
        with open(version_file, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        return Response(markdown_content, mimetype='text/plain; charset=utf-8')
    except FileNotFoundError:
        logger.error(f"Markdown file not found at {version_file}")
        abort(404, description="Project information file not found.")
    except Exception as e:
        logger.error(f"Error reading markdown file: {e}")
        abort(500, description="Could not read project information file.")

# --- Play Sequence (Next/Previous) ---
@bp.route("/api/play-sequence/<song_id>/<action>")
@login_required
def api_play_sequence(song_id, action):
    user_id = session['user_id']
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Get the latest entry for the current song to establish context
            cur.execute("""
                SELECT session_id, sequence_number FROM user_play_queue_history
                WHERE user_id = %s AND song_id = %s
                ORDER BY played_at DESC LIMIT 1
            """, (user_id, song_id))
            current_song_context = cur.fetchone()

            if not current_song_context and action == "previous":
                 logger.warning(f"Cannot go to previous: song {song_id} not in history for user {user_id}.")
                 return jsonify({"error": "Current song not in history, cannot determine previous."}), 404

            current_session_id = current_song_context['session_id'] if current_song_context else None
            current_seq_no = current_song_context['sequence_number'] if current_song_context else -1

            if action == "previous":
                if not current_session_id: # Should have been caught above
                     return jsonify({"error": "Cannot determine current play session for 'previous'."}), 400

                cur.execute("""
                    SELECT song_id, title, artist, thumbnail FROM user_play_queue_history
                    WHERE user_id = %s AND session_id = %s AND sequence_number < %s
                    ORDER BY sequence_number DESC LIMIT 1
                """, (user_id, current_session_id, current_seq_no))
                prev_song_db = cur.fetchone() # Renamed var
                if prev_song_db:
                    logger.info(f"Found previous song in PG: {prev_song_db['song_id']}")
                    # Use get_media_info to fetch full details (local or YT)
                    return jsonify(get_media_info(prev_song_db['song_id']))
                else:
                    logger.info(f"No previous song in PG session {current_session_id} for user {user_id}.")
                    return jsonify({"error": "No previous song found in this session"}), 404

            elif action == "next":
                logger.info(f"Next song requested for {song_id}. Prioritizing LLM.")
                llm_recommendation = None
                try:
                    # CRITICAL: llm_recommender.recommend_next_song might need DB access (PG now)
                    # If it queries user history or stats, it must be updated for PostgreSQL.
                    llm_recommendation = llm_recommender.recommend_next_song(
                        current_song_id=song_id, user_id=user_id, ytmusic_client=ytmusic
                    )
                except Exception as llm_err:
                    logger.error(f"Error calling LLM recommender: {llm_err}", exc_info=True)

                if llm_recommendation and llm_recommendation.get('type') == 'available':
                    recommended_id = llm_recommendation['id']
                    logger.info(f"LLM recommended: {recommended_id}")
                    # Use get_media_info to get full details
                    song_details = get_media_info(recommended_id)
                    if song_details and song_details.get("title", "").lower() != "unknown title":
                        return jsonify(song_details)
                    else:
                        logger.warning(f"LLM recommended ID {recommended_id} but get_media_info failed. Falling back.")

                logger.warning(f"LLM failed for {song_id} or no match. Using YTMusic fallback.")
                try:
                    if song_id.startswith("local-"): # For local, YTMusic watch playlist isn't direct
                        # util.get_fallback_recommendations might need PG updates if it uses history
                        return util.fallback_recommendations()

                    fb_watch_playlist = ytmusic.get_watch_playlist(videoId=song_id, limit=5)
                    if fb_watch_playlist and "tracks" in fb_watch_playlist:
                        for track in fb_watch_playlist["tracks"]:
                            fb_id = track.get("videoId")
                            if fb_id and fb_id != song_id and track.get("isAvailable", True):
                                # Return the first valid recommendation directly
                                return jsonify({
                                    "id": fb_id, "title": track.get("title", "Unknown"),
                                    "artist": track["artists"][0]["name"] if track.get("artists") else "Unknown Artist",
                                    "thumbnail": f"https://i.ytimg.com/vi/{fb_id}/hqdefault.jpg",
                                    "duration": track.get("duration_seconds", 0) # Added duration
                                })
                    return util.fallback_recommendations() # Final fallback
                except Exception as fallback_err:
                    logger.error(f"Error during YTMusic fallback: {fallback_err}")
                    return util.fallback_recommendations()
            else:
                return jsonify({"error": "Invalid action specified"}), 400
    except psycopg2.Error as e_db:
        logger.error(f"PostgreSQL error in /api/play-sequence: {e_db}")
        if conn: conn.rollback()
        return jsonify({"error": "Database service error"}), 500
    except Exception as e_gen:
        logger.error(f"Unexpected error in /api/play-sequence for {song_id}, action {action}: {e_gen}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn: conn.close()




# --- Health Check (Unchanged) ---
@bp.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "message": "Server is running"}), 200
def _s_get_best_thumbnail(thumbnails_list):
    if not thumbnails_list or not isinstance(thumbnails_list, list):
        return ''
    return max(thumbnails_list, key=lambda x: x.get('width', 0) * x.get('height', 0), default={}).get('url', '')

def _s_process_description(description_text):
    if not description_text:
        return ''
    paragraphs = description_text.split('\n\n')
    processed_desc = paragraphs[0]
    if len(processed_desc) > 700: # Increased limit slightly
        processed_desc = processed_desc[:697] + "..."
    return processed_desc

def _s_extract_year(artist_data_ytmusic):
    if artist_data_ytmusic and isinstance(artist_data_ytmusic, dict):
        # YTMusic API's get_artist does not reliably provide a 'year'.
        # This might be in the description or another field.
        # Attempt to parse from description if it exists and contains year-like patterns.
        description = artist_data_ytmusic.get('description', '')
        if description:
            # Look for patterns like "formed in YYYY", "active since YYYY", "released ... YYYY"
            year_match = re.search(r'\b(19[89]\d|20\d{2})\b', description) # Matches 1980-2099
            if year_match:
                return year_match.group(0)
        return artist_data_ytmusic.get('year') # Fallback to a 'year' field if it ever exists
    return None

def _s_process_genres(artist_data_ytmusic):
    if artist_data_ytmusic and isinstance(artist_data_ytmusic, dict):
         # Genres are not a standard field in `get_artist`.
         # If description contains keywords, they could be parsed.
         # For now, this remains a placeholder or relies on a hypothetical field.
        return artist_data_ytmusic.get('genres', [])
    return []

def _s_get_artist_stats(artist_data_ytmusic):
    # Removed subscribers and views as per user request.
    # Monthly listeners are also not standard in YTMusic.
    # This function can be simplified or removed if no relevant stats are available.
    stats = {'monthlyListeners': '-'} # Defaulting to '-'
    if not artist_data_ytmusic or not isinstance(artist_data_ytmusic, dict):
        return stats
    # If YTMusic API ever adds relevant, non-subscriber/view stats, they can be processed here.
    # For now, it will likely always return {'monthlyListeners': '-'}
    return stats

def _s_process_top_songs(artist_data_ytmusic):
    top_songs = []
    if not artist_data_ytmusic or not isinstance(artist_data_ytmusic, dict):
        return top_songs

    songs_section = artist_data_ytmusic.get('songs')
    if songs_section:
        song_list_raw = []
        if isinstance(songs_section, dict) and 'results' in songs_section:
            song_list_raw = songs_section['results']
        elif isinstance(songs_section, list):
            song_list_raw = songs_section
        
        for song_item in song_list_raw[:5]: # Limit to top 5 for display
            if not song_item or not isinstance(song_item, dict):
                continue
            
            video_id = song_item.get('videoId')
            if not video_id:
                continue

            title = song_item.get('title', 'Unknown Title')
            artists_raw = song_item.get('artists')
            artist_display_name = "Unknown Artist" # This is the artist(s) of the song itself
            if isinstance(artists_raw, list) and artists_raw:
                artist_display_name = ', '.join([a.get('name', '') for a in artists_raw if a.get('name')])
                if not artist_display_name: artist_display_name = "Unknown Artist"
            
            thumbnail_url = _s_get_best_thumbnail(song_item.get('thumbnails', []))
            if not thumbnail_url and video_id:
                 thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" # Use mqdefault for song thumbs
            
            duration_seconds = 0
            duration_text = song_item.get('duration') # e.g. "3:45"
            if duration_text and isinstance(duration_text, str) and ':' in duration_text:
                parts = duration_text.split(':')
                try:
                    if len(parts) == 2: duration_seconds = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3: duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except ValueError: pass
            elif song_item.get('duration_seconds'): # Check if API provides it directly
                duration_seconds = song_item.get('duration_seconds')

            top_songs.append({
                'id': video_id,
                'videoId': video_id,
                'title': title,
                'artist': artist_display_name, # Artist(s) of this popular song
                'thumbnail': thumbnail_url,
                'duration': duration_seconds,
                'plays': song_item.get('viewCountText') or song_item.get('views', '-') # viewCountText is often better
            })
    return top_songs

def _s_process_artist_links(artist_data_ytmusic, artist_browse_id):
    links = {}
    if not artist_data_ytmusic or not isinstance(artist_data_ytmusic, dict):
        return links
        
    if artist_browse_id:
        links['youtube_music'] = f"https://music.youtube.com/channel/{artist_browse_id}"
    
    # Attempt to find other links if provided (e.g. from description if explicitly listed)
    # This part remains basic as YTMusic API is not rich in these external links.
    # For example, if there was a 'website' field: links['official_site'] = artist_data_ytmusic.get('website')
    return links


@bp.route('/api/artist-info/<path:artist_name_query>')
@login_required
def get_artist_info_standalone(artist_name_query):
    # The artist_name_query could be "Artist A" or "Artist A ft. Artist B"
    # This route will focus on the *primary* artist name provided for simplicity of the backend.
    # The frontend can call this route multiple times if it parses multiple artists from a song.
    primary_artist_name = artist_name_query.split(',')[0].split(' ft.')[0].split(' feat.')[0].split(' & ')[0].strip()
    
    logger.info(f"Fetching standalone artist info for primary: '{primary_artist_name}' from query '{artist_name_query}'")

    default_response = {
        'name': primary_artist_name, # Return the name we tried to search for
        'thumbnail': '',
        'description': '', # Empty by default, frontend can show "Not available"
        'genres': [],
        'year': None,
        'stats': {}, # Stats removed
        'topSongs': [],
        'links': {},
        'error': None
    }

    try:
        # Search for the artist to get their browseId
        search_results = ytmusic.search(primary_artist_name, filter='artists', limit=1)
        
        artist_browse_id = None
        artist_data_from_search = None

        if search_results and search_results[0].get('browseId'):
            artist_data_from_search = search_results[0]
            artist_browse_id = artist_data_from_search.get('browseId')
        else: # Try a broader search if no direct artist match
            logger.info(f"No direct artist match for '{primary_artist_name}', trying general search.")
            general_search_results = ytmusic.search(primary_artist_name, limit=5)
            for r in general_search_results:
                if r.get('resultType') == 'artist' and r.get('browseId'):
                    artist_data_from_search = r
                    artist_browse_id = r.get('browseId')
                    logger.info(f"Found potential artist '{r.get('artist')}' via general search.")
                    break
        
        if not artist_browse_id:
            logger.warning(f"No artist browseId found for: '{primary_artist_name}' after searches.")
            default_response['error'] = 'Artist not found.'
            default_response['description'] = 'Information for this artist could not be found.'
            return jsonify(default_response), 404

        # Fetch detailed artist data using the browseId
        artist_data_yt = ytmusic.get_artist(artist_browse_id)
        if not artist_data_yt:
            logger.warning(f"Failed to fetch details for artist ID {artist_browse_id} ('{primary_artist_name}')")
            default_response['error'] = 'Could not fetch artist details.'
            default_response['description'] = 'Detailed information for this artist is currently unavailable.'
            return jsonify(default_response), 404
        
        # Process data using internal helper functions
        final_name = artist_data_yt.get('name', primary_artist_name) # Prefer API name
        thumbnail_url = _s_get_best_thumbnail(artist_data_yt.get('thumbnails', []))
        description = _s_process_description(artist_data_yt.get('description', ''))
        genres = _s_process_genres(artist_data_yt)
        year = _s_extract_year(artist_data_yt)
        # stats = _s_get_artist_stats(artist_data_yt) # Stats removed
        top_songs = _s_process_top_songs(artist_data_yt)
        links = _s_process_artist_links(artist_data_yt, artist_browse_id)
        
        return jsonify({
            'name': final_name,
            'thumbnail': thumbnail_url,
            'description': description,
            'genres': genres,
            'year': year,
            'stats': {}, # Explicitly empty as per removal of subs/views
            'topSongs': top_songs,
            'links': links
        })

    except Exception as e:
        logger.error(f"Error in get_artist_info_standalone for '{primary_artist_name}': {e}", exc_info=True)
        default_response['error'] = 'An internal server error occurred while fetching artist information.'
        default_response['description'] = 'Could not retrieve artist information at this time.'
        return jsonify(default_response), 500


# --- Spotify to YouTube Matching (Unchanged logic, ensure ytmusic instance is global) ---
def normalize_text(text): # Unchanged
    if not text: return ""
    text = text.lower()
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'(official music video|official audio|lyrics|lyric video|feat\.|ft\.)', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def find_youtube_match(sp_title, sp_artist, sp_duration_sec, ytmusic_api_instance): # Unchanged
    norm_sp_title = normalize_text(sp_title)
    norm_sp_artist = normalize_text(sp_artist)
    search_query = f"{sp_title} {sp_artist}" # Use original for search, normalized for comparison
    logger.info(f"Searching YTMusic for Spotify track: '{search_query}' (Duration: {sp_duration_sec}s)")
    try:
        results = ytmusic_api_instance.search(search_query, filter='songs', limit=10)
        if not results: return None
        duration_tolerance, min_title_ratio, min_artist_ratio, min_overall_score = 5, 85, 80, 170
        best_match_id, highest_score = None, -1

        for item in results:
            yt_video_id = item.get('videoId')
            yt_title = item.get('title')
            yt_artists_list = [a.get('name') for a in item.get('artists', [])]
            yt_duration_sec = item.get('duration_seconds')
            if not all([yt_video_id, yt_title, yt_artists_list, yt_duration_sec is not None]): continue
            if abs(yt_duration_sec - sp_duration_sec) > duration_tolerance: continue

            norm_yt_title = normalize_text(yt_title)
            norm_yt_primary_artist = normalize_text(yt_artists_list[0]) if yt_artists_list else ""
            title_ratio = fuzz.token_sort_ratio(norm_sp_title, norm_yt_title)
            artist_ratio_primary = fuzz.token_sort_ratio(norm_sp_artist, norm_yt_primary_artist)

            if title_ratio >= min_title_ratio and artist_ratio_primary >= min_artist_ratio:
                score = title_ratio + artist_ratio_primary
                if abs(yt_duration_sec - sp_duration_sec) <= 2: score += (3 - abs(yt_duration_sec - sp_duration_sec)) * 10
                if score >= min_overall_score and score > highest_score:
                    highest_score, best_match_id = score, yt_video_id
        if best_match_id: logger.info(f"Best YTMusic match for '{sp_title} - {sp_artist}': {best_match_id} (Score: {highest_score})")
        else: logger.warning(f"No suitable YTMusic match for '{sp_title} - {sp_artist}'.")
        return best_match_id
    except Exception as e:
        logger.error(f"Error during YTMusic search/match for '{search_query}': {e}")
    return None




@bp.route("/api/search")
@login_required
def api_search():
    """
    Enhanced search endpoint that handles:
    - Regular text search
    - YouTube/YouTube Music URLs (songs, playlists, albums)
    - Direct video IDs
    - Empty queries with local songs from Redis and default songs
    """
    # Load local songs from Redis instead of a file
    local_songs = load_local_songs_from_postgres()
    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 0))
    limit = int(request.args.get("limit", 20))
    results = [] # Initialize results list

    def get_thumbnail(song_id_param):
        """
        Fetches the thumbnail URL for a given song_id from the PostgreSQL database.
        Falls back to a default YouTube thumbnail URL if not found or if the thumbnail is empty.
        """
        conn = None
        try:
            conn = get_pg_connection()
            if not conn:
                logger.error("Failed to get database connection for thumbnail.")
                # Fallback if DB connection itself fails
                return f"https://i.ytimg.com/vi/{song_id_param}/hqdefault.jpg"

            c = conn.cursor()
            # The local_songs table has 'id' as the primary key and a 'thumbnail' column.
            c.execute("SELECT thumbnail FROM local_songs WHERE id = %s", (song_id_param,))
            result = c.fetchone()

            if result and result[0] and result[0].strip(): # Check if thumbnail exists and is not empty
                return result[0]
            else:
                # Fallback if song_id not found or thumbnail field is empty/null
                logger.info(f"Thumbnail not found in DB for song_id: {song_id_param}. Using fallback.")
                return f"https://i.ytimg.com/vi/{song_id_param}/hqdefault.jpg"

        except psycopg2.Error as e:
            logger.error(f"Database error while fetching thumbnail for song_id {song_id_param}: {e}")
            # Fallback in case of database error during query
            return f"https://i.ytimg.com/vi/{song_id_param}/hqdefault.jpg"
        except Exception as e:
            logger.error(f"An unexpected error occurred while fetching thumbnail for {song_id_param}: {e}")
            # Fallback for any other unexpected error
            return f"https://i.ytimg.com/vi/{song_id_param}/hqdefault.jpg"
        finally:
            if conn:
                if 'c' in locals() and c:
                    c.close()
                conn.close()
    if sp is None:
        logger.error("Spotipy client not available. Spotify link processing disabled.")
    

    # === 1. Process if input is a YouTube link ===
    if "youtube.com" in q or "youtu.be" in q:
        try:
            # Handle as a playlist if the URL contains "playlist" or "list="
            if "playlist" in q or "list=" in q:
                ydl_opts = {
                    'quiet': True,
                    'extract_flat': True,
                    'force_generic_extractor': False
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(q, download=False)
                    if info and 'entries' in info:
                        results = []
                        seen_ids = set()
                        for entry in info['entries']:
                            if not entry:
                                continue
                            video_id = entry.get('id')
                            if not video_id or video_id in seen_ids:
                                continue
                            thumbnail = get_thumbnail(video_id)
                            result = {
                                "id": video_id,
                                "title": entry.get('title', 'Unknown'),
                                "artist": entry.get('artist', entry.get('uploader', 'Unknown Artist')),
                                "album": entry.get('album', ''),
                                "duration": int(entry.get('duration', 0)),
                                "thumbnail": thumbnail
                            }
                            results.append(result)
                            seen_ids.add(video_id)
                            if len(results) >= limit:
                                break
                        start = page * limit
                        end = start + limit
                        return jsonify(results[start:end])

            # Handle as a single video/song
            parsed = urlparse(q)
            params = parse_qs(parsed.query)
            video_id = None
            if "youtu.be" in q:
                video_id = q.split("/")[-1].split("?")[0]
            elif "v" in params:
                video_id = params["v"][0]

            if video_id:
                ydl_opts = {
                    'quiet': True,
                    'extract_flat': False,
                    'force_generic_extractor': False
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"https://youtube.com/watch?v={video_id}", download=False)
                    if info:
                        thumbnail = get_thumbnail(video_id)
                        result = [{
                            "id": video_id,
                            "title": info.get('title', 'Unknown'),
                            "artist": info.get('artist', info.get('uploader', 'Unknown Artist')),
                            "album": info.get('album', ''),
                            "duration": int(info.get('duration', 0)),
                            "thumbnail": thumbnail
                        }]
                        return jsonify(result)
        except Exception as e:
            logger.error(f"Error processing link '{q}': {e}")
            # Fall through to regular search if link processing fails
    
    elif sp and ("spotify.com" in q or q.startswith("spotify:")):
        logger.info(f"Attempting to process as Spotify input: {q}")
        # results = [] # Results list is already initialized above
        spotify_id = None
        spotify_type = None

        try:
            # --- Multi-method Spotify ID and Type Extraction ---
            # Method 1: Regex for specific open.spotify.com/album/7... links
            track_match_specific = re.search(r'open.spotify.com/track//([a-zA-Z0-9]+)', q)
            album_match_specific = re.search(r'open.spotify.com/album//([a-zA-Z0-9]+)', q)
            playlist_match_specific = re.search(r'open.spotify.com/playlist//([a-zA-Z0-9]+)', q)

            if track_match_specific: spotify_id=track_match_specific.group(1); spotify_type='track'; logger.info(f"M1: Track ID {spotify_id}")
            elif album_match_specific: spotify_id=album_match_specific.group(1); spotify_type='album'; logger.info(f"M1: Album ID {spotify_id}")
            elif playlist_match_specific: spotify_id=playlist_match_specific.group(1); spotify_type='playlist'; logger.info(f"M1: Playlist ID {spotify_id}")

            # Method 2: Parse standard open.spotify.com or open.spotify.com/album/8... URLs
            if not spotify_id and ("open.spotify.com/album/8" in q or "open.spotify.com" in q):
                try:
                    parsed_url = urlparse(q)
                    path_parts = [part for part in parsed_url.path.split('/') if part]
                    if len(path_parts) >= 2:
                        possible_type = path_parts[-2].lower(); possible_id = path_parts[-1]
                        if possible_type in ['track', 'album', 'playlist'] and re.match(r'^[a-zA-Z0-9]+$', possible_id):
                            spotify_type=possible_type; spotify_id=possible_id; logger.info(f"M2: Type {spotify_type}, ID {spotify_id}")
                except Exception as parse_err: logger.warning(f"M2 Parse Error: {parse_err}")

            # Method 3: Check for Spotify URI format
            if not spotify_id and q.startswith("spotify:"):
                parts = q.split(':')
                if len(parts) == 3 and parts[1] in ['track', 'album', 'playlist']:
                    spotify_type=parts[1]; spotify_id=parts[2]; logger.info(f"M3: URI Type {spotify_type}, ID {spotify_id}")
            # --- End of ID/Type extraction ---


            # --- Use extracted ID/Type -> Call Spotipy -> Find YouTube Match ---
            if spotify_id and spotify_type:
                processed_count = 0

                if spotify_type == 'track':
                    track_info = sp.track(spotify_id)
                    if track_info:
                        sp_title = track_info['name']
                        sp_artist = track_info['artists'][0]['name'] if track_info['artists'] else 'Unknown'
                        sp_duration = track_info['duration_ms'] // 1000
                        sp_album = track_info['album']['name']
                        sp_thumbnail = track_info['album']['images'][0]['url'] if track_info['album']['images'] else None

                        youtube_id = find_youtube_match(sp_title, sp_artist, sp_duration, ytmusic)
                        if youtube_id:
                            results.append({
                                "id": youtube_id, "title": sp_title,
                                "artist": ", ".join([a['name'] for a in track_info['artists']]),
                                "album": sp_album, "duration": sp_duration,
                                "thumbnail": sp_thumbnail, "source": "spotify_yt"
                            })
                            processed_count += 1
                        else: logger.warning(f"No YT match for Spotify track: {sp_title}")

                elif spotify_type == 'album':
                    album_info = sp.album(spotify_id)
                    album_thumbnail = album_info['images'][0]['url'] if album_info['images'] else None
                    album_name = album_info['name']
                    album_data = sp.album_tracks(spotify_id, limit=50) # Fetch more tracks initially
                    if album_data and album_data['items']:
                        # Consider parallelizing the YT search for albums
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                            future_to_sp_track = {}
                            for item in album_data['items']:
                                if processed_count >= limit: break # Check overall limit early
                                sp_title = item['name']
                                sp_artist = item['artists'][0]['name'] if item['artists'] else 'Unknown'
                                sp_duration = item['duration_ms'] // 1000
                                # Submit search to thread pool
                                future = executor.submit(find_youtube_match, sp_title, sp_artist, sp_duration, ytmusic)
                                future_to_sp_track[future] = item # Store original item data

                            for future in concurrent.futures.as_completed(future_to_sp_track):
                                if processed_count >= limit: break # Check limit again
                                sp_track_item = future_to_sp_track[future]
                                try:
                                    youtube_id = future.result()
                                    if youtube_id:
                                        results.append({
                                            "id": youtube_id,
                                            "title": sp_track_item['name'],
                                            "artist": ", ".join([a['name'] for a in sp_track_item['artists']]),
                                            "album": album_name,
                                            "duration": sp_track_item['duration_ms'] // 1000,
                                            "thumbnail": album_thumbnail, # Use album cover
                                            "source": "spotify_yt"
                                        })
                                        processed_count += 1
                                    else:
                                        logger.warning(f"No YT match (in thread) for Spotify album track: {sp_track_item['name']}")
                                except Exception as exc:
                                    logger.error(f"Error in album track YT search thread: {exc}")


                elif spotify_type == 'playlist':
                    # Similar parallel logic for playlists
                    playlist_data = sp.playlist_items(spotify_id, limit=50, # Fetch more tracks initially
                                                     fields='items(track(id,name,artists(name),album(name,images),duration_ms,uri))')
                    if playlist_data and playlist_data['items']:
                         with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                            future_to_sp_track = {}
                            for item in playlist_data['items']:
                                if processed_count >= limit: break
                                track = item.get('track')
                                if track and track.get('id'):
                                     sp_title = track['name']
                                     sp_artist = track['artists'][0]['name'] if track['artists'] else 'Unknown'
                                     sp_duration = track['duration_ms'] // 1000
                                     future = executor.submit(find_youtube_match, sp_title, sp_artist, sp_duration, ytmusic)
                                     future_to_sp_track[future] = track # Store original track data

                            for future in concurrent.futures.as_completed(future_to_sp_track):
                                if processed_count >= limit: break
                                sp_track_data = future_to_sp_track[future]
                                try:
                                    youtube_id = future.result()
                                    if youtube_id:
                                        sp_thumbnail = sp_track_data['album']['images'][0]['url'] if sp_track_data['album']['images'] else None
                                        results.append({
                                            "id": youtube_id,
                                            "title": sp_track_data['name'],
                                            "artist": ", ".join([a['name'] for a in sp_track_data['artists']]),
                                            "album": sp_track_data['album']['name'],
                                            "duration": sp_track_data['duration_ms'] // 1000,
                                            "thumbnail": sp_thumbnail,
                                            "source": "spotify_yt"
                                        })
                                        processed_count += 1
                                    else:
                                         logger.warning(f"No YT match (in thread) for Spotify playlist track: {sp_track_data['name']}")
                                except Exception as exc:
                                    logger.error(f"Error in playlist track YT search thread: {exc}")


                # If Spotify processing happened and found matches
                if results:
                    # Apply pagination if needed (although limit was mostly handled above)
                    start = page * limit
                    end = start + limit
                    return jsonify(results[start:end])

            # If Spotify ID/Type extraction failed or no matches found after search
            logger.info(f"Could not extract/match valid YouTube data for Spotify input: {q}")
            # Fall through to next checks

        except spotipy.SpotifyException as e:
            logger.error(f"Spotify API error processing input '{q}': {e.http_status} - {e.msg}")
        except Exception as e:
            logger.error(f"Generic error processing Spotify input '{q}': {e}")

    # === 2. Process if input is a direct YouTube video ID ===
    if re.match(r'^[a-zA-Z0-9_-]{11}$', q):
        try:
            ydl_opts = {
                'quiet': True,
                'extract_flat': False,
                'force_generic_extractor': False
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://youtube.com/watch?v={q}", download=False)
                if info:
                    thumbnail = get_thumbnail(q)
                    result = [{
                        "id": q,
                        "title": info.get('title', 'Unknown'),
                        "artist": info.get('artist', info.get('uploader', 'Unknown Artist')),
                        "album": info.get('album', ''),
                        "duration": int(info.get('duration', 0)),
                        "thumbnail": thumbnail
                    }]
                    return jsonify(result)
        except Exception as e:
            logger.error(f"Error processing video ID '{q}': {e}")
            return jsonify([])

    # === 3. Process empty query: Combine local songs from Redis and default songs ===
    if not q:
        combined_res = []
        seen_ids = set()
        # Add all local songs from Redis
        local_songs_list = list(local_songs.values())
        for song in local_songs_list:
            if song["id"] not in seen_ids:
                combined_res.append(song)
                seen_ids.add(song["id"])
        # Add default songs
        default_songs = get_default_songs()
        for song in default_songs:
            if song["id"] not in seen_ids:
                song["thumbnail"] = get_thumbnail(song["id"])
                combined_res.append(song)
                seen_ids.add(song["id"])
        start = page * limit
        end = start + limit
        return jsonify(combined_res[start:end])

    # === 4. Regular text search ===
    seen_ids = set()
    combined_res = []

    # Add local songs matching the query
    local_res = util.filter_local_songs_pg(q)
    for song in local_res:
        if song["id"] not in seen_ids:
            combined_res.append(song)
            seen_ids.add(song["id"])

    # Define helper function to search using yt-dlp
    def search_ytdlp():
        try:
            ydl_opts = {
                'quiet': True,
                'extract_flat': True,
                'force_generic_extractor': False
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
                results = []
                if info and 'entries' in info:
                    for entry in info['entries']:
                        if not entry:
                            continue
                        video_id = entry.get('id')
                        if not video_id:
                            continue
                        thumbnail = get_thumbnail(video_id)
                        results.append({
                            "id": video_id,
                            "title": entry.get('title', 'Unknown'),
                            "artist": entry.get('artist', entry.get('uploader', 'Unknown Artist')),
                            "album": entry.get('album', ''),
                            "duration": int(entry.get('duration', 0)),
                            "thumbnail": thumbnail
                        })
                return results
        except Exception as e:
            logger.error(f"Error in YouTube search via yt-dlp: {e}")
            return []

    # Run both search methods concurrently
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_utmusic = executor.submit(util.search_songs, q)
        future_yt = executor.submit(search_ytdlp)
        utmusic_results = future_utmusic.result()
        yt_results = future_yt.result()

    # Merge results with YTMusic results first, then yt-dlp results
    for song in (utmusic_results + yt_results):
        if song["id"] not in seen_ids:
            # Check Redis for thumbnail if not already set
            if not song.get("thumbnail"):
                song["thumbnail"] = get_thumbnail(song["id"])
            combined_res.append(song)
            seen_ids.add(song["id"])

    start = page * limit
    end = start + limit
    return jsonify(combined_res[start:end])





# --- Song Info API (PostgreSQL Ready via get_media_info) ---
@bp.route("/api/song-info/<song_id>")
@login_required # Usually good to keep this to track usage, even if info is public
def api_song_info(song_id):
    media_details = get_media_info(song_id) # Uses PG-aware get_media_info
    if not media_details or (media_details.get("title", "Unknown Title").lower() in ["unknown title", "unknown local title"]):
        # Specific check for YT IDs that might be valid but lack full metadata from get_video_info initial fetch
        if not song_id.startswith("local-") and util.is_potential_video_id(song_id):
            logger.warning(f"Song info for {song_id} was basic; returning minimal structure.")
            return jsonify({
                "id": song_id, "title": media_details.get("title", f"Video ID: {song_id}"),
                "artist": media_details.get("artist", "Unknown Artist"),
                "album": media_details.get("album", ""),
                "thumbnail": media_details.get("thumbnail") or f"https://i.ytimg.com/vi/{song_id}/hqdefault.jpg",
                "duration": int(media_details.get("duration", 0))
            })
        logger.warning(f"api_song_info: Song {song_id} not found or metadata unavailable.")
        return jsonify({"error": "Song not found or metadata unavailable"}), 404
    return jsonify(media_details) # get_media_info should return a consistent dict


# --- Recommendations (YTMusic API based, util.py dependent) ---
@bp.route('/api/get-recommendations/<song_id>')
@login_required
def api_get_recommendations(song_id):
    # This route primarily uses ytmusic and util functions.
    # util.get_local_song_recommendations and util.add_recommendation, util.fallback_recommendations
    # would need PG updates if they analyze user's listening_history or user_statistics.
    try:
        if song_id.startswith("local-"):
            # util.get_local_song_recommendations needs PG update if it queries history/stats
            return util.get_local_song_recommendations(song_id) # Pass ytmusic if needed

        recommendations, seen_songs = [], set()
        song_info_rec = ytmusic.get_song(song_id) # Renamed
        if not song_info_rec: return util.fallback_recommendations() # util.py

        # 1. Watch playlist (most reliable)
        try:
            watch_playlist = ytmusic.get_watch_playlist(videoId=song_id, limit=15) # More initial candidates
            if watch_playlist and "tracks" in watch_playlist:
                for track in watch_playlist["tracks"]:
                    # util.add_recommendation needs to be robust
                    if util.add_recommendation(track, recommendations, seen_songs, song_id, ytmusic): # Pass ytmusic
                        if len(recommendations) >= 5: break # Target 5 good ones
                if len(recommendations) >= 5: return jsonify(recommendations)
        except Exception as e_wp: logger.warning(f"Watch playlist error for recommendations ({song_id}): {e_wp}")

        # 2. Artist's songs (if not enough from watch playlist)
        if len(recommendations) < 5 and song_info_rec.get("artists"):
            try:
                artist_id_rec = song_info_rec["artists"][0].get("id") # Renamed
                if artist_id_rec:
                    artist_data_rec = ytmusic.get_artist(artist_id_rec) # Renamed
                    if artist_data_rec and "songs" in artist_data_rec and artist_data_rec["songs"]: # Check songs exist
                        artist_songs_list = list(artist_data_rec["songs"].get("results", [])) # Get actual list if nested
                        if not artist_songs_list and isinstance(artist_data_rec["songs"], list): # Fallback if not nested
                             artist_songs_list = artist_data_rec["songs"]
                        random.shuffle(artist_songs_list)
                        for track in artist_songs_list:
                            if util.add_recommendation(track, recommendations, seen_songs, song_id, ytmusic):
                                if len(recommendations) >= 5: break
                        if len(recommendations) >= 5: return jsonify(recommendations)
            except Exception as e_as: logger.warning(f"Artist recommendations error ({song_id}): {e_as}")

        # 3. Search similar (if still not enough) - this can be broad
        if len(recommendations) < 5:
            try:
                title_rec = song_info_rec.get("videoDetails", {}).get("title", "") # Renamed
                # artist_name_rec = song_info_rec.get("videoDetails", {}).get("author", "") # Renamed
                # Simplified search query for broader results if specific artist search fails
                search_query_rec = title_rec # Renamed
                if song_info_rec.get("artists"):
                    search_query_rec = f"{title_rec} {song_info_rec['artists'][0]['name']}"

                if search_query_rec: # Only search if we have a query
                    search_results_rec = ytmusic.search(search_query_rec, filter="songs", limit=10) # Renamed
                    for track in search_results_rec:
                        if util.add_recommendation(track, recommendations, seen_songs, song_id, ytmusic):
                            if len(recommendations) >= 5: break
                    if len(recommendations) >= 5: return jsonify(recommendations)
            except Exception as e_sr: logger.warning(f"Search recommendations error ({song_id}): {e_sr}")

        if not recommendations: return util.fallback_recommendations()
        random.shuffle(recommendations) # Shuffle if we have some but less than 5
        return jsonify(recommendations[:5])

    except Exception as e_main_rec:
        logger.error(f"General recommendations error for {song_id}: {e_main_rec}")
        return util.fallback_recommendations()




@bp.route("/api/insights")
@login_required
def get_insights():
    user_id = session['user_id']
    # CRITICAL: All util functions (get_overview_stats, get_recent_activity, etc.)
    # MUST be updated to query PostgreSQL 'listening_history' and 'user_statistics' tables.
    # This route itself doesn't change if util functions are correctly adapted.
    # For safety, I'll add a try-except for the util calls.
    try:
        insights = {
            "overview": util.get_overview_stats_pg(user_id), # Assuming renamed util for PG
            "recent_activity": util.get_recent_activity_pg(user_id),
            "top_artists": util.get_top_artists_pg(user_id),
            "listening_patterns": util.get_listening_patterns_pg(user_id),
            "completion_rates": util.get_completion_rates_pg(user_id)
        }
        return jsonify(insights)
    except AttributeError as ae:
        logger.error(f"Insights error: A utility function might not be updated for PostgreSQL or is missing. {ae}", exc_info=True)
        return jsonify({"error": "Insights data partially unavailable due to internal error."}), 500
    except Exception as e_insights: # Renamed
        logger.error(f"Error generating insights for user {user_id}: {e_insights}", exc_info=True)
        return jsonify({"error": "Could not generate insights."}), 500


@bp.route("/api/listen/start", methods=["POST"])
@login_required
def api_listen_start():
    # CRITICAL: util.record_listen_start MUST be updated for PostgreSQL 'listening_history' table.
    # It should handle inserting a new record and returning its new ID (SERIAL from PG).
    try:
        user_id = session['user_id']
        data = request.json
        if not data or not all(k in data for k in ["songId", "title", "artist"]):
            return jsonify({"error": "Missing required fields for listen/start"}), 400

        session_id_ls = util.generate_session_id() # Assumed util.py helper, no DB
        # util.record_listen_start should return the PostgreSQL generated listen_id
        listen_id_ls = util.record_listen_start_pg( # Assuming renamed util for PG
            user_id=user_id, song_id=data["songId"], title=data["title"],
            artist=data["artist"], session_id=session_id_ls,
            duration=data.get("duration", 0) # Pass duration if available at start
        )
        if listen_id_ls is None: # If util function indicates failure
            return jsonify({"error": "Failed to record listen start"}), 500
        return jsonify({"status": "success", "listenId": listen_id_ls, "sessionId": session_id_ls})
    except AttributeError as ae:
         logger.error(f"Listen start error: util.record_listen_start_pg might be missing or not updated. {ae}", exc_info=True)
         return jsonify({"error": "Internal error processing listen start."}), 500
    except Exception as e_ls: # Renamed
        logger.error(f"Listen start error: {e_ls}", exc_info=True)
        return jsonify({"error": "An error occurred while starting listen record."}), 500


@bp.route("/api/listen/end", methods=["POST"])
@login_required
def api_listen_end():
    conn_le = None # Renamed
    try:
        user_id = session['user_id']
        data = request.json
        if not data or "listenId" not in data:
            return jsonify({"error": "Missing listenId"}), 400
        listen_id_le = int(data["listenId"]) # Renamed

        conn_le = get_pg_connection()
        with conn_le.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_le: # Renamed
            # Verify listen_id belongs to user and is not ended
            cur_le.execute("SELECT user_id, ended_at FROM listening_history WHERE id = %s", (listen_id_le,))
            listen_entry_le = cur_le.fetchone() # Renamed
        # conn_le remains open for util.record_listen_end_pg if it takes a connection object
        # or util function will open its own.

        if not listen_entry_le or listen_entry_le['user_id'] != user_id:
            return jsonify({"error": "Invalid listen ID or unauthorized"}), 403
        if listen_entry_le['ended_at'] is not None: # Already ended
             logger.warning(f"Listen ID {listen_id_le} already marked as ended.")
             return jsonify({"status": "already_ended", "message": "Listen session was already ended."})


        # CRITICAL: util.record_listen_end_pg MUST update PostgreSQL 'listening_history'
        util.record_listen_end_pg( # Assuming renamed util for PG
            listen_id=listen_id_le,
            listened_duration=data.get("listenedDuration", 0),
            # duration can be passed again if it's more accurate now, or rely on what was stored at start
            # duration=data.get("duration", 0) # Optional
        )
        return jsonify({"status": "success", "message": "Listen session ended."})
    except ValueError:
        return jsonify({"error": "Invalid listenId format."}), 400
    except psycopg2.Error as db_err_le: # Renamed
        logger.error(f"Listen end DB error: {db_err_le}")
        return jsonify({"error": "Database error during listen end"}), 500
    except AttributeError as ae:
        logger.error(f"Listen end error: util.record_listen_end_pg might be missing or not updated. {ae}", exc_info=True)
        return jsonify({"error": "Internal error processing listen end."}), 500
    except Exception as e_le: # Renamed
        logger.error(f"Listen end error: {e_le}", exc_info=True)
        return jsonify({"error": "An error occurred while ending listen record."}), 500
    finally:
        if conn_le: conn_le.close() # Close if it was opened here


@bp.route('/api/queue', methods=['GET'])
@login_required
def api_queue():
    user_id = session['user_id']
    limit = int(request.args.get('limit', 20)) # Default for queue display
    offset = int(request.args.get('offset', 0))
    conn_q = None # Renamed
    history_items_q = [] # Renamed
    try:
        conn_q = get_pg_connection()
        with conn_q.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_q: # Renamed
            # Fetches from denormalized user_play_queue_history
            cur_q.execute("""
                SELECT song_id, title, artist, thumbnail, played_at, session_id, sequence_number
                FROM user_play_queue_history
                WHERE user_id = %s
                ORDER BY played_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            for row_q in cur_q.fetchall(): # Renamed
                history_items_q.append({
                    "id": row_q['song_id'],
                    "title": row_q['title'] or "Unknown Title",
                    "artist": row_q['artist'] or "Unknown Artist",
                    "thumbnail": row_q['thumbnail'] or get_thumbnail_for_search_results(row_q['song_id'], False), # Fallback thumb
                    "played_at": row_q['played_at'].isoformat() if row_q['played_at'] else None,
                    "session_id": row_q['session_id'],
                    "sequence_number": row_q['sequence_number']
                })
        return jsonify(history_items_q)
    except psycopg2.Error as e_q_db: # Renamed
        logger.error(f"Error fetching PG queue history for user {user_id}: {e_q_db}")
        return jsonify({"error": "Database error fetching queue"}), 500
    except Exception as e_q_gen: # Renamed
        logger.error(f"Error fetching queue for user {user_id}: {e_q_gen}", exc_info=True)
        return jsonify({"error": "An error occurred while fetching queue."}), 500
    finally:
        if conn_q: conn_q.close()


@bp.route("/api/stats")
@login_required
def api_stats():
    user_id = session['user_id']
    conn_stats = None # Renamed
    try:
        conn_stats = get_pg_connection()
        stats_bundle = {} # Renamed

        with conn_stats.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_stats: # Renamed
            # From user_statistics table
            cur_stats.execute("SELECT * FROM user_statistics WHERE user_id = %s", (user_id,))
            user_stats_db = cur_stats.fetchone() # Renamed
            if user_stats_db:
                stats_bundle = dict(user_stats_db) # Convert to dict
                if stats_bundle.get('last_played_at'):
                    stats_bundle['last_played_at'] = stats_bundle['last_played_at'].isoformat()
            else: # Initialize defaults if no record yet
                stats_bundle = {"total_plays": 0, "total_listened_time": 0, "favorite_song_id": None, "favorite_artist": None, "last_played_at": None, "user_id": user_id}

            # Unique songs from user_play_queue_history
            cur_stats.execute("SELECT COUNT(DISTINCT song_id) as c FROM user_play_queue_history WHERE user_id = %s", (user_id,))
            stats_bundle["unique_songs"] = (cur_stats.fetchone() or {}).get('c', 0)

            # Downloads count from user_downloads
            cur_stats.execute("SELECT COUNT(*) as c FROM user_downloads WHERE user_id = %s", (user_id,))
            stats_bundle["total_downloads"] = (cur_stats.fetchone() or {}).get('c', 0)

            # Top 5 songs (could be slow, consider if this needs optimization or pre-calculation)
            cur_stats.execute("""
                SELECT song_id, title, artist, COUNT(*) as plays FROM user_play_queue_history
                WHERE user_id = %s GROUP BY song_id, title, artist ORDER BY plays DESC LIMIT 5
            """, (user_id,))
            stats_bundle["top_songs"] = [dict(r) for r in cur_stats.fetchall()]

            # Download size (file system check, same as before but paths from PG)
            cur_stats.execute("SELECT path FROM user_downloads WHERE user_id = %s AND path IS NOT NULL", (user_id,))
            download_size_fs_stats = 0 # Renamed
            for row_path_stats in cur_stats.fetchall(): # Renamed
                try:
                    if os.path.exists(row_path_stats['path']):
                        download_size_fs_stats += os.path.getsize(row_path_stats['path'])
                except Exception: continue # Skip if path error
            stats_bundle["download_size"] = download_size_fs_stats

        stats_bundle["local_songs_count"] = len(load_local_songs_from_db()) # Uses PG-loaded cache

        return jsonify(stats_bundle)
    except psycopg2.Error as e_stats_db: # Renamed
        logger.error(f"Stats DB error for user {user_id}: {e_stats_db}")
        if conn_stats: conn_stats.rollback() # Though mostly SELECTs, good practice
        return jsonify({"error": "Database error fetching stats"}), 500
    except Exception as e_stats_gen: # Renamed
        logger.error(f"General stats error for user {user_id}: {e_stats_gen}", exc_info=True)
        return jsonify({"error": "An error occurred while fetching stats."}), 500
    finally:
        if conn_stats: conn_stats.close()


@bp.route("/api/history/clear", methods=["POST"])
@login_required
def api_clear_history():
    user_id = session['user_id']
    conn_ch = None # Renamed
    try:
        conn_ch = get_pg_connection()
        with conn_ch.cursor() as cur_ch: # Renamed
            # Clear from both history tables
            cur_ch.execute("DELETE FROM listening_history WHERE user_id = %s", (user_id,))
            del_lh_count = cur_ch.rowcount # Renamed
            cur_ch.execute("DELETE FROM user_play_queue_history WHERE user_id = %s", (user_id,))
            del_upqh_count = cur_ch.rowcount # Renamed
            # Also consider resetting relevant fields in user_statistics
            cur_ch.execute("""
                UPDATE user_statistics 
                SET total_plays = 0, total_listened_time = 0, 
                    favorite_song_id = NULL, favorite_artist = NULL, last_played_at = NULL
                WHERE user_id = %s
            """, (user_id,))
            conn_ch.commit()
        logger.info(f"Cleared history for user {user_id}: {del_lh_count} listening, {del_upqh_count} queue entries. Stats reset.")
        return jsonify({"status": "success", "message": "All history and related stats cleared."})
    except psycopg2.Error as e_ch_db: # Renamed
        logger.error(f"Clear history DB error for user {user_id}: {e_ch_db}")
        if conn_ch: conn_ch.rollback()
        return jsonify({"error": "Database error clearing history"}), 500
    except Exception as e_ch_gen: # Renamed
        logger.error(f"Clear history error for user {user_id}: {e_ch_gen}", exc_info=True)
        return jsonify({"error": "An error occurred while clearing history."}), 500
    finally:
        if conn_ch: conn_ch.close()


# --- Downloads Management (PostgreSQL Ready) ---
@bp.route("/api/downloads/clear", methods=["POST"])
@login_required
def api_clear_downloads():
    user_id = session['user_id']
    conn_cd = None # Renamed
    try:
        conn_cd = get_pg_connection()
        paths_to_delete_cd = [] # Renamed
        with conn_cd.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_cd: # Renamed
            cur_cd.execute("SELECT path FROM user_downloads WHERE user_id = %s AND path IS NOT NULL", (user_id,))
            for row_cd_path in cur_cd.fetchall(): paths_to_delete_cd.append(row_cd_path['path']) # Renamed
            cur_cd.execute("DELETE FROM user_downloads WHERE user_id = %s", (user_id,)) # Delete DB records
            conn_cd.commit()

        deleted_files_cd_count = 0 # Renamed
        for path_cd in paths_to_delete_cd: # Renamed
            try:
                if os.path.exists(path_cd):
                    os.remove(path_cd); deleted_files_cd_count += 1
            except OSError as e_file_del_cd: logger.warning(f"Failed to delete file {path_cd}: {e_file_del_cd}") # Renamed
        logger.info(f"Cleared {len(paths_to_delete_cd)} download records and {deleted_files_cd_count} files for user {user_id}.")
        return jsonify({"status": "success", "message": "Downloads cleared."})
    except psycopg2.Error as e_cd_db: # Renamed
        logger.error(f"Clear downloads DB error for user {user_id}: {e_cd_db}")
        if conn_cd: conn_cd.rollback()
        return jsonify({"error": "Database error clearing downloads"}), 500
    except Exception as e_cd_gen: # Renamed
        logger.error(f"Clear downloads error for user {user_id}: {e_cd_gen}", exc_info=True)
        return jsonify({"error": "An error occurred while clearing downloads."}), 500
    finally:
        if conn_cd: conn_cd.close()


@bp.route("/api/downloads") # This is for LISTING downloads
@login_required
def api_downloads_list(): # Renamed from original ambiguous /api/download
    user_id = session['user_id']
    conn_dl = None # Renamed
    items_dl = [] # Renamed
    try:
        conn_dl = get_pg_connection()
        with conn_dl.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_dl: # Renamed
            cur_dl.execute("""
                SELECT video_id, title, artist, album, path, downloaded_at
                FROM user_downloads WHERE user_id = %s ORDER BY downloaded_at DESC
            """, (user_id,))
            for row_dl in cur_dl.fetchall(): # Renamed
                # Check if file still exists; if not, maybe don't list or flag it
                if row_dl['path'] and os.path.exists(row_dl['path']):
                    items_dl.append({
                        "id": row_dl['video_id'], "title": row_dl['title'], "artist": row_dl['artist'],
                        "album": row_dl['album'], "downloaded_at": row_dl['downloaded_at'].isoformat(),
                        "thumbnail": get_thumbnail_for_search_results(row_dl['video_id'], False) # Generic thumbnail
                    })
                else:
                    logger.warning(f"Downloaded file for {row_dl['video_id']} (user {user_id}) not found at {row_dl['path']}. Not listing.")
        return jsonify(items_dl)
    except psycopg2.Error as e_dl_db: # Renamed
        logger.error(f"List downloads DB error for user {user_id}: {e_dl_db}")
        return jsonify({"error": "Database error listing downloads"}), 500
    except Exception as e_dl_gen: # Renamed
        logger.error(f"List downloads error for user {user_id}: {e_dl_gen}", exc_info=True)
        return jsonify({"error": "An error occurred while listing downloads."}), 500
    finally:
        if conn_dl: conn_dl.close()


@bp.route("/api/play/<song_id>") # This seems to be a redirector to the actual stream
@login_required
def redirect_play(song_id):
    user_id = session['user_id']
    client_timestamp = request.args.get('timestamp') # Optional client timestamp

    # CRITICAL: util.record_song MUST be updated for PostgreSQL 'user_play_queue_history'
    try:
        # util.record_song should now also fetch/store title, artist, thumbnail if not already there for this song_id by this user.
        util.record_song_pg(song_id, user_id, client_timestamp) # Assuming renamed util for PG
    except AttributeError as ae:
        logger.error(f"redirect_play: util.record_song_pg might be missing. {ae}", exc_info=True)
        # Decide if failure to record should prevent playback or just log
    except Exception as record_err_rp: # Renamed
        logger.error(f"Failed to record song play for {song_id} (user {user_id}): {record_err_rp}")
        # Continue to redirect even if recording fails for now

    # The STREAM_HOST logic seems specific to your deployment.
    # This typically would redirect to /api/stream-file or /api/stream-local
    # If STREAM_HOST is for a separate streaming server, that logic remains.
    # Otherwise, choose the correct internal streaming endpoint.

    return redirect(f"/stream-server/api/stream/{song_id}/{user_id}")
   


# --- Miscellaneous Routes ---
@bp.route("/api/random-song")
@login_required
def api_random_song():
    user_id = session['user_id']
    conn_rs = None # Renamed
    try:
        # Option 1: Get truly random from user's play queue history (can be slow on large history)
        # conn_rs = get_pg_connection()
        # with conn_rs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_rs:
        #     cur_rs.execute("""
        #         SELECT song_id FROM user_play_queue_history WHERE user_id = %s
        #         ORDER BY RANDOM() LIMIT 1
        #     """, (user_id,))
        #     random_song_row = cur_rs.fetchone()
        # if random_song_row:
        #     return jsonify(get_media_info(random_song_row['song_id']))

        # Option 2: Get latest from user's history (as per original logic)
        conn_rs = get_pg_connection()
        with conn_rs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_rs: # Renamed
            cur_rs.execute("""
                SELECT song_id FROM user_play_queue_history WHERE user_id = %s
                ORDER BY played_at DESC LIMIT 1
            """, (user_id,))
            latest_song_row_rs = cur_rs.fetchone() # Renamed
        if latest_song_row_rs:
            song_details_rs = get_media_info(latest_song_row_rs['song_id']) # Renamed
            if song_details_rs and song_details_rs.get("title","").lower() != "unknown title":
                return jsonify(song_details_rs)
        
        # Fallback to a random song from all local songs (if any)
        current_local_songs_rs = load_local_songs_from_db() # Renamed
        if current_local_songs_rs:
            random_local_id = random.choice(list(current_local_songs_rs.keys()))
            return jsonify(current_local_songs_rs[random_local_id])

        # Absolute fallback (hardcoded YouTube songs)
        default_yt_ids = ["dQw4w9WgXcQ", "kJQP7kiw5Fk", "9bZkp7q19f0", "L_jWHffIx5E"] # Added one more
        random_default_id = random.choice(default_yt_ids)
        return jsonify(get_media_info(random_default_id)) # get_media_info handles YT

    except psycopg2.Error as e_rs_db: # Renamed
        logger.error(f"Random song DB error: {e_rs_db}")
    except Exception as e_rs_gen: # Renamed
        logger.error(f"Error in api_random_song: {e_rs_gen}", exc_info=True)
    finally:
        if conn_rs: conn_rs.close()
    
    # Fallback if all DB attempts fail before reaching hardcoded YT fallback
    logger.warning("api_random_song: All DB/local fallbacks failed, serving hardcoded YT song.")
    final_fallback_id = "dQw4w9WgXcQ"
    return jsonify(get_media_info(final_fallback_id))


@bp.route("/api/proxy/image")
@login_required # Or remove if images should be public proxy
def proxy_image(): # Unchanged logic, no DB
    url_param = request.args.get('url') # Renamed
    if not url_param: return "No URL provided", 400
    allowed_domains = {'i.ytimg.com', 'lh3.googleusercontent.com'} # YT Music uses googleusercontent
    try:
        domain = urlparse(url_param).netloc
        if domain not in allowed_domains:
            logger.warning(f"Proxy image attempt from disallowed domain: {domain}")
            return "Invalid domain for image proxy", 403
    except Exception: return "Invalid URL format", 400

    # util.fetch_image should handle requests and return (content, content_type) or (None, None)
    content, content_type = util.fetch_image(url_param) 
    if content and content_type:
        response = make_response(content)
        response.headers['Content-Type'] = content_type
        response.headers['Access-Control-Allow-Origin'] = '*' # Or restrict to your frontend domain
        response.headers['Cache-Control'] = 'public, max-age=31536000' # Long cache
        return response
    logger.error(f"Failed to fetch image via proxy: {url_param}")
    return "Failed to fetch image", 502 # Bad Gateway


@bp.route("/get-extension") # Unchanged
def get_extension(): return render_template("extension.html")

@bp.route('/download/extension') # Unchanged
def download_extension():
    try:
        return send_file(os.path.join(os.getcwd(), "payloads", "extension", "sangeet-premium.zip"),
                         as_attachment=True, download_name='sangeet-premium.zip', mimetype='application/zip')
    except FileNotFoundError: abort(404, "Extension file not found.")
    except Exception as e: logger.error(f"Error downloading extension: {e}"); abort(500)


# --- Download Page Routes (HTML rendering, not file serving) ---
@bp.route('/sangeet-download/<video_id>') # Renders a download confirmation page
@login_required # Ensure user is logged in to see this page
def sangeet_download_page(video_id): # Renamed function
    # user_id = session['user_id'] # Already available via @login_required
    try:
        # Get metadata for display on the confirmation page
        # This doesn't trigger the download yet, just shows info.
        info = get_media_info(video_id) # Uses PG-aware get_media_info
        if not info or info.get("title", "Unknown Title").lower() == "unknown title":
            logger.warning(f"Could not get info for sangeet_download_page: {video_id}")
            # Render a simple error page or redirect
            return render_template_string("<html><body>Song info not found. Cannot prepare download page.</body></html>"), 404

        safe_title = util.sanitize_filename(info.get("title", "Track"))
        dl_name = f"{safe_title}.flac"
        return render_template('download.html', # Assuming download.html exists
                               title=info.get("title"), artist=info.get("artist"), album=info.get("album", ""),
                               thumbnail=info.get("thumbnail"), dl_name=dl_name, video_id=video_id)
    except Exception as e_sdp: # Renamed
        logger.error(f"Error preparing sangeet_download_page for {video_id}: {e_sdp}", exc_info=True)
        return render_template_string("<html><body>Error preparing download page. Please try again.</body></html>"), 500


@bp.route('/download-file/<video_id>') # Actual file download trigger from the page above
@login_required
def download_file_trigger(video_id): # Renamed function
    user_id = session['user_id']
    # CRITICAL: util.download_flac MUST be updated for PG `user_downloads` table
    # It handles the actual download/conversion and DB record, then returns the path.
    flac_path = util.download_flac(video_id, user_id)
    if not flac_path or not os.path.exists(flac_path):
        logger.error(f"download_file_trigger: util.download_flac failed or file missing for {video_id}")
        # Provide a user-friendly error, maybe redirect back with error message
        return jsonify({"error": "Failed to process or find the audio file for download."}), 500
    
    # Get title for filename from user_downloads table (should have been populated by util.download_flac)
    conn_dft = None # Renamed
    download_filename_dft = f"{video_id}.flac" # Default Renamed
    try:
        conn_dft = get_pg_connection()
        with conn_dft.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_dft: # Renamed
            cur_dft.execute("SELECT title FROM user_downloads WHERE video_id = %s AND user_id = %s", (video_id, user_id))
            row_dft = cur_dft.fetchone() # Renamed
            if row_dft and row_dft['title']:
                download_filename_dft = f"{util.sanitize_filename(row_dft['title'])}.flac"
    except psycopg2.Error as db_err_dft: # Renamed
        logger.warning(f"DB error fetching title for download {video_id} (user {user_id}): {db_err_dft}")
    finally:
        if conn_dft: conn_dft.close()
        
    return send_file(flac_path, as_attachment=True, download_name=download_filename_dft, mimetype='audio/flac')


@bp.route("/embed/<song_id>")
# @login_required # Decide if embeds require login.
def embed_player(song_id):
    try:
        size = request.args.get("size", "normal")
        theme = request.args.get("theme", "default")
        autoplay = request.args.get("autoplay", "false").lower() == "true"

        song_info_embed = get_media_info(song_id)  # PG-aware

        if not song_info_embed or song_info_embed.get("title", "Unknown Title").lower() in ["unknown title", "unknown local title"]:
            logger.error(f"Embed error: Could not find valid info for song ID {song_id}")
            return render_template_string("<html><body>Song not found or unavailable for embedding.</body></html>"), 404

        stream_url_embed = None
        if song_id.startswith("local-"):
            if song_info_embed.get("path") and os.path.exists(song_info_embed["path"]):
                # CORRECTED LINE: Use url_for to generate the correct relative path
                stream_url_embed = url_for('sangeet_all.api_stream_local', song_id=song_id)
            else:
                logger.error(f"Embed error: Local file path missing/invalid for {song_id}")
                return render_template_string("<html><body>Local file for embedding is currently unavailable.</body></html>"), 404
        else:  # YouTube song
            # CORRECTED LINE: Use url_for to generate the correct relative path
            stream_url_embed = url_for('sangeet_all.stream_file', song_id=song_id)
            
            # Check if the file actually exists for YT embeds for robustness
            # This check remains important if stream_file serves from MUSIC_DOWNLOAD_PATH
            flac_path_for_yt_check = os.path.join(MUSIC_DOWNLOAD_PATH, f"{song_id}.flac")
            if not os.path.exists(flac_path_for_yt_check):
                logger.warning(f"Embed for YT song {song_id}: FLAC file not found at {flac_path_for_yt_check}. Embed might not play audio until downloaded via other means.")
                # Depending on desired behavior, you could return an error or let the player try and fail.
                # For now, it will proceed and the player will likely fail to load the audio source.

        return render_template(
            "embed.html", song=song_info_embed, size=size, theme=theme,
            autoplay=autoplay, stream_url=stream_url_embed,
            host_url=os.getenv("SANGEET_BACKEND")
        )
    except Exception as e_embed:
        logger.error(f"General embed error for {song_id}: {e_embed}", exc_info=True)
        return render_template_string("<html><body>Error generating embed player.</body></html>"), 500



@bp.route("/play/<song_id>") # Redirect to home with song pre-selected
def play_song(song_id):
    return redirect(url_for('sangeet_ui_server.home', song=song_id))


@bp.route("/api/embed-code/<song_id>")
# @login_required # Usually embed codes can be fetched publicly
def get_embed_code(song_id): # Unchanged logic
    try:
        size = request.args.get("size", "normal")
        theme = request.args.get("theme", "default")
        autoplay = request.args.get("autoplay", "false")
        dimensions = {"small": (320, 160), "normal": (400, 200), "large": (500, 240)}
        width, height = dimensions.get(size, dimensions["normal"])
        
        # Use _external=True for url_for to get full URL
        embed_url_ec = url_for('sangeet_all.embed_player', song_id=song_id, size=size, theme=theme, autoplay=autoplay, _external=True) # Renamed
        iframe_code = f'<iframe src="{embed_url_ec}" width="{width}" height="{height}" frameborder="0" allowtransparency="true" allow="encrypted-media; autoplay" loading="lazy"></iframe>'
        return jsonify({"code": iframe_code, "url": embed_url_ec, "width": width, "height": height})
    except Exception as e_ec: # Renamed
        logger.error(f"Embed code error for {song_id}: {e_ec}")
        return jsonify({"error": "Internal server error generating embed code"}), 500

@bp.route('/stream2/open/<media_id>') # Alternative streaming, direct file or redirect
def stream2(media_id):
    if media_id.startswith("local-"):
        current_local_songs_s2 = load_local_songs_from_db() # Renamed
        details_s2 = current_local_songs_s2.get(media_id) # Renamed
        if details_s2 and details_s2.get("path") and os.path.exists(details_s2["path"]):
            _, ext_s2 = os.path.splitext(details_s2["path"]) # Renamed
            mimetype_s2 = f'audio/{ext_s2.lstrip(".").lower()}' if ext_s2 else 'application/octet-stream' # Renamed
            return send_file(details_s2["path"], mimetype=mimetype_s2)
        else:
            logger.error(f"stream2: Local song {media_id} file/path issue.")
            abort(404, description="Local song file for stream2 not found.")
    else: # YouTube ID, redirect to the standard FLAC streaming endpoint
        # This assumes the file will be available (downloaded) via stream_file.
        # It doesn't trigger download here.
        return redirect(url_for('sangeet_all.stream_file', song_id=media_id))
# --- Share Routes (PostgreSQL Ready via get_media_info) ---
@bp.route('/share/open/<media_id>')
def share(media_id):
    # load_local_songs_from_db() # Ensure local cache is fresh if many local songs shared
    song_info_share = get_media_info(media_id) # PG-aware

    if not song_info_share or song_info_share.get("title", "Unknown Title").lower() in ["unknown title", "unknown local title"]:
        # Additionally check if the file physically exists for local songs
        if media_id.startswith("local-") and (not song_info_share.get("path") or not os.path.exists(song_info_share["path"])):
            logger.warning(f"Share: Local song {media_id} file path missing or invalid.")
            return render_template('share_not_found.html'), 404
        # For YT songs, check if the FLAC is downloaded if you only want to share playable ones
        elif not media_id.startswith("local-") and not os.path.exists(os.path.join(MUSIC_DOWNLOAD_PATH, f"{media_id}.flac")):
            logger.warning(f"Share: YT song {media_id} FLAC not found. Sharing page might have no playable media yet.")
            # If info is still somewhat valid (e.g. title from API), you can still show the page
            # but the player might not work.
            # If get_media_info truly failed, then it's a 404.
            if not song_info_share or song_info_share.get("title", "Unknown Title").lower() == "unknown title":
                 return render_template('share_not_found.html'), 404
        elif not song_info_share or song_info_share.get("title", "Unknown Title").lower() == "unknown title": # General not found
            return render_template('share_not_found.html'), 404


    # Construct share_url that points back to the main player
    share_player_url = url_for('sangeet_ui_server.home', song=media_id, _external=True) # Renamed
    # Pass all of song_info_share to the template using **
    return render_template('share.html', share_url=share_player_url, **song_info_share)




@bp.route('/api/playlists', methods=['GET'])
@login_required
def get_playlists():
    user_id = session['user_id']
    conn_pl_list = None # Renamed
    try:
        conn_pl_list = get_pg_connection()
        with conn_pl_list.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_pl_list: # Renamed
            cur_pl_list.execute("""
                SELECT p.id, p.name, p.share_id, p.is_public, p.created_at, COUNT(ps.song_id) as song_count
                FROM playlists p LEFT JOIN playlist_songs ps ON p.id = ps.playlist_id
                WHERE p.user_id = %s GROUP BY p.id ORDER BY p.created_at DESC
            """, (user_id,))
            playlists_data = [{**row, 'created_at': row['created_at'].isoformat()} for row in cur_pl_list.fetchall()] # Renamed vars
        return jsonify(playlists_data)
    except psycopg2.Error as e_pl_list_db: # Renamed
        logger.error(f"Error fetching playlists for user {user_id}: {e_pl_list_db}")
        return jsonify({"error": "Database error fetching playlists"}), 500
    finally:
        if conn_pl_list: conn_pl_list.close()

@bp.route('/api/playlists/create', methods=['POST'])
@login_required
def create_playlist():
    user_id = session['user_id']
    data = request.json
    name_pl_create = data.get('name') # Renamed
    if not name_pl_create: return jsonify({'error': 'Playlist name required'}), 400
    conn_pl_create = None # Renamed
    try:
        conn_pl_create = get_pg_connection()
        with conn_pl_create.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_pl_create: # Renamed
            share_id_pl_create = secrets.token_urlsafe(16) # Renamed
            cur_pl_create.execute(
                "INSERT INTO playlists (user_id, name, share_id) VALUES (%s, %s, %s) RETURNING id, name, share_id, created_at, is_public",
                (user_id, name_pl_create, share_id_pl_create)
            )
            new_playlist_data = cur_pl_create.fetchone() # Renamed
            conn_pl_create.commit()
            # Add song_count (0 for new playlist) and format created_at
            response_data = {**new_playlist_data, 'song_count': 0, 'created_at': new_playlist_data['created_at'].isoformat()}
        return jsonify({'status': 'success', 'playlist': response_data}), 201
    except psycopg2.Error as e_pl_create_db: # Renamed
        logger.error(f"Error creating playlist '{name_pl_create}' for user {user_id}: {e_pl_create_db}")
        if conn_pl_create: conn_pl_create.rollback()
        if hasattr(e_pl_create_db, 'pgcode') and e_pl_create_db.pgcode == '23505': # Unique constraint violation
             return jsonify({'error': f"Playlist '{name_pl_create}' already exists."}), 409
        return jsonify({'error': 'Database error creating playlist'}), 500
    finally:
        if conn_pl_create: conn_pl_create.close()

@bp.route('/api/playlists/add_song', methods=['POST'])
@login_required
def add_song_to_playlist():
    user_id = session['user_id']
    data = request.json
    playlist_id_as = data.get('playlist_id') # Renamed
    song_id_as = data.get('song_id') # Renamed
    if not playlist_id_as or not song_id_as:
        return jsonify({'error': 'Playlist ID and Song ID are required'}), 400
    conn_as = None # Renamed
    try:
        conn_as = get_pg_connection()
        with conn_as.cursor() as cur_as: # Renamed
            cur_as.execute("SELECT user_id FROM playlists WHERE id = %s", (playlist_id_as,))
            owner_as = cur_as.fetchone() # Renamed
            if not owner_as or owner_as[0] != user_id:
                return jsonify({'error': 'Unauthorized or playlist not found'}), 403
            # ON CONFLICT ensures no duplicates if song already in playlist
            cur_as.execute("INSERT INTO playlist_songs (playlist_id, song_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                           (playlist_id_as, song_id_as))
            conn_as.commit()
        return jsonify({'status': 'success', 'message': f"Song {song_id_as} processed for playlist {playlist_id_as}."})
    except psycopg2.Error as e_as_db: # Renamed
        logger.error(f"Error adding song {song_id_as} to playlist {playlist_id_as}: {e_as_db}")
        if conn_as: conn_as.rollback()
        return jsonify({'error': 'Database error adding song to playlist'}), 500
    finally:
        if conn_as: conn_as.close()


@bp.route('/api/playlists/<int:playlist_id>/songs', methods=['GET'])
@login_required
def get_playlist_songs(playlist_id):
    user_id = session['user_id']
    conn_pls = None # Renamed
    songs_list_pls = [] # Renamed
    try:
        conn_pls = get_pg_connection()
        with conn_pls.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_pls: # Renamed
            cur_pls.execute("SELECT user_id, is_public FROM playlists WHERE id = %s", (playlist_id,))
            playlist_info_pls = cur_pls.fetchone() # Renamed
            if not playlist_info_pls or (playlist_info_pls['user_id'] != user_id and not playlist_info_pls['is_public']):
                return jsonify({'error': 'Unauthorized or playlist not found'}), 403
            cur_pls.execute("SELECT song_id FROM playlist_songs WHERE playlist_id = %s ORDER BY added_at ASC", (playlist_id,))
            song_ids_pls = [row['song_id'] for row in cur_pls.fetchall()] # Renamed

        # load_local_songs_from_db() # Ensure local cache is up-to-date for get_media_info

        for song_id_item_pls in song_ids_pls: # Renamed
            if not song_id_item_pls: continue
            media_info_item_pls = get_media_info(song_id_item_pls) # Renamed, uses PG-aware get_media_info
            if media_info_item_pls and media_info_item_pls.get("title", "Unknown Title").lower() not in ["unknown title", "unknown local title"]:
                songs_list_pls.append({
                    'id': song_id_item_pls, 'title': media_info_item_pls.get('title'), 'artist': media_info_item_pls.get('artist'),
                    'thumbnail': media_info_item_pls.get('thumbnail') or get_thumbnail_for_search_results(song_id_item_pls, song_id_item_pls.startswith("local-")),
                    'duration': media_info_item_pls.get('duration', 0)
                })
            else:
                logger.warning(f"Could not retrieve valid info for playlist song ID: {song_id_item_pls} in playlist {playlist_id}.")
                songs_list_pls.append({
                    'id': song_id_item_pls, 'title': f"Song Unavailable ({song_id_item_pls[:11]}...)", 'artist': 'Unknown Artist',
                    'thumbnail': get_thumbnail_for_search_results(song_id_item_pls, False), 'unavailable': True
                })
        return jsonify(songs_list_pls)
    except psycopg2.Error as e_pls_db: # Renamed
        logger.error(f"DB error fetching songs for playlist {playlist_id}: {e_pls_db}")
        return jsonify({"error": "Database error fetching playlist songs"}), 500
    except Exception as e_pls_gen: # Renamed
        logger.error(f"Error fetching songs for playlist {playlist_id}: {e_pls_gen}", exc_info=True)
        return jsonify({"error": "Internal server error fetching playlist songs"}), 500
    finally:
        if conn_pls: conn_pls.close()


@bp.route('/api/playlists/<int:playlist_id>/share', methods=['POST'])
@login_required
def share_playlist(playlist_id):
    user_id = session['user_id']
    conn_pl_share = None # Renamed
    try:
        conn_pl_share = get_pg_connection()
        with conn_pl_share.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_pl_share: # Renamed
            cur_pl_share.execute("SELECT share_id FROM playlists WHERE id = %s AND user_id = %s", (playlist_id, user_id))
            playlist_data_pl_share = cur_pl_share.fetchone() # Renamed
            if not playlist_data_pl_share:
                return jsonify({'error': 'Unauthorized or playlist not found'}), 403
            
            share_id_val_pl_share = playlist_data_pl_share['share_id'] # Renamed
            # Ensure share_id exists; schema has UNIQUE constraint, so it should be there if row exists.
            # If it could be NULL, generate one:
            # if not share_id_val_pl_share:
            #    share_id_val_pl_share = secrets.token_urlsafe(16)
            #    cur_pl_share.execute("UPDATE playlists SET is_public = TRUE, share_id = %s WHERE id = %s", (share_id_val_pl_share, playlist_id))
            # else:
            cur_pl_share.execute("UPDATE playlists SET is_public = TRUE WHERE id = %s", (playlist_id,))
            conn_pl_share.commit()
        return jsonify({'share_id': share_id_val_pl_share, 'message': 'Playlist is now public.'})
    except psycopg2.Error as e_pl_share_db: # Renamed
        logger.error(f"Error sharing playlist {playlist_id} for user {user_id}: {e_pl_share_db}")
        if conn_pl_share: conn_pl_share.rollback()
        return jsonify({'error': 'Database error sharing playlist'}), 500
    finally:
        if conn_pl_share: conn_pl_share.close()

@bp.route('/playlists/share/<share_id>', methods=['GET']) # Import via GET link
@login_required # User must be logged in to import
def import_shared_playlist(share_id):
    importer_user_id = session['user_id'] # Renamed
    conn_pl_import = None # Renamed
    try:
        conn_pl_import = get_pg_connection()
        with conn_pl_import.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_pl_import: # Renamed
            cur_pl_import.execute("SELECT id, name, user_id as owner_user_id FROM playlists WHERE share_id = %s AND is_public = TRUE", (share_id,))
            original_playlist_data = cur_pl_import.fetchone() # Renamed
            if not original_playlist_data:
                # Better to redirect with a message than jsonify error for a GET route user might click
                return redirect(url_for('sangeet_ui_server.home', error_msg='Playlist not found or not public.'))

            original_playlist_id_import = original_playlist_data['id'] # Renamed
            original_playlist_name_import = original_playlist_data['name'] # Renamed
            original_owner_id_import = original_playlist_data['owner_user_id'] # Renamed

            if original_owner_id_import == importer_user_id: # Prevent importing own playlist
                 return redirect(url_for('sangeet_ui_server.home', info_msg='This is already your playlist.'))

            cur_pl_import.execute("SELECT song_id FROM playlist_songs WHERE playlist_id = %s", (original_playlist_id_import,))
            song_ids_to_import_list = [row['song_id'] for row in cur_pl_import.fetchall()] # Renamed

            # Create a new playlist for the importer
            new_imported_playlist_name = f"{original_playlist_name_import} (Imported)" # Renamed
            new_imported_share_id = secrets.token_urlsafe(16) # New playlist gets its own share_id
            cur_pl_import.execute(
                "INSERT INTO playlists (user_id, name, share_id, is_public) VALUES (%s, %s, %s, FALSE) RETURNING id",
                (importer_user_id, new_imported_playlist_name, new_imported_share_id)
            )
            new_imported_playlist_id = cur_pl_import.fetchone()['id'] # Renamed

            if song_ids_to_import_list: # Batch insert songs if any
                # Create list of tuples for executemany
                songs_to_insert_tuples = [(new_imported_playlist_id, s_id) for s_id in song_ids_to_import_list] # Renamed
                # ON CONFLICT handles if somehow a song was already there (shouldn't be for new playlist)
                psycopg2.extras.execute_values(
                    cur_pl_import,
                    "INSERT INTO playlist_songs (playlist_id, song_id) VALUES %s ON CONFLICT DO NOTHING",
                    songs_to_insert_tuples
                )
            conn_pl_import.commit()
        logger.info(f"User {importer_user_id} imported playlist {original_playlist_id_import} (shared by {original_owner_id_import}) as new playlist {new_imported_playlist_id}.")
        return redirect(url_for('sangeet_ui_server.home', success_msg=f"Playlist '{new_imported_playlist_name}' imported!"))
    except psycopg2.Error as e_pl_import_db: # Renamed
        logger.error(f"DB error importing shared playlist {share_id} for user {importer_user_id}: {e_pl_import_db}")
        if conn_pl_import: conn_pl_import.rollback()
        return redirect(url_for('sangeet_ui_server.home', error_msg='Failed to import playlist due to database error.'))
    except Exception as e_pl_import_gen: # Renamed
        logger.error(f"Error importing shared playlist {share_id}: {e_pl_import_gen}", exc_info=True)
        if conn_pl_import: conn_pl_import.rollback() # Also rollback on general exception
        return redirect(url_for('sangeet_ui_server.home', error_msg='An unexpected error occurred during playlist import.'))
    finally:
        if conn_pl_import: conn_pl_import.close()





@bp.route('/view/issues', methods=['GET', 'POST'])
def view_admin_issues():
    # This route primarily handles admin password authentication for the issues dashboard.
    # Its core logic (checking ADMIN_PASSWORD against form input, setting session) is not DB-dependent.
    # The HTML template it renders ('admin_issues.html') will make API calls to fetch data.
    error_vai = None # Renamed
    session_expired_vai = request.args.get('session_expired') # Renamed

    if request.method == 'POST':
        password_vai = request.form.get('password') # Renamed
        if password_vai and ADMIN_PASSWORD and password_vai == ADMIN_PASSWORD:
            session['admin_authed'] = True
            session.permanent = True # Optional: make admin session more persistent
            return redirect(url_for('sangeet_all.view_admin_issues'))
        else:
            error_vai = "Invalid Admin Password"
            session.pop('admin_authed', None) # Clear on failure
    elif request.method == 'GET' and session_expired_vai:
        error_vai = "Admin session expired or logged out. Please re-enter password."
        session.pop('admin_authed', None)

    if session.get('admin_authed'):
        return render_template('admin_issues.html') # This template will use the admin APIs below

    # Password prompt HTML (same as original, ensure it's robust)
    password_prompt_html_vai = """
    <!DOCTYPE html><html><head><title>Admin Issues Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #121212; color: #e0e0e0; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 15px; box-sizing: border-box; }
        .login-container { background-color: #1e1e1e; padding: 25px; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); text-align: center; width: 100%; max-width: 380px; }
        h2 { margin-top: 0; color: #ffffff; font-weight: 500; }
        label { display: block; margin-bottom: 8px; font-weight: 500; color: #b3b3b3; text-align: left; }
        input[type='password'] { width: calc(100% - 22px); padding: 10px; margin-bottom: 20px; border: 1px solid #333; border-radius: 4px; background-color: #282828; color: #e0e0e0; font-size: 1rem; }
        input[type='submit'] { background-color: #1db954; color: white; padding: 10px 0; border: none; border-radius: 25px; cursor: pointer; font-size: 1rem; font-weight: bold; width: 100%; transition: background-color 0.2s; }
        input[type='submit']:hover { background-color: #1ed760; }
        .error { color: #f44336; margin-top: 15px; font-weight: bold; }
    </style>
    </head><body>
    <div class="login-container">
        <h2>Admin Issue Dashboard</h2>
        <form method="post">
            <label for="password">Enter Admin Password:</label>
            <input type="password" id="password" name="password" required autofocus>
            <input type="submit" value="Login">
            {% if error %}
                <p class="error">{{ error }}</p>
            {% endif %}
        </form>
    </div>
    </body></html>
    """ # Keep your well-styled HTML here.
    return render_template_string(password_prompt_html_vai, error=error_vai)


@bp.route('/api/report-issue', methods=['POST'])
@login_required
def report_issue():
    user_id = session['user_id'] # Guaranteed by @login_required
    data = request.get_json()
    if not data: return jsonify({"error": "No data provided"}), 400
    topic_ri = data.get('topic') # Renamed
    details_ri = data.get('details') # Renamed
    if not topic_ri or not details_ri: return jsonify({"error": "Topic and details are required"}), 400

    conn_ri = None # Renamed
    try:
        conn_ri = get_pg_connection()
        with conn_ri.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_ri: # Renamed
            cur_ri.execute("""
                INSERT INTO user_issues (user_id, topic, details, status)
                VALUES (%s, %s, %s, 'Open') RETURNING id, created_at
            """, (user_id, topic_ri, details_ri))
            new_issue_ri = cur_ri.fetchone() # Renamed
            conn_ri.commit()
        return jsonify({
            "success": True, "message": "Issue reported successfully.",
            "issue": {"id": new_issue_ri['id'], "topic": topic_ri, "status": "Open", "created_at": new_issue_ri['created_at'].isoformat()}
        })
    except psycopg2.Error as e_ri_db: # Renamed
        logger.error(f"DB error reporting issue for user {user_id}: {e_ri_db}")
        if conn_ri: conn_ri.rollback()
        return jsonify({"error": "Database error reporting issue"}), 500
    except Exception as e_ri_gen: # Renamed
        logger.error(f"Error reporting issue for user {user_id}: {e_ri_gen}", exc_info=True)
        if conn_ri: conn_ri.rollback() # Also rollback on general exception
        return jsonify({"error": "An unexpected error occurred while reporting issue."}), 500
    finally:
        if conn_ri: conn_ri.close()


@bp.route('/api/user-issues', methods=['GET'])
@login_required
def get_user_issues():
    user_id = session['user_id']
    conn_ui = None # Renamed
    issues_map_ui = {} # Use map to aggregate comments correctly # Renamed
    try:
        conn_ui = get_pg_connection()
        with conn_ui.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_ui: # Renamed
            # Fetch issues and their comments, joining with users for usernames
            cur_ui.execute("""
                SELECT
                    i.id, i.topic, i.details, i.status, i.created_at, i.updated_at,
                    u_issue.username AS reporter_username,
                    c.id AS comment_id, c.comment, c.is_admin, c.created_at AS comment_created_at,
                    u_comment.username AS commenter_username
                FROM user_issues i
                JOIN users u_issue ON i.user_id = u_issue.id
                LEFT JOIN issue_comments c ON i.id = c.issue_id
                LEFT JOIN users u_comment ON c.user_id = u_comment.id AND c.is_admin = FALSE
                WHERE i.user_id = %s
                ORDER BY i.updated_at DESC, c.created_at ASC
            """, (user_id,))
            for row_ui in cur_ui.fetchall(): # Renamed
                issue_id_ui = row_ui['id'] # Renamed
                if issue_id_ui not in issues_map_ui:
                    issues_map_ui[issue_id_ui] = {
                        'id': issue_id_ui, 'topic': row_ui['topic'], 'details': row_ui['details'], 'status': row_ui['status'],
                        'created_at': row_ui['created_at'].isoformat(), 'updated_at': row_ui['updated_at'].isoformat(),
                        'reporter_username': row_ui['reporter_username'], 'comments': []
                    }
                if row_ui['comment_id'] is not None:
                    comment_user = "Admin" if row_ui['is_admin'] else (row_ui['commenter_username'] or "User")
                    issues_map_ui[issue_id_ui]['comments'].append({
                        'id': row_ui['comment_id'], 'content': row_ui['comment'], 'is_admin': bool(row_ui['is_admin']),
                        'username': comment_user, 'created_at': row_ui['comment_created_at'].isoformat()
                    })
        return jsonify(list(issues_map_ui.values())) # Convert map values to list
    except psycopg2.Error as e_ui_db: # Renamed
        logger.error(f"DB error fetching user issues for {user_id}: {e_ui_db}")
        return jsonify({"error": "Database error fetching your issues."}), 500
    except Exception as e_ui_gen: # Renamed
        logger.error(f"Error fetching user issues for {user_id}: {e_ui_gen}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred fetching your issues."}), 500 # Robust: return empty list or error
    finally:
        if conn_ui: conn_ui.close()


@bp.route('/api/issues/<int:issue_id>/reply', methods=['POST'])
@login_required
def reply_to_issue(issue_id):
    user_id = session['user_id']
    data = request.get_json()
    comment_text_rti = data.get('comment') # Renamed
    if not comment_text_rti: return jsonify({"error": "Comment text cannot be empty"}), 400

    conn_rti = None # Renamed
    try:
        conn_rti = get_pg_connection()
        with conn_rti.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_rti: # Renamed
            cur_rti.execute("SELECT user_id FROM user_issues WHERE id = %s", (issue_id,))
            issue_owner_rti = cur_rti.fetchone() # Renamed
            if not issue_owner_rti: return jsonify({"error": "Issue not found"}), 404
            if issue_owner_rti['user_id'] != user_id: return jsonify({"error": "Unauthorized to comment"}), 403

            # Insert user's comment (is_admin = FALSE)
            cur_rti.execute("""
                INSERT INTO issue_comments (issue_id, user_id, is_admin, comment)
                VALUES (%s, %s, FALSE, %s) RETURNING id, created_at
            """, (issue_id, user_id, comment_text_rti))
            new_comment_data_rti = cur_rti.fetchone() # Renamed

            # Update issue's updated_at timestamp and set status to 'In Progress' if 'Open'
            cur_rti.execute("""
                UPDATE user_issues SET updated_at = CURRENT_TIMESTAMP,
                                      status = CASE WHEN status = 'Open' THEN 'In Progress' ELSE status END
                WHERE id = %s
            """, (issue_id,))
            conn_rti.commit()

            # Fetch username for response
            cur_rti.execute("SELECT username FROM users WHERE id = %s", (user_id,)) # New query in same transaction is fine
            username_rti = (cur_rti.fetchone() or {}).get('username', 'User') # Renamed

        return jsonify({
            "success": True, "message": "Reply submitted.",
            "comment": {'id': new_comment_data_rti['id'], 'content': comment_text_rti, 'is_admin': False,
                        'username': username_rti, 'created_at': new_comment_data_rti['created_at'].isoformat()}
        })
    except psycopg2.Error as e_rti_db: # Renamed
        logger.error(f"DB error user reply to issue {issue_id}: {e_rti_db}")
        if conn_rti: conn_rti.rollback()
        return jsonify({"error": "Database error processing reply"}), 500
    except Exception as e_rti_gen: # Renamed
        logger.error(f"Error user reply to issue {issue_id}: {e_rti_gen}", exc_info=True)
        if conn_rti: conn_rti.rollback()
        return jsonify({"error": "Unexpected error processing reply"}), 500
    finally:
        if conn_rti: conn_rti.close()


# --- Admin API Endpoints for Issues (PostgreSQL Ready) ---
@bp.route('/api/admin/issues', methods=['GET'])
@admin_required # Your existing admin auth decorator
def api_get_admin_issues():
    conn_ai = None # Renamed
    try:
        conn_ai = get_pg_connection()
        with conn_ai.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_ai: # Renamed
            cur_ai.execute("""
                SELECT i.id, i.user_id, u.username as reporter_username, i.topic, i.details, i.status,
                       i.created_at, i.updated_at
                FROM user_issues i JOIN users u ON i.user_id = u.id
                ORDER BY CASE i.status 
                             WHEN 'Open' THEN 1 
                             WHEN 'In Progress' THEN 2 
                             ELSE 3 
                         END, i.updated_at DESC
            """)
            issues_admin_list = [ # Renamed
                {**row, 'created_at': row['created_at'].isoformat(), 'updated_at': row['updated_at'].isoformat()}
                for row in cur_ai.fetchall()
            ]
        return jsonify(issues_admin_list)
    except psycopg2.Error as e_ai_db: # Renamed
        logger.error(f"Admin DB error fetching issues: {e_ai_db}")
        return jsonify({"error": "Database error fetching admin issues"}), 500
    except Exception as e_ai_gen: # Renamed
        logger.error(f"Admin error fetching issues: {e_ai_gen}", exc_info=True)
        return jsonify({"error": "Unexpected error fetching admin issues"}), 500
    finally:
        if conn_ai: conn_ai.close()

@bp.route('/api/admin/issues/<int:issue_id>', methods=['GET'])
@admin_required
def api_get_admin_issue_details(issue_id):
    conn_aid = None # Renamed
    try:
        conn_aid = get_pg_connection()
        issue_details_aid = None # Renamed
        comments_list_aid = [] # Renamed
        with conn_aid.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_aid: # Renamed
            cur_aid.execute("SELECT i.*, u.username as reporter_username FROM user_issues i JOIN users u ON i.user_id = u.id WHERE i.id = %s", (issue_id,))
            raw_issue_aid = cur_aid.fetchone() # Renamed
            if not raw_issue_aid: abort(404, description="Issue not found by admin.")
            issue_details_aid = {**raw_issue_aid, 'created_at': raw_issue_aid['created_at'].isoformat(), 'updated_at': raw_issue_aid['updated_at'].isoformat()}

            cur_aid.execute("""
                SELECT c.id, c.user_id, u.username as commenter_username, c.is_admin, c.comment, c.created_at
                FROM issue_comments c LEFT JOIN users u ON c.user_id = u.id AND c.is_admin = FALSE
                WHERE c.issue_id = %s ORDER BY c.created_at ASC
            """, (issue_id,))
            for comment_row_aid in cur_aid.fetchall(): # Renamed
                comment_dict_aid = dict(comment_row_aid) # Renamed
                comment_dict_aid['created_at'] = comment_dict_aid['created_at'].isoformat()
                if comment_dict_aid['is_admin']: comment_dict_aid['commenter_username'] = "Admin"
                elif not comment_dict_aid['commenter_username']: comment_dict_aid['commenter_username'] = "User" # Fallback
                comments_list_aid.append(comment_dict_aid)
            issue_details_aid['comments'] = comments_list_aid
        return jsonify(issue_details_aid)
    except psycopg2.Error as e_aid_db: # Renamed
        logger.error(f"Admin DB error fetching issue details {issue_id}: {e_aid_db}")
        return jsonify({"error": "Database error fetching issue details"}), 500
    except Exception as e_aid_gen: # Renamed
        logger.error(f"Admin error fetching issue details {issue_id}: {e_aid_gen}", exc_info=True)
        return jsonify({"error": "Unexpected error fetching issue details"}), 500
    finally:
        if conn_aid: conn_aid.close()

@bp.route('/api/admin/issues/<int:issue_id>/comment', methods=['POST'])
@admin_required
def api_add_admin_comment(issue_id):
    data = request.get_json()
    comment_text_aic = data.get('comment') # Renamed
    if not comment_text_aic: return jsonify({"error": "Admin comment text required"}), 400

    conn_aic = None # Renamed
    try:
        conn_aic = get_pg_connection()
        with conn_aic.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_aic: # Renamed
            cur_aic.execute("SELECT id FROM user_issues WHERE id = %s", (issue_id,))
            if not cur_aic.fetchone(): abort(404, description="Issue not found for admin comment.")

            # Admin user_id is NULL, is_admin = TRUE
            cur_aic.execute("""
                INSERT INTO issue_comments (issue_id, user_id, is_admin, comment)
                VALUES (%s, NULL, TRUE, %s) RETURNING id, created_at
            """, (issue_id, comment_text_aic))
            new_admin_comment_aic = cur_aic.fetchone() # Renamed
            
            # Update issue status to 'In Progress' if it was 'Open', and always update 'updated_at'
            cur_aic.execute("""
                UPDATE user_issues SET 
                    status = CASE WHEN status = 'Open' THEN 'In Progress' ELSE status END,
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = %s
            """, (issue_id,))
            conn_aic.commit()
        return jsonify({
            "success": True, "message": "Admin comment added.",
            "comment": {'id': new_admin_comment_aic['id'], 'content': comment_text_aic, 'is_admin': True,
                        'username': 'Admin', 'created_at': new_admin_comment_aic['created_at'].isoformat()}
        })
    except psycopg2.Error as e_aic_db: # Renamed
        logger.error(f"Admin DB error adding comment to issue {issue_id}: {e_aic_db}")
        if conn_aic: conn_aic.rollback()
        return jsonify({"error": "Database error adding admin comment"}), 500
    except Exception as e_aic_gen: # Renamed
        logger.error(f"Admin error adding comment to issue {issue_id}: {e_aic_gen}", exc_info=True)
        if conn_aic: conn_aic.rollback()
        return jsonify({"error": "Unexpected error adding admin comment"}), 500
    finally:
        if conn_aic: conn_aic.close()

@bp.route('/api/admin/issues/<int:issue_id>/update_status', methods=['POST'])
@admin_required
def api_update_issue_status(issue_id):
    data = request.get_json()
    new_status_ais = data.get('status') # Renamed
    allowed_statuses_ais = ["Open", "In Progress", "Resolved", "Closed"] # From schema # Renamed
    if not new_status_ais or new_status_ais not in allowed_statuses_ais:
        return jsonify({"error": f"Invalid status. Allowed: {', '.join(allowed_statuses_ais)}"}), 400

    conn_ais = None # Renamed
    try:
        conn_ais = get_pg_connection()
        with conn_ais.cursor() as cur_ais: # Renamed
            cur_ais.execute("UPDATE user_issues SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                            (new_status_ais, issue_id))
            if cur_ais.rowcount == 0: abort(404, description="Issue not found or status already set.")
            conn_ais.commit()
        return jsonify({"success": True, "message": f"Issue {issue_id} status updated to {new_status_ais}."})
    except psycopg2.Error as e_ais_db: # Renamed
        logger.error(f"Admin DB error updating status for issue {issue_id}: {e_ais_db}")
        if conn_ais: conn_ais.rollback()
        return jsonify({"error": "Database error updating issue status"}), 500
    except Exception as e_ais_gen: # Renamed
        logger.error(f"Admin error updating status for issue {issue_id}: {e_ais_gen}", exc_info=True)
        if conn_ais: conn_ais.rollback()
        return jsonify({"error": "Unexpected error updating issue status"}), 500
    finally:
        if conn_ais: conn_ais.close()

@bp.route('/api/admin/stats', methods=['GET'])
@admin_required
def api_get_admin_stats():
    conn_as = None # Renamed
    try:
        conn_as = get_pg_connection()
        admin_stats_data = {"total": 0, "open": 0, "in_progress": 0, "resolved": 0, "closed": 0} # Renamed
        with conn_as.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_as: # Renamed
            cur_as.execute("SELECT status, COUNT(*) as count FROM user_issues GROUP BY status")
            for row_as in cur_as.fetchall(): # Renamed
                status_key_as = row_as['status'].lower().replace(' ', '_') # Renamed
                if status_key_as in admin_stats_data: admin_stats_data[status_key_as] = row_as['count']
                admin_stats_data['total'] += row_as['count']
        return jsonify(admin_stats_data)
    except psycopg2.Error as e_as_db: # Renamed
        logger.error(f"Admin DB error fetching stats: {e_as_db}")
        return jsonify({"error": "Database error fetching admin stats"}), 500
    except Exception as e_as_gen: # Renamed
        logger.error(f"Admin error fetching stats: {e_as_gen}", exc_info=True)
        return jsonify({"error": "Unexpected error fetching admin stats"}), 500
    finally:
        if conn_as: conn_as.close()

# --- Final Error Handlers (Unchanged) ---
@bp.route("/api/connection-test", methods=['GET']) # Unchanged
def api_connection_test(): return jsonify({"success": True, "message": "Backend connection OK."})

@bp.errorhandler(404) # Unchanged
def not_found_error(error): # Renamed param
    # Differentiate API 404s from page 404s if needed
    if request.path.startswith('/api/'):
        return jsonify(error=str(error.description if hasattr(error, 'description') else "Resource not found")), 404
    # return render_template('404.html'), 404 # Or a generic 404 HTML page
    return "Error 404: Not Found - " + str(error.description if hasattr(error, 'description') else "The requested URL was not found on the server."), 404


@bp.errorhandler(500) # Unchanged
def internal_error_handler(error): # Renamed param
    # Log the actual error for server-side debugging
    logger.error(f"Internal Server Error on {request.path}: {error}", exc_info=True) # Add exc_info
    if request.path.startswith('/api/'):
        return jsonify(error="Internal server error. Please try again later."), 500
    # return render_template('500.html'), 500 # Or a generic 500 HTML page
    return "Error 500: Internal Server Error - An unexpected error occurred. Please try again later.", 500




# --- Streaming Routes (PostgreSQL Ready for local part) ---
@bp.route("/api/stream-file/<song_id>") # For downloaded YouTube FLACs
# @login_required # Consider if auth is needed for direct file streaming if URL is known
def stream_file(song_id):
    # This assumes util.download_flac (called elsewhere) stored files in MUSIC_DOWNLOAD_PATH
    flac_path = os.path.join(MUSIC_DOWNLOAD_PATH, f"{song_id}.flac")
    if not os.path.exists(flac_path):
        logger.error(f"Stream_file: FLAC not found for {song_id} at {flac_path}")
        return jsonify({"error": "File not found or not processed"}), 404
    try:
        # send_file handles Range requests for seeking
        return send_file(flac_path, mimetype='audio/flac')
    except Exception as e_sf: # Renamed
        logger.error(f"stream_file error for {song_id}: {e_sf}", exc_info=True)
        return jsonify({"error": "Server error during streaming file."}), 500

@bp.route("/api/stream-local/<song_id>") # For user's original local files
# @login_required # Consider if auth needed
def api_stream_local(song_id):
    if not song_id.startswith("local-"):
        return jsonify({"error": "Invalid request for local stream; ID must start with 'local-'"}), 400
    
    current_local_songs_sl = load_local_songs_from_db() # Ensure cache is fresh for this request Renamed
    meta_sl = current_local_songs_sl.get(song_id) # Renamed

    if not meta_sl or not meta_sl.get("path") or not os.path.isfile(meta_sl["path"]):
        logger.error(f"api_stream_local: Local song {song_id} metadata or file path invalid/missing.")
        return jsonify({"error": "Local song file not found or metadata error"}), 404
    
    try:
        _, ext_sl = os.path.splitext(meta_sl["path"]) # Renamed
        mimetype_sl = f'audio/{ext_sl.lstrip(".").lower()}' if ext_sl else 'application/octet-stream' # Renamed
        return send_file(meta_sl["path"], mimetype=mimetype_sl)
    except Exception as e_sl_gen: # Renamed
        logger.error(f"Error streaming local file {song_id} ({meta_sl.get('path')}): {e_sl_gen}", exc_info=True)
        return jsonify({"error": "Server error streaming local file."}), 500
