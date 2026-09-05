"""models.py - Modelli del database (SQLite via SQLAlchemy)."""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False, index=True)
    source_url = db.Column(db.String(500), nullable=True)  # link album Yupoo originale
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

