import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db


def _import_backend_main():
    backend_dir = Path(__file__).resolve().parents[1]
    previous_cwd = os.getcwd()
    try:
        os.chdir(backend_dir)
        return importlib.import_module("main")
    finally:
        os.chdir(previous_cwd)


backend_main = _import_backend_main()


class RefreshIntervalPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="openusage-refresh-test-")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await db.init_db()
        self.user_id = await db.create_user("refresh-test-user", "test-password")

    async def asyncTearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_new_service_defaults_to_five_minutes(self):
        service_id = await db.add_user_service(
            self.user_id, "custom", "Default cadence", {}
        )

        service = await db.get_user_service(service_id, self.user_id)
        self.assertEqual(service["refresh_interval_minutes"], 5)

    async def test_custom_interval_is_persisted_and_preserved_when_omitted(self):
        service_id = await db.add_user_service(
            self.user_id,
            "custom",
            "Slow cadence",
            {},
            refresh_interval_minutes=720,
        )

        await db.update_user_service(
            service_id, self.user_id, "Slow cadence renamed", {"changed": True}
        )
        service = await db.get_user_service(service_id, self.user_id)
        self.assertEqual(service["refresh_interval_minutes"], 720)

        await db.update_user_service(
            service_id,
            self.user_id,
            "Faster cadence",
            {"changed": True},
            refresh_interval_minutes=60,
        )
        service = await db.get_user_service(service_id, self.user_id)
        self.assertEqual(service["refresh_interval_minutes"], 60)


class RefreshSchedulingTests(unittest.IsolatedAsyncioTestCase):
    service = {
        "id": 9876,
        "user_id": 7,
        "service_type": "custom",
        "name": "Quota API",
        "config": {},
        "refresh_interval_minutes": 720,
    }

    def tearDown(self):
        backend_main._last_fetch_attempt_at.pop(self.service["id"], None)
        backend_main._last_fetch_error.pop(self.service["id"], None)
        backend_main._inflight_fetches.pop(self.service["id"], None)

    async def test_failed_attempt_does_not_retry_before_service_interval(self):
        now = datetime.now(timezone.utc)
        backend_main._last_fetch_attempt_at[self.service["id"]] = now
        backend_main._last_fetch_error[self.service["id"]] = "temporary failure"
        fetched_at = (now - timedelta(hours=24)).isoformat()

        with (
            patch.object(
                backend_main,
                "get_latest_service_usage",
                AsyncMock(return_value=({"requests_remaining": 1400}, fetched_at)),
            ),
            patch.object(backend_main, "_trigger_background_live_fetch") as trigger,
        ):
            result = await backend_main._fetch_and_save_user_service(self.service)

        trigger.assert_not_called()
        self.assertEqual(result["result"]["stale_error"], "temporary failure")

    async def test_stale_service_refreshes_after_its_interval(self):
        now = datetime.now(timezone.utc)
        backend_main._last_fetch_attempt_at[self.service["id"]] = now - timedelta(hours=13)
        fetched_at = (now - timedelta(hours=24)).isoformat()

        with (
            patch.object(
                backend_main,
                "get_latest_service_usage",
                AsyncMock(return_value=({"requests_remaining": 1400}, fetched_at)),
            ),
            patch.object(backend_main, "_trigger_background_live_fetch") as trigger,
        ):
            await backend_main._fetch_and_save_user_service(self.service)

        trigger.assert_called_once_with(self.service)


if __name__ == "__main__":
    unittest.main()
