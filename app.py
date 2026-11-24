"""
TripNova - app.py
=================

Flask backend. It connects the website (index.html + script.js) to the
trip-planning engine in travel_planner.py and to the SQLite database.

Routes
------
GET    /                         the website
GET    /health                   simple "is the server alive" check
GET    /api/config               destinations, activity types, limits, features
POST   /api/generate             build a trip plan (no login needed)
POST   /api/register             create an account
POST   /api/login                log in
POST   /api/logout               log out
GET    /api/me                   who is logged in?
GET    /api/trips                list my saved trips
POST   /api/trips                save a trip
GET    /api/trips/<id>           open one saved trip
DELETE /api/trips/<id>           delete a saved trip
POST   /api/trips/<id>/ai-brief  OPTIONAL: real AI summary (needs API key)
"""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# Load settings from a local ".env" file if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from travel_planner import (
    AITravelPlanner,
    BASE_CURRENCY,
    BUDGET_TYPES,
    CURRENCIES,
    INTERNATIONAL_ACTIVITY_PRICE_FACTOR,
    MAX_DAYS,
    MAX_TRAVELLERS,
    ValidationError,
)

# ----------------------------------------------------------------------
# APP SETUP
# ----------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

# Needed when the app runs behind a hosting provider's proxy (Render, etc.)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()

if not SECRET_KEY:
    SECRET_KEY = "dev-only-secret-change-me"
    print(
        "WARNING: SECRET_KEY is not set. Using an unsafe development key. "
        "Set SECRET_KEY before deploying (see README.md)."
    )

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=true on a site served over HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=64 * 1024,
)

app.json.sort_keys = False
app.json.ensure_ascii = False

planner = AITravelPlanner()

MAX_SAVED_TRIPS = 100
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = generate_password_hash("not-a-real-password")

# ----------------------------------------------------------------------
# DATABASE (SQLite)
# ----------------------------------------------------------------------

DATABASE_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(BASE_DIR, "tripnova.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    destinations     TEXT NOT NULL,
    start_date       TEXT NOT NULL,
    days             INTEGER NOT NULL,
    travellers       INTEGER NOT NULL,
    budget_type      TEXT NOT NULL,
    total_amount     REAL NOT NULL,
    display_currency TEXT NOT NULL,
    plan_json        TEXT NOT NULL,
    ai_brief         TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trips_user ON trips (user_id, id);
"""


def init_db():
    folder = os.path.dirname(DATABASE_PATH)

    if folder:
        os.makedirs(folder, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")

    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)

    if db is not None:
        db.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# ERRORS
# ----------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=400, fields=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.fields = fields


def error_response(message, status, fields=None):
    body = {"error": message}

    if fields:
        body["fields"] = fields

    return jsonify(body), status


@app.errorhandler(ApiError)
def handle_api_error(error):
    return error_response(error.message, error.status, error.fields)


@app.errorhandler(HTTPException)
def handle_http_error(error):
    if request.path.startswith("/api/"):
        return error_response(error.description, error.code)

    page = (
        "<!doctype html><title>TripNova</title>"
        "<body style='font-family:sans-serif;text-align:center;padding:4rem'>"
        f"<h1>{error.code} - {error.name}</h1>"
        "<p><a href='/'>Back to TripNova</a></p></body>"
    )

    return page, error.code


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("Unexpected error: %s", error)

    if request.path.startswith("/api/"):
        return error_response(
            "Something went wrong on the server. Please try again.", 500
        )

    return "Something went wrong. Please try again.", 500


# ----------------------------------------------------------------------
# SECURITY HELPERS
# ----------------------------------------------------------------------

@app.before_request
def require_site_header():
    """Block cross-site form posts: every change must come from our script.js
    (browsers do not let other websites add this custom header)."""

    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and request.path.startswith("/api/")
        and request.headers.get("X-Requested-With") != "TripNova"
    ):
        raise ApiError("Missing security header.", 400)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")

    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


def read_json():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        raise ApiError("Please send valid JSON.", 400)

    return data


def text_value(data, key):
    value = data.get(key)

    return value.strip() if isinstance(value, str) else ""


def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    user = get_db().execute(
        "SELECT id, name, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    if user is None:
        session.clear()

    return user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()

        if user is None:
            raise ApiError("Please log in to continue.", 401)

        g.user = user

        return view(*args, **kwargs)

    return wrapper


# Very small login rate limit (per server process).
_failed_logins = {}


def too_many_attempts(key):
    now = time.time()
    attempts = [t for t in _failed_logins.get(key, []) if now - t < 600]
    _failed_logins[key] = attempts

    return len(attempts) >= 5


def record_failed_login(key):
    _failed_logins.setdefault(key, []).append(time.time())


# ----------------------------------------------------------------------
# PAGES
# ----------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# ----------------------------------------------------------------------
# PUBLIC DATA + TRIP GENERATION
# ----------------------------------------------------------------------

@app.get("/api/config")
def api_config():
    return jsonify({
        "app": {
            "name": "TripNova",
            "tagline": "Explore More, Travel Smarter",
        },
        "destinations": planner.get_destination_list(),
        "categories": planner.get_category_list(),
        "budget_types": list(BUDGET_TYPES),
        "currencies": [
            {"code": code, **info} for code, info in CURRENCIES.items()
        ],
        "base_currency": BASE_CURRENCY,
        "limits": {
            "max_days": MAX_DAYS,
            "max_travellers": MAX_TRAVELLERS,
            "max_destinations": len(planner.destinations),
        },
        "features": {
            "live_weather": planner._flag("USE_LIVE_WEATHER"),
            "live_rates": planner._flag("USE_LIVE_RATES"),
            "ai_brief": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            "international_activity_factor": INTERNATIONAL_ACTIVITY_PRICE_FACTOR,
        },
    })


@app.post("/api/generate")
def api_generate():
    data = read_json()

    try:
        clean = planner.validate_trip_request(data)
    except ValidationError as error:
        raise ApiError(
            "Please fix the highlighted fields.", 400, error.errors
        )

    return jsonify(planner.generate_trip(clean))


# ----------------------------------------------------------------------
# ACCOUNTS
# ----------------------------------------------------------------------

def user_json(user):
    return {"id": user["id"], "name": user["name"], "email": user["email"]}


@app.post("/api/register")
def api_register():
    data = read_json()

    name = text_value(data, "name")
    email = text_value(data, "email").lower()
    password = data.get("password") if isinstance(data.get("password"), str) else ""

    errors = {}

    if not 2 <= len(name) <= 60:
        errors["name"] = "Please enter your name (2 to 60 characters)."

    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        errors["email"] = "Please enter a valid email address."

    if len(password) < 8:
        errors["password"] = "Password must be at least 8 characters."
    elif len(password) > 128:
        errors["password"] = "Password must be 128 characters or fewer."
    elif not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        errors["password"] = "Password must contain at least one letter and one number."

    if errors:
        raise ApiError("Please fix the highlighted fields.", 400, errors)

    db = get_db()

    try:
        cursor = db.execute(
            "INSERT INTO users (name, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), utc_now()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(
            "An account with this email already exists.",
            409,
            {"email": "This email is already registered. Try logging in."},
        )

    session.clear()
    session["user_id"] = cursor.lastrowid
    session.permanent = True

    return jsonify({"user": {"id": cursor.lastrowid, "name": name, "email": email}}), 201


@app.post("/api/login")
def api_login():
    data = read_json()

    email = text_value(data, "email").lower()
    password = data.get("password") if isinstance(data.get("password"), str) else ""

    attempt_key = f"{request.remote_addr}|{email}"

    if too_many_attempts(attempt_key):
        raise ApiError(
            "Too many failed attempts. Please wait 10 minutes and try again.", 429
        )

    user = get_db().execute(
        "SELECT id, name, email, password_hash FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    # Always run one hash check so the response time does not reveal
    # whether the email exists.
    stored_hash = user["password_hash"] if user else DUMMY_HASH
    password_ok = check_password_hash(stored_hash, password)

    if user is None or not password_ok:
        record_failed_login(attempt_key)
        raise ApiError("Incorrect email or password.", 401)

    _failed_logins.pop(attempt_key, None)

    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True

    return jsonify({"user": user_json(user)})


@app.post("/api/logout")
def api_logout():
    session.clear()

    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    user = current_user()

    return jsonify({"user": user_json(user) if user else None})


# ----------------------------------------------------------------------
# SAVED TRIPS
# ----------------------------------------------------------------------

def trip_summary_json(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "destinations": json.loads(row["destinations"]),
        "start_date": row["start_date"],
        "days": row["days"],
        "travellers": row["travellers"],
        "budget_type": row["budget_type"],
        "total_amount": row["total_amount"],
        "display_currency": row["display_currency"],
        "has_ai_brief": bool(row["ai_brief"]),
        "created_at": row["created_at"],
    }


@app.get("/api/trips")
@login_required
def api_list_trips():
    rows = get_db().execute(
        "SELECT id, title, destinations, start_date, days, travellers, "
        "budget_type, total_amount, display_currency, ai_brief, created_at "
        "FROM trips WHERE user_id = ? ORDER BY id DESC",
        (g.user["id"],),
    ).fetchall()

    return jsonify({"trips": [trip_summary_json(row) for row in rows]})


@app.post("/api/trips")
@login_required
def api_save_trip():
    data = read_json()

    try:
        clean = planner.validate_trip_request(data)
    except ValidationError as error:
        raise ApiError("Please fix the highlighted fields.", 400, error.errors)

    # The plan is rebuilt on the server from the same inputs and seed, so the
    # browser can never save made-up plan data.
    seed = data.get("seed")

    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 < seed < 2 ** 31:
        raise ApiError("This plan cannot be saved. Please generate it again.", 400)

    db = get_db()

    count = db.execute(
        "SELECT COUNT(*) FROM trips WHERE user_id = ?", (g.user["id"],)
    ).fetchone()[0]

    if count >= MAX_SAVED_TRIPS:
        raise ApiError(
            f"You can save up to {MAX_SAVED_TRIPS} trips. Delete one first.", 400
        )

    plan = planner.generate_trip(clean, seed=seed)

    title = text_value(data, "title")[:100] or (
        " + ".join(clean["destinations"]) + f" - {clean['days']} days"
    )

    cursor = db.execute(
        "INSERT INTO trips (user_id, title, destinations, start_date, days, "
        "travellers, budget_type, total_amount, display_currency, plan_json, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            g.user["id"],
            title,
            json.dumps(clean["destinations"], ensure_ascii=False),
            clean["start_date"].isoformat(),
            clean["days"],
            clean["travellers"],
            clean["budget_type"],
            plan["summary"]["grand_total"],
            plan["summary"]["display_currency"],
            json.dumps(plan, ensure_ascii=False),
            utc_now(),
        ),
    )
    db.commit()

    return jsonify({"id": cursor.lastrowid, "message": "Trip saved."}), 201


def load_own_trip(trip_id):
    row = get_db().execute(
        "SELECT * FROM trips WHERE id = ? AND user_id = ?",
        (trip_id, g.user["id"]),
    ).fetchone()

    if row is None:
        raise ApiError("Trip not found.", 404)

    return row


@app.get("/api/trips/<int:trip_id>")
@login_required
def api_get_trip(trip_id):
    row = load_own_trip(trip_id)

    return jsonify({
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "plan": json.loads(row["plan_json"]),
        "ai_brief": row["ai_brief"],
    })


@app.delete("/api/trips/<int:trip_id>")
@login_required
def api_delete_trip(trip_id):
    load_own_trip(trip_id)

    db = get_db()
    db.execute(
        "DELETE FROM trips WHERE id = ? AND user_id = ?",
        (trip_id, g.user["id"]),
    )
    db.commit()

    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# OPTIONAL REAL AI FEATURE
# ----------------------------------------------------------------------
# Everything above is rule-based. This is the ONLY place a real AI model is
# used. It is switched on by setting ANTHROPIC_API_KEY. The model reads the
# finished plan and writes a short friendly brief. It does not change the
# plan, prices or hotels.
# ----------------------------------------------------------------------

class AIError(Exception):
    pass


def build_ai_prompt(plan):
    request_data = plan["request"]

    lines = [
        f"Destinations: {', '.join(request_data['destinations'])}",
        f"Dates: {request_data['start_date']} to {request_data['end_date']} "
        f"({request_data['days']} days)",
        f"Travellers: {request_data['travellers']}",
        f"Budget level: {request_data['budget_type']}",
        f"Interests: {', '.join(request_data['categories'])}",
        "",
        "Day-by-day plan:",
    ]

    for day in plan["itinerary"]:
        lines.append(
            f"Day {day['day']} ({day['date']}) in {day['destination']}: "
            f"morning {day['morning']['activity']} at {day['morning']['attraction']}; "
            f"afternoon {day['afternoon']['activity']} at {day['afternoon']['attraction']}; "
            f"evening {day['evening']['activity']}."
        )

    return (
        "You are a friendly travel-writing assistant inside a trip planner "
        "called TripNova. Using ONLY the trip data below, write a short trip "
        "brief in plain text (no markdown symbols, under 220 words) with: "
        "a warm two-sentence overview, a packing checklist of 5 to 7 items "
        "suited to the destinations and dates, and 3 practical tips. "
        "Do not invent prices, opening hours, visa rules, flight details or "
        "live conditions; if something needs checking, tell the traveller to "
        "check an official source.\n\n--- TRIP DATA ---\n" + "\n".join(lines)
    )


def call_anthropic(prompt, api_key):
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()

    payload = json.dumps({
        "model": model,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    api_request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(api_request, timeout=40) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AIError("The AI service rejected the API key. Check ANTHROPIC_API_KEY.")
        if error.code == 429:
            raise AIError("The AI service is busy or the usage limit was reached. Try again later.")
        raise AIError(f"The AI service returned an error (HTTP {error.code}).")
    except (urllib.error.URLError, TimeoutError):
        raise AIError("Could not reach the AI service. Please try again.")
    except ValueError:
        raise AIError("The AI service sent an unreadable reply.")

    text = "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    ).strip()

    if not text:
        raise AIError("The AI service returned an empty answer.")

    return text


@app.post("/api/trips/<int:trip_id>/ai-brief")
@login_required
def api_ai_brief(trip_id):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if not api_key:
        raise ApiError(
            "The AI brief is switched off on this server. "
            "The site owner must set ANTHROPIC_API_KEY.",
            503,
        )

    row = load_own_trip(trip_id)
    plan = json.loads(row["plan_json"])

    try:
        text = call_anthropic(build_ai_prompt(plan), api_key)
    except AIError as error:
        raise ApiError(str(error), 502)

    db = get_db()
    db.execute(
        "UPDATE trips SET ai_brief = ? WHERE id = ? AND user_id = ?",
        (text, trip_id, g.user["id"]),
    )
    db.commit()

    return jsonify({"ai_brief": text})


# ----------------------------------------------------------------------
# START
# ----------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
