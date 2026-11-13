import os
import re
import io
import time
import uuid
import sqlite3
import secrets
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from functools import wraps
from collections import defaultdict, deque

import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, send_file, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import qrcode
import qrcode.image.svg

APP_NAME = "GuestDrop"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))
DB_PATH = DATA_DIR / "app.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

IMMICH_URL = os.getenv("IMMICH_URL", "http://immich-server:2283").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")
IMMICH_PUBLIC_URL = os.getenv("IMMICH_PUBLIC_URL", "").rstrip("/")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
INTERNAL_UPLOAD_TOKEN = os.getenv("INTERNAL_UPLOAD_TOKEN") or SECRET_KEY
GUESTDROP_PUBLIC_URL = (os.getenv("GUESTDROP_PUBLIC_URL") or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
CHUNK_SIZE_MB = int(os.getenv("CHUNK_SIZE_MB", "5"))
MAX_CHUNK_BYTES = min(max(CHUNK_SIZE_MB, 1), 80) * 1024 * 1024
MAX_FILE_GB = float(os.getenv("MAX_FILE_GB", "10"))
MAX_FILE_BYTES = int(MAX_FILE_GB * 1024 * 1024 * 1024)
PARTIAL_RETENTION_HOURS = int(os.getenv("PARTIAL_RETENTION_HOURS", "72"))
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "true").lower() in ("1", "true", "yes")
LOGIN_ATTEMPTS = max(int(os.getenv("LOGIN_ATTEMPTS", "8")), 3)
LOGIN_WINDOW_SECONDS = max(int(os.getenv("LOGIN_WINDOW_SECONDS", "900")), 60)
UPLOAD_STARTS_PER_HOUR = max(int(os.getenv("UPLOAD_STARTS_PER_HOUR", "200")), 20)

ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif",
    ".dng", ".tif", ".tiff", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"
}
RESERVED_SLUGS = {"admin", "api", "health", "static", "login", "logout"}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=MAX_CHUNK_BYTES + 1024 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_db_lock = threading.RLock()
_worker_started = False
_login_failures = defaultdict(deque)
_login_lock = threading.Lock()
_upload_starts = defaultdict(deque)
_upload_rate_lock = threading.Lock()
_chunk_locks = defaultdict(threading.Lock)
_chunk_locks_guard = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def column_names(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def slugify(value):
    value = (value or "").strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-+", "-", value)
    return value[:80]


def unique_slug(db, desired, exclude_id=None):
    base = slugify(desired) or "event"
    if base in RESERVED_SLUGS:
        base = f"{base}-event"
    candidate = base
    n = 2
    while True:
        if exclude_id is None:
            row = db.execute("SELECT id FROM events WHERE slug=?", (candidate,)).fetchone()
        else:
            row = db.execute("SELECT id FROM events WHERE slug=? AND id<>?", (candidate, exclude_id)).fetchone()
        if not row:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def init_db():
    with _db_lock, db_conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            slug TEXT UNIQUE,
            title TEXT NOT NULL,
            welcome TEXT NOT NULL DEFAULT '',
            album_id TEXT NOT NULL,
            album_name TEXT NOT NULL,
            share_url TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            original_name TEXT NOT NULL,
            safe_name TEXT NOT NULL,
            mime TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL,
            mtime INTEGER,
            received INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'uploading',
            path TEXT NOT NULL,
            asset_id TEXT,
            duplicate INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
            UNIQUE(event_id, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);
        CREATE INDEX IF NOT EXISTS idx_uploads_event ON uploads(event_id);
        """)
        cols = column_names(db, "events")
        if "slug" not in cols:
            db.execute("ALTER TABLE events ADD COLUMN slug TEXT")
        # Migrate v0.1 random-token events to stable human-readable slugs without breaking old URLs.
        rows = db.execute("SELECT id,title,album_name,slug FROM events ORDER BY id").fetchall()
        for row in rows:
            if not row["slug"]:
                desired = row["album_name"] or row["title"] or f"event-{row['id']}"
                db.execute("UPDATE events SET slug=? WHERE id=?", (unique_slug(db, desired, row["id"]), row["id"]))
        try:
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(slug)")
        except sqlite3.IntegrityError:
            pass
        db.commit()


init_db()


def api_url(path: str):
    return f"{IMMICH_URL}/api/{path.lstrip('/')}"


def immich_request(method, path, *, json_body=None, data=None, files=None, timeout=120):
    if not IMMICH_API_KEY:
        raise RuntimeError("IMMICH_API_KEY is not configured")
    headers = {"Accept": "application/json", "x-api-key": IMMICH_API_KEY}
    r = requests.request(method, api_url(path), headers=headers, json=json_body, data=data, files=files, timeout=timeout)
    if r.status_code >= 400:
        detail = r.text[:1000]
        raise RuntimeError(f"Immich {method} {path} failed ({r.status_code}): {detail}")
    if not r.content:
        return None
    ctype = r.headers.get("content-type", "")
    return r.json() if "json" in ctype else r.text


def get_albums():
    return immich_request("GET", "albums") or []


def get_album(album_id):
    return immich_request("GET", f"albums/{album_id}") or {}


def create_album(name):
    return immich_request("POST", "albums", json_body={"albumName": name})


def create_share(album_id):
    # View-only on purpose: GuestDrop is the only upload UI guests should see.
    payload = {
        "type": "ALBUM",
        "albumId": album_id,
        "allowDownload": True,
        "allowUpload": False,
        "showMetadata": True,
    }
    result = immich_request("POST", "shared-links", json_body=payload)
    key = (result or {}).get("key")
    if not key:
        raise RuntimeError("Immich created a shared link but did not return a share key")
    base = IMMICH_PUBLIC_URL or IMMICH_URL
    return f"{base}/share/{key}"


def add_to_album(album_id, asset_id):
    return immich_request("PUT", f"albums/{album_id}/assets", json_body={"ids": [asset_id]})


def upload_to_immich(row):
    path = Path(row["path"])
    if not path.exists():
        raise RuntimeError("Staged file is missing")
    ts = row["mtime"] / 1000 if row["mtime"] else path.stat().st_mtime
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    data = {
        "deviceAssetId": f"guestdrop:{row['id']}",
        "deviceId": "guestdrop",
        "fileCreatedAt": dt,
        "fileModifiedAt": dt,
        "isFavorite": "false",
    }
    with path.open("rb") as fh:
        files = {"assetData": (row["original_name"], fh, row["mime"] or "application/octet-stream")}
        return immich_request("POST", "assets", data=data, files=files, timeout=3600)


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


app.jinja_env.globals.update(csrf_token=csrf_token, APP_NAME=APP_NAME)


def require_csrf():
    expected = session.get("csrf", "")
    supplied = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, "Invalid CSRF token")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def public_base():
    return GUESTDROP_PUBLIC_URL or request.url_root.rstrip("/")


def guest_url(event):
    return f"{public_base()}/{event['slug']}"


def safe_next_url(value):
    return value if value and value.startswith("/") and not value.startswith("//") else url_for("admin_index")


def valid_media_name(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def get_event_by_token(token):
    with db_conn() as db:
        return db.execute("SELECT * FROM events WHERE token=?", (token,)).fetchone()


def get_event_by_slug(slug):
    with db_conn() as db:
        return db.execute("SELECT * FROM events WHERE slug=?", (slug,)).fetchone()


def event_counts(event_id):
    with db_conn() as db:
        rows = db.execute("SELECT status, COUNT(*) c FROM uploads WHERE event_id=? GROUP BY status", (event_id,)).fetchall()
    out = {r["status"]: r["c"] for r in rows}
    out["total"] = sum(out.values())
    return out


def album_asset_count(album_id):
    try:
        album = get_album(album_id)
        for key in ("assetCount", "assetsCount"):
            if isinstance(album.get(key), int):
                return album[key]
        assets = album.get("assets")
        if isinstance(assets, list):
            return len(assets)
    except Exception:
        pass
    return None


def login_key():
    forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def login_rate_limited(key):
    now = time.time()
    with _login_lock:
        q = _login_failures[key]
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        return len(q) >= LOGIN_ATTEMPTS


def record_login_failure(key):
    with _login_lock:
        _login_failures[key].append(time.time())


def clear_login_failures(key):
    with _login_lock:
        _login_failures.pop(key, None)


def upload_start_allowed(event_token):
    source = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "unknown"
    key = (event_token, source)
    now = time.time()
    with _upload_rate_lock:
        q = _upload_starts[key]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= UPLOAD_STARTS_PER_HOUR:
            return False
        q.append(now)
    return True


def chunk_lock(upload_id):
    with _chunk_locks_guard:
        return _chunk_locks[upload_id]


def internal_upload_authorized():
    supplied = request.headers.get("X-GuestDrop-Internal", "")
    return bool(INTERNAL_UPLOAD_TOKEN and supplied and secrets.compare_digest(INTERNAL_UPLOAD_TOKEN, supplied))


@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "immichConfigured": bool(IMMICH_API_KEY), "publicUrl": GUESTDROP_PUBLIC_URL}


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    key = login_key()
    if request.method == "POST":
        require_csrf()
        if login_rate_limited(key):
            flash("Too many failed sign-in attempts. Try again later.", "error")
            return render_template("login.html"), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            clear_login_failures(key)
            session.clear()
            session.permanent = True
            session["admin"] = True
            session["admin_username"] = ADMIN_USERNAME
            csrf_token()
            return redirect(safe_next_url(request.args.get("next")))
        record_login_failure(key)
        flash("Incorrect username or password", "error")
    else:
        csrf_token()
    return render_template("login.html")


@app.post("/admin/logout")
@admin_required
def admin_logout():
    require_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin_index():
    with db_conn() as db:
        events = db.execute("SELECT * FROM events ORDER BY id DESC").fetchall()
    enriched = [(e, event_counts(e["id"])) for e in events]
    return render_template("admin.html", events=enriched, public_base=public_base())


@app.get("/admin/setup")
@admin_required
def admin_setup():
    immich_ok = False
    immich_error = ""
    album_count = None
    try:
        albums = get_albums()
        album_count = len(albums)
        immich_ok = True
    except Exception as exc:
        immich_error = str(exc)
    return render_template(
        "setup.html",
        immich_ok=immich_ok,
        immich_error=immich_error,
        album_count=album_count,
        public_url=public_base(),
        immich_url=IMMICH_URL,
        immich_public_url=IMMICH_PUBLIC_URL,
        chunk_size_mb=MAX_CHUNK_BYTES // 1024 // 1024,
    )


@app.route("/admin/events/new", methods=["GET", "POST"])
@admin_required
def admin_new_event():
    error = None
    try:
        albums = sorted(get_albums(), key=lambda a: a.get("albumName", "").lower())
    except Exception as exc:
        albums = []
        error = str(exc)

    if request.method == "POST":
        require_csrf()
        title = request.form.get("title", "").strip()
        welcome = request.form.get("welcome", "").strip()
        mode = request.form.get("album_mode", "existing")
        make_share = request.form.get("make_share") == "on"
        requested_slug = slugify(request.form.get("slug", ""))
        if not title:
            error = "Event title is required."
        else:
            try:
                if mode == "new":
                    album_name = request.form.get("new_album_name", "").strip() or title
                    album = create_album(album_name)
                    album_id = album["id"]
                    album_name = album.get("albumName", album_name)
                else:
                    album_id = request.form.get("album_id", "").strip()
                    album = next((a for a in albums if a.get("id") == album_id), None)
                    if not album:
                        raise RuntimeError("Choose an existing album or create a new one.")
                    album_name = album.get("albumName", title)
                share_url = create_share(album_id) if make_share else request.form.get("share_url", "").strip()
                token = secrets.token_urlsafe(24)
                with _db_lock, db_conn() as db:
                    slug = unique_slug(db, requested_slug or album_name or title)
                    db.execute(
                        "INSERT INTO events(token,slug,title,welcome,album_id,album_name,share_url,active,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                        (token, slug, title, welcome, album_id, album_name, share_url, now_iso()),
                    )
                    db.commit()
                    event_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                return redirect(url_for("admin_event_detail", event_id=event_id))
            except Exception as exc:
                error = str(exc)
    return render_template("event_new.html", albums=albums, error=error, public_base=public_base())


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_event(event_id):
    with db_conn() as db:
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        abort(404)
    error = None
    if request.method == "POST":
        require_csrf()
        title = request.form.get("title", "").strip()
        welcome = request.form.get("welcome", "").strip()
        share_url = request.form.get("share_url", "").strip()
        requested_slug = slugify(request.form.get("slug", ""))
        if not title:
            error = "Event title is required."
        elif not requested_slug:
            error = "Guest link ending is required."
        else:
            try:
                with _db_lock, db_conn() as db:
                    slug = unique_slug(db, requested_slug, event_id)
                    if slug != requested_slug:
                        raise RuntimeError(f"/{requested_slug} is already in use. Choose another link ending.")
                    db.execute("UPDATE events SET title=?,welcome=?,share_url=?,slug=? WHERE id=?", (title, welcome, share_url, slug, event_id))
                    db.commit()
                flash("Event updated.", "ok")
                return redirect(url_for("admin_event_detail", event_id=event_id))
            except Exception as exc:
                error = str(exc)
        with db_conn() as db:
            event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return render_template("event_edit.html", event=event, error=error, public_base=public_base())


@app.post("/admin/events/<int:event_id>/toggle")
@admin_required
def admin_toggle_event(event_id):
    require_csrf()
    with _db_lock, db_conn() as db:
        db.execute("UPDATE events SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (event_id,))
        db.commit()
    return redirect(request.referrer or url_for("admin_index"))


@app.post("/admin/uploads/<upload_id>/retry")
@admin_required
def admin_retry_upload(upload_id):
    require_csrf()
    with _db_lock, db_conn() as db:
        db.execute("UPDATE uploads SET status='ready', error=NULL, updated_at=? WHERE id=? AND status='error'", (now_iso(), upload_id))
        db.commit()
    return redirect(request.referrer or url_for("admin_index"))


@app.get("/admin/events/<int:event_id>")
@admin_required
def admin_event_detail(event_id):
    with db_conn() as db:
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not event:
            abort(404)
        uploads = db.execute("SELECT * FROM uploads WHERE event_id=? ORDER BY created_at DESC LIMIT 500", (event_id,)).fetchall()
    return render_template(
        "event_detail.html",
        event=event,
        uploads=uploads,
        guest_url=guest_url(event),
        asset_count=album_asset_count(event["album_id"]),
    )


def build_qr(event, fmt):
    target = guest_url(event)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=10)
    qr.add_data(target)
    qr.make(fit=True)
    if fmt == "svg":
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        mimetype = "image/svg+xml"
        ext = "svg"
    else:
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mimetype = "image/png"
        ext = "png"
    buf.seek(0)
    filename = f"{secure_filename(event['title']) or 'guestdrop'}-qr.{ext}"
    return buf, mimetype, filename


@app.get("/admin/events/<int:event_id>/qr.<fmt>")
@admin_required
def admin_event_qr(event_id, fmt):
    if fmt not in ("png", "svg"):
        abort(404)
    with db_conn() as db:
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        abort(404)
    buf, mimetype, filename = build_qr(event, fmt)
    return send_file(buf, mimetype=mimetype, download_name=filename, as_attachment=request.args.get("download") == "1")


@app.get("/e/<token>")
def legacy_event_page(token):
    event = get_event_by_token(token)
    if not event:
        abort(404)
    return redirect(url_for("event_page", slug=event["slug"]), code=301)


@app.get("/<slug>")
def event_page(slug):
    event = get_event_by_slug(slug)
    if not event:
        abort(404)
    return render_template(
        "event.html",
        event=event,
        chunk_size=MAX_CHUNK_BYTES,
        asset_count=album_asset_count(event["album_id"]),
    )


@app.get("/api/events/<token>/status")
def event_status(token):
    event = get_event_by_token(token)
    if not event:
        abort(404)
    counts = event_counts(event["id"])
    return jsonify({
        "active": bool(event["active"]),
        "counts": counts,
        "shareUrl": event["share_url"],
        "albumAssetCount": album_asset_count(event["album_id"]),
    })


@app.post("/api/events/<token>/uploads")
def create_upload(token):
    event = get_event_by_token(token)
    if not event or not event["active"]:
        abort(404)
    if not upload_start_allowed(token):
        return jsonify({"error": "Too many upload starts from this device. Try again later."}), 429
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    size = int(body.get("size", 0) or 0)
    mime = str(body.get("type", ""))[:200]
    mtime = int(body.get("lastModified", 0) or 0)
    fingerprint = str(body.get("fingerprint", ""))[:500]
    if not name or not fingerprint or size <= 0:
        return jsonify({"error": "Missing file metadata"}), 400
    if size > MAX_FILE_BYTES:
        return jsonify({"error": f"File exceeds {MAX_FILE_GB:g} GB limit"}), 413
    if not valid_media_name(name):
        return jsonify({"error": "Unsupported file type"}), 415

    with _db_lock, db_conn() as db:
        existing = db.execute("SELECT * FROM uploads WHERE event_id=? AND fingerprint=?", (event["id"], fingerprint)).fetchone()
        if existing:
            if existing["status"] == "error" and existing["received"] == existing["size"] and Path(existing["path"]).exists():
                db.execute("UPDATE uploads SET status='ready', error=NULL, updated_at=? WHERE id=?", (now_iso(), existing["id"]))
                db.commit()
                return jsonify({"id": existing["id"], "received": existing["received"], "status": "ready"})
            if existing["status"] == "expired" or (existing["status"] == "uploading" and not Path(existing["path"]).exists()):
                db.execute("DELETE FROM uploads WHERE id=?", (existing["id"],))
                db.commit()
            else:
                return jsonify({"id": existing["id"], "received": existing["received"], "status": existing["status"]})
        upload_id = str(uuid.uuid4())
        safe = secure_filename(name) or f"upload-{upload_id}"
        path = UPLOAD_DIR / f"{upload_id}.part"
        path.touch(exist_ok=False)
        ts = now_iso()
        db.execute(
            "INSERT INTO uploads(id,event_id,fingerprint,original_name,safe_name,mime,size,mtime,received,status,path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,0,'uploading',?,?,?)",
            (upload_id, event["id"], fingerprint, name, safe, mime, size, mtime, str(path), ts, ts),
        )
        db.commit()
    return jsonify({"id": upload_id, "received": 0, "status": "uploading"}), 201


@app.get("/api/uploads/<upload_id>")
def upload_status(upload_id):
    with db_conn() as db:
        row = db.execute("SELECT id,received,size,status,error FROM uploads WHERE id=?", (upload_id,)).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.get("/internal/uploads/<upload_id>")
def internal_upload_info(upload_id):
    if not internal_upload_authorized():
        abort(404)
    with db_conn() as db:
        row = db.execute(
            "SELECT u.id,u.size,u.received,u.status,e.active FROM uploads u JOIN events e ON e.id=u.event_id WHERE u.id=?",
            (upload_id,),
        ).fetchone()
    if not row:
        abort(404)
    return jsonify({**dict(row), "maxChunkBytes": MAX_CHUNK_BYTES})


@app.post("/internal/uploads/<upload_id>/commit")
def internal_upload_commit(upload_id):
    if not internal_upload_authorized():
        abort(404)
    body = request.get_json(silent=True) or {}
    offset = int(body.get("offset", -1))
    written = int(body.get("written", 0))
    if offset < 0 or written <= 0 or written > MAX_CHUNK_BYTES:
        return jsonify({"error": "invalid commit"}), 400
    with _db_lock, db_conn() as db:
        row = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if not row:
            abort(404)
        if row["received"] != offset:
            return jsonify({"error": "offset mismatch", "received": row["received"]}), 409
        received = offset + written
        if received > row["size"]:
            return jsonify({"error": "chunk exceeds declared file size"}), 400
        path = Path(row["path"])
        if not path.exists() or path.stat().st_size < received:
            return jsonify({"error": "staged bytes are missing"}), 409
        status = "ready" if received == row["size"] else "uploading"
        db.execute("UPDATE uploads SET received=?,status=?,updated_at=? WHERE id=?", (received, status, now_iso(), upload_id))
        db.commit()
    return jsonify({"received": received, "status": status})


@app.post("/api/uploads/<upload_id>/chunk")
def upload_chunk(upload_id):
    offset = request.args.get("offset", type=int)
    if offset is None or offset < 0:
        return jsonify({"error": "offset is required"}), 400
    content_length = request.content_length
    if content_length is not None and content_length <= 0:
        return jsonify({"error": "empty chunk"}), 400
    if content_length is not None and content_length > MAX_CHUNK_BYTES:
        return jsonify({"error": "chunk too large"}), 413

    # Serialize chunks for one upload while still allowing different guests/files
    # to stream concurrently. Bytes go straight to disk instead of buffering the
    # whole request in memory, which also keeps reverse-proxy flow control moving.
    with chunk_lock(upload_id):
        with _db_lock, db_conn() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if not row:
                abort(404)
            event = db.execute("SELECT active FROM events WHERE id=?", (row["event_id"],)).fetchone()
            if not event or not event["active"]:
                return jsonify({"error": "event disabled"}), 403
            if row["status"] in ("ready", "importing", "complete"):
                return jsonify({"received": row["received"], "status": row["status"]})
            if offset != row["received"]:
                return jsonify({"error": "offset mismatch", "received": row["received"]}), 409
            if content_length is not None and offset + content_length > row["size"]:
                return jsonify({"error": "chunk exceeds declared file size"}), 400

        path = Path(row["path"])
        written = 0
        try:
            with path.open("r+b") as fh:
                fh.seek(offset)
                while True:
                    remaining_limit = MAX_CHUNK_BYTES - written
                    if content_length is not None:
                        remaining_limit = min(remaining_limit, content_length - written)
                    if remaining_limit <= 0:
                        break
                    block = request.stream.read(min(256 * 1024, remaining_limit))
                    if not block:
                        break
                    fh.write(block)
                    written += len(block)
                if not written:
                    raise ValueError("empty chunk")
                if content_length is not None and written != content_length:
                    raise ValueError("incomplete chunk; retrying is safe")
                if offset + written > row["size"]:
                    raise ValueError("chunk exceeds declared file size")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:
            # The database offset remains authoritative. Remove bytes from a
            # disconnected/incomplete request so retrying the same offset is safe.
            try:
                with path.open("r+b") as fh:
                    fh.truncate(offset)
            except Exception:
                pass
            return jsonify({"error": str(exc) or "incomplete chunk; retrying is safe"}), 400

        received = offset + written
        status = "ready" if received == row["size"] else "uploading"
        with _db_lock, db_conn() as db:
            db.execute("UPDATE uploads SET received=?,status=?,updated_at=? WHERE id=?", (received, status, now_iso(), upload_id))
            db.commit()
    return jsonify({"received": received, "status": status})


def import_one(upload_id):
    with _db_lock, db_conn() as db:
        row = db.execute("SELECT u.*, e.album_id FROM uploads u JOIN events e ON e.id=u.event_id WHERE u.id=?", (upload_id,)).fetchone()
        if not row or row["status"] != "ready":
            return
        db.execute("UPDATE uploads SET status='importing',updated_at=? WHERE id=?", (now_iso(), upload_id))
        db.commit()

    try:
        result = upload_to_immich(row)
        asset_id = (result or {}).get("id")
        if not asset_id:
            raise RuntimeError(f"Immich upload response did not include an asset id: {result}")
        duplicate = 1 if (result or {}).get("duplicate") else 0
        add_to_album(row["album_id"], asset_id)
        with _db_lock, db_conn() as db:
            db.execute(
                "UPDATE uploads SET status='complete',asset_id=?,duplicate=?,error=NULL,updated_at=? WHERE id=?",
                (asset_id, duplicate, now_iso(), upload_id),
            )
            db.commit()
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        with _db_lock, db_conn() as db:
            db.execute(
                "UPDATE uploads SET status='error',error=?,retry_count=retry_count+1,updated_at=? WHERE id=?",
                (str(exc)[:2000], now_iso(), upload_id),
            )
            db.commit()


def cleanup_partials():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PARTIAL_RETENTION_HOURS)
    with _db_lock, db_conn() as db:
        rows = db.execute("SELECT * FROM uploads WHERE status='uploading'").fetchall()
        for row in rows:
            try:
                updated = datetime.fromisoformat(row["updated_at"])
                if updated < cutoff:
                    Path(row["path"]).unlink(missing_ok=True)
                    db.execute("UPDATE uploads SET status='expired',error='Partial upload expired',updated_at=? WHERE id=?", (now_iso(), row["id"]))
            except Exception:
                continue
        db.commit()


def worker_loop():
    last_cleanup = 0
    while True:
        try:
            with db_conn() as db:
                row = db.execute("SELECT id FROM uploads WHERE status='ready' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                import_one(row["id"])
            else:
                time.sleep(1)
            if time.time() - last_cleanup > 3600:
                cleanup_partials()
                last_cleanup = time.time()
        except Exception:
            time.sleep(2)


def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=worker_loop, daemon=True, name="guestdrop-import-worker").start()


start_worker_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
