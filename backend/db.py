import aiosqlite
import json
import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("DB_PATH", "/app/data/openusage.db")


def hash_password(password: str, salt: str = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
    ).hex()
    return f"{salt}:{pw_hash}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, pw_hash = stored_hash.split(":", 1)
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000
        ).hex()
        return secrets.compare_digest(expected, pw_hash)
    except Exception:
        return False


async def init_db():
    # Remove legacy single-user database if needed or ensure directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                service_type TEXT NOT NULL,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_usage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_service_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                FOREIGN KEY (user_service_id) REFERENCES user_services(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Lightweight migration: add category_id to user_services if this DB
        # predates categories. No migration framework in this project, so
        # every column addition is guarded by a PRAGMA check like this one.
        async with db.execute("PRAGMA table_info(user_services)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
        if "category_id" not in cols:
            await db.execute("ALTER TABLE user_services ADD COLUMN category_id INTEGER")

        # Automatic refresh cadence is service-specific. Existing installs
        # keep the historical five-minute behavior; individual services can
        # opt into a slower interval without putting scheduler metadata in
        # the provider-specific config JSON.
        if "refresh_interval_minutes" not in cols:
            await db.execute(
                "ALTER TABLE user_services ADD COLUMN refresh_interval_minutes INTEGER NOT NULL DEFAULT 5"
            )

        # Same pattern for the account/auth columns added alongside OIDC,
        # password reset and 2FA. password_hash stays NOT NULL even for
        # OIDC-only accounts (they get an unusable random hash on creation)
        # so this never needs a NOT NULL migration, which SQLite can't do
        # without a full table rebuild.
        async with db.execute("PRAGMA table_info(users)") as cursor:
            user_cols = [row[1] for row in await cursor.fetchall()]
        if "email" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "is_admin" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oidc_states (
                state TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS two_factor_auth (
                user_id INTEGER PRIMARY KEY,
                secret TEXT,
                pending_secret TEXT,
                pending_expires_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                recovery_hashes TEXT NOT NULL DEFAULT '[]',
                challenge_hash TEXT,
                challenge_expires_at TEXT,
                last_step INTEGER NOT NULL DEFAULT -1,
                failures INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # notify_thresholds holds this service's push-notification config
        # (e.g. {"session_threshold": 80, "weekly_threshold": null,
        # "monthly_threshold": null}) -- which keys are meaningful depends on
        # the service_type (see backend/thresholds.py's APPLICABLE_THRESHOLDS).
        if "notify_thresholds" not in cols:
            await db.execute("ALTER TABLE user_services ADD COLUMN notify_thresholds TEXT NOT NULL DEFAULT '{}'")

        # One row per browser/device a user has enabled push notifications on.
        # There's no separate "push enabled" flag on the user -- having at
        # least one subscription here is what "enabled" means.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Tracks whether a given service/threshold-type pairing is still
        # "armed" to fire a push notification. Sending sets armed=0 so the
        # background refresh loop doesn't re-notify on every service-specific
        # poll while usage stays above the threshold; dropping back
        # below it (a new session, week, or billing month) sets it back to 1.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_notify_state (
                user_service_id INTEGER NOT NULL,
                threshold_type TEXT NOT NULL,
                armed INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_service_id, threshold_type),
                FOREIGN KEY (user_service_id) REFERENCES user_services(id) ON DELETE CASCADE
            )
        """)

        await db.commit()


async def create_user(username: str, password: str, email: str | None = None):
    pw_hash = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            is_first_user = (await cursor.fetchone())[0] == 0
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, email, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username.lower().strip(), pw_hash, email, int(is_first_user), now),
        )
        await db.commit()
        return cursor.lastrowid


async def create_oidc_user(username: str, email: str) -> int:
    """OIDC-only account: no local password, so it gets an unusable random
    hash (never NULL, to avoid a NOT NULL migration SQLite can't do without
    a full table rebuild) - logging in with a password is simply never
    going to match it."""
    pw_hash = hash_password(secrets.token_urlsafe(32))
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            is_first_user = (await cursor.fetchone())[0] == 0
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, email, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, pw_hash, email, int(is_first_user), now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_by_email(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email = ?", (email,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_user_email(user_id: int, email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
        await db.commit()


async def update_user_password(user_id: int, new_password: str):
    pw_hash = hash_password(new_password)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
        await db.commit()


async def get_user_by_username(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE username = ?", (username.lower().strip(),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_by_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, email, is_admin, created_at FROM users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires_at),
        )
        await db.commit()
        return token


async def get_user_by_session(token: str):
    if not token:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT u.id, u.username, u.email, u.is_admin, u.password_hash, u.created_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, datetime.now(timezone.utc).isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def delete_session(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()


async def delete_all_sessions_for_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.commit()


def _load_service_row(row) -> dict:
    item = dict(row)
    item["config"] = json.loads(item["config"]) if item["config"] else {}
    item["notify_thresholds"] = json.loads(item["notify_thresholds"]) if item["notify_thresholds"] else {}
    return item


async def get_user_services(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_services WHERE user_id = ? ORDER BY id ASC", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_load_service_row(r) for r in rows]


async def get_all_user_services():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_services ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
            return [_load_service_row(r) for r in rows]


async def get_user_service(service_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_services WHERE id = ? AND user_id = ?", (service_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return _load_service_row(row) if row else None


async def add_user_service(
    user_id: int,
    service_type: str,
    name: str,
    config: dict,
    category_id: int | None = None,
    notify_thresholds: dict | None = None,
    refresh_interval_minutes: int = 5,
):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO user_services (user_id, service_type, name, config, category_id, notify_thresholds, refresh_interval_minutes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                service_type,
                name,
                json.dumps(config),
                category_id,
                json.dumps(notify_thresholds or {}),
                refresh_interval_minutes,
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def update_user_service(
    service_id: int,
    user_id: int,
    name: str,
    config: dict,
    category_id: int | None = None,
    notify_thresholds: dict | None = None,
    refresh_interval_minutes: int | None = None,
):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if notify_thresholds is None:
            # Internal credential rotation and older PUT callers update only
            # fetcher config. Omitting thresholds must preserve both their
            # values and the once-per-crossing armed state.
            await db.execute(
                """
                UPDATE user_services
                SET name = ?, config = ?, category_id = ?,
                    refresh_interval_minutes = COALESCE(?, refresh_interval_minutes),
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    name,
                    json.dumps(config),
                    category_id,
                    refresh_interval_minutes,
                    now,
                    service_id,
                    user_id,
                ),
            )
        else:
            normalized_thresholds = notify_thresholds or {}
            async with db.execute(
                "SELECT notify_thresholds FROM user_services WHERE id = ? AND user_id = ?",
                (service_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
            existing_thresholds = json.loads(row[0]) if row and row[0] else {}

            await db.execute(
                """
                UPDATE user_services
                SET name = ?, config = ?, category_id = ?, notify_thresholds = ?,
                    refresh_interval_minutes = COALESCE(?, refresh_interval_minutes),
                    updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    name,
                    json.dumps(config),
                    category_id,
                    json.dumps(normalized_thresholds),
                    refresh_interval_minutes,
                    now,
                    service_id,
                    user_id,
                ),
            )
            if existing_thresholds != normalized_thresholds:
                # A genuinely changed threshold should be free to fire under
                # the new configuration, even if the old one already fired.
                await db.execute(
                    "DELETE FROM service_notify_state WHERE user_service_id = ?",
                    (service_id,),
                )
        await db.commit()


async def delete_user_service(service_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_services WHERE id = ? AND user_id = ?", (service_id, user_id)
        )
        await db.execute("DELETE FROM service_usage_history WHERE user_service_id = ?", (service_id,))
        await db.execute("DELETE FROM service_notify_state WHERE user_service_id = ?", (service_id,))
        await db.commit()


async def delete_all_user_services(user_id: int) -> int:
    """Delete every configured service owned by a user and its dependent data.

    Foreign-key cascades are not assumed here because SQLite requires
    PRAGMA foreign_keys=ON for every connection. Keeping the cleanup explicit
    also makes the deletion behavior consistent for older installations.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM user_services WHERE user_id = ?", (user_id,)
        ) as cursor:
            deleted_count = (await cursor.fetchone())[0]
        await db.execute(
            """
            DELETE FROM service_usage_history
            WHERE user_service_id IN (SELECT id FROM user_services WHERE user_id = ?)
            """,
            (user_id,),
        )
        await db.execute(
            """
            DELETE FROM service_notify_state
            WHERE user_service_id IN (SELECT id FROM user_services WHERE user_id = ?)
            """,
            (user_id,),
        )
        await db.execute("DELETE FROM user_services WHERE user_id = ?", (user_id,))
        await db.commit()
        return deleted_count


async def delete_user_account(user_id: int) -> bool:
    """Permanently delete a user and all data that belongs to the account."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM service_usage_history
            WHERE user_service_id IN (SELECT id FROM user_services WHERE user_id = ?)
            """,
            (user_id,),
        )
        await db.execute(
            """
            DELETE FROM service_notify_state
            WHERE user_service_id IN (SELECT id FROM user_services WHERE user_id = ?)
            """,
            (user_id,),
        )
        await db.execute("DELETE FROM user_services WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM categories WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM two_factor_auth WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_service_notify_armed(user_service_id: int, threshold_type: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT armed FROM service_notify_state WHERE user_service_id = ? AND threshold_type = ?",
            (user_service_id, threshold_type),
        ) as cursor:
            row = await cursor.fetchone()
            return True if row is None else bool(row[0])


async def set_service_notify_armed(user_service_id: int, threshold_type: str, armed: bool):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO service_notify_state (user_service_id, threshold_type, armed, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_service_id, threshold_type)
            DO UPDATE SET armed = excluded.armed, updated_at = excluded.updated_at
            """,
            (user_service_id, threshold_type, int(armed), now),
        )
        await db.commit()


async def add_push_subscription(user_id: int, endpoint: str, p256dh: str, auth: str):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth
            """,
            (user_id, endpoint, p256dh, auth, now),
        )
        await db.commit()


async def delete_push_subscription(user_id: int, endpoint: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?", (user_id, endpoint)
        )
        await db.commit()


async def delete_push_subscription_by_endpoint(endpoint: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        await db.commit()


async def get_push_subscriptions(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM push_subscriptions WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def save_service_usage(user_service_id: int, data: dict) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO service_usage_history (user_service_id, data, fetched_at)
            SELECT ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM user_services WHERE id = ?)
            """,
            (user_service_id, json.dumps(data), now, user_service_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_latest_service_usage(user_service_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT data, fetched_at FROM service_usage_history WHERE user_service_id = ? ORDER BY id DESC LIMIT 1",
            (user_service_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0]), row[1]
            return None, None


async def get_categories(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM categories WHERE user_id = ? ORDER BY position ASC, id ASC", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def create_category(user_id: int, name: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM categories WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            position = row[0]
        cursor = await db.execute(
            "INSERT INTO categories (user_id, name, position, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, position, now),
        )
        await db.commit()
        return cursor.lastrowid


async def update_category(category_id: int, user_id: int, name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE categories SET name = ? WHERE id = ? AND user_id = ?", (name, category_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_category(category_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # No FK-cascade reliance elsewhere in this file, so clear the
        # reference explicitly -- affected services fall back to Uncategorized.
        await db.execute(
            "UPDATE user_services SET category_id = NULL WHERE category_id = ? AND user_id = ?",
            (category_id, user_id),
        )
        await db.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))
        await db.commit()


async def move_category(category_id: int, user_id: int, direction: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM categories WHERE user_id = ? ORDER BY position ASC, id ASC", (user_id,)
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

        idx = next((i for i, r in enumerate(rows) if r["id"] == category_id), None)
        if idx is None:
            return False
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if swap_idx < 0 or swap_idx >= len(rows):
            return False

        a, b = rows[idx], rows[swap_idx]
        await db.execute("UPDATE categories SET position = ? WHERE id = ?", (b["position"], a["id"]))
        await db.execute("UPDATE categories SET position = ? WHERE id = ?", (a["position"], b["id"]))
        await db.commit()
        return True


# ── OIDC state (CSRF token for the authorize -> callback round trip) ──

OIDC_STATE_MAX_AGE = timedelta(minutes=10)


async def create_oidc_state() -> str:
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        # Best-effort cleanup of abandoned states, piggybacked on the next
        # authorize call rather than a separate scheduled job.
        await db.execute(
            "DELETE FROM oidc_states WHERE created_at < ?", ((now - OIDC_STATE_MAX_AGE).isoformat(),)
        )
        await db.execute("INSERT INTO oidc_states (state, created_at) VALUES (?, ?)", (state, now.isoformat()))
        await db.commit()
        return state


async def consume_oidc_state(state: str) -> bool:
    """Single-use: returns whether it was valid, and deletes it either way."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT created_at FROM oidc_states WHERE state = ?", (state,)) as cursor:
            row = await cursor.fetchone()
        await db.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
        await db.commit()
        if not row:
            return False
        created_at = datetime.fromisoformat(row[0])
        return datetime.now(timezone.utc) - created_at <= OIDC_STATE_MAX_AGE


# ── Password reset tokens ──

async def create_password_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Only one live reset link per user, same as candlr.
        await db.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
        await db.execute(
            "INSERT INTO password_reset_tokens (user_id, token, created_at) VALUES (?, ?, ?)",
            (user_id, token, now),
        )
        await db.commit()
        return token


async def get_password_reset_token(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM password_reset_tokens WHERE token = ?", (token,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def delete_password_reset_token(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
        await db.commit()


# ── Two-factor authentication ──

def _row_to_2fa(row) -> dict:
    item = dict(row)
    item["recovery_hashes"] = json.loads(item["recovery_hashes"]) if item["recovery_hashes"] else []
    item["enabled"] = bool(item["enabled"])
    return item


async def get_2fa(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM two_factor_auth WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_2fa(row) if row else None


async def ensure_2fa_row(user_id: int) -> dict:
    """Returns the user's 2FA row, creating a disabled default one first if
    it doesn't exist yet - mirrors candlr's locked_state()."""
    existing = await get_2fa(user_id)
    if existing:
        return existing
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO two_factor_auth (user_id) VALUES (?)", (user_id,))
        await db.commit()
    return await get_2fa(user_id)


async def get_2fa_by_challenge_hash(challenge_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM two_factor_auth WHERE challenge_hash = ?", (challenge_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_2fa(row) if row else None


async def set_2fa_pending(user_id: int, pending_secret: str, pending_expires_at: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE two_factor_auth SET pending_secret = ?, pending_expires_at = ? WHERE user_id = ?",
            (pending_secret, pending_expires_at, user_id),
        )
        await db.commit()


async def enable_2fa(user_id: int, secret: str, recovery_hashes: list, last_step: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE two_factor_auth
            SET secret = ?, pending_secret = NULL, pending_expires_at = NULL, enabled = 1,
                recovery_hashes = ?, last_step = ?, failures = 0, locked_until = NULL
            WHERE user_id = ?
            """,
            (secret, json.dumps(recovery_hashes), last_step, user_id),
        )
        await db.commit()


async def disable_2fa(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE two_factor_auth
            SET secret = NULL, pending_secret = NULL, pending_expires_at = NULL, enabled = 0,
                recovery_hashes = '[]', challenge_hash = NULL, challenge_expires_at = NULL,
                last_step = -1, failures = 0, locked_until = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )
        await db.commit()


async def set_2fa_challenge(user_id: int, challenge_hash: str, challenge_expires_at: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE two_factor_auth SET challenge_hash = ?, challenge_expires_at = ? WHERE user_id = ?",
            (challenge_hash, challenge_expires_at, user_id),
        )
        await db.commit()


async def clear_2fa_challenge(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE two_factor_auth SET challenge_hash = NULL, challenge_expires_at = NULL WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def update_2fa_recovery_hashes(user_id: int, recovery_hashes: list):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE two_factor_auth SET recovery_hashes = ? WHERE user_id = ?",
            (json.dumps(recovery_hashes), user_id),
        )
        await db.commit()


async def record_2fa_success(user_id: int, last_step: int, recovery_hashes: list):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE two_factor_auth
            SET last_step = ?, recovery_hashes = ?, failures = 0, locked_until = NULL
            WHERE user_id = ?
            """,
            (last_step, json.dumps(recovery_hashes), user_id),
        )
        await db.commit()


async def record_2fa_failure(user_id: int, failures: int, locked_until: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE two_factor_auth SET failures = ?, locked_until = NULL WHERE user_id = ?"
            if locked_until is None
            else "UPDATE two_factor_auth SET failures = ?, locked_until = ? WHERE user_id = ?",
            (failures, user_id) if locked_until is None else (failures, locked_until, user_id),
        )
        await db.commit()
