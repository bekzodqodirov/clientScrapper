# tests/fixtures/telegram

Hand-written synthetic messages (07 §1.0): never a real message, a real chat
or a real person. Phones are `+998 90 000 00 NN`, handles `namuna_<n>`.

| file | date | what | scrubbed? | note |
|---|---|---|---|---|
| `prefilter_cases.json` | 2026-10-24 | 25 messages (group, channel, private) in Latin Uzbek, Cyrillic Uzbek and Russian, each with the `prefilter_hit` the reader stores — `[]` = not stored | n/a (synthetic) | T-13; `tests/test_prefilter.py` pins every verdict |
