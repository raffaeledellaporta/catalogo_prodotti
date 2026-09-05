"""
config.py
=========

Configurazione del sito vetrina.

PER RINOMINARE IL SITO: cambia SITE_NAME qui sotto (oppure, meglio, crea un
file `.env` nella cartella del progetto con questa riga:

    SITE_NAME=Il Nome Che Vuoi

e riavvia il sito. Il nome verrà usato ovunque: titolo delle pagine, header,
footer.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # Nome del sito mostrato agli utenti (titolo pagine, header, footer).
    # Per rinominarlo: imposta la variabile d'ambiente SITE_NAME, o mettila
    # in un file .env nella cartella del progetto (vedi sopra).
    SITE_NAME = os.environ.get("SITE_NAME", "EG EXCLUSIVE GEAR")

    SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-questa-chiave-in-produzione")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'catalogo.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Cartella dove vengono salvate le immagini caricate (sia da upload
    # manuale via admin, sia da upload automatico via API dallo scraper).
    UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200 MB per richiesta

    # Password per accedere alla pagina di amministrazione (upload manuale).
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

    # Chiave richiesta dallo script locale (yupoo_scraper.py + image_cleaner.py)
    # per pubblicare automaticamente i prodotti via API (POST /api/prodotti).
    API_KEY = os.environ.get("API_KEY", "cambia-questa-api-key")

