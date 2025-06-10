

from flask import Blueprint , redirect , session




bp = Blueprint("sangeet_download_server_proxy" , __name__)




@bp.route("/api/download/<song_id>")
def proxy_download(song_id):
    user_id = session["user_id"]
    return redirect(f"/download-server/api/download/{song_id}/{user_id}")