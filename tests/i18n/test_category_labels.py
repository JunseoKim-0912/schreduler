import pytest

from app.i18n import SUPPORTED_LANGUAGES, load_non_compliance_category_labels, non_compliance_category_label
from app.models.enums import NonComplianceCategory


def test_every_category_has_labels_and_no_extras() -> None:
    labels = load_non_compliance_category_labels()

    assert set(labels) == {category.value for category in NonComplianceCategory}
    for code, translations in labels.items():
        assert set(translations) == set(SUPPORTED_LANGUAGES), code
        assert all(text.strip() for text in translations.values()), code


@pytest.mark.parametrize(
    ("language", "expected"), [("ko", "늦잠/기상 실패"), ("en", "Overslept"), (None, "늦잠/기상 실패"), ("ja", "늦잠/기상 실패")]
)
def test_label_by_language_with_korean_fallback(language: str | None, expected: str) -> None:
    assert non_compliance_category_label(NonComplianceCategory.OVERSLEPT, language) == expected
