from django.conf import settings
from django.db import models


class EBook(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    thumbnail = models.FileField(upload_to="ebooks/thumbnails/%Y/%m/", null=True, blank=True)
    pages_count = models.PositiveIntegerField(default=1)
    language = models.CharField(max_length=64, blank=True, default="English")
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    full_pdf = models.FileField(upload_to="ebooks/full/%Y/%m/", null=True, blank=True)
    preview_pdf = models.FileField(upload_to="ebooks/preview/%Y/%m/", null=True, blank=True)
    file_url = models.URLField(max_length=500)
    is_active = models.BooleanField(default=True)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "courses_ebook"

    def __str__(self) -> str:
        return self.title


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

    def __str__(self) -> str:
        return f"{self.user_id} → {self.ebook.title} ({self.order_id})"
