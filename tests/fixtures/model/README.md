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

The batch lane's tests (T-16, `tests/test_model_jobs.py`) answer with the files
below. They conform to the test's own SYNTHETIC stand-in schema `Verdict`
(`imports_from_china`, `reasons`, `confidence`) — not to `RelevanceVerdict`,
which T-30 builds and registers; the lane itself is schema-agnostic.
`tests/fakes.succeeded()` wraps one into a Message Batches result line.

| file | SYNTHETIC | what it is |
|---|---|---|
| `batch_verdict_confirmed.json` | yes | a company that wrote it brought a container from Yiwu: `confirmed`, two reasons |
| `batch_verdict_likely.json` | yes | a wholesaler of tiles: `likely` (a sector prior) |
| `batch_verdict_unlikely.json` | yes | a local manufacturer: `unlikely` |
| `batch_verdict_leak.json` | yes | one reason carries a synthetic phone (`+998 90 000 00 09` shape) — the output-hygiene case (04 §4.7) |
