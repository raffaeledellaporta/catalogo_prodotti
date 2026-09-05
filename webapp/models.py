"""models.py - Modelli del database (SQLite via SQLAlchemy)."""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
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

    category = db.relationship("Category", back_populates="products")
    subcategory = db.relationship("Subcategory", back_populates="products")

    images = db.relationship(
        "ProductImage",
        backref="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.position",
    )

    def cover_image(self):
        return self.images[0] if self.images else None


class ProductImage(db.Model):
    __tablename__ = "product_images"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    filename = db.Column(db.String(500), nullable=False)  # percorso relativo in static/uploads
    position = db.Column(db.Integer, default=0)
