from django.core.management.base import BaseCommand

from apps.payments.models import GSTInvoice
from apps.payments.services import ensure_gst_invoice_pdf


class Command(BaseCommand):
    help = "Regenerate GST invoice PDFs for existing invoices."

    def add_arguments(self, parser):
        parser.add_argument(
            "--invoice-id",
            type=int,
            help="Regenerate only one invoice by GSTInvoice.id",
        )
        parser.add_argument(
            "--order-id",
            type=int,
            help="Regenerate only one invoice by Order.id",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print target invoices without writing files.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing PDF files as well.",
        )

    def handle(self, *args, **options):
        qs = GSTInvoice.objects.select_related("order").order_by("id")
        invoice_id = options.get("invoice_id")
        order_id = options.get("order_id")
        dry_run = bool(options.get("dry_run"))
        force = bool(options.get("force"))

        if invoice_id:
            qs = qs.filter(pk=invoice_id)
        if order_id:
            qs = qs.filter(order_id=order_id)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No matching GST invoices found."))
            return

        self.stdout.write(
            f"Processing {total} GST invoice(s) | force={force} dry_run={dry_run}"
        )
        done = 0
        for inv in qs.iterator():
            existing_name = (getattr(inv.pdf_file, "name", None) or "").strip()
            if dry_run:
                self.stdout.write(
                    f"[DRY] invoice_id={inv.pk} order_id={inv.order_id} has_file={bool(existing_name)}"
                )
                continue
            ensure_gst_invoice_pdf(inv.order, force=force)
            done += 1
            self.stdout.write(
                f"[OK] invoice_id={inv.pk} order_id={inv.order_id}"
            )

        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry run completed."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Regenerated {done} invoice PDF(s)."))

