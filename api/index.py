"""Vercel serverless entry point — exposes the Flask app in app.py.

Vercel's rewrite sends every request to this function with the real path
carried in the __vpath query param (see vercel.json). The wrapper below
restores it before Flask routes the request.
"""
import os
import sys
from urllib.parse import unquote

# Make the project root importable (app.py lives one level up from api/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import app as flask_app  # noqa: E402,F401


def _restore_original_path(wsgi):
    """Restore PATH_INFO from the __vpath query param, however Vercel presents the path."""
    def handler(environ, start_response):
        raw = environ.get("QUERY_STRING", "")
        vpath = ""
        keep = []
        for pair in raw.split("&") if raw else []:
            if pair.startswith("__vpath="):
                vpath = pair[len("__vpath="):]
            else:
                keep.append(pair)
        if vpath:
            environ["PATH_INFO"] = "/" + unquote(vpath).strip("/")
            environ["QUERY_STRING"] = "&".join(keep)
        return wsgi(environ, start_response)
    return handler


app = _restore_original_path(flask_app)
