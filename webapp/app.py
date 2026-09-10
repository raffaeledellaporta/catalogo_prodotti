"""app.py - Applicazione Flask del sito vetrina."""

import re
import unicodedata
from uuid import uuid4
from functools import wraps
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import safe_join, secure_filename

from config import Config
from models import Category, Product, ProductAlbum, ProductImage, Subcategory, db

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
THUMBNAIL_SIZES = {640, 1280}
WEBP_MAX_DIMENSION = 1_600
WEBP_QUALITY = 82
TITLE_CODE_SUFFIX = re.compile(
    r"\s*(?:style\s*)?code(?:\s*[:#-]?\s*[A-Za-z0-9]+(?:-[A-Za-z0-9]*)*)?\s*$",
    re.IGNORECASE,
)
TRAILING_SKU = re.compile(
    r"\s+(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z]{1,4}\d+(?:-[A-Za-z0-9]*)+\s*$",
    re.IGNORECASE,
)


def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    name = re.sub(r"[\s_-]+", "-", name).strip("-")
    return name or "prodotto"


def _display_name(name: str) -> str:
    """Normalizza i nomi mostrati sul sito senza alterare gli slug."""
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def _clean_product_title(name: str) -> str:
    """Rimuove dal titolo il suffisso con lo Style Code del prodotto."""
    title = _display_name(name)
    title = TITLE_CODE_SUFFIX.sub("", title)
    title = TRAILING_SKU.sub("", title)
    return _display_name(title)


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_database_columns(app)
        _normalize_existing_product_titles()
        _convert_existing_images_to_webp(app)

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
        categories = (
            Category.query.options(
                selectinload(Category.products),
                selectinload(Category.subcategories).selectinload(Subcategory.products),
            )
            .order_by(Category.name.asc())
            .all()
        )
        return {"site_name": app.config["SITE_NAME"], "site_categories": categories}

    @app.template_global()
    def thumbnail_url(filename: str, size: int = 640) -> str:
        return url_for("thumbnail", size=size, filename=filename)

    # ------------------------------------------------------------------
    # Pagine pubbliche
    # ------------------------------------------------------------------
    @app.route("/")
    @app.route("/categoria/<category_slug>")
    @app.route("/categoria/<category_slug>/<subcategory_slug>")
    def index(category_slug=None, subcategory_slug=None):
        category = None
        subcategory = None
        products_query = Product.query.options(
            joinedload(Product.category),
            joinedload(Product.subcategory),
            selectinload(Product.albums).selectinload(ProductAlbum.images),
            selectinload(Product.images),
        ).filter(Product.is_model.is_(True))
        search_query = request.args.get("q", "").strip()
        page = max(request.args.get("page", 1, type=int), 1)

        if category_slug:
            category = Category.query.filter_by(slug=category_slug).first_or_404()
            products_query = products_query.filter_by(category_id=category.id)
            if subcategory_slug:
                subcategory = Subcategory.query.filter_by(
                    category_id=category.id, slug=subcategory_slug
                ).first_or_404()
                products_query = products_query.filter_by(subcategory_id=subcategory.id)

        if search_query:
            products_query = products_query.filter(Product.name.ilike(f"%{search_query}%"))

        show_category_overview = (
            category is None
            and not search_query
            and Category.query.first() is not None
        )
        products = None
        if not show_category_overview:
            products = products_query.order_by(Product.created_at.desc()).paginate(
                page=page,
                per_page=app.config["PRODUCTS_PER_PAGE"],
                error_out=False,
            )
        unmodeled_products = []
        if subcategory:
            unmodeled_products = (
                Product.query.options(
                    selectinload(Product.albums).selectinload(ProductAlbum.images),
                    selectinload(Product.images),
                )
                .filter_by(
                    category_id=category.id,
                    subcategory_id=subcategory.id,
                    is_model=False,
                )
                .all()
            )
        return render_template(
            "index.html",
            products=products,
            unmodeled_products=unmodeled_products,
            search_query=search_query,
            selected_category=category,
            selected_subcategory=subcategory,
        )

    @app.route("/prodotto/<slug>")
    def product_detail(slug):
        product = (
            Product.query.options(
                joinedload(Product.category),
                joinedload(Product.subcategory),
                selectinload(Product.albums).selectinload(ProductAlbum.images),
                selectinload(Product.images),
            )
            .filter_by(slug=slug, is_model=True)
            .first_or_404()
        )
        return render_template("product.html", product=product)

    @app.route("/media/anteprima/<int:size>/<path:filename>")
    def thumbnail(size, filename):
        if size not in THUMBNAIL_SIZES:
            abort(404)
        return _thumbnail_response(app, filename, size)

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

        products = (
            Product.query.filter(Product.is_model.is_(True))
            .order_by(Product.created_at.desc())
            .all()
        )
        photo_containers = (
            Product.query.filter(Product.is_model.is_(False))
            .order_by(Product.created_at.desc())
            .all()
        )
        photo_container_image_counts = {
            product.id: ProductImage.query.filter_by(product_id=product.id).count()
            for product in photo_containers
        }
        categories = Category.query.order_by(Category.name.asc()).all()
        return render_template(
            "admin.html",
            products=products,
            photo_containers=photo_containers,
            photo_container_image_counts=photo_container_image_counts,
            categories=categories,
            subcategory_count=sum(len(category.subcategories) for category in categories),
        )

    @app.route("/admin/categorie", methods=["POST"])
    @login_required
    def admin_create_category():
        name = request.form.get("name", "").strip()
        slug = _slugify(name)
        direct_subcategory_access = (
            request.form.get("direct_subcategory_access") == "1"
        )

        if not name:
            flash("Inserisci il nome della categoria.", "error")
        elif Category.query.filter_by(slug=slug).first():
            flash("Questa categoria esiste già.", "error")
        else:
            db.session.add(
                Category(
                    name=name,
                    slug=slug,
                    direct_subcategory_access=direct_subcategory_access,
                )
            )
            db.session.commit()
            flash(f"Categoria '{name}' creata.", "success")

        return redirect(url_for("admin_upload", _anchor="categorie"))

    @app.route("/admin/categorie/<int:category_id>", methods=["POST"])
    @login_required
    def admin_rename_category(category_id):
        category = db.session.get(Category, category_id)
        if category is None:
            abort(404)

        name = _display_name(request.form.get("name", ""))
        slug = _slugify(name)
        direct_subcategory_access = (
            request.form.get("direct_subcategory_access") == "1"
        )
        duplicate = Category.query.filter(
            Category.slug == slug, Category.id != category.id
        ).first()
        if not name:
            flash("Inserisci il nome della macro-categoria.", "error")
        elif duplicate:
            flash("Esiste già una macro-categoria con questo nome.", "error")
        else:
            category.name = name
            category.slug = slug
            category.direct_subcategory_access = direct_subcategory_access
            db.session.commit()
            flash("Macro-categoria aggiornata.", "success")

        return redirect(url_for("admin_upload", _anchor="categorie"))

    @app.route("/admin/modelli", methods=["POST"])
    @login_required
    def admin_create_model():
        name = _clean_product_title(request.form.get("name", ""))
        category_id = request.form.get("category_id", type=int)
        subcategory_id = request.form.get("subcategory_id", type=int)
        category = db.session.get(Category, category_id) if category_id else None

        if category is None:
            flash("Seleziona la macro-categoria del modello.", "error")
        elif not name:
            flash("Inserisci il nome del modello.", "error")
        else:
            subcategory = _validate_subcategory(category, subcategory_id)
            if subcategory is None:
                flash("Seleziona la micro-categoria del modello.", "error")
            else:
                slug = _slugify(name)
                if Product.query.filter_by(slug=slug).first():
                    flash("Esiste già un modello con questo nome.", "error")
                else:
                    db.session.add(
                        Product(
                            name=name,
                            slug=slug,
                            category=category,
                            subcategory=subcategory,
                        )
                    )
                    db.session.commit()
                    flash(
                        f"Modello '{name}' creato in '{category.name} → {subcategory.name}'.",
                        "success",
                    )
        return redirect(url_for("admin_upload", _anchor="modelli"))

    @app.route("/admin/sottocategorie", methods=["POST"])
    @login_required
    def admin_create_subcategory():
        name = request.form.get("name", "").strip()
        category_id = request.form.get("category_id", type=int)
        category = db.session.get(Category, category_id) if category_id else None

        if category is None:
            flash("Seleziona la categoria della sottocategoria.", "error")
        elif not name:
            flash("Inserisci il nome della micro-categoria.", "error")
        else:
            slug = _slugify(name)
            if Subcategory.query.filter_by(category_id=category.id, slug=slug).first():
                flash("Questa micro-categoria esiste già nella macro-categoria selezionata.", "error")
            else:
                db.session.add(Subcategory(name=name, slug=slug, category=category))
                db.session.commit()
                flash(f"Micro-categoria '{name}' creata in '{category.name}'.", "success")
        return redirect(url_for("admin_upload", _anchor="sottocategorie"))

    @app.route("/admin/sottocategorie/<int:subcategory_id>", methods=["POST"])
    @login_required
    def admin_rename_subcategory(subcategory_id):
        subcategory = db.session.get(Subcategory, subcategory_id)
        if subcategory is None:
            abort(404)

        name = _display_name(request.form.get("name", ""))
        slug = _slugify(name)
        duplicate = Subcategory.query.filter(
            Subcategory.category_id == subcategory.category_id,
            Subcategory.slug == slug,
            Subcategory.id != subcategory.id,
        ).first()
        if not name:
            flash("Inserisci il nome della micro-categoria.", "error")
        elif duplicate:
            flash(
                "Esiste già una micro-categoria con questo nome nella macro-categoria scelta.",
                "error",
            )
        else:
            subcategory.name = name
            subcategory.slug = slug
            db.session.commit()
            flash("Micro-categoria rinominata.", "success")

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
            f"Micro-categoria eliminata. {product_count} prodotti restano nella macro-categoria.",
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
            destination = category.name
            if product.subcategory:
                destination = f"{destination} → {product.subcategory.name}"
            flash(f"'{product.name}' spostato in '{destination}'.", "success")
        else:
            flash(f"Categoria rimossa da '{product.name}'.", "success")
        return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))

    @app.route("/admin/prodotti/<int:product_id>", methods=["POST"])
    @login_required
    def admin_rename_product(product_id):
        product = db.session.get(Product, product_id)
        if product is None:
            abort(404)

        name = _clean_product_title(request.form.get("name", ""))
        slug = _slugify(name)
        duplicate = Product.query.filter(
            Product.slug == slug, Product.id != product.id
        ).first()
        if not name:
            flash("Inserisci il nome del modello.", "error")
        elif duplicate:
            flash("Esiste già un modello con questo nome.", "error")
        else:
            product.name = name
            product.slug = slug
            db.session.commit()
            flash("Modello rinominato.", "success")

        return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))

    @app.route("/admin/prodotti/<int:product_id>/rimuovi-modello", methods=["POST"])
    @login_required
    def admin_remove_model(product_id):
        product = (
            Product.query.filter(
                Product.id == product_id, Product.is_model.is_(True)
            ).first_or_404()
        )
        if product.subcategory is None:
            flash(
                "Assegna prima il modello a una micro-categoria per conservarne le foto.",
                "error",
            )
        else:
            product.is_model = False
            db.session.commit()
            flash(
                f"Il modello non è stato eliminato: le foto restano nella micro-categoria '{product.subcategory.name}'.",
                "success",
            )
        return redirect(url_for("admin_upload", _anchor="modelli"))

    @app.route("/admin/importa-cartelle", methods=["POST"])
    @login_required
    def admin_import_folders():
        category_id = request.form.get("category_id", type=int)
        subcategory_id = request.form.get("subcategory_id", type=int)
        model_id = request.form.get("model_id", type=int)
        category = db.session.get(Category, category_id) if category_id else None
        if category is None:
            flash("Seleziona una macro-categoria per importare le immagini.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        subcategory = _validate_subcategory(category, subcategory_id)
        if subcategory is None:
            flash("Seleziona una micro-categoria per importare le immagini.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        model = db.session.get(Product, model_id) if model_id else None
        if model_id and (
            model is None
            or not model.is_model
            or model.category_id != category.id
            or model.subcategory_id != subcategory.id
        ):
            flash("Seleziona un modello della micro-categoria scelta.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))
        if model is None:
            model = _get_or_create_photo_container(category, subcategory)

        files = _image_files(request.files.getlist("folders"))
        if not files:
            flash("Seleziona almeno una cartella contenente immagini valide.", "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        try:
            files_by_folder = _group_files_by_parent_folder(files)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("admin_upload", _anchor="importa-cartelle"))

        for folder_name, folder_files in files_by_folder.items():
            _create_or_update_album(
                app,
                model,
                folder_name,
                folder_files,
            )
        flash(
            f"Importate {len(files_by_folder)} cartelle e {len(files)} immagini in '{model.name}'. "
            "Le cartelle con lo stesso nome sono state unite nello stesso album.",
            "success",
        )
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
        flash("Modello e relative foto eliminati definitivamente.", "success")
        return redirect(url_for("admin_upload"))

    @app.route("/admin/prodotti/elimina", methods=["POST"])
    @login_required
    def admin_delete_selected_products():
        selected_ids = set(request.form.getlist("product_ids", type=int))
        if not selected_ids:
            flash("Seleziona almeno un modello da eliminare.", "error")
            return redirect(url_for("admin_upload", _anchor="modelli"))

        products = Product.query.filter(Product.id.in_(selected_ids)).all()
        if len(products) != len(selected_ids):
            abort(400, "I modelli selezionati non sono validi.")

        product_slugs = []
        for product in products:
            for image in product.images:
                _delete_uploaded_file(app, image.filename)
            product_slugs.append(product.slug)
            db.session.delete(product)

        db.session.commit()
        for slug in product_slugs:
            _remove_empty_product_directory(app, slug)
        flash(f"Eliminati {len(products)} modelli selezionati.", "success")
        return redirect(url_for("admin_upload", _anchor="modelli"))

    @app.route("/admin/prodotti/unisci", methods=["POST"])
    @login_required
    def admin_merge_products():
        destination_id = request.form.get("destination_id", type=int)
        source_ids = set(request.form.getlist("source_ids", type=int))
        if destination_id is None:
            flash("Seleziona il modello in cui unire le foto.", "error")
            return redirect(url_for("admin_upload", _anchor="modelli"))
        if not source_ids:
            flash("Seleziona almeno un modello da unire.", "error")
            return redirect(url_for("admin_upload", _anchor="modelli"))
        if destination_id in source_ids:
            flash("Il modello di destinazione non può essere incluso tra quelli da unire.", "error")
            return redirect(url_for("admin_upload", _anchor="modelli"))

        destination = Product.query.filter(
            Product.id == destination_id, Product.is_model.is_(True)
        ).first()
        sources = Product.query.filter(
            Product.id.in_(source_ids), Product.is_model.is_(True)
        ).all()
        if destination is None or len(sources) != len(source_ids):
            abort(400, "I modelli selezionati non sono validi.")

        for source in sources:
            _merge_product_into(destination, source)

        db.session.commit()
        flash(
            f"Uniti {len(sources)} modelli in '{destination.name}'. Tutte le foto e gallerie sono state conservate.",
            "success",
        )
        return redirect(url_for("admin_upload", _anchor=f"product-{destination.id}"))

    @app.route("/admin/immagini/<int:image_id>/elimina", methods=["POST"])
    @login_required
    def admin_delete_image(image_id):
        image = ProductImage.query.get_or_404(image_id)
        product = image.product
        page = max(request.form.get("page", 1, type=int), 1)

        if len(product.images) <= 1:
            flash(
                "Non puoi eliminare l'ultima foto: elimina tutte le foto oppure aggiungi prima un'altra immagine.",
                "error",
            )
            return _admin_product_redirect(product, page)

        _delete_uploaded_file(app, image.filename)
        product.images.remove(image)

        for position, remaining_image in enumerate(product.images, start=1):
            remaining_image.position = position

        db.session.commit()
        flash(f"Foto rimossa da '{product.name}'.", "success")
        return _admin_product_redirect(product, page)

    @app.route("/admin/prodotti/<int:product_id>/immagini/elimina", methods=["POST"])
    @login_required
    def admin_delete_selected_images(product_id):
        product = Product.query.get_or_404(product_id)
        selected_ids = set(request.form.getlist("image_ids", type=int))
        page = max(request.form.get("page", 1, type=int), 1)
        if not selected_ids:
            flash("Seleziona almeno una foto da eliminare.", "error")
            return _admin_product_redirect(product, page)

        selected_images = [
            image for image in product.images if image.id in selected_ids
        ]
        if len(selected_images) != len(selected_ids):
            abort(400, "Le foto selezionate non appartengono a questo modello.")
        if len(selected_images) >= len(product.images):
            flash(
                "Devi lasciare almeno una foto. Per eliminarle tutte usa 'Elimina tutte le foto'.",
                "error",
            )
            return _admin_product_redirect(product, page)

        selected_album_ids = {image.album_id for image in selected_images if image.album_id}
        for image in selected_images:
            _delete_uploaded_file(app, image.filename)
            db.session.delete(image)

        for position, image in enumerate(
            (image for image in product.images if image.id not in selected_ids),
            start=1,
        ):
            image.position = position

        db.session.flush()
        for album_id in selected_album_ids:
            if not ProductImage.query.filter_by(album_id=album_id).first():
                album = db.session.get(ProductAlbum, album_id)
                if album is not None:
                    db.session.delete(album)

        db.session.commit()
        flash(f"Eliminate {len(selected_images)} foto da '{product.name}'.", "success")
        return _admin_product_redirect(product, page)

    @app.route("/admin/foto-senza-modello/<int:product_id>")
    @login_required
    def admin_manage_unmodeled_photos(product_id):
        product = (
            Product.query.options(joinedload(Product.category), joinedload(Product.subcategory))
            .filter(Product.id == product_id, Product.is_model.is_(False))
            .first_or_404()
        )
        page = max(request.args.get("page", 1, type=int), 1)
        images = (
            ProductImage.query.options(joinedload(ProductImage.album))
            .filter_by(product_id=product.id)
            .order_by(ProductImage.position.asc(), ProductImage.id.asc())
            .paginate(page=page, per_page=100, error_out=False)
        )
        return render_template(
            "admin_photo_container.html",
            product=product,
            images=images,
        )

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
    name = _clean_product_title(name)
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
                _delete_uploaded_file(app, old_image.filename)
                db.session.delete(old_image)
            product.images = []

    db.session.flush()  # assicura che product.id sia disponibile

    product_dir = Path(app.config["UPLOAD_FOLDER"]) / slug
    product_dir.mkdir(parents=True, exist_ok=True)

    first_position = len(product.images) + 1
    for i, file in enumerate(files, start=first_position):
        filename = f"{i:03d}.webp"
        _save_uploaded_image_as_webp(file, product_dir / filename)

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
    relative_path = file_path.relative_to(upload_folder)
    for size in THUMBNAIL_SIZES:
        thumbnail_path = (
            upload_folder
            / ".thumbnails"
            / relative_path.parent
            / f"{file_path.stem}-{size}.webp"
        )
        thumbnail_path.unlink(missing_ok=True)


def _remove_empty_product_directory(app, slug: str) -> None:
    product_dir = Path(app.config["UPLOAD_FOLDER"]) / slug
    if product_dir.is_dir() and not any(product_dir.iterdir()):
        product_dir.rmdir()


def _ensure_database_columns(app) -> None:
    """Aggiorna i database esistenti quando vengono aggiunte nuove colonne."""
    inspector = db.inspect(db.engine)
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    required_product_columns = {
        "category_id": "INTEGER",
        "subcategory_id": "INTEGER",
        "is_model": "INTEGER NOT NULL DEFAULT 1",
    }
    for column_name, column_type in required_product_columns.items():
        if column_name in product_columns:
            continue
        with db.engine.begin() as connection:
            connection.execute(
                db.text(f"ALTER TABLE products ADD COLUMN {column_name} {column_type}")
            )
        app.logger.info("Aggiunta colonna %s alla tabella products.", column_name)

    category_columns = {column["name"] for column in inspector.get_columns("categories")}
    if "direct_subcategory_access" not in category_columns:
        with db.engine.begin() as connection:
            connection.execute(
                db.text(
                    "ALTER TABLE categories ADD COLUMN "
                    "direct_subcategory_access INTEGER NOT NULL DEFAULT 0"
                )
            )
            connection.execute(
                db.text(
                    "UPDATE categories SET direct_subcategory_access = 1 "
                    "WHERE slug = 'scarpe-adidas'"
                )
            )
        app.logger.info("Aggiunta impostazione accesso diretto alle micro-categorie.")

    image_columns = {column["name"] for column in inspector.get_columns("product_images")}
    if "album_id" not in image_columns:
        with db.engine.begin() as connection:
            connection.execute(db.text("ALTER TABLE product_images ADD COLUMN album_id INTEGER"))
        app.logger.info("Aggiunta colonna album_id alla tabella product_images.")


def _validate_subcategory(
    category: Category | None, subcategory_id: int | None
) -> Subcategory | None:
    if subcategory_id is None:
        return None
    subcategory = db.session.get(Subcategory, subcategory_id)
    if subcategory is None or category is None or subcategory.category_id != category.id:
        abort(400, "Micro-categoria non valida per la macro-categoria selezionata.")
    return subcategory


def _get_or_create_photo_container(
    category: Category, subcategory: Subcategory
) -> Product:
    """Restituisce il contenitore invisibile delle foto caricate senza modello."""
    product = Product.query.filter_by(
        category_id=category.id,
        subcategory_id=subcategory.id,
        is_model=False,
    ).first()
    if product is not None:
        return product

    name = f"Foto {category.name} {subcategory.name}"
    base_slug = _slugify(f"foto-{category.slug}-{subcategory.slug}")
    slug = base_slug
    suffix = 2
    while Product.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    product = Product(
        name=name,
        slug=slug,
        category=category,
        subcategory=subcategory,
        is_model=False,
    )
    db.session.add(product)
    db.session.flush()
    return product


def _merge_product_into(destination: Product, source: Product) -> None:
    """Trasferisce immagini e album nel modello di destinazione."""
    for source_album in list(source.albums):
        destination_album = ProductAlbum.query.filter_by(
            product_id=destination.id, slug=source_album.slug
        ).first()
        if destination_album is None:
            source_album.product = destination
            continue

        next_position = max(
            (image.position for image in destination_album.images),
            default=0,
        )
        for image in list(source_album.images):
            next_position += 1
            image.product = destination
            image.album = destination_album
            image.position = next_position

    for image in list(source.images):
        image.product = destination

    db.session.flush()
    db.session.delete(source)


def _admin_product_redirect(product: Product, page: int):
    if not product.is_model:
        return redirect(
            url_for("admin_manage_unmodeled_photos", product_id=product.id, page=page)
        )
    return redirect(url_for("admin_upload", _anchor=f"product-{product.id}"))


def _image_files(files) -> list:
    return [
        file
        for file in files
        if file and file.filename and Path(file.filename).suffix.lower() in ALLOWED_EXTENSIONS
    ]


def _save_uploaded_image_as_webp(file, destination_path: Path) -> None:
    """Ridimensiona e salva un upload come WebP ottimizzato per il sito."""
    try:
        file.stream.seek(0)
        _save_image_as_webp(file.stream, destination_path)
    except (Image.UnidentifiedImageError, OSError) as error:
        raise ValueError(
            f"Il file '{secure_filename(file.filename)}' non è un'immagine valida."
        ) from error
    finally:
        file.stream.seek(0)


def _save_image_as_webp(source, destination_path: Path) -> None:
    """Crea un WebP atomico, orientato correttamente e adatto alla visualizzazione web."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(
        f"{destination_path.name}.{uuid4().hex}.tmp"
    )
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(
                (WEBP_MAX_DIMENSION, WEBP_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            image.save(temporary_path, "WEBP", quality=WEBP_QUALITY, method=6)
        temporary_path.replace(destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _convert_existing_images_to_webp(app) -> None:
    """Migra una volta le immagini preesistenti al formato WebP."""
    legacy_images = ProductImage.query.filter(
        ~ProductImage.filename.ilike("%.webp")
    ).all()
    if not legacy_images:
        return

    upload_folder = Path(app.config["UPLOAD_FOLDER"]).resolve()
    converted = 0
    for image in legacy_images:
        source_path = (upload_folder / image.filename).resolve()
        if upload_folder not in source_path.parents or not source_path.is_file():
            app.logger.error(
                "Impossibile convertire l'immagine mancante o non valida: %s",
                image.filename,
            )
            continue

        destination_path = source_path.with_suffix(".webp")
        try:
            _save_image_as_webp(source_path, destination_path)
        except (Image.UnidentifiedImageError, OSError) as error:
            app.logger.error("Impossibile convertire %s: %s", image.filename, error)
            continue

        original_path = source_path
        image.filename = (
            Path(image.filename).with_suffix(".webp").as_posix()
        )
        original_path.unlink()
        converted += 1

    if converted:
        db.session.commit()
        app.logger.info("Convertite %s immagini esistenti in WebP.", converted)


def _thumbnail_response(app, filename: str, size: int):
    """Restituisce una miniatura WebP memorizzata accanto alle immagini caricate."""
    upload_folder = Path(app.config["UPLOAD_FOLDER"]).resolve()
    source_name = safe_join(str(upload_folder), filename)
    if source_name is None:
        abort(404)

    source_path = Path(source_name).resolve()
    if not source_path.is_file():
        abort(404)
    try:
        relative_path = source_path.relative_to(upload_folder)
    except ValueError:
        abort(404)

    thumbnail_path = (
        upload_folder
        / ".thumbnails"
        / relative_path.parent
        / f"{source_path.stem}-{size}.webp"
    )
    if not thumbnail_path.is_file() or thumbnail_path.stat().st_mtime < source_path.stat().st_mtime:
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = thumbnail_path.with_name(
            f"{thumbnail_path.name}.{uuid4().hex}.tmp"
        )
        try:
            with Image.open(source_path) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(temporary_path, "WEBP", quality=78, method=6)
            temporary_path.replace(thumbnail_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    return send_file(
        thumbnail_path,
        mimetype="image/webp",
        conditional=True,
        max_age=31_536_000,
    )


def _group_files_by_parent_folder(files) -> dict[str, list]:
    """Raggruppa le immagini per la cartella che le contiene."""
    grouped_files: dict[str, list] = {}
    for file in files:
        file_path = PurePosixPath(file.filename.replace("\\", "/"))
        if len(file_path.parts) < 2 or file_path.parent.name in {"", ".", ".."}:
            raise ValueError(
                "Seleziona una cartella che contenga una sottocartella per ciascun prodotto."
            )
        grouped_files.setdefault(file_path.parent.name, []).append(file)
    return grouped_files


def _normalize_existing_product_titles() -> None:
    """Normalizza i titoli già pubblicati e ne rimuove gli Style Code."""
    changed = False
    for item in Product.query.all():
        title = _clean_product_title(item.name)
        if title and item.name != title:
            item.name = title
            changed = True
    for item in ProductAlbum.query.all():
        title = _clean_product_title(item.name)
        if title and item.name != title:
            item.name = title
            changed = True
    if changed:
        db.session.commit()


def _create_or_update_album(app, product: Product, name: str, files) -> ProductAlbum:
    """Aggiunge le immagini a un album del modello o della micro-categoria."""
    name = _clean_product_title(name)
    slug = _slugify(name)
    album = ProductAlbum.query.filter_by(product_id=product.id, slug=slug).first()
    if album is None:
        album = ProductAlbum(product=product, name=name, slug=slug)
        db.session.add(album)
        db.session.flush()

    product_dir = Path(app.config["UPLOAD_FOLDER"]) / product.slug
    product_dir.mkdir(parents=True, exist_ok=True)

    first_position = len(album.images) + 1
    for position, file in enumerate(files, start=first_position):
        filename = f"{album.slug}-{position:03d}.webp"
        _save_uploaded_image_as_webp(file, product_dir / filename)
        db.session.add(
            ProductImage(
                product=product,
                album=album,
                filename=f"{product.slug}/{filename}",
                position=position,
            )
        )

    db.session.commit()
    return album


if __name__ == "__main__":
    flask_app = create_app()
    flask_app.run(debug=True, host="0.0.0.0", port=5000)
