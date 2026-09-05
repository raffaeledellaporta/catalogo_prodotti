"""wsgi.py - Punto di ingresso per il server di produzione (gunicorn/Render).

Render (e gunicorn in generale) hanno bisogno di un oggetto Flask già creato
a livello di modulo, chiamato "app". app.py usa invece una funzione factory
create_app(), quindi qui la richiamiamo una sola volta e la esponiamo.

Comando di avvio da usare su Render (Start Command):
    gunicorn wsgi:app

AVVIO "TUTTO IN UNO" IN LOCALE (PyCharm: click su Run su questo file):
Se lanci questo file direttamente (non tramite gunicorn), il blocco
"if __name__ == '__main__'" qui sotto:
  1. Avvia il sito Flask in locale, in un thread in background
     (http://127.0.0.1:5000)
  2. Subito dopo, avvia nello stesso processo il loop interattivo di
     pubblica_da_yupoo.py, dove puoi incollare i link Yupoo uno alla volta

Non serve più aprire due terminali separati: un solo "Run" avvia entrambi.
Su Render questo blocco non viene mai eseguito (gunicorn importa solo
l'oggetto "app" sopra), quindi il deploy online non cambia comportamento.
"""

import sys
import threading
import time
from pathlib import Path

from app import create_app

app = create_app()


def _run_flask_in_background(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Avvia il server di sviluppo Flask in un thread separato (daemon),
    così il processo principale resta libero per il loop interattivo."""
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Rende importabili yupoo_scraper.py / image_cleaner.py / pubblica_da_yupoo.py,
    # che si trovano nella cartella superiore (root del progetto), non in webapp/.
    ROOT_DIR = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT_DIR))

    print("=" * 70)
    print("Avvio sito vetrina in locale su http://127.0.0.1:5000 ...")
    print("=" * 70)

    server_thread = threading.Thread(target=_run_flask_in_background, daemon=True)
    server_thread.start()
    time.sleep(1.5)  # piccola attesa per essere sicuri che il server sia pronto

    import pubblica_da_yupoo as pdy

    pdy._interactive_loop()


