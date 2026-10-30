# Fixtures: company sites, link hubs, Telegram public pages (SYNTHETIC)

Pages for enrichment steps **E2** (a company's own website, a link hub) and **E3** (a
Telegram channel's public page `https://t.me/s/<username>`) of `docs/spec/04-processing.md`
§3, and for 03 §2.1's `parse_company_site` (shared with T-25's `serp_layer`):
`clientscrapper/collectors/company_site.py`, `clientscrapper/collectors/telegram_page.py`.

**Every file here is SYNTHETIC — hand-written for T-24, not recorded.** No company site,
link hub or `t.me/s/` page had been recorded when T-24 was built (the recorder's three
stages of 2026-10-29 fetched registries, tenders and maps only). The shapes below are the
ones the parsers assume: schema.org, `tel:` and `mailto:` links, a footer, Telegram's
`tgme_*` widget classes as the builder knew them. **STILL UNVERIFIED** until real pages
are recorded from the VPS with `python -m clientscrapper.tools.record_fixture` (keep the
recorded files next to these, without the `_synthetic` suffix, and re-run the parse tests
against them). Phones are `+998 90 000 00 NN` / `+998 71 200 00 NN`, e-mails
`namuna_NN@example.uz`, handles `namuna_*`, domains `*.example.uz` / `*.example.cn`.

| File | Stands for | What it proves (`tests/collectors/test_company_site_parse.py`) |
|---|---|---|
| `site_landing_synthetic.html` | a wholesaler's landing page: `<title>` «Namuna Tekstil Savdo \| Optom mato Toshkentda», `og:site_name`, a `tel:` and a `mailto:` link, Telegram and Instagram links (and a `t.me/share` link that is no handle), a footer with the address, «"Namuna Tekstil Savdo" MChJ · STIR 300000031», a link to `/kontakty` | name from the title's first segment; the `tel:` phone once (deduplicated against the footer text); TG/IG from the links; STIR + legal form → `entity_solid`; optom + mato → `wholesale_bulky`; «Xitoydan» → `china_origin`; the address line without its «Manzil:» label; `about_link` → `/kontakty` |
| `site_contacts_synthetic.html` | the same firm's `/kontakty` page (E2's second page): a Tashkent landline and the mobile in the text, a Russian address line, «© 2026 ООО …» in the footer | a generic title segment («Контакты») is skipped; phones from the visible text; `улица` is a street word |
| `site_jsonld_synthetic.html` | a page whose contacts live in a schema.org `Organization` JSON-LD block (`telephone`, `taxID`, `PostalAddress`, `sameAs` Telegram/Instagram), title «Bosh sahifa — Namuna Elektronika» | the JSON-LD path: phone, STIR, handles, address; «Bosh sahifa» skipped |
| `site_js_only_synthetic.html` | a single-page app shell: an empty `#root` and script bundles | `js_only` (04 §3 E2: `result='js_only'`), no observation |
| `site_cargo_synthetic.html` | a cargo forwarder's own site («Namuna Kargo — Xitoydan yuk tashish», «Kargo xizmati … rastamojka») | `competitor` only through the cargo core (`COMPETITOR_NAME_RE` on the name, `COMPETITOR_SELF_RE` on the text, 03 §1.4) |
| `site_cn_title_synthetic.html` | a Chinese supplier's site: the title «义乌市纳穆纳贸易有限公司», a `+86` number | `name_raw` is the title as written — the shape gate 3 reads for «website title in Chinese» (04 §6, `gates.SITE_SOURCE`); the `+86` number is not read as an Uzbek one |
| `hub_taplink_synthetic.html` | a link hub (taplink): WhatsApp, Telegram, Instagram and website links | `parse_link_hub`: the `wa.me` number, the handles, the first own website |
| `tme_channel_synthetic.html` | a channel's public preview `t.me/s/namuna_optom`: title, description with phone, site and address, two posts with dates | E3: `parse_channel_page` + `channel_observation` (phone, site, address, newest post date) |
| `tme_user_synthetic.html` | what `t.me/s/<a person>` redirects to: a user's page with a bio | not a channel → None: a person's page is never read (06 §5.1), E3 queues the username for the reader instead |

## Recorder ledger

- site_landing_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, E2 landing page
- site_contacts_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, E2 second page
- site_jsonld_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, schema.org Organization
- site_js_only_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, JS-only shell
- site_cargo_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, competitor self-description
- site_cn_title_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, Chinese title (gate 3)
- hub_taplink_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, link hub
- tme_channel_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, E3 channel page
- tme_user_synthetic.html · 2026-10-29 · synthetic (no URL) · scrubbed: n/a (synthetic) · T-24, E3 user page (not read)
