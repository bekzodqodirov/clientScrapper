"""tools/scrub_fixture (07 §1.1): person data out, structure, keys and lengths
kept, the same real value → the same synthetic NN inside one file.

Pure. The "real" inputs are synthetic too (07 §1.0): phones +998 90 000 00 NN,
handles namuna_<n>, generic first names.
"""

from __future__ import annotations

import json
import re

import pytest

from clientscrapper.tools import scrub_fixture as sf

TAG = re.compile(r"<[^>]+>")


def html(text: str) -> bytes:
    return text.encode()


def test_phones_become_synthetic_digit_for_digit_and_repeat_the_same_nn():
    page = (
        '<div class="card"><a href="tel:+998900000041">+998 (90) 000-00-41</a>'
        "<span>90 000 00 42</span><span>+998 90 000 00 41</span></div>"
    )
    out = sf.scrub_bytes(html(page), kind="html").body.decode()
    assert out == (
        '<div class="card"><a href="tel:+998900000001">+998 (90) 000-00-01</a>'
        "<span>90 000 00 02</span><span>+998 90 000 00 01</span></div>"
    )
    assert len(out) == len(page)
    # The same tags in the same order; only digits inside them moved.
    assert [re.sub(r"\d", "#", t) for t in TAG.findall(out)] == [
        re.sub(r"\d", "#", t) for t in TAG.findall(page)
    ]


def test_numbers_that_are_not_phones_stay():
    page = "<p>Narxi 1250000 so'm, ID 123456789, yil 2026, STIR 300000041</p>"
    assert sf.scrub_bytes(html(page), kind="html").body.decode() == page


def test_handles_become_namuna_nn_but_css_emails_and_reserved_paths_stay():
    page = (
        "<style>@media (max-width: 600px) { a { color: red } }</style>"
        '<script type="application/ld+json">{"@context": "https://schema.org"}</script>'
        "<p>Yozing: @namuna_sotuvchi yoki https://t.me/namuna_kanal, "
        "yana @namuna_sotuvchi. Pochta: info@namuna.example.uz. "
        "Guruh: https://t.me/joinchat/AbCdEf. Insta: instagram.com/namuna.shop</p>"
    )
    out = sf.scrub_bytes(html(page), kind="html").body.decode()
    assert "@media (max-width" in out
    assert '"@context"' in out
    assert "@namuna_01 yoki https://t.me/namuna_02" in out
    assert "yana @namuna_01." in out
    assert "info@namuna.example.uz" in out
    assert "https://t.me/joinchat/AbCdEf" in out
    assert "instagram.com/namuna_03" in out
    assert "namuna_sotuvchi" not in out and "namuna_kanal" not in out


def test_json_keeps_keys_and_list_lengths_and_scrubs_person_fields():
    data = {
        "data": [
            {
                "id": 1,
                "title": "Optom divan, tel +998 90 000 00 41",
                "user": {"id": 7, "name": "Sardor", "company_name": None},
                "contact": {"name": "Namuna Mebel MChJ", "phone": "+998900000042"},
                "params": [{"key": "price", "value": "1 250 000"}],
            },
            {
                "id": 2,
                "title": "Stol",
                "user": {"id": 8, "name": "Sardor"},
                "seller_name": "Aziz aka",
                "phone_int": 998900000041,
            },
        ],
        "metadata": {"total": 2},
    }
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode() + b"\n"
    out_raw = sf.scrub_bytes(raw, kind="json").body
    out = json.loads(out_raw)

    def shape(value):
        if isinstance(value, dict):
            return {k: shape(v) for k, v in value.items()}
        if isinstance(value, list):
            return [shape(v) for v in value]
        return type(value).__name__

    assert shape(out) == shape(data)
    first, second = out["data"]
    assert first["title"] == "Optom divan, tel +998 90 000 00 01"
    assert first["user"]["name"] == "Seller 01"
    assert second["user"]["name"] == "Seller 01"  # the same person, the same NN
    assert second["seller_name"] == "Seller 02"
    assert first["contact"] == {"name": "Namuna Mebel MChJ", "phone": "+998900000002"}
    assert second["phone_int"] == 998900000001
    assert first["params"] == [{"key": "price", "value": "1 250 000"}]
    assert out_raw.decode().startswith('{\n  "data": [')  # its own layout
    assert out_raw.endswith(b"\n")


def test_compact_json_with_escapes_stays_compact():
    data = {"user": {"name": "Сардор"}, "city": "Тошкент"}
    raw = json.dumps(data, separators=(",", ":")).encode()
    out = sf.scrub_bytes(raw, kind="json").body.decode()
    city = "\\u0422\\u043e\\u0448\\u043a\\u0435\\u043d\\u0442"  # «Тошкент», escaped
    assert out == '{"user":{"name":"Seller 01"},"city":"' + city + '"}'


def test_escaped_state_json_inside_a_page_is_scrubbed():
    page = (
        '<script>window.__PRERENDERED_STATE__= "{\\"ad\\":{\\"id\\":5,'
        '\\"user\\":{\\"id\\":9,\\"name\\":\\"Rustam Karimov\\",'
        '\\"seller_type\\":null}},\\"shop\\":{\\"seller\\":{\\"name\\":'
        '\\"Namuna Mebel MChJ\\"}}}";'
        "</script>"
    )
    out = sf.scrub_bytes(html(page), kind="html").body.decode()
    assert '\\"name\\":\\"Seller 01\\"' in out
    assert "Rustam" not in out and "Karimov" not in out
    assert '\\"name\\":\\"Namuna Mebel MChJ\\"' in out  # a firm stays
    assert out.count("<script>") == 1 and out.endswith("</script>")


def test_an_entrepreneur_name_becomes_seller_nn_and_a_firm_stays():
    page = (
        "<li>ИП Каримов Р.Р. — +998 90 000 00 41</li>"
        "<li>Aliyev Sardor YaTT</li>"
        "<li>Namuna Mebel MChJ</li>"
    )
    out = sf.scrub_bytes(html(page), kind="html").body.decode()
    assert out == (
        "<li>ИП Seller 01 — +998 90 000 00 01</li>"
        "<li>Seller 02 YaTT</li>"
        "<li>Namuna Mebel MChJ</li>"
    )


def test_scrubbing_is_deterministic():
    page = html("<p>@namuna_bir +998 90 000 00 43, @namuna_ikki +998 90 000 00 44</p>")
    assert sf.scrub_bytes(page, kind="html") == sf.scrub_bytes(page, kind="html")
    result = sf.scrub_bytes(page, kind="html")
    assert result.body == html(
        "<p>@namuna_01 +998 90 000 00 01, @namuna_02 +998 90 000 00 02</p>"
    )
    assert result.counts == {"phones": 2, "handles": 2}


@pytest.mark.parametrize(
    ("key", "body", "expected"),
    [
        ("olx_business", b"<p>Namuna Mebel MChJ</p>", True),
        ("telegram_monitor", b"{}", True),
        ("instagram_discovery", b"{}", True),
        ("directories", "<p>ИП Каримов Р.Р.</p>".encode(), True),
        ("directories", b"<p>YaTT Aliyev Sardor</p>", True),
        ("directories", b"<p>Namuna Mebel MChJ, optom</p>", False),
        ("hh_vacancies", b'{"employer": {"name": "Namuna Import"}}', False),
    ],
)
def test_needs_scrub(key, body, expected):
    assert sf.needs_scrub(key, body) is expected


def test_the_cli_rewrites_in_place(tmp_path, capsys):
    path = tmp_path / "seller.html"
    path.write_bytes(html("<b>+998 90 000 00 45</b>"))
    assert sf.main([str(path)]) == 0
    assert path.read_bytes() == html("<b>+998 90 000 00 01</b>")
    assert "phones=1" in capsys.readouterr().out
