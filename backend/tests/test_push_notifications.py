import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import db


def _import_backend_main():
    """main.py mounts ./static at import time, so import it from backend/."""
    backend_dir = Path(__file__).resolve().parents[1]
    previous_cwd = os.getcwd()
    try:
        os.chdir(backend_dir)
        return importlib.import_module("main")
    finally:
        os.chdir(previous_cwd)


backend_main = _import_backend_main()


class ServiceThresholdPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="openusage-push-test-")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await db.init_db()
        self.user_id = await db.create_user("push-test-user", "test-password")
        self.service_id = await db.add_user_service(
            self.user_id,
            "claude",
            "Claude",
            {"credential": "old"},
            notify_thresholds={"session_threshold": 80},
        )
        await db.set_service_notify_armed(self.service_id, "session", False)

    async def asyncTearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_omitted_thresholds_preserve_config_and_armed_state(self):
        await db.update_user_service(
            self.service_id,
            self.user_id,
            "Claude",
            {"credential": "rotated"},
        )

        service = await db.get_user_service(self.service_id, self.user_id)
        self.assertEqual(service["notify_thresholds"], {"session_threshold": 80})
        self.assertFalse(await db.get_service_notify_armed(self.service_id, "session"))

    async def test_unchanged_thresholds_do_not_rearm(self):
        await db.update_user_service(
            self.service_id,
            self.user_id,
            "Claude renamed",
            {"credential": "old"},
            notify_thresholds={"session_threshold": 80},
        )

        self.assertFalse(await db.get_service_notify_armed(self.service_id, "session"))

    async def test_changed_thresholds_rearm(self):
        await db.update_user_service(
            self.service_id,
            self.user_id,
            "Claude",
            {"credential": "old"},
            notify_thresholds={"session_threshold": 90},
        )

        self.assertTrue(await db.get_service_notify_armed(self.service_id, "session"))


class ThresholdDeliveryStateTests(unittest.IsolatedAsyncioTestCase):
    service = {
        "id": 42,
        "user_id": 7,
        "service_type": "claude",
        "name": "Claude",
        "notify_thresholds": {"session_threshold": 80},
    }
    usage = {"five_hour": {"utilization": 90}}

    async def test_failed_delivery_stays_armed_for_retry(self):
        with (
            patch.object(backend_main, "get_service_notify_armed", AsyncMock(return_value=True)),
            patch.object(backend_main, "set_service_notify_armed", AsyncMock()) as set_armed,
            patch.object(
                backend_main.push_notifier,
                "send_push",
                AsyncMock(side_effect=backend_main.push_notifier.PushNotifierError("temporary failure")),
            ),
        ):
            await backend_main._check_thresholds_and_notify(self.service, self.usage)

        set_armed.assert_not_awaited()

    async def test_successful_delivery_disarms_crossing(self):
        with (
            patch.object(backend_main, "get_service_notify_armed", AsyncMock(return_value=True)),
            patch.object(backend_main, "set_service_notify_armed", AsyncMock()) as set_armed,
            patch.object(backend_main.push_notifier, "send_push", AsyncMock()),
        ):
            await backend_main._check_thresholds_and_notify(self.service, self.usage)

        set_armed.assert_awaited_once_with(42, "session", False)


if __name__ == "__main__":
    unittest.main()
