from django.conf import settings
from django.db import models


class EBook(models.Model):
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=100)
    file_url = models.URLField(max_length=500)
    is_active = models.BooleanField(default=True)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "courses_ebook"


class Enrollment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="enrollments",
    )
    ebook = models.ForeignKey(EBook, on_delete=models.CASCADE, related_name="enrollments")
    order = models.ForeignKey(
        "payments.Order",
        on_delete=models.CASCADE,
        related_name="enrollments",
    )
    is_retail = models.BooleanField(default=False)
    download_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "courses_enrollment"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "ebook", "order"],
                name="uniq_enrollment_user_ebook_order",
            )
        ]
