"""Configuración central del prototipo."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "crawl.db")
RAW_DIR = os.path.join(DATA_DIR, "raw")
REPLICA_DIR = os.path.join(DATA_DIR, "replica")

# Cortesía y límites
REQUEST_TIMEOUT = 30
RENDER_TIMEOUT = 60
RENDER_MIN_CHARS = 200      # si el HTML crudo trae menos texto, se renderiza
PER_DOMAIN_DELAY = 1.0      # segundos entre hits al mismo dominio
MAX_DEPTH = 3
MAX_PAGES_PER_DOMAIN = 200
MAX_BYTES = 15 * 1024 * 1024
MAX_RETRIES = 3
BACKOFF = [2, 8, 30]        # espera entre reintentos
USER_AGENT = "PrototipoCrawler/0.1 (+uso local con permiso del cliente)"

for d in (DATA_DIR, RAW_DIR, REPLICA_DIR):
    os.makedirs(d, exist_ok=True)
