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
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from config import Config
from models import Category, Product, ProductImage, Subcategory, db

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
        _ensure_category_column(app)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "La richiesta supera il limite di caricamento."}), 413
        flash(
            "Il caricamento supera il limite per singola richiesta. "
            "Seleziona nuovamente le cartelle: verranno inviate a blocchi.",
            "error",
        )
        return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

    @app.context_processor
    def inject_site_name():
        categories = Category.query.order_by(Category.name.asc()).all()
        return {"site_name": app.config["SITE_NAME"], "site_categories": categories}

    # ------------------------------------------------------------------
    # Pagine pubbliche
    # ------------------------------------------------------------------
    @app.route("/")
    @app.route("/categoria/<category_slug>")
    @app.route("/categoria/<category_slug>/<subcategory_slug>")
    def index(category_slug=None, subcategory_slug=None):
        category = None
        subcategory = None
        products_query = Product.query

        if category_slug:
            category = Category.query.filter_by(slug=category_slug).first_or_404()
            products_query = products_query.filter_by(category_id=category.id)
            if subcategory_slug:
                subcategory = Subcategory.query.filter_by(
                    category_id=category.id, slug=subcategory_slug
                ).first_or_404()
                products_query = products_query.filter_by(subcategory_id=subcategory.id)

        products = products_query.order_by(Product.created_at.desc()).all()
        return render_template(
            "index.html",
            products=products,
            selected_category=category,
            selected_subcategory=subcategory,
        )

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
            category_id = request.form.get("category_id", type=int)
            subcategory_id = request.form.get("subcategory_id", type=int)
            append_images = request.form.get("append") == "1"
            files = _image_files(request.files.getlist("images"))

            if not name:
                flash("Inserisci il nome del prodotto.", "error")
            elif category_id is None:
                flash("Seleziona una categoria per il prodotto.", "error")
            elif not files:
                flash("Seleziona almeno un'immagine.", "error")
            else:
                category = db.session.get(Category, category_id)
                if category is None:
                    abort(400, "Categoria non valida.")
                subcategory = _validate_subcategory(category, subcategory_id)
                _create_or_update_product(
                    app,
                    name,
                    files,
                    category=category,
                    subcategory=subcategory,
                    replace_images=not append_images,
                )
                flash(f"Prodotto '{name}' pubblicato con successo.", "success")
                return redirect(url_for("admin_upload"))

        products = Product.query.order_by(Product.created_at.desc()).all()
        categories = Category.query.order_by(Category.name.asc()).all()
        return render_template(
            "admin.html",
            products=products,
            categories=categories,
            subcategory_count=sum(len(category.subcategories) for category in categories),
        )

    @app.route("/admin/categorie", methods=["POST"])
    @login_required
    def admin_create_category():
        name = request.form.get("name", "").strip()
        slug = _slugify(name)

        if not name:
            flash("Inserisci il nome della categoria.", "error")
        elif Category.query.filter_by(slug=slug).first():
            flash("Questa categoria esiste già.", "error")
        else:
            db.session.add(Category(name=name, slug=slug))
            db.session.commit()
            flash(f"Categoria '{name}' creata.", "success")

        return redirect(url_for("admin_upload", _anchor="categorie"))

    @app.route("/admin/sottocategorie", methods=["POST"])
    @login_required
    def admin_create_subcategory():
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", type=int)
        category = db.session.get(Category, category_id) if category_id else None

        if category is None:
            flash("Seleziona la categoria della sottocategoria.", "error")
        elif not name:
            flash("Inserisci il nome del modello.", "error")
        else:
            slug = _slugify(name)
            if Subcategory.query.filter_by(category_id=category.id, slug=slug).first():
                flash("Questo modello esiste già nella categoria selezionata.", "error")
            else:
                db.session.add(Subcategory(name=name, slug=slug, category=category))
                db.session.commit()
                flash(f"Modello '{name}' creato in '{category.name}'.", "success")
        return redirect(url_for("admin_upload", _anchor="sottocategorie"))

    @app.route("/admin/sottocategorie/<int:subcategory_id>/elimina", methods=["POST"])
    @login_required
    def admin_delete_subcategory(subcategory_id):
        subcategory = db.session.get(Subcategory, subcategory_id)
        if subcategory is None:
            abort(404)
        product_count = len(subcategory.products)
        for product in subcategory.products:
            product.subcategory = None
        db.session.delete(subcategory)
        db.session.commit()
        flash(
            f"Modello eliminato. {product_count} prodotti restano nella categoria principale.",
            "success",
        )
        return redirect(url_for("admin_upload", _anchor="sottocategorie"))

    @app.route("/admin/categorie/<int:category_id>/elimina", methods=["POST"])
    @login_required
    def admin_delete_category(category_id):
        category = db.session.get(Category, category_id)
        if category is None:
            abort(404)

        product_count = len(category.products)
        for product in category.products:
            product.category = None
            product.subcategory = None
        db.session.delete(category)
        db.session.commit()
        flash(
            f"Categoria eliminata. {product_count} prodotti sono ora senza categoria.",
            "success",
        )
        return redirect(url_for("admin_upload", _anchor="categorie"))

    @app.route("/admin/prodotti/<int:product_id>/categoria", methods=["POST"])
    @login_required
    def admin_assign_category(product_id):
        product = db.session.get(Product, product_id)
        if product is None:
            abort(404)

        category_id = request.form.get("category_id", type=int)
        subcategory_id = request.form.get("subcategory_id", type=int)
        category = db.session.get(Category, category_id) if category_id else None
        if category_id and category is None:
            abort(400, "Categoria non valida.")

        product.category = category
        product.subcategory = _validate_subcategory(category, subcategory_id) if category else None
        db.session.commit()
        if category:
            flash(f"'{product.name}' assegnato a '{category.name}'.", "success")
        else:
            flash(f"Categoria rimossa da '{product.name}'.", "success")
        return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))

    @app.route("/admin/importa-cartelle", methods=["POST"])
    @login_required
    def admin_import_folders():
        category_id = request.form.get("category_id", type=int)
        subcategory_id = request.form.get("subcategory_id", type=int)
        append_images = request.form.get("append") == "1"
        category = db.session.get(Category, category_id) if category_id else None
        if category is None:
            flash("Seleziona una categoria per importare le cartelle.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        subcategory = _validate_subcategory(category, subcategory_id)
        folders: dict[str, list] = {}
        for file in _image_files(request.files.getlist("folders")):
            folder_name = _folder_product_name(file.filename)
            folders.setdefault(folder_name, []).append(file)

        if not folders:
            flash("Seleziona almeno una cartella contenente immagini valide.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        imported = 0
        for folder_name, files in folders.items():
            _create_or_update_product(
                app,
                folder_name,
                files,
                category=category,
                subcategory=subcategory,
                replace_images=not append_images,
            )
            imported += 1

        flash(f"Importati automaticamente {imported} prodotti da {len(folders)} cartelle.", "success")
        return redirect(url_for("admin_upload"))

    @app.route("/admin/elimina/<int:product_id>", methods=["POST"])
    @login_required
    def admin_delete(product_id):
        product = Product.query.get_or_404(product_id)
        for image in product.images:
            _delete_uploaded_file(app, image.filename)
        db.session.delete(product)
        db.session.commit()
        _remove_empty_product_directory(app, product.slug)
        flash("Prodotto eliminato.", "success")
        return redirect(url_for("admin_upload"))

    @app.route("/admin/immagini/<int:image_id>/elimina", methods=["POST"])
    @login_required
    def admin_delete_image(image_id):
        image = ProductImage.query.get_or_404(image_id)
        product = image.product

        if len(product.images) <= 1:
            flash(
                "Non puoi eliminare l'ultima foto: elimina il prodotto completo oppure aggiungi prima un'altra immagine.",
                "error",
            )
            return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))

        _delete_uploaded_file(app, image.filename)
        product.images.remove(image)

        for position, remaining_image in enumerate(product.images, start=1):
            remaining_image.position = position

        db.session.commit()
        flash(f"Foto rimossa da '{product.name}'.", "success")
        return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))

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
        files = _image_files(request.files.getlist("images"))

        if not name:
            return jsonify({"error": "Campo 'name' mancante"}), 400
        if not files:
            return jsonify({"error": "Nessuna immagine inviata (campo 'images')"}), 400

        category_name = request.form.get("category", "").strip()
        subcategory_name = request.form.get("subcategory", "").strip()
        category = None
        if category_name:
            category_slug = _slugify(category_name)
            category = Category.query.filter_by(slug=category_slug).first()
            if category is None:
                category = Category(name=category_name, slug=category_slug)
                db.session.add(category)

        subcategory = None
        if subcategory_name:
            if category is None:
                return jsonify({"error": "La sottocategoria richiede una categoria"}), 400
            subcategory_slug = _slugify(subcategory_name)
            subcategory = Subcategory.query.filter_by(
                category_id=category.id, slug=subcategory_slug
            ).first()
            if subcategory is None:
                subcategory = Subcategory(
                    name=subcategory_name, slug=subcategory_slug, category=category
                )
                db.session.add(subcategory)

        product = _create_or_update_product(
            app,
            name,
            files,
            source_url=source_url,
            category=category,
            subcategory=subcategory,
        )

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


def _create_or_update_product(
    app,
    name: str,
    files,
    source_url: str | None = None,
    category: Category | None = None,
    subcategory: Subcategory | None = None,
    replace_images: bool = True,
) -> Product:
    """Crea un nuovo prodotto (o sostituisce le immagini di uno esistente
    con lo stesso nome) salvando i file ricevuti su disco."""
    slug = _slugify(name)

    product = Product.query.filter_by(slug=slug).first()
    if product is None:
        product = Product(
            name=name,
            slug=slug,
            source_url=source_url,
            category=category,
            subcategory=subcategory,
        )
        db.session.add(product)
    else:
        product.source_url = source_url or product.source_url
        if category is not None:
            product.category = category
        product.subcategory = subcategory
        if replace_images:
            # Rimuoviamo le immagini precedenti per sostituirle con quelle
            # nuove appena ricevute.
            for old_image in list(product.images):
                old_path = Path(app.config["UPLOAD_FOLDER"]) / old_image.filename
                old_path.unlink(missing_ok=True)
                db.session.delete(old_image)
            product.images = []

    db.session.flush()  # assicura che product.id sia disponibile

    product_dir = Path(app.config["UPLOAD_FOLDER"]) / slug
    product_dir.mkdir(parents=True, exist_ok=True)

    first_position = len(product.images) + 1
    for i, file in enumerate(files, start=first_position):
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


def _delete_uploaded_file(app, filename: str) -> None:
    upload_folder = Path(app.config["UPLOAD_FOLDER"]).resolve()
    file_path = (upload_folder / filename).resolve()

    if upload_folder not in file_path.parents:
        app.logger.error("Percorso immagine non valido nel database: %s", filename)
        return

    file_path.unlink(missing_ok=True)


def _remove_empty_product_directory(app, slug: str) -> None:
    product_dir = Path(app.config["UPLOAD_FOLDER"]) / slug
    if product_dir.is_dir() and not any(product_dir.iterdir()):
        product_dir.rmdir()


def _ensure_category_column(app) -> None:
    """Aggiorna i database esistenti creati prima dell'aggiunta delle categorie."""
    inspector = db.inspect(db.engine)
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    required_columns = {
        "category_id": "INTEGER",
        "subcategory_id": "INTEGER",
    }
    for column_name, column_type in required_columns.items():
        if column_name in product_columns:
            continue
        with db.engine.begin() as connection:
            connection.execute(
                db.text(f"ALTER TABLE products ADD COLUMN {column_name} {column_type}")
            )
        app.logger.info("Aggiunta colonna %s alla tabella products.", column_name)


def _validate_subcategory(
    category: Category | None, subcategory_id: int | None
) -> Subcategory | None:
    if subcategory_id is None:
        return None
    subcategory = db.session.get(Subcategory, subcategory_id)
    if subcategory is None or category is None or subcategory.category_id != category.id:
        abort(400, "Modello non valido per la categoria selezionata.")
    return subcategory


def _image_files(files) -> list:
    return [
        file
        for file in files
        if file and file.filename and Path(file.filename).suffix.lower() in ALLOWED_EXTENSIONS
    ]


def _folder_product_name(filename: str) -> str:
    normalized = filename.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part.lower() != "fakepath"]

    if not parts:
        return "prodotto"

    if len(parts) > 1 and re.fullmatch(r"[a-zA-Z]:", parts[0]):
        parts = parts[1:]

    folder_name = parts[-2] if len(parts) > 1 else Path(parts[0]).stem
    return folder_name.replace("_", " ").strip() or "prodotto"


if __name__ == "__main__":
    flask_app = create_app()
    flask_app.run(debug=True, host="0.0.0.0", port=5000)
