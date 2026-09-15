"""Central account recovery preserves identity and revokes credentials atomically."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text

from lerobot.data_platform.account_passwords import PasswordReset, issue_reset, redeem_reset
from lerobot.data_platform.control_plane import ControlPlaneStore, _token_digest, _utcnow
from lerobot.data_platform.management_storage import AuditOutbox
from tests.datasets.test_control_plane import _app, _bootstrap, _store


def test_viewer_change_password_revokes_all_sessions(tmp_path):
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    admin = app.test_client()
    _bootstrap(admin)
    user = store.register_user(username="viewer-user", password="original-password")
    token, _ = store.authenticate_user("viewer-user", "original-password")
    viewer = app.test_client()
    viewer.post("/api/auth/login", json={"username": "viewer-user", "password": "original-password"})
    assert viewer.get("/account/password").status_code == 200
    payload = {
        "current_password": "wrong",
        "new_password": "replacement-password",
        "confirm_password": "replacement-password",
    }
    assert viewer.post("/api/auth/password", json=payload).status_code == 403
    assert store.verify_session(token)
    payload["current_password"] = "original-password"
    assert viewer.post("/api/auth/password", json=payload).status_code == 200
    assert store.verify_session(token) is None
    assert viewer.get("/api/auth/me").status_code == 401
    with pytest.raises(PermissionError):
        store.authenticate_user("viewer-user", "original-password")
    _, changed = store.authenticate_user("viewer-user", "replacement-password")
    assert changed["user_id"] == user["user_id"]
    assert "display_name" not in changed


def test_reset_permissions_reissue_expiry_and_disabled_state(tmp_path):
    store = _store(tmp_path)
    app = _app(tmp_path, store)
    admin = app.test_client()
    actor = _bootstrap(admin).json["user"]
    user = store.register_user(username="viewer-user", password="original-password")
    anonymous = app.test_client()
    endpoint = f"/api/auth/users/{user['user_id']}/password-reset"
    assert anonymous.post(endpoint, json={}).status_code == 401
    viewer = app.test_client()
    viewer.post("/api/auth/login", json={"username": "viewer-user", "password": "original-password"})
    assert viewer.post(endpoint, json={}).status_code == 403
    issued = admin.post(endpoint, json={})
    assert issued.status_code == 200
    assert issued.headers["Cache-Control"] == "no-store"
    first = issued.json["token"]
    second = issue_reset(store, user["user_id"], actor)
    with store.sessions() as session:
        stored = session.get(PasswordReset, user["user_id"])
        assert stored.token_digest == _token_digest(second)
        assert stored.token_digest != second
    with pytest.raises(ValueError):
        redeem_reset(store, first, "replacement-password")
    store.update_user(user["user_id"], active=False)
    payload = {
        "token": second,
        "new_password": "replacement-password",
        "confirm_password": "replacement-password",
    }
    assert anonymous.post("/api/auth/reset-password", json=payload).status_code == 200
    assert anonymous.post("/api/auth/reset-password", json=payload).status_code == 400
    unchanged = next(row for row in store.list_users() if row["user_id"] == user["user_id"])
    assert unchanged["active"] is False
    assert unchanged["role"] == "viewer"
    expired = issue_reset(store, user["user_id"], actor)
    with store.sessions.begin() as session:
        session.get(PasswordReset, user["user_id"]).expires_at = _utcnow() - timedelta(seconds=1)
    with pytest.raises(ValueError):
        redeem_reset(store, expired, "replacement-password")
    assert anonymous.get("/reset-password").status_code == 200
    assert anonymous.get("/reset-password").headers["Referrer-Policy"] == "no-referrer"


def test_reset_concurrent_consumption_and_session_revocation(tmp_path):
    store = _store(tmp_path)
    actor = _bootstrap(_app(tmp_path, store).test_client()).json["user"]
    user = store.register_user(username="viewer-user", password="original-password")
    old_session, _ = store.authenticate_user("viewer-user", "original-password")
    token = issue_reset(store, user["user_id"], actor)
    assert store.verify_session(old_session)

    def redeem(_):
        try:
            redeem_reset(store, token, "replacement-password")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(redeem, range(2))) == [False, True]
    assert store.verify_session(old_session) is None
    with store.sessions() as session:
        events = session.scalars(select(AuditOutbox)).all()
        payloads = str([event.payload for event in events])
        assert token not in payloads
        assert "replacement-password" not in payloads
        assert "account.password_reset_completed" in payloads


def test_rate_limit_and_validation(tmp_path):
    store = _store(tmp_path)
    client = _app(tmp_path, store).test_client()
    for _ in range(10):
        assert client.post("/api/auth/reset-password", json=[]).status_code == 400
    assert client.post("/api/auth/reset-password", json={}).status_code == 429


def test_legacy_display_name_migration_preserves_accounts(tmp_path):
    store = _store(tmp_path)
    actor = _bootstrap(_app(tmp_path, store).test_client()).json["user"]
    token, _ = store.authenticate_user(actor["username"], "strong-admin-password")
    with store.engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE dp_users ADD COLUMN display_name VARCHAR(128) NOT NULL DEFAULT 'Legacy'")
        )
    migrated = ControlPlaneStore(str(store.engine.url))
    assert "display_name" not in {
        column["name"] for column in inspect(migrated.engine).get_columns("dp_users")
    }
    assert migrated.verify_session(token)["user_id"] == actor["user_id"]
    assert (
        migrated.authenticate_user(actor["username"], "strong-admin-password")[1]["username"]
        == actor["username"]
    )
    assert migrated.register_user(username="new-user", password="new-user-password")["username"] == "new-user"
