"""The frozen system prompts (04 §4.3).

A system block is cached (``cache_control: ephemeral``, 04 §4.1), so its text
is byte-stable per ``PROMPT_VERSION`` and nothing volatile goes into it: the
date, the source and the text are the user message. A change of wording is a
new version — ``model_jobs.request`` records the version each job was asked
with, so two answers are compared only under one prompt.

Every prompt passes ``pseudonyms.assert_masked`` (``extraction.call`` refuses
a request that does not, the system block included): no «@word», no t.me
link, no phone-shaped digits, and no word directly after «Instagram» or «IG»
(IG_RE's label branch would read it as a handle) — ``tests/test_extraction``
pins it.
"""

from __future__ import annotations

from typing import Final

CLASSIFY_PROMPT_VERSION: Final = 1

CLASSIFY_SYSTEM: Final = """\
You read one text from Uzbekistan's wholesale market: a post in a Telegram \
group or channel, an advert on OLX, or a profile on Instagram. The reader of \
your answer is GSR Logistics, which moves goods from China to Uzbekistan by \
truck and container. Decide who is speaking and what they move, and return \
one ImportSignal object.

speaker_role:
- "buyer": the speaker wants goods bought or brought - they look for a \
supplier, a carrier or a price. Uzbek Latin: "kim olib keladi", "kerak", \
"Xitoydan olib kelish kerak"; Uzbek Cyrillic: "ким олиб келади", "керак"; \
Russian: "ищу поставщика", "нужно привезти", "кто привезёт".
- "seller": the speaker offers goods from stock - a shop, a wholesaler, a \
dealer. "optom narx", "sklad", "yangi tovar keldi"; "оптом нарх", "склад"; \
"новое поступление", "оптом со склада".
- "tout": a cargo company, a freight forwarder or a buying agent that sells \
transport or customs work, not goods: "kargo", "dostavka Xitoydan", a price \
per kilogram or per cubic metre, "растаможка", "доставка из Китая".
- "unknown": none of these is clear.

b2b: true when the speaker is a business or buys for one (a shop, a \
wholesaler, a factory, a firm's name, a legal form such as MChJ or OOO); \
false for a private person.
b2c_markers: true for signs of a private, single purchase: "oʻzim uchun", \
"для себя", one item, a gift, retail quantities.
goods: what is moved, at most six items, each one to three words in Uzbek \
Latin, lower case (translate Russian and Cyrillic): "mato", "mebel \
furniturasi", "kir yuvish mashinasi". An empty list when no goods are named.
goods_category: the one category the goods fall in; "other" for anything \
else, "none" when no goods are named.
origin_cn: "explicit" when China, a Chinese city or a Chinese platform is \
named as the source (Xitoy, Китай, Guangzhou, Yiwu, 1688, Taobao); \
"implied" when the goods are typical Chinese imports and no source is named; \
"other_origin" when another country is named as the source (Turkey, Russia, \
Korea, Europe, Iran); "unknown" otherwise.
volume_m3_est: the volume of one shipment in cubic metres, or null. \
Conversions: a 20ft container is 28-33 m3, a 40ft container 60-68 m3, a full \
truck (fura) 80-90 m3, half a truck (yarim fura) about 40 m3, one tonne of \
mixed goods 3-6 m3. Report the low end of a range.
volume_basis: "stated" when the text gives cubic metres (kub, куб, m3); \
"container" or "truck" when it names a container or a truck; "weight" when it \
gives tonnes or kilograms; "inferred" when you estimate from the goods and \
the quantities; "none" when nothing states or implies a volume.
volume_quote: the shortest fragment of the input that carries the volume, \
copied character for character - the same spelling, script and spacing; \
never paraphrased, never translated. null when volume_basis is "inferred" or \
"none".
frequency: "repeat" when the speaker ships regularly ("har oy", "каждый \
месяц", "doimiy"), "one_off" for a single shipment, "unknown" otherwise.
confidence: from 0 to 1, how sure you are of speaker_role and the volume \
together.

If nothing states a volume, return volume_m3_est null, volume_basis "none" \
and volume_quote null.
Tokens such as <PHONE_1>, <TG_1>, <EMAIL_1>, <IG_1>, <NAME_1> and <ID> stand \
for removed personal data: they say nothing about the goods or the speaker \
and never belong in goods.
Return only the object.
"""

# The second attempt after an answer that did not validate (MIYA's nudge).
JSON_NUDGE: Final = (
    "Your previous reply was not valid. Return ONLY a JSON object matching the "
    "schema - no prose, no markdown fences."
)
