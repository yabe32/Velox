from __future__ import annotations

import base64
import copy
import csv
import functools
import io
import json
import math
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = ROOT / "data" / "vokabeltrainer.sqlite3"
SAMPLE_CSV = ROOT / "vokabeln_export_20260720_104046.csv"
CSV_COLUMNS = ["fremdsprache", "deutsch", "deklination", "lektion", "richtig", "falsch"]
EXCLUDED_LESSONS = {"rhetorische mittel", "rethorische mittel"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get("VOCAB_DATABASE", str(DEFAULT_DATABASE)),
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
        OPENAI_MODEL=os.environ.get("OPENAI_MODEL", "gpt-5.6"),
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "velox-local-development-only"),
        ADMIN_ACCESS_CODE=os.environ.get("ADMIN_ACCESS_CODE", "").strip(),
        LEARNER_ACCESS_CODE=os.environ.get("LEARNER_ACCESS_CODE", "").strip(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    @app.teardown_appcontext
    def close_database(_error: BaseException | None) -> None:
        database = g.pop("database", None)
        if database is not None:
            database.close()

    with app.app_context():
        init_database()
        if not app.config.get("TESTING"):
            seed_sample_if_empty()

    register_routes(app)
    return app


def get_db() -> sqlite3.Connection:
    if "database" not in g:
        from flask import current_app

        g.database = sqlite3.connect(current_app.config["DATABASE"])
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys = ON")
        g.database.execute("PRAGMA journal_mode = WAL")
    return g.database


def init_database() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vocabulary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            foreign_text TEXT NOT NULL,
            german_text TEXT NOT NULL,
            declension TEXT NOT NULL DEFAULT '',
            lesson TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            legacy_correct INTEGER NOT NULL DEFAULT 0,
            legacy_wrong INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vocab_source_lesson ON vocabulary(source_id, lesson, position);
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vocabulary_id INTEGER NOT NULL REFERENCES vocabulary(id) ON DELETE CASCADE,
            session_token TEXT NOT NULL,
            correct INTEGER NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempt_vocab_recent ON attempts(vocabulary_id, id DESC);
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            token TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_drafts (
            token TEXT PRIMARY KEY,
            items_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO app_settings(key, value) VALUES ('check_declension', 'false');
        INSERT OR IGNORE INTO app_settings(key, value) VALUES ('allow_typos', 'true');
        CREATE TABLE IF NOT EXISTS study_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS study_list_items (
            list_id INTEGER NOT NULL REFERENCES study_lists(id) ON DELETE CASCADE,
            vocabulary_id INTEGER NOT NULL REFERENCES vocabulary(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL,
            PRIMARY KEY (list_id, vocabulary_id)
        );
        """
    )
    conn.commit()


def decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Die Datei konnte nicht als Text gelesen werden.")


def parse_csv(raw: bytes) -> list[dict[str, Any]]:
    text = decode_csv(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = {str(header or "").strip().lower(): header for header in (reader.fieldnames or [])}
    aliases = {
        "foreign_text": ("fremdsprache", "latein", "vokabel", "foreign"),
        "german_text": ("deutsch", "übersetzung", "uebersetzung", "translation"),
        "declension": ("deklination", "grammatik", "formen", "declension"),
        "lesson": ("lektion", "lesson", "gruppe"),
        "correct": ("richtig", "correct"),
        "wrong": ("falsch", "wrong", "incorrect"),
    }

    def column(kind: str) -> str | None:
        return next((headers[name] for name in aliases[kind] if name in headers), None)

    foreign_col, german_col = column("foreign_text"), column("german_text")
    if not foreign_col or not german_col:
        raise ValueError("Benötigt werden mindestens die Spalten „fremdsprache“ und „deutsch“.")
    parsed: list[dict[str, Any]] = []
    for number, row in enumerate(reader, start=2):
        foreign = str(row.get(foreign_col) or "").strip()
        german = str(row.get(german_col) or "").strip()
        if not foreign and not german:
            continue
        lesson = str(row.get(column("lesson")) or "").strip() if column("lesson") else ""
        if lesson.casefold() in EXCLUDED_LESSONS:
            continue
        if not foreign or not german:
            raise ValueError(f"Zeile {number}: Fremdsprache und Deutsch dürfen nicht leer sein.")
        parsed.append(
            {
                "foreign_text": foreign,
                "german_text": german,
                "declension": str(row.get(column("declension")) or "").strip() if column("declension") else "",
                "lesson": lesson,
                "legacy_correct": safe_int(row.get(column("correct")), 0) if column("correct") else 0,
                "legacy_wrong": safe_int(row.get(column("wrong")), 0) if column("wrong") else 0,
            }
        )
    if not parsed:
        raise ValueError("Die CSV enthält keine Vokabeln.")
    return parsed


def safe_int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def insert_source(name: str, items: list[dict[str, Any]]) -> int:
    conn = get_db()
    cursor = conn.execute("INSERT INTO sources(name, created_at) VALUES (?, ?)", (name.strip() or "Neue Sammlung", utcnow()))
    source_id = int(cursor.lastrowid)
    now = utcnow()
    conn.executemany(
        """INSERT INTO vocabulary
           (source_id, foreign_text, german_text, declension, lesson, position,
            legacy_correct, legacy_wrong, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                source_id,
                item["foreign_text"],
                item["german_text"],
                item.get("declension", ""),
                item.get("lesson", ""),
                index,
                safe_int(item.get("legacy_correct")),
                safe_int(item.get("legacy_wrong")),
                now,
                now,
            )
            for index, item in enumerate(items, start=1)
        ],
    )
    conn.commit()
    return source_id


def seed_sample_if_empty() -> None:
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0 and SAMPLE_CSV.exists():
        insert_source("Latein – Beispiel", parse_csv(SAMPLE_CSV.read_bytes()))


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def difficulty_for(row: sqlite3.Row, recent: list[int]) -> tuple[str, float]:
    weighted_wrong = 0.0
    total_weight = 0.0
    # The newest answers receive exponentially more weight than older answers.
    for age, correct in enumerate(recent):
        weight = math.pow(0.78, age)
        weighted_wrong += weight * (0 if correct else 1)
        total_weight += weight
    legacy_total = row["legacy_correct"] + row["legacy_wrong"]
    if legacy_total:
        legacy_error = row["legacy_wrong"] / legacy_total
        weighted_wrong += legacy_error * 0.7
        total_weight += 0.7
    if total_weight == 0:
        score = 0.5
    else:
        # A small Bayesian prior prevents one lucky answer from immediately becoming "easy".
        score = (weighted_wrong + 0.5) / (total_weight + 1.0)
    if score < 0.18:
        label = "leicht"
    elif score < 0.34:
        label = "eher leicht"
    elif score < 0.53:
        label = "mittel"
    elif score < 0.72:
        label = "schwierig"
    else:
        label = "sehr schwierig"
    return label, round(score, 4)


def vocab_dict(row: sqlite3.Row, include_difficulty: bool = True) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "source_id": row["source_id"],
        "foreign_text": row["foreign_text"],
        "german_text": row["german_text"],
        "declension": row["declension"],
        "lesson": row["lesson"],
        "position": row["position"],
        "legacy_correct": row["legacy_correct"],
        "legacy_wrong": row["legacy_wrong"],
    }
    if include_difficulty:
        recent_rows = get_db().execute(
            "SELECT correct FROM attempts WHERE vocabulary_id = ? ORDER BY id DESC LIMIT 14", (row["id"],)
        ).fetchall()
        label, score = difficulty_for(row, [recent["correct"] for recent in recent_rows])
        item.update(difficulty=label, difficulty_score=score, attempts=len(recent_rows))
    return item


GERMAN_REPLACEMENTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
PROTECTED_UMLAUT_PAIRS = {"ae", "oe", "ue"}
KEYBOARD_ROWS = (("1234567890", 0.0), ("qwertzuiop", 0.25), ("asdfghjkl", 0.5), ("yxcvbnm", 0.75))
KEYBOARD_POSITIONS = {
    char: (index + offset, row_number)
    for row_number, (row, offset) in enumerate(KEYBOARD_ROWS)
    for index, char in enumerate(row)
}


def normalized_text(value: str) -> str:
    value = value.casefold().translate(GERMAN_REPLACEMENTS)
    return unicodedata.normalize("NFKD", value)


def normalize_answer(value: str) -> str:
    return "".join(char for char in normalized_text(value) if char.isalnum())


def normalized_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalized_text(value))


def keyboard_neighbors(first: str, second: str) -> bool:
    if first not in KEYBOARD_POSITIONS or second not in KEYBOARD_POSITIONS:
        return False
    first_x, first_y = KEYBOARD_POSITIONS[first]
    second_x, second_y = KEYBOARD_POSITIONS[second]
    return abs(first_y - second_y) <= 1 and abs(first_x - second_x) <= 1.1


def likely_single_typo(given: str, expected: str, expected_tokens: list[str]) -> bool:
    """Accept one conservative keyboard typo in one sufficiently long expected word."""
    if not given or not expected or abs(len(given) - len(expected)) > 1:
        return False

    edit_index = -1
    edited_pair = ""
    if len(given) == len(expected):
        differences = [index for index, (actual, wanted) in enumerate(zip(given, expected)) if actual != wanted]
        if len(differences) == 1:
            edit_index = differences[0]
            if not keyboard_neighbors(given[edit_index], expected[edit_index]):
                return False
        elif (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and given[differences[0]] == expected[differences[1]]
            and given[differences[1]] == expected[differences[0]]
        ):
            edit_index = differences[0]
        else:
            return False
    elif len(given) == len(expected) + 1:
        edit_index = next((index for index, chars in enumerate(zip(given, expected)) if chars[0] != chars[1]), len(expected))
        if given[:edit_index] + given[edit_index + 1:] != expected:
            return False
        edited_pair = given[max(0, edit_index - 1):edit_index + 1]
    else:
        edit_index = next((index for index, chars in enumerate(zip(expected, given)) if chars[0] != chars[1]), len(given))
        if expected[:edit_index] + expected[edit_index + 1:] != given:
            return False
        edited_pair = expected[max(0, edit_index - 1):edit_index + 1]

    # Missing/extra "e" in ae/oe/ue may change the word (for example schön/schon),
    # so umlaut spellings are never guessed fuzzily.
    if edited_pair in PROTECTED_UMLAUT_PAIRS:
        return False

    position = min(edit_index, len(expected) - 1)
    token_start = 0
    for token in expected_tokens:
        token_end = token_start + len(token)
        if token_start <= position < token_end:
            return len(token) >= 6
        token_start = token_end
    return False


def unordered_tokens_match(given: str, expected_tokens: list[str]) -> bool:
    """Match all expected words exactly once, even if the answer order differs."""
    tokens = tuple(sorted(token for token in expected_tokens if token))
    if not tokens or len(given) != sum(len(token) for token in tokens):
        return False

    @functools.lru_cache(maxsize=None)
    def consume(position: int, remaining: tuple[str, ...]) -> bool:
        if not remaining:
            return position == len(given)
        previous = None
        for index, token in enumerate(remaining):
            if token == previous:
                continue
            previous = token
            if given.startswith(token, position):
                next_remaining = remaining[:index] + remaining[index + 1:]
                if consume(position + len(token), next_remaining):
                    return True
        return False

    return consume(0, tokens)


def answer_translation_tokens(answer: str, translation_length: int) -> list[str]:
    """Keep typed word boundaries while removing an optional exact declension suffix."""
    remaining = translation_length
    result = []
    for token in normalized_tokens(answer):
        if remaining <= 0:
            break
        part = token[:remaining]
        if part:
            result.append(part)
            remaining -= len(part)
    return result if remaining == 0 else []


def unordered_tokens_match_with_typo(given_tokens: list[str], expected_tokens: list[str]) -> bool:
    """Match reordered words while allowing one conservative typo in the whole answer."""
    if len(given_tokens) != len(expected_tokens) or not given_tokens:
        return False
    remaining = tuple(sorted(expected_tokens))

    @functools.lru_cache(maxsize=None)
    def match(index: int, candidates: tuple[str, ...], typo_used: bool) -> bool:
        if index == len(given_tokens):
            return not candidates and typo_used
        actual = given_tokens[index]
        previous = None
        for candidate_index, expected in enumerate(candidates):
            if expected == previous:
                continue
            previous = expected
            is_exact = actual == expected
            is_typo = not typo_used and likely_single_typo(actual, expected, [expected])
            if not is_exact and not is_typo:
                continue
            next_candidates = candidates[:candidate_index] + candidates[candidate_index + 1:]
            if match(index + 1, next_candidates, typo_used or is_typo):
                return True
        return False

    return match(0, remaining, False)


def evaluate_answer(
    answer: str, item: sqlite3.Row, check_declension: bool, allow_typos: bool = True
) -> tuple[bool, bool]:
    """Return (correct, accepted_as_typo); declensions always require an exact suffix."""
    answer_normalized = normalize_answer(answer)
    german = item["german_text"]
    variants = [part.strip() for part in re.split(r"\s*(?:;|\||\s/\s)\s*", german) if part.strip()]
    variants.append(german)
    suffix = item["declension"] if check_declension and item["declension"] else ""
    suffix_normalized = normalize_answer(suffix)

    if suffix_normalized:
        if not answer_normalized.endswith(suffix_normalized):
            return False, False
        translation_answer = answer_normalized[:-len(suffix_normalized)]
    else:
        translation_answer = answer_normalized

    given_tokens = answer_translation_tokens(answer, len(translation_answer))

    for variant in variants:
        expected_normalized = normalize_answer(variant)
        expected_tokens = normalized_tokens(variant)
        if translation_answer == expected_normalized or unordered_tokens_match(translation_answer, expected_tokens):
            return True, False
    if allow_typos:
        for variant in variants:
            expected_normalized = normalize_answer(variant)
            expected_tokens = normalized_tokens(variant)
            if likely_single_typo(translation_answer, expected_normalized, expected_tokens):
                return True, True
            if unordered_tokens_match_with_typo(given_tokens, expected_tokens):
                return True, True
    return False, False


def selected_vocab(payload: dict[str, Any]) -> list[sqlite3.Row]:
    conn = get_db()
    targeted = [safe_int(value) for value in payload.get("vocabulary_ids", []) if safe_int(value) > 0]
    source_ids = [safe_int(value) for value in payload.get("source_ids", []) if safe_int(value) > 0]
    lessons = [str(value) for value in payload.get("lessons", [])]
    where, values = [], []
    if targeted:
        where.append(f"v.id IN ({','.join('?' for _ in targeted)})")
        values.extend(targeted)
    else:
        if source_ids:
            where.append(f"v.source_id IN ({','.join('?' for _ in source_ids)})")
            values.extend(source_ids)
        if lessons:
            where.append(f"v.lesson IN ({','.join('?' for _ in lessons)})")
            values.extend(lessons)
    query = "SELECT v.* FROM vocabulary v"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY v.source_id, v.position, v.id"
    rows = conn.execute(query, values).fetchall()
    difficulties = {str(value) for value in payload.get("difficulties", []) if str(value).strip()}
    if difficulties:
        rows = [row for row in rows if vocab_dict(row)["difficulty"] in difficulties]
    return rows


def current_quiz_payload(state: dict[str, Any], mode: str, settings: dict[str, Any]) -> dict[str, Any]:
    current_id = state.get("current_id")
    item = None
    if current_id:
        row = get_db().execute("SELECT * FROM vocabulary WHERE id = ?", (current_id,)).fetchone()
        if row:
            item = {"id": row["id"], "foreign_text": row["foreign_text"], "lesson": row["lesson"]}
            if mode == "through":
                item["german_text"] = row["german_text"]
    base = {
        "mode": mode,
        "completed": bool(state.get("completed")),
        "current": item,
        "answered": state.get("answered", 0),
        "correct": state.get("correct", 0),
        "wrong": state.get("wrong", 0),
    }
    undo = state.get("undo") or {}
    attempt_id = safe_int(undo.get("attempt_id"))
    pending_wrong = None
    if attempt_id:
        wrong_attempt = get_db().execute(
            """SELECT a.answer, v.foreign_text, v.german_text, v.declension
               FROM attempts a JOIN vocabulary v ON v.id = a.vocabulary_id
               WHERE a.id = ? AND a.correct = 0""",
            (attempt_id,),
        ).fetchone()
        if wrong_attempt:
            expected = wrong_attempt["german_text"]
            if settings.get("check_declension") and wrong_attempt["declension"]:
                expected += " " + wrong_attempt["declension"]
            pending_wrong = {
                "correct": False,
                "expected": expected,
                "given": wrong_attempt["answer"],
                "foreign_text": wrong_attempt["foreign_text"],
            }
    base["pending_wrong"] = pending_wrong
    if mode == "block":
        base.update(total=state["total"], remaining=len(state["queue"]) + (1 if current_id else 0))
    elif mode == "through":
        base.update(
            total=state["total"],
            remaining=len(state["queue"]) + (1 if current_id else 0),
            side=state.get("side", "foreign"),
            step=state.get("step", 0),
            total_steps=state["total"] * 2,
            timer_enabled=settings.get("timer_enabled", False),
            timer_seconds=settings.get("timer_seconds", 2),
        )
    else:
        base.update(
            box=state.get("current_box", 1),
            boxes={key: len(value) for key, value in state["boxes"].items()},
            mastered=len(state.get("mastered", [])),
            total=state["total"],
        )
    base["check_declension"] = settings.get("check_declension", False)
    base["allow_typos"] = settings.get("allow_typos", True)
    return base


def advance_flashcards(state: dict[str, Any]) -> None:
    if state["queue"]:
        state["current_id"] = state["queue"].pop(0)
        return
    if len(state["mastered"]) >= state["total"]:
        state["completed"] = True
        state["current_id"] = None
        return
    start = state["current_box"] + 1
    candidates = list(range(start, 6)) + list(range(1, start))
    next_box = next((box for box in candidates if state["boxes"][str(box)]), None)
    if next_box is None:
        state["completed"] = True
        state["current_id"] = None
        return
    state["current_box"] = next_box
    state["queue"] = list(state["boxes"][str(next_box)])
    random.SystemRandom().shuffle(state["queue"])
    state["current_id"] = state["queue"].pop(0)


def apply_answer_transition(state: dict[str, Any], mode: str, vocabulary_id: int, correct: bool) -> None:
    state["answered"] += 1
    state["correct" if correct else "wrong"] += 1
    if mode == "block":
        if not correct:
            # A wrong block-mode answer is retried immediately after the reveal.
            state["total"] += 1
            state["current_id"] = vocabulary_id
        elif state["queue"]:
            state["current_id"] = state["queue"].pop(0)
        else:
            state["current_id"] = None
            state["completed"] = True
        return

    current_box = str(state["current_box"])
    if vocabulary_id in state["boxes"][current_box]:
        state["boxes"][current_box].remove(vocabulary_id)
    if correct:
        if state["current_box"] == 5:
            state["mastered"].append(vocabulary_id)
        else:
            state["boxes"][str(state["current_box"] + 1)].append(vocabulary_id)
    else:
        state["boxes"]["1"].append(vocabulary_id)
    state["current_id"] = None
    advance_flashcards(state)


class ExtractedVocabulary(BaseModel):
    foreign_text: str = Field(description="Lateinisches Wort oder lateinischer Ausdruck")
    german_text: str = Field(description="Deutsche Übersetzung")
    declension: str = Field(default="", description="Deklination, Genitiv/Genus oder andere Formen")
    lesson: str = Field(default="", description="Erkennbare Lektion oder Gruppe")


class ExtractionResult(BaseModel):
    vocabulary: list[ExtractedVocabulary]


def register_routes(app: Flask) -> None:
    admin_only = {
        "import_source", "delete_source", "create_vocabulary", "bulk_create_vocabulary",
        "edit_vocabulary", "delete_vocabulary", "ai_extract", "ai_commit", "delete_study_list",
    }

    @app.before_request
    def protect_access() -> Response | None:
        auth_enabled = bool(app.config["ADMIN_ACCESS_CODE"] or app.config["LEARNER_ACCESS_CODE"])
        if not auth_enabled or request.endpoint in {"static", "access", "submit_access"}:
            return None
        role = session.get("role")
        if role not in {"admin", "learner"}:
            if request.path.startswith("/api/"):
                return jsonify(error="Bitte zuerst mit einem Zugangscode anmelden."), 401
            return redirect(url_for("access"))
        if request.endpoint in admin_only and role != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="Diese Funktion ist nur im Admin-Modus verfügbar."), 403
            return redirect(url_for("index"))
        return None

    @app.get("/access")
    def access() -> str | Response:
        if session.get("role") in {"admin", "learner"}:
            return redirect(url_for("index"))
        return render_template("access.html", error=None)

    @app.post("/access")
    def submit_access() -> str | Response:
        code = str(request.form.get("access_code", "")).strip()
        if app.config["ADMIN_ACCESS_CODE"] and secrets.compare_digest(code, app.config["ADMIN_ACCESS_CODE"]):
            session["role"] = "admin"
            return redirect(url_for("index"))
        if app.config["LEARNER_ACCESS_CODE"] and secrets.compare_digest(code, app.config["LEARNER_ACCESS_CODE"]):
            session["role"] = "learner"
            return redirect(url_for("index"))
        return render_template("access.html", error="Der Zugangscode ist nicht korrekt."), 401

    @app.post("/logout")
    def logout() -> Response:
        session.clear()
        return jsonify(ok=True)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/bootstrap")
    def bootstrap() -> Response:
        conn = get_db()
        source_rows = conn.execute(
            """SELECT s.id, s.name, s.created_at, COUNT(v.id) AS vocabulary_count
               FROM sources s LEFT JOIN vocabulary v ON v.source_id = s.id
               GROUP BY s.id ORDER BY s.id"""
        ).fetchall()
        lessons = conn.execute(
            """SELECT source_id, lesson, COUNT(*) AS count FROM vocabulary
               GROUP BY source_id, lesson ORDER BY source_id, MIN(position)"""
        ).fetchall()
        by_source: dict[int, list[dict[str, Any]]] = {}
        for lesson in lessons:
            by_source.setdefault(lesson["source_id"], []).append({"name": lesson["lesson"], "count": lesson["count"]})
        sources = [dict(row) | {"lessons": sorted(by_source.get(row["id"], []), key=lambda x: natural_key(x["name"]))} for row in source_rows]
        stored_settings = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('check_declension', 'allow_typos')"
        ).fetchall())
        check = stored_settings.get("check_declension") == "true"
        allow_typos = stored_settings.get("allow_typos", "true") == "true"
        study_lists = [dict(row) for row in conn.execute(
            """SELECT l.id, l.name, l.updated_at, COUNT(i.vocabulary_id) AS vocabulary_count
               FROM study_lists l LEFT JOIN study_list_items i ON i.list_id = l.id
               GROUP BY l.id ORDER BY l.updated_at DESC, l.name"""
        ).fetchall()]
        return jsonify(
            sources=sources,
            study_lists=study_lists,
            settings={"check_declension": check, "allow_typos": allow_typos},
            ai_available=bool(os.environ.get("OPENAI_API_KEY")),
            auth={
                "enabled": bool(app.config["ADMIN_ACCESS_CODE"] or app.config["LEARNER_ACCESS_CODE"]),
                "role": session.get("role") or "admin",
            },
        )

    @app.put("/api/settings")
    def update_settings() -> Response:
        payload = request.get_json(force=True)
        updates = {}
        for key in ("check_declension", "allow_typos"):
            if key in payload:
                updates[key] = bool(payload[key])
        if updates:
            get_db().executemany(
                "UPDATE app_settings SET value = ? WHERE key = ?",
                [("true" if value else "false", key) for key, value in updates.items()],
            )
            get_db().commit()
        return jsonify(updates)

    @app.post("/api/sources/import")
    def import_source() -> tuple[Response, int] | Response:
        upload = request.files.get("file")
        if not upload:
            return jsonify(error="Bitte eine CSV-Datei auswählen."), 400
        try:
            items = parse_csv(upload.read())
            name = request.form.get("name", "").strip() or Path(upload.filename or "Import").stem
            source_id = insert_source(name, items)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(id=source_id, name=name, count=len(items)), 201

    @app.get("/api/sources/<int:source_id>/export")
    def export_source(source_id: int) -> Response:
        source = get_db().execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not source:
            return jsonify(error="Datenquelle nicht gefunden."), 404
        rows = get_db().execute("SELECT * FROM vocabulary WHERE source_id = ? ORDER BY position, id", (source_id,)).fetchall()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            totals = get_db().execute(
                "SELECT SUM(correct), COUNT(*) - SUM(correct) FROM attempts WHERE vocabulary_id = ?", (row["id"],)
            ).fetchone()
            writer.writerow(
                [
                    row["foreign_text"], row["german_text"], row["declension"], row["lesson"],
                    row["legacy_correct"] + (totals[0] or 0), row["legacy_wrong"] + (totals[1] or 0),
                ]
            )
        filename = re.sub(r"[^\w.-]+", "_", source["name"], flags=re.UNICODE).strip("_") or "vokabeln"
        return Response(
            "\ufeff" + stream.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    @app.delete("/api/sources/<int:source_id>")
    def delete_source(source_id: int) -> Response:
        cursor = get_db().execute("DELETE FROM sources WHERE id = ?", (source_id,))
        get_db().commit()
        if cursor.rowcount == 0:
            return jsonify(error="Datenquelle nicht gefunden."), 404
        return jsonify(ok=True)

    @app.get("/api/vocabulary")
    def list_vocabulary() -> Response:
        source_ids = [safe_int(value) for value in request.args.getlist("source_id") if safe_int(value) > 0]
        search = request.args.get("q", "").strip()
        lessons = request.args.getlist("lesson")
        where, values = [], []
        if source_ids:
            where.append(f"v.source_id IN ({','.join('?' for _ in source_ids)})")
            values.extend(source_ids)
        if lessons:
            where.append(f"v.lesson IN ({','.join('?' for _ in lessons)})")
            values.extend(lessons)
        if search:
            where.append("(v.foreign_text LIKE ? OR v.german_text LIKE ? OR v.declension LIKE ?)")
            values.extend([f"%{search}%"] * 3)
        query = "SELECT v.* FROM vocabulary v" + ((" WHERE " + " AND ".join(where)) if where else "")
        query += " ORDER BY v.source_id, v.position, v.id LIMIT 2000"
        items = [vocab_dict(row) for row in get_db().execute(query, values).fetchall()]
        return jsonify(items=items)

    @app.post("/api/vocabulary")
    def create_vocabulary() -> tuple[Response, int]:
        payload = request.get_json(force=True)
        if not str(payload.get("foreign_text", "")).strip() or not str(payload.get("german_text", "")).strip():
            return jsonify(error="Fremdsprache und Deutsch sind Pflichtfelder."), 400
        source_id = safe_int(payload.get("source_id"))
        exists = get_db().execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not exists:
            return jsonify(error="Datenquelle nicht gefunden."), 404
        position = get_db().execute("SELECT COALESCE(MAX(position), 0) + 1 FROM vocabulary WHERE source_id = ?", (source_id,)).fetchone()[0]
        now = utcnow()
        cursor = get_db().execute(
            """INSERT INTO vocabulary(source_id, foreign_text, german_text, declension, lesson, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, str(payload["foreign_text"]).strip(), str(payload["german_text"]).strip(),
             str(payload.get("declension", "")).strip(), str(payload.get("lesson", "")).strip(), position, now, now),
        )
        get_db().commit()
        return jsonify(id=cursor.lastrowid), 201

    @app.post("/api/vocabulary/bulk")
    def bulk_create_vocabulary() -> tuple[Response, int] | Response:
        payload = request.get_json(force=True)
        source_id = safe_int(payload.get("source_id"))
        if not get_db().execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
            return jsonify(error="Datenquelle nicht gefunden."), 404
        items = []
        for raw in payload.get("items", []):
            foreign = str(raw.get("foreign_text", "")).strip()
            german = str(raw.get("german_text", "")).strip()
            if not foreign and not german:
                continue
            if not foreign or not german:
                return jsonify(error="Jede ausgefüllte Zeile benötigt Latein und Deutsch."), 400
            items.append({
                "foreign_text": foreign,
                "german_text": german,
                "declension": str(raw.get("declension", "")).strip(),
                "lesson": str(raw.get("lesson", "")).strip(),
            })
        if not items:
            return jsonify(error="Bitte mindestens eine vollständige Vokabel eingeben."), 400
        start = get_db().execute(
            "SELECT COALESCE(MAX(position), 0) FROM vocabulary WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
        now = utcnow()
        get_db().executemany(
            """INSERT INTO vocabulary(source_id, foreign_text, german_text, declension, lesson, position, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (source_id, item["foreign_text"], item["german_text"], item["declension"], item["lesson"], start + index, now, now)
                for index, item in enumerate(items, 1)
            ],
        )
        get_db().commit()
        return jsonify(count=len(items)), 201

    @app.get("/api/study-lists/<int:list_id>")
    def get_study_list(list_id: int) -> tuple[Response, int] | Response:
        study_list = get_db().execute("SELECT * FROM study_lists WHERE id = ?", (list_id,)).fetchone()
        if not study_list:
            return jsonify(error="Lernliste nicht gefunden."), 404
        rows = get_db().execute(
            """SELECT v.* FROM study_list_items i JOIN vocabulary v ON v.id = i.vocabulary_id
               WHERE i.list_id = ? ORDER BY i.added_at, v.id""",
            (list_id,),
        ).fetchall()
        return jsonify(id=list_id, name=study_list["name"], items=[vocab_dict(row) for row in rows])

    @app.delete("/api/study-lists/<int:list_id>")
    def delete_study_list(list_id: int) -> Response:
        cursor = get_db().execute("DELETE FROM study_lists WHERE id = ?", (list_id,))
        get_db().commit()
        if cursor.rowcount == 0:
            return jsonify(error="Lernliste nicht gefunden."), 404
        return jsonify(ok=True)

    @app.patch("/api/vocabulary/<int:vocabulary_id>")
    def edit_vocabulary(vocabulary_id: int) -> tuple[Response, int] | Response:
        payload = request.get_json(force=True)
        allowed = ("foreign_text", "german_text", "declension", "lesson")
        updates = {key: str(payload[key]).strip() for key in allowed if key in payload}
        if any(key in updates and not updates[key] for key in ("foreign_text", "german_text")):
            return jsonify(error="Fremdsprache und Deutsch dürfen nicht leer sein."), 400
        if not updates:
            return jsonify(error="Keine Änderung übergeben."), 400
        updates["updated_at"] = utcnow()
        cursor = get_db().execute(
            "UPDATE vocabulary SET " + ", ".join(f"{key} = ?" for key in updates) + " WHERE id = ?",
            [*updates.values(), vocabulary_id],
        )
        get_db().commit()
        if cursor.rowcount == 0:
            return jsonify(error="Vokabel nicht gefunden."), 404
        return jsonify(ok=True)

    @app.delete("/api/vocabulary/<int:vocabulary_id>")
    def delete_vocabulary(vocabulary_id: int) -> Response:
        cursor = get_db().execute("DELETE FROM vocabulary WHERE id = ?", (vocabulary_id,))
        get_db().commit()
        if cursor.rowcount == 0:
            return jsonify(error="Vokabel nicht gefunden."), 404
        return jsonify(ok=True)

    @app.post("/api/quiz/start")
    def start_quiz() -> tuple[Response, int] | Response:
        payload = request.get_json(force=True)
        mode = payload.get("mode")
        if mode not in ("block", "cards", "through"):
            return jsonify(error="Unbekannter Lernmodus."), 400
        if not payload.get("vocabulary_ids") and (not payload.get("source_ids") or not payload.get("lessons")):
            return jsonify(error="Bitte mindestens eine Datenquelle und eine Lektion auswählen."), 400
        words = selected_vocab(payload)
        if not words:
            return jsonify(error="Für diese Auswahl wurden keine Vokabeln gefunden."), 400
        settings = {
            "check_declension": bool(payload.get("check_declension")),
            "allow_typos": bool(payload.get("allow_typos", True)),
            "source_ids": payload.get("source_ids", []),
            "lessons": payload.get("lessons", []),
        }
        ids = [row["id"] for row in words]
        if mode in ("block", "through"):
            block_size = safe_int(payload.get("block_size"), 5, 1, 200)
            repetitions = safe_int(payload.get("repetitions"), 1, 1, 30)
            all_blocks = [ids[offset : offset + block_size] for offset in range(0, len(ids), block_size)]
            if "block_numbers" in payload:
                block_numbers = sorted({
                    safe_int(value) for value in payload.get("block_numbers", [])
                    if 1 <= safe_int(value) <= len(all_blocks)
                })
                if not block_numbers:
                    return jsonify(error="Bitte mindestens einen vorhandenen Block auswählen."), 400
            else:
                # Compatibility for older clients that sent only the first N blocks.
                block_count = safe_int(payload.get("block_count"), 1, 1, 100)
                block_numbers = list(range(1, min(block_count, len(all_blocks)) + 1))
            queue: list[int] = []
            selected_ids: list[int] = []
            for block_number in block_numbers:
                block = all_blocks[block_number - 1]
                selected_ids.extend(block)
            for _ in range(repetitions):
                shuffled_selection = list(selected_ids)
                random.SystemRandom().shuffle(shuffled_selection)
                queue.extend(shuffled_selection)
            if mode == "block":
                state = {
                    "queue": queue[1:], "current_id": queue[0], "total": len(queue),
                    "answered": 0, "correct": 0, "wrong": 0, "completed": False,
                }
            else:
                state = {
                    "queue": queue[1:], "current_id": queue[0], "total": len(queue),
                    "answered": 0, "correct": 0, "wrong": 0, "completed": False,
                    "side": "foreign", "step": 0,
                }
            settings.update(
                block_size=block_size,
                block_numbers=block_numbers,
                repetitions=repetitions,
                vocabulary_count=len(selected_ids),
            )
            if mode == "through":
                settings.update(
                    timer_enabled=bool(payload.get("timer_enabled")),
                    timer_seconds=safe_int(payload.get("timer_seconds"), 2, 1, 60),
                )
        else:
            card_count = safe_int(payload.get("card_count"), 20, 1, 1000)
            ids = ids[:card_count]
            boxes = {str(number): [] for number in range(1, 6)}
            boxes["1"] = list(ids)
            initial_queue = list(ids)
            random.SystemRandom().shuffle(initial_queue)
            state = {
                "boxes": boxes, "mastered": [], "current_box": 1, "queue": initial_queue[1:],
                "current_id": initial_queue[0], "total": len(ids), "answered": 0, "correct": 0,
                "wrong": 0, "completed": False,
            }
            settings.update(card_count=len(ids))
        token = secrets.token_urlsafe(24)
        now = utcnow()
        get_db().execute(
            "INSERT INTO quiz_sessions VALUES (?, ?, ?, ?, 0, ?, ?)",
            (token, mode, json.dumps(settings), json.dumps(state), now, now),
        )
        get_db().commit()
        return jsonify(token=token, **current_quiz_payload(state, mode, settings)), 201

    @app.get("/api/quiz/<token>")
    def get_quiz(token: str) -> tuple[Response, int] | Response:
        session = get_db().execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        return jsonify(token=token, **current_quiz_payload(json.loads(session["state_json"]), session["mode"], json.loads(session["settings_json"])))

    @app.post("/api/quiz/<token>/answer")
    def submit_answer(token: str) -> tuple[Response, int] | Response:
        conn = get_db()
        session = conn.execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        if session["mode"] == "through":
            return jsonify(error="Der Durchlaufmodus erwartet keine Texteingabe."), 409
        state, settings = json.loads(session["state_json"]), json.loads(session["settings_json"])
        if state.get("completed") or not state.get("current_id"):
            return jsonify(error="Diese Lernsitzung ist bereits beendet."), 409
        payload = request.get_json(force=True)
        answer = str(payload.get("answer", ""))
        item = conn.execute("SELECT * FROM vocabulary WHERE id = ?", (state["current_id"],)).fetchone()
        if not item:
            return jsonify(error="Die aktuelle Vokabel existiert nicht mehr."), 409
        expected = item["german_text"] + ((" " + item["declension"]) if settings.get("check_declension") and item["declension"] else "")
        correct, accepted_as_typo = evaluate_answer(
            answer,
            item,
            settings.get("check_declension", False),
            settings.get("allow_typos", True),
        )
        state_before = copy.deepcopy(state)
        state_before.pop("undo", None)
        attempt = conn.execute(
            "INSERT INTO attempts(vocabulary_id, session_token, correct, answer, created_at) VALUES (?, ?, ?, ?, ?)",
            (item["id"], token, int(correct), answer, utcnow()),
        )
        apply_answer_transition(state, session["mode"], item["id"], correct)
        if correct:
            state.pop("undo", None)
        else:
            state["undo"] = {
                "attempt_id": attempt.lastrowid,
                "vocabulary_id": item["id"],
                "before_state": state_before,
            }
        conn.execute(
            "UPDATE quiz_sessions SET state_json = ?, completed = ?, updated_at = ? WHERE token = ?",
            (json.dumps(state), int(state["completed"]), utcnow(), token),
        )
        conn.commit()
        result = current_quiz_payload(state, session["mode"], settings)
        result.update(result={
            "correct": correct,
            "accepted_as_typo": accepted_as_typo,
            "expected": expected,
            "given": answer,
            "foreign_text": item["foreign_text"],
        })
        return jsonify(result)

    @app.post("/api/quiz/<token>/advance")
    def advance_through(token: str) -> tuple[Response, int] | Response:
        conn = get_db()
        quiz_session = conn.execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not quiz_session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        if quiz_session["mode"] != "through":
            return jsonify(error="Diese Lernsitzung ist kein Durchlauf."), 409
        state = json.loads(quiz_session["state_json"])
        settings = json.loads(quiz_session["settings_json"])
        if state.get("completed"):
            return jsonify(token=token, **current_quiz_payload(state, "through", settings))
        if state.get("side") == "foreign":
            state["side"] = "german"
            state["step"] += 1
        else:
            state["answered"] += 1
            state["step"] += 1
            state["side"] = "foreign"
            if state["queue"]:
                state["current_id"] = state["queue"].pop(0)
            else:
                state["current_id"] = None
                state["completed"] = True
        conn.execute(
            "UPDATE quiz_sessions SET state_json = ?, completed = ?, updated_at = ? WHERE token = ?",
            (json.dumps(state), int(state["completed"]), utcnow(), token),
        )
        conn.commit()
        return jsonify(token=token, **current_quiz_payload(state, "through", settings))

    @app.post("/api/quiz/<token>/mark-correct")
    def mark_answer_correct(token: str) -> tuple[Response, int] | Response:
        conn = get_db()
        quiz_session = conn.execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not quiz_session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        state = json.loads(quiz_session["state_json"])
        settings = json.loads(quiz_session["settings_json"])
        undo = state.get("undo") or {}
        attempt_id = safe_int(undo.get("attempt_id"))
        vocabulary_id = safe_int(undo.get("vocabulary_id"))
        before_state = undo.get("before_state")
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE id = ? AND session_token = ? AND correct = 0",
            (attempt_id, token),
        ).fetchone()
        if not attempt or not isinstance(before_state, dict) or vocabulary_id <= 0:
            return jsonify(error="Diese Antwort kann nicht mehr korrigiert werden."), 409
        corrected_state = before_state
        apply_answer_transition(corrected_state, quiz_session["mode"], vocabulary_id, True)
        corrected_state.pop("undo", None)
        conn.execute("UPDATE attempts SET correct = 1 WHERE id = ?", (attempt_id,))
        conn.execute(
            "UPDATE quiz_sessions SET state_json = ?, completed = ?, updated_at = ? WHERE token = ?",
            (json.dumps(corrected_state), int(corrected_state["completed"]), utcnow(), token),
        )
        conn.commit()
        result = current_quiz_payload(corrected_state, quiz_session["mode"], settings)
        result.update(corrected=True)
        return jsonify(result)

    @app.post("/api/quiz/<token>/continue")
    def continue_after_wrong(token: str) -> tuple[Response, int] | Response:
        conn = get_db()
        quiz_session = conn.execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not quiz_session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        state = json.loads(quiz_session["state_json"])
        settings = json.loads(quiz_session["settings_json"])
        if not state.pop("undo", None):
            return jsonify(error="Es gibt keine offene falsche Antwort."), 409
        conn.execute(
            "UPDATE quiz_sessions SET state_json = ?, updated_at = ? WHERE token = ?",
            (json.dumps(state), utcnow(), token),
        )
        conn.commit()
        return jsonify(token=token, **current_quiz_payload(state, quiz_session["mode"], settings))

    @app.get("/api/quiz/<token>/summary")
    def quiz_summary(token: str) -> tuple[Response, int] | Response:
        quiz_session = get_db().execute("SELECT * FROM quiz_sessions WHERE token = ?", (token,)).fetchone()
        if not quiz_session:
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        settings = json.loads(quiz_session["settings_json"])
        rows = get_db().execute(
            """SELECT a.id AS attempt_id, a.answer, a.created_at, v.*
               FROM attempts a JOIN vocabulary v ON v.id = a.vocabulary_id
               WHERE a.session_token = ? AND a.correct = 0 ORDER BY a.id""",
            (token,),
        ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            vocabulary_id = row["id"]
            expected = row["german_text"]
            if settings.get("check_declension") and row["declension"]:
                expected += " " + row["declension"]
            entry = grouped.setdefault(vocabulary_id, {
                "id": vocabulary_id,
                "foreign_text": row["foreign_text"],
                "expected": expected,
                "lesson": row["lesson"],
                "wrong_count": 0,
                "answers": [],
            })
            entry["wrong_count"] += 1
            entry["answers"].append(row["answer"])
        state = json.loads(quiz_session["state_json"])
        return jsonify(
            token=token,
            mode=quiz_session["mode"],
            answered=state.get("answered", 0),
            correct=state.get("correct", 0),
            wrong=state.get("wrong", 0),
            mistakes=list(grouped.values()),
        )

    @app.post("/api/quiz/<token>/mistakes/save")
    def save_quiz_mistakes(token: str) -> tuple[Response, int] | Response:
        conn = get_db()
        if not conn.execute("SELECT 1 FROM quiz_sessions WHERE token = ?", (token,)).fetchone():
            return jsonify(error="Lernsitzung nicht gefunden."), 404
        vocabulary_ids = [row[0] for row in conn.execute(
            "SELECT DISTINCT vocabulary_id FROM attempts WHERE session_token = ? AND correct = 0", (token,)
        ).fetchall()]
        if not vocabulary_ids:
            return jsonify(error="In diesem Durchgang gibt es keine falschen Vokabeln."), 400
        payload = request.get_json(force=True)
        list_id = safe_int(payload.get("list_id"))
        now = utcnow()
        if list_id:
            study_list = conn.execute("SELECT * FROM study_lists WHERE id = ?", (list_id,)).fetchone()
            if not study_list:
                return jsonify(error="Lernliste nicht gefunden."), 404
        else:
            name = str(payload.get("name", "")).strip()
            if not name:
                return jsonify(error="Bitte einen Namen für die neue Lernliste eingeben."), 400
            try:
                cursor = conn.execute(
                    "INSERT INTO study_lists(name, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now)
                )
            except sqlite3.IntegrityError:
                return jsonify(error="Eine Lernliste mit diesem Namen existiert bereits. Wähle sie zum Erweitern aus."), 409
            list_id = int(cursor.lastrowid)
            study_list = conn.execute("SELECT * FROM study_lists WHERE id = ?", (list_id,)).fetchone()
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO study_list_items(list_id, vocabulary_id, added_at) VALUES (?, ?, ?)",
            [(list_id, vocabulary_id, now) for vocabulary_id in vocabulary_ids],
        )
        added = conn.total_changes - before
        conn.execute("UPDATE study_lists SET updated_at = ? WHERE id = ?", (now, list_id))
        total = conn.execute("SELECT COUNT(*) FROM study_list_items WHERE list_id = ?", (list_id,)).fetchone()[0]
        conn.commit()
        return jsonify(list_id=list_id, name=study_list["name"], added=added, total=total)

    @app.post("/api/ai/extract")
    def ai_extract() -> tuple[Response, int] | Response:
        uploads = request.files.getlist("images")
        if not uploads:
            return jsonify(error="Bitte mindestens ein Bild auswählen."), 400
        if not os.environ.get("OPENAI_API_KEY"):
            return jsonify(error="OPENAI_API_KEY ist auf dem Server nicht gesetzt."), 503
        try:
            from openai import OpenAI

            content: list[dict[str, Any]] = [{
                "type": "input_text",
                "text": (
                    "Extrahiere alle Latein-Vokabeln aus den Bildern. Übernimm deutsche Übersetzungen, "
                    "Deklination/Genitiv/Genus beziehungsweise Stammformen und Lektion möglichst genau. "
                    "Erfinde nichts. Bei nicht erkennbarer Lektion nutze die vorgegebene Standardlektion. "
                    f"Standardlektion: {request.form.get('lesson', '').strip()}"
                ),
            }]
            for upload in uploads[:8]:
                raw = upload.read()
                if len(raw) > 10 * 1024 * 1024:
                    return jsonify(error=f"{upload.filename}: Bild ist größer als 10 MB."), 400
                mime = upload.mimetype or mimetypes.guess_type(upload.filename or "")[0] or "image/jpeg"
                if not mime.startswith("image/"):
                    return jsonify(error=f"{upload.filename}: kein unterstütztes Bild."), 400
                content.append({"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}", "detail": "high"})
            response = OpenAI().responses.parse(
                model=app.config["OPENAI_MODEL"],
                input=[
                    {"role": "system", "content": "Du extrahierst sorgfältig strukturierte Vokabellisten aus Schulbuchfotos."},
                    {"role": "user", "content": content},
                ],
                text_format=ExtractionResult,
            )
            parsed = response.output_parsed
            if not parsed or not parsed.vocabulary:
                return jsonify(error="Auf den Bildern wurden keine Vokabeln erkannt."), 422
            default_lesson = request.form.get("lesson", "").strip()
            items = [item.model_dump() | {"lesson": item.lesson or default_lesson} for item in parsed.vocabulary]
            token = secrets.token_urlsafe(24)
            get_db().execute("INSERT INTO ai_drafts VALUES (?, ?, ?)", (token, json.dumps(items, ensure_ascii=False), utcnow()))
            get_db().commit()
            return jsonify(draft_token=token, items=items)
        except Exception as error:  # SDK errors are translated into a safe user-facing message.
            app.logger.exception("AI extraction failed")
            return jsonify(error=f"KI-Auswertung fehlgeschlagen: {type(error).__name__}"), 502

    @app.post("/api/ai/commit")
    def ai_commit() -> tuple[Response, int] | Response:
        payload = request.get_json(force=True)
        draft_token = str(payload.get("draft_token", ""))
        draft = get_db().execute("SELECT 1 FROM ai_drafts WHERE token = ?", (draft_token,)).fetchone()
        if not draft:
            return jsonify(error="Dieser KI-Entwurf existiert nicht mehr."), 404
        raw_items = payload.get("items", [])
        items = []
        for item in raw_items:
            foreign, german = str(item.get("foreign_text", "")).strip(), str(item.get("german_text", "")).strip()
            if foreign and german:
                items.append({
                    "foreign_text": foreign, "german_text": german,
                    "declension": str(item.get("declension", "")).strip(),
                    "lesson": str(item.get("lesson", "")).strip(),
                })
        if not items:
            return jsonify(error="Der geprüfte Entwurf enthält keine vollständige Vokabel."), 400
        source_id = safe_int(payload.get("source_id"))
        if source_id:
            source = get_db().execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone()
            if not source:
                return jsonify(error="Datenquelle nicht gefunden."), 404
            position = get_db().execute("SELECT COALESCE(MAX(position), 0) FROM vocabulary WHERE source_id = ?", (source_id,)).fetchone()[0]
            now = utcnow()
            get_db().executemany(
                """INSERT INTO vocabulary(source_id, foreign_text, german_text, declension, lesson, position, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(source_id, item["foreign_text"], item["german_text"], item["declension"], item["lesson"], position + i, now, now) for i, item in enumerate(items, 1)],
            )
        else:
            source_id = insert_source(str(payload.get("source_name", "KI-Import")), items)
        get_db().execute("DELETE FROM ai_drafts WHERE token = ?", (draft_token,))
        get_db().commit()
        return jsonify(source_id=source_id, count=len(items)), 201


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=safe_int(os.environ.get("PORT"), 8090), debug=os.environ.get("FLASK_DEBUG") == "1")
