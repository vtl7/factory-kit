# Arbeitsregeln für das Fabrik-Kit

Dieses Repo ist das Kit selbst. Es wird ganz normal in Claude Code gepflegt. Maßgeblich ist `KONZEPT.md`.

## Sprache

- Doku, Skills, Berichte und Meldungen der Skripte auf Deutsch.
- Code, Bezeichner, Dateinamen und Commit-Betreffs der Skripte auf Englisch, Commit-Texte dürfen deutsch sein.

## Allgemein bleiben

- Das Kit kennt kein Projekt. Keine Repo-Namen, Pfade, Befehle oder Begriffe eines Produkts in `kit/`, `bin/` oder `tests/`. Alles Projektspezifische steht in `.fabrik/fabrik.yml` des Zielprojekts.
- Keine persönlichen Daten: keine Namen, privaten Repos, lokalen Pfade, E-Mail-Adressen oder Auswertungen eigener Projekte in Doku, Code oder Tests.

## Technik

- Skripte unter `kit/fabrik/` laufen mit `python3` ab 3.9, nur Standardbibliothek. Sie laufen im Zielprojekt lokal, in geplanten Aufgaben, in der Cloud und in der CI.
- Aufrufe von `gh` und `git` laufen über eine austauschbare Funktion, damit Tests ohne Netz auskommen.
- Tests: `python3 -m unittest discover -s tests`. Jede Änderung an einem Skript kommt mit Test.
- Skills höchstens etwa 40 Anweisungen. Harte Regeln gehören in ein Skript, nicht in einen Skill.
- Worktrees und virtuelle Umgebungen nie unter iCloud-Pfaden.

## Lizenz

- Das Kit steht unter der MIT-Lizenz (`LICENSE`).
- Kein fremder Code mit Copyleft-Lizenz (etwa GPL oder AGPL), auch nicht umformuliert.
- Fremder Code mit freizügiger Lizenz (MIT, BSD, Apache-2.0) nur mit Herkunft und Lizenztext im Repo.
- Skripte bleiben bei der Standardbibliothek. Eine neue Abhängigkeit braucht eine Lizenzprüfung im PR.

## Versionen

- Semantische Versionierung, die Nummer steht in `kit/VERSION`.
- Jede Änderung an `kit/` oder `bin/` bekommt einen Eintrag in `CHANGELOG.md` unter „Unveröffentlicht“.
- Eine Veröffentlichung folgt `RELEASING.md`.

## Git

- Nie direkt auf `main` pushen. Jede Änderung über Branch und PR.
- Commits nur mit der noreply-Adresse von GitHub, nie mit einer privaten E-Mail-Adresse.

## Funktionsstopp

Während der Einführung wird nur behoben, was eine Lieferung blockiert. Neue Funktionen (Router, mehrere Partner, Statusseite, `fabrik update`, Sweeps) erst nach bestandener erster Woche. Ideen kommen als Issue mit Label `Idee`.
