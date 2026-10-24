# VENDORED_FROM dailyJournal@6b1f67f miya/services/people.py
"""Given names and honorifics: what makes a «company name» a person (04 §1.6).

``HONORIFICS`` is MIYA's ``_HONORIFICS`` verbatim. ``FIRST_NAMES`` is new: common
Uzbek, Russian and neighbouring given names in ``name_key`` form (Latin,
lower-case, no apostrophes), with the usual spelling variants (x / kh / h,
yo / e). ``normalise.is_person_shaped`` reads ``FIRST_NAMES`` and, of the
honorifics, only 04 §1.6's «aka»/«opa» (``normalise.PERSON_HONORIFICS``): the
rest name shops as often as people («Ustoz Mebel»). The whole list stays for
stripping an honorific off a person's name, as MIYA does. The resolver's fuzzy
guard reads the same predicate (04 §2.2).

Names that are also ordinary words a shop is called by («nur», «gul», «lola»,
«oy») are left out on purpose: a false «person» renames a company card. A
listed name inside a business name is harmless — «Umid Savdo» carries a
generic token and stays a company.
"""

from __future__ import annotations

# Uzbek/Russian kinship and honorific suffixes carry no identity — "Akmal aka"
# and "Akmal" are the same person.
HONORIFICS = frozenset(
    {
        "aka",
        "uka",
        "opa",
        "singil",
        "amaki",
        "tog'a",
        "toga",
        "xola",
        "amma",
        "bobo",
        "buva",
        "buvi",
        "ustoz",
        "domla",
        "brat",
        "bro",
    }
)

_UZ_MALE = """
    abbos abdulla abdullo abdurahmon abdurashid abdusalom abror adham akbar akmal
    akrom alisher anvar asad asadbek asilbek aziz azizbek azamat bahodir bahrom
    baxtiyor baxrom bekzod bobur bobomurod botir behruz bunyod damir dilmurod
    dilshod diyor diyorbek doniyor dostonbek elbek elyor erkin eldor farhod farxod
    farrux farruh fazliddin furqat g'ayrat gayrat hamid hasan husan husniddin ibrohim
    ilhom ilxom islom ismoil izzat jahongir jaloliddin jamshid jasur javlon javohir
    jonibek kamol kamoliddin karim komil laziz lochin mahmud mansur mardon mirjalol
    mirzo muhammad muhammadali muhammadjon muhriddin murod murodjon nodir nodirbek
    nosir nurbek nurali nuriddin nurislom obid odil oybek olim otabek ozod polat
    pulat qahramon qodir rahim ravshan rasul rashid rustam ruslan sanjar sardor sarvar
    sarvarbek shavkat sherzod shohruh shohrux shuhrat shuxrat sobir sodiq sohib
    sulton sunnat suxrob suhrob tohir temur timur tolib ulugbek umar umid usmon
    valijon xurshid hurshid xusan xasan yusuf zafar zohid zokir ziyod
    """.split()
_UZ_FEMALE = """
    aziza barno charos dildora dilfuza dilnoza dilorom durdona feruza gavhar gulbahor
    gulchehra gulnora gulnoza gulshan gulsanam hilola hulkar iroda kamola kumush
    laylo madina mahliyo malika marjona mavluda mohira muattar munisa muxlisa
    muhlisa nargiza nasiba nigora nilufar nodira oydin rayhona robiya rano sabina
    sadoqat sevara shahlo shahnoza shoira shohsanam sitora umida yulduz zarina
    zebo zilola zuhra zulfiya zulayho
    """.split()
_RU_MALE = """
    aleksandr aleksey alexey anatoliy andrey anton arkadiy artem artyom boris
    vadim valentin valeriy vasiliy viktor vitaliy vladimir vladislav vyacheslav
    gennadiy georgiy grigoriy denis dmitriy evgeniy yevgeniy egor yegor ivan igor
    ilya kirill konstantin leonid maksim maxim mikhail mixail mihail nikita nikolay
    oleg pavel petr pyotr roman sergey sergei stanislav stepan timofey fedor fyodor
    yuriy yuri yaroslav
    """.split()
_RU_FEMALE = """
    alina alla anastasiya anna antonina valentina valeriya vera viktoriya galina
    darya ekaterina yekaterina elena yelena elizaveta zhanna zoya inna irina
    kristina ksenia kseniya larisa lyudmila margarita mariya marina nadezhda natalya
    nataliya nina oksana olga polina svetlana sofiya tamara tatyana tatiana yuliya
    yana
    """.split()
_OTHER = """
    abay aybek ayday asel aygul baurzhan bauyrjan daniyar dauren erlan nurlan
    nursultan saule talgat yerlan
    """.split()

FIRST_NAMES = frozenset(
    name.replace("'", "")
    for name in (*_UZ_MALE, *_UZ_FEMALE, *_RU_MALE, *_RU_FEMALE, *_OTHER)
)
