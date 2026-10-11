import pytest
from pydantic import ValidationError

from companion.domain import Garment
from companion.inputs import input_error, temperature_range


@pytest.mark.parametrize("text", ["67", "50 67", "3 67", "15 5", "nan 15", "5 inf", "один два", "1 2 3"])
def test_invalid_temperature_has_readable_error(text):
    with pytest.raises(ValueError) as result:
        temperature_range(text)
    response = input_error(result.value)
    assert "validation error" not in response and "unpack" not in response
    assert "input_value" not in response


def test_temperature_decimal_comma_and_unicode_minus():
    assert temperature_range("−5,5 15") == (-5.5, 15)


def test_model_validation_does_not_echo_input_or_traceback():
    with pytest.raises(ValidationError) as result:
        Garment(name="Private garment name", minimum=5, maximum=67)
    response = input_error(result.value)
    assert "Верхняя температура" in response and "60" in response
    assert "Private garment name" not in response and "input_value" not in response
