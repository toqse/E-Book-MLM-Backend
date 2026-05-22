# Commission processing — production verification

After deploying the `transaction.on_commit` commission dispatch fix, confirm credits land in MySQL and Celery logs show structured commission lines.

## 1. Check commission ledger (MySQL shell)

```bash
cd ~/E-Book-MLM-Backend/mlm_backend/docker
docker compose exec db mysql -u mlm_user -pmlm_pass mlm_db
```

At the `mysql>` prompt:

```sql
SELECT id, order_id, recipient_id, commission_type, amount, tds_deducted, net_amount, status, created_at
FROM commissions_ledger
ORDER BY id DESC
LIMIT 20;

SELECT id, user_id, tx_type, amount, balance_after, reference, created_at
FROM wallet_transaction
WHERE tx_type='CREDIT'
ORDER BY id DESC
LIMIT 20;

exit;
```

## 2. Check Celery worker logs

```bash
docker compose logs --tail=200 celery | grep -E 'commission_task|commission_process|commission_credited|commission_skipped|commission_held'
```

Expected sequence per paid MLM order (buyer already in binary tree, or after placement completes):

- `commission_task_start order_id=N`
- `commission_task_order_loaded ... status=PAID sponsor_id=...`
- `commission_process_start order_id=N`
- `commission_credited ...` (one line per recipient) **or** a skip reason (`commission_skipped_*`, `commission_held_*`)
- `commission_process_done order_id=N`
- `commission_task_done order_id=N`

## 3. Re-process a stuck PAID order (if needed)

If an order was paid before the fix and has no ledger rows, trigger commission manually from the web container:

```bash
docker compose exec web python manage.py shell
```

```python
from apps.commissions.engine import CommissionEngine
from apps.payments.models import Order
order = Order.objects.get(pk=YOUR_ORDER_ID)  # must be PAID, buyer in binary tree
CommissionEngine.process_order(order)
```

## 4. Common skip reasons (not bugs)

| Log line | Meaning |
|----------|---------|
| `commission_skipped_not_paid` | Order not PAID when task ran (race before fix; should not recur) |
| `commission_skipped_duplicate` | Commissions already exist for this order |
| `commission_skipped_no_binary_node` | Buyer not placed in tree yet; wait for placement or auto-place job |
| `commission_held_kyc` | Recipient KYC not verified — ledger HELD, no wallet credit |
| `commission_held_cap` | Recipient hit earning cap |
