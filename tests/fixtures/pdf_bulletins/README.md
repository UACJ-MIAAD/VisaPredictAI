# PDF reales para aceptación offline

Copias exactas e intactas suministradas por Javier del Visa Bulletin público del
U.S. Department of State, octubre, noviembre y diciembre de 2026. No son PDF sintéticos ni
transcripciones: las pruebas ejecutan `pdftotext -layout` sobre estos bytes.
El título interno y cuatro matrices son la autoridad de mes y celdas.

| Archivo | Tamaño | SHA-256 |
|---|---:|---|
| Visa Bulletin For October 2026.pdf | 187279 bytes | dfb3ad38a6277973723921103310cbde9e1de80433a15f066e85291b17ba4180 |
| Visa Bulletin For November 2026.pdf | 181994 bytes | 1fa41e4ed632f1b46304e09b9db9c7079f8d297955224636e12393c9e9972ed0 |
| Visa Bulletin For December 2026.pdf | 74892 bytes | 5be56527d8557b4bd50a5ae88688f4d574db45d9c94af430d64931a1656830ed |

La incorporación a fixtures conserva esos hashes; no se convierte, reimprime ni
edita el original. Poppler es una herramienta del sistema, declarada tanto en el
runbook como en los jobs CI que ejecutan estas pruebas. La instalación ausente
provoca un fallo explícito; no un `skip` ni una tabla supuesta.

Diciembre llegó impreso desde Safari, que repite al inicio de cada página la
última o dos últimas líneas de la anterior. `drop_page_overlap` sólo quita esa
repetición exacta en el salto de página; el resto de comprobaciones no cambia.
