---
name: pruefen
description: Prüfung über Kreuz eines PR durch den Partner oder einen frischen Claude-Subagenten, mit festem Urteil APPROVE, CHANGES oder ESCALATE und höchstens drei Runden je Auftrag. Nutzen, wenn ein PR mit grünem Gate geprüft werden soll.
---

# Prüfen über Kreuz

Wer baut, prüft nicht. Das Skript `review.py` setzt die harten Regeln durch: Gate rot heißt keine Prüfung, gleicher Commit heißt keine neue Prüfung, nach drei Runden ist Schluss.

Ergänze bei Zwischenprüfungen sowohl für `run` als auch für `prompt` die Argumente `--slice K/N --pending "<was noch kommt>"`. Bei der letzten Scheibe entfällt `--pending`. Verwende dann `--slice N/N` oder lasse die Scheibenangabe weg.

## Mit Partner

1. Stand pushen. Dann: `python3 .claude/fabrik/review.py run --pr <PR>`. Der Lauf dauert mehrere Minuten (erst Gate, dann Partner): im Vordergrund mit zehn Minuten Zeitgrenze aufrufen und in derselben Runde auf das Urteil warten.
2. Exitcode 0 heißt `APPROVE`, 10 `CHANGES`, 11 `ESCALATE`.
3. Exitcode 3: kein Partner, oder der Auftrag ist `Dringend`. Verwende die von `run` ausgegebene Risikostufe. Bei T0 oder T1 weiter unter „Ohne Partner“. Bei T2 oder T3 nicht ohne Partner prüfen: Übergabe, `Stapel`, halten. Bei Exitcode 14 schreibe die Übergabe mit Reset-Zeit, setze `Stapel` und halte an.
4. Bei Exitcode 12 hat der Partner mitgebaut. Folge auf jeder Stufe „Ohne Partner“ mit dem ausgegebenen Prompt-Befehl. Bei Exitcode 13 hat sich der PR geändert. Rufe `run` für den neuen Stand mit denselben Argumenten erneut auf.
5. Exitcode 6: Antwort ohne Urteil. Wiederhole `run` einmal mit denselben Argumenten. Bei erneutem Exitcode 6 gelten die Regeln von Exitcode 3; verwende die ausgegebene Risikostufe.

## Ohne Partner

6. Hole den Prompt mit den Argumenten aus `run` und speichere ihn wie hier gezeigt. `python3 .claude/fabrik/review.py prompt --pr <PR> > /tmp/fabrik-pruefung-<PR>.md`.
7. Prüfe Claudes Wochenverbrauch vor jedem neuen Auftrag und vor jedem Claude-Einsatz in diesem Skill, auch nachts. Starte nur unter 70 %. Ab 25 % darf der Verbrauch dem verstrichenen Wochenanteil höchstens 10 Prozentpunkte voraus sein. Bei Claude-Bremse schreibe die Übergabe und halte an. Sonst starte einen frischen Subagenten ohne deinen Bauverlauf mit dem Modell aus der Zeile „Modell für den Subagenten“.
8. Gib ihm nur den Inhalt dieser Datei und den Auftrag, nur lesend zu prüfen.
9. Der Subagent übernimmt `pruef_id` unverändert aus dem Prompt in seine JSON-Antwort mit `urteil`, `kriterien` (je ein Eintrag mit `nr` und `satz`), `funde` und `frage`. Speichere seine Antwort unverändert in eine Datei.
10. Übernimm ein explizites `--issue` auch beim Eintragen des Urteils. `python3 .claude/fabrik/review.py record --pr <PR> --reviewer claude --file <Datei>`.
11. Das Skript setzt das Label `Prüfung: Claude allein` an PR und Issue nur, wenn Claude auch gebaut hat.

## Urteil umsetzen

12. Bei `APPROVE` zurück zum Skill `bauen`. Nach der Zwischenprüfung folgt die nächste Scheibe, nach der Gesamtprüfung der Merge.
13. `CHANGES`: jeden Fund beheben oder im PR begründen, warum nicht. Erst mit neuem Commit wieder prüfen.
14. `ESCALATE`: anhalten. Die eine Frage aus dem Urteil geht an den Product Owner.

## Runden und Festgefahren

15. Jedes `CHANGES` oder `ESCALATE` ist eine Runde. Sie zählen am Issue, auch über neue PRs und Neustarts.
16. Exitcode 4 heißt Festgefahren. Das Skript setzt `Festgefahren` und `Wartet auf dich`.
17. Dann schreibe ins Issue eine Diagnose in höchstens 30 Zeilen und genau eine fachliche Frage.
18. Exitcode 5: kein neuer Diff. Nicht noch einmal prüfen lassen, erst etwas ändern. Direkt nach einem Push kann GitHub den neuen Stand noch nicht kennen: eine Minute warten, einmal erneut aufrufen.
19. Runden zurücksetzen nur mit neuer Voraussetzung vom Product Owner:
    `python3 .claude/fabrik/review.py reset --issue N --reason "<was neu ist>"`.
20. Stand ansehen: `python3 .claude/fabrik/review.py status --pr <PR>`.

## Ein Prüflauf je Scheibe

21. Prüfe nach jeder Scheibe mit grünem Gate. Den Entwurfs-PR gibt es ab der ersten Scheibe (Skill `bauen`); in einer Folgerunde liefert das Skript bei passendem Vorurteil den Teil-Diff, die Dateiliste des Gesamtdiffs und die bisherigen Funde.
22. Urteile nie selbst über den eigenen Code und trage nie selbst ein Urteil ein, das kein Prüfer abgegeben hat.
