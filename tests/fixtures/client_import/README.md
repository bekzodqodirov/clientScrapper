# The ERP client export — synthetic fixture for T-11

`clients.xlsx` is what the ERP's **Mijozlar → XLSX** (`GET /api/clients/xlsx`,
fullprompt `agent-feed` `d138b3bce6ac05f5938809e6d5203209c1c7711f`,
`src/app/api/clients/xlsx/route.ts`) writes for the client book in
`clients_book.json` — made by hand, never exported from a real installation:
every card is a «Namuna …» on a `+998 90 000 00 NN`-shaped number (07 §1.0).

Not a capture: the route needs a session and a database, so
`make_clients_xlsx.mjs` repeats its writing calls with the ERP's own writer
(exceljs 4.4.0 from `/home/user/fullprompt/node_modules`): one sheet «Clients»,
a bold header row of `CORE_TITLES` (`Code, Name, Manager, Phones, Active`) plus
one custom field's label («Shahar»), the column widths, one row per card with
`phones.join(', ')` and Active `✓` or empty.

    cd tests/fixtures/client_import
    NODE_PATH=/home/user/fullprompt/node_modules node make_clients_xlsx.mjs

The workbook's creation stamps are fixed; the zip's entry times are not, so a
re-run writes a different sha256 — update the line below with the file.

## Files

- `clients.xlsx` · made 2026-10-24 by `make_clients_xlsx.mjs` · sha256
  `4c7e1c5e28ad1c6d4a1153d933183b234fd4884f999a7827cde32665e36002a7` · 7800 bytes ·
  23 cards: one inactive (NMN12), one with no phone (NMN13), one 7-digit legacy
  number (NMN11), two codes on one phone (NMN01, NMN23), and one card per shape
  of `tailsSql` (05 §A.7): a list of two, a slash, `( )` around the code, a
  24-digit `998…998…` run, a 27-digit run, a 20-digit part, a 130-digit part,
  126 and 132 digits (past 120), exactly 120 digits of `998` numbers, a
  fragment-only element before another number (NMN14), LF, CR LF, two spaces,
  a tab, a 36-digit `998` run, a semicolon with words, a space before a
  foreign `+86` number, an 18-digit pair.
- `clients_book.json` · sha256
  `015f98b0434c2c4f4e865368721fa777cc7a6348e88e0e647c839b0050d8d728` · the
  input of the generator and of the fake ERP's cards in
  `tests/test_client_import.py` (one book, two readers).
- `make_clients_xlsx.mjs` · sha256
  `6a4a6c7e69e5ab2dd8b11a6461c2725785c2a68a0de1a43fc62e86792f015ca0` · the
  generator.

## What the writer did that the book does not say

- NMN16's `\r\n` reads back as `\n`: XML end-of-line handling turns a literal
  CR LF in a shared string into LF. Both are separators to the ERP, so the
  tails do not change.
- Every cell is a string, the phone cell included: exceljs writes what the
  route hands it. A number only appears when somebody re-saved the file in
  Excel (`tests/test_client_import.py` builds those with openpyxl).
