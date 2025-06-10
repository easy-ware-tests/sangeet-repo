from flask import (Flask, send_file, session, redirect, request, jsonify, url_for)
from database.database import get_pg_connection
import psycopg2
from utils import util
from functools import wraps
import logging
import os
from flask_cors import CORS
import subprocess

# Import the Celery app and the task
from celery_app import celery
from tasks import download_youtube_song_task

bp = Flask("sangeet_download_server")
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
CORS(bp)



# @bp.route("/download-server/api/download/<song_id>/<user_id_session>")
# def api_download(song_id, user_id_session):
#     conn = None
#     try:
#         conn = get_pg_connection()
#         with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
#             # 1. Check local songs
#             cur.execute("SELECT * FROM local_songs WHERE id = %s", (song_id,))
#             local_song = cur.fetchone()
#             if local_song and local_song.get('path') and os.path.exists(local_song['path']):
#                 logger.info(f"Serving local song (from local_songs table): {song_id}")
#                 return send_local_file(local_song)

#             # 2. Check previously downloaded YT songs
#             cur.execute("SELECT * FROM user_downloads WHERE video_id = %s AND user_id = %s", (song_id, user_id_session))
#             downloaded_song = cur.fetchone()
#             if downloaded_song and downloaded_song.get('path') and os.path.exists(downloaded_song['path']):
#                 logger.info(f"Serving cached YouTube download: {song_id}")
#                 return send_local_file(downloaded_song, is_yt=True)

#             # 3. Start Celery download task
#             if not song_id.startswith("local-"):
#                 logger.info(f"Initiating background download for YouTube song: {song_id}")
#                 task = download_youtube_song_task.delay(song_id, user_id_session)
#                 return jsonify({
#                     'status': 'processing',
#                     'task_id': task.id,
#                     'check_status_url': url_for('task_status', task_id=task.id, _external=True)
#                 }), 202

#             else:
#                 logger.error(f"Local song {song_id} requested but not found or path invalid.")
#                 return jsonify({"error": "Local file not found"}), 404

#     except Exception as e:
#         logger.error(f"Download route error for {song_id}: {e}", exc_info=True)
#         return jsonify({"error": "An error occurred during download preparation."}), 500
#     finally:
#         if conn and not conn.closed:
#             conn.close()





@bp.route("/download-server/api/download/<song_id>/<user_id_session>")
def api_download(song_id, user_id_session):
    """
    Handles all song requests with a "cache-first" optimization.
    It checks for existing files before starting any new download process.
    """
    conn = None
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            # OPTIMIZATION 1: Check if it's a pre-existing local song.
            # This is the fastest path for files in the main library.
            cur.execute("SELECT * FROM local_songs WHERE id = %s", (song_id,))
            local_song = cur.fetchone()
            if local_song and local_song.get('path') and os.path.exists(local_song['path']):
                logger.info(f"LIGHTNING FAST: Serving pre-existing local file for song_id: {song_id}")
                return send_local_file(local_song)

            # OPTIMIZATION 2: Check if the song has been previously downloaded for this user.
            # This is the "lightning fast" cache check for YouTube downloads.
            cur.execute("SELECT * FROM user_downloads WHERE video_id = %s AND user_id = %s", (song_id, user_id_session))
            downloaded_song = cur.fetchone()
            if downloaded_song and downloaded_song.get('path') and os.path.exists(downloaded_song['path']):
                logger.info(f"LIGHTNING FAST: Serving cached YouTube download for song_id: {song_id}")
                return send_local_file(downloaded_song, is_yt=True)

            # If the song is not found in either cache and is a YouTube ID,
            # then we proceed to the asynchronous Celery download.
            if not song_id.startswith("local-"):
                logger.info(f"Cache miss for YT song: {song_id}. Initiating background download.")
                task = download_youtube_song_task.delay(song_id, user_id_session)
                # Respond immediately that the task is processing.
                return jsonify({
                    'status': 'processing',
                    'task_id': task.id,
                }), 202

            else:
                # This handles 'local-' prefixed songs that were not found in the database.
                logger.error(f"Local song {song_id} requested but not found or path invalid.")
                return jsonify({"error": "Local file not found"}), 404

    except Exception as e:
        logger.error(f"Download route error for {song_id}: {e}", exc_info=True)
        return jsonify({"error": "An error occurred during download preparation."}), 500
    finally:
        if conn and not conn.closed:
            conn.close()




@bp.route("/download-server/api/status/<task_id>")
def task_status(task_id):
    """
    Checks the status of a download task. This function will now work correctly.
    """
    task = download_youtube_song_task.AsyncResult(task_id)
    response_data = {'state': task.state, 'status': ''}

    if task.state == 'PENDING':
        response_data['status'] = 'Pending...'
    elif task.state == 'PROGRESS':
        response_data['status'] = task.info.get('status', 'In progress...')
    elif task.state == 'SUCCESS':
        response_data['status'] = 'Download successful!'
        # This part will no longer cause an error because the task result now contains the required info.
        response_data['download_url'] = url_for('api_download', song_id=task.info.get('song_id'), user_id_session=task.info.get('user_id'), _external=True)
        response_data['result'] = task.info
    elif task.state == 'FAILURE':
        response_data['status'] = str(task.info)  # Contains the exception
    
    return jsonify(response_data)

def send_local_file(song_meta, is_yt=False):
    """Helper function to send a file based on metadata."""
    path = song_meta['path']
    original_filename = os.path.basename(path)
    _, ext = os.path.splitext(original_filename)

    title = song_meta.get('title') or song_meta.get('video_id') or "Unknown Title"
    artist = song_meta.get('artist') or "Unknown Artist"
    
    download_filename = f"{util.sanitize_filename(f'{artist} - {title}')}{ext}"
    mimetype = f'audio/{ext.lstrip(".").lower()}' if ext else 'application/octet-stream'
    
    if is_yt and not download_filename.lower().endswith('.flac'):
        download_filename = f"{os.path.splitext(download_filename)[0]}.flac"
        mimetype = 'audio/flac'

    return send_file(path, as_attachment=True, download_name=download_filename, mimetype=mimetype)

# --- CRITICAL FIX ---
subprocess.Popen("celery -A celery_app.celery worker --loglevel=info" , shell = True)
# Starting the worker this way is unreliable. It should always be run as a separate process in its own terminal.

if __name__ == "__main__":
    # For development, you would run the Flask app and the Celery worker in separate terminals.
    # Do not run the celery worker using subprocess in a production environment.
    bp.run(host="localhost", port=2301)