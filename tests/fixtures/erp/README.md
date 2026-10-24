# ERP agent-feed contract fixtures — v2

Captured from the FINAL `agent-feed` branch of fullprompt, commit `23fe173680c2a97a615c033443fd69a1e0630ee8` (the head
`git -C /home/user/fullprompt log -1` showed; two commits past f2d96526 — aefbf278
«the landing never throws half-way, one delivery at a time, the agent door says 503»,
and 23fe1736, docs only). `pnpm build` was re-run at that head (the previous `.next`
predated aefbf278), the standalone server ran on 127.0.0.1:3198, every request was
made with `curl` against the real routes, and the database was the throwaway
`gsr_fixture2` (drop → create → migrate, ledger 136 = journal length → `pnpm db:seed` →
`pnpm create-admin` for one synthetic super admin «Namuna Admin» +998900000099; NO
seed:demo). Server and database were removed afterwards. **No real data anywhere**:
every client, lead, phone and name is a synthetic «Namuna …» row on `+998 90 000 00 NN`.

## The fixture key (THROWAWAY — not a credential)

- Agent door key (`lead_sources.webhook_secret` of `agent`, set by hand on the dropped
  database): `fixture2-only-agent-key-NOT-a-credential` — written here ONLY because it is a fixture key for a database
  that no longer exists. Every request below shows it as `<REDACTED: fixture agent key>`.
- «Boshqa» (`other`) door key, used once for the fallback push: also a fixture-only
  key, redacted in the files.
- `salt_id` = `7a659490` = sha256(key UTF-8).hexdigest()[:8] (checked with
  `openssl dgst -sha256` and python hashlib; equals the body's `salt_id`).
- Hash rule string, exactly as the body carries it: `hmac-sha256(secret, last9digits) hex[0:20]`
- h = hmac_sha256(key = secret UTF-8, msg = tail9 ASCII).hexdigest()[:20], lowercase.
  Doc vector re-checked: key `k`, tail `901234567` → `2ea6869e504cebca50f0`; sha256(`k`)[:8] → `8254c329`.

## tail → hash (verified independently: `openssl dgst -sha256 -hmac <key>` and python `hmac`)

The full pull has exactly 9 entries; every one is explained by a row below, and every
«absent» hash is checked to be in neither the full pull nor the delta.

| tail9 | h | kind | in full | in delta | check | what |
|---|---|---|---|---|---|---|
| `900000001` | `bbc9434b92f2f7f81776` | client | 1 | 0 | ✓ openssl = python | card NMN01, phone 1 of 2 (n=1) |
| `900000002` | `10edb63c599ee8181a50` | client | 1 | 0 | ✓ openssl = python | card NMN01, phone 2 of 2 (n=2) |
| `900000004` | `897984ecd03eae2d5c14` | client | 1 | 0 | ✓ openssl = python | card NMN04, INACTIVE |
| `900000010` | `65165ff193bf0ae2a1f4` | lost | 1 | 0 | ✓ openssl = python | lead «Namuna lid 10», lost «Qayta qilinmasin» (non-agent) |
| `900000022` | `534dd284275834ecc27f` | client | 2 | 0 | ✓ openssl = python | ×2: card GS100 minted by winLead, AND the won agent lead «Namuna Import 22» (not deduplicated) |
| `900000025` | `bdd8244b10f2ed414998` | lost | 1 | 1 | ✓ openssl = python | agent lead «Namuna Qurilish 25», lost «Qayta qilinmasin» after the anchor (also in the delta) |
| `900000003` | `46e22afa1ddbf67af6c1` | client | 1 | 1 | ✓ openssl = python | card NMN03, phone 1 (card edited after the anchor → in the delta) |
| `900000007` | `c0b7c4e6d58f3e0fac32` | client | 1 | 1 | ✓ openssl = python | card NMN03, phone 2, added after the anchor (in the delta) |
| `900000005` | `3e73be8633433535801d` | — | 0 | 0 | ✓ openssl = python | absent: client NMN05's card holds «000-00-05» (7 digits): no 9-digit tail, never in the feed; the BELT still suppresses +998 90 000 00 05 (post_suppressed_legacy_card.json) |
| `900000011` | `3333c6439deff894a0a4` | — | 0 | 0 | ✓ openssl = python | absent: lead «Namuna lid 11»: lost «Qayta qilinmasin», REVIVED to open after the anchor — gone from the full pull; the delta says nothing |
| `900000012` | `7601bacafc0422ddbcc5` | — | 0 | 0 | ✓ openssl = python | absent: lead «Namuna lid 12»: lost «Narx qimmat» — a dictionary label, not in agent_no_contact_reasons |
| `900000013` | `980b363507d7cb4dd741` | — | 0 | 0 | ✓ openssl = python | absent: lead «Namuna lid 13»: open (the joined push landed on it) |
| `900000021` | `6a42863687ca41a8a0de` | — | 0 | 0 | ✓ openssl = python | absent: agent lead «Namuna Savdo 21»: open, contacted |
| `900000023` | `ee9f36bdab81c3bbbe33` | — | 0 | 0 | ✓ openssl = python | absent: agent lead «Namuna Tekstil 23»: lost «Narx qimmat» |
| `900000024` | `1eb4ed43dc041328a166` | — | 0 | 0 | ✓ openssl = python | absent: agent lead «Namuna Mebel 24»: lost with free text |
| `900000099` | `9baca406fe4913ba3753` | — | 0 | 0 | ✓ openssl = python | absent: the synthetic admin user's phone: users are not the client book |

## What was done, in order

1. Seed (tsx over the app's own db module): both fixture keys; cards NMN01 (two phones
   …01, …02), NMN03 (…03), NMN04 (…04, inactive), NMN05 (legacy «000-00-05»); leads
   «Namuna lid 10» lost «Qayta qilinmasin», «11» lost «Qayta qilinmasin», «12» lost
   «Narx qimmat», «13» open. The lost-reason dictionary is EMPTY at this point;
   `agent_no_contact_reasons` is the default `Qayta qilinmasin`.
2. The POSTs (`post_*.json`, `err_post_*.txt`), in the order listed below.
3. touch1 (tsx, the app's own service functions, actor = the synthetic admin):
   «📞 Bog'landim» note (`CONTACT_NOTE`, kind call) on lead 21 + `stampFirstContacts`;
   `winLead` on lead 22 (mints card GS100 with …22 and a deal); `moveLead` lead 24 to
   lost with a free-text reason (`Namuna erkin matn: … +998 90 000 00 98 …`, synthetic);
   the dictionary gets «Qayta qilinmasin» and «Narx qimmat»; `moveLead` lead 23 to lost
   «Narx qimmat»; `stampFirstContacts(now)`.
4. Delta anchor `2026-10-24T08:56:34.544Z` (taken 1.2 s after touch1, 1.2 s before touch2).
5. touch2: `moveLead` agent lead 25 to lost «Qayta qilinmasin»; `moveLead` lead 11 back
   to open (revival); card NMN03 gets a second phone …07 (`updated_at` = now);
   `stampFirstContacts(now)`.
6. The GETs and the GET refusals.

## Files

One line each: file · capture time (UTC) · request (method, path, query, headers — the
key REDACTED) · sha256 of the stored file · status · bytes stored · note.
GET bodies (`suppression_*.json`, `outcomes*.json`) are the exact body bytes the server
sent, gunzipped where the wire was gzip (only `outcomes.json` was: bodies under 1 KB go
out identity). `post_*.json` are this capture's own wrapper — `request` (headers
redacted, body as sent), `response` (status, headers, body) and `erp_ledger_after`,
which is NOT on the wire: the arrival's `lead_intakes` rows read from the throwaway
database right after the call (every outcome answers the same 200). `err_*.txt` are
`curl -i`-style: status line, headers, blank line, body.

- `post_created.json` · 2026-10-24T08:56:29.912Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `179b563c302394e921f3a89208350cae1b783efa62e6cc57729c5f98505dc786` · 200 · 1414 bytes · created: a new phone → a new lead (later contacted via «📞 Bog'landim»)
- `post_created_won.json` · 2026-10-24T08:56:30.104Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `5920061cbca16340af5fa6ec5115699fccb7c5b8c6b1e77d4b605e83acf845d2` · 200 · 1137 bytes · created → later won through winLead (a client card is minted with this phone)
- `post_created_lost_listed.json` · 2026-10-24T08:56:30.218Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `e1af26cc5c8bce7c9ef3a24da821351af9f45b43d992752e2d95e416f3ea6de6` · 200 · 1119 bytes · created → later lost «Narx qimmat» (a dictionary label, not in agent_no_contact_reasons)
- `post_created_lost_free.json` · 2026-10-24T08:56:30.309Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `d84b51eb51c773af412ca5d1c026fdeb6abb85ab21010bfebcdaf09592362fba` · 200 · 1117 bytes · created → later lost with a seller's FREE TEXT (moved before the dictionary existed)
- `post_created_lost_no_contact.json` · 2026-10-24T08:56:30.423Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `ff92209cdcd0027710600fd0ffbab14cadac53baf2d7f6c9133583308c526c83` · 200 · 1139 bytes · created → lost «Qayta qilinmasin» AFTER the delta anchor (no_contact true; enters suppression as lost)
- `post_joined.json` · 2026-10-24T08:56:30.527Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `b9fae89850d0653ac2bfe6452805a4683e5ecb0225afec6b7603324857c9830e` · 200 · 1149 bytes · joined: the phone of an OPEN non-agent lead (Namuna lid 13) → a note on it
- `post_suppressed_client.json` · 2026-10-24T08:56:30.634Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `8ac76d665a76a23fb56bb7db4591692235e5084f9ee3d75b8ae9b72c5144c31b` · 200 · 1126 bytes · dropped/suppressed: the SECOND phone of the two-phone client NMN01
- `post_suppressed_inactive_client.json` · 2026-10-24T08:56:30.724Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `05b37e2af611cdfcef2cbee3a010c5903b79655942270f6657a45dab4ce7372b` · 200 · 1126 bytes · dropped/suppressed: an INACTIVE client's phone (NMN04)
- `post_suppressed_legacy_card.json` · 2026-10-24T08:56:30.804Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `092319258ba9c772a0cf7cb49921740b773857b3c3278c31d317d37d7a41fc31` · 200 · 1126 bytes · dropped/suppressed by the BELT's suffix rule: client NMN05's card holds only «000-00-05» (7 digits), never in the feed
- `post_suppressed_lost_1.json` · 2026-10-24T08:56:30.874Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `d9dc1380bec47aa425dc6405619eac357b9e8bd9552be9276d57a1130c00920c` · 200 · 1126 bytes · dropped/suppressed: lead «Namuna lid 10» lost «Qayta qilinmasin» (agent push 1 of 4 on this phone today)
- `post_suppressed_lost_2.json` · 2026-10-24T08:56:30.942Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `8c3c21fb6e21033499b97a83b040545ffb70dd7e0bc22fb495e2d9ff6a864b60` · 200 · 1126 bytes · dropped/suppressed: lead «Namuna lid 10» lost «Qayta qilinmasin» (agent push 2 of 4 on this phone today)
- `post_suppressed_lost_3.json` · 2026-10-24T08:56:31.010Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `0e4e12effdc380196f16a751dc7599408f2dc9527c25bb4be971b2b1015894e8` · 200 · 1126 bytes · dropped/suppressed: lead «Namuna lid 10» lost «Qayta qilinmasin» (agent push 3 of 4 on this phone today)
- `post_capped.json` · 2026-10-24T08:56:31.083Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `e79b1bd60cee92e7621ce82586b24e67c788bdf16ea4a83200b36671e59c38ab` · 200 · 1122 bytes · dropped/capped: the 4th agent push on one phone in a day — capped wins over suppressed (caps before belt)
- `post_no_contact.json` · 2026-10-24T08:56:31.143Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `fa10c5cd7bd9bce435eb8e4e0a315ea35db26a4687009214f70e5b243f242327` · 200 · 1083 bytes · dropped/no_contact: neither name nor phone
- `post_no_phone.json` · 2026-10-24T08:56:31.197Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `c0c6dac3442bc32115b52cadf13c6f222f2aafec2005182fefc02825f7cebc7a` · 200 · 1117 bytes · dropped/no_phone: a phone of fewer than nine digits through the agent's door
- `post_replay.json` · 2026-10-24T08:56:31.254Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `75a5d0f617a6fc1c1ac76e765a825b304661bb09cbebc5d2a08334c96101c288` · 200 · 1121 bytes · replay of post_created's external_id: 200, nothing landed (still ONE ledger row)
- `post_fallback_client.json` · 2026-10-24T08:56:31.307Z · POST /api/leads/in/other (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture «Boshqa» key>) · sha256 `a2d674405c254d24b335835eec5be5922a6a168bc02eda215ba46ff986bb5c0a` · 200 · 1092 bytes · outcome client: only through the «Boshqa» fallback door (no belt) — an ACTIVE client's phone (NMN03)
- `err_post_404_no_key.txt` · 2026-10-24T08:56:31.384Z · POST /api/leads/in/agent (Content-Type: application/json, body '{"external_id": "cs:00000000-0000-4000-8000-000000000099:202610", "name": "Namuna rad 99", "phone": "+998 90 000 00 99"}') · sha256 `fcdab9a44e94953cf814b6082cb950fe42cfaf5436a0d4682df0d65e41cbec90` · 404 · 274 bytes · no key → 404 text/plain, nothing recorded
- `err_post_404_wrong_key.txt` · 2026-10-24T08:56:31.402Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: a WRONG key>, body '{"external_id": "cs:00000000-0000-4000-8000-000000000099:202610", "name": "Namuna rad 99", "phone": "+998 90 000 00 99"}') · sha256 `fcdab9a44e94953cf814b6082cb950fe42cfaf5436a0d4682df0d65e41cbec90` · 404 · 274 bytes · wrong key → 404, nothing recorded
- `err_post_404_erp_key_only.txt` · 2026-10-24T08:56:31.415Z · POST /api/leads/in/agent (Content-Type: application/json, X-ERP-Key: <REDACTED: fixture agent key>, body '{"external_id": "cs:00000000-0000-4000-8000-000000000099:202610", "name": "Namuna rad 99", "phone": "+998 90 000 00 99"}') · sha256 `fcdab9a44e94953cf814b6082cb950fe42cfaf5436a0d4682df0d65e41cbec90` · 404 · 274 bytes · only X-ERP-Key (the right key, wrong header) → 404: only X-GSR-Key is read
- `err_post_404_not_json.txt` · 2026-10-24T08:56:31.430Z · POST /api/leads/in/agent (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, body 'name=Namuna') · sha256 `fcdab9a44e94953cf814b6082cb950fe42cfaf5436a0d4682df0d65e41cbec90` · 404 · 274 bytes · the right key, a body that is not JSON → 404
- `err_post_404_unknown_source.txt` · 2026-10-24T08:56:31.443Z · POST /api/leads/in/agentx (Content-Type: application/json, X-GSR-Key: <REDACTED: fixture agent key>, body '{"external_id": "cs:00000000-0000-4000-8000-000000000099:202610", "name": "Namuna rad 99", "phone": "+998 90 000 00 99"}') · sha256 `fcdab9a44e94953cf814b6082cb950fe42cfaf5436a0d4682df0d65e41cbec90` · 404 · 274 bytes · unknown source in the path → 404
- `suppression_full.json` · 2026-10-24T08:56:38.457Z · GET /api/agent/suppression (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `65f0742d7d900313bd4c8a5573f8226132acdab3d9cf1e29e8c203495544ce60` · 200 · 943 bytes · full pull: no since, no cursor (mode full); next null, resume = the last row's cursor [content-type application/json; cache-control private, no-store; etag yes; wire 943 bytes identity]
- `suppression_page1.json` · 2026-10-24T08:56:38.548Z · GET /api/agent/suppression?limit=5 (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `6d7ea1dd91b43dbae91ae2f620902f8e4c80f91e3a74f2b487eac9dff4644c8f` · 200 · 723 bytes · walk page 1 of 2 (limit 5 of 9 entries): next set [content-type application/json; cache-control private, no-store; etag yes; wire 723 bytes identity]
- `suppression_page2.json` · 2026-10-24T08:56:38.577Z · GET /api/agent/suppression?limit=5&cursor=MjAyNi0xMC0yNFQwODo1NjozMy4wMjg5MzZafDAxYTE2ZDIwLTQyMDktNzEzYi1hMTY4LTU1ZGViNDc5MDk4OXwx (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `c6868fb1430474558064664c647afc6e07e9bb287124396ef59629667544d03c` · 200 · 560 bytes · walk page 2 of 2: cursor = page 1's next verbatim; next null, resume = its last row [content-type application/json; cache-control private, no-store; etag yes; wire 560 bytes identity]
- `suppression_resume_empty.json` · 2026-10-24T08:56:38.599Z · GET /api/agent/suppression?limit=5&cursor=MjAyNi0xMC0yNFQwODo1NjozNy4yMTQwMDBafDAxYTE2ZDFmLWUyNWMtNzBhZC05OTNiLWVjM2JmM2FiZmYzYXwy (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `e299bca6709baa8ebcd67a19a077556d16841926929196f03f2fc568e433d866` · 200 · 255 bytes · resume after the end: cursor = page 2's resume → no entries, next null, resume = the cursor sent [content-type application/json; cache-control private, no-store; etag yes; wire 255 bytes identity]
- `suppression_delta.json` · 2026-10-24T08:56:38.623Z · GET /api/agent/suppression?since=2026-10-24T08:56:34.544Z (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `0141a2cf634e18b847c5887680fb6391a8e33e9ba046acadad90969cbb1f231b` · 200 · 484 bytes · delta since the anchor 2026-10-24T08:56:34.544Z (mode delta): rows moved after it; the revived lead's removal is NOT expressible [content-type application/json; cache-control private, no-store; etag yes; wire 484 bytes identity]
- `outcomes.json` · 2026-10-24T08:56:38.643Z · GET /api/agent/outcomes (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `0a7c5363e51f32f4f6c910ed23c2a52f9d6c6edfa43c8c1a6abea6f50f4474c2` · 200 · 6688 bytes · outcomes, full (no since, no cursor): every cs: arrival of the year, ordered (at, intake_id) [content-type application/json; cache-control private, no-store; etag yes; wire 1226 bytes gzip]
- `outcomes_since.json` · 2026-10-24T08:56:38.669Z · GET /api/agent/outcomes?since=2026-10-24T08:56:34.544Z (X-GSR-Key: <REDACTED: fixture agent key>, X-ERP-Key: <REDACTED: fixture agent key>, Accept-Encoding: gzip) · sha256 `94ada512484e8c3baec671e7f343f2c0f6409cbb9c3baeb6bba58d21f8feb4c7` · 200 · 639 bytes · outcomes since the anchor 2026-10-24T08:56:34.544Z: rows whose at moved strictly after it [content-type application/json; cache-control private, no-store; etag yes; wire 639 bytes identity]
- `err_404_no_key.txt` · 2026-10-24T08:56:38.689Z · GET /api/agent/suppression (no headers) · sha256 `5549056f6d85b356567411a3f6d55ebaf264b0c430ba2275b03462d11578cb30` · 404 · 274 bytes · no key → 404 text/plain «Not found»
- `err_404_wrong_key.txt` · 2026-10-24T08:56:38.704Z · GET /api/agent/outcomes (X-GSR-Key: <REDACTED: a WRONG key>) · sha256 `5549056f6d85b356567411a3f6d55ebaf264b0c430ba2275b03462d11578cb30` · 404 · 274 bytes · wrong key → 404
- `err_404_erp_key_only.txt` · 2026-10-24T08:56:38.717Z · GET /api/agent/suppression (X-ERP-Key: <REDACTED: fixture agent key>) · sha256 `5549056f6d85b356567411a3f6d55ebaf264b0c430ba2275b03462d11578cb30` · 404 · 274 bytes · only X-ERP-Key → 404 (only X-GSR-Key is read)
- `err_400_bad_cursor.txt` · 2026-10-24T08:56:38.729Z · GET /api/agent/suppression?cursor=bm9wZQ (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `e07180796a0af7b04c15b514a9b4f255f640f0f4490eba7835a209aff34ca880` · 400 · 369 bytes · a cursor that does not decode (base64url «nope») → 400 bad_cursor
- `err_400_bad_cursor_outcomes.txt` · 2026-10-24T08:56:38.743Z · GET /api/agent/outcomes?cursor=MjAyNi0wMi0zMFQwMDowMDowMC4wMDAwMDBafDAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMHww (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `e07180796a0af7b04c15b514a9b4f255f640f0f4490eba7835a209aff34ca880` · 400 · 369 bytes · a well-formed cursor carrying a rolled date (2026-02-30) → 400 bad_cursor
- `err_400_bad_since.txt` · 2026-10-24T08:56:38.757Z · GET /api/agent/outcomes?since=yesterday (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `1b36e7e050cca5c255941060b64984d5a33df2e0e0332cf1846ac2611254d7aa` · 400 · 368 bytes · since not an instant → 400 bad_since
- `err_400_bad_since_zero.txt` · 2026-10-24T08:56:38.771Z · GET /api/agent/suppression?since=0 (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `1b36e7e050cca5c255941060b64984d5a33df2e0e0332cf1846ac2611254d7aa` · 400 · 368 bytes · since=0 (Date.parse would read the year 2000) → 400 bad_since
- `err_400_bad_since_plus.txt` · 2026-10-24T08:56:38.786Z · GET /api/agent/outcomes?since=2026-10-24T05:00:00+05:00 (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `1b36e7e050cca5c255941060b64984d5a33df2e0e0332cf1846ac2611254d7aa` · 400 · 368 bytes · an UNENCODED «+» offset arrives as a space → 400 bad_since (send %2B05:00)
- `err_400_bad_since_naive.txt` · 2026-10-24T08:56:38.800Z · GET /api/agent/outcomes?since=2026-10-24T05:00:00 (X-GSR-Key: <REDACTED: fixture agent key>) · sha256 `1b36e7e050cca5c255941060b64984d5a33df2e0e0332cf1846ac2611254d7aa` · 400 · 368 bytes · no zone → 400 bad_since

## Notes for the fake ERP and the agent

- Every POST outcome — created, joined, client, dropped (suppressed / capped /
  no_contact / no_phone) and the replay — answered `200 {"ok":true}`,
  `content-type: application/json`, `cache-control: no-store`. The replay left ONE
  ledger row for `cs:…021` (`erp_ledger_after` in `post_replay.json`).
- The 4th agent push on one phone in a day is `dropped/capped` even though the phone is
  suppressed: caps run before the belt (`post_suppressed_lost_1..3` → `post_capped`).
- `client` came only through the «Boshqa» fallback (`post_fallback_client.json`). The
  agent's own door suppressed every client phone first, including the 7-digit legacy
  card by suffix (`post_suppressed_legacy_card.json`) — that tail can never arrive as a
  hash, so the agent must keep the pushed phone in its own suppression on that outcome.
- Outcomes rows have exactly 15 fields (`no_contact` is the 15th). `lost_reason` is the
  label for «Narx qimmat» and «Qayta qilinmasin», `null` for the seller's free text;
  `no_contact` is true only for the «Qayta qilinmasin» row.
- `quoted_volume_m3` is null on every row: `kub` is not mapped on this database (owner
  step 4 never done), so the agent's own `kub` was not echoed back.
- `contact_kind` for the «📞 Bog'landim» press reads `note`; the stage moves made by
  the admin on unowned leads count as first contact (`stage`) — the leads had no owner
  because no taqsimot rota was configured.
- Cursors are opaque base64url of `<microsecond ISO Z>|<uuid>|<n>`; e.g. page 1's
  `next` decodes to a client card's `created_at` with n = 1. Page 1's `resume` equals
  its `next`; page 2 (the last) has `next: null` and `resume` = full pull's `resume`;
  resuming from it returns no entries, `next: null`, `resume` = the cursor sent.
- Entry and row timestamps are millisecond ISO with `Z`.
- The delta holds 3 entries (lead 25's `lost`, NMN03's two phones). Lead 11's revival
  removed its hash from the full pull and is invisible to the delta — only a full pull
  deletes.
- Not captured: a 503 (it needs the database to fail during a request; the shared
  postgres serves other agents, so it was not stopped), a 304, and a gzip POST.
