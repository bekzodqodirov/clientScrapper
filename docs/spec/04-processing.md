# 04 — Processing: normaliser, resolution, enrichment, Claude, scoring, gates, suppression, budget

Scope: everything between an `Observation` (03) and a `lead_candidates` row that `crm_push` (05) may send. Tables, DDL and the signal-key registry are canonical in `02-data-model.md`; this section says how each row is computed. Shared context (business, ERP contract, env names, budget split) is assumed.

## 0. Modules and jobs

| Module (`clientscrapper/services/`) | Pure? | Job (`worker/main.py`, Asia/Tashkent) | Reads → writes |
|---|---|---|---|
| `keys.py` (02 §1) + `normalise.py` | yes | inside `resolve_job` | `Observation` dataclass (03) → `observations.parsed` JSON (02 §2.5) |
| `companies.py` (← MIYA `people.py`) | no | `resolve_job` every 2 min, ≤ 200 rows | `observations(status='new')` → `companies`, `company_aliases`, `company_ids`, `contacts`, `signals`, `lead_candidates(found)` |
| `signals.py` | mostly | called by resolver, enrich, model collectors | `evidence` + rule/model output → `signals` (quote validated) |
| `enrich.py` | no | `enrich_job` every 10 min 07:00–22:00, ≤ 30 candidates | `lead_candidates(enriching, next_enrich_at ≤ now)` → evidence, contacts, signals |
| `pseudonyms.py`, `extraction.py`, `model_jobs.py`, `redlines.py` | mostly | `classify_job` wakes on `NOTIFY cs_interaction` (fallback sweep every 15 min); `verdict_job` every 10 min; `model_batch_submit` 23:30; `model_batch_collect` every 30 min 00:30–08:00 | `interactions`/`evidence` → `model_jobs` → `signals`, `companies.verdict` |
| `scoring.py` | yes | `score_job` wakes on `NOTIFY cs_interaction`, fallback sweep every 15 min (companies with `last_signal_at > last_scored_at`) | `signals`, `contacts`, `scoring_weights` → `lead_candidates.score/score_breakdown/tier/state` |
| `gates.py`, `suppression.py` | gates yes / lookup no | inside `score_job`, `review_cards_job`, `crm_push_job` | `suppression`, `push_log`, `companies` flags → candidate state |
| `costs.py` (← MIYA `jarvis/costs.py`), `usage.py` | no | wraps every paid call | `usage_log` |

Every job is idempotent, takes `FOR UPDATE SKIP LOCKED` on the rows it claims, beats `job:<id>` (06 §4.1), and stops at its row cap so a backlog never starves the others.

**The Telegram path is event-driven** (01 N-06 «Telegram intent → owner card p90 < 5 minutes»). The userbot's insert transaction ends with `NOTIFY cs_interaction, '<id>'` (02 §2.12); `classify_job` and `score_job` `LISTEN` on that channel and run at once instead of waiting for a cron tick, and the worker re-raises the same notify when `classify_job` or `resolve_job` commits a Telegram-sourced row so each stage wakes the next: classify (seconds) → `resolve_job` (≤ 2 min) → score (on notify) → `review_cards_job` every 2 min inside `REVIEW_HOURS` (06 §1.6; `REVIEW_BURST_MAX` and the other burst caps unchanged). The 15-minute sweep of both jobs is only the fallback for a lost notification; every other source keeps its cron.

## 1. Normaliser

Home (as built, T-03): `services/keys.py` holds **every** key function of 02 §1 — the three with SQL mirrors (`phone_tail`, `username_key`, `domain_key` ↔ `cs_phone_tail`, `cs_username_key`, `cs_domain_key`) and the rest: `phone_e164`, `phone_kind` (with `MOBILE_CODES`, `MOBILE_CODES_UNCONFIRMED`, `LANDLINE_CODES`), `instagram_key`, `stir_key`, `name_key` (with `LEGAL_FORMS`), `loose_key`, `site_key` (with `SITE_SUFFIXES`, `TENANT_HOSTS`) and the ERP hash. `services/normalise.py` re-exports `phone_e164` / `phone_tail` / `phone_kind`, so the spelling below names the same functions, and holds what turns text into keys: `parse_phones`/`make_phone`, `telegram_key`/`telegram_handles`/`instagram_handles` (with `TG_RESERVED`), `website_key` (with `LINK_HUB_HOSTS`, `SHARED_HOSTS`), `find_stir`, `find_legal_form`/`legal_form_code`, `canonical_city`, `is_person_shaped`, `parsed_kind` and `to_observation_json`. `services/names.py` holds `FIRST_NAMES` and MIYA's `HONORIFICS`; `services/text.py` `fold_apostrophes`, `to_latin`, `transliterate`, `storable`. `tests/test_keys.py` and `tests/test_normalise.py` are golden-table tests over the examples below.

### 1.1 Phones

```python
@dataclass(frozen=True, slots=True)
class Phone:
    e164: str            # '+998901234567'
    tail: str            # last 9 digits of the national number — the ERP identity
    kind: Literal["mobile", "landline", "short", "unknown"]
    country: str         # 'UZ' | 'other'
    raw: str

def parse_phones(text: str, *, limit: int = 5) -> list[Phone]: ...   # PHONE_RE (03 §1.4) over text, dedupe by tail, in order of appearance
def phone_e164(raw: str, *, city: str | None = None) -> str | None: ...   # city lets rule (6) through
def phone_tail(raw: str) -> str | None: ...                             # == cs_phone_tail, identical to ERP inboundPhoneDigits
def phone_kind(e164: str) -> Literal["mobile", "landline", "short", "unknown"]: ...
```

Algorithm (`phone_e164`): (1) keep `+` only when leading, drop every other non-digit; `00` prefix → `+`; (2) `+998` + 9 digits → done; (3) bare `998` + 9 digits → `+998…`; (4) `8` + 9 digits (legacy trunk) or `0` + 9 digits → strip, `+998`; (5) exactly 9 digits → `+998`; (6) 7 digits (local Tashkent landline without area code) → `+99871` + 7 only when the observation's city is Toshkent, else `None`; (7) `+` other country, 7–15 digits → kept as E.164 with `country='other'`; (8) anything else → `None`. Reject: fewer than 9 national digits for UZ, the **seven subscriber digits all equal** (the table's `+998 90 000 00 00 → None`, which `(\d)\1{8}` alone would let through; it also covers `(\d)\1{8}`, and it rejects a real vanity number such as `+998 90 777 77 77` — accepted, a placeholder is far commoner in adverts), `123456789`/`987654321`, national number starting with `0`.

`phone_kind` on a UZ number by the two digits after `+998`: mobile `33 77 88 90 91 93 94 95 97 98 99` (`keys.MOBILE_CODES`); `20 50 55` are `keys.MOBILE_CODES_UNCONFIRMED` and read **`unknown`** — so they are not usable lead phones — until they are checked against the current numbering plan on uztelecom.uz (Q4); landline `61 62 65 66 67 69 70 71 72 73 74 75 76 78 79` (area codes, `keys.LANDLINE_CODES`; same source) — 70–79 except 77 are landlines, as 02 §1 now says too; `< 7` national digits → `short`, which `phone_e164` never produces (it requires nine national digits) — the value stays for a hand-entered contact; everything else `unknown`. `country='other'` → `unknown`.

| raw | e164 | tail | kind |
|---|---|---|---|
| `+998 90 123 45 67` | `+998901234567` | `901234567` | mobile |
| `90 123-45-67` | `+998901234567` | `901234567` | mobile |
| `8 (71) 200-00-00` | `+998712000000` | `712000000` | landline |
| `998 33 000 00 01` | `+998330000001` | `330000001` | mobile |
| `+7 926 000 00 00` | `+79260000000` | `260000000` | unknown (other) |
| `1234` | None | None | — |
| `+998 90 000 00 00` | None (all equal after prefix) | — | — |
| `+998 70 200 00 26` | `+998702000026` | `702000026` | landline (Tashkent region) |
| `+998 20 000 00 20` | `+998200000020` | `200000020` | unknown (code unconfirmed, Q4) |

A **usable lead phone** (used by tiers and the push gate) = `country='UZ'`, `kind ∈ {mobile, landline}`, on a contact with `anonymised_at IS NULL` and `shared_phone=false`.

### 1.2 Handles

`username_key` exactly as 02 §1 (`^[a-z0-9_]{5,32}$`, not ending in `bot`); additionally `normalise.py` drops reserved paths before calling it: `TG_RESERVED = {"joinchat","addstickers","addtheme","share","proxy","socks","iv","s","c","login","setlanguage","confirmphone","telegram","durov"}` and anything after `t.me/+`. `instagram_key`: strip `instagram.com/`, `@`, query, trailing `/`; lowercase; strip trailing dots; `^[a-z0-9._]{1,30}$`; reject `IG_RESERVED = {"p","reel","reels","explore","stories","tv","accounts","tags","direct","about","developer","legal"}`.

### 1.3 Domain

`domain_key` exactly as 02 §1 (equal to `cs_domain_key`, plus IDNA in Python, and every leading `www.` stripped). `normalise.website_key(raw)` is the company-website filter on top of it: `None` for a social/marketplace host (`keys.NOT_WEBSITE_HOSTS`, routed to the handle fields by `classify_hit`, 03 §2.1), a link hub — `LINK_HUB_HOSTS = {"taplink.cc","taplink.ws","linktr.ee","lnk.bio","instabio.cc","bio.site","clck.ru","bit.ly","cutt.ly","wa.me","api.whatsapp.com"}` (the enrichment step E2 fetches the hub page once and parses it for phones/handles/website) — a **shared host** (`SHARED_HOSTS`, as built: hosts whose pages belong to many companies — maps and search (2gis.*, yandex.*, google.*, goo.gl, g.page), the directories of 03 §2.1 `DIRECTORY_HOSTS`, the news hosts of `NEWS_HOSTS` and wikipedia, social/video/messengers (tiktok.com, vk.com, ok.ru, x.com, linkedin.com, youtu.be, fb.me, telegra.ph, whatsapp.com …), short links (tinyurl.com, vk.cc, t.ly, is.gd), marketplaces and the Chinese suppliers' platforms (wildberries.*, ozon.ru, alibaba.com, 1688.com, taobao.com, aliexpress.*, made-in-china.com): two firms whose «website» is a 2GIS card must not share an exact identity key, and `ux_companies_domain` would refuse the second), a subdomain of any of these (`m.facebook.com`, `sites.google.com`), and a site builder's own host. For **matching** only, `site_key(domain) -> str` folds to the registrable domain with a built-in suffix list `SITE_SUFFIXES = {"uz","co.uz","com.uz","org.uz","net.uz","biz.uz","com","net","org","ru","kz","shop","store"}` (`shop.example.uz` and `example.uz` resolve to one company; a suffix outside the list folds nothing) and treats the site builders and free hosts of `keys.TENANT_HOSTS` (blogspot.com, wixsite.com, wordpress.com, tilda.ws, business.site, weebly.com, webflow.io, github.io, netlify.app, myshopify.com, jimdosite.com, nethouse.ru, ukit.me, narod.ru and uCoz's domains) like a public suffix — `shop.namuna.blogspot.com` → `namuna.blogspot.com`, the tenant, so every free-hosted firm is not one «blogspot.com» identity; stored columns keep `domain_key`. The two host lists live in code (each pinned by `tests/test_normalise.py`); a host joins them in a code review, not a migration.

### 1.4 Name key

```python
LEGAL_FORMS = (  # matched as whole tokens AFTER transliteration to lower-case Latin, longest first (keys.LEGAL_FORMS)
  "masuliyati cheklangan jamiyat", "masuliyati cheklangan jamiyati",
  "obshchestvo s ogranichennoy otvetstvennostyu", "obshestvo s ogranichennoy otvetstvennostyu",   # both: MIYA's to_latin makes щ → sh
  "xususiy korxona", "xususiy korxonasi", "qoshma korxona", "qoshma korxonasi", "oilaviy korxona", "oilaviy korxonasi",
  "chastnoe predpriyatie", "individualnyy predprinimatel", "individualniy predprinimatel",               # ы → i in to_latin
  "yakka tartibdagi tadbirkor", "aksiyadorlik jamiyati", "aktsiyadorlik jamiyati", "aktsionernoe obshchestvo",
  "aktsionernoe obshestvo", "fermer xojaligi", "mchj", "ooo", "llc", "ltd", "qk", "xk", "aj", "ao", "oao", "zao", "chp",
  "xchk", "ip", "yatt", "sp", "too", "jv", "ok", "fx", "uk", "dp", "gup",
)   # the Uzbek possessive forms («… korxonasi») are how the full forms follow a name
QUOTES = "«»“”\"'`„‟‹›"

def name_key(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw)
    s = fold_apostrophes(s)              # MIYA text.py: ʻ ʼ ‘ ’ ` ʹ → '
    s = to_latin(s)                      # MIYA text.py: Cyrillic → Uzbek Latin, lower-cased (ў→o', ғ→g', ҳ→h, ё→yo …)
    s = strip_marks(s)                   # NFKD + drop combining marks, ı ł ø đ ß æ œ folded: «Öztürk» → «ozturk», «İstanbul» → «istanbul»
    s = s.replace("'", "")               # go'zal == gozal
    s = re.sub(f"[{re.escape(QUOTES)}]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)    # punctuation, dashes, & → space
    s = LEGAL_FORM_TOKENS_RE.sub(" ", s) # (?<![a-z0-9])(mchj|ooo|…)(?![a-z0-9]) built from LEGAL_FORMS
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160] or to_latin(fold_apostrophes(raw)).strip()[:160]   # a name that is only a legal form keeps the form

def loose_key(key: str) -> str:
    """Comparison form only, never stored: q→k, w→v, x→h, doubled letters collapsed, 'tsh'→'ch'. 'Toshkent Tekstil' ~ 'Tashkent Textile' still differs (o/a, e/i) — that is rapidfuzz's job."""
```

| raw | name_key |
|---|---|
| `"NAMUNA TEKSTIL SAVDO" MChJ` | `namuna tekstil savdo` |
| `ООО «Намуна Текстиль Савдо»` | `namuna tekstil savdo` |
| `Go'zal Mebel XK` / `Гўзал Мебель ХК` | `gozal mebel` |
| `ИП Каримов Р.Р.` | `karimov r r` (person-named YaTT; see §2.2 generic guard) |
| `OOO` | `ooo` (fallback; never fuzzy-matched) |
| `Öztürk Tekstil` | `ozturk tekstil` (the diacritics step; without it «zt rk tekstil») |

The diacritics step is the one addition to MIYA's pipeline (T-03): without it the marked letters fell out with the punctuation and Python lowers «İ» to «i» + a combining dot, so the Turkish and old-alphabet spellings of a name never met their plain ones.

### 1.5 STIR

`stir_key(raw)`: digits only, exactly 9, first digit `2–3` (legal entity) or `4–6` (individual entrepreneur), not all equal, not `123456789`. `STIR_RE` (03 §1.4) finds labelled numbers; an unlabelled 9-digit run is **not** a STIR (it is usually a phone tail). Checksum: **UNVERIFIED — no published algorithm found.** Confirm before v1.1: (a) `tools/stir_probe.py` reads ≥ 300 STIRs the collectors recorded from registry-grade pages (uzts.uz member list, openinfo.uz, orginfo.uz, myorg.uz) and tests candidate schemes — weighted mod 11 over the first 8 digits with weights `1..8`, the Russian 10-digit INN scheme truncated, and plain Luhn; a scheme that explains ≥ 99.5 % of them (and rejects ≥ 90 % of random 9-digit numbers) is adopted into `stir_key` behind a feature flag; (b) in parallel the owner asks the Tax Committee help line whether the 9-digit STIR carries a check digit. Until then shape-only, and `ent_legal_confirmed` requires a registry hit, never a bare number.

### 1.6 `Observation` (03) → `observations.parsed` (02 §2.5)

`normalise.to_observation_json(obs: Observation) -> dict` is the only writer of `parsed`. Mapping:

| 03 field | 02 JSON | rule |
|---|---|---|
| `kind` company / listing / post / vacancy / exhibitor / place | `kind` per 02 §2.5's mapping table (`normalise.parsed_kind`): a `company` by its source — `seller` (olx_business, uzum_sellers), `dealer_page`, `registry` (registry_lookup, tenders), `directory_entry` (directories, maps, exhibitors, a page fetched under a `search_*` row), else `profile`; `post` → intent_post (group post) or profile (channel post, `attrs.chat_type='channel'`) | `serp_hit` (and `suppression`, `outcome`) is **never** an observation (`normalise.NOT_OBSERVATIONS`, 02 §2.5 — `parsed_kind` raises): a hit only enqueues its URL for the owning collector and is kept as evidence; the directory card or company site that collector then fetches emits the observation |
| `company_name_raw`, `legal_form`, `stir`, `address`, `city`, `goods_text`, `website` | `company.*` | `legal_form` normalised to the 02 enum (`ООО`→`OOO`, `ХК`→`XK`, `ЧП`→`ChP`, `ИП`→`IP`, `ЯТТ`→`YaTT`, `ТОО`→`TOO`, `AO`/`АО`→`AJ`, else `other`); `city` = `canonical_city(city)`, else of `address`, else of `attrs.city`: the first city `GEO_CITY` names (03 §1.4), Cyrillic → Latin canonical (`Ташкент`→`Toshkent`, `Фергана`→`Fargʻona`); the Tashkent **region** («Toshkent viloyati», «Ташкентская обл.») returns `normalise.TASHKENT_REGION = 'Toshkent viloyati'`, apart from the city — its landlines are +998 70, the city's 71, so neither rule (6) of §1.1 nor the resolver's city agreement (§2.1/§2.2) reads a region firm as a city one; a Tashkent bazaar or district (`GEO_TASHKENT_PLACES`) gives `Toshkent` only when no city is named («Chorsu bozori Samarqand» is Samarqand). **Person-shaped names never become a company name**: `normalise.is_person_shaped(name_key)` is true for an initials pattern (`^[a-z]+( [a-z]\.?){1,2}$`), a token with a surname/patronymic suffix (`ov|ova|ev|eva|ovich|ovna|evich|evna`), an honorific token (`aka|opa|ака|опа`) or a 1–2-token key whose first token is in `FIRST_NAMES` (`services/names.py`: 309 common Uzbek, Russian and neighbouring given names in `name_key` form with the usual spelling variants, **hand-written** in T-03 — MIYA has no first-name list; MIYA's `_HONORIFICS` is vendored beside it as `HONORIFICS`, of which only «aka»/«opa» count here, `normalise.PERSON_HONORIFICS`, because the rest name shops as often as people) and no `GENERIC_TOKENS` business word («Sardor», «Aziz aka», «Karimov R.R.»). Such a `company_name_raw` (an OLX `user.name`, a poster's display name) is moved to `person.name`; `company.name` becomes `'<source label> <external id>'` — for OLX `'OLX sotuvchi <olx_user_id>'`, for Uzum `'Uzum sotuvchi <uzum_shop_slug>'` (03 §2.5) — with **`name_key = null`** (two such labels would fuzzy-match each other and merge two sellers; the external id resolves them exactly), or `null` itself when the source has no label or no external id, and the resolver stores the person as the one person-tier contact with `legal_basis='public_source'` (§2.4). The same predicate is the §2.2 fuzzy guard. |
| `telegram`, `instagram`, `external_ids` | `handles.*` | keys renamed: `place_id`→`google_place_id`, `twogis_firm_id`→`gis2_firm_id`, `uzum_shop_slug`→`uzum_shop`, `tg_chat_id`→`tg_chat` |
| `phones[]` | `phones[]` | `parse_phones`; `owner` = `person` when `kind=intent_post` and the phone is in the poster's text/bio, `company` for directory/site/place/channel-about, else `unknown` |
| `person_name`, `person_role`, `person_telegram` | `person`, `handles.telegram` (+ `tg_user_id` from `attrs`) | kept only for `kind ∈ {intent_post, profile, vacancy, registry, seller}` (`PERSON_KINDS`; `seller` so a person-shaped OLX `user.name` has its one place); a run of exactly 14 digits anywhere drops the field (`hints.pinfl_dropped`; a 17-digit 2GIS or Graph id survives). A poster's `tg_user_id` is person-tier: the resolver writes it to `contacts.telegram_id`, never to `company_ids` (`ID_TYPES` has no `tg_user`, 02 §2.4), and retention R3 strips `handles.tg_user_id` and a poster's personal `handles.telegram` from an `intent_post`'s `parsed` at 30 d (02 §4) |
| `excerpt`, `volume_text`, `origin_text` | `text.*` | `intent_words` = `INTENT_RE` matches |
| `signal_types`, `attrs` | `hints.*` | `container_truck`→`container_words`; `wholesale_bulky`/`directory_wholesale`→`wholesale_words`; `china_origin`→`china_words`; `b2c_marker`→`b2c_words`; `competitor`→`competitor_words`; `state_body`→`state_words`; `attrs.volume_m3`/`basis`→`hints.volume_m3`/`volume_basis`; the raw `signal_types` list and `attrs` are copied under `hints.collector` for the signal mapper (§5.3) |

## 2. Entity resolution (`services/companies.py`)

### 2.1 Exact keys

```python
@dataclass(frozen=True, slots=True)
class IdentityKeys:
    stir: str | None
    phone_tails: tuple[str, ...]          # UZ mobile+landline tails from phones[] with owner != 'unknown' or kind=intent_post
    tg_username: str | None               # company channel OR poster username
    site_key: str | None
    instagram: str | None
    external_ids: tuple[tuple[str, str], ...]   # (id_type, id_value) as in company_ids
    name_key: str
    city: str | None

@dataclass(slots=True)
class Resolution:
    company: Company
    how: Literal["stir","phone","telegram","domain","instagram","external_id","fuzzy","new"]
    score: float                          # 100 for exact, token_set_ratio for fuzzy, 0 for new
    created: bool
    duplicate_of: Company | None          # §2.2 review band
    conflict: tuple[str, uuid.UUID] | None   # a lower-precedence key pointed elsewhere

async def resolve_company(session, obs: ObservationRow, *, source_key: str, now: datetime) -> Resolution
```

Order, first hit wins: `stir` (`companies.stir`, `state <> 'merged'`) → `phone_tails` (`contacts.phone_tail` with `shared_phone=false`, `anonymised_at IS NULL`; a **landline** tail matches only if the city agrees or is unknown on either side) → `tg_username` (`companies.telegram_channel`, then `contacts.telegram_username`) → `site_key` (`site_key(companies.website_domain)`) → `instagram` (`companies.instagram`, `contacts.instagram`) → `external_ids` (`company_ids`). A match through a `merged` company follows `merged_into` to the root. The winning company absorbs every other key the observation carries (`_carry_identity`: fill `stir/website_domain/telegram_channel/instagram/city/legal_form` only where NULL; new phones/handles become `contacts` rows; new external ids become `company_ids`). **Conflict:** when a later key resolves to a *different* company, nothing is merged automatically; the observation stays with the higher-precedence match, `Resolution.conflict` is logged, and if `token_set_ratio` between the two companies' names ≥ 80 the lower one gets `dup_of` (`companies.dup_of uuid REFERENCES companies(id) ON DELETE SET NULL` + `dup_score smallint`, day-1 columns of `0001`, 02 §2.4/§5) so the owner's 🔀 can merge. A phone that conflicts is a `shared_phone` candidate (§2.3).

### 2.2 Fuzzy name match

Candidates: `SELECT company_id, alias_key FROM company_aliases WHERE alias_key % :name_key UNION SELECT id, name_key FROM companies WHERE name_key % :name_key` (pg_trgm, `set_limit(0.3)`, ≤ 50 rows; the `%` operator reads the GIN indexes — a bare `similarity() >` scans). Filter: `state NOT IN ('merged','purged')`, city equal or NULL on either side. Score = `rapidfuzz.fuzz.token_set_ratio(loose_key(a), loose_key(b))`. Guards before any fuzzy decision: both keys ≥ 4 characters and ≥ 1 non-generic token (`GENERIC_TOKENS = {"optom","ulgurji","savdo","trade","trading","group","company","kompaniya","firma","textile","tekstil","mebel","stroy","qurilish","import","export","uz","uzbekistan","toshkent","tashkent","plus","pro","star","gold","best","lux","market","shop","dokon","sklad","baza","biznes","business"}`); a person-shaped key (`normalise.is_person_shaped`, §1.6: initials pattern, surname/patronymic suffix, honorific, or a 1–2-token given name — ≤ 3 tokens) never fuzzy-matches another person-shaped key below 100. Decisions: **≥ 90** → attach to that company (`how='fuzzy'`, alias learned); **80–89** → create the new company, set `dup_of=<best>, dup_score`, card shows «🔀 Birlashtir: … (87)»; **< 80** → new company. Exact-equal `name_key` or alias (`_exact_name`) wins before scoring, as in MIYA `best_match`. Ambiguity (`runner_up` within `AMBIGUITY_MARGIN=5` of the best, both ≥ 90) is treated as the review band, never auto-attached. Creation runs under `pg_advisory_xact_lock(hashtext('cs:company'), hashtext(name_key))` (MIYA pattern) so two workers cannot create the same company twice.

### 2.3 Shared phones

After each contact insert: `SELECT count(DISTINCT company_id) FROM contacts c JOIN companies k ON k.id=c.company_id WHERE c.phone_tail=:tail AND k.state<>'merged'`. If ≥ 3 and no pair of those companies scores ≥ 80 by name → `UPDATE contacts SET shared_phone=true WHERE phone_tail=:tail` (accountant, broker, business centre, cargo office). Shared phones never merge, never count for `derived_contact_mobile`, and a lead whose only phone is shared is pushed only after owner approval with the card line «⚠️ Raqam 3+ firmada uchraydi». A tail on exactly 2 companies with names < 80 → not merged, flagged `Resolution.conflict`, counted again on the next observation.

### 2.4 Contacts inside a company

One live person-tier row per company (`ux_contacts_one_person`). `upsert_contact(session, company, *, tier, phone, username, instagram, full_name, role, source_url, source_key, now)`: match inside the company by phone tail, then username, then instagram; a new person while one exists → the existing row is anonymised (`anonymise(contact)`, 02 R4) **only if** the new one is a better decision maker (`ROLE_RANK`: direktor/rahbar/owner/учредитель 3 > ta'minot/закупки/ВЭД 2 > menejer/sotuv 1 > unknown 0); otherwise the new person's phone is attached as a company-tier row without the name. Person-tier rows always carry `legal_basis='public_source'` (self-published post, bio, site) or `'public_registry'` (director from a registry page), `source_url`, `collected_at`. HR contacts from vacancies are never contacts (03 §2.7).

Attribution (seam review 2026-10-24): `observations.contact_id` is the contact the evidence's Telegram id or phone **matched** (the id first, then the tail) — the number's holder, not necessarily the message's author: a post by one sender quoting another's number lands on that number's holder, and a person's second personal number is never stored (06 §5.1 «second/third personal numbers» are forbidden data). The card (T-21, `services/cards.py`) names the author from the evidence itself (`interactions.sender_*`, the verbatim quote) and the contact only as «who answers this number».

### 2.5 Merge and unmerge

`merge_into(session, source, target, *, by, score) -> Company` per 02 §2.14, refusing when both have different `stir`, different registry-confirmed `website_domain`, different `telegram_channel`, or either is `purged`. Re-pointed: `contacts` (a duplicate phone tail on both → source row deleted), `company_aliases` (dedupe on `alias_key`), `company_ids`, `observations`, `signals` (dedupe on `(key, evidence_id)`), `model_jobs`, `usage_log`, open `lead_candidates` (source's open candidate → `excluded(merged)`; if the target has none, the source's is moved instead). **Not** re-pointed (historical ledgers): `push_log`, `outcomes`; readers use `company_lineage(id) -> set[uuid]` (the id plus every `merge_log.source_company_id` whose chain ends at it). `unmerge(session, merge_log_id, *, by)` within 24 h: re-points the ids recorded in `moved`, clears `carried` fields on the target, sets `source.state` back to `active`, stamps `undone_at/undone_by`. Owner path: 🔀 on a card → `lead_reviews.answer='merge'` → `merge_into(by='owner:<id>')`.

### 2.6 MIYA `people.py` → `companies.py`

| MIYA | Verdict | clientScrapper |
|---|---|---|
| `normalise`, `_HONORIFICS`, `_is_cyrillic` | rename | `keys.name_key`, `LEGAL_FORMS`; honorifics are irrelevant for companies |
| `MATCH_THRESHOLD=85`, `QUESTION_THRESHOLD=70`, `AMBIGUITY_MARGIN=10` | re-tune | `AUTO_MERGE=90`, `REVIEW_LOW=80`, `AMBIGUITY_MARGIN=5`; `QUESTION_THRESHOLD=70` kept for `/qidir` |
| `NameKey`, `_name_and_codes`, `_candidates`, `_score`, `_is_exact`, `best_match`, `_person_score` | keep, drop codes | `_alias_keys(company)`, `_score(a,b)` = token_set_ratio over `loose_key`, `_exact_name`, `best_name_match(name_key, companies)` |
| `Match` (+ `ambiguous`), `find_person`, `_find_by_name` | keep | `NameMatch`, `find_company(session, name, *, city, threshold)` — loads trigram candidates, not the whole table; used by `/qidir` and the merge card |
| `resolve_person` | adapt | `resolve_company` (§2.1): telegram_id shortcut → exact-key ladder; advisory lock kept; `_backfill` → `_carry_identity`; `_learn_alias` → `learn_alias(session, company, spelling, source_key)` writing `company_aliases` |
| `_give_codes`, `UnknownCode`, `CodePolicy`, `is_placeholder`, `set_profile`, `set_relationship` | drop | no client codes in v1 |
| `IdentityConflict` | keep (renamed) | `KeyConflict` — exact keys disagree; logged, never raised past the resolver |
| `OwnerNamed`, `_owner_guard`, `is_owner_alias` | adapt | `_self_guard`: `SELF_KEYS` (GSR Logistics / Imex services names, its domains, its channels) never become a company; such observations are `rejected(competitor)` |
| `find_by_phone` | keep | `find_by_phone_tail(session, tail) -> list[Company]` through `contacts` |
| `whatsapp_jid`, `known_addresses`, `_merge_reply_rules`, `_merge_style_tone` | drop | no sender, no reply rules |
| `MergeRefused`, `_fk_columns`, `merge_plan`, `merge_into` | keep | §2.5 plus `unmerge` and `merge_log` rows |

## 3. Enrichment (`services/enrich.py`)

Runs on `lead_candidates(state='enriching')`; `enrich_attempts` ≤ 3, `next_enrich_at` = now + {1 h, 24 h, 7 d}; one attempt walks the chain below in order and stops at the first **stop condition**: (S1) usable phone **and** score ≥ 60 → `scored`; (S2) company became `excluded`/`suppressed`; (S3) per-company paid spend ≥ `$0.10` (`SUM(usage_log.cost_usd) WHERE company_id = …`) → only free steps continue; (S4) after attempt 3 still no phone: `score ≥ 60 AND contacts.telegram_username IS NOT NULL` → state `review` with `tier='warm'` and `score_breakdown.gates.no_phone=true` — `review_cards_job` renders the handoff variant `[👤 Menejerga]` for that row (06 §1.3; this is the one producer of the Telegram-only card, 02 §2.9) — otherwise `hold` (`hold_until` NULL; a new signal re-opens). Each step records what it did in `lead_candidates.score_breakdown.enrichment[]` (`{step, at, cost_usd, result}`) and writes `evidence` for every fetched page, under `source_key` = `registry_lookup` (E4), the engine key (E5 SERP), `maps_google`/`maps_2gis` (E5 maps), `instagram_discovery` (E6), or `enrich_site` (E2, E3, E7) — `enrich_site` is a seeded `sources` row of `0001` (family `search`, `enabled=true`, `cron=NULL`, 02 §2.1) so that a company's own page counts as a source family of its own for corroboration.

| # | Step | Needs | Yields | Cost / daily cap (`costs.ask` op) | Stop / failure |
|---|---|---|---|---|---|
| E0 | Regex over own evidence (`signals.from_rules`) | evidence rows | phones, handles, STIR, legal form, `vol_*`, `cn_origin`, negatives, city | free | always runs at resolve time |
| E1 | Cross-source join | company id | corroboration, newer signals, aliases | free | — |
| E2 | Company site / link hub fetch (`Fetcher`, robots) | `website_domain` or hub URL, not fetched in 30 d | phones (`tel:` first), TG/IG, STIR in footer, legal name, goods | 2 pages/company, 150 pages/day from `sources.registry_lookup.daily_page_budget` | 403/429 → host breaker; JS-only page → `result='js_only'` |
| E3 | Telegram channel `about` from the **public page** `https://t.me/s/<username>` through `Fetcher` (title, description, pinned text; `evidence(source_key='enrich_site', url=https://t.me/s/<username>)`) — the worker never imports `telethon` (06 §2 one client per session; 07 `test_collector_imports`) | `telegram_channel` or a contact username, not fetched in 30 d | phones, site, address | free; pages inside the E2 page budget | page empty/404 (private channel, user account) → enqueue `chat_monitors(status='queued', priority 3)`; `userbot/about.py` drains it (`GetFullChannelRequest`, ≤ 20/day, 03 §2.3) into `evidence` + `observations(kind='profile')`, which the next attempt reads as E0/E1 |
| E4 | Registry by STIR | `stir` | status, registered_on, OKED, legal address, director role | ≤ 60 lookups/day across hosts, 1 req/3 s, cache 90 d in `company_ids(id_type='myorg')` + evidence | captcha/403 → that host's breaker 24 h; all hosts down → `result='registry_unavailable'`, retried next attempt |
| E4′ | Registry by name | no `stir`, `name_key` non-generic | `stir` candidates (name + city must agree, else none) | same budget | ambiguous (≥ 2 hits) → none |
| E5 | Directory/maps by name + city | name_key, city | phones, website, 2GIS/Places ids | `serp_brave` 2 queries ($0.01) or free engines; Places text (free) + details inside the maps cap (`places_details_contact`) | no phone → next |
| E6 | Instagram Business Discovery (Graph API) | `instagram` handle, `IG_GRAPH_TOKEN` | bio → phone/TG/site, `media_count`, `is_business` | free; ≤ 100 calls/day (UNVERIFIED rate: 200/h/token) | `not_business` → skip |
| E7 | Anthropic `web_search` contact find (§4.6) | score ≥ 50, no phone after E0–E6, not tried in 30 d | phones/handles with source URLs | `enrich_search` ≈ $0.03/company, cap $0.75/day (≈ 25) | refused by budget → attempt ends, retried tomorrow |

Registry hosts (all per-INN, all **UNVERIFIED** for URL shape and captcha on day 1; record fixtures first): `orginfo.uz` public company page (name/INN search, HTML; the first host to try — no login seen in the research sample); `registr.stat.uz` EGRPO module (per-INN form, captcha reported → never automated past one form post; on captcha the lookup goes to the **manual queue**: the weekly digest lists ≤ 10 `(display_name, city)` and the owner answers `/stir <candidate_id> <9 digits>` (06 §1.2: writes `companies.stir` and re-runs E4)); `my.soliq.uz` VAT-payer check (per-INN; returns VAT registration → `registry_status='active'`, no name search); `myorg.uz` public page (per-INN; sometimes a company phone — company-tier, `legal_basis='public_registry'`). A registry hit writes `ent_legal_confirmed` (+ `ent_age_2y_active` when `registered_on ≤ now − 2 y` and status active), sets `companies.registry_status/registered_on/oked/legal_form`, and `neg_liquidated` on `liquidated`.

Google Places data (E5) obeys 03 §2.9: only `place_id` is kept past 30 days; the phone copied to `contacts` carries `source_key='maps_google'`, `source_url=https://www.google.com/maps/place/?q=place_id:<id>`, is cleared by retention step R4b (02 §4: `collected_at < now() − 30 d` and no `push_log(sent|unknown)` for the company) and is re-fetched on demand after 30 days if the card is still open.

## 4. Claude

### 4.1 Call shape, routing, token math

All calls go through `extraction.call(session, *, op, schema, system, user, model, max_tokens, effort, tools=None, company_id, evidence_ids) -> ModelResult`, which: pseudonymises (§4.2), runs `costs.ask` (§8), prefers `client.messages.parse(output_format=Schema)` and falls back to prompted JSON on a grammar failure exactly as MIYA (`is_grammar_failure`, `note_grammar_failure`, `json_payload`), passes `output_config={"effort": effort}` (thinking stays adaptive — `disabled` is a 400 on Sonnet 5.5/Opus 5.5), caches the system block (`cache_control: ephemeral`; the system text is frozen per prompt version, nothing volatile before it), retries once with the JSON nudge, maps `stop_reason='refusal'` → `model_jobs.status='failed', error='refusal'` (no retry, no fallback model — cost stays predictable), `max_tokens` → one retry with `max_tokens × 2`, and records the job in `model_jobs` with the pseudonymised `request`, the `pseudonyms` reverse map and the validated `result`. No `temperature`, no prefill, no forced `tool_choice`.

| op | model (env) | lane | effort | max_tokens | per call (est.) | cap/day | ≈ calls/day |
|---|---|---|---|---|---|---|---|
| `classify` ImportSignal | `EXTRACT_MODEL=claude-haiku-5-5` | instant | low | 400 | 0.9k cached sys ($0.01/M) + 0.3k in ($0.10/M) + 0.15k out ($0.50/M) ≈ **$0.00012** | $0.30 | ≈ 2,000 (03 caps it at 300) |
| `verdict` RelevanceVerdict | `VERDICT_MODEL=claude-sonnet-5-5` | instant | low | 1,200 | 1.5k cached ($0.20/M) + 2.5k in ($2/M) + 0.6k out incl. thinking ($10/M) ≈ **$0.011** | $1.50 | ≈ 130 |
| `company_verdict` RelevanceVerdict | `BATCH_MODEL=claude-opus-5-5` Batch (−50 %) | batch | low | 1,500 | 4k in ($2/M) + 0.7k out ($10/M) ≈ **$0.015** | $1.50 | ≈ 100/night |
| `draft` DraftOpener | `EXTRACT_MODEL` | instant | low | 600 | 1.2k in + 0.2k out ≈ **$0.0003** | $0.20 | ≈ 600 (used ≤ 40) |
| `enrich_search` ContactFind | `EXTRACT_MODEL` + `web_search` | instant | low | 2,000 | 3 searches × $0.01 ($10/1k searches, checked against the pricing page 2026-10-23) + 6k in + 0.3k out ≈ **$0.031** | $0.75 | ≈ 24 |
| `model_reserve` | — | — | — | — | borrowed by any op above once its own cap is hit | $0.75 | — |

Sum $5.00/day. Prices from the shared contract (Haiku 5.5 $0.10/$0.50 up to 100K-token prompts; Sonnet 5.5 $2/$10; Opus 5.5 $4/$20; cache read Haiku ≈ 0.1× input, Sonnet/Opus $0.20/M; Batch half). `usage.py` keeps `MODEL_PRICES` keyed by the three env model ids; an unknown model id is unpriced and `costs.ask` refuses it. Web search on Haiku 5.5 uses tool type `web_search_20250305`; the `_20260209` variant needs Sonnet 5.5/Opus 5.5 — `enrich_search` falls back to `VERDICT_MODEL` only when `allowed_domains` filtering is needed and Haiku rejects it (day-1 check).

When each op fires: `classify` — every `interactions` row that passed the tout filter, every Instagram profile with B2B markers but no volume number, one call per OLX seller over its concatenated ad texts (never per ad), never for directories/vacancies/exhibitors/places (regex suffices). `verdict` — a candidate entering `review` without a verdict younger than 30 d, and a candidate scored 40–74 with ≥ 2 evidence rows when the night batch is > 12 h away (Telegram intent must reach a card in < 5 min p90, N-06 — the notify chain of §0). `company_verdict` — nightly for companies `enriching`/`scored` 40–59 with evidence newer than `verdict_at`, and weekly for `hold` companies with new evidence; ≤ 100 per night. `draft` — at card render for `hot` candidates; a refusal or budget miss renders the playbook template instead. `enrich_search` — §3 E7.

### 4.2 Pseudonymisation (`services/pseudonyms.py`)

```python
@dataclass(frozen=True, slots=True)
class Pseudonymised:
    text: str
    reverse: dict[str, str]        # {"<PHONE_1>": "+998901234567", "<TG_1>": "namuna_tekstil", "<NAME_1>": "Rustam"}

def pseudonymise(text: str, *, names: Iterable[str] = (), prior: Pseudonymised | None = None) -> Pseudonymised
def restore(text: str, reverse: Mapping[str, str]) -> str
def assert_masked(text: str) -> None      # raises UnmaskedPayload when PHONE_RE / TG_RE / IG_RE / EMAIL_RE still match; extraction.call runs it on every request before dispatch
```

These are the only names (06 §5.2 and 07 T-15 point here; there is no `services/pseudonym.py`, `Masked`, `mask` or `unmask`). The regexes are imported from `collectors/keywords.py` (03 §1.4), never redefined here. Order and tokens: phones (`PHONE_RE` → `phone_e164`; same number → same token) `<PHONE_n>`; Telegram usernames/links (`TG_RE`) `<TG_n>`; e-mails (`EMAIL_RE`) `<EMAIL_n>`; **then** Instagram handles/links (`IG_RE`) `<IG_n>` — last of the four on purpose: IG_RE's label branch takes the word after «IG »/«Instagram » (03 §1.4), so run first it would swallow a phone, a handle or an address that follows the label; 14-digit runs `<ID>` (not reversible, never stored); `names` (sender first/last name, bio-stated person names) matched case-insensitively in both scripts via `transliterate` → `<NAME_n>`. STIR, company names, cities, goods, prices stay (company-tier). `prior` keeps numbering stable across the evidence blocks of one bundle. `tests/test_pseudonyms.py`: round trip, no `\+998\d{9}|@[a-z0-9_]{5,}` left (`tests/test_pseudonymised_requests.py` greps stored requests), idempotence.

### 4.3 `ImportSignal`

```python
class ImportSignal(BaseModel):
    speaker_role: Literal["buyer", "seller", "tout", "unknown"]       # tout = cargo/forwarder/buying agent
    b2b: bool                                                          # a business, not a private person
    b2c_markers: bool                                                  # «o'zim uchun», single item, retail
    goods: list[str] = Field(max_length=6)                             # Uzbek Latin, 1–3 words each
    goods_category: Literal["textile","footwear","apparel","household","auto_parts","building","furniture","toys",
                            "cosmetics","electronics","lighting","equipment","raw","packaging","stationery","bags","sport","other","none"]
    origin_cn: Literal["explicit", "implied", "other_origin", "unknown"]
    volume_m3_est: float | None                                        # per shipment; None when nothing stated or implied
    volume_basis: Literal["stated", "container", "truck", "weight", "inferred", "none"]
    volume_quote: str | None                                           # verbatim fragment that carries the volume
    frequency: Literal["one_off", "repeat", "unknown"]
    confidence: float = Field(ge=0, le=1)
```

03's field names map onto it: `is_buyer_intent = speaker_role == "buyer"`, `competitor = speaker_role == "tout"`, `b2c = b2c_markers`; `volume_basis` values `stated|container|truck|inferred` are a subset.

System prompt (frozen, `PROMPT_VERSION=1`, English, ~900 tokens): the task («one Telegram/OLX/Instagram text from Uzbekistan's wholesale market; decide who is speaking and what they move»), the role definitions with examples in Uzbek Latin, Uzbek Cyrillic and Russian (buyer: «kim olib keladi», «kerak», «ищу поставщика»; seller: «optom narx», «sklad», «новое поступление»; tout: «kargo», «dostavka Xitoydan», price per kg), the volume conversions (`20ft ≈ 28–33 m³, 40ft ≈ 60–68 m³, fura ≈ 80–90 m³, yarim fura ≈ 40, 1 t of mixed goods ≈ 3–6 m³ — report as the low end`), the rule «`volume_quote` must be copied verbatim from the input; if nothing states volume return null/none/none», «goods in Uzbek Latin», «pseudonym tokens like `<PHONE_1>` are not information», and «return only the object». User content: `SOURCE: telegram group «<title>»\nDATE: <iso>\nSENDER_BIO: <pseudonymised bio or ->\nTEXT: <pseudonymised text ≤ 1,500 chars>`. Validators (§4.7) then turn the result into signals (§5.3).

### 4.4 `RelevanceVerdict`

```python
class EvidenceQuote(BaseModel):
    ref: str = Field(pattern=r"^E\d{1,2}$")      # the bundle block the quote comes from
    quote: str = Field(max_length=200)

class RelevanceVerdict(BaseModel):
    imports_from_china: Literal["confirmed", "likely", "unlikely", "no"]
    segment: Literal["A", "B", "C", "D", "E", "X", "Z"]              # A bazaar · B marketplace · C factory · D construction/furniture · E electronics/auto/appliances · X competitor/supplier · Z unclear
    est_volume_band: Literal["unknown", "lt1", "1_3", "3_10", "truck", "container"]
    is_b2b: bool
    is_competitor: bool
    is_supplier_cn: bool
    is_state_body: bool
    decision_maker_role: str | None = Field(max_length=40)           # a role, never a name
    goods: list[str] = Field(max_length=6)
    reasons: list[str] = Field(max_length=5)                        # Uzbek Latin, ≤ 120 chars each, shown on the card
    evidence_quotes: list[EvidenceQuote] = Field(max_length=6)
    confidence: float = Field(ge=0, le=1)
```

Evidence bundle (`model_jobs.request`): header `COMPANY: <display_name> | LEGAL_FORM | CITY | STIR: <9 digits or ->`, then ≤ 12 blocks `[E<n>] <source family> <date> <url host>\n<pseudonymised excerpt ≤ 600 chars>`, newest first, capped at 6,000 tokens (older blocks dropped, count noted). System prompt (`PROMPT_VERSION=1`, ~1,500 tokens): the ICP (01 §2) verbatim, the segment definitions, the band definitions, the exclusion definitions (competitor words, Chinese-side supplier markers, state bodies), «`evidence_quotes[].quote` is a verbatim substring of block `ref`; a claim without a quote is a guess — lower `confidence`», «`imports_from_china=confirmed` needs an explicit China/1688/Taobao/Yiwu/Guangzhou mention or a Chinese brand dealership quoted; `likely` is a sector prior», «no names of people anywhere in the output», «reasons in Uzbek Latin for a sales manager». Both lanes (instant Sonnet, batch Opus) use the same request builder (`build_verdict_params`) so results are comparable; the batch path passes `output_config` built with `transform_schema` as MIYA does.

Effects (`apply_verdict`, same transaction as the job): `companies.verdict/verdict_at/segment/volume_band/goods` (goods merged, ≤ 6); `imports_from_china=confirmed` with a validated quote → `cn_origin` signal (`model=<id>`), `likely` → `cn_sector_prior`; `is_competitor` → `flag_competitor` + `companies.is_competitor`; `is_supplier_cn`, `is_state_body` likewise; `est_volume_band` **never adds volume points** (it is a band for the card and routing; points come from quoted evidence) except `lt1` with `confidence ≥ 0.8` → `neg_b2c_words`-like hold flag `verdict_small` (card shows it, no points). `decision_maker_role` → `contacts.role` if the person-tier row has none.

### 4.5 `DraftOpener` and the redline gate

```python
class DraftOpener(BaseModel):
    lang: Literal["uz", "ru"]
    text: str = Field(min_length=200, max_length=520)
    hook: str = Field(max_length=120)                               # the one public fact the opener leans on
```

Input: company display name, goods (≤ 3), the source family and date («OLX, 2026-10-21»), the manager's first name placeholder `[Men]`, the contact's first name if person-tier (`<NAME_1>` in the request, restored only in the card), segment, and the matching playbook skeleton (`docs/sotuv-playbook.md` §3; template A–E chosen by segment). Prompt: «one personalised first message for a human manager to send from their own account; Uzbek Latin (or Russian when the evidence is Russian); no price, tariff, percentage, day count, delivery time, guarantee, discount, link, file, competitor name, customs/tax/declaration/STIR mention; one public fact as the hook; end with the opt-out sentence exactly: `Agar qiziq bo'lmasa, bir so'z yozing — boshqa bezovta qilmayman.` / `Если неактуально — напишите одно слово, больше не побеспокою.`». (The sentence and the playbook templates are spelled with ASCII apostrophes, against 07 §1.11's ʻ/ʼ rule for texts the code renders; the redline gate folds apostrophes, so either spelling passes it. Respell or exempt — Open question 9, decided in T-28/T-30.)

`redlines.check_opener(text, *, lang, allowed_tokens=("2018",)) -> GuardResult(ok, flags)` — pure, a 60-line descendant of MIYA `jarvis/redlines.py` (surface view NFKC + apostrophes folded + `to_latin`; word view `normalise_for_search`): flags `digit` (any digit outside `allowed_tokens`), `money` (`\$|usd|so'?m|сум|y\.?e\.?|narx|цена|tarif|тариф|chegirma|скидк|%`), `time` (`kun|kunda|hafta|soat|день|дней|недел|час|tez|быстр|срок|muddat`), `promise` (`kafolat|гарант|100|albatta|обязательно|eng arzon|самый дешев|bojsiz|без пошлин`), `legal` (`stir|инн|deklaratsiya|деклар|bojxona|таможн|soliq|налог`), `link` (`https?://|t\.me/|@[a-z0-9_]{5,}|www\.`), `competitor` (names from the seed list), `optout_missing` (the exact sentence absent), `language` (dominant script not Uzbek Latin/Cyrillic or Russian, via `safety.dominant_script`), `hook_missing` (none of the goods words or the source name appears). Any flag → the draft is discarded, the card gets the playbook template filled by string substitution (which passes the same gate by construction; `tests/test_redlines.py` pins both the corpus and the templates).

### 4.6 `ContactFind` (web search enrichment)

```python
class FoundContact(BaseModel):
    kind: Literal["phone", "telegram", "instagram", "website", "stir"]
    value: str
    source_url: str

class ContactFind(BaseModel):
    found: list[FoundContact] = Field(max_length=8)
    company_match: Literal["same", "probably_same", "different", "unknown"]
    note: str = Field(max_length=200)
```

Request: tool `{"type": "web_search_20250305", "name": "web_search", "max_uses": 3, "allowed_domains": ["instagram.com","t.me","yellowpages.uz","goldenpages.uz","orginfo.uz","myorg.uz","2gis.uz","top.uz","glotr.uz","prom.uz","flagma.uz","olx.uz","uzts.uz","hh.uz","facebook.com"], "user_location": {"type": "approximate", "country": "UZ", "city": "Tashkent"}}`; user content names the company, city, goods and known handles. Validator: every `value` must occur verbatim (after normalisation) in the `web_search_result` snippets/titles or in a `web_fetch` body of that call — otherwise dropped; `company_match ∈ {different, unknown}` → nothing stored; accepted values become `evidence(source_key='enrich_site', url=source_url)` and company-tier contacts. An error block (`max_uses_exceeded`, `unavailable`) is `result='search_error'`, billed, not retried the same day.

### 4.7 Validators (all pure, `services/validators.py`)

- **Quote validator** `verify_quote(quote, *, excerpt, reverse) -> str | None`: `restore(quote, reverse)` → `_ws()` (NFKC, apostrophes folded, whitespace collapsed, case-folded) on both sides → the quote must be a substring of the evidence excerpt (for a bundle, of block `ref`) and 3–200 characters. A failed quote returns `None`; the signal that needs a quote (`vol_stated_*`, `vol_container`, `cn_origin` from a model) is **not** written; `volume_m3_est` without a verified quote is treated as `inferred`.
- **Volume sanity**: `volume_m3_est` ∈ (0, 200]; `basis='container'` → 28 ≤ v ≤ 70; `truck` → 40 ≤ v ≤ 95; `stated` must agree with `VOLUME_RE` on the quote within ±10 % when the regex finds a number; disagreement → the regex wins.
- **Role consistency**: `speaker_role='tout'` whose text offers no cargo service (`keywords.cargo_offer`, the tout filter's own text rule, 03 §2.3: «kim kargo qiladi?» names the service, it does not offer it) and `confidence < 0.8` → `unknown` (the tout filter in 03 already caught the obvious ones; a weak model flag must not exclude a buyer). A tout that stands excludes the company at gate 2 (§6).
- **Goods**: lower-cased, deduped, ≤ 3 words, ≤ 30 chars each, Latin or Cyrillic letters only.
- **Output hygiene**: any `+998\d{9}`, `@\w{5,}` or 14-digit run in `reasons`/`note`/`text` → field dropped, job flagged `pii_leak` (health problem `pii_leak`, warning, 06 §4.2 `PROBLEM_KEYS`).

### 4.8 `model_jobs` lifecycle (`services/model_jobs.py`, MIYA `batch.py` shape)

Instant lane: `insert(pending)` → call → `applied` (or `failed`) in one transaction with its effects; the `usage_log` row is written by `costs.ask` in its own transaction first (never undercounts). Batch lane: `model_batch_submit` 23:30 collects `pending` jobs of kind `company_verdict` (≤ 100), checks `costs.ask('company_verdict', est=Σ)` once for the whole batch — recording one **provisional** `usage_log` row per job at the estimate — then `client.messages.batches.create(requests=[{custom_id, params}])`, stamps `batch_id, status='submitted', submitted_at, attempts+1`. `model_batch_collect` polls `batches.retrieve(batch_id).processing_status == 'ended'`, streams `batches.results()`, keys by `custom_id` (never by position), applies each `succeeded` result through the same validators, and **updates the provisional `usage_log` row in place** with the real tokens and `cost_usd` (`usage_log` refuses DELETE, not UPDATE). `errored`/`expired` → `attempts < 3` → back to `pending` for the next night; `attempts = 3` → `fallback`: the instant lane (Sonnet) takes it within its own cap. A batch older than 26 h without `ended` → health problem `batch_stuck` (warning, 06 §4.2 `PROBLEM_KEYS`).

## 5. Scoring (`services/scoring.py`)

### 5.1 Version-1 weights

| key | group (cap) | points | decay d (×0.5 after) | source | detection |
|---|---|---|---|---|---|
| `vol_container` | volume (40) | 40 | 180 | own text, brand pages, channel posts | `CONTAINER_RE` with a container/truck word (`konteyner|контейнер|20ft|40ft|fura|фура|full truck`), ImportSignal `basis ∈ {container, truck}` with quote, `brand_distributor(official=True)` for a bulky brand group |
| `vol_stated_3plus` | volume | 30 | 180 | own text | `VOLUME_RE` n ≥ 3; `(\d+)\s*(tonna|тонн)` ≥ 1 t (`value` in m³ = 3 × t low-end, basis `weight`); `yarim fura|полфуры`; ImportSignal stated ≥ 3 with quote |
| `vol_stated_1_3` | volume | 20 | 180 | own text | `VOLUME_RE` 1 ≤ n < 3; ImportSignal stated 1–3 with quote |
| `vol_wholesale_bulky` | volume | 20 | 365 | profile/listing/site | `B2B_MARKERS` hit **and** (a `BULKY_CATEGORIES` term in the same evidence or `companies.sectors ∩ BULKY`); `SUPPLY_RE`; pack units; `brand_distributor(official=False)` for bulky brands; ImportSignal `basis='inferred'` with `b2b` |
| `vol_marketplace_velocity` | volume | 20 | 60 | OLX / Uzum | ≥ 10 active ads in one bulky category (OLX); ≥ 15 SKUs and Δorders ≥ 300/30 d (Uzum) |
| `vol_vacancy_import` | volume | 15 | 180 | hh.uz | vacancy text `ВЭД|деклар|закуп\w*\s.{0,20}Кита|tashqi savdo|xitoy tili|китайск\w+ язык` |
| `vol_exhibitor` | volume | 15 | 365 | exhibitor lists | UZ exhibitor at a China-heavy show |
| `vol_directory_wholesale` | volume | 10 | 365 | directories, 2GIS, Places | wholesale rubric / `оптом|оптовая база|склад|ulgurji` in rubric or Places `types` |
| `cn_origin` | origin (20) | 20 | 365 | own text, verdict `confirmed` | `CHINA_WORDS`; brand dealership of a Chinese brand; `origin_cn='explicit'` with quote |
| `cn_sector_prior` | origin | 10 | — | goods | `goods_category ∈ BULKY` China-dominant set (all 17 keys except `stationery`, `cosmetics`); verdict `likely` |
| `ent_legal_confirmed` | solidity (15) | 8 | — | registry | STIR confirmed on a registry page (E4) |
| `ent_age_2y_active` | solidity | 4 | — | registry | `registered_on ≤ now − 730 d`, status active |
| `ent_not_micro` | solidity | 3 | — | hh / OLX / directories / Uzum | > 1 open vacancy, hh `trusted`, ≥ 20 SKUs, ≥ 10 wholesale ads on a business account registered ≥ 1 y, ≥ 2 distinct phones, branches |
| `derived_fresh_7d/30d/90d` | recency (10) | 10 / 7 / 4 | rolling | scorer | age of the newest signal in the volume group or `cn_origin` |
| `derived_contact_mobile` | contact (10) | 6 | — | scorer | usable **mobile** phone (§1.1) |
| `derived_contact_decision_maker` | contact | 2 | — | scorer | person-tier row with `is_decision_maker` |
| `derived_contact_dm_channel` | contact | 2 | — | scorer | telegram username or instagram on company or contact |
| `derived_corroboration` | corroboration (5) | 5 | — | scorer | volume/origin signals from ≥ 2 source families (`sources.family`) |
| `neg_b2c_words` | negative | −40 | 180 | own text | `B2C_RE` or ImportSignal `b2c_markers` **and** no volume signal ≥ 1 m³ |
| `neg_liquidated` | negative | −50 | — | registry / Places | `registry_status='liquidated'`, `businessStatus=CLOSED_PERMANENTLY` |
| `neg_non_cn_origin` | negative | −20 | 365 | own text / verdict | `(Rossiya|Россия|Turkiya|Турция|Koreya|Корея|Yevropa|Европа|Eron|Иран)` named as origin and no China word; `origin_cn='other_origin'` |
| `neg_state_body` | negative | −20 | — | name / text | `davlat|Davlat|ГУП|ДУК|министер|vazirlig|hokimiyat|hokimligi|agentlig|maktab|shifoxona|поликлиник|МВД|IIB` |
| `flag_competitor`, `flag_supplier_cn` | gate | 0 | — | §6 | evidence for an exclusion |

Points, caps and decay live in `scoring_weights` (02 §2.7); the table above is the seeded version 1 and `tests/test_weights_seed.py` holds the two equal. `brand_distributor` from 03 is decided here: `official=True` and brand group ∈ {HVAC/appliances, solar/power, vehicles, machinery, tyres, plumbing/lighting} → `vol_container` with the «rasmiy distribyutor/официальный дистрибьютор + brand» phrase as quote; dealer mention (`official=False`) in those groups → `vol_wholesale_bulky`; electronics brands (phones, routers, cameras) → `cn_origin` only.

### 5.2 The function

```python
@dataclass(frozen=True, slots=True)
class WeightSet: version: int; rows: Mapping[str, Weight]      # Weight(points, group, cap, decay_days, decayed_factor)
@dataclass(frozen=True, slots=True)
class ScoreResult: total: int; tier: Literal["hot","warm","cold"]; breakdown: dict; newest_signal_at: datetime | None

def score(signals: Sequence[SignalView], contacts: Sequence[ContactView], *, source_families: Mapping[str, str],
          weights: WeightSet, now: datetime) -> ScoreResult:
    """Pure and deterministic. Per key: the best (max points after decay) live signal counts once.
    Group total = min(cap, Σ keys). total = clamp(0, 100, Σ positive groups + Σ negatives).
    Decay: observed_at + decay_days < now → points × decayed_factor. Negatives decay the same way.
    Derived keys are computed here from contacts/signal ages, never read from `signals`."""
```

Stored `score_breakdown` (02 §2.9 shape, extended):

```json
{"version": 1, "computed_at": "2026-10-20T07:53:00+05:00",
 "groups": {"volume": {"cap": 40, "raw": 20, "used": 20, "signals": [{"key": "vol_stated_1_3", "points": 20, "factor": 1.0, "signal_id": 8101, "evidence_id": 7001, "value": 2.0}]},
            "origin": {"cap": 20, "raw": 20, "used": 20, "signals": [{"key": "cn_origin", "points": 20, "factor": 1.0, "signal_id": 8102, "evidence_id": 7001}]},
            "solidity": {"cap": 15, "raw": 0, "used": 0, "signals": []},
            "recency": {"cap": 10, "raw": 10, "used": 10, "signals": [{"key": "derived_fresh_7d", "points": 10, "age_days": 0}]},
            "contact": {"cap": 10, "raw": 2, "used": 2, "signals": [{"key": "derived_contact_dm_channel", "points": 2}]},
            "corroboration": {"cap": 5, "raw": 0, "used": 0, "families": ["telegram"]}},
 "negatives": [], "total": 52, "tier": "warm",
 "gates": {"is_b2b": "pass:intent_b2b", "not_competitor": "pass", "not_supplier": "pass", "not_client": "pass", "not_dnc": "pass", "not_recent": "pass", "active_entity": "unknown"},
 "enrichment": []}
```

Tiers (02 §2.9): `hot` = total ≥ 60 and a usable phone → `review` (or `approved(auto:<source>)` when `sources.auto_push`); `warm` = 40–59, or ≥ 60 without a usable phone → `enriching`; `cold` < 40 → `hold`. **Phone-less Telegram candidate (F-12):** after enrichment attempt 3 with `score ≥ 60 AND contacts.telegram_username IS NOT NULL`, `enrich_job` moves the row to `review` with `tier='warm'` and `score_breakdown.gates.no_phone=true` (§3 S4); `review_cards_job` renders the handoff variant `[👤 Menejerga]` (06 §1.3). The owner's `handoff` answer puts it in `hold` (`hold_until = now + 30 d`) with a `lead_reviews(kind='manager')` row; a manager's `/tel` phone re-scores it at once — `hold → scored → approved` with `approved_by='manager:<tg_id>'` (02 §2.9, 06 §1.7; the owner's `handoff` was the owner's approval). `approved_by` takes exactly `'owner:<id>'`, `'auto:<source>'` or `'manager:<tg_id>'`; it is never pushed without a usable phone.

### 5.3 The ≥ 1 m³ rule in code

`signals.from_rules(evidence, parsed, company) -> list[SignalDraft]`, pure: (1) `VOLUME_RE` over `excerpt` + `volume_text`: the largest number wins per evidence; n ≥ 3 → `vol_stated_3plus`, 1 ≤ n < 3 → `vol_stated_1_3`, n < 1 → no volume signal and `hints.sub_1m3=true` (feeds the B2C negative); (2) `CONTAINER_RE` with a container/truck word → `vol_container` (`partiya|партия|tonna` alone do not count as container; tonnes feed rule 1); (3) «1–3 kub» through the model: `ImportSignal.volume_basis='stated'` with a verified `volume_quote` and `1 ≤ volume_m3_est < 3` → `vol_stated_1_3`, ≥ 3 → `vol_stated_3plus`, `container|truck` → `vol_container`, `weight` → rule 1 on the quoted tonnes; (4) «optom + bulky category»: `B2B_RE.search(text)` — 03 §1.4's `terms_re` over every `B2B_MARKERS` list: whole words, inflection-aware («optomchilar», «оптовые»), never a substring test, which would make «опт» match «оптика» — **and** (`bulky_categories(text)` non-empty — the `BULKY_CATEGORIES` terms through the same rule — or `goods_category ∈ BULKY` from the model, or the OLX/Uzum category id is bulky) → `vol_wholesale_bulky`; a marker without a bulky category (food, cosmetics, stationery) gives nothing; (5) collector-level counts (`attrs.ads_count ≥ 10` in one bulky category, Uzum thresholds) → `vol_marketplace_velocity` with `value` = the count; (6) vacancy, exhibitor, directory keys from `hints.collector.signal_types` through the 03→02 map (`container_truck`→`vol_container`, `volume_m3_3plus`→`vol_stated_3plus`, `volume_m3_1to3`→`vol_stated_1_3`, `wholesale_bulky`→`vol_wholesale_bulky`, `directory_wholesale`→`vol_directory_wholesale`, `ved_vacancy`→`vol_vacancy_import`, `exhibitor`→`vol_exhibitor`, `marketplace_velocity`→`vol_marketplace_velocity`, `china_origin`→`cn_origin`, `entity_solid`→`ent_not_micro` (a parsed STIR or legal block is shape, not confirmation — `ent_legal_confirmed` is written only by a registry hit in E4, §1.5), `b2c_marker`→`neg_b2c_words`, `non_china_origin`→`neg_non_cn_origin`, `state_body`→`neg_state_body`, `liquidated`→`neg_liquidated`, `competitor`→`flag_competitor`; `contact_phone`/`contact_handle`/`intent_ask` are not signals). Every draft carries `quote` = the matched fragment (regex) or the verified model quote; `signals.emit` upserts on `(company_id, key, evidence_id)`.

## 6. Gates (`services/gates.py`)

`run_gates(company, contacts, signals, verdict, suppression_hit, recent, now) -> GateResult(decision, reason, lines)`; evaluated in this order before a card and again inside the push transaction; the first failing gate decides. The competitor/B2C regexes apply to the **entity's self-description** (name, bio, channel about, site title/about, its own ads/posts) — never to a buyer's question («kim kargo qiladi?» is a buyer).

| # | gate | passes when | fails → |
|---|---|---|---|
| 1 | `is_b2b` | `LEGAL_FORM_RE` on name **or** `stir` **or** an id in `company_ids` of type `hh_employer|gis2_firm|google_place|uzum_shop|goldenpages|yellowpages|glotr|prom` **or** `B2B_MARKERS` in own text **or** ImportSignal `b2b ∧ speaker_role='buyer' ∧ confidence ≥ 0.7` **or** verdict `is_b2b` **or** ≥ 2 volume signals ≥ 1 m³ from distinct evidence within 90 d **or** `repeat_count_60d ≥ 3` | `B2C_RE`/`b2c_markers` hit and no volume ≥ 1 m³ → `excluded(b2c)`; otherwise `hold` (may become B2B after enrichment) |
| 2 | `not_competitor` | no `COMPETITOR_RE` hit on self-description (as built: `COMPETITOR_NAME_RE` on a name, `COMPETITOR_SELF_RE` on a self-description), `companies.is_competitor=false`, verdict `is_competitor=false`, no ImportSignal `speaker_role='tout'` the validators kept (§4.7; mark `fail:model`), no `flag_competitor`, poster is not the owner of a `chat_monitors.role='competitor'` channel | `excluded(competitor)`, `companies.is_competitor=true`, `suppression(kind='competitor', key_type='company_id', source='resolver')` |
| 3 | `not_supplier` | none of: name `Co\.?,?\s*Ltd|有限公司|Trading Co\.|Imp(ort)?\s*&\s*Exp(ort)?` without a UZ legal form/address; exhibitor country CN; phone `+86`; website title in Chinese; 1688/Alibaba seller URL; verdict `is_supplier_cn` | `excluded(supplier_cn)`, `is_supplier_cn=true` |
| 4 | `not_client` | `is_suppressed(keys)` returns no `client` hit; `companies.state <> 'client'` | `suppressed` (`suppressed_by`), `companies.state='client'` |
| 5 | `not_dnc` | no `dnc|lost|legal|bounced|manager_veto|b2c|competitor` hit on any key | `suppressed` |
| 6 | `not_recent` | no live `cooldown`; no `push_log(sent|unknown)` for any id in `company_lineage` with outcome `open` or absent and `sent_at > now − 90 d`; no `push_log` for any of the company's phone tails in 24 h; last candidate for the company not `rejected` within 180 d | `hold` with `hold_until` = the earliest of those windows |
| 7 | `active_entity` | `registry_status <> 'liquidated'`, Places status not closed, no state-body match on the **name** | liquidated → `excluded(liquidated)`; state body in the name → `excluded(state_body)` (a state word only in text → `neg_state_body`, no exclusion); `inactive` → `hold` |

Gate lines (`GateResult.lines`, Uzbek) go on the card («Darvozalar: B2B ✓ (MChJ) · raqobatchi emas ✓ · mijoz emas ✓ · DNC yo'q ✓ · 90 kunda yuborilmagan ✓ · reestr: faol» — the window is gate 6's: no open push for the company in 90 d, none for the phone in 24 h; 06 §1.3 prints the same line). `tests/test_gates.py` fixtures: a cargo channel owner, a buyer asking «kim kargo qiladi», a Chinese exhibitor, a client phone, a cooldown, a liquidated STIR, a hokimiyat.

## 7. Suppression and push eligibility (`services/suppression.py`, `services/push_gate.py`)

**Sources and keys** (02 §2.8): ERP feed `client|lost` as `phone_hash` (+ `salt_id`); local `dnc|bounced|legal|manager_veto|b2c|competitor` as `phone_tail|tg_username|tg_user_id|instagram|domain|stir|company_id`; `cooldown` (expiring) from outcomes. `build_keys(company, contacts, secret) -> list[tuple[key_type, key]]`: every contact tail (not only the lead contact) as `phone_tail` **and** as `phone_hash = hmac_sha256(secret, tail).hexdigest()[:20]`, `stir`, `telegram_channel` and every contact username, `tg_user_id`, `instagram`, `website_domain`, every id in `company_lineage`. `is_suppressed(session, keys) -> Hit | None` is the single query of 02 §2.8; its `ORDER BY array_position(ARRAY[…], kind) LIMIT 1` is rendered from the one constant `KIND_RANK` in this module (`legal 0 · dnc 1 · client 2 · lost 3 · bounced 4 · competitor 5 · manager_veto 6 · b2c 7 · cooldown 8`, 06 §5.5), so a permanent kind always beats a `cooldown` and the gate reports the lasting reason.

**HMAC verification.** `erp_salt_id(secret) = sha256(secret).hexdigest()[:8]` must equal the feed's `salt_id`, and the feed's `rule` string must equal `"hmac-sha256(secret, last9digits) hex[0:20]"` — a different rule is a contract break (`feed_contract`, critical, no rows applied). Self-test on every full pull: the agent hashes one tail it knows is a client (the `push_log.phone_tail` of the newest `landed_client` or `won` outcome — `landed_client` exists only through the fallback door, so `won` is the usual source; 05 §B.2 step 5) and expects it in the feed; absence after a completed pull → `suppression_selftest_failed` (warning; the pull is still applied). Salt change → full pull before any push (`suppression_stale` until done). **Freshness has one home**, `services/erp_feeds.suppression_fresh()` (`FULL_MAX_AGE_H = 26`, `DELTA_MAX_AGE_H = 3`, stored `salt_id` equal to the current key's; 05 §B.2) — this module only calls it. XLSX fallback (`make import-clients`) stores clear `phone_tail` rows with `source='manual_import', kind='client'` (the feed's secret is unknown before the feed exists, 05 §B.3) and is retired automatically by the first successful full pull: a `manual_import` row is deleted when the pull holds the hash of the same tail (05 §B.2 step 5).

**Full vs delta** as 02 §2.8 / 05 §B.2 steps 3–4: delta hourly upserts by `(kind, phone_hash, key)`; nightly full pull (`30 2 * * *`) stamps `last_seen_at` and deletes unseen `erp_feed` rows only when the pull completed and returned ≥ 90 % of the previous count. **Local events**: owner 🔒 on a card (`lead_reviews.answer='dnc'`) → `suppression(dnc, phone_tail + tg_username + instagram of the lead contact, source='owner_bot')`; `/dnc <phone|@user>` (06 §1.2); outcome labels (05 §B.2: `won`/`landed_client` → `companies.state='client'` **and** a local `suppression(client, phone_tail, source='outcomes')` row at once — the feed's hashed row may lag a day; `lost_wrong_number` ×2 → `bounced`, `lost_no_contact` → `lost`, `lost_price` → `cooldown 180 d`, `lost_not_importer` → `cooldown 365 d` on `company_id`); `manager_veto` from the manager card «bu bizga kerak emas» (06). **Expiry**: only `cooldown` rows expire; a purge (`/unut`) keeps `dnc|legal|lost|client` rows (the tail stays — 02 open question 2) and removes the company. A `dnc` hit on **any** key of a company suppresses the whole company, not just that contact.

**Pre-push checklist** (`push_gate.check(session, candidate, now) -> list[Refusal]`; all must be empty, evaluated under `SELECT … FOR UPDATE` on the candidate, in one transaction with the `push_log` claim):

1. `candidate.state = 'approved'` and `approved_by` set.
2. `CRM_PUSH_ENABLED`, no `/toxtat` (`runtime_flags.paused`), `sources.erp_sync.breaker_until` clear (05 §B.1; `/yoq crm` clears it by hand), `services/erp_feeds.suppression_fresh()` true (26 h full / 3 h delta / `salt_id` match, 05 §B.2).
3. Gates 2–7 re-run on live data (a competitor flag or a DNC that arrived after approval wins).
4. Lead contact: usable phone (§1.1), `country='UZ'`, not `shared_phone` (or owner approval carries `note='shared_ok'`), `anonymised_at IS NULL`.
5. `is_suppressed(build_keys(...))` → none (`client|lost|dnc|…`); `cooldown` → refusal `cooldown_until`.
6. No `push_log(sent|unknown)` for the same `external_id` (partial unique index is the fence).
7. No `push_log(sent|unknown)` for the same `phone_tail` in 24 h; the ERP's own cap of 3 per phone per rolling 24 h (05 §A.2) is never approached by the agent itself.
8. No open push for `company_lineage` within 90 d (02 §2.10 rule 2).
9. `count(push_log today sent|unknown) < PUSH_DAILY_CAP`; `count(push_log sent|unknown, sent_at > now − 24 h, source_key_sent) < 200` — the ERP's source cap is a **rolling 24 h** over its arrivals ledger (05 §A.2), so the agent mirrors the window, not the Tashkent day. On the `agent` door this count is the ERP's count of the agent's own arrivals; during the «Boshqa» fallback the ERP's `other` cap also counts every person's «Boshqa», bot and hand-posted arrival, which the agent cannot see — this check cannot predict it there, and a `dropped_capped` outcome (read after the switch-over) is the only evidence.
10. Mon–Sat 09:30–17:30 Tashkent (outside: `deferred`, retried by the scheduler).
11. Payload built by `crm_push.build_payload` (05 §B.1) passes `tests/test_payload_schema`: `name` = the company `display_name` only (never the person, never «Rustam (direktor) — …»; the person appears only in the note line «Masʼul: <ism> (<rol>)» and only with a `legal_basis`), `phone` E.164, `kub` = the best **stated** volume `value` as string (`"2"`, `"25"`) and nothing otherwise — a band goes into the HAJM section as its 05 §B.1 `BAND_HAJM` words («fura (taxminan 80 kub)»), never as `kub`, because a mapped `kub` becomes the lead's quoted volume, the seller's «📦 N kub» and the won deal's quote (05 §A.2), `note` ≤ 700 (measured by `text_len`, 05 §B.1) whose first 159 code points (the NEGA TOPILDI section — the seller's Telegram push) stand alone, and a landed note (note + the ERP's `key: value` lines, whitespace collapsed) ≤ 1,000 so the ERP never cuts it (05 §B.1 `NOTE_MAX`, `landed_note`) carrying the NEGA TOPILDI / NIMA OLIB KELADI / HAJM / KOMPANIYA / NIMA DEYISH / BALL blocks, every quote verified, no pseudonym token left (`<PHONE_|<TG_|<NAME_`), no second person's name.
12. `external_id = f"cs:{company_id}:{yyyymm}"` for the root company id, month at claim time.
13. Score ≥ 60 at push time (a re-score after approval that fell below 60 returns the candidate to `review` with note «Ball tushdi»).
14. Owner-visible refusals are logged as `lead_reviews.note` and surfaced in `/lidlar`.

## 8. Budget controller (`services/costs.py`)

Adapted from MIYA `jarvis/costs.py`: the sum-in-SQL, the own-transaction record, the «unknown price counts as over the cap» rule and the `CostCapReached(ModelUnavailable)` code-only exception are kept; the call is generalised to any provider.

```python
Operation = Literal["classify","verdict","company_verdict","draft","enrich_search",
                    "serp_brave","serp_yandex_night","serp_searxng","serp_ddg",
                    "places_text_ids","places_details_contact","twogis_api",
                    "apify_olx","apify_2gis","apify_instagram","apify_uzum","proxy_uzum","model_reserve","serp_reserve","other"]

ALLOCATIONS: Final[Mapping[Operation, Decimal]] = {            # USD per Tashkent day; Σ = 10.00
    "classify": D("0.30"), "verdict": D("1.50"), "company_verdict": D("1.50"), "draft": D("0.20"),
    "enrich_search": D("0.75"), "model_reserve": D("0.75"),                              # Claude 5.00
    "serp_brave": D("0.165"), "serp_yandex_night": D("0"), "serp_searxng": D("0"), "serp_ddg": D("0"), "serp_reserve": D("0.835"),  # SERP 1.00 (Brave 32/day under the ≥ rule, 03 §1.3 / Q11)
    "places_text_ids": D("0"), "places_details_contact": D("1.00"), "twogis_api": D("0"),                                         # Places 1.00
    "apify_olx": D("0.60"), "apify_2gis": D("0.40"), "apify_instagram": D("0.40"), "apify_uzum": D("0.60"),                        # Apify 2.00
    "proxy_uzum": D("1.00"),                                                                                                        # proxy 1.00
}
FAMILY: Final[Mapping[Operation, str]] = {...}                  # anthropic | serp | places | apify | proxy
FAMILY_CAPS = {"anthropic": D("5.00"), "serp": D("1.00"), "places": D("1.00"), "apify": D("2.00"), "proxy": D("1.00")}

class CostCapReached(ModelUnavailable): ...                    # message is one of the codes below

async def spend(session, *, since: datetime, until: datetime | None = None, operations=None, family=None, company_id=None,
                source_key: str | None = None, with_subsources: bool = False) -> Spend   # (usd, units, calls, refused, unpriced); refused:% rows excluded from the sums
async def source_budget_usd(session, *, source_key: str, now: datetime) -> Decimal   # 03 §1.2 Budget.usd, the one home
SUBSOURCE_PREFIXES = {"serp_layer": ("search_",), "directories": ("directories_",), "maps": ("maps_",)}   # with_subsources widens a parent row
async def ask(session, *, operation: Operation, est_usd: Decimal, call: Callable[[], Awaitable[T]], price: Callable[[T], Priced],
              now: datetime, source_key: str | None = None, company_id: UUID | None = None,
              model_job_id: int | None = None, collector_run_id: int | None = None) -> T
```

This is **the** `costs.ask` signature — 02 §2.13 and 03 §1.3 point here and restate nothing. Two budget layers run in a fixed order: first the collector's `Budget.can_spend(est)` (03 §1.2, per source: `Budget.usd = sources.daily_cost_cap_usd − spend(source_key)` — the column, not a `config` key), then `costs.ask` (month → day → family → op, below). Enrichment and model jobs have no per-source `Budget` and call `costs.ask` directly.

`ask` in order: (1) `est_usd` must be a priced `Decimal` (`None`, NaN or ∞ → refuse `unpriced`; **an estimate of $0 is `unpriced` too** unless the call is free by definition — `usage.FREE_OPERATIONS` (`serp_yandex_night`, `serp_searxng`, `serp_ddg`, `places_text_ids`, `twogis_api`) or a `places_details_contact` call inside the month's first 1,000 (`usage.free_call`): a paid call priced at nothing would bypass the hard cap; only a free call skips the money rungs, so at the daily cap paid calls stop while the free part carries on); (2) refuse `cost_cap_monthly` when month spend + est ≥ `MONTHLY_COST_CAP_USD`; (3) refuse `cost_cap_daily` when day spend + est ≥ `DAILY_COST_CAP_USD`; (4) refuse `cost_cap_family:<f>` when the family's day spend + est ≥ `FAMILY_CAPS[f]`; (5) refuse `cost_cap_op:<op>` when the op's day spend + est ≥ `ALLOCATIONS[op]` — except that a Claude op may borrow from `model_reserve` (its unspent remainder) and a SERP op from `serp_reserve` only when `sources.config.serp_paid_overflow=true` (owner switch; default false); (5b) the per-op **unit** rungs, added as built (T-06) after the money rungs: `cost_cap_day_op:<op>` when the op's units today + 1 > `usage.DAILY_UNIT_CAPS[op]` (the free ops' daily counts of 03 §1.3: `serp_yandex_night` 35, `serp_searxng` 300, `serp_ddg` 40, `places_text_ids` 200, `twogis_api` 40 — free calls skip the money rungs, so their cap is a count), then `cost_cap_month_op:<op>` when the op's units this calendar month + 1 > `usage.MONTHLY_UNIT_CAPS[op]` (`serp_brave` 1,000 — the $5 monthly credit; past it the card is charged silently, 03 §1.3, so the day cap (32) and the month counter both bind — and `serp_yandex_night` 1,000, its free night quota); (5c) `op_window:<op>` outside `usage.OP_WINDOWS[op]` (`serp_yandex_night` only 02:00–09:59 Tashkent, `[start, end)`); (6) for `anthropic` ops also refuse `anthropic_silence` when the registered health check (`costs.set_anthropic_health`, T-08) reports the API down (MIYA `ANTHROPIC_SILENCE`; health problem `anthropic_silence`, warning, 06 §4.2). Every code that starts `cost_cap` is a cap (`costs.is_cap`); `unpriced`, `op_window:<op>` and `anthropic_silence` refuse for another reason, and a run says which (`meta.exhausted` `cost_cap` | `refused`, 03 §1.2). **Check and claim are one step**: under `pg_advisory_xact_lock(LEDGER_LOCK)` in its own transaction `ask` reads the ledger, decides, and writes either the refusal row or a *claim* row at `est_usd` (a counted op claims one unit), commits — releasing the lock, which covers the ledger read only, never the call — and only then (7) runs `call()`; so two concurrent calls near a cap cannot both pass, and a process killed mid-call leaves its estimate counted. (8) `price(result)` → `Priced(cost_usd, units, tokens…)` updates that claim row in place (`usage_log` refuses DELETE, not UPDATE); an answer it cannot price keeps the estimate. The attribution foreign keys (`company_id`, `model_job_id`, `collector_run_id`) are taken `FOR KEY SHARE NOWAIT` in ask's own transaction — a caller holding its row `FOR UPDATE` would otherwise deadlock with ask unseen — and a reference it could not take is written later in the **caller's** transaction (a savepoint), landing when the caller commits. When no own transaction can be had at all, the claim falls back to the caller's `begin_nested()` as MIYA `_record`, under a session advisory lock released before the caller commits (Q10); (9) if `call()` raised after the request was dispatched (timeout, connection reset) the row is written with `cost_usd=est_usd` and `units=0` — the cap never undercounts; a refusal before dispatch is **persisted** as a `usage_log` row with `cost_usd=0, units=0, model='refused:<code>'` (`provider`/`operation` as the call would have written them) — the bot is another process and cannot read a worker counter, so `/xarajat` and the digest count these rows (02 §2.13); there is no in-process `refusals[op]`. The day boundary is Asia/Tashkent midnight (`platform` rule «today is the business day»); `spend` uses `created_at >= day_start(now)` and excludes `refused:%` rows from the sums (they are 0 anyway) while `/xarajat` counts them.

**Refusal behaviour per operation.** `classify` → the interaction keeps `processed_at NULL` and is retried by the next sweep (tomorrow if the cap is daily); `verdict` → candidate stays `scored`/`review` with `verdict=NULL`, card line «AI xulosasi: byudjet tugadi»; `company_verdict` → jobs stay `pending`; `draft` → template opener; `enrich_search`/`serp_*`/`places_*` → the enrichment step is skipped with `result='budget'` and `next_enrich_at` moves to tomorrow 07:00; `apify_*`/`proxy_*` → the collector run ends `partial` (03). Nothing retries inside the same minute: a refusal sets `refused_until[op] = next hour` in process memory.

**Pricing (`usage.py`).** `MODEL_PRICES` is **code**, keyed by the configured ids: `claude-haiku-5-5 (0.10, 0.50, cache_read 0.01, cache_write 0.125)` for prompts (input + cache reads + cache writes) up to 100K tokens and **$0.50 / $2.50 above** (`HAIKU_LONG_ABOVE = 100_000`, Anthropic's model table cached 2026-10-06), its cache rates scaled by the same ratio — a long `classify` bundle or an `enrich_search` full of web results costs five times the short rate; `claude-sonnet-5-5 (2, 10, 0.20, 2.50)`, `claude-opus-5-5 (4, 20, 0.20, 5.00)`; the table's `cache_write` is the 5-minute write, a **1-hour** cache write is billed at 2× input (`cache_write_1h_tokens`, read from `usage.cache_creation.ephemeral_1h_input_tokens`); there is no `MODEL_PRICES` or `CLAUDE_DAILY_CAP_USD` in `.env` (06 §3.4) — the Claude cap is `FAMILY_CAPS["anthropic"] = 5.00` above; `batch=True` halves everything; web search `units × 0.010` ($10/1k, checked 2026-10-23) added to the token cost; `brave 0.005/query`, `yandex_night 0` inside the quota else refuse, `places_details_contact 0` for the first 1,000/month then `0.020` (UNVERIFIED SKU), `apify_* per unit` from 03 §1.3, `proxy_uzum` per MB from `sources.config.proxy_usd_per_gb`. Prices for the three model roles may be overridden by `EXTRACT_MODEL_PRICE`/`VERDICT_MODEL_PRICE`/`BATCH_MODEL_PRICE` (`parse_price`, MIYA: `in/out[/cache_read/cache_write]` in $/MTok) so a price change is a `.env` edit; an override names no long-context tier, so it keeps its model's table tier scaled to the override (an override never prices a long prompt at the short rate).

**Alerts.** `health_job` raises `spend_high` (warning; 06 §4.2) when day spend ≥ `SPEND_ALERT_PCT` (80 %) of `DAILY_COST_CAP_USD`, any family ≥ 80 % of its cap, or the month ≥ 80 % of `MONTHLY_COST_CAP_USD` — one key, the text names which; `cost_cap` (warning, told first by `costs.ask`) at a cap; each once per Tashkent day through `reminder_log(kind='alert:<key>')` (MIYA `alerts_due`). As built (T-06, seam review): `costs.claim_alert` checks and writes the `reminder_log` row under `pg_advisory_xact_lock(ALERT_LOCK, hashtext('alert:<key>'))` in its own transaction, committed before the owner is told, and deletes the claim when delivery fails — so four refusals that land together at the cap tell the owner once; `ask`'s `cost_cap` and `fire_spend_alerts` share it. The owner is told through the notifier T-08 registers (`costs.set_cap_notifier`); until then `cost_cap` is claimed by nobody and never said. 06 §4.2's sweep repeats a `TOLD_FIRST_ELSEWHERE` key after `ALERT_REPEAT_HOURS`, while these two keys are once per Tashkent day — T-08 reconciles the two rules (Q11). While `anthropic_silence` holds, `ask` refuses every Anthropic op — including the call that would prove the API back — so the health check needs a probe path outside `ask` (T-08, Q12). Every health key this section raises — `spend_high`, `cost_cap`, `anthropic_silence`, `batch_stuck`, `pii_leak`, `suppression_selftest_failed`, `suppression_stale_hit` — is a row of 06 §4.2 `PROBLEM_KEYS`; nothing here invents a key. `/xarajat` prints today and month per family and operation with the `refused:<code>` row counts; the 09:00 digest carries the same table.

## 9. Worked examples (synthetic; version-1 weights)

### 9.1 Telegram: «Guangzhoudan 2 kub mato kerak, kim olib keladi?»

Stored by the userbot (`prefilter_hit={volume_kub, china, intent}`), sender `@namuna_tekstil`, bio «Namuna Tekstil Savdo MChJ direktori», no phone in the text. Tout filter: no `COMPETITOR_RE` on sender/bio, no price list history → passes. `classify` (Haiku) on `TEXT: Guangzhoudan 2 kub mato kerak, kim olib keladi?` with `SENDER_BIO: Namuna Tekstil Savdo MChJ direktori` → `{speaker_role: buyer, b2b: true, b2c_markers: false, goods: ["mato"], goods_category: textile, origin_cn: explicit, volume_m3_est: 2, volume_basis: stated, volume_quote: "2 kub mato", frequency: unknown, confidence: 0.86}`; cost ≈ $0.00012. Quote verified (substring). Resolver: no STIR/phone; `tg_username=namuna_tekstil` unknown; `name_key='namuna tekstil savdo'` (from the bio) → no trigram candidate → **new company** `Namuna Tekstil Savdo MChJ` (legal_form MChJ, city unknown), person-tier contact `{full_name: NULL (not stated), role: direktor, telegram_username: namuna_tekstil, legal_basis: public_source}`. Signals from rules and the model (same evidence, upsert): `vol_stated_1_3 (value 2, quote "2 kub mato")`, `cn_origin (quote "Guangzhoudan")`. Gates: `is_b2b` pass (MChJ in bio + model); competitor/supplier pass; suppression: username not listed; recent: none. Score: volume 20 + origin 20 + solidity 0 + recency 10 (`derived_fresh_7d`) + contact 2 (`dm_channel`) + corroboration 0 = **52 → warm → `enriching`**, `next_enrich_at = +1 h`.

Enrichment attempt 1: E2 skipped (no site); E3 `about` not applicable (user, not channel); E4′ registry by name «Namuna Tekstil Savdo» → orginfo hit, STIR `300000011`, active since 2019 → `ent_legal_confirmed 8`, `ent_age_2y_active 4`, `registered_on`, legal address Toshkent (`city` carried); E5 SERP `"Namuna Tekstil Savdo" Toshkent site:yellowpages.uz OR site:2gis.uz` (Brave, $0.01) → a directory card with `+998 90 000 00 01` → company-tier contact, `phone_kind=mobile`, `shared_phone=false` (first sighting). Stop S1 reached after re-score: volume 20 + origin 20 + solidity 12 + recency 10 + contact 8 (mobile 6 + dm 2) + corroboration 0 (registry/directory are not volume/origin families) = **70 → hot → `review`**; `verdict` (Sonnet, ≈ $0.011) adds segment `A`, band `1_3`, reasons in Uzbek; card sent; spend on this company ≈ $0.02. (00 §4 quotes the same 70 for this path — `derived_corroboration` is 5 or 0, and here it is 0.)

### 9.2 OLX: business seller, 14 ads «оптом со склада, стиральные машины»

`olx_business` emits one `kind=company` (`company_name_raw="Sardor"`, `olx_user_id=AujTE`, `business=true`, `registered=2024-05`, `ads_count=14` in «Бытовая техника», `telegram=sardor_optom_tex` from ad text) plus listings; no phone (reveal endpoint never called). `classify` over the concatenated ad texts → `{speaker_role: seller, b2b: true, goods: ["kir yuvish mashinasi","konditsioner"], goods_category: electronics, origin_cn: unknown, volume_basis: inferred, volume_m3_est: null, …}`. Resolver: `name_key='sardor'` is person-shaped (`is_person_shaped`, §1.6: a given name in `FIRST_NAMES`) → no fuzzy and **not a company name**; `tg_username` unknown → new company `display_name='OLX sotuvchi AujTE'` with `company_ids(olx_user, AujTE)`, and «Sardor» becomes the one person-tier contact (`full_name='Sardor'`, `role=NULL`, `telegram_username=sardor_optom_tex`, `legal_basis='public_source'`, `source_url` = the seller page, `collected_at=now()`). Signals: `vol_wholesale_bulky 20` (`оптом` + «бытовая техника»), `vol_marketplace_velocity 20` (14 ≥ 10 ads in one bulky category, value 14), `cn_sector_prior 10` (appliances), `ent_not_micro 3` (≥ 10 wholesale ads, business account registered ≥ 1 y). Gates: `is_b2b` pass (B2B markers + business flag); not competitor (no cargo words). Score: volume min(40, 40) = 40 + origin 10 + solidity 3 + recency 10 (newest ad 3 d) + contact 2 + corroboration 0 = **65 → ≥ 60 but no usable phone → warm → `enriching`**. Attempts: E2 none; E5 directory search by «Sardor» refused by the generic-name rule (no SERP spent); E6 no Instagram; E7 `enrich_search` allowed (score ≥ 50) → nothing verifiable → `result='none'`; after attempt 3, because score ≥ 60 and `contacts.telegram_username` exists, `enrich_job` sets state `review`, `tier='warm'`, `score_breakdown.gates.no_phone=true` (§3 S4) and `review_cards_job` sends the **handoff variant** of the owner card (`[👤 Menejerga] [✖️ Yoʻq]`, «📨 Faqat Telegram: @sardor_optom_tex», 06 §1.3); the owner's `handoff` → `hold` + manager card; a manager's `/tel` → `hold → scored → approved(manager:<tg_id>)` → the normal push gate. Never pushed to the ERP without a phone.

### 9.3 hh.uz: «Менеджер ВЭД со знанием китайского» at OOO «Tex Snab Import»

`hh_vacancies` emits `kind=vacancy` (excerpt: «Менеджер ВЭД со знанием китайского … закупки оборудования в Китае, работа с 1688 и фабриками, контроль отгрузок контейнеров», published 2 d ago) and `kind=company` (`hh_employer_id=7781`, `site_url=texsnab.example.uz`, `open_vacancies=4`, `trusted=true`). Normaliser: `legal_form=OOO`, `name_key='tex snab import'`. Resolver: new company; `company_ids(hh_employer, 7781)`; domain `texsnab.example.uz`. Signals (regex only, no model): `vol_vacancy_import 15` (quote «Менеджер ВЭД»), `cn_origin 20` (quote «в Китае»), `vol_container 40`? — **no**: «контроль отгрузок контейнеров» is a job duty, and `CONTAINER_RE` is applied to the entity's own trading text, not to vacancy duties (`from_rules` skips volume regexes for `kind=vacancy` other than `vol_vacancy_import`); `ent_not_micro 3` (4 open vacancies, trusted). Score: 15 + 20 + 3 + 10 + 0 + 0 = **48 → warm → `enriching`**. Attempt 1: E2 fetches `texsnab.example.uz/contacts` → `+998 71 200 00 00` (landline, company-tier), `@texsnab_uz`, footer «ИНН 301234567», about page «оптовые поставки сантехники и насосного оборудования» → `vol_wholesale_bulky 20` (оптом + сантехника), `cn_sector_prior` already covered by `cn_origin`; E4 registry by STIR → active since 2017 → `ent_legal_confirmed 8`, `ent_age_2y_active 4`. Re-score: volume min(40, 15 + 20) = 35 + origin 20 + solidity 15 + recency 10 + contact 2 (`dm_channel`; landline gives no mobile points) + corroboration 5 (vacancy family + search family via `enrich_site`) = **87 → hot** (landline is a usable phone) → `verdict` (segment D, band `3_10`, decision_maker_role «ВЭД менежер / rahbar») → `review` card with a template opener if `draft` is refused. The nightly `company_verdict` is not needed (verdict < 30 d).

## Open questions

1. **Schema touches for 02 — settled in the review pass:** `companies.dup_of uuid REFERENCES companies(id) ON DELETE SET NULL` + `dup_score smallint`, the seeded `sources` row `enrich_site` (family `search`) and the `usage_log.operation` CHECK equal to §8's `Operation` literal verbatim (with `provider` gaining `'twogis'`) are all day-1 parts of `0001_initial` (02 §5). No `merge_proposals` table.
2. **03 alignment:** `ImportSignal` here is a superset of the field list in 03 §2.3 (`speaker_role` replaces `is_buyer_intent`/`competitor`, `b2c_markers` replaces `b2c`, adds `goods_category`, `volume_quote`). 03 to adopt these names, or 04 to emit the 03 aliases?
3. **STIR checksum** (§1.5) — run `stir_probe` on day 1 data; adopt only if a scheme explains ≥ 99.5 %.
4. **Mobile prefixes** `20/50/55` and the landline area-code list (§1.1) — confirm against the current numbering plan before the kind affects `derived_contact_mobile`.
5. **Web search tool variant on Haiku 5.5** — the price ($10/1k searches) and `web_search_20250305` on Haiku 5.5 are confirmed against the current API docs (review pass, 2026-10-23); still a day-1 check: that the tool accepts `allowed_domains` on Haiku; otherwise `enrich_search` runs on Sonnet 5.5 at ≈ 3× the token cost (cap unchanged, ≈ 15 companies/day).
6. **Registry hosts** — which of orginfo/registr.stat.uz/my.soliq/myorg answers a per-INN GET without captcha on day 1 decides whether E4 is automated or the manual `/stir` queue (`/stir <candidate_id> <9 digits>`, 06 §1.2).
7. **Landline-only leads** count as usable phones (ERP identity works, managers can call). Owner to confirm, or require a mobile for `hot`.
8. **Verdict policy under budget pressure** — when `verdict` is refused, should a `hot` candidate still be carded without AI reasons (current: yes, with a line saying so), or wait for the night batch?
9. **The opt-out sentence's apostrophes** (§4.5, T-28/T-30): `bo'lmasa`, `so'z` and the playbook templates (`docs/sotuv-playbook.md` §3) use ASCII `'`, which 07 §1.11's lint refuses in text the code renders. (a) respell them `boʻlmasa`, `soʻz` in the prompt, the templates and the redline corpus — recommended (the gate folds apostrophes, so the check is unchanged); (b) exempt the sentence by name in `test_uz_text.py`.
10. **`ask`'s caller-transaction fallback** (§8 step 8, seam review S2): when `ask` cannot open a transaction of its own, the claim sits in the caller's savepoint under a session advisory lock that is released before the caller commits — so another process can pass the cap meanwhile, and a caller rollback deletes the claim. Holding the lock until the caller commits would serialise every `ask` behind a 2-hour run session. Recommended: fail closed — refuse with a code of its own (`ledger_unavailable`) when no own transaction can be had; decide before T-15 (the first caller that holds long transactions).
11. **Once a day vs `ALERT_REPEAT_HOURS`** (§8 Alerts): `spend_high` and `cost_cap` are said once per Tashkent day, 06 §4.2's sweep repeats a told-first key after `ALERT_REPEAT_HOURS`. Recommended: the money keys keep once a day (a cap holds until midnight; repeating it tells the owner nothing new). T-08 decides and pins it.
12. **`anthropic_silence` latches** (§8 step 6): while it holds, `ask` refuses every Anthropic op, so no successful call can clear it. T-08's health check needs a probe outside `ask` (e.g. a free `models.retrieve` / count-tokens call through the extraction client, or the age of the last success alone with a timed retry).

## Acceptance criteria

1. `tests/test_normalise.py`: golden tables of ≥ 40 phones (every row of §1.1 plus Cyrillic context, `tel:` links, `00` prefix, foreign numbers, all-equal rejection), ≥ 20 names (§1.4 rows, every `LEGAL_FORMS` token as a whole token only — «Sklad Plus» keeps «plus», «IP Telecom» loses «ip» and the test documents it), handles with reserved paths, domains with link hubs and `site_key` folds, STIR shape cases; `tests/test_keys.py` (02) stays green.
2. `tests/test_resolver.py` (postgres): exact-key precedence (a STIR match beats a conflicting phone match; conflict recorded), fuzzy ≥ 90 attaches and learns the alias, 80–89 creates with `dup_of`, < 80 creates, generic-token and person-shaped guards refuse merges, landline tails need city agreement, two concurrent inserts of one name produce one company (advisory lock), `merge_into` refuses different STIRs and `unmerge` restores `moved` ids within 24 h.
3. `tests/test_shared_phone.py`: the third unrelated company on one tail flips `shared_phone` on all rows; a shared tail never resolves by phone and never scores `derived_contact_mobile`.
4. `tests/test_enrich.py` with fake fetcher/SERP/registry/Places: the chain stops at S1 (phone + ≥ 60), respects the per-company $0.10 cap (paid steps skipped, free steps run), schedules attempts at 1 h/24 h/7 d, lands in `hold` after attempt 3 — or in `review` with `tier='warm'` and `gates.no_phone=true` when score ≥ 60 and a `telegram_username` exists (S4) — never calls a paid step outside `costs.ask`, never imports `telethon` (E3 reads `t.me/s/<username>` through the fake fetcher and enqueues `chat_monitors(priority 3)` on a 404), and writes an `enrichment[]` entry per step.
5. `tests/test_pseudonyms.py` + `tests/test_pseudonymised_requests.py`: no raw phone/handle/name reaches `model_jobs.request` or the API client (recorded through a fake transport); round trip restores exactly; numbering stable across a bundle.
6. `tests/test_extraction.py` with recorded responses: structured path, grammar-failure fallback, JSON nudge retry, refusal → `failed`, `max_tokens` retry; `test_validators.py`: a quote that is not a substring yields no `vol_*`/`cn_origin` signal, container sanity bounds, tout downgrade, output PII hygiene.
7. `tests/test_scoring.py`: the three worked examples reproduce 52 → 70, 65, 48 → 87 exactly from fixture signals; caps bind; decay halves a 181-day-old `vol_stated_3plus`; derived keys come only from contacts/ages; the breakdown JSON validates against `schemas/score_breakdown.json`; `score()` is referentially transparent (same input twice, same bytes).
8. `tests/test_gates.py`: each gate has a failing fixture and the first failure decides; «kim kargo qiladi?» from a buyer passes `not_competitor`; a state word in text only produces `neg_state_body`, in the name produces exclusion.
9. `tests/test_suppression.py`: `build_keys` covers all contact tails and lineage ids; HMAC rule and `salt_id` verification; self-test; full-pull shrink guard; a `dnc` on any key suppresses the company; cooldown expiry.
10. `tests/test_push_gate.py` (fake ERP, fake clock): all 14 checklist items individually refuse; the refusal reason reaches `lead_reviews.note`; two workers claiming one candidate produce one `push_log` row.
11. `tests/test_costs.py`: allocations sum to 10.00 and family caps to 10.00 (asserted); refusal order monthly → daily → family → op; reserve borrowing only for Claude ops (a SERP op only with `serp_paid_overflow`); `serp_brave` refused at 1,001 units in a month; unpriced refused; a timeout after dispatch records `est_usd`; a refusal before dispatch writes a `usage_log` row with `cost_usd=0, units=0, model='refused:<code>'` and nothing else; batch provisional rows are updated, not duplicated; `spend_high` and `cost_cap` alerts fire once per day.
12. `tests/test_redlines.py`: the opener corpus (≥ 30 bad drafts with prices, days, guarantees, links, STIR, competitor names, English, missing opt-out; ≥ 10 good ones) and every playbook template pass/fail as expected; a flagged draft is never rendered.
13. `make doctor` reports the configured model ids are priced, the allocations table, and the last `salt_id`.

## Review pass changelog

Items of `REVIEW-2026-10-23.md` applied to this file (2026-10-23):

- **4** — §2.1 and §3: `companies.dup_of/dup_score` and the seeded `enrich_site` row are day-1 parts of `0001` (02 §2.1/§5), no longer "schema touches to decide"; Open Q1 marked settled (also covers **5**: `usage_log.operation` CHECK = §8 `Operation` verbatim, `provider` + `twogis`).
- **6** — §7: `KIND_RANK` (`legal 0 … cooldown 8`) named as the one constant in `services/suppression.py` that renders `is_suppressed`'s `array_position(...)`.
- **7** — §7 body and checklist item 2: freshness is `services/erp_feeds.suppression_fresh()` (`FULL_MAX_AGE_H = 26`, `DELTA_MAX_AGE_H = 3`, 05 §B.2); full pull `30 2 * * *`; the old «03 §2.2» pointer replaced.
- **2 / 3** — §7: full/delta pointer to 02 §2.8 / 05 §B.2 steps 3–4; XLSX fallback = clear `phone_tail`, `source='manual_import'`, retired per 05 §B.2 step 5.
- **8** — §0: `classify_job`/`score_job` wake on `NOTIFY cs_interaction` (15-min fallback sweep), `review_cards_job` every 2 min inside `REVIEW_HOURS`, new paragraph on the event-driven chain; §4.1 N-06 text «< 3 min» → «< 5 min p90». Heartbeat spelled `job:<id>` as 06 §4.1 (item **17** touches 02/03; 04 only aligned the name).
- **9** — §3 S4, §5.2, §9.2, AC 4: after attempt 3 with `score ≥ 60 AND contacts.telegram_username IS NOT NULL` → `review`, `tier='warm'`, `gates.no_phone=true`; handoff variant rendered by the card job; `handoff → hold`, `/tel` → `hold → scored → approved(manager:<tg_id>)`; `approved_by` values listed.
- **10** — §3 E3: `about` is read from `https://t.me/s/<username>` through `Fetcher`; a Telethon read only via `chat_monitors(status='queued', priority 3)` drained by `userbot/about.py`; no `telethon` import in the worker (AC 4 pins it).
- **11** — §7 checklist item 11: payload `name` = company `display_name` only; the person only in the «Masʼul: <ism> (<rol>)» note line.
- **12 (a)** — §1.6: a poster's `tg_user_id` goes to `contacts.telegram_id`, never `company_ids`; R3 strips it. **12 (b)** — §1.6 `is_person_shaped` defined (initials, surname/patronymic suffix, honorific, given name in `FIRST_NAMES`); person-shaped OLX `user.name` → `display_name='OLX sotuvchi <olx_user_id>'` + person-tier contact with `legal_basis='public_source'`; §2.2 guard points to it; §9.2 rewritten accordingly. **12 (d)** — §3 Places paragraph: the copied phone carries `source_key='maps_google'` and is cleared by retention R4b (02 §4).
- **14** — §8 pricing: `MODEL_PRICES` is code (`usage.py`), no `MODEL_PRICES`/`CLAUDE_DAILY_CAP_USD` in `.env`; the Claude cap is `FAMILY_CAPS["anthropic"]`.
- **15** — §8: declared the one `costs.ask` signature and the order `Budget.can_spend` (per source, `sources.daily_cost_cap_usd − spend(source_key)`) → `costs.ask` (month → day → family → op).
- **18** — §8 alerts: `cost_80`/`cost_month_80` → `spend_high`; `cost_cap`, `anthropic_silence`, `batch_stuck` (§4.8), `pii_leak` (§4.7), `suppression_selftest_failed`, `suppression_stale_hit` named as rows of 06 §4.2 `PROBLEM_KEYS`.
- **19** — §3 registry paragraph and Open Q6: `/stir <candidate_id> <9 digits>` exists in 06 §1.2 (writes `companies.stir`, re-runs E4); §7 `/dnc <phone|@user>` points to 06 §1.2.
- **21** — §7 checklist item 2: breaker is `sources.erp_sync.breaker_until` (`/yoq crm` clears it), not `crm_push`.
- **22** — §7 checklist item 11: `note` ≤ 700 chars, first line ≤ 160 (05 §B.1 `NOTE_MAX`).
- **25** — §5.3: `entity_solid` → `ent_not_micro`; `ent_legal_confirmed` only from a registry hit (E4).
- **26** — §9.1: the parenthetical about 00 quoting 74 replaced — 00 §4 now quotes the same 70.
- **27** — §6 gate line: «30 kun ichida yuborilmagan» → «90 kunda yuborilmagan» (gate 6: 90 d per company, 24 h per phone).
- **29** — §4.2: `assert_masked` added to `services/pseudonyms.py`; the names `Pseudonymised/pseudonymise/restore` declared the only ones; regexes imported from `collectors/keywords.py` (03 §1.4).
- **30** — §8 step 9 and AC 11: a refusal is persisted as a `usage_log` row `cost_usd=0, units=0, model='refused:<code>'`; no in-process `refusals[op]`.
- **31** — §8: `ALLOCATIONS["serp_brave"] = 0.165` (33 queries/day), `serp_reserve` 0.80 → 0.835 so Σ stays 10.00 and SERP 1.00; new rung (5b) `serp_brave ≤ 1,000` per month inside `costs.ask`; AC 11 pins it.
- **32** — §7 local events: `won`/`landed_client` → `companies.state='client'` **and** a local `suppression(client, phone_tail, source='outcomes')` row (05 §B.2).
- **36 / preamble** — the «UNVERIFIED $10/1k» web-search marker dropped in §4.1 and §8 (verified against the pricing page 2026-10-23); Open Q5 narrowed to the `allowed_domains`-on-Haiku day-1 check.

Not touched here because another file is the home: **1** (sub-source keys stay valid as `evidence.source_key`, nothing to change), **13**, **16**, **20**, **23**, **24**, **28** (05 reads §4.3's conversions; `BAND_KUB` lived in 05 — superseded: 05 §B.1 `BAND_HAJM` replaced it at the T-07 verification), **33**, **34**, **35**, **37** (no `db/models.py`, `bot/cards`, `rv:` or `collector_runs` text in 04), **38**.

**T-07 as built (2026-10-24)** — §7 checklist item 11 follows the ERP as built (05 §A.2, §B.1): the ERP collapses the note's line breaks and the push shows the first 159 code points, so the rule is «first 159 code points stand alone» and «landed note ≤ 1,000», measured by `text_len` (UTF-16 units, safe for both ERP builds). Nothing else in this file depends on the ERP's answers.

**T-07 verification (2026-10-24)** — §7 checked against fullprompt `agent-feed` at `23fe1736`: item 11's `kub` is a STATED volume only (a band goes into the HAJM text as 05's `BAND_HAJM`, which replaces `BAND_KUB`: the ERP writes a mapped `kub` as the lead's quoted volume and carries it into the deal, `inbound.ts`/`winLead`); item 9's 200-per-source check is a rolling 24 h like the ERP's `inboundCount`, and it cannot predict the ERP's `other` cap during the fallback; item 7 says rolling 24 h; the HMAC self-test reads the newest `landed_client` **or `won`** tail (05 §B.2 step 5).

**T-07 third review (2026-10-24)** — against fullprompt `agent-feed` at `a4f9f433`: the review-pass item 28 pointer «`BAND_KUB` lives in 05» is marked superseded (05 holds `BAND_HAJM`). Item 11 is unchanged in substance: its «fura (taxminan 80 kub)» still routes as 80, and 05 §B.1 now writes the two range bands with their low end right before «kub» («taxminan 1 kubdan 3 kubgacha», «taxminan 3 kubdan 10 kubgacha»), because the ERP's `textVolume` reads the number right before the first «kub» and «1–3 kub» routed as 3. The «first 159 code points» of item 11 stay code points (the Telegram preview is the one ERP cut that counts them); every other ERP cut is UTF-16 units, which `text_len` already measures.

**Wave 1 as built (2026-10-24)** — folded from the wave-1 build notes and the seam review, checked against `services/keys.py`, `services/normalise.py`, `services/names.py`, `services/text.py`, `collectors/keywords.py`, `services/costs.py` and `services/usage.py` at `f8c3303`:

- §1: `keys.py` is the home of the key functions. §1.1 `phone_e164`'s signature, the reject rule (all-equal subscriber digits refused — the table won over `(\d)\1{8}`), `phone_kind`'s codes (20/50/55 `unknown`, Q4 still open). §1.3 `website_key` with `SHARED_HOSTS`, `site_key` with `TENANT_HOSTS` (the builder's lists moved into the spec). §1.4 `LEGAL_FORMS` as built (both transliterations of «obshchestvo»/«individualnyy»), `strip_marks`, `name_key`'s diacritic folding («Öztürk»). §1.6 the kind mapping and `serp_hit`, `canonical_city`'s «Toshkent viloyati» (code 70) apart from the city (71), 309 hand-written `FIRST_NAMES`, a replacement label with `name_key` NULL, `seller` in `PERSON_KINDS`, the exactly-14-digit rule.
- §4.2 mask order: phones, Telegram, e-mail, then Instagram (`IG_RE`'s label branch over-matches what follows «IG »). §4.5 the ASCII-apostrophe note (Q9). §5.3 `B2B_RE`.
- §8 as built: `spend(…, source_key, with_subsources)` and `source_budget_usd`; the allocations comment (Brave 32/day); a $0 estimate is `unpriced` unless the call is free (`FREE_OPERATIONS`, Places Details within the month's first 1,000); the unit and window rungs after 5b (`cost_cap_day_op:`, `cost_cap_month_op:`, `op_window:`); check and record one atomic claim under `pg_advisory_xact_lock(LEDGER_LOCK)`, the caller-savepoint fallback stated (Q10); Haiku's long tier above 100K tokens ($0.50/$2.50) and the 1-hour cache write at 2× input; alerts through `claim_alert` (`ALERT_LOCK`), `set_cap_notifier`, `set_anthropic_health`.
- Open questions 9–12 added: opt-out apostrophes, the S2 fallback (recommended: fail closed with `ledger_unavailable`), once a day vs `ALERT_REPEAT_HOURS`, a probe for `anthropic_silence`.
