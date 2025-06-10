from flask import (
    Flask, send_file , Blueprint , abort , jsonify
)



import os 


import logging


bp = Blueprint("cdn-sangeet-ui" , __name__)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


STATIC_FOLDER = 'static'


@bp.route("/package/<name>")
def package_send(name):
    package_path = os.path.join(os.getcwd() , "css-package")
    if name == "material-icons":
        return send_file(os.path.join(package_path , "package-material-icons.woff2") , mimetype="font/woff2")
    elif name == "Poppins-01":
        return send_file(os.path.join(package_path , "Poppins-01.woff2") , mimetype="font/woff2")
    elif name == "Poppins-02":
        return send_file(os.path.join(package_path , "Poppins-02.woff2") , mimetype="font/woff2")
    elif name == "material-icons-round":
        return send_file(os.path.join(package_path , "material-icon-round.woff2") , mimetype="font/woff2")
    else:
        return "bad request"


@bp.route("/cdn/<type_n>/<name>")
def cdn_network(type_n , name):
    js_path = os.path.join(os.getcwd() , "design" , "js")
    css_path = os.path.join(os.getcwd() , "design" , "css")
    if type_n == "js":
        if name == "marked.min.js":
            return send_file(os.path.join(js_path , "marked.min.js"))
        elif name == "tailwind":
            return send_file(os.path.join(js_path , "tailwind.js"))
        else:
            return "bad request"
    elif type_n == "css":
        if name == "bootstrap.min.css":
            return send_file(os.path.join(css_path , "bootstrap.min.css"))
        elif name == "animate-min":
            return send_file(os.path.join(css_path , "animate.min.css"))
        elif name == "material-icon-round":
            return send_file(os.path.join(css_path , "material-icon-round.css"))
        else:
            return "bad request"
    else:
        return "bad request"

@bp.route("/design/<type>")
def design(type):
    """Serves CSS files from the design directory."""
    css_dir = os.path.join(os.getcwd(), "design", "css")
    allowed_files = {
        "index": "index.css",
        "embed": "embed.css",
        "settings" : "settings.css",
        "share" : "share.css",
        "report-issues" : "report_issues.css",
        "admin-issues": "admin_issues.css",   # Admin issue dashboard style
        "material-icon-set" : "material-icon-set.css",
        "Poppins" : "Poppins.css"
        # Add other CSS files here as needed
    }

    if type in allowed_files:
        file_path = os.path.join(css_dir, allowed_files[type])
        if os.path.exists(file_path):
            return send_file(file_path, mimetype="text/css")
        else:
            logger.error(f"CSS file not found for type '{type}' at path: {file_path}")
            abort(404) # Use Flask's abort for standard error handling
    else:
        logger.warning(f"Request for unknown design type: {type}")
        abort(404)





@bp.route("/data/download/icons/<type_icon>") # Renamed param
def icons(type_icon): # This seems to serve base64 encoded icons from text files.
    icon_base_path = os.path.join(os.getcwd(), "assets") # Assuming this is the root
    icon_map = {
        "download": ("favicons", "download", "fav.txt"),
        "sangeet-home": ("gifs", "sangeet", "index.gif"), # This one is a GIF
        "get-extension": ("favicons", "get-extension", "fav.txt"),
        "login-system-login": ("favicons", "login-system", "login.txt"),
        "login-system-register": ("favicons", "login-system", "register.txt"),
        "login-system-forgot": ("favicons", "login-system", "forgot.txt"),
        "generic_fallback": ("favicons", "genric", "fav.txt") # Added a key for fallback
    }
    
    path_parts = icon_map.get(type_icon)
    if not path_parts and type_icon != "generic_fallback": # Fallback if specific not found
        path_parts = icon_map.get("generic_fallback")
    elif not path_parts and type_icon == "generic_fallback": # Should not happen if map is correct
        logger.error("Generic fallback icon config missing.")
        abort(500)

    icon_file_path = os.path.join(icon_base_path, *path_parts)

    if type_icon == "sangeet-home": # Special case for GIF
        try: return send_file(icon_file_path, mimetype="image/gif")
        except FileNotFoundError: abort(404)
    
    try: # For base64 text files
        with open(icon_file_path, 'r', encoding='utf-8') as fav_file: # Added encoding
            data = fav_file.read().strip() # Strip whitespace
        return jsonify({"base64": data})
    except FileNotFoundError:
        logger.error(f"Icon text file not found: {icon_file_path}")
        # If specific icon not found, try generic again (already handled by logic above)
        # For robustness, if generic also fails (which it shouldn't if map is right):
        if type_icon != "generic_fallback": # Avoid infinite loop if generic itself is missing
             return icons("generic_fallback") # Try generic if specific fails
        abort(404) # If generic also missing
    except Exception as e_icon: # Renamed
        logger.error(f"Error reading icon file {icon_file_path}: {e_icon}")
        abort(500)




@bp.route('/static/<path:filename>')
def serve_static_file(filename):
    """
    Handles all requests under the /static/ route.

    It attempts to find the requested file within the 'static' directory.
    If the file exists, it serves the file.
    If the file does not exist, it returns a 404 Not Found error.
    """
    # Construct the absolute path to the 'static' directory
    static_dir = os.path.join(app.root_path, STATIC_FOLDER)

    # Construct the full path to the requested file
    file_path = os.path.join(static_dir, filename)

    # Check if the requested path points to an actual file
    # os.path.isfile() is important to ensure we only serve files,
    # and not directories or non-existent paths.
    if os.path.isfile(file_path):
        # Use send_from_directory. This is the recommended and secure way
        # to send static files in Flask. It handles security aspects like
        # preventing directory traversal attacks and sets appropriate
        # Content-Type headers based on the file extension.
        return send_from_directory(static_dir, filename)
    else:
        # If the file doesn't exist, return a 404 Not Found error.
        # abort(404) will raise an HTTPException, which Flask catches
        # and turns into the standard "Not Found" response.
        abort(404)
