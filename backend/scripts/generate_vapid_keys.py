"""Generates a VAPID key pair for Web Push notifications.

Run from backend/: python scripts/generate_vapid_keys.py

Paste the printed values into VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY in .env
(or the openusage service's environment in docker-compose.yml).
"""
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode

vapid = Vapid02()
vapid.generate_keys()

private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
public_raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

print(f"VAPID_PRIVATE_KEY={b64urlencode(private_raw)}")
print(f"VAPID_PUBLIC_KEY={b64urlencode(public_raw)}")
