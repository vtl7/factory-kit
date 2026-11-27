"""Alle Randfälle aus den Prüfrunden 1 bis 4 zu PR #15 in zwei Tabellen.

Neue Randfälle kommen als Zeile in die passende Tabelle, nicht als eigener Test.
"""
import json
import unittest

from helpers import lib
import review

YAML_CASES = [
    # (Herkunft, YAML-Zeilen, Schlüssel, erwarteter Wert)
    ("Runde 1: Komma im zitierten Listenwert",
     'a: ["Co-Authored-By: Doe, Jane <j@example.org>"]  # Projektregel', "a",
     ["Co-Authored-By: Doe, Jane <j@example.org>"]),
    ("Runde 1: maskiertes Anführungszeichen vor #",
     'b:\n  - "sagt \\" # kein Kommentar"  # Kommentar', "b", ['sagt " # kein Kommentar']),
    ("Runde 1: Backslash am Ende eines Zitats", 'c: "Ende \\\\"  # Kommentar', "c", "Ende \\"),
    ("Runde 2: Apostroph mitten im Pfad", "p: [docs/owner's-guide.md, config/**]", "p",
     ["docs/owner's-guide.md", "config/**"]),
    ("Runde 2: Apostroph in einem Befehl", "check: echo it's ok  # Kommentar", "check", "echo it's ok"),
    ("Runde 3: Apostroph nach Doppelpunkt in Inline-Liste",
     "q: [docs/owner:'s-guide.md, config/**]  # Kommentar", "q", ["docs/owner:'s-guide.md", "config/**"]),
    ("Runde 3: Apostroph nach Doppelpunkt in Blockliste",
     "s:\n  - docs/owner:'s-guide.md  # Kommentar", "s", ["docs/owner:'s-guide.md"]),
    ("Runde 4: Zitat direkt nach dem Doppelpunkt", 'check:"echo # marker"', "check", "echo # marker"),
    ("Runde 4: Apostroph mitten im Listeneintrag, danach Kommentar",
     "protected_paths: [a 'b, config/**] # Kommentar", "protected_paths", ["a 'b", "config/**"]),
    ("Runde 5: Glob-Klammern in unzitiertem Listeneintrag",
     "protected_paths: [docs/a[bc].md, config/**]  # Kommentar", "protected_paths",
     ["docs/a[bc].md", "config/**"]),
    ("Glob-Klammer mit Komma im Eintrag", "g: [src/[a,b]/*.py, tests/]", "g", ["src/[a,b]/*.py", "tests/"]),
    ("Glob-Klammern in Blockliste", "h:\n  - docs/a[bc].md  # Kommentar", "h", ["docs/a[bc].md"]),
    ("Zitat am Wertanfang schützt #", 'quoted: "a # not a comment"', "quoted", "a # not a comment"),
    ("Einfaches Zitat mit verdoppeltem Apostroph", "single: 'it''s'  # Kommentar", "single", "it's"),
    ("Zitat am Anfang eines Listeneintrags", "r: ['x, #y', \"z\", plain]  # Kommentar", "r",
     ["x, #y", "z", "plain"]),
    ("# ohne Leerzeichen davor ist kein Kommentar", "u: a#b", "u", "a#b"),
    ("Zitat erst mitten im Wert öffnet nicht, ' #' bleibt Kommentar",
     'cmd: echo "a # b"', "cmd", 'echo "a'),
]

APPROVE_WITH = json.dumps({"urteil": "APPROVE", "kriterien": [{"nr": 1, "satz": "Der Test belegt x."}]})
SIGNED_APPROVE = json.dumps(dict(json.loads(APPROVE_WITH), pruef_id="aktuell"))
APPROVE_BARE = '{"urteil":"APPROVE"}'

APPROVE_OUTSIDE_CLOSING_LINE = {
    "urteil": "CHANGES", "kriterien": [], "frage": "",
    "funde": ["Das APPROVE steht nicht in der Schlusszeile der Antwort und gilt deshalb nicht."],
}

ANSWER_CASES = [
    # (Herkunft, Antwort, Kriterien Pflicht, erwartetes Ergebnis, optional Prüf-ID)
    ("Runde 1: verneintes Freitext-Urteil",
     "Ich kann das frühere Prüfurteil: APPROVE wegen des Tests nicht bestätigen", False, None),
    ("Runde 1: Freitext-APPROVE nach CHANGES", '{"urteil":"CHANGES"}\nUrteil: APPROVE', False, None),
    ("Runde 2: JSON-Zitat mitten im Satz",
     f"Die frühere Antwort {APPROVE_BARE} ist wegen des Fehlers ungültig.", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 2: JSON-Zitat nach Doppelpunkt", f"Ungültig: {APPROVE_BARE}", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 2: JSON-Zitat in Backticks", f"`{APPROVE_BARE}` gilt nicht.", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 3: zitiertes APPROVE mit Kriteriumssatz",
     f"Verworfen: {APPROVE_WITH} wegen des Fehlers.", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 3: APPROVE in mehrzeiliger JSON-Liste", f"[\n{APPROVE_WITH}\n]", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 3: kriterienloses APPROVE in mehrzeiliger JSON-Liste", f"[\n{APPROVE_BARE}\n]", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 3: mehrzeiliges JSON-Objekt", '{"urteil":\n "APPROVE"}', False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 3: Text nach dem Objekt", f"{APPROVE_WITH}\nDas war die alte Antwort.", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Runde 4: Codeblock-Abschluss in derselben Zeile", f"```json\n{APPROVE_BARE}```", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Schlusszeile ohne Kriterien", f"Fertig.\n  {APPROVE_BARE}  \n", False, "APPROVE"),
    ("Schlusszeile im Codeblock", f"```json\n{APPROVE_BARE}\n```", False, "APPROVE"),
    ("Zu kurze Abschlussmarke im Codeblock",
     f"````json\n{APPROVE_BARE}\n```", False, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Gleich lange Abschlussmarke im Codeblock", f"````json\n{APPROVE_BARE}\n````", False, "APPROVE"),
    ("Längere Abschlussmarke im Codeblock", f"```json\n{APPROVE_BARE}\n````", False, "APPROVE"),
    ("Codeblock mit Backtick-Zeile im Inhalt", f"```json\n```text\n{APPROVE_BARE}\n```", False, "APPROVE"),
    ("Schlusszeile mit Kriterien, Leerzeilen danach", f"Geprüft.\n{APPROVE_WITH}\n\n", False, "APPROVE"),
    ("CHANGES zählt auch im Freitext", f"{APPROVE_WITH}\nPrüfurteil: CHANGES", False, "CHANGES"),
    ("Signiertes APPROVE vor Klartext-CHANGES", SIGNED_APPROVE + "\nUrteil: CHANGES", True, "CHANGES", "aktuell"),
    ("Signiertes APPROVE vor Klartext-ESCALATE", SIGNED_APPROVE + "\nUrteil: ESCALATE", True, "ESCALATE", "aktuell"),
    ("Signiertes APPROVE vor Klartext-APPROVE",
     SIGNED_APPROVE + "\nUrteil: APPROVE", True, APPROVE_OUTSIDE_CLOSING_LINE, "aktuell"),
    ("Signiertes CHANGES vor Klartext-APPROVE",
     '{"urteil":"CHANGES","pruef_id":"aktuell"}\nUrteil: APPROVE', True, None, "aktuell"),
    ("Klartext-Urteil ohne JSON bei erwarteter Prüf-ID", "Urteil: CHANGES", True, lib.FabrikError, "aktuell"),
    ("Falsche Prüf-ID im letzten JSON-Urteil vor Klartext",
     SIGNED_APPROVE + '\n{"urteil":"CHANGES","pruef_id":"alt"}\nUrteil: ESCALATE',
     True, lib.FabrikError, "aktuell"),
    ("Signiertes APPROVE vor verwaister Codeblock-Marke",
     SIGNED_APPROVE + "\n```", True, APPROVE_OUTSIDE_CLOSING_LINE, "aktuell"),
    ("Signiertes APPROVE im Codeblock", "```json\n" + SIGNED_APPROVE + "\n```", True, "APPROVE", "aktuell"),
    ("Mit Blatt: APPROVE ohne Kriterien ungültig", APPROVE_BARE, True, None),
    ("Mit Blatt: zitiertes APPROVE mit Kriterien und Verneinung",
     f"Frühere Antwort: {APPROVE_WITH}\nIch kann die Freigabe nicht bestätigen.", True, APPROVE_OUTSIDE_CLOSING_LINE),
    ("Mit Blatt: APPROVE mit Kriterien in der Schlusszeile",
     f"Geprüft.\n{APPROVE_WITH}", True, "APPROVE"),
]


class EdgeCaseTableTest(unittest.TestCase):
    def test_yaml_cases(self):
        for origin, text, key, expected in YAML_CASES:
            with self.subTest(origin):
                self.assertEqual(lib.load_yaml(text + "\n")[key], expected)

    def test_answer_cases(self):
        for origin, text, require, expected, *review_id in ANSWER_CASES:
            with self.subTest(origin):
                if expected is lib.FabrikError:
                    with self.assertRaisesRegex(lib.FabrikError, "Prüf-ID"):
                        review.parse_answer(text, *review_id, require_criteria=require)
                    continue
                answer = review.parse_answer(text, *review_id, require_criteria=require)
                if isinstance(expected, dict):
                    self.assertEqual(answer, expected)
                else:
                    self.assertEqual(answer and answer["urteil"], expected)


if __name__ == "__main__":
    unittest.main()
