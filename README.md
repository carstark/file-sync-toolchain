Projektidee

Dieses Projekt dient dazu, Dateien auf Mac, Windows-Rechner und USB-Laufwerk kontrolliert und nachvollziehbar abzugleichen. Der Grundgedanke ist dabei nicht, sofort blind zu synchronisieren, sondern zunächst auf jedem System einen Bestands-Snapshot zu erzeugen, damit Unterschiede sauber erkannt werden können.

Wichtig ist: Keiner dieser drei Orte ist automatisch die alleinige Wahrheit. Sowohl auf dem Mac als auch auf dem Windows-Rechner können neue, geänderte oder gelöschte Dateien entstehen. Der USB-Stick ist dabei nicht nur ein Transportmedium, sondern der gemeinsame Abgleichsraum, über den die Zustände zusammengeführt und verglichen werden können.

Dafür arbeitet das Projekt in klar getrennten Phasen: Zuerst wird mit current_journal.py ein Journal des aktuellen Bestands erzeugt. Danach vergleicht journal_checker.py die drei Journals miteinander, um echte Unterschiede zu erkennen. Anschließend sammelt file_harvester.py nur noch die Dateien ein, die tatsächlich betroffen sind. Erst in einem letzten Schritt setzt change_execution.py die Änderungen kontrolliert um.

Diese Trennung ist bewusst gewählt, weil sie Risiken reduziert. Es soll nicht aus Versehen etwas überschrieben oder gelöscht werden, nur weil ein Vergleich unklar war. Gerade bei großen Datenmengen ist es wichtig, erst zu verstehen, was wo anders ist, bevor man irgendetwas verändert.

Für den ersten Abgleich wird bewusst nicht über alle Dateien gehasht. Stattdessen reichen Metadaten wie Pfad, Größe und Änderungszeitpunkt aus, um den Bestand schnell zu erfassen. Hash-Prüfungen werden später nur dort eingesetzt, wo sie wirklich nötig sind, also bei den Dateien, die tatsächlich verändert werden sollen.

Zusätzlich werden typische macOS-Metadaten wie ._* konsequent ignoriert. Diese Dateien sind keine eigentlichen Nutzdaten, sondern technische Begleitdateien, die auf Windows und USB sonst für unnötiges Rauschen sorgen würden.

Das Ziel des Projekts ist ein System, das schnell genug für große Bestände, robust gegen Plattform-Unterschiede und auch nach Monaten noch verständlich ist. Es soll nicht nur technisch funktionieren, sondern auch gedanklich sauber bleiben: erst erfassen, dann vergleichen, dann gezielt sammeln, dann kontrolliert ausführen.
