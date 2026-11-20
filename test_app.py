"""Tests for Charged. The network is stubbed: no test calls a real API.

`fake_fetch` answers each URL with a small captured-shape payload, so the
tests check the decisions (which bike, rain, budget) rather than live data.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner

import app as app_module
from app import App, MissingPlace, PlaceNotFound

# 14:00 UTC on a Friday: the ride starts inside the 14:00 hour.
NOW = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)

SOUTH_KEN = (51.4941, -0.1738)
CAMDEN = (51.5390, -0.1426)


def nominatim(point, name):
    return [{"lat": str(point[0]), "lon": str(point[1]),
             "display_name": f"{name}, London, Greater London, England"}]


def lime_bikes():
    return {"data": {"bikes": [
        # Right next to the start, almost flat: must be skipped.
        {"bike_id": "a", "lat": 51.4942, "lon": -0.1737, "is_reserved": False,
         "is_disabled": False, "vehicle_type_id": "ebike", "current_range_meters": 1500},
        # A few minutes away with plenty of range: the one to pick.
        {"bike_id": "b", "lat": 51.4965, "lon": -0.1745, "is_reserved": False,
         "is_disabled": False, "vehicle_type_id": "ebike", "current_range_meters": 30000},
        # A scooter with lots of range: never offered.
        {"bike_id": "c", "lat": 51.4941, "lon": -0.1738, "is_reserved": False,
         "is_disabled": False, "vehicle_type_id": "scooter", "current_range_meters": 40000},
    ]}}


LIME_TYPES = {"data": {"vehicle_types": [
    {"vehicle_type_id": "ebike", "form_factor": "bicycle", "propulsion_type": "electric_assist"},
    {"vehicle_type_id": "scooter", "form_factor": "scooter", "propulsion_type": "electric"},
]}}

LIME_DISCOVERY = {"data": {"en": {"feeds": [
    {"name": "free_bike_status", "url": "https://data.lime.bike/x/free_bike_status"},
    {"name": "vehicle_types", "url": "https://data.lime.bike/x/vehicle_types"},
]}}}


def dock(name, point, ebikes, empty):
    return {"commonName": name, "lat": point[0], "lon": point[1],
            "additionalProperties": [
                {"key": "Installed", "value": "true"},
                {"key": "Locked", "value": "false"},
                {"key": "NbEBikes", "value": str(ebikes)},
                {"key": "NbEmptyDocks", "value": str(empty)},
            ]}


BIKEPOINT = [
    dock("Exhibition Road, Knightsbridge", (51.4960, -0.1740), 2, 5),
    dock("Parkway, Camden Town", (51.5385, -0.1430), 0, 7),
]


def weather(precip=None, chance=20, wind=10.0, wind_from=0.0):
    times = [f"2026-11-20T{h:02d}:{m:02d}" for h in range(14, 17) for m in (0, 15, 30, 45)]
    precip = precip if precip is not None else [0.0] * len(times)
    hours = [f"2026-11-20T{h:02d}:00" for h in range(14, 17)]
    return {"minutely_15": {"time": times, "precipitation": precip},
            "hourly": {"time": hours, "precipitation_probability": [chance] * 3},
            "current": {"wind_speed_10m": wind, "wind_direction_10m": wind_from}}


def leg(mode, route=""):
    return {"mode": {"name": mode}, "routeOptions": [{"name": route}]}


def journey(minutes=34, fare=290):
    return {"journeys": [
        {"duration": minutes, "fare": {"totalCost": fare},
         "legs": [leg("walking"), leg("tube", "Piccadilly"), leg("tube", "Northern"),
                  leg("walking")]},
        # A bus that is a little slower and much cheaper.
        {"duration": minutes + 6, "fare": {"totalCost": 175},
         "legs": [leg("walking"), leg("bus", "C2"), leg("walking")]},
        # A bus that is far slower: never offered as the cheaper option.
        {"duration": minutes + 30, "fare": {"totalCost": 175},
         "legs": [leg("bus", "88")]},
    ]}


def make_fetch(**overrides):
    payloads = {
        "nominatim": None,
        "lime_discovery": LIME_DISCOVERY,
        "lime_bikes": lime_bikes(),
        "lime_types": LIME_TYPES,
        "bikepoint": BIKEPOINT,
        "journey": journey(),
        "weather": weather(),
        "elevation": {"elevation": [10.0, 40.0]},
    }
    payloads.update(overrides)

    def fake_fetch(url, timeout=6.0):
        if "nominatim" in url:
            if payloads["nominatim"] is not None:
                return json.dumps(payloads["nominatim"]).encode()
            if "Camden" in url:
                return json.dumps(nominatim(CAMDEN, "Camden Market")).encode()
            if "Narnia" in url:
                return b"[]"
            return json.dumps(nominatim(SOUTH_KEN, "South Kensington")).encode()
        key = None
        if "gbfs.json" in url:
            key = "lime_discovery"
        elif "free_bike_status" in url:
            key = "lime_bikes"
        elif "vehicle_types" in url:
            key = "lime_types"
        elif "BikePoint" in url:
            key = "bikepoint"
        elif "Journey" in url:
            key = "journey"
        elif "elevation" in url:
            key = "elevation"
        elif "forecast" in url:
            key = "weather"
        value = payloads[key]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode()

    return fake_fetch


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)


def run(inputs, fetch=None, explicit=False):
    instance = App()
    instance._fetch = fetch or make_fetch()
    return LocalRunner(instance).call(
        "ebike.trip", inputs, granted_secrets={}, now=NOW, request_id="t1",
        explicit=explicit,
    ).result


TRIP = {"origin": "South Kensington", "destination": "Camden Market"}


def test_the_manifest_is_valid():
    assert App().manifest.app_id == "hack.app_33b2"


NO_SANTANDER_EBIKES = [dock("Exhibition Road, Knightsbridge", (51.4960, -0.1740), 0, 5)]


def test_it_picks_a_lime_with_enough_battery_and_skips_the_flat_one():
    data = run(TRIP, make_fetch(bikepoint=NO_SANTANDER_EBIKES)).data
    assert data["verdict"] == "lime"
    assert data["bike_operator"] == "Lime"
    assert data["bike_range_km"] == 30.0
    assert data["low_battery_bikes_skipped"] == 1
    assert data["range_needed_km"] > data["trip_km"]
    assert "Skip the 1 closer one" in data["summary"]


def test_a_cheaper_santander_wins_when_nearly_as_fast():
    data = run(TRIP).data
    assert data["verdict"] == "santander"
    assert data["bike_cost_gbp"] == 3.3


def test_the_climb_raises_the_range_needed():
    flat = run(TRIP, make_fetch(elevation={"elevation": [10.0, 10.0]})).data
    hilly = run(TRIP, make_fetch(elevation={"elevation": [10.0, 110.0]})).data
    assert hilly["range_needed_km"] > flat["range_needed_km"]
    assert hilly["climb_m"] == 100


def test_a_headwind_raises_the_range_needed():
    # The trip heads roughly north, so wind from the north is a headwind.
    calm = run(TRIP, make_fetch(weather=weather(wind=0.0))).data
    windy = run(TRIP, make_fetch(weather=weather(wind=30.0, wind_from=0.0))).data
    assert windy["headwind_kmh"] > 20
    assert windy["range_needed_km"] > calm["range_needed_km"]


def test_all_day_rain_sends_you_on_the_tube():
    data = run(TRIP, make_fetch(weather=weather(precip=[1.2] * 12, chance=90))).data
    assert data["verdict"] == "public_transport"
    assert "rain" in data["summary"].lower()


def test_rain_near_the_end_says_when_it_starts():
    precip = [0.0, 0.0, 0.8, 0.8] + [0.8] * 8   # wet from 14:30
    data = run(TRIP, make_fetch(weather=weather(precip=precip, chance=80))).data
    assert "14:30" in data["rain"]


def test_over_budget_takes_the_bus():
    data = run(dict(TRIP, budget_left_gbp=2.0)).data
    assert data["verdict"] == "public_transport"
    assert "more than" in data["budget_note"]


def test_budget_pace_over_several_days():
    data = run(dict(TRIP, budget_left_gbp=12.0, days_left=4)).data
    assert "a day" in data["budget_note"]


def test_fare_cap_makes_the_tube_free():
    data = run(dict(TRIP, fare_cap_reached=True),
               make_fetch(journey=journey(minutes=30))).data
    assert data["transit_cost_gbp"] == 0.0


def test_lime_down_falls_back_to_santander():
    fetch = make_fetch(lime_discovery=urllib.error.URLError("down"))
    data = run(TRIP, fetch).data
    assert data["bike_operator"] == "Santander Cycles"
    assert "Exhibition Road" in data["bike_pickup"]
    assert "Parkway" in data["parking"]
    assert "Lime" in data["data_note"]


def test_no_bikes_and_no_journey_is_a_clear_error():
    fetch = make_fetch(lime_discovery=urllib.error.URLError("down"),
                       bikepoint=urllib.error.URLError("down"),
                       journey=TimeoutError("slow"))
    with pytest.raises(app_module.NothingToCompare):
        run(TRIP, fetch)


def test_missing_start_asks_for_it():
    with pytest.raises(MissingPlace) as failure:
        run({"origin": "", "destination": "Camden Market"})
    assert "Where are you starting from" in failure.value.user_message


def test_unknown_place_is_named():
    with pytest.raises(PlaceNotFound) as failure:
        run({"origin": "Narnia", "destination": "Camden Market"})
    assert "Narnia" in failure.value.user_message


def test_picked_by_name_answers_the_same():
    assert run(TRIP, explicit=True).data == run(TRIP).data


def test_limeprime_makes_the_lime_cheaper():
    fetch = lambda: make_fetch(bikepoint=NO_SANTANDER_EBIKES)
    normal = run(TRIP, fetch()).data
    prime = run(dict(TRIP, has_lime_prime=True), fetch()).data
    assert prime["bike_cost_gbp"] < normal["bike_cost_gbp"]
    # A 28 minute ride: £1.70 covers 20 minutes, then 8 minutes at 31p.
    assert prime["bike_cost_gbp"] == 4.18


def test_santander_members_pay_a_pound():
    assert run(dict(TRIP, santander_member=True)).data["bike_cost_gbp"] == 1.0


def test_unmentioned_subscriptions_are_offered_as_a_tip():
    data = run(TRIP).data
    assert "LimePrime" in data["subscription_tip"]
    assert "Santander Cycles membership" in data["subscription_tip"]
    assert "If you have a subscription" in data["summary"]


def test_no_tip_once_the_user_says_they_subscribe():
    data = run(dict(TRIP, has_lime_prime=True, santander_member=True)).data
    assert "subscription_tip" not in data


def test_the_route_is_named():
    data = run(TRIP).data
    assert data["transit_route"] == "Piccadilly line, then Northern line"


def test_a_cheaper_bus_that_is_not_much_slower_is_offered():
    data = run(TRIP).data
    assert data["cheaper_transit"] == "bus C2: 40 min, £1.75"


def test_on_a_tight_budget_it_picks_the_cheaper_bus():
    data = run(dict(TRIP, budget_left_gbp=2.0)).data
    assert data["verdict"] == "public_transport"
    assert data["transit_route"] == "bus C2"
    assert data["transit_cost_gbp"] == 1.75
    assert data["summary"].startswith("Take the bus C2")


def test_no_cheaper_option_once_the_fare_cap_is_hit():
    data = run(dict(TRIP, fare_cap_reached=True)).data
    assert "cheaper_transit" not in data


def test_a_feed_that_hangs_is_left_out_not_waited_for(monkeypatch):
    import threading
    import time as real_time
    monkeypatch.setattr(app_module, "FEEDS_DEADLINE", 0.3)
    release = threading.Event()
    base = make_fetch()

    def slow_weather(url, timeout=6.0):
        if "forecast" in url:
            release.wait(5)
        return base(url, timeout)

    started = real_time.monotonic()
    data = run(TRIP, slow_weather).data
    release.set()
    assert real_time.monotonic() - started < 3
    assert "rain" not in data
    assert "No weather forecast" in data["data_note"]


def test_without_live_lime_data_it_says_what_range_to_look_for():
    data = run(TRIP, make_fetch(lime_discovery=urllib.error.URLError("404"))).data
    assert f"at least {data['range_needed_km']:.1f} km of range" in data["lime_advice"]
    assert "LimePrime" in data["lime_advice"]
    assert data["lime_advice"] in data["summary"]


def test_no_lime_advice_when_a_live_lime_was_found():
    data = run(TRIP, make_fetch(bikepoint=NO_SANTANDER_EBIKES)).data
    assert "lime_advice" not in data
