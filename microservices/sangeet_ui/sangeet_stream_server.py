from flask import Flask,  send_file , redirect , Blueprint , session , request , jsonify , url_for



from utils import util
from functools import wraps
from flask_cors import CORS # Import the extension
import redis
from database.database import get_pg_connection
import logging
from dotenv import load_dotenv
import psycopg2
import os

stream_app = Flask("stream app")
CORS(stream_app)
redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
load_dotenv(dotenv_path=os.path.join(os.getcwd() , "config" , ".env"))
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) 







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



@stream_app.route("/stream-server/api/stream/<song_id>/<user_id>")
def api_stream(song_id , user_id):
    """
    Returns the appropriate streaming URL for a given song_id (local or YouTube).
    Records the song play event.
    """
    # user_id = session['user_id']
    # client_timestamp = request.args.get('timestamp') # Get timestamp from client if provided

    # # Record the play event (can happen before checking type)
    # # Consider fetching title/artist here if needed for recording, but might slow down response
    # # For now, assuming record_song handles fetching details if necessary or uses ID only initially
    # try:
    #     util.record_song(song_id, user_id, client_timestamp)
    # except Exception as record_err:
    #     logger.error(f"Failed to record song play for {song_id}: {record_err}")
    #     # Continue to provide stream URL even if recording fails

    # --- Handle Local Song ---
    if song_id.startswith("local-"):
        local_songs_cache = load_local_songs_from_postgres()
        if song_id in local_songs_cache:
            # Check if path exists just in case cache is stale
            if os.path.exists(local_songs_cache[song_id].get("path", "")):
                logger.info(f"Providing local stream URL for {song_id}")
                # Point to the dedicated local streaming endpoint
                return jsonify({
                    "local": True,
                    "url": f"/api/stream-local/{song_id}"
                })
            else:
                logger.error(f"Local song file path missing for ID {song_id}, cannot generate stream URL.")
                return jsonify({"error": "Local file not found on server"}), 404
        else:
            logger.error(f"Local song ID {song_id} not found in cache, cannot generate stream URL.")
            return jsonify({"error": "Local song metadata not found"}), 404

    # --- Handle YouTube Song ---
    else:
        # Use the utility function to ensure the FLAC file exists or is downloaded
        flac_path = util.download_flac_pg(song_id, user_id)
        if not flac_path:
            logger.error(f"Failed to process YouTube song {song_id} for streaming.")
            return jsonify({"error": "Failed to prepare YouTube song for streaming"}), 500

        logger.info(f"Providing YouTube stream URL for {song_id}")
        # Point to the endpoint that serves downloaded FLAC files
        return jsonify({
            "local": False, # Indicate it's not from the user's original local library
            "url": f"/api/stream-file/{song_id}"
        })




if __name__ == "__main__":
    stream_app.run(host="localhost", port=2300)