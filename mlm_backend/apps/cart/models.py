from django.conf import settings
from django.db import models


class Cart(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cart",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cart_cart"


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="items",
    )
    ebook = models.ForeignKey(
        "courses.EBook",
        on_delete=models.CASCADE,
        related_name="cart_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cart_cartitem"
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "ebook"],
                name="uniq_cartitem_cart_ebook",
            ),
        ]
