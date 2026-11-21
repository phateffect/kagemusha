import logging
import os

from .app import create_app
from .config import Settings

os.umask(0o077)
logging.basicConfig(level=logging.INFO, format="%(message)s")

app = create_app(Settings())
