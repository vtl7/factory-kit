<!-- fabrik:start -->
## Ablauf mit dem Fabrik-Kit

Angelegt von `fabrik init`. Der Block zwischen den Marken wird vom Kit gepflegt.

- Aufträge laufen über GitHub-Issues. Labels sind die Spalten: `Idee`, `Konzept`, `Stapel`, `In Arbeit`, `Wartet auf dich`. Dazu `nachts`, `T0` bis `T3`, `Vorfall`, `Festgefahren`, `Prüfung: Claude allein`.
- Skills: `auftrag` klärt und legt das Issue an, `bauen` baut in Scheiben, `pruefen` lässt über Kreuz prüfen, `abnahme` liefert aus und schließt das Issue.
- Befehle des Projekts stehen in `.fabrik/fabrik.yml`: `check`, `test_cmd`, `ship`, `verify`.
- Nie direkt auf den Hauptzweig pushen. Jede Änderung über Branch und PR mit `Refs #N`, nie `Closes`.
- Merge und Ship entscheidet der Stufen-Hook: T0 und T1 mit grünem Gate und Prüfurteil `APPROVE`, T2 fragt einmal, T3 immer. Ohne Prüfurteil kein Merge.
- Den Hook nie umgehen, auch nicht über Skripte oder die API. Ein Merge ohne Prüfurteil wird in der CI als Vorfall gemeldet und zurückgenommen.
- Höchstens drei Prüfrunden je Auftrag, auch über Neustarts. Danach genau eine fachliche Frage an den Product Owner.
- Das Issue schließt erst nach der Abnahme. Bei jedem Halt einen Übergabe-Kommentar ins Issue: Commit, PR, letzter Beleg, nächster Schritt.
<!-- fabrik:end -->
