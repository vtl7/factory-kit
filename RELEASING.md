# Eine Version veröffentlichen

1. **Nummer wählen** nach [Semantic Versioning](https://semver.org/lang/de/):
   - Patch (0.4.0 → 0.4.1): nur Fehlerbehebungen, kein Projekt muss etwas ändern.
   - Minor (0.4.1 → 0.5.0): neue Funktionen. Solange die Hauptnummer 0 ist, auch Inkompatibles.
   - Major (ab 1.0.0): Inkompatibles an Skripten, Skills, `fabrik.yml` oder Partnerdatei.
2. **Branch** `release/vX.Y.Z` anlegen.
3. **`kit/VERSION`** auf die neue Nummer setzen.
4. **`CHANGELOG.md`:** Einträge unter „Unveröffentlicht“ in einen neuen Abschnitt `## X.Y.Z – JJJJ-MM-TT` verschieben, „Unveröffentlicht“ leer stehen lassen.
5. **Tests:** `python3 -m unittest discover -s tests`. Der Test der Versionshistorie scheitert, wenn Nummer und Eintrag nicht zusammenpassen.
6. **PR** öffnen und nach grüner CI mergen.
7. **Tag** auf dem Merge-Commit in `main`:

   ```bash
   git tag -a vX.Y.Z -m "Fabrik-Kit X.Y.Z"
   git push origin vX.Y.Z
   ```

8. **GitHub-Release** zum Tag anlegen, Text aus dem Abschnitt der Versionshistorie.

Ein Projekt übernimmt eine neue Version mit `fabrik init` aus dem neuen Kit-Stand. Danach steht die Nummer in `.claude/fabrik/VERSION`.
