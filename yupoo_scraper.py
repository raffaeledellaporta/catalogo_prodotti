"""
yupoo_scraper.py
================

Scraper per cataloghi Yupoo.

Per ogni prodotto (album) trovato, scarica TUTTE le foto di quel prodotto
(le diverse angolazioni), ognuna alla risoluzione ORIGINALE più alta
disponibile (attributo `data-origin-src` delle pagine Yupoo).

Funziona sia con:
  - link di RICERCA/CATALOGO Yupoo, che elencano più album/prodotti,
    con eventuale paginazione (es.
    https://ustradeshoes2020.x.yupoo.com/search/album?uid=1&sort=unix&q=Jordan)
  - link diretto a un SINGOLO album/prodotto Yupoo
    (es. https://ustradeshoes2020.x.yupoo.com/albums/243194703?uid=1)

Uso interattivo (consigliato):
    python yupoo_scraper.py
    Poi incolla un link Yupoo quando richiesto. Al termine del download puoi
    incollarne un altro, e così via. Scrivi "exit" (o "quit"/"q") per uscire.

Uso non interattivo (uno o più link come argomenti):
    python yupoo_scraper.py <link1> <link2> ...

Struttura di output:
    download/<catalogo>/<nome_prodotto>/001.jpg, 002.jpg, ...
(per un link diretto a un singolo album, la cartella <catalogo> viene omessa)

Note tecniche su Yupoo (verificate su un catalogo reale)
----------------------------------------------------------
- Le pagine di ricerca/catalogo elencano gli album con link tipo
  `/albums/<id>?uid=...`. La paginazione è basata su `&page=N`, con il
  numero totale di pagine indicato nel testo "共X页" della pagina.
- Nella pagina di un album, ogni foto è un tag <img> con attributi:
    data-origin-src  -> URL dell'immagine ORIGINALE (risoluzione più alta)
    data-src         -> URL versione "big" (media risoluzione)
    src              -> URL versione "small"/"medium" (thumbnail)
  Si scarica sempre `data-origin-src` quando presente (fallback su
  data-src, poi su src).
- Il nome del prodotto si trova nell'attributo `data-name` dello span con
  classe `showalbumheader__gallerytitle`.
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup

# Disabilitiamo il warning di "unverified HTTPS" che stampiamo comunque noi
# in modo controllato (vedi _request_get). Necessario in reti aziendali con
# proxy/certificati self-signed.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_DELAY_SECONDS = 0.6  # pausa tra una richiesta e l'altra, per non martellare il server


@dataclass
class Photo:
    url: str
    index: int


@dataclass
class Product:
    name: str
    album_url: str
    photos: list[Photo] = field(default_factory=list)
    saved_files: list[Path] = field(default_factory=list)


def _slugify(name: str) -> str:
    """Rende il nome del catalogo/prodotto un nome di file/cartella sicuro."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\s-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    name = name.strip("_")
    return name or "prodotto"


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.verify = True  # verrà messo a False automaticamente se serve (vedi _request_get)
    return s


def _request_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET con fallback automatico su verify=False in caso di errore SSL
    (tipico di reti aziendali con proxy/certificati self-signed).

    Il risultato del primo tentativo viene "ricordato" sulla sessione
    (session.verify), così le richieste successive non perdono tempo a
    ritentare due volte ogni singola chiamata: è quello che rendeva lo
    scraping di un intero catalogo (centinaia di richieste) molto lento.
    """
    kwargs.setdefault("verify", session.verify)
    try:
        resp = session.get(url, timeout=25, **kwargs)
    except requests.exceptions.SSLError:
        if session.verify:
            print(
                "[INFO] Verifica SSL fallita: disattivo la verifica del certificato "
                "per il resto della sessione (rete con proxy/certificato aziendale)."
            )
            session.verify = False
        kwargs["verify"] = False
        resp = session.get(url, timeout=25, **kwargs)
    resp.raise_for_status()
    return resp


def _get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    resp = _request_get(session, url)
    return BeautifulSoup(resp.text, "html.parser")


def _album_id(url: str) -> str | None:
    m = re.search(r"/albums/(\d+)", url)
    return m.group(1) if m else None


def _is_album_page(soup: BeautifulSoup) -> bool:
    return soup.select_one(".showalbumheader__gallerytitle") is not None


def _album_title(soup: BeautifulSoup, fallback: str = "prodotto_senza_nome") -> str:
    title_span = soup.select_one(".showalbumheader__gallerytitle")
    if title_span is not None:
        name = title_span.get("data-name") or title_span.get_text(strip=True)
        if name:
            return name.strip()

    title_tag = soup.find("title")
    if title_tag is not None:
        text = title_tag.get_text(strip=True)
        if text:
            # Il <title> Yupoo è tipo "Nome prodotto | 相册 | Yupoo ..."
            return text.split("|")[0].strip() or fallback

    return fallback


def _find_album_links_on_page(soup: BeautifulSoup, base_url: str) -> list[str]:
    links = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/albums/" not in href:
            continue
        full_url = urljoin(base_url, href)
        album_id = _album_id(full_url)
        if album_id and album_id not in seen_ids:
            seen_ids.add(album_id)
            links.append(full_url)
    return links


def _total_pages(soup: BeautifulSoup) -> int:
    """Cerca il testo tipo '共9页' per determinare il numero totale di pagine."""
    text = soup.get_text()
    m = re.search(r"共\s*(\d+)\s*页", text)
    if m:
        return int(m.group(1))
    return 1


def _with_page_param(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    new_query = "&".join(f"{k}={v[0]}" for k, v in query.items())
    return parsed._replace(query=new_query).geturl()


def collect_album_urls(session: requests.Session, listing_url: str) -> list[str]:
    """Data una pagina di ricerca/catalogo Yupoo, raccoglie tutti gli URL
    degli album (prodotti), gestendo la paginazione."""
    first_soup = _get_soup(session, listing_url)

    if _is_album_page(first_soup):
        # Il link fornito è già la pagina di un singolo album/prodotto.
        return [listing_url]

    all_links: list[str] = []
    seen_ids: set[str] = set()

    def add_links(soup: BeautifulSoup, url: str) -> None:
        for link in _find_album_links_on_page(soup, url):
            album_id = _album_id(link)
            if album_id not in seen_ids:
                seen_ids.add(album_id)
                all_links.append(link)

    add_links(first_soup, listing_url)
    total_pages = _total_pages(first_soup)

    for page in range(2, total_pages + 1):
        page_url = _with_page_param(listing_url, page)
        print(f"[INFO] Scarico pagina elenco {page}/{total_pages}...")
        time.sleep(REQUEST_DELAY_SECONDS)
        try:
            page_soup = _get_soup(session, page_url)
        except requests.RequestException as exc:
            print(f"[WARN] Impossibile leggere la pagina {page}: {exc}")
            continue
        add_links(page_soup, page_url)

    return all_links


def _extract_photos(soup: BeautifulSoup) -> list[str]:
    """Estrae, in ordine, gli URL a risoluzione originale (più alta) di
    tutte le foto (angolazioni) del prodotto, deduplicando per URL.

    Le foto reali del prodotto sono i tag <img> con l'attributo
    `data-origin-src` (presente solo sulle immagini della galleria
    principale, non su loghi/icone del sito né sulle miniature di
    navigazione, che infatti non hanno questo attributo).
    """
    urls: list[str] = []
    seen = set()

    for img in soup.select("img[data-origin-src]"):
        best = img.get("data-origin-src") or img.get("data-src") or img.get("src")
        if not best:
            continue
        if best.startswith("//"):
            best = "https:" + best
        if best in seen:
            continue
        seen.add(best)
        urls.append(best)

    return urls


def get_album_product(session: requests.Session, album_url: str) -> Product:
    soup = _get_soup(session, album_url)
    name = _album_title(soup)
    photo_urls = _extract_photos(soup)
    photos = [Photo(url=u, index=i + 1) for i, u in enumerate(photo_urls)]
    return Product(name=name, album_url=album_url, photos=photos)


def _guess_extension(url: str, content_type: str | None = None) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix
    if ext and len(ext) <= 5:
        return ext
    if content_type:
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
    return ".jpg"


def download_product_photos(
    session: requests.Session, product: Product, dest_dir: Path, skip_existing: bool = True
) -> Product:
    dest_dir.mkdir(parents=True, exist_ok=True)

    if skip_existing and any(dest_dir.iterdir()):
        print(f"[SKIP] '{product.name}' -> cartella già presente e non vuota: {dest_dir}")
        return product

    for photo in product.photos:
        try:
            # Yupoo protegge le immagini con controllo del Referer: senza
            # questo header le richieste dirette falliscono (errore 567).
            resp = _request_get(
                session, photo.url, headers={"Referer": product.album_url}
            )
        except requests.RequestException as exc:
            print(f"[WARN] Download fallito ({photo.url}): {exc}")
            continue

        ext = _guess_extension(photo.url, resp.headers.get("Content-Type"))
        file_path = dest_dir / f"{photo.index:03d}{ext}"
        file_path.write_bytes(resp.content)
        product.saved_files.append(file_path)
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"[OK] '{product.name}' -> {len(product.saved_files)} foto salvate in {dest_dir}")
    return product


def _listing_folder_name(session: requests.Session, listing_url: str) -> str | None:
    """Nome di cartella da usare per raggruppare i prodotti di un catalogo/
    ricerca. Ritorna None se il link è già un singolo album (nessun
    raggruppamento necessario)."""
    parsed = urlparse(listing_url)
    query = parse_qs(parsed.query)
    shop = parsed.netloc.split(".")[0]

    if "q" in query:
        return _slugify(f"{shop}_{query['q'][0]}")

    # Prova a usare il titolo della pagina come nome catalogo
    try:
        soup = _get_soup(session, listing_url)
    except requests.RequestException:
        return _slugify(shop)

    if _is_album_page(soup):
        return None

    title_tag = soup.find("title")
    if title_tag is not None and title_tag.get_text(strip=True):
        return _slugify(title_tag.get_text(strip=True).split("|")[0])

    return _slugify(shop)


def scrape_yupoo_link(yupoo_url: str, output_dir: str = "download") -> list[Product]:
    """Scarica, per ogni prodotto (album) trovato a partire dal link
    fornito, TUTTE le sue foto (diverse angolazioni) alla risoluzione
    originale più alta."""
    session = _make_session()

    print(f"[INFO] Analizzo link: {yupoo_url}")
    album_urls = collect_album_urls(session, yupoo_url)

    if not album_urls:
        print("[WARN] Nessun album/prodotto trovato per questo link.")
        return []

    is_single_album = len(album_urls) == 1 and _album_id(yupoo_url) == _album_id(album_urls[0])
    catalog_folder = None if is_single_album else _listing_folder_name(session, yupoo_url)

    base_out = Path(output_dir)
    if catalog_folder:
        base_out = base_out / catalog_folder

    print(f"[INFO] Trovati {len(album_urls)} prodotto/i. Output in: {base_out}")

    products: list[Product] = []
    for i, album_url in enumerate(album_urls, start=1):
        print(f"\n[{i}/{len(album_urls)}] Apro album: {album_url}")
        time.sleep(REQUEST_DELAY_SECONDS)
        try:
            product = get_album_product(session, album_url)
        except requests.RequestException as exc:
            print(f"[WARN] Impossibile aprire l'album {album_url}: {exc}")
            continue

        if not product.photos:
            print(f"[WARN] Nessuna foto trovata per '{product.name}', salto.")
            continue

        dest_dir = base_out / _slugify(product.name)
        download_product_photos(session, product, dest_dir)
        products.append(product)

    total_photos = sum(len(p.saved_files) for p in products)
    print(
        f"\n[RIEPILOGO] {len(products)} prodotto/i elaborato/i, "
        f"{total_photos} foto scaricate in totale."
    )
    return products


def _interactive_loop(output_dir: str) -> None:
    print("=" * 70)
    print("Yupoo scraper - modalità interattiva")
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
            scrape_yupoo_link(link, output_dir=output_dir)
        except requests.RequestException as exc:
            print(f"[ERRORE] Impossibile scaricare il link: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERRORE] Errore inatteso: {exc}")


def main() -> None:
    args = sys.argv[1:]
    output_dir = "download"

    # Consente anche: python yupoo_scraper.py --out cartella link1 link2 ...
    if "--out" in args:
        idx = args.index("--out")
        output_dir = args[idx + 1]
        del args[idx : idx + 2]

    if args:
        # Uno o più link passati da riga di comando: li elaboro e poi
        # entro comunque in modalità interattiva per eventuali altri link.
        for link in args:
            scrape_yupoo_link(link, output_dir=output_dir)

    _interactive_loop(output_dir)


if __name__ == "__main__":
    main()

