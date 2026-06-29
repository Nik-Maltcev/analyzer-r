"""Tests for Twelve Data response normalization."""

from app.data.fetcher import parse_time_series_response


def test_parse_keyed_batch_response():
    result = parse_time_series_response({
        "AAPL": {
            "meta": {"symbol": "AAPL"},
            "values": [
                {"datetime": "2026-06-26", "close": "201.5", "volume": "100"},
            ],
            "status": "ok",
        },
        "MSFT": {
            "meta": {"symbol": "MSFT"},
            "values": [
                {"datetime": "2026-06-26", "close": "510.2", "volume": "200"},
            ],
            "status": "ok",
        },
    })

    assert set(result["ticker"]) == {"AAPL", "MSFT"}
    assert len(result) == 2
    assert result["close"].sum() == 711.7


def test_parse_single_symbol_response():
    result = parse_time_series_response(
        {
            "meta": {"symbol": "BTC/USD"},
            "values": [
                {"datetime": "2026-06-26", "close": "100000"},
            ],
            "status": "ok",
        },
        "BTC/USD",
    )

    assert result.iloc[0]["ticker"] == "BTC/USD"
    assert result.iloc[0]["volume"] == 0
