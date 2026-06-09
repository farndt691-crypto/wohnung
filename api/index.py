# Vercel entry point
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TEMPLATES_DIR", os.path.join(ROOT, "templates"))

from main import app  # noqa: F401  ← Vercel findet "app" hier
