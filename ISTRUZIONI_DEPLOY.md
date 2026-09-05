# Guida Deploy su Render.com (Web Service gratuito)

## 1. Crea il servizio
- Su Render, dopo aver collegato GitHub: **New +** -> **Web Service**
- Seleziona questo repository

## 2. Impostazioni (se non usa automaticamente render.yaml)
| Campo             | Valore                          |
|--------------------|----------------------------------|
| Name               | catalogo-prodotti (o quello che vuoi: sarà parte dell'URL) |
| Region             | Frankfurt (o la più vicina)     |
| Branch             | main (o quello che usi)         |
| Root Directory     | `webapp`                        |
| Runtime            | Python 3 (etichetta generica: la versione esatta, 3.13, è fissata dal file `webapp/.python-version`, non è obsoleta) |
| Build Command      | `pip install -r requirements.txt` |
| Start Command      | `gunicorn wsgi:app`             |
| Instance Type      | Free                             |

## 3. Variabili d'ambiente (tab "Environment")
Aggiungi queste (stessi nomi del file webapp/.env, ma qui vanno su Render,
NON nel file .env che rimane solo per il tuo PC):

| Key              | Value                                  |
|-------------------|-----------------------------------------|
| SITE_NAME         | Il nome che vuoi dare al sito           |
| ADMIN_PASSWORD    | Una password a tua scelta               |
| API_KEY           | Una chiave lunga/complessa a tua scelta |
| SECRET_KEY        | Una stringa casuale lunga               |

⚠️ La API_KEY qui deve essere IDENTICA a quella che poi userai nello script
locale (`pubblica_da_yupoo.py` -> file `.env` nella root del progetto,
variabile `SITE_API_URL`/`API_KEY`), altrimenti l'upload automatico verrà
rifiutato con errore 401.

## 4. Deploy
Clicca **Create Web Service**. Render farà build + deploy automaticamente.
Al termine ti darà un link tipo:

    https://catalogo-prodotti.onrender.com

Quello è il link pubblico del tuo sito vetrina, raggiungibile da chiunque,
24 ore su 24, anche a PC spento.

## 5. IMPORTANTE: limite del piano gratuito (storage effimero)
Il piano Free di Render **non ha un disco persistente**: ogni volta che il
servizio si riavvia (va in "sleep" dopo 15 minuti di inattività e poi si
risveglia, oppure fai un nuovo deploy), la cartella `webapp/static/uploads/`
e il file `webapp/catalogo.db` vengono RESETTATI, cioè le immagini/prodotti
caricati tramite lo script vengono persi.

Per ora, finché testi il funzionamento, va benissimo così. Quando vorrai
rendere il sito definitivo, ti consiglio (come discusso all'inizio) di
aggiungere **Cloudinary** (storage immagini gratuito e persistente) e un
piccolo **Render Postgres free** (o Render Disk a pagamento, ~1$/mese) per
il database, così i prodotti restano salvati per sempre. Fammi sapere quando
vuoi che implementi questo passaggio.

## 6. Come pubblicare un prodotto dopo il deploy
Nel file `.env` nella ROOT del progetto (sul tuo PC), aggiungi/aggiorna:

    SITE_API_URL=https://catalogo-prodotti.onrender.com/api/prodotti
    API_KEY=la-stessa-chiave-messa-su-render

Poi lancia normalmente:

    python pubblica_da_yupoo.py

Lo script scaricherà da Yupoo, pulirà le immagini e le pubblicherà
automaticamente sul sito online.

