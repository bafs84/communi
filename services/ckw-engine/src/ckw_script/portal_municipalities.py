from __future__ import annotations

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class Municipality:
    name: str
    canton: str = "LU"
    ckw_area: bool = True


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().replace("-", " ").split())


# MVP whitelist. Keep this list editable until an official machine-readable CKW
# supply-area source is wired in.
CKW_LU_MUNICIPALITIES: tuple[Municipality, ...] = tuple(
    Municipality(name)
    for name in (
        "Adligenswil",
        "Aesch",
        "Alberswil",
        "Altbueron",
        "Altishofen",
        "Ballwil",
        "Beromuenster",
        "Buchrain",
        "Bueron",
        "Buttisholz",
        "Dagmersellen",
        "Dierikon",
        "Doppleschwand",
        "Ebikon",
        "Egolzwil",
        "Eich",
        "Emmen",
        "Entlebuch",
        "Eschenbach",
        "Escholzmatt-Marbach",
        "Ettiswil",
        "Fischbach",
        "Fluehli",
        "Geuensee",
        "Gisikon",
        "Greppen",
        "Grossdietwil",
        "Grosswangen",
        "Hasle",
        "Hergiswil bei Willisau",
        "Hildisrieden",
        "Hitzkirch",
        "Hochdorf",
        "Hohenrain",
        "Honau",
        "Horw",
        "Inwil",
        "Knutwil",
        "Kriens",
        "Luthern",
        "Malters",
        "Mauensee",
        "Meggen",
        "Meierskappel",
        "Menznau",
        "Nebikon",
        "Neuenkirch",
        "Nottwil",
        "Oberkirch",
        "Pfaffnau",
        "Rain",
        "Reiden",
        "Rickenbach",
        "Roggliswil",
        "Romoos",
        "Root",
        "Rothenburg",
        "Ruswil",
        "Schenkon",
        "Schlierbach",
        "Schongau",
        "Schuepfheim",
        "Schwarzenberg",
        "Sempach",
        "Sursee",
        "Triengen",
        "Udligenswil",
        "Ufhusen",
        "Vitznau",
        "Wauwil",
        "Weggis",
        "Werthenstein",
        "Wikon",
        "Willisau",
        "Wolhusen",
        "Zell",
    )
)

_MUNICIPALITY_BY_KEY = {normalize_name(m.name): m for m in CKW_LU_MUNICIPALITIES}


def list_municipalities() -> list[dict[str, object]]:
    return [
        {"name": municipality.name, "canton": municipality.canton, "ckw_area": municipality.ckw_area}
        for municipality in sorted(CKW_LU_MUNICIPALITIES, key=lambda item: item.name)
    ]


def validate_ckw_municipality(name: str) -> Municipality:
    key = normalize_name(name)
    municipality = _MUNICIPALITY_BY_KEY.get(key)
    if municipality is None or not municipality.ckw_area:
        raise ValueError(f"{name!r} is not configured as a CKW-LU municipality")
    return municipality
