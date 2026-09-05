"""
image_cleaner.py
================

Rimuove le scritte cinesi (watermark, prezzi, codici articolo scritti sulla
foto, ecc.) dalle immagini prodotto, tramite:

  1) OCR con PaddleOCR (rileva le aree di testo, incluso il cinese)
  2) Inpainting con OpenCV (ricostruisce lo sfondo dove il testo viene
     rimosso, in modo che l'immagine resti "pulita" e naturale)

Uso come libreria:
    from image_cleaner import clean_image, clean_folder

    clean_image("download/prodotto/001.jpg", "pulito/prodotto/001.jpg")
    clean_folder("download/prodotto", "pulito/prodotto")

Uso da riga di comando:
    python image_cleaner.py <cartella_input> [--out cartella_output]

Note:
- Il modello PaddleOCR viene scaricato automaticamente al primo utilizzo
  (richiede una connessione internet la prima volta; poi resta in cache
  locale). L'inizializzazione può richiedere qualche secondo.
- L'inpainting usa cv2.INPAINT_TELEA. Per risultati ancora migliori su
  sfondi complessi si può in futuro sostituire con un modello di deep
  learning (es. LaMa), mantenendo la stessa interfaccia `clean_image`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import requests
import urllib3

# ---------------------------------------------------------------------------
# Reti aziendali con proxy/certificato self-signed: disabilitiamo la verifica
# SSL per TUTTE le richieste fatte tramite la libreria `requests` (usata
# internamente anche da huggingface_hub/paddlex per scaricare i modelli
# PaddleOCR). Senza questo, il download dei modelli fallisce con errori SSL.
# ---------------------------------------------------------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_original_merge_environment_settings = requests.Session.merge_environment_settings


def _merge_environment_settings_no_verify(self, url, proxies, stream, verify, cert):
    settings = _original_merge_environment_settings(self, url, proxies, stream, verify, cert)
    settings["verify"] = False
    return settings


requests.Session.merge_environment_settings = _merge_environment_settings_no_verify

# Evita il controllo di connettività preliminare (più veloce) e usa BOS
# (Baidu Object Storage) come sorgente preferita per i modelli, con fallback
# automatico alle altre sorgenti (huggingface, modelscope, aistudio) se BOS
# non fosse raggiungibile.
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

_ocr_instance = None  # inizializzato pigramente, è costoso da creare


def _get_ocr():
    """Crea (una sola volta) l'istanza PaddleOCR condivisa."""
    global _ocr_instance
    if _ocr_instance is None:
        from paddleocr import PaddleOCR

        print("[INFO] Inizializzo PaddleOCR (può richiedere qualche secondo "
              "la prima volta, scarica i modelli)...")
        _ocr_instance = PaddleOCR(
            lang="ch",  # modello cinese (riconosce anche testo latino/numeri)
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            # Necessario: con oneDNN/mkldnn abilitato, alcuni modelli PP-OCRv6
            # causano un errore "ConvertPirAttribute2RuntimeAttribute" nel
            # nuovo executor PIR di PaddlePaddle (bug noto su CPU).
            enable_mkldnn=False,
        )
    return _ocr_instance


def detect_text_polygons(image_path: str | Path) -> list[np.ndarray]:
    """Ritorna la lista dei poligoni (array Nx2 di punti int) delle aree di
    testo individuate nell'immagine."""
    ocr = _get_ocr()
    results = ocr.predict(str(image_path))

    polygons: list[np.ndarray] = []
    for res in results:
        data = res.json if hasattr(res, "json") else res
        # paddleocr 3.x: il risultato (o il suo .json) contiene la chiave
        # "res" con "rec_polys" (o "dt_polys" se non è stato fatto il
        # riconoscimento del testo, solo la detection).
        payload = data.get("res", data) if isinstance(data, dict) else data
        polys = None
        if isinstance(payload, dict):
            polys = payload.get("rec_polys") or payload.get("dt_polys")
        if polys is None:
            continue
        for poly in polys:
            arr = np.array(poly, dtype=np.int32).reshape(-1, 2)
            polygons.append(arr)

    return polygons


def _build_mask(shape: tuple[int, int], polygons: list[np.ndarray], padding: int = 6) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    for poly in polygons:
        cv2.fillPoly(mask, [poly], 255)
    if padding > 0:
        kernel = np.ones((padding, padding), np.uint8)
        mask = cv2.dilate(mask, kernel, iterations=1)
    return mask


def clean_image(
    input_path: str | Path,
    output_path: str | Path,
    inpaint_radius: int = 7,
    mask_padding: int = 6,
) -> bool:
    """Rimuove il testo individuato da un'immagine e salva il risultato.

    Ritorna True se è stato trovato ed effettivamente rimosso del testo,
    False se non è stato rilevato alcun testo (in tal caso l'immagine
    viene comunque copiata così com'è in output_path).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    image = cv2.imread(str(input_path))
    if image is None:
        raise ValueError(f"Impossibile leggere l'immagine: {input_path}")

    polygons = detect_text_polygons(input_path)

    if not polygons:
        cv2.imwrite(str(output_path), image)
        return False

    mask = _build_mask(image.shape[:2], polygons, padding=mask_padding)
    cleaned = cv2.inpaint(image, mask, inpaintRadius=inpaint_radius, flags=cv2.INPAINT_TELEA)
    cv2.imwrite(str(output_path), cleaned)
    return True


def clean_folder(input_dir: str | Path, output_dir: str | Path) -> dict:
    """Pulisce tutte le immagini di una cartella (non ricorsivo),
    salvandole con lo stesso nome nella cartella di output.

    Ritorna un riepilogo {"totale": N, "con_testo_rimosso": N, "senza_testo": N}.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    files = sorted(
        f for f in input_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    )

    summary = {"totale": 0, "con_testo_rimosso": 0, "senza_testo": 0}

    for f in files:
        summary["totale"] += 1
        out_path = output_dir / f.name
        try:
            had_text = clean_image(f, out_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Errore su {f}: {exc}")
            continue

        if had_text:
            summary["con_testo_rimosso"] += 1
            print(f"[OK] {f.name}: testo rimosso -> {out_path}")
        else:
            summary["senza_testo"] += 1
            print(f"[OK] {f.name}: nessun testo rilevato (copiata) -> {out_path}")

    return summary


def clean_tree(input_root: str | Path, output_root: str | Path) -> None:
    """Pulisce ricorsivamente tutte le sottocartelle (utile per la struttura
    download/<catalogo>/<prodotto>/*.jpg generata da yupoo_scraper.py)."""
    input_root = Path(input_root)
    output_root = Path(output_root)

    subdirs = [d for d in input_root.rglob("*") if d.is_dir()]
    subdirs.append(input_root)

    for d in subdirs:
        images = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS] if d.exists() else []
        if not images:
            continue
        rel = d.relative_to(input_root)
        out_dir = output_root / rel
        print(f"\n[INFO] Pulizia cartella: {d} -> {out_dir}")
        clean_folder(d, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rimuove le scritte cinesi dalle immagini prodotto (OCR + inpainting)."
    )
    parser.add_argument("input", help="Cartella con le immagini da pulire (ricerca ricorsiva)")
    parser.add_argument("--out", default="pulito", help="Cartella di output (default: ./pulito)")
    args = parser.parse_args()

    clean_tree(args.input, args.out)


if __name__ == "__main__":
    sys.exit(main() or 0)

