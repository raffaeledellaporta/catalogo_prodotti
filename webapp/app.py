"""app.py - Applicazione Flask del sito vetrina."""

import re
import unicodedata
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from config import Config
from models import Product, ProductImage, db

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    name = re.sub(r"[\s_-]+", "-", name).strip("-")
    return name or "prodotto"


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()

    @app.context_processor
    def inject_site_name():
        return {"site_name": app.config["SITE_NAME"]}

    # ------------------------------------------------------------------
    # Pagine pubbliche
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        products = Product.query.order_by(Product.created_at.desc()).all()
        return render_template("index.html", products=products)

    @app.route("/prodotto/<slug>")
    def product_detail(slug):
        product = Product.query.filter_by(slug=slug).first_or_404()
        return render_template("product.html", product=product)

    # ------------------------------------------------------------------
    # Area admin (upload manuale, protetta da password)
    # ------------------------------------------------------------------
    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("is_admin"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if password == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                next_url = request.args.get("next") or url_for("admin_upload")
                return redirect(next_url)
            flash("Password errata.", "error")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        return redirect(url_for("index"))

    @app.route("/admin", methods=["GET", "POST"])
    @login_required
    def admin_upload():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            files = [f for f in request.files.getlist("images") if f and f.filename]

            if not name:
                flash("Inserisci il nome del prodotto.", "error")
            elif not files:
                flash("Seleziona almeno un'immagine.", "error")
            else:
                _create_or_update_product(app, name, files)
                flash(f"Prodotto '{name}' pubblicato con successo.", "success")
                return redirect(url_for("admin_upload"))

        products = Product.query.order_by(Product.created_at.desc()).all()
        return render_template("admin.html", products=products)

    @app.route("/admin/elimina/<int:product_id>", methods=["POST"])
    @login_required
    def admin_delete(product_id):
        product = Product.query.get_or_404(product_id)
        db.session.delete(product)
        db.session.commit()
        flash("Prodotto eliminato.", "success")
        return redirect(url_for("admin_upload"))

    # ------------------------------------------------------------------
    # API per upload automatico dallo script locale (scraper + pulizia)
    # ------------------------------------------------------------------
    @app.route("/api/prodotti", methods=["POST"])
    def api_create_product():
        api_key = request.headers.get("X-API-KEY")
        if api_key != app.config["API_KEY"]:
            return jsonify({"error": "API key non valida"}), 401

        name = request.form.get("name", "").strip()
        source_url = request.form.get("source_url", "").strip() or None
        files = [f for f in request.files.getlist("images") if f and f.filename]

        if not name:
            return jsonify({"error": "Campo 'name' mancante"}), 400
        if not files:
            return jsonify({"error": "Nessuna immagine inviata (campo 'images')"}), 400

        product = _create_or_update_product(app, name, files, source_url=source_url)

        return jsonify(
            {
                "id": product.id,
                "name": product.name,
                "slug": product.slug,
                "url": url_for("product_detail", slug=product.slug, _external=True),
                "immagini": len(product.images),
            }
        ), 201

    return app


def _create_or_update_product(app, name: str, files, source_url: str | None = None) -> Product:
    """Crea un nuovo prodotto (o sostituisce le immagini di uno esistente
    con lo stesso nome) salvando i file ricevuti su disco."""
    slug = _slugify(name)

    product = Product.query.filter_by(slug=slug).first()
    if product is None:
        product = Product(name=name, slug=slug, source_url=source_url)
        db.session.add(product)
    else:
        product.source_url = source_url or product.source_url
        # Rimuoviamo le immagini precedenti (sia dal DB che dal disco) per
        # sostituirle con quelle nuove appena ricevute.
        for old_image in list(product.images):
            old_path = Path(app.config["UPLOAD_FOLDER"]) / old_image.filename
            old_path.unlink(missing_ok=True)
            db.session.delete(old_image)
        product.images = []

    db.session.flush()  # assicura che product.id sia disponibile

    product_dir = Path(app.config["UPLOAD_FOLDER"]) / slug
    product_dir.mkdir(parents=True, exist_ok=True)

    for i, file in enumerate(files, start=1):
        ext = Path(secure_filename(file.filename)).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            ext = ".jpg"
        filename = f"{i:03d}{ext}"
        file.save(product_dir / filename)

        db.session.add(
            ProductImage(
                product=product,
                filename=f"{slug}/{filename}",
                position=i,
            )
        )

    db.session.commit()
    return product


if __name__ == "__main__":
    flask_app = create_app()
    flask_app.run(debug=True, host="0.0.0.0", port=5000)

