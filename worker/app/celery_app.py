from celery import Celery

from repolace_shared.config import SharedSettings

_settings = SharedSettings()

celery_app = Celery(
    "repolace_worker",
    broker=_settings.celery_broker_url,
    backend=_settings.redis_url,
)
