"""wsgi.py - Punto di ingresso per il server di produzione (gunicorn/Render).

Render (e gunicorn in generale) hanno bisogno di un oggetto Flask già creato
a livello di modulo, chiamato "app". app.py usa invece una funzione factory
create_app(), quindi qui la richiamiamo una sola volta e la esponiamo.

Comando di avvio da usare su Render (Start Command):
    gunicorn wsgi:app
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run()

