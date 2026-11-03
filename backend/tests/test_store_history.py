import os
import tempfile
import unittest

import aiosqlite

import db


class StoreHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="openusage-history-test-")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await db.init_db()
        self.user_id = await db.create_user("history-test-user", "test-password")

    async def asyncTearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def _count(self, service_id):
        return len(await db.get_service_usage_history(service_id))

    async def test_off_by_default_keeps_only_latest(self):
        sid = await db.add_user_service(self.user_id, "custom", "s", {})
        for i in range(3):
            await db.save_service_usage(sid, {"n": i})
        self.assertEqual(await self._count(sid), 1)
        data, _ = await db.get_latest_service_usage(sid)
        self.assertEqual(data, {"n": 2})

    async def test_enabled_appends_every_fetch(self):
        sid = await db.add_user_service(self.user_id, "custom", "s", {}, store_history=True)
        for i in range(3):
            await db.save_service_usage(sid, {"n": i})
        history = await db.get_service_usage_history(sid)
        self.assertEqual([h["data"]["n"] for h in history], [0, 1, 2])

    async def test_omitted_flag_on_update_is_preserved(self):
        sid = await db.add_user_service(self.user_id, "custom", "s", {}, store_history=True)
        await db.update_user_service(sid, self.user_id, "renamed", {})
        svc = await db.get_user_service(sid, self.user_id)
        self.assertTrue(svc["store_history"])

    async def test_disabling_prunes_to_latest(self):
        sid = await db.add_user_service(self.user_id, "custom", "s", {}, store_history=True)
        for i in range(3):
            await db.save_service_usage(sid, {"n": i})
        await db.update_user_service(sid, self.user_id, "s", {}, store_history=False)
        history = await db.get_service_usage_history(sid)
        self.assertEqual([h["data"]["n"] for h in history], [2])
        self.assertFalse((await db.get_user_service(sid, self.user_id))["store_history"])

    async def test_existing_install_is_grandfathered_as_enabled(self):
        # Simulate a DB created before the column existed.
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("ALTER TABLE user_services DROP COLUMN store_history")
            await conn.commit()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO user_services (user_id, service_type, name, config, notify_thresholds, refresh_interval_minutes, created_at, updated_at)"
                " VALUES (?, 'custom', 'old', '{}', '{}', 5, 'x', 'x')",
                (self.user_id,),
            )
            await conn.commit()
        await db.init_db()
        svc = (await db.get_user_services(self.user_id))[0]
        self.assertTrue(svc["store_history"])


if __name__ == "__main__":
    unittest.main()
