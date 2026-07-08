"""Schedule MSG91 lifecycle notifications after DB commit."""

from __future__ import annotations

from typing import Any

from django.db import transaction


def schedule_msg91_lifecycle(task: Any, *args, **kwargs) -> None:
    transaction.on_commit(lambda: task.delay(*args, **kwargs))
