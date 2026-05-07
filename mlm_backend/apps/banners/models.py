from django.db import models


class Banner(models.Model):
    """
    Home/landing page banners managed by admins.
    """

    title = models.CharField(max_length=255, blank=True, default="")
    image = models.FileField(upload_to="banners/%Y/%m/")
    link_url = models.URLField(max_length=500, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "banners_banner"
        ordering = ["sort_order", "-id"]

    def __str__(self) -> str:
        return f"{self.title or 'Banner'} #{self.pk}"

