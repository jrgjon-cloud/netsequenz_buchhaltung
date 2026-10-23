# Architektur-Dokumentation

Dieses Dokument beschreibt die Gesamtarchitektur für das selbstgehostete, GoBD-konforme Steuer- und Buchhaltungs-Dashboard. Es dient als Leitfaden für zukünftige KI-Sessions und Entwicklungen.

## Tech-Stack
- **Programmiersprache:** Python 3.12
- **Web-Framework:** Flask (mit Application Factory Pattern)
- **ORM / Datenbank:** SQLAlchemy (Kompatibel mit SQLite und PostgreSQL)
- **Testing:** Pytest

## Datenquellen
- **Invoice Ninja v5:** Ausgangsrechnungen
- **Paperless-ngx:** Eingangsrechnungen / Belege
- **Legacy-Import:** Papierkram CSV

## Steuer-Logik (DE)
Das System setzt eine strikte Trennung von Datenpunkten voraus, um die korrekte steuerliche Behandlung nach deutschem Recht sicherzustellen:
- **`invoice_date`:** Leistungs- bzw. Rechnungsdatum. Maßgeblich für die Soll-Vorsteuer.
- **`payment_date`:** Zufluss-/Abflussprinzip nach § 11 EStG. Maßgeblich für die Einnahmen-Überschuss-Rechnung (EÜR) und die Ist-Versteuerung, inklusive der 10-Tage-Regel zum Jahreswechsel.

## GoBD-Unveränderbarkeit
Zur Einhaltung der GoBD (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff) gilt folgendes Konzept:
- Buchungen im Status `draft` oder `review_pending` sind frei bearbeitbar.
- Sobald Buchungen festgeschrieben werden (`status = 'locked'`), sind sie unveränderbar (Append-Only).
- Festgeschriebene Buchungen erhalten einen verketteten kryptografischen SHA-256-Hash (`current_hash` wird aus `previous_hash` + den aktuellen Buchungsdaten gebildet).
- Korrekturen an bereits gesperrten Buchungen sind nicht durch Löschung oder Modifikation möglich. Sie dürfen nur über Storno-Gegenbuchungen (`storno_of_id`) erfolgen.

## Deployment & Tailscale-Netzwerk
Die Anwendung wird vollständig über Docker (docker-compose) bereitgestellt und erfordert keine nach außen geöffneten Ports am Host-System.
Alle Verbindungen werden sicher über ein Tailscale-Sidecar in das Tailnet geroutet.

- **Dienste**: Die Infrastruktur besteht aus dem Flask-Dashboard, PostgreSQL (für Flask und Paperless), MariaDB (für Invoice Ninja), Redis, Invoice Ninja und Paperless-ngx.
- **Zero-Open-Ports**: Es werden keine `ports:` in der `docker-compose.yml` für den Host freigegeben. Der Zugriff erfolgt ausschließlich durch das Tailnet.
- **Tailscale Sidecar**: Der Container `tailscale` agiert als Reverse Proxy, indem er über `tailscale-serve.json` eingehenden internen Traffic des Tailnets auf die jeweiligen Container (`tax-dashboard`, `paperless-ngx`, `invoiceninja-web`) weiterleitet.
- **Invoice Ninja Snappdf Fix**: Die `tailscale`-Konfiguration nutzt `extra_hosts`, um die URL von Invoice Ninja intern auf `127.0.0.1` aufzulösen, sodass die lokale PDF-Generierung mittels `snappdf` ohne Timeouts funktioniert.
