"""
pubblica_da_yupoo.py
=====================

Script "tutto in uno" da eseguire in LOCALE sul tuo PC. Per ogni link Yupoo
che incolli:

  1. Scarica tutte le foto di ogni prodotto (album), alla risoluzione più
     alta (yupoo_scraper.py)
  2. Rimuove le scritte cinesi da ogni foto con OCR + inpainting
     (image_cleaner.py)
  3. Pubblica automaticamente il prodotto sul sito vetrina, chiamando la
     API POST /api/prodotti (Opzione B: upload automatico)

Uso:
    python pubblica_da_yupoo.py

Configurazione (variabili d'ambiente, oppure file ".env" in questa cartella):
    SITE_API_URL   URL del sito vetrina (default: http://127.0.0.1:5000)
    API_KEY        Deve coincidere con API_KEY in webapp/.env

Il sito vetrina (webapp/) deve essere in esecuzione perché l'upload
automatico funzioni. Se preferisci separare i passaggi, puoi anche usare
yupoo_scraper.py e image_cleaner.py singolarmente da riga di comando.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

import image_cleaner as ic
import yupoo_scraper as ys

load_dotenv(Path(__file__).resolve().parent / ".env")

SITE_API_URL = os.environ.get("SITE_API_URL", "http://127.0.0.1:5000")
API_KEY = os.environ.get("API_KEY", "cambia-questa-api-key")

RAW_DIR = "download"
CLEAN_DIR = "pulito"


def publish_product(product: ys.Product, raw_dir: Path, clean_dir: Path) -> bool:
    """Pulisce le foto di un prodotto e le pubblica sul sito. Ritorna True
    se la pubblicazione è andata a buon fine."""
    print(f"\n[PULIZIA] '{product.name}' ({len(product.saved_files)} foto)...")
    summary = ic.clean_folder(raw_dir, clean_dir)
    print(
        f"[PULIZIA] Completata: {summary['con_testo_rimosso']} foto pulite, "
        f"{summary['senza_testo']} già senza testo."
    )

    clean_files = sorted(
        f for f in clean_dir.iterdir() if f.is_file() and f.suffix.lower() in ic.IMAGE_EXTENSIONS
    )
    if not clean_files:
        print(f"[WARN] Nessuna immagine pulita da pubblicare per '{product.name}'.")
        return False

    print(f"[UPLOAD] Pubblico '{product.name}' su {SITE_API_URL} ({len(clean_files)} foto)...")
    files_payload = []
    open_handles = []
    try:
        for f in clean_files:
            handle = open(f, "rb")
            open_handles.append(handle)
            files_payload.append(("images", (f.name, handle, "image/jpeg")))

        data = {"name": product.name, "source_url": product.album_url}
        headers = {"X-API-KEY": API_KEY}

        resp = requests.post(
            f"{SITE_API_URL.rstrip('/')}/api/prodotti",
            data=data,
            files=files_payload,
            headers=headers,
            timeout=60,
        )
    except requests.RequestException as exc:
        print(f"[ERRORE] Impossibile contattare il sito ({SITE_API_URL}): {exc}")
        return False
    finally:
        for handle in open_handles:
            handle.close()

    if resp.status_code == 201:
        result = resp.json()
        print(f"[OK] Pubblicato: {result['url']}")
        return True

    print(f"[ERRORE] Il sito ha risposto {resp.status_code}: {resp.text[:300]}")
    return False


def process_link(yupoo_url: str) -> None:
    products = ys.scrape_yupoo_link(yupoo_url, output_dir=RAW_DIR)

    if not products:
        print("[WARN] Nessun prodotto scaricato da questo link.")
        return

    published = 0
    for product in products:
        # Ricostruiamo la cartella in cui yupoo_scraper ha salvato le foto
        # grezze di questo prodotto, per passarla alla pulizia.
        if not product.saved_files:
            continue
        raw_dir = product.saved_files[0].parent
        rel = raw_dir.relative_to(RAW_DIR)
        clean_dir = Path(CLEAN_DIR) / rel

        if publish_product(product, raw_dir, clean_dir):
            published += 1

    print(f"\n[RIEPILOGO] {published}/{len(products)} prodotto/i pubblicato/i sul sito.")


def _interactive_loop() -> None:
    print("=" * 70)
    print("Pubblicazione automatica da Yupoo al sito vetrina")
    print(f"Sito di destinazione: {SITE_API_URL}")
    print("Incolla un link Yupoo (catalogo/ricerca o singolo album/prodotto).")
    print("Scrivi 'exit', 'quit' o 'q' per uscire.")
    print("=" * 70)

    while True:
        try:
            link = input("\nLink Yupoo> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nUscita.")
            break

        if not link:
            continue
        if link.lower() in {"exit", "quit", "q"}:
            print("Uscita.")
            break
        if "yupoo" not in link:
            print("[WARN] Il link non sembra un link Yupoo valido, riprova.")
            continue

        try:
            process_link(link)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERRORE] Errore inatteso: {exc}")


if __name__ == "__main__":
    import sys

    links = sys.argv[1:]
    for link in links:
        process_link(link)

    _interactive_loop()

