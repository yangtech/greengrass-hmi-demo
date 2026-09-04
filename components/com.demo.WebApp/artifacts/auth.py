"""
auth.py — Hybrid Cognito + Local SQLite Authentication Blueprint for the
FANUC HMI demo.

Login flow
----------
1. Check whether Cognito is reachable (HEAD request, ~2s timeout).
2. ONLINE  -> authenticate via Cognito InitiateAuth (USER_PASSWORD_AUTH) only.
              On success, cache the user into local SQLite (argon2id hash of
              the password just used) with the role derived from Cognito
              group membership (AdminListGroupsForUser). The local table is
              never consulted while online — this is what makes admin-created
              LOCAL-only accounts (which don't exist in Cognito) automatically
              stop working the moment the cloud is reachable again, per the
              "local users disabled when cloud is available" requirement.
3. OFFLINE -> authenticate against local SQLite only. This covers both
              Cognito-cached users (source="cognito") and admin-created
              local-only users (source="local").

Roles: admin (3) > advanced (2) > basic (1). @require_role(min_role) gates a
route so the caller's session role level must be >= the decorator's level.

Routes exposed by this blueprint (mount with app.register_blueprint(auth_bp)):
  POST   /login              -> {username, password} -> session cookie
  POST   /logout             -> clears session
  GET    /me                 -> current session identity
  GET    /admin/users        -> list all users (admin only)
  POST   /admin/users        -> create a LOCAL-only user (admin only, offline only)
  DELETE /admin/users/<name> -> delete a LOCAL-only user (admin only)

This module is backend-only (JSON API) — no HTML is served here. Wire a
login page / admin panel UI against these endpoints separately.

Dependencies: flask, boto3, argon2-cffi (module name: argon2), sqlite3 (stdlib).
"""

import os
import sqlite3
import secrets
import urllib.request
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.exceptions import ClientError
from flask import Blueprint, request, jsonify, session

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COGNITO_REGION = os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_REACHABILITY_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
COGNITO_TIMEOUT_SECONDS = 2

ROLE_LEVELS = {"basic": 1, "advanced": 2, "admin": 3}
VALID_ROLES = tuple(ROLE_LEVELS.keys())
DEFAULT_ROLE = "basic"


def _writable_dir(candidates):
    """Return the first candidate directory we can actually write to."""
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".auth_write_test")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return d
        except Exception:
            continue
    return "/tmp"


# Anchor local state on the process CWD first: Greengrass sets the Run
# script's working directory to the component's persistent work directory
# (unlike the versioned artifacts/ dir, this survives redeploys). Fall back
# to the artifacts dir, then /tmp, if that's ever not writable.
_DB_DIR = _writable_dir([
    os.environ.get("AUTH_DATA_DIR", ""),
    os.getcwd(),
    os.path.dirname(os.path.abspath(__file__)),
    "/tmp",
])
DB_PATH = os.path.join(_DB_DIR, "auth.db")
_SECRET_KEY_PATH = os.path.join(_DB_DIR, ".flask_secret_key")

_ph = PasswordHasher()  # argon2-cffi defaults to the Argon2id variant

auth_bp = Blueprint("auth", __name__)


class AuthError(Exception):
    """Raised for any expected auth failure; carries the HTTP status to return."""

    def __init__(self, message, status=401):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Secret key persistence (so sessions survive component restarts/redeploys)
# ---------------------------------------------------------------------------
def get_or_create_secret_key() -> bytes:
    """Load a persisted Flask secret key, generating one on first run.

    Call this once at app startup: app.secret_key = get_or_create_secret_key()
    """
    try:
        if os.path.exists(_SECRET_KEY_PATH):
            with open(_SECRET_KEY_PATH, "rb") as f:
                key = f.read().strip()
                if key:
                    return key
    except Exception as e:
        print(f"[auth] could not read secret key file: {e}", flush=True)

    key = secrets.token_bytes(32)
    try:
        with open(_SECRET_KEY_PATH, "wb") as f:
            f.write(key)
        os.chmod(_SECRET_KEY_PATH, 0o600)
    except Exception as e:
        print(f"[auth] could not persist secret key (sessions reset on restart): {e}", flush=True)
    return key


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the users table if it doesn't already exist. Call at startup."""
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'basic',
                source        TEXT NOT NULL DEFAULT 'local',
                email         TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        conn.commit()
    print(f"[auth] SQLite ready at {DB_PATH}", flush=True)


# ---------------------------------------------------------------------------
# Cognito reachability
# ---------------------------------------------------------------------------
def is_cognito_reachable() -> bool:
    """Quick connectivity probe to Cognito's regional endpoint (~2s timeout).

    We only care whether the network path to Cognito is up, not the HTTP
    status code: Cognito answers a bare HEAD with a 4xx, which still proves
    reachability. Also returns False if Cognito isn't configured at all.
    """
    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        return False
    try:
        req = urllib.request.Request(COGNITO_REACHABILITY_URL, method="HEAD")
        urllib.request.urlopen(req, timeout=COGNITO_TIMEOUT_SECONDS)
        return True
    except urllib.error.HTTPError:
        return True  # got a real HTTP response -> network path is up
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cognito auth
# ---------------------------------------------------------------------------
def _cognito_client():
    return boto3.client("cognito-idp", region_name=COGNITO_REGION)


def cognito_login(username: str, password: str) -> dict:
    """Authenticate against Cognito with USER_PASSWORD_AUTH.

    Returns {"tokens": {...}, "groups": [...]} on success.
    Raises AuthError on invalid credentials or other Cognito errors.
    """
    client = _cognito_client()
    try:
        resp = client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise AuthError("Invalid username or password", 401)
        if code == "UserNotConfirmedException":
            raise AuthError("User is not confirmed", 403)
        if code == "PasswordResetRequiredException":
            raise AuthError("Password reset required", 403)
        print(f"[auth] cognito_login error for {username}: {code}", flush=True)
        raise AuthError(f"Cognito error: {code}", 502)

    if "ChallengeName" in resp:
        # e.g. NEW_PASSWORD_REQUIRED — not handled by this demo's simple flow.
        raise AuthError(f"Additional challenge required: {resp['ChallengeName']}", 403)

    result = resp.get("AuthenticationResult", {})
    groups = get_user_groups(username)
    return {"tokens": result, "groups": groups}


def get_user_groups(username: str) -> list:
    """Return the list of Cognito group names the user belongs to."""
    client = _cognito_client()
    try:
        resp = client.admin_list_groups_for_user(
            UserPoolId=COGNITO_USER_POOL_ID, Username=username
        )
        return [g["GroupName"] for g in resp.get("Groups", [])]
    except ClientError as e:
        print(f"[auth] get_user_groups error for {username}: {e}", flush=True)
        return []


def _role_from_groups(groups: list) -> str:
    """Highest-privilege role implied by the user's Cognito groups."""
    for role in ("admin", "advanced", "basic"):
        if role in groups:
            return role
    return DEFAULT_ROLE


# ---------------------------------------------------------------------------
# Local SQLite auth + sync
# ---------------------------------------------------------------------------
def sync_user_to_local(username: str, password: str, role: str, source: str = "cognito", email: str = None):
    """Upsert a user record into local SQLite with an argon2id password hash.

    Called after every successful Cognito login so the same credentials keep
    working if the device later goes offline.
    """
    role = role if role in VALID_ROLES else DEFAULT_ROLE
    pw_hash = _ph.hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as conn:
        existing = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET password_hash=?, role=?, source=?, "
                "email=COALESCE(?, email), updated_at=? WHERE username=?",
                (pw_hash, role, source, email, now, username),
            )
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, source, email, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, pw_hash, role, source, email, now, now),
            )
        conn.commit()


def local_login(username: str, password: str) -> dict:
    """Authenticate against the local SQLite cache. Raises AuthError on failure."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        raise AuthError("Invalid username or password", 401)
    try:
        _ph.verify(row["password_hash"], password)
    except VerifyMismatchError:
        raise AuthError("Invalid username or password", 401)
    except InvalidHash:
        raise AuthError("Corrupt credential store entry", 500)
    return {"username": row["username"], "role": row["role"], "source": row["source"]}


def create_local_user(username: str, password: str, role: str, email: str = None):
    """Admin-only: create a LOCAL-only user (source='local')."""
    if role not in VALID_ROLES:
        raise AuthError(f"Invalid role '{role}'", 400)
    with closing(_connect()) as conn:
        existing = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        raise AuthError(f"User '{username}' already exists", 409)
    sync_user_to_local(username, password, role, source="local", email=email)


def delete_local_user(username: str):
    """Admin-only: delete a LOCAL-only user. Cognito-cached rows are protected
    here since they're a read-only cache of the cloud source of truth."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT source FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise AuthError(f"User '{username}' not found", 404)
        if row["source"] != "local":
            raise AuthError("Cannot delete a Cognito-synced user from here", 400)
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()


def list_users() -> list:
    """Return all cached users (both cognito-synced and local-only)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT username, role, source, email, created_at, updated_at "
            "FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# RBAC decorator
# ---------------------------------------------------------------------------
def require_role(min_role: str):
    """Route decorator: caller's session role level must be >= min_role's level."""
    required_level = ROLE_LEVELS.get(min_role, 0)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            username = session.get("username")
            role = session.get("role")
            if not username:
                return jsonify({"error": "Not authenticated"}), 401
            if ROLE_LEVELS.get(role, 0) < required_level:
                return jsonify({"error": "Insufficient role"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Routes — session auth
# ---------------------------------------------------------------------------
@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    online = is_cognito_reachable()
    try:
        if online:
            # Cloud is reachable: Cognito is the only source of truth. This
            # also means admin-created LOCAL-only accounts (which don't
            # exist in Cognito) correctly fail here with 401 while online.
            result = cognito_login(username, password)
            role = _role_from_groups(result["groups"])
            sync_user_to_local(username, password, role, source="cognito")
            source = "cognito"
        else:
            local_result = local_login(username, password)
            role = local_result["role"]
            source = local_result["source"]
    except AuthError as e:
        # Include the online flag on failures too, so the login page can
        # show an "Offline Mode" badge even when the attempt fails
        # (e.g. wrong local password while Cognito is unreachable).
        return jsonify({"error": e.message, "online": online}), e.status

    session["username"] = username
    session["role"] = role
    session["source"] = source
    return jsonify({"username": username, "role": role, "source": source, "online": online})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/me", methods=["GET"])
def me():
    if not session.get("username"):
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "username": session["username"],
        "role": session.get("role"),
        "source": session.get("source"),
    })


# ---------------------------------------------------------------------------
# Routes — admin panel
# ---------------------------------------------------------------------------
@auth_bp.route("/admin/users", methods=["GET"])
@require_role("admin")
def admin_list_users():
    return jsonify({"users": list_users(), "cloud_available": is_cognito_reachable()})


@auth_bp.route("/admin/users", methods=["POST"])
@require_role("admin")
def admin_create_user():
    if is_cognito_reachable():
        return jsonify({"error": "Cannot create local users while cloud authentication is available"}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or DEFAULT_ROLE
    email = data.get("email")
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    try:
        create_local_user(username, password, role, email=email)
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify({"ok": True, "username": username, "role": role, "source": "local"})


@auth_bp.route("/admin/users/<username>", methods=["DELETE"])
@require_role("admin")
def admin_delete_user(username):
    try:
        delete_local_user(username)
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify({"ok": True})
