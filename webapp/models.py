"""models.py - Modelli del database (SQLite via SQLAlchemy)."""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    direct_subcategory_access = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    products = db.relationship("Product", back_populates="category")
    subcategories = db.relationship(
        "Subcategory",
        back_populates="category",
        cascade="all, delete-orphan",
        order_by="Subcategory.name",
    )


class Subcategory(db.Model):
    __tablename__ = "subcategories"
    __table_args__ = (db.UniqueConstraint("category_id", "slug", name="uq_subcategory_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(120), nullable=False)

    category = db.relationship("Category", back_populates="subcategories")
    products = db.relationship("Product", back_populates="subcategory")


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False, index=True)
    source_url = db.Column(db.String(500), nullable=True)  # link album Yupoo originale
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True, index=True)
    subcategory_id = db.Column(
        db.Integer, db.ForeignKey("subcategories.id"), nullable=True, index=True
    )
    is_model = db.Column(db.Boolean, default=True, nullable=False, index=True)

    category = db.relationship("Category", back_populates="products")
    subcategory = db.relationship("Subcategory", back_populates="products")

    images = db.relationship(
        "ProductImage",
        backref="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.position",
    )
    albums = db.relationship(
        "ProductAlbum",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductAlbum.created_at",
    )

    def cover_image(self):
        if self.albums:
            return self.albums[0].cover_image()
        return self.images[0] if self.images else None


class ProductAlbum(db.Model):
    __tablename__ = "product_albums"
    __table_args__ = (db.UniqueConstraint("product_id", "slug", name="uq_product_album_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product", back_populates="albums")
    images = db.relationship(
        "ProductImage",
        back_populates="album",
        cascade="all, delete-orphan",
        order_by="ProductImage.position",
    )

    def cover_image(self):
        return self.images[0] if self.images else None


class ProductImage(db.Model):
    __tablename__ = "product_images"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    album_id = db.Column(db.Integer, db.ForeignKey("product_albums.id"), nullable=True, index=True)
    filename = db.Column(db.String(500), nullable=False)  # percorso relativo in static/uploads
    position = db.Column(db.Integer, default=0)

    album = db.relationship("ProductAlbum", back_populates="images")
