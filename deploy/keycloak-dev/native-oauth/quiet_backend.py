"""Use the production app without logging opaque capability paths."""

import logging

from app.main import app  # noqa: F401

logging.getLogger("uvicorn.access").disabled = True
