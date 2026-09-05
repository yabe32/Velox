import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import create_app, get_db, insert_source, parse_csv


class VocabAppTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tempdir.name) / "test.sqlite3")})
        self.client = self.app.test_client()
        with self.app.app_context():
            self.source_id = insert_source(
                "Test",
                [
                    {"foreign_text": "amicus", "german_text": "der Freund", "declension": "i m", "lesson": "1"},
                    {"foreign_text": "videre", "german_text": "sehen", "declension": "", "lesson": "1"},
                    {"foreign_text": "puella", "german_text": "das Mädchen", "declension": "ae f", "lesson": "2"},
                    {"foreign_text": "currere", "german_text": "laufen", "declension": "", "lesson": "2"},
                ],
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def start(self, **overrides):
        payload = {
            "mode": "block", "source_ids": [self.source_id], "lessons": ["1", "2"],
            "block_size": 2, "block_count": 1, "repetitions": 2, "check_declension": False,
        }
        payload.update(overrides)
        return self.client.post("/api/quiz/start", json=payload)

    def test_csv_parser_excludes_rhetorical_material(self):
        raw = (
            "fremdsprache,deutsch,deklination,lektion,richtig,falsch\n"
            "amo,lieben,,1,2,1\n"
            "Metapher,Bildlicher Ausdruck,,Rethorische Mittel,0,0\n"
        ).encode()
        items = parse_csv(raw)
        self.assertEqual([item["foreign_text"] for item in items], ["amo"])

    def test_block_mode_builds_blocks_and_repetitions(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        quiz = response.get_json()
        self.assertEqual(quiz["total"], 4)
        answers = {"amicus": "der Freund", "videre": "sehen"}
        while not quiz["completed"]:
            response = self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": answers[quiz["current"]["foreign_text"]]})
            quiz = {**quiz, **response.get_json()}
        self.assertEqual(quiz["answered"], 4)
        self.assertEqual(quiz["correct"], 4)

    def test_block_wrong_answer_is_retried_immediately(self):
        quiz = self.start(block_size=1, block_count=1, repetitions=1).get_json()
        original_id = quiz["current"]["id"]
        result = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "der Hund"}
        ).get_json()
        self.assertFalse(result["result"]["correct"])
        self.assertEqual(result["current"]["id"], original_id)
        self.assertEqual(result["total"], 2)
        correct_answer = "der Freund" if result["current"]["foreign_text"] == "amicus" else "sehen"
        finished = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": correct_answer}
        ).get_json()
        self.assertTrue(finished["completed"])

    def test_wrong_answer_can_be_explicitly_marked_correct(self):
        quiz = self.start(block_size=1, block_numbers=[1], repetitions=1).get_json()
        wrong = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "der Hund"}
        ).get_json()
        self.assertEqual(wrong["wrong"], 1)
        self.assertEqual(wrong["total"], 2)
        corrected = self.client.post(f"/api/quiz/{quiz['token']}/mark-correct").get_json()
        self.assertTrue(corrected["completed"])
        self.assertEqual(corrected["wrong"], 0)
        self.assertEqual(corrected["correct"], 1)
        self.assertEqual(corrected["total"], 1)
        summary = self.client.get(f"/api/quiz/{quiz['token']}/summary").get_json()
        self.assertEqual(summary["mistakes"], [])

    def test_wrong_reveal_survives_reload_until_enter_continues(self):
        quiz = self.start(block_size=1, block_numbers=[1], repetitions=1).get_json()
        self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": "der Hund"})
        resumed = self.client.get(f"/api/quiz/{quiz['token']}").get_json()
        self.assertEqual(resumed["pending_wrong"]["expected"], "der Freund")
        self.assertEqual(resumed["pending_wrong"]["given"], "der Hund")

        continued = self.client.post(f"/api/quiz/{quiz['token']}/continue").get_json()
        self.assertIsNone(continued["pending_wrong"])
        self.assertEqual(continued["wrong"], 1)
        self.assertEqual(continued["current"]["id"], quiz["current"]["id"])

    def test_mark_correct_restores_flashcard_transition(self):
        quiz = self.start(mode="cards", card_count=1).get_json()
        wrong = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "falsch"}
        ).get_json()
        self.assertEqual(wrong["wrong"], 1)
        corrected = self.client.post(f"/api/quiz/{quiz['token']}/mark-correct").get_json()
        self.assertEqual(corrected["wrong"], 0)
        self.assertEqual(corrected["correct"], 1)
        self.assertEqual(corrected["box"], 2)
        self.assertEqual(corrected["boxes"]["1"], 0)
        self.assertEqual(corrected["boxes"]["2"], 1)

    def test_all_selected_blocks_are_shuffled_together_for_each_repetition(self):
        with mock.patch("app.random.SystemRandom.shuffle", autospec=True) as shuffle:
            response = self.start(block_size=2, block_count=1, repetitions=3)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(shuffle.call_count, 3)

    def test_global_block_shuffle_can_interleave_selected_blocks(self):
        with self.app.app_context():
            source_id = insert_source("Gemischte Blöcke", [
                {"foreign_text": f"wort{i}", "german_text": f"antwort{i}", "lesson": "Mix"}
                for i in range(15)
            ])

        def interleave(values):
            original = list(values)
            order = [10, 0, 5, 11, 1, 6, 12, 2, 7, 13, 3, 8, 14, 4, 9]
            values[:] = [original[index] for index in order]

        with mock.patch("app.random.SystemRandom.shuffle", side_effect=interleave):
            quiz = self.client.post("/api/quiz/start", json={
                "mode": "block", "source_ids": [source_id], "lessons": ["Mix"],
                "block_size": 5, "block_numbers": [1, 2, 3], "repetitions": 1,
            }).get_json()

        asked = []
        for _ in range(3):
            asked.append(quiz["current"]["foreign_text"])
            number = asked[-1].removeprefix("wort")
            quiz = {**quiz, **self.client.post(
                f"/api/quiz/{quiz['token']}/answer", json={"answer": f"antwort{number}"}
            ).get_json()}
        self.assertEqual(asked, ["wort10", "wort0", "wort5"])

    def test_requested_five_by_five_by_five_is_125_questions(self):
        with self.app.app_context():
            items = [
                {"foreign_text": f"wort{i}", "german_text": f"antwort{i}", "lesson": "Beispiel"}
                for i in range(25)
            ]
            source_id = insert_source("125-Test", items)
        response = self.client.post("/api/quiz/start", json={
            "mode": "block", "source_ids": [source_id], "lessons": ["Beispiel"],
            "block_size": 5, "block_numbers": [1, 2, 3, 4, 5], "repetitions": 5,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total"], 125)

    def test_specific_blocks_one_two_three_and_five_are_selected(self):
        with self.app.app_context():
            source_id = insert_source("Blockauswahl", [
                {"foreign_text": f"wort{i}", "german_text": f"antwort{i}", "lesson": "Auswahl"}
                for i in range(25)
            ])
        response = self.client.post("/api/quiz/start", json={
            "mode": "block", "source_ids": [source_id], "lessons": ["Auswahl"],
            "block_size": 5, "block_numbers": [1, 2, 3, 5], "repetitions": 1,
        })
        self.assertEqual(response.status_code, 201)
        quiz = response.get_json()
        self.assertEqual(quiz["total"], 20)
        asked = set()
        while not quiz["completed"]:
            foreign = quiz["current"]["foreign_text"]
            asked.add(foreign)
            number = foreign.removeprefix("wort")
            quiz = {**quiz, **self.client.post(
                f"/api/quiz/{quiz['token']}/answer", json={"answer": f"antwort{number}"}
            ).get_json()}
        expected = {f"wort{i}" for i in [*range(15), *range(20, 25)]}
        self.assertEqual(asked, expected)

    def test_explicit_empty_block_selection_is_rejected(self):
        response = self.start(block_numbers=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn("Block", response.get_json()["error"])

    def test_declension_is_concatenated_without_required_separator(self):
        quiz = self.start(block_size=1, block_count=1, repetitions=1, check_declension=True).get_json()
        result = self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": "der Freundim"}).get_json()
        self.assertTrue(result["result"]["correct"])

    def test_typo_preference_can_be_switched_without_changing_declension_setting(self):
        self.client.put("/api/settings", json={"check_declension": True})
        changed = self.client.put("/api/settings", json={"allow_typos": False})
        self.assertEqual(changed.get_json(), {"allow_typos": False})
        settings = self.client.get("/api/bootstrap").get_json()["settings"]
        self.assertEqual(settings, {"check_declension": True, "allow_typos": False})

    def test_conservative_translation_typos_but_exact_declensions(self):
        with self.app.app_context():
            source_id = insert_source("Tippfehler", [
                {"foreign_text": "exspectare", "german_text": "erwarten", "declension": "i m", "lesson": "T"},
                {"foreign_text": "pulcher", "german_text": "schön", "declension": "", "lesson": "T"},
            ])

        def start_word(vocabulary_id, check_declension=False, allow_typos=True):
            return self.start(
                vocabulary_ids=[vocabulary_id], source_ids=[], lessons=[], block_size=1,
                block_numbers=[1], repetitions=1, check_declension=check_declension,
                allow_typos=allow_typos,
            ).get_json()

        items = self.client.get(f"/api/vocabulary?source_id={source_id}").get_json()["items"]
        erwarten = next(item for item in items if item["german_text"] == "erwarten")
        schoen = next(item for item in items if item["german_text"] == "schön")

        quiz = start_word(erwarten["id"])
        typo = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "erwartzen"}
        ).get_json()["result"]
        self.assertTrue(typo["correct"])
        self.assertTrue(typo["accepted_as_typo"])

        quiz = start_word(erwarten["id"], allow_typos=False)
        rejected_typo = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "erwartzen"}
        ).get_json()["result"]
        self.assertFalse(rejected_typo["correct"])
        self.assertFalse(rejected_typo["accepted_as_typo"])

        quiz = start_word(erwarten["id"], check_declension=True)
        exact_suffix = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "erwartzen i m"}
        ).get_json()["result"]
        self.assertTrue(exact_suffix["correct"])
        self.assertTrue(exact_suffix["accepted_as_typo"])

        quiz = start_word(erwarten["id"], check_declension=True)
        wrong_suffix = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "erwartzen i n"}
        ).get_json()["result"]
        self.assertFalse(wrong_suffix["correct"])

        quiz = start_word(schoen["id"])
        umlaut_changes_meaning = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "schon"}
        ).get_json()["result"]
        self.assertFalse(umlaut_changes_meaning["correct"])

        lesson_one = self.client.get(f"/api/vocabulary?source_id={self.source_id}").get_json()["items"]
        sehen = next(item for item in lesson_one if item["german_text"] == "sehen")
        quiz = start_word(sehen["id"])
        short_different_word = self.client.post(
            f"/api/quiz/{quiz['token']}/answer", json={"answer": "gehen"}
        ).get_json()["result"]
        self.assertFalse(short_different_word["correct"])

    def test_multiple_unseparated_translations_can_be_entered_in_any_order(self):
        with self.app.app_context():
            source_id = insert_source("Mehrere Übersetzungen", [{
                "foreign_text": "capere",
                "german_text": "packen erobern nehmen erhalten",
                "declension": "",
                "lesson": "T",
            }])
        item = self.client.get(f"/api/vocabulary?source_id={source_id}").get_json()["items"][0]
        self.assertEqual(item["german_text"], "packen erobern nehmen erhalten")

        def ask(answer):
            quiz = self.start(
                vocabulary_ids=[item["id"]], source_ids=[], lessons=[], block_size=1,
                block_numbers=[1], repetitions=1,
            ).get_json()
            return self.client.post(
                f"/api/quiz/{quiz['token']}/answer", json={"answer": answer}
            ).get_json()["result"]

        reordered = ask("erhalten nehmen packen erobern")
        self.assertTrue(reordered["correct"])
        self.assertFalse(reordered["accepted_as_typo"])

        reordered_typo = ask("erhaltzen nehmen packen erobern")
        self.assertTrue(reordered_typo["correct"])
        self.assertTrue(reordered_typo["accepted_as_typo"])

        self.assertFalse(ask("erhalten nehmen packen")["correct"])
        self.assertFalse(ask("erhalten nehmen packen erobern bekommen")["correct"])

    def test_cards_move_correct_forward_and_wrong_to_box_one(self):
        quiz = self.start(mode="cards", card_count=2).get_json()
        first_id = quiz["current"]["id"]
        quiz = {**quiz, **self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": "falsch"}).get_json()}
        # The currently displayed second card still visibly belongs to box 1.
        self.assertEqual(quiz["boxes"]["1"], 2)
        second_answer = "sehen" if quiz["current"]["foreign_text"] == "videre" else "der Freund"
        quiz = {**quiz, **self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": second_answer}).get_json()}
        self.assertEqual(quiz["box"], 2)
        self.assertEqual(quiz["boxes"]["2"], 1)
        self.assertEqual(quiz["boxes"]["1"], 1)

    def test_one_card_reaches_mastered_after_all_five_boxes(self):
        quiz = self.start(mode="cards", card_count=1).get_json()
        for expected_box in range(1, 6):
            self.assertEqual(quiz["box"], expected_box)
            quiz = {**quiz, **self.client.post(
                f"/api/quiz/{quiz['token']}/answer", json={"answer": "der Freund"}
            ).get_json()}
        self.assertTrue(quiz["completed"])
        self.assertEqual(quiz["mastered"], 1)
        self.assertEqual(quiz["answered"], 5)

    def test_crud_and_csv_export(self):
        created = self.client.post("/api/vocabulary", json={
            "source_id": self.source_id, "foreign_text": "novus", "german_text": "neu", "declension": "a um", "lesson": "3",
        })
        self.assertEqual(created.status_code, 201)
        vocabulary_id = created.get_json()["id"]
        self.assertEqual(self.client.patch(f"/api/vocabulary/{vocabulary_id}", json={"german_text": "ein neu"}).status_code, 200)
        exported = self.client.get(f"/api/sources/{self.source_id}/export")
        rows = list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig"))))
        self.assertTrue(any(row["fremdsprache"] == "novus" and row["deutsch"] == "ein neu" for row in rows))
        self.assertEqual(self.client.delete(f"/api/vocabulary/{vocabulary_id}").status_code, 200)

    def test_bulk_manual_entry(self):
        response = self.client.post("/api/vocabulary/bulk", json={
            "source_id": self.source_id,
            "items": [
                {"foreign_text": "amo", "german_text": "lieben", "lesson": "3", "declension": ""},
                {"foreign_text": "rex", "german_text": "der König", "lesson": "3", "declension": "regis m"},
                {"foreign_text": "", "german_text": "", "lesson": "", "declension": ""},
            ],
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["count"], 2)

    def test_mistakes_can_create_and_extend_a_reusable_list(self):
        def finish_with_error(vocabulary_id, answer):
            quiz = self.start(
                vocabulary_ids=[vocabulary_id], source_ids=[], lessons=[],
                block_size=1, block_numbers=[1], repetitions=1,
            ).get_json()
            word = quiz["current"]["foreign_text"]
            self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": "falsch"})
            self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": answer})
            return quiz["token"], word

        items = self.client.get(f"/api/vocabulary?source_id={self.source_id}").get_json()["items"]
        first, second = items[0], items[1]
        token_one, _ = finish_with_error(first["id"], first["german_text"])
        created = self.client.post(f"/api/quiz/{token_one}/mistakes/save", json={"name": "Klausur"})
        self.assertEqual(created.status_code, 200)
        list_id = created.get_json()["list_id"]
        self.assertEqual(created.get_json()["total"], 1)

        token_two, _ = finish_with_error(second["id"], second["german_text"])
        extended = self.client.post(f"/api/quiz/{token_two}/mistakes/save", json={"list_id": list_id})
        self.assertEqual(extended.get_json()["added"], 1)
        self.assertEqual(extended.get_json()["total"], 2)

        token_three, _ = finish_with_error(first["id"], first["german_text"])
        duplicate = self.client.post(f"/api/quiz/{token_three}/mistakes/save", json={"list_id": list_id})
        self.assertEqual(duplicate.get_json()["added"], 0)
        self.assertEqual(duplicate.get_json()["total"], 2)
        study_list = self.client.get(f"/api/study-lists/{list_id}").get_json()
        self.assertEqual({item["id"] for item in study_list["items"]}, {first["id"], second["id"]})

    def test_through_mode_alternates_latin_and_german(self):
        response = self.client.post("/api/quiz/start", json={
            "mode": "through", "source_ids": [self.source_id], "lessons": ["1"],
            "block_size": 1, "block_numbers": [1], "repetitions": 1,
            "timer_enabled": True, "timer_seconds": 3,
        })
        self.assertEqual(response.status_code, 201)
        quiz = response.get_json()
        self.assertEqual(quiz["side"], "foreign")
        self.assertEqual(quiz["total_steps"], 2)
        german = self.client.post(f"/api/quiz/{quiz['token']}/advance").get_json()
        self.assertEqual(german["side"], "german")
        self.assertTrue(german["current"]["german_text"])
        completed = self.client.post(f"/api/quiz/{quiz['token']}/advance").get_json()
        self.assertTrue(completed["completed"])
        self.assertEqual(completed["answered"], 1)

    def test_optional_admin_and_learner_access(self):
        database = str(Path(self.tempdir.name) / "auth.sqlite3")
        protected = create_app({
            "TESTING": True, "DATABASE": database,
            "ADMIN_ACCESS_CODE": "admin-secret", "LEARNER_ACCESS_CODE": "learn-secret",
            "SECRET_KEY": "test-secret",
        })
        client = protected.test_client()
        self.assertEqual(client.get("/").status_code, 302)
        self.assertEqual(client.get("/api/bootstrap").status_code, 401)
        login = client.post("/access", data={"access_code": "learn-secret"})
        self.assertEqual(login.status_code, 302)
        self.assertEqual(client.get("/api/bootstrap").get_json()["auth"]["role"], "learner")
        self.assertEqual(client.post("/api/vocabulary/bulk", json={"source_id": 1, "items": []}).status_code, 403)

    def test_ai_import_requires_key_and_reviewed_draft(self):
        old_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            response = self.client.post(
                "/api/ai/extract",
                data={"images": (io.BytesIO(b"not-a-real-image"), "page.jpg")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 503)
        finally:
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
        self.assertEqual(self.client.post("/api/ai/commit", json={
            "draft_token": "not-reviewed", "source_id": self.source_id,
            "items": [{"foreign_text": "amo", "german_text": "lieben"}],
        }).status_code, 404)

        with self.app.app_context():
            get_db().execute(
                "INSERT INTO ai_drafts VALUES (?, ?, ?)",
                ("reviewed", json.dumps([{"foreign_text": "raw", "german_text": "raw"}]), "2026-01-01T00:00:00+00:00"),
            )
            get_db().commit()
        committed = self.client.post("/api/ai/commit", json={
            "draft_token": "reviewed", "source_id": self.source_id,
            "items": [{"foreign_text": "amo", "german_text": "lieben", "lesson": "4", "declension": ""}],
        })
        self.assertEqual(committed.status_code, 201)
        self.assertEqual(committed.get_json()["count"], 1)

    def test_recent_attempts_drive_difficulty(self):
        quiz = self.start(block_size=1, block_count=1, repetitions=5).get_json()
        for _ in range(5):
            quiz = {**quiz, **self.client.post(f"/api/quiz/{quiz['token']}/answer", json={"answer": "sicher falsch"}).get_json()}
        items = self.client.get(f"/api/vocabulary?source_id={self.source_id}").get_json()["items"]
        amicus = next(item for item in items if item["foreign_text"] == "amicus")
        self.assertIn(amicus["difficulty"], ("schwierig", "sehr schwierig"))

    def test_quiz_can_filter_directly_by_difficulty(self):
        with self.app.app_context():
            vocabulary_id = get_db().execute(
                "SELECT id FROM vocabulary WHERE source_id = ? AND foreign_text = 'amicus'", (self.source_id,)
            ).fetchone()[0]
            get_db().executemany(
                "INSERT INTO attempts(vocabulary_id, session_token, correct, answer, created_at) VALUES (?, 'difficulty', 0, '', '2026-01-01')",
                [(vocabulary_id,)] * 12,
            )
            get_db().commit()
        quiz = self.client.post("/api/quiz/start", json={
            "mode": "block", "source_ids": [self.source_id], "lessons": ["1", "2"],
            "difficulties": ["sehr schwierig"], "block_size": 5,
            "block_numbers": [1], "repetitions": 1,
        }).get_json()
        self.assertEqual(quiz["total"], 1)
        self.assertEqual(quiz["current"]["foreign_text"], "amicus")


if __name__ == "__main__":
    unittest.main()
