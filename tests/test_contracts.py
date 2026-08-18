from core.contracts import CalcParams, CalcResult, Dimensions


def make_params() -> CalcParams:
    return CalcParams(
        from_city="Moscow",
        to_city="Kazan",
        places=2,
        weight_kg=25.0,
        volume_m3=0.15,
        dims=Dimensions(length_cm=50, width_cm=40, height_cm=30),
    )


def test_calc_params_defaults_are_stable() -> None:
    params = make_params()

    assert params.client_type == "yl"
    assert params.service_type == "warehouse_warehouse"
    assert params.pay_type == "cashless_on_order"
    assert params.extra == {}


def test_calc_params_do_not_share_extra_dict() -> None:
    first = make_params()
    second = make_params()

    first.extra["request_id"] = "test"

    assert second.extra == {}


def test_calc_result_defaults() -> None:
    result = CalcResult(price=1250.5)

    assert result.currency == "RUB"
    assert result.days is None
    assert result.allowances == {}
