from retailprintguard.common.service_mode import classify_service_mode


def test_service_mode_requires_unique_observed_marker() -> None:
    assert classify_service_mode(order_code=None, table_code="Z-99").mode == "DINE_IN"
    assert classify_service_mode(order_code="9001", table_code=None).mode == "TAKEAWAY"
    assert classify_service_mode(order_code="9001", table_code="Z-99").mode == (
        "UNDETERMINED"
    )
    assert classify_service_mode(order_code=" ", table_code=None).mode == (
        "UNDETERMINED"
    )
