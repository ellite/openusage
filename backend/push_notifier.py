"""Web Push delivery -- the only notification channel this app has. VAPID
keys are one instance-wide identity (see scripts/generate_vapid_keys.py);
each browser/device a user enables notifications on gets its own row in
push_subscriptions, and a single send fans out to all of a user's devices."""
import asyncio
import json
import os

from pywebpush import webpush, WebPushException

from db import get_push_subscriptions, delete_push_subscription_by_endpoint

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")


class PushNotifierError(Exception):
    pass


def is_configured() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def _send_one(subscription_info: dict, payload: str):
    webpush(
        subscription_info=subscription_info,
        data=payload,
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_SUBJECT},
        timeout=10,
    )


async def send_push(user_id: int, title: str, body: str, url: str = "/") -> None:
    if not is_configured():
        raise PushNotifierError("Push notifications are not configured on this instance")

    subs = await get_push_subscriptions(user_id)
    if not subs:
        raise PushNotifierError("No devices are subscribed to push notifications")

    payload = json.dumps({"title": title, "body": body, "url": url})
    delivered = 0
    errors: list[str] = []

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            await asyncio.to_thread(_send_one, subscription_info, payload)
            delivered += 1
        except WebPushException as e:
            status_code = getattr(e.response, "status_code", None)
            if status_code in (404, 410):
                # The browser/OS dropped this subscription; stop targeting it.
                await delete_push_subscription_by_endpoint(sub["endpoint"])
            else:
                errors.append(str(e))
        except Exception as e:
            errors.append(str(e))

    if delivered == 0:
        raise PushNotifierError("; ".join(errors) if errors else "No active devices could be reached")
