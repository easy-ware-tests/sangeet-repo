from flask import (
    request , Blueprint , session , render_template , render_template_string , redirect , url_for , 
    jsonify
)

from datetime import datetime , timedelta , timezone

import secrets

import psycopg2

import os
import bcrypt
import time

import random
from utils import util

import logging
from database.database import get_pg_connection

from var_templates import var_templates
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)



bp = Blueprint("auth_ui_server" , __name__)

@bp.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    conn = None
    try:
        if request.method == 'POST':
            if 'step' not in session: # Email submission step
                email = request.form.get('email')
                if not email:
                    return render_template_string(var_templates.RESET_PASSWORD_HTML, step='email', error='Email is required')
                conn = get_pg_connection()
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute('SELECT id FROM users WHERE email = %s', (email,))
                    user = cur.fetchone()
                if not user:
                    return render_template_string(var_templates.RESET_PASSWORD_HTML, step='email', error='No account found with this email')
                
                # CRITICAL: util.generate_otp and util.store_otp_pg need to use PostgreSQL 'pending_otps'
                otp = util.generate_otp() 
                util.store_otp_pg(email, otp, 'reset_password') # Changed purpose for clarity

                email_sent = var_templates.send_forgot_password_email(email, otp) # Assumed to be just email sending
                if email_sent:
                    session['reset_email'] = email
                    session['step'] = 'verify_reset'
                    return render_template_string(var_templates.RESET_PASSWORD_HTML, step='verify', email=email) # step 'verify' for template
                else:
                    logger.warning(f"Failed to send password reset OTP to {email}.")
                    return render_template('email_not_supported.html', error_message="Could not send reset email. Please try again or contact support.")

            elif session.get('step') == 'verify_reset': # OTP verification step
                email = session.get('reset_email')
                otp_entered = request.form.get('otp')
                if not email or not otp_entered:
                    session.clear(); return redirect(url_for('auth_ui_server.login', error='Invalid session.'))
                
                # CRITICAL: util.verify_otp_pg needs to use PostgreSQL 'pending_otps'
                if not util.verify_otp_pg(email, otp_entered, 'reset_password'):
                    return render_template_string(var_templates.RESET_PASSWORD_HTML, step='verify', email=email, error='Invalid or expired code')
                
                conn = get_pg_connection() # Check user still exists
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute('SELECT id FROM users WHERE email = %s', (email,))
                    user_data = cur.fetchone()
                if not user_data:
                    session.clear(); return redirect(url_for('auth_ui_server.login', error='User not found.'))
                
                session['user_id_reset'] = user_data['id']
                session['step'] = 'new_password'
                return render_template_string(var_templates.RESET_PASSWORD_HTML, step='new_password', email=email) # Pass email for display

            elif session.get('step') == 'new_password': # New password setting step
                new_password = request.form.get('new_password')
                user_id_to_reset = session.get('user_id_reset') # Renamed
                email_for_reset = session.get('reset_email') # Get email for logging/clearing OTP

                if not new_password or not user_id_to_reset or not email_for_reset:
                    session.clear(); return redirect(url_for('auth_ui_server.login', error='Invalid reset state.'))
                if len(new_password) < 6:
                    return render_template_string(var_templates.RESET_PASSWORD_HTML, step='new_password', email=email_for_reset, error='Password too short.')

                password_hash_new = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8') # Renamed
                conn = get_pg_connection()
                with conn.cursor() as cur:
                    cur.execute('UPDATE users SET password_hash = %s WHERE id = %s', (password_hash_new, user_id_to_reset))
                    # CRITICAL: util.clear_otp should clear from PostgreSQL 'pending_otps'
                    
                    conn.commit()
                
                session.pop('step', None); session.pop('reset_email', None); session.pop('user_id_reset', None)
                logger.info(f"Password reset successful for user ID: {user_id_to_reset}")
                return redirect(url_for('auth_ui_server.login', success='Password has been reset successfully.'))
            else: # Invalid step
                session.clear(); return redirect(url_for('auth_ui_server.login'))
        else: # GET request
            session.pop('step', None) # Clear any partial reset state on fresh GET
            return render_template_string(var_templates.RESET_PASSWORD_HTML, step='email')
    except psycopg2.Error as db_err_reset: # Renamed
        logger.error(f"PostgreSQL error during password reset: {db_err_reset}")
        if conn: conn.rollback()
        return render_template_string(var_templates.RESET_PASSWORD_HTML, step='email', error='A database error occurred. Please try again.')
    except Exception as e_reset: # Renamed
        logger.error(f"Unexpected error during password reset: {e_reset}", exc_info=True)
        if conn: conn.rollback()
        return render_template_string(var_templates.RESET_PASSWORD_HTML, step='email', error='An unexpected error occurred.')
    finally:
        if conn: conn.close()

@bp.route('/forgot_username', methods=['GET', 'POST'])
def forgot_username():
    conn = None
    if request.method == 'POST':
        email_form_fu = request.form.get('email') # Renamed
        if not email_form_fu:
            return render_template_string(var_templates.FORGOT_USERNAME_HTML, step='email', error='Email is required')
        try:
            conn = get_pg_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute('SELECT username FROM users WHERE email = %s', (email_form_fu,))
                user_fu = cur.fetchone() # Renamed
            if not user_fu:
                return render_template_string(var_templates.FORGOT_USERNAME_HTML, step='email', error='No account with this email.')
            
            # var_templates.send_forgot_username_email is assumed to be just email sending logic
            email_sent_fu = var_templates.send_forgot_username_email(email_form_fu, user_fu['username']) # Renamed
            if email_sent_fu:
                return render_template_string(var_templates.LOGIN_HTML, login_step='initial', success='Username sent to your email.')
            else:
                logger.warning(f"Failed to send 'forgot username' email to {email_form_fu}.")
                return render_template('email_not_supported.html', error_message="Could not send username email.")
        except psycopg2.Error as db_err_fu: # Renamed
            logger.error(f"DB error in forgot_username: {db_err_fu}")
            return render_template_string(var_templates.FORGOT_USERNAME_HTML, step='email', error='Database error.')
        except Exception as e_fu: # Renamed
            logger.error(f"Error in forgot_username: {e_fu}", exc_info=True)
            return render_template_string(var_templates.FORGOT_USERNAME_HTML, step='email', error='An error occurred.')
        finally:
            if conn: conn.close()
    return render_template_string(var_templates.FORGOT_USERNAME_HTML, step='email')


@bp.route('/logout')
def logout():
    # login_required is not needed here
    conn = None
    if 'user_id' in session and 'session_token' in session:
        try:
            conn = get_pg_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_sessions WHERE user_id = %s AND session_token = %s",
                            (session['user_id'], session['session_token']))
                conn.commit()
            logger.info(f"User {session.get('user_id')} logged out, session token {session.get('session_token')} removed.")
        except psycopg2.Error as e_logout_db: # Renamed
            logger.error(f"DB error during logout for user {session.get('user_id')}: {e_logout_db}")
            if conn: conn.rollback()
        except Exception as e_logout: # Renamed
            logger.error(f"Unexpected error during logout: {e_logout}", exc_info=True)
        finally:
            if conn: conn.close()
    session.clear()
    return redirect(url_for('auth_ui_server.login'))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    conn = None
    # Session check
    if 'user_id' in session and 'session_token' in session:
        try:
            conn = get_pg_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM active_sessions WHERE user_id = %s AND session_token = %s AND expires_at > CURRENT_TIMESTAMP",
                            (session['user_id'], session['session_token']))
                if cur.fetchone():
                    return redirect(url_for('sangeet_ui_server.home'))
                else: # Stale session in client
                    session.clear()
        except psycopg2.Error as e_sess_check: # Renamed
            logger.error(f"DB error during login session check: {e_sess_check}")
            session.clear() # Clear session on DB error
        finally:
            if conn: conn.close(); conn = None # Important to reset conn after use

    # Main login logic
    if request.method == 'POST':
        login_id_form_li = request.form.get('login_id') # Renamed
        password_form_li = request.form.get('password') # Renamed
        if not login_id_form_li or not password_form_li:
            return render_template_string(var_templates.LOGIN_HTML, login_step='initial', error='All fields required.')
        
        session.pop('temp_login', None) # Clear previous temp data
        try:
            conn = get_pg_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT id, password_hash, twofa_method, email,
                           (SELECT COUNT(*) FROM active_sessions WHERE user_id = users.id AND expires_at > CURRENT_TIMESTAMP) as active_sessions_count
                    FROM users WHERE email = %s OR username = %s
                """, (login_id_form_li, login_id_form_li))
                user_row_li = cur.fetchone() # Renamed

                if user_row_li and bcrypt.checkpw(password_form_li.encode('utf-8'), user_row_li['password_hash'].encode('utf-8')):
                    user_id_li = user_row_li['id'] # Renamed
                    if user_row_li['active_sessions_count'] > 0: # Terminate other sessions
                        cur.execute("DELETE FROM active_sessions WHERE user_id = %s", (user_id_li,))
                        logger.info(f"Terminated {user_row_li['active_sessions_count']} existing sessions for user {user_id_li}.")
                        # Commit will happen with other operations or at the end of 'with'

                    if user_row_li['twofa_method'] != 'none':
                        token_2fa = secrets.token_urlsafe(32) # Renamed
                        if user_row_li['twofa_method'] == 'email':
                            otp_2fa = util.generate_otp() # Renamed
                            # CRITICAL: util.store_otp_pg needs to use PostgreSQL 'pending_otps'
                            util.store_otp_pg(user_row_li['email'], otp_2fa, 'login') 
                            # util.send_email is assumed to be just email sending
                            email_sent_2fa = util.send_email(user_row_li['email'], 'Login Verification', f'Code: {otp_2fa}') # Renamed
                            if email_sent_2fa:
                                session['temp_login'] = {'token': token_2fa, 'user_id': user_id_li, 'twofa_method': user_row_li['twofa_method']}
                                conn.commit() # Commit session termination and OTP storage
                                return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', login_token=token_2fa, twofa_method=user_row_li['twofa_method'])
                            else:
                                conn.rollback() # Rollback session termination if email fails
                                return render_template('email_not_supported.html', error_message="Could not send 2FA email.")
                        # Add other 2FA methods (e.g., 'totp') here
                        else: # Unsupported 2FA
                            conn.rollback()
                            return render_template_string(var_templates.LOGIN_HTML, login_step='initial', error='Unsupported 2FA.')
                    else: # No 2FA, direct login
                        session_token_li = secrets.token_urlsafe(32) # Renamed
                        expires_at_li = datetime.now() + timedelta(days=7) # Renamed
                        cur.execute("INSERT INTO active_sessions (user_id, session_token, expires_at) VALUES (%s, %s, %s)",
                                    (user_id_li, session_token_li, expires_at_li))
                        conn.commit()
                        session.clear()
                        session['user_id'] = user_id_li
                        session['session_token'] = session_token_li
                        return redirect(url_for('sangeet_ui_server.home'))
                else: # Invalid credentials
                    time.sleep(random.uniform(0.1,0.3)) # Basic rate limiting
                    return render_template_string(var_templates.LOGIN_HTML, login_step='initial', error='Invalid credentials.')
        except psycopg2.Error as db_err_li: # Renamed
            logger.error(f"DB error during login: {db_err_li}")
            if conn: conn.rollback()
            return render_template_string(var_templates.LOGIN_HTML, login_step='initial', error='Database error.')
        except Exception as e_li: # Renamed
            logger.error(f"Login error: {e_li}", exc_info=True)
            if conn: conn.rollback()
            return render_template_string(var_templates.LOGIN_HTML, login_step='initial', error='An error occurred.')
        finally:
            if conn: conn.close()
    else: # GET request
        return render_template_string(var_templates.LOGIN_HTML, login_step='initial')


@bp.route('/login_verify', methods=['POST'])
def login_verify():
    conn = None
    if 'temp_login' not in session: return redirect(url_for('auth_ui_server.login'))
    temp_data_lv = session['temp_login'] # Renamed
    otp_form_lv = request.form.get('otp') # Renamed
    token_form_lv = request.form.get('login_token') # Renamed

    if token_form_lv != temp_data_lv.get('token'): # Use .get for safety
        return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', error='Invalid session token.')

    try:
        if temp_data_lv['twofa_method'] == 'email':
            conn_lv_check = get_pg_connection() # Renamed
            with conn_lv_check.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_lv_check: # Renamed
                cur_lv_check.execute('SELECT email FROM users WHERE id = %s', (temp_data_lv['user_id'],))
                email_row_lv = cur_lv_check.fetchone() # Renamed
            if not email_row_lv: # Should not happen if temp_login is valid
                 return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', login_token=token_form_lv, twofa_method=temp_data_lv['twofa_method'], error='User data inconsistent.')
            # CRITICAL: util.verify_otp_pg needs to use PostgreSQL 'pending_otps'
            if not util.verify_otp_pg(email_row_lv['email'], otp_form_lv, 'login'):
                return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', login_token=token_form_lv, twofa_method=temp_data_lv['twofa_method'], error='Invalid or expired code.')
        # Add elif for 'totp' if implemented
        
        # If OTP is valid, create permanent session
        session_token_perm = secrets.token_urlsafe(32) # Renamed
        expires_at_perm = datetime.now() + timedelta(days=7) # Renamed
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO active_sessions (user_id, session_token, expires_at) VALUES (%s, %s, %s)",
                        (temp_data_lv['user_id'], session_token_perm, expires_at_perm))
            # CRITICAL: util.clear_otp should clear from PostgreSQL 'pending_otps'
          
            conn.commit()
        
        session.clear()
        session['user_id'] = temp_data_lv['user_id']
        session['session_token'] = session_token_perm
        return redirect(url_for('sangeet_ui_server.home'))
    except psycopg2.Error as db_err_lv: # Renamed
        logger.error(f"DB error in login_verify: {db_err_lv}")
        if conn: conn.rollback()
        return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', login_token=token_form_lv, error='Database error during verification.')
    except Exception as e_lv: # Renamed
        logger.error(f"Error in login_verify: {e_lv}", exc_info=True)
        if conn: conn.rollback()
        return render_template_string(var_templates.LOGIN_HTML, login_step='2fa', login_token=token_form_lv, error='An unexpected error occurred.')
    finally:
        if conn: conn.close()
        if 'conn_lv_check' in locals() and conn_lv_check: conn_lv_check.close()


@bp.route('/register', methods=['GET', 'POST'])
def register():
    conn = None
    if request.method == 'POST':
        email_reg = request.form.get('email') # Renamed
        username_reg = request.form.get('username') # Renamed
        full_name_reg = request.form.get('full_name') # Renamed
        password_reg = request.form.get('password') # Renamed

        if not all([email_reg, username_reg, full_name_reg, password_reg]):
            return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='All fields required.')
        if len(password_reg) < 6:
             return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='Password too short.')
        
        try:
            conn = get_pg_connection()
            with conn.cursor() as cur: # Check for existing email/username
                cur.execute("SELECT 1 FROM users WHERE email = %s OR username = %s", (email_reg, username_reg))
                if cur.fetchone():
                    return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='Email or username exists.')
            # conn is closed by 'with' here. OTP storage will need its own or new connection.

            otp_reg = util.generate_otp() # Renamed
            # CRITICAL: util.store_otp_pg needs to use PostgreSQL 'pending_otps'
            util.store_otp_pg(email_reg, otp_reg, 'register')
            # var_templates.send_register_otp_email assumed to be just email sending
            email_sent_reg = var_templates.send_register_otp_email(email_reg, otp_reg) # Renamed

            if email_sent_reg:
                token_reg_sess = secrets.token_urlsafe(32) # Renamed
                session['register_data'] = {
                    'token': token_reg_sess, 'email': email_reg, 'username': username_reg,
                    'full_name': full_name_reg, 'password': password_reg # Store plain pass temporarily
                }
                return render_template_string(var_templates.REGISTER_HTML, register_step='verify', email=email_reg, register_token=token_reg_sess)
            else:
                logger.warning(f"Failed to send registration OTP to {email_reg}.")
                # CRITICAL: util.clear_otp should clear from PostgreSQL 'pending_otps' if OTP stored before send attempt failed
             
                return render_template('email_not_supported.html', error_message="Could not send registration email.")
        except psycopg2.Error as db_err_reg: # Renamed
            logger.error(f"DB error during registration: {db_err_reg}")
            # No conn.rollback() here as conn from 'with' is likely closed or wasn't the one failing.
            # If util.store_otp_pg handles its own transaction, that's separate.
            return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='Database error.')
        except Exception as e_reg: # Renamed
            logger.error(f"Registration error: {e_reg}", exc_info=True)
            return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='An error occurred.')
        # No conn.close() needed here for the initial check connection.
    else: # GET request
        return render_template_string(var_templates.REGISTER_HTML, register_step='initial')

@bp.route('/register/verify', methods=['POST'])
def register_verify():
    conn = None
    if 'register_data' not in session: return redirect(url_for('auth_ui_server.register'))
    reg_data_rv = session['register_data'] # Renamed
    otp_form_rv = request.form.get('otp') # Renamed
    token_form_rv = request.form.get('register_token') # Renamed

    if token_form_rv != reg_data_rv.get('token'):
        return render_template_string(var_templates.REGISTER_HTML, register_step='verify', email=reg_data_rv.get('email'), error='Invalid session token.')
    
    # CRITICAL: util.verify_otp_pg needs to use PostgreSQL 'pending_otps'
    if not util.verify_otp_pg(reg_data_rv['email'], otp_form_rv, 'register'):
        return render_template_string(var_templates.REGISTER_HTML, register_step='verify', email=reg_data_rv['email'], register_token=token_form_rv, error='Invalid or expired code.')
    
    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: # Use DictCursor for RETURNING id
            password_hash_rv = bcrypt.hashpw(reg_data_rv['password'].encode('utf-8'), bcrypt.gensalt()).decode('utf-8') # Renamed
            cur.execute("INSERT INTO users (email, username, full_name, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
                        (reg_data_rv['email'], reg_data_rv['username'], reg_data_rv['full_name'], password_hash_rv))
            user_id_rv = cur.fetchone()['id'] # Renamed

            session_token_rv = secrets.token_urlsafe(32) # Renamed
            expires_at_rv = datetime.now() + timedelta(days=7) # Renamed
            cur.execute("INSERT INTO active_sessions (user_id, session_token, expires_at) VALUES (%s, %s, %s)",
                        (user_id_rv, session_token_rv, expires_at_rv))
            # CRITICAL: util.clear_otp should clear from PostgreSQL 'pending_otps'
         
            conn.commit()
        
        session.pop('register_data')
        session['user_id'] = user_id_rv
        session['session_token'] = session_token_rv
        return redirect(url_for('sangeet_ui_server.home'))
    except psycopg2.Error as db_err_rv: # Renamed
        logger.error(f"DB error in register_verify: {db_err_rv}")
        if conn: conn.rollback()
        return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='Registration failed due to database error.')
    except Exception as e_rv: # Renamed
        logger.error(f"Error in register_verify: {e_rv}", exc_info=True)
        if conn: conn.rollback()
        return render_template_string(var_templates.REGISTER_HTML, register_step='initial', error='An unexpected error occurred during registration.')
    finally:
        if conn: conn.close()


@bp.route('/api/resend-otp', methods=['POST'])
def resend_otp():
    # This route's logic for determining purpose (login, register, reset) and OTP handling
    # needs to be PG-aware via util.store_otp_pg and util.send_email.
    # The core logic of identifying the context from session/tokens remains.
    conn = None # For user email lookup if needed
    data = request.json
    if not data: return jsonify({'error': 'No JSON data provided'}), 400

    purpose, email_to_send = None, None # Renamed
    user_id_for_email_lookup = None

    # Determine context
    if 'login_token' in data and 'temp_login' in session:
        temp_login_ro = session['temp_login'] # Renamed
        if data['login_token'] == temp_login_ro.get('token'):
            user_id_for_email_lookup = temp_login_ro.get('user_id')
            purpose = 'login'
    elif 'register_token' in data and 'register_data' in session:
        reg_data_ro = session['register_data'] # Renamed
        if data['register_token'] == reg_data_ro.get('token'):
            email_to_send = reg_data_ro.get('email')
            purpose = 'register'
    elif 'reset_email' in data and session.get('step') == 'verify_reset': # Check against the reset step
        email_to_send = session.get('reset_email') # Email is directly in session for reset
        # Verify user still exists for reset context
        if email_to_send:
            try:
                conn_check_ro = get_pg_connection() # Renamed
                with conn_check_ro.cursor() as cur_check_ro: #Renamed
                    cur_check_ro.execute("SELECT 1 FROM users WHERE email = %s", (email_to_send,))
                    if not cur_check_ro.fetchone(): email_to_send = None # User deleted
            except psycopg2.Error: email_to_send = None # DB error, can't confirm
            finally: 
                if 'conn_check_ro' in locals() and conn_check_ro: conn_check_ro.close()
        purpose = 'reset_password' if email_to_send else None


    if not purpose: return jsonify({'error': 'Invalid request or session state for OTP resend'}), 400
    
    # Fetch email if only user_id was available (login context)
    if user_id_for_email_lookup and not email_to_send:
        try:
            conn = get_pg_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT email FROM users WHERE id = %s", (user_id_for_email_lookup,))
                user_email_row = cur.fetchone() # Renamed
                if user_email_row: email_to_send = user_email_row['email']
        except psycopg2.Error as db_err_ro_email: # Renamed
            logger.error(f"DB error fetching email for OTP resend (user {user_id_for_email_lookup}): {db_err_ro_email}")
            return jsonify({'error': 'Database error preparing OTP resend.'}), 500
        finally:
            if conn: conn.close(); conn = None
    
    if not email_to_send: return jsonify({'error': 'Could not determine email address for OTP.'}), 400

    otp_new_ro = util.generate_otp() # Renamed
    # CRITICAL: util.store_otp_pg needs to use PostgreSQL 'pending_otps'
    util.store_otp_pg(email_to_send, otp_new_ro, purpose) 
    
    subject_map = {'login': 'New Login Code', 'register': 'New Registration Code', 'reset_password': 'New Password Reset Code'}
    # util.send_email is assumed to be just email sending
    email_sent_ro = util.send_email(email_to_send, subject_map.get(purpose, 'New Verification Code'), f'New code: {otp_new_ro}') # Renamed

    if email_sent_ro:
        logger.info(f"Resent OTP for {purpose} to {email_to_send}.")
        return jsonify({'status': 'success', 'message': 'New OTP sent.'})
    else:
        logger.warning(f"Failed to resend {purpose} OTP to {email_to_send}.")
        return jsonify({'error': 'Failed to send email.', 'status': 'email_failed'}), 503


# --- End of Authentication Routes Block ---


# --- Session Management (PostgreSQL Ready) ---
@bp.before_request
def before_request_hook(): # Renamed to avoid conflict if other BPs have before_request
    # CRITICAL: util.cleanup_expired_sessions() MUST be updated to use PostgreSQL 'active_sessions'
    util.cleanup_expired_sessions_pg() 

    if 'user_id' in session and 'session_token' in session:
        # Rate limit this check or make it less frequent if it becomes a bottleneck
        # For now, it checks on every request for logged-in users.
        conn_br = None # Renamed
        try:
            conn_br = get_pg_connection()
            with conn_br.cursor() as cur_br: # Renamed
                cur_br.execute("SELECT 1 FROM active_sessions WHERE user_id = %s AND session_token = %s AND expires_at > CURRENT_TIMESTAMP",
                               (session['user_id'], session['session_token']))
                if not cur_br.fetchone():
                    logger.info(f"Invalidating session for user {session.get('user_id')} due to before_request check.")
                    session.clear()
        except psycopg2.Error as e_br_db: # Renamed
            logger.error(f"DB error in before_request session check: {e_br_db}")
            session.clear() # Invalidate session on DB error during check for safety
        finally:
            if conn_br: conn_br.close()

@bp.route("/api/session-status")
def check_session_status():
    if 'user_id' not in session or 'session_token' not in session:
        return jsonify({"valid": False, "reason": "no_session"}), 401
    conn_ss = None # Renamed
    try:
        conn_ss = get_pg_connection()
        with conn_ss.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur_ss: # Renamed
            cur_ss.execute("SELECT 1 FROM active_sessions WHERE user_id = %s AND session_token = %s AND expires_at > CURRENT_TIMESTAMP",
                           (session['user_id'], session['session_token']))
            current_session_valid_ss = cur_ss.fetchone() is not None # Renamed

            other_sessions_exist_ss = False # Renamed
            if not current_session_valid_ss: # Only check if current is invalid
                cur_ss.execute("SELECT 1 FROM active_sessions WHERE user_id = %s AND expires_at > CURRENT_TIMESTAMP LIMIT 1", (session['user_id'],))
                other_sessions_exist_ss = cur_ss.fetchone() is not None
        
        if not current_session_valid_ss:
            reason = "logged_out_elsewhere" if other_sessions_exist_ss else "expired_or_invalid" # More generic
            logger.info(f"Session invalid for user {session.get('user_id')}: {reason}")
            return jsonify({"valid": False, "reason": reason}), 401
        return jsonify({"valid": True})
    except psycopg2.Error as e_ss_db: # Renamed
        logger.error(f"Session status check DB error: {e_ss_db}")
        return jsonify({"valid": False, "reason": "error_db_check"}), 500 # Indicate server-side issue
    except Exception as e_ss_gen: # Renamed
        logger.error(f"Unexpected error in session status check: {e_ss_gen}", exc_info=True)
        return jsonify({"valid": False, "reason": "error_unexpected"}), 500
    finally:
        if conn_ss: conn_ss.close()


# --- Static Asset and Design Routes (Unchanged, no DB interaction) ---
@bp.route("/favicon.ico")
def set_fake(): return "Favicon not configured here.", 404 # More appropriate 404

@bp.route('/terms-register', methods=['GET'])
def terms_register():
    try:
        with open(os.path.join(os.getcwd(), "terms", "terms_register.txt"), 'r', encoding='utf-8') as file: # Added encoding
            terms_content = file.read()
        formatted_terms = f"""<div class="space-y-4"><h4 class="text-xl font-semibold mb-3">Sangeet Premium Terms of Service</h4>{terms_content}</div>"""
        return formatted_terms
    except FileNotFoundError:
        logger.error("terms_register.txt not found.")
        return "Terms and Conditions are currently unavailable. Please try again later.", 503 # Service Unavailable
    except Exception as e_terms: # Renamed
        logger.error(f"Error reading terms file: {e_terms}")
        return "Could not load Terms and Conditions due to an internal error.", 500