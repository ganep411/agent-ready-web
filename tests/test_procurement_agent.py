import pytest

from app.specifications import shopify_details
from examples.procurement_agent import parse_request


@pytest.mark.parametrize("request_text", [
    "電源を、12Vか24V、2個見積もって",
    "電源を、電圧12 V以上、2個見積もって",
    "電源を、電圧12 V、電圧24 V、2個見積もって",
    "電源を、電圧60 W、2個見積もって",
    "電源を、0個見積もって",
    "電源を、101個見積もって",
])
def test_unsupported_or_ambiguous_requests_are_rejected(request_text):
    with pytest.raises(ValueError):
        parse_request(request_text)


def test_only_explicit_structured_attributes_are_used():
    data = shopify_details({"id": "parent", "description": "12 V, 60 W"},
                           {"id": "variant", "title": "12 V",
                            "selectedOptions": [{"name": "色", "value": "12 V"}]})
    assert data["attributes"] == {"voltage": None, "capacity": None}
    assert data["sku"] is None and data["mpn"] is None


def test_metafield_precedes_option_and_units_are_normalized():
    data = shopify_details({"id": "parent"}, {
        "id": "variant", "sku": "sku",
        "selectedOptions": [{"name": "出力電圧", "value": "24 V"}],
        "procurement": {"type": "json", "value": '{"voltage":"12000 mV","capacity":"2000 mAh"}'},
    })
    assert data["attributes"] == {
        "voltage": {"value": "12", "unit": "V", "source": "variant_metafield"},
        "capacity": {"value": "2", "unit": "Ah", "source": "variant_metafield"},
    }


def test_admin_custom_metafield_is_used_only_when_primary_is_absent():
    variant = {
        "id": "variant",
        "procurement": None,
        "customProcurement": {"type": "json", "value": '{"mpn":"ARW-TEST-PS-12","capacity":"60 W"}'},
    }
    details = shopify_details({"id": "parent"}, variant)
    assert details["mpn"] == "ARW-TEST-PS-12"
    assert details["attributes"]["capacity"] == {"value": "60", "unit": "W", "source": "variant_metafield"}
    variant["procurement"] = {"type": "json", "value": '{"mpn":"PRIMARY"}'}
    details = shopify_details({"id": "parent"}, variant)
    assert details["mpn"] == "PRIMARY"
    assert details["attributes"]["capacity"] is None
