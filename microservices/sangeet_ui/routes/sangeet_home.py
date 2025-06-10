from flask import Blueprint , render_template , session , redirect , jsonify , render_template_string , url_for , request



import bcrypt
import time

import random
from datetime import datetime , timedelta , timezone

from interconnect.config import config
import psycopg2
from database.database import get_pg_connection
import os
import logging
from functools import wraps

server = Blueprint("sangeet_ui_server" , __name__)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

env_data_01 = config.get_env_data(os.path.join(os.getcwd() , "configs" , "ui" , "config.conf"))




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



@server.route("/")
@login_required
def home():
    info_to_render_index = {
        "version" : env_data_01.APP_VERSION,
        "app_url" : env_data_01.PROJECT_URL
    }
    return render_template("index.html" , config = info_to_render_index)


