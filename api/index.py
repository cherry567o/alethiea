"""Vercel serverless entry point — exposes the Flask app in app.py."""
import os
import sys

# Make the project root importable (app.py lives one level up from api/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import app  # noqa: E402,F401  (Vercel looks for `app` here)
