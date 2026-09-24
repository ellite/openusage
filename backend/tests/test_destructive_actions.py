import os
import tempfile
import unittest

import aiosqlite
import db


class DestructiveActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="openusage-delete-test-")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await db.init_db()

        self.user_id = await db.create_user("delete-test-user", "test-password", "delete@example.com")
        self.other_user_id = await db.create_user("other-user", "test-password", "other@example.com")
        self.service_id = await db.add_user_service(
            self.user_id,
            "custom",
            "Private API",
            {"api_key": "secret"},
            notify_thresholds={"monthly_threshold": 80},
        )
        self.other_service_id = await db.add_user_service(
            self.other_user_id, "custom", "Other API", {"api_key": "keep"}
        )
        await db.save_service_usage(self.service_id, {"remaining": 10})
        await db.save_service_usage(self.other_service_id, {"remaining": 20})
        await db.set_service_notify_armed(self.service_id, "monthly", False)
        await db.create_category(self.user_id, "Work")

    async def asyncTearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def _count(self, table: str, where: str = "", params: tuple = ()) -> int:
        async with aiosqlite.connect(db.DB_PATH) as connection:
            async with connection.execute(
                f"SELECT COUNT(*) FROM {table} {where}", params
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def test_delete_all_services_removes_dependents_but_preserves_account_data(self):
        deleted = await db.delete_all_user_services(self.user_id)

        self.assertEqual(deleted, 1)
        self.assertEqual(await db.get_user_services(self.user_id), [])
        self.assertIsNotNone(await db.get_user_service(self.other_service_id, self.other_user_id))
        self.assertEqual(
            await self._count(
                "service_usage_history", "WHERE user_service_id = ?", (self.service_id,)
            ),
            0,
        )
        self.assertEqual(
            await self._count(
                "service_notify_state", "WHERE user_service_id = ?", (self.service_id,)
            ),
            0,
        )
        self.assertEqual(await self._count("categories", "WHERE user_id = ?", (self.user_id,)), 1)
        self.assertIsNotNone(await db.get_user_by_id(self.user_id))
        self.assertFalse(await db.save_service_usage(self.service_id, {"orphan": True}))

    async def test_delete_account_removes_all_owned_records_only(self):
        token = await db.create_session(self.user_id)
        await db.create_password_reset_token(self.user_id)
        await db.ensure_2fa_row(self.user_id)
        await db.add_push_subscription(
            self.user_id,
            "https://push.example/subscription",
            "public-key",
            "auth-secret",
        )

        deleted = await db.delete_user_account(self.user_id)

        self.assertTrue(deleted)
        self.assertIsNone(await db.get_user_by_id(self.user_id))
        self.assertIsNone(await db.get_user_by_session(token))
        self.assertEqual(await self._count("user_services", "WHERE user_id = ?", (self.user_id,)), 0)
        self.assertEqual(await self._count("categories", "WHERE user_id = ?", (self.user_id,)), 0)
        self.assertEqual(await self._count("push_subscriptions", "WHERE user_id = ?", (self.user_id,)), 0)
        self.assertEqual(await self._count("password_reset_tokens", "WHERE user_id = ?", (self.user_id,)), 0)
        self.assertEqual(await self._count("two_factor_auth", "WHERE user_id = ?", (self.user_id,)), 0)
        self.assertIsNotNone(await db.get_user_by_id(self.other_user_id))
        self.assertIsNotNone(await db.get_user_service(self.other_service_id, self.other_user_id))


if __name__ == "__main__":
    unittest.main()
