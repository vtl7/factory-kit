# Fabrik-Kit – Konzept

Stand 18. November 2026, seither fortgeschrieben. Bei Widerspruch zu anderen Dokumenten gilt dieses Dokument.

## Kern in einem Absatz

Die guten Ideen einer vorher eigenständig betriebenen Software-Fabrik bleiben, ihre eigene Maschinerie fällt weg. Claude Code übernimmt Bedienung, Sitzungen und Zeitpläne. Das Kit liefert nur, was Claude Code nicht kann: Ablaufregeln als Skills, harte Regeln als Skripte und Hooks, und die Prüfung über Kreuz mit einem Partner. Das Kit kennt kein Projekt. Projektwissen liegt im jeweiligen Repo: `AGENTS.md` und `.fabrik/fabrik.yml`.

## Bausteine

- **Leitstand:** ein Chat pro Produkt. Nur dort schreibt der Product Owner. Er klärt Aufträge, legt Issues an und startet Bau-Sitzungen nacheinander.
- **Planung:** GitHub-Issues. Labels sind die Spalten: `Idee`, `Konzept`, `Stapel`, `In Arbeit`, `Wartet auf dich`. Dazu `nachts`, `Dringend`, `T0` bis `T3`, `Vorfall`, `Festgefahren`, `Prüfung: Claude allein`. Erledigt heißt geschlossen, und zwar erst nach der Abnahme.
- **Bau-Sitzungen:** je Teilauftrag eine Sitzung mit eigenem Worktree. Sie öffnet den PR, merged ihn und schließt das Issue nach der Abnahme.
- **Ablauf-Skills:** `auftrag`, `bauen`, `pruefen`, `abnahme`, je höchstens etwa 40 Anweisungen.
- **Harte Sperren:** Gate-Skript, Risiko-Skript, Stufen-Hook, CI-Workflow.
- **Geplante Aufgaben:** Nachtschicht und Morgenbericht. Sie kommen nach dem Kern.

## Ablauf eines Auftrags

1. **auftrag:** Auftragsblatt mit Ziel, Kriterien, Belegvertrag, Verhaltensänderung, Fundstellen, Größe S/M/L und höchstens drei Fragen. Bei L zuerst Forschung durch einen frischen Subagenten und eine Design-Gegenprüfung. Der Product Owner bestätigt.
2. **bauen:** Claude schneidet vertikale Scheiben zu. Der Partner baut nach Prompt, bei Verhaltensänderungen mit Test zuerst. Claude prüft den Diff, committet mit `Gebaut-von`-Trailer und startet das Gate. Baut Claude selbst, committet er ohne `Gebaut-von`. Die Trailer aus `commit_trailers` in `.fabrik/fabrik.yml` trägt jeder Commit. Bei Rot prüft kein Modell.
3. **pruefen:** Wer gebaut hat, prüft nicht. Nach grünem Gate prüft ein frischer Claude-Subagent, wenn der Partner am PR mitgebaut hat. Sonst prüft der verfügbare Partner. Der Prüfer liest Auftragsblatt, Diff und Gate-Ausgabe und urteilt mit `APPROVE`, `CHANGES` oder `ESCALATE`.
4. **PR, CI, Merge:** Der PR verweist mit `Refs #N` auf das Issue, nie mit `Closes`. Der Stufen-Hook entscheidet über den Merge.
5. **abnahme:** Ship und Verify über die Befehle des Repos, Belege gegen den Belegvertrag. Danach schließt die Sitzung das Issue.

## Regeln gegen das alte Übergabeproblem

Die Fabrik scheiterte selten am Produktcode, sondern an ihren eigenen Übergaben: Ein Schritt verlangte, was der vorige nicht liefern konnte. Deshalb gilt:

- **Belegvertrag vor dem Bau:** Jedes Kriterium nennt vorab einen Beleg, den es wirklich gibt. Unerreichbares wird gestrichen, abgetrennt oder als „Abnahme durch den Product Owner“ markiert. Fehlt später ein Beleg, ist das ein Hinweis im Bericht, keine Sperre.
- **Keine Formprüfung von Modelltext:** Skripte prüfen nur, was Code und Tests zeigen. ADRs nur bei einer bleibenden Wahl.
- **Drei Runden je Auftrag, auch über Neustarts:** Gezählt wird jedes Urteil `CHANGES` oder `ESCALATE` am Issue. Nach der dritten Runde heißt der Auftrag „Festgefahren“: genau eine fachliche Frage an den Product Owner, Diagnose in höchstens 30 Zeilen. Zurückgesetzt wird nur mit neuer Voraussetzung. Ohne neuen Diff gibt es keine neue Prüfung.
- **Issue erst nach Abnahme schließen.** Hält eine Sitzung an, schreibt sie einen Übergabe-Kommentar: Commit, PR, letzter Beleg, genau nächster Schritt.
- **Vorfall heißt Fehler:** Erwartetes Warten auf den Product Owner ist Status, kein Vorfall.

## Modelle und Kontingent

- **Standard** für Bauen, Prüfen, Fragen und Abnahme sind Codex `gpt-6.2-sol` mit `low` und Claude Sonnet 5.5 mit `medium`.
- **Tief** wählt das Kit automatisch und ohne Rückfrage für Design und Design-Gegenprüfung bei Größe L, für PR-Prüfungen auf T2/T3 und für die Bau-Eskalation nach zwei gescheiterten Runden derselben Scheibe. Die Bau-Eskalation ist einmal pro Auftrag möglich. Tief nutzt Codex `gpt-6.2-astra` mit `medium` oder Claude Opus 5.5. In allen übrigen Fällen gilt Standard. Eine günstige Stufe entfällt. Die 6.2-Reihe hat kein Terra, die 6.1-Reihe wird nicht mehr genutzt.
- **Partnerkontingent.** Ab 90 % Verbrauch im 5-Stunden-Fenster wartet der Auftrag bis zum Reset. Nur mit `Dringend` darf Claude übernehmen. Für den Partner gibt es weder Wochenbremse noch Reserve.
- **Claude-Kontingent.** Claude startet unter 70 % Wochenverbrauch. 30 Prozentpunkte bleiben für den Product Owner. Ab 25 % Verbrauch darf dieser höchstens 10 Prozentpunkte über dem verstrichenen Wochenanteil liegen. Das gilt auch nachts.
- **Nutzung.** Das lokale JSONL-Protokoll erfasst Partnerläufe, Bremsen und Kontingentstände für ein späteres Steuerungsmodell. Die Messwerte aus dem Nutzungskonzept der Fabrik gelten vorerst als Schätzung für die 6.2-Reihe und werden bei Gelegenheit nachgemessen.
- **Prüfaufträge.** Folgerunden nach CHANGES oder ESCALATE prüfen bei passender Commit-Historie nur die Änderungen seit der Vorrunde, mit der Dateiliste des Gesamtdiffs und den bisherigen Funden. Die Gate-Ausgabe bleibt kompakt, und Kriteriensätze bleiben Pflicht.

## Risikostufen

Das Risiko-Skript stuft nach Pfaden ein. Die Kategorien stehen im Kit, ein Projekt ordnet ihnen Pfade zu und darf Kategorien ergänzen, aber keine streichen.

| Stufe | Beispiele | Merge und Ship |
| --- | --- | --- |
| T0 | Doku, Tests | durch, wenn Gate grün und Prüfurteil `APPROVE` |
| T1 | Features, Bugfixes | wie T0 |
| T2 | Migration, neue Abhängigkeit, CI und Deploy, Kit-Dateien | eine Rückfrage |
| T3 | Auth und Secrets, Zahlungen, Rechte, Löschen sensibler Daten | immer eine Rückfrage |

Der Stufen-Hook (PreToolUse auf Bash) zielt auf `gh pr merge`, den Merge über die API, `git push` nach `main` und den Ship-Befehl aus `.fabrik/fabrik.yml`. Ohne Prüfurteil sperrt er auf jeder Stufe. Nachts kann niemand bestätigen, eine Rückfrage wird dann abgelehnt und der Lauf macht mit dem nächsten Issue weiter.

## Ergebnisse der Proben (18. November, privates Wegwerf-Repo)

| Annahme | Ergebnis |
| --- | --- |
| Auto-Modus merged T0/T1 nur mit Ausnahme | merged auch ohne – Ausnahme unnötig |
| Auto-Modus hält Auslieferung an | **widerlegt** – `./ship.sh` lief ohne Halt |
| Benutzer- vs. Projektregeln | entfällt |
| Kind-Sitzungen melden zurück | bestanden; eine wartende Kind-Sitzung meldet sich nicht |
| Verpasste geplante Aufgaben laufen nach | bestanden, genau einmal |
| Nachtschicht arbeitet nach Halt weiter | teils, kein echter Halt aufgetreten |
| Geplante Aufgaben haben `gh` und Partner | bestanden; Meldung kommt als Push, nicht im Leitstand |
| Cloud lädt Skills und Hooks aus dem Repo | bestanden |
| Merge-Sperre gilt auf jedem Weg | **widerlegt** – Wrapper-Skript umgeht den Hook; Branch-Schutz braucht GitHub Pro |
| Rückfrage des Hooks ohne Zuschauer | bestanden – abgelehnt, nächster Schritt läuft |
| Kontingente maschinell lesbar | bestanden, nur als Nebenprodukt eines Laufs |

## Entscheidungen

1. **Planung in GitHub-Issues,** vollständig von Claude bedient.
2. **Merge und Ship entscheidet ein Hook nach Stufe,** nicht der Auto-Modus.
3. **Ohne GitHub Pro.** Der Hook sperrt den normalen Merge-Weg. Einen Umweg meldet ein CI-Schritt nach dem Merge auf `main` als Vorfall und öffnet einen Revert-PR.
4. **Vorgänger eingefroren:** Die eigenständige Fabrik läuft nicht mehr parallel, damit sie nicht um dasselbe Kontingent konkurriert.
5. **Einkopieren statt Plugin:** Skills und Hooks liegen im Projekt-Repo, damit sie lokal, in geplanten Aufgaben und in der Cloud laden.
6. **Nachtschicht startet je Repo einen kopflosen Lauf** im Repo-Ordner, weil eine geplante Aufgabe im Ordner läuft, in dem sie angelegt wurde. In der ersten Woche endet sie am PR.
7. **Partner austauschbar:** ein Eintrag je Rechner mit Befehlen und Modellstufen. Codex baut vorrangig. Hat der Partner mitgebaut, prüft ein frischer Claude-Subagent. Ohne eingerichteten Partner übernimmt Claude nach den eigenen Kontingentregeln. PR und Issue erhalten `Prüfung: Claude allein`, wenn Claude prüft und mindestens einen Commit selbst gebaut hat.

## Was schwächer ist als in der eigenständigen Fabrik

Die Liste steht in [`kit/fabrik/known_limits.md`](kit/fabrik/known_limits.md). `fabrik init` installiert sie nach `.claude/fabrik/`, und `review.py` gibt sie jedem Prüfer als bekannte Grenzen mit, die kein Fund sind.

## Umfang des Kerns und Funktionsstopp

Im Kern: die vier Skills, Gate, Risiko, Stufen-Hook, Prüf-Skript mit Partner und Rundenzähler, CI-Workflow mit Gate und Merge-Prüfung, `fabrik init` und `fabrik setup`.

Später, erst nach bestandener erster Woche: Router nach Kontingent, mehrere Partner, Statusseite, `fabrik update`, Wartungs-Sweep, Rückblick, Wartbarkeit mit Sperrklinke.

Während der Einführung wird am Kit nur behoben, was eine Lieferung blockiert. Alles andere sammelt der Rückblick.

## Abbruchkriterien

Drei kleine, echte Produktaufträge werden vorab gewählt. Kit-Arbeit zählt nicht als Lieferung, jede technische Rettung durch den Product Owner zählt als Eingriff.

- **Nach einer Woche Ausbau stoppen,** wenn kein Auftrag ohne Rettung abgenommen ist, derselbe Übergabefehler nach Behebung wiederkehrt, mehr Tage ins Kit als in Aufträge fließen oder der Product Owner Code oder Befehle im Chat vorgeben muss.
- **Nach zwei Wochen Versuch beenden,** wenn weniger als drei Aufträge abgenommen sind, weniger als zwei ohne Rettung, ein Auftrag in Schleifen hängt oder ein „Festgefahren“ ohne neue Voraussetzung zurückgesetzt wurde.
