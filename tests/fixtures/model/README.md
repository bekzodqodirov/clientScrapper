# Model answers — SYNTHETIC

Every file here is **SYNTHETIC**: hand-written structured answers that conform
to `services/schemas.ImportSignal` (04 §4.3), made for the T-15 tests on
2026-10-28. None was produced by a model and none is a recording. Real
recordings of the Messages API (already pseudonymised, 07 §1.0) are a day-1
task on the VPS (07 §5.5); when one lands it gets its own line here marked
`recorded`, never one of these names.

`tests/fakes.message()` wraps an answer into a Messages API response; the
`usage` numbers it adds are synthetic too.

| file | SYNTHETIC | what it is |
|---|---|---|
| `classify_buyer_stated.json` | yes | 04 §9.1: «Guangzhoudan 2 kub mato kerak, kim olib keladi?» — a buyer, 2 m³ stated, quote «2 kub mato» |
| `classify_seller_inferred.json` | yes | 04 §9.2: an OLX seller of washing machines, volume inferred, no quote |
| `classify_tout_weak.json` | yes | a `tout` at confidence 0.55 — the validators' role-consistency case |
| `classify_container.json` | yes | a 40 ft container, 60 m³, quote «40 futlik konteyner»; duplicate goods «Kafel»/«kafel» |
| `classify_cyrillic_stated.json` | yes | Uzbek Cyrillic: «5 куб ўйинчоқ», goods in Latin with ʻ |
| `classify_quote_with_token.json` | yes | a quote that carries a pseudonym token («<NAME_1>ga 3 kub mebel»), restored before it is checked |
