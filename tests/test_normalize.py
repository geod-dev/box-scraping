from decimal import Decimal

import pytest

from competitor_catalog.core.normalize import (
    build_attribute,
    build_normalized_attributes,
    convert_unit,
    fold,
    parse_decimal,
    split_label_unit,
)
from competitor_catalog.core.urls import normalize_url


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("€ 0,81", Decimal("0.81")),
        ("€ 1 234,56", Decimal("1234.56")),
        ("1 234,5", Decimal("1234.5")),
        ("1.234,56 €", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("1280.0", Decimal("1280.0")),
        ("40,00%", Decimal("40.00")),
        ("(201 g )", Decimal("201")),
        ("Prix sur demande", None),
        (None, None),
    ],
)
def test_parse_decimal(text, expected):
    assert parse_decimal(text) == expected


def test_split_label_unit():
    assert split_label_unit("Largeur (mm)") == ("Largeur", "mm")
    assert split_label_unit("Epaisseur (µ)") == ("Epaisseur", "µ")
    assert split_label_unit("Couleur de base") == ("Couleur de base", None)


def test_convert_unit():
    assert convert_unit(Decimal("50"), "m", "mm") == Decimal("50000")
    assert convert_unit(Decimal("2.5"), "cm", "mm") == Decimal("25.0")
    assert convert_unit(Decimal("120"), "µ", "um") == Decimal("120")
    assert convert_unit(Decimal("1"), "kg", "g") == Decimal("1000")
    assert convert_unit(Decimal("1"), "kg", "mm") is None  # never convert across dimensions
    assert convert_unit(Decimal("1"), None, "mm") is None


def test_fold_is_accent_and_case_insensitive():
    assert fold("Largeur intérieure (mm)") == fold("largeur interieure (MM)")


def test_build_attribute_keeps_original_label_and_value():
    attr = build_attribute("Dimensions", "Longueur (mm)", " 305 ")
    assert attr == {
        "group": "Dimensions",
        "label": "Longueur (mm)",
        "value": "305",
        "key": "longueur_mm",
        "name": "Longueur",
        "unit": "mm",
        "numeric_value": 305,
    }
    assert build_attribute(None, "Couleur", "")["empty"] is True
    assert "numeric_value" not in build_attribute(None, "FEFCO", "FEFCO 0701")


def test_normalized_attributes_mapping_and_units():
    attrs = [
        build_attribute("D", "Largeur interieure (mm)", "215"),
        build_attribute("D", "Longueur des rouleaux (m)", "50.00"),
        build_attribute("D", "Epaisseur (µ)", "120"),
        build_attribute("C", "Matériau", "kraft"),
        build_attribute("C", "Quantité sur palette", "1280.0"),
        build_attribute("C", "Couleur", ""),
    ]
    mapping = {
        "inner_width_mm": ["Largeur intérieure (mm)"],
        "roll_length_mm": ["Longueur des rouleaux (m)"],
        "thickness_um": ["Epaisseur (µ)"],
        "material": ["Matière", "Matériau"],
        "pallet_quantity": ["Quantité sur palette"],
        "color": ["Couleur"],
    }
    assert build_normalized_attributes(attrs, mapping) == {
        "inner_width_mm": 215,
        "roll_length_mm": 50000,
        "thickness_um": 120,
        "material": "kraft",
        "pallet_quantity": 1280,
    }


def test_normalize_url():
    assert normalize_url("/eshop/a/b/?utm_source=x&b=2&a=1#frag", "https://WWW.antalis.fr") == (
        "https://www.antalis.fr/eshop/a/b?a=1&b=2"
    )
    assert normalize_url("https://www.antalis.fr/eshop/x?no=20") == "https://www.antalis.fr/eshop/x?no=20"
