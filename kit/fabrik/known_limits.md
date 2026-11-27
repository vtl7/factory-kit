# Bekannte Grenzen des Fabrik-Kits

Diese Grenzen sind dokumentiert und bewusst angenommen. `review.py` übernimmt sie ausschließlich aus der regulären Datei in der Merge-Base.

- Skills sind Anweisungen, keine Zustandsmaschine. Harte Regeln liegen deshalb in Skripten.
- Der Hook sieht nur Befehle. Ein Merge über ein Skript fällt erst hinterher in der CI auf.
- Projektbefehle laufen ohne GitHub-Token und ohne gespeicherte Checkout-Zugangsdaten. Der Aufräumschritt von `actions/checkout` erhält auf demselben Runner ein Gate-Token mit `contents: read`, und Schreibrechte erhalten nur Jobs, die keinen PR-Code ausführen. Das Gate-Skript kommt aus dem Basiszweig, bei der ersten Einbindung aus dem PR.
- Ein eigener Job setzt das Risiko-Label an PR und referenzierten Issues mit dem Skript aus dem Basiszweig. Fehlt es dort, entfällt das Label. Schreibrechte auf Inhalte braucht nur die Merge-Prüfung auf dem Hauptzweig für Revert-PRs.
- Die Workflow-Datei selbst nimmt GitHub aus dem PR. Wer sie ändert, ist T2 und braucht eine Bestätigung. Eine echte Sperre gibt es erst mit Branch-Schutz.
- Die Ship-Stufe ist die Stufe des PR, aus dem der ausgelieferte Commit stammt.
- Prüft Claude allein, fehlen die blinden Flecken eines zweiten Herstellers.
- Kindprozesse des Partners können nach Bauende noch Dateien ändern. Claude prüft den Diff vor dem Commit. Danach läuft das Gate.
- In Containern kann ein Zombie-Wrapper ohne Exitdatei `build.py wait` dauerhaft auf Exitcode 9 halten. Auch `build.py clear` bricht dann mit Exitcode 2 ab. Zeigt die Sichtprüfung der Prozessgruppe nur noch Zombies, entferne im betroffenen Worktree nach Prüfung des Diffs gezielt die Zustandsdatei `$(git rev-parse --absolute-git-dir)/fabrik/build.json`.
- Beim Squash gelangen die `Gebaut-von`-Trailer der einzelnen Commits nicht auf den Hauptzweig. Die Bauherkunft wird am PR geprüft.
- Das Urteil ist an den Head-Commit gebunden. Wer danach den Zielzweig des PR wechselt, greift bewusst in den geprüften Stand ein.
- Läufe ohne Sitzungsdatei, etwa mit `--ephemeral`, und Verbrauch auf anderen Geräten werden erst nach dem nächsten Partnerlauf sichtbar. Ein Quellstand über 30 Minuten wird als veraltet protokolliert. Die Bremse wertet weiterhin den gelesenen Stand aus.
- Die Kontingentquelle `usage.py codex-kontingent` nimmt das jüngste Ereignis mit Kontingentangabe. Enthielte ein jüngeres Ereignis nur ein Wochenfenster, verdrängte es einen älteren 5-Stunden-Wert, und die 90-%-Bremse bliebe aus. Codex liefert heute immer beide Fenster. Offen seit fabrik-kit#17, Runde 3.
- Die Suche bricht ab, sobald eine Sitzungsdatei älter ist als der beste Messwert. Eine wiederhergestellte Sitzungsdatei mit älterer mtime, aber jüngerem Messwert, wird dann übersehen. Offen seit fabrik-kit#17, Runde 3.
- Der eigene YAML-Leser kennt nicht jeden Sonderfall: In `check # Beispiel: echo ok` gilt der Doppelpunkt nach dem Kommentar als Schlüsseltrenner. Ablösung durch TOML in fabrik-kit#18.
- Ein unmaskiertes U+2028 im JSON-String zerlegt die Schlusszeile der Antwort. Das APPROVE wird als CHANGES gewertet. Ablösung durch ein festes JSON-Urteil in fabrik-kit#18.
- Die Erkennung von Codeblock-Marken in `closing_line` folgt CommonMark nicht vollständig: Sie entfernt die Einrückung vorher, sodass eine um vier Leerzeichen eingerückte Abschlussmarke als Abschluss gilt, und sie wertet eine Öffnungsmarke mit Backticks im Zusatztext als Öffnung. Ein APPROVE zählt dadurch nur, wenn der Prüfer selbst so formatiert. Offen seit fabrik-kit#20, Runde 3. Ablösung durch ein festes JSON-Urteil in fabrik-kit#18.
