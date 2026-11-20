"""Charged: should I e-bike this London trip, which bike, and is it worth it?

One call does this:

1. Turn the start and destination into coordinates (Nominatim, OpenStreetMap).
2. In parallel, fetch:
   - Lime's public GBFS feed for London: every free e-bike and its battery range.
   - TfL BikePoint: Santander Cycles docks, e-bikes available, empty docks.
   - TfL Journey Planner: door-to-door time and fare by tube, bus and train.
   - Open-Meteo: rain in 15-minute blocks, chance of rain per hour, wind.
   - Open-Meteo elevation: how much the trip climbs.
3. Work out how much battery range the trip really needs (distance, plus a
   margin for hills, headwind and safety), skip bikes that cannot make it,
   and compare the best bike with public transport on time, rain and budget.

Every number returned comes from one of those feeds or from the user's own
message. When a feed fails, the app says so and decides with what it has.
The hill and wind factors are rules of thumb, not measured battery data.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    Result,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

# Nominatim answers 403 without a descriptive User-Agent.
USER_AGENT = "pippa-agent-app-charged/0.1 (https://github.com/Pippa-Labs)"

# ── Where the data comes from ────────────────────────────────────────────────
NOMINATIM = "https://nominatim.openstreetmap.org/search?"
LIME_DISCOVERY = "https://data.lime.bike/api/partners/v2/gbfs/london/gbfs.json"
TFL_BIKEPOINT = "https://api.tfl.gov.uk/BikePoint"
TFL_JOURNEY = "https://api.tfl.gov.uk/Journey/JourneyResults/"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast?"
OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation?"

# Greater London, so "Victoria" means the station, not somewhere in Australia.
LONDON_VIEWBOX = "-0.52,51.70,0.34,51.28"

# ── Prices (pay-as-you-go, no passes or subscriptions) ──────────────────────
# Operators change prices often and do not publish them in an open feed we
# can rely on, so these are typed in by hand. Integer pence, never floats.
# Lime: £1 unlock (Lime's London scheme pages) and 31p a minute (City AM's
# report of Lime's London rate rise). Santander: £3.30 a single e-bike ride of
# up to 30 minutes (TfL's "Using our e-bikes" page).
PRICES_CHECKED = "pay-as-you-go prices from TfL and press reports, checked Nov 2026"
LIME_UNLOCK_PENCE = 100
LIME_PER_MINUTE_PENCE = 31
SANTANDER_EBIKE_RIDE_PENCE = 330  # one ride of up to 30 minutes
# Subscriptions, if the user says they have one.
# LimePrime (London): £6.99 a month, then £1.70 for a ride of up to 20 minutes.
# Lime charges a discounted per-minute rate after that, which it does not
# publish, so we use the standard rate: the estimate can only be too high.
LIMEPRIME_RIDE_PENCE = 170
LIMEPRIME_INCLUDED_MIN = 20
# Santander Cycles monthly or annual members: £1 an e-bike ride (TfL).
SANTANDER_MEMBER_EBIKE_PENCE = 100

# ── Rules of thumb (named so anyone can see and argue with them) ────────────
WALK_KMH = 5.0
RIDE_KMH = 15.0
ROAD_DETOUR = 1.3        # streets are longer than a straight line
WALK_DETOUR = 1.25
MAX_WALK_MIN = 10        # nobody walks further than this to a bike
RANGE_SAFETY = 1.25      # never plan to arrive on an empty battery
HILL_PER_M = 1 / 250     # +0.4% range needed per metre climbed
WIND_PER_KMH = 1 / 60    # +1.7% range needed per km/h of headwind
WET_MM = 0.1             # a 15-minute block with this much rain counts as wet
LIKELY_RAIN_PCT = 60
CLOSE_ENOUGH_MIN = 5     # a bike this much slower is still worth it
WORTH_PAYING_MIN = 10    # over budget pace, a bike must save this much time

# ── Time budget: Pippa stops the app at 20 seconds ──────────────────────────
# Worst case: two map lookups (4 s each) + the 1 s pause + 9 s for every feed
# at once = 18 s. A feed still loading at the deadline is left out.
GEOCODE_TIMEOUT = 4.0
FEEDS_DEADLINE = 9.0


# ── Errors a user may see ────────────────────────────────────────────────────
class MissingPlace(AgentAppError):
    user_message = "Tell me where you're starting from and where you're going in London."
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I couldn't find that place in London."
    retryable = False


class ServiceBusy(AgentAppError):
    user_message = "The map service is busy. Please try again shortly."
    retryable = True


class ServiceUnavailable(AgentAppError):
    user_message = "I couldn't reach the map service just now."
    retryable = True


class NothingToCompare(AgentAppError):
    user_message = (
        "I couldn't get e-bike or public transport information for that trip "
        "just now. Please try again shortly."
    )
    retryable = True


# ── Small maths helpers ──────────────────────────────────────────────────────
def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin(lon2 - lon1) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def compass(degrees: float) -> str:
    points = ["north", "north-east", "east", "south-east",
              "south", "south-west", "west", "north-west"]
    return points[int((degrees + 22.5) // 45) % 8]


def walk_minutes(a, b) -> int:
    return max(1, round(km_between(a, b) * WALK_DETOUR / WALK_KMH * 60))


def pounds(pence: int | None) -> float | None:
    return None if pence is None else round(pence / 100, 2)


def money(pence: int) -> str:
    return f"£{pence // 100}.{pence % 100:02d}"


def utc(text: str) -> datetime:
    """Open-Meteo times come without an offset; we ask for UTC, so add it."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # The one method that touches the network. Tests replace it.
    def _fetch(self, url: str, timeout: float = 6.0) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _json(self, url: str, timeout: float = 6.0):
        return json.loads(self._fetch(url, timeout))

    # ── Entry point ──────────────────────────────────────────────────────────
    def handle(self, call: Call) -> Result:
        if call.outcome_id != "ebike.trip":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        origin_text = (call.inputs.get("origin") or "").strip()
        dest_text = (call.inputs.get("destination") or "").strip()
        if not origin_text or not dest_text:
            error = MissingPlace("origin or destination missing")
            if origin_text and not dest_text:
                error.user_message = "Where in London are you going?"
            elif dest_text and not origin_text:
                error.user_message = (
                    f"Where are you starting from? Tell me a place, street or "
                    f"station, and I'll check the trip to {dest_text}."
                )
            raise error

        now: datetime = call.context.now
        origin = self._geocode(origin_text)
        time.sleep(1.0)  # Nominatim's usage policy: one request per second.
        dest = self._geocode(dest_text)
        a, b = origin["point"], dest["point"]

        straight_km = km_between(a, b)
        trip_km = straight_km * ROAD_DETOUR
        ride_min = max(1, round(trip_km / RIDE_KMH * 60))

        feeds = self._gather(a, b)
        notes: list[str] = []

        # How much range the trip really needs.
        climb = feeds["elevation"]
        headwind = None
        if feeds["weather"] is not None:
            w = feeds["weather"]
            if w.get("wind_kmh") is not None and w.get("wind_from") is not None:
                headwind = w["wind_kmh"] * math.cos(
                    math.radians(w["wind_from"] - bearing(a, b)))
        needed_km = trip_km * RANGE_SAFETY
        needed_km *= 1 + max(0.0, climb or 0.0) * HILL_PER_M
        needed_km *= 1 + max(0.0, headwind or 0.0) * WIND_PER_KMH

        options = []
        lime_prime = bool(call.inputs.get("has_lime_prime"))
        santander_member = bool(call.inputs.get("santander_member"))
        lime = self._best_lime(feeds["lime"], a, b, needed_km, ride_min, notes,
                               lime_prime)
        if lime:
            options.append(lime)
        santander = self._best_santander(feeds["bikepoint"], a, b, ride_min, notes,
                                         santander_member)
        if santander:
            options.append(santander)
        bike = None
        if options:
            options.sort(key=lambda o: o["minutes"])
            bike = options[0]
            # A cheaper bike that is barely slower wins.
            for other in options[1:]:
                if (other["minutes"] - bike["minutes"] <= CLOSE_ENOUGH_MIN
                        and other["pence"] < bike["pence"]):
                    bike = other

        transit = feeds["journey"]
        cap = bool(call.inputs.get("fare_cap_reached"))
        if transit is not None and cap:
            transit = dict(transit, pence=0, cheaper=None)

        # On a budget, use the cheaper route when the fastest one doesn't fit.
        budget_gbp = call.inputs.get("budget_left_gbp")
        if transit is not None and transit.get("cheaper") and budget_gbp is not None:
            days = call.inputs.get("days_left")
            days = days if isinstance(days, int) and days > 0 else 1
            daily = round(float(budget_gbp) * 100) // days
            if transit["pence"] is not None and transit["pence"] > daily:
                cheaper = transit["cheaper"]
                transit = dict(cheaper, cheaper=None)
                notes.append("Using the cheaper public transport route to fit your budget.")

        if bike is None and transit is None:
            raise NothingToCompare("no bike option and no journey plan")

        rain = None
        if feeds["weather"] is not None:
            start = now + timedelta(minutes=(bike or {}).get("walk_to", 0))
            rain = self._rain_on_ride(start, ride_min, feeds["weather"])
        else:
            notes.append("No weather forecast was available.")

        verdict, reasons, budget_note = self._decide(
            bike, transit, rain, cap,
            call.inputs.get("budget_left_gbp"), call.inputs.get("days_left"),
        )

        data = {
            "verdict": verdict,
            "summary": "",
            "origin": origin["name"],
            "destination": dest["name"],
            "trip_km": round(trip_km, 1),
            "data_note": "",
        }
        if bike:
            data["bike_operator"] = bike["operator"]
            data["bike_pickup"] = bike["pickup"]
            data["bike_minutes"] = bike["minutes"]
            data["bike_cost_gbp"] = pounds(bike["pence"])
            data["parking"] = bike["parking"]
            if bike["operator"] == "Lime":
                data["bike_range_km"] = round(bike["range_km"], 1)
                data["low_battery_bikes_skipped"] = bike["skipped"]
        data["range_needed_km"] = round(needed_km, 1)
        if transit is not None:
            data["transit_minutes"] = transit["minutes"]
            data["transit_route"] = transit.get("route") or "public transport"
            if transit.get("cheaper"):
                c = transit["cheaper"]
                data["cheaper_transit"] = (f"{c['route']}: {c['minutes']} min, "
                                           f"{money(c['pence'])}")
            if transit.get("pence") is not None:
                data["transit_cost_gbp"] = pounds(transit["pence"])
        else:
            notes.append("TfL's Journey Planner did not answer, so public "
                         "transport could not be compared.")
        if climb is not None:
            data["climb_m"] = round(climb)
        if headwind is not None:
            data["headwind_kmh"] = round(headwind)
        if rain:
            data["rain"] = rain["text"]
        if budget_note:
            data["budget_note"] = budget_note
        if lime is None:
            data["lime_advice"] = self._lime_advice(needed_km, ride_min, lime_prime)
        tip = self._subscription_tip(lime, santander)
        if tip:
            data["subscription_tip"] = tip

        data["summary"] = self._summary(verdict, bike, transit, rain, reasons)
        if data.get("lime_advice"):
            data["summary"] += " " + data["lime_advice"]
        if tip:
            data["summary"] += " " + tip
        notes.append(
            f"Hill and wind adjustments are rules of thumb. E-bike prices are "
            f"typed in from the operators' apps ({PRICES_CHECKED}). Sources: "
            f"Lime GBFS, TfL, Open-Meteo, OpenStreetMap."
        )
        data["data_note"] = " ".join(notes)
        return Result(data=data, narrative=data["summary"])

    # ── Fetching ─────────────────────────────────────────────────────────────
    def _geocode(self, text: str) -> dict:
        query = urlencode({
            "q": text, "format": "jsonv2", "limit": 1, "countrycodes": "gb",
            "viewbox": LONDON_VIEWBOX, "bounded": 1,
        })
        try:
            results = self._json(NOMINATIM + query, timeout=GEOCODE_TIMEOUT)
        except urllib.error.HTTPError as exc:   # before URLError: it is one
            if exc.code == 429:
                raise ServiceBusy(f"nominatim rate limited: {exc.code}")
            raise ServiceUnavailable(f"nominatim returned {exc.code}")
        except TimeoutError as exc:
            raise ServiceUnavailable(f"nominatim timed out: {exc}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ServiceUnavailable(f"nominatim failed: {exc}")
        if not results:
            error = PlaceNotFound(f"nominatim found nothing for {text!r}")
            error.user_message = f"I couldn't find {text} in London."
            raise error
        top = results[0]
        name = ", ".join(part.strip() for part in top["display_name"].split(",")[:2])
        return {"point": (float(top["lat"]), float(top["lon"])), "name": name}

    def _gather(self, a, b) -> dict:
        """Fetch every feed at once. A feed that fails comes back as None."""
        jobs = {
            "lime": lambda: self._lime(),
            "bikepoint": lambda: self._json(TFL_BIKEPOINT, timeout=FEEDS_DEADLINE),
            "journey": lambda: self._journey(a, b),
            "weather": lambda: self._weather(a),
            "elevation": lambda: self._elevation(a, b),
        }
        out = {}
        pool = ThreadPoolExecutor(max_workers=len(jobs))
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        wait(futures.values(), timeout=FEEDS_DEADLINE)
        for name, future in futures.items():
            if not future.done():
                out[name] = None    # too slow: answer without it
                continue
            try:
                out[name] = future.result()
            except Exception:       # one feed failing must not sink the answer
                out[name] = None
        # Don't wait for stragglers: the answer must go out inside 20 seconds.
        pool.shutdown(wait=False, cancel_futures=True)
        return out

    def _lime(self) -> dict:
        discovery = self._json(LIME_DISCOVERY, timeout=3.0)
        feeds = {}
        for language in (discovery.get("data") or {}).values():
            for feed in language.get("feeds", []):
                feeds[feed.get("name")] = feed.get("url")
        if not feeds.get("free_bike_status"):
            raise ValueError("no free_bike_status feed")
        # Only follow links on Lime's own host.
        for name, url in list(feeds.items()):
            if not str(url).startswith("https://data.lime.bike/"):
                feeds.pop(name)
        bikes = self._json(feeds["free_bike_status"], timeout=5.0)
        result = {"bikes": (bikes.get("data") or {}).get("bikes") or [],
                  "types": {}, "stations": []}
        if feeds.get("vehicle_types"):
            try:
                vt = self._json(feeds["vehicle_types"], timeout=2.0)
                for t in (vt.get("data") or {}).get("vehicle_types") or []:
                    result["types"][t.get("vehicle_type_id")] = t
            except Exception:
                pass
        if feeds.get("station_information"):
            try:
                st = self._json(feeds["station_information"], timeout=2.0)
                result["stations"] = (st.get("data") or {}).get("stations") or []
            except Exception:
                pass
        return result

    def _journey(self, a, b) -> dict | None:
        path = f"{a[0]:.5f},{a[1]:.5f}/to/{b[0]:.5f},{b[1]:.5f}"
        query = urlencode({
            "mode": "tube,bus,overground,elizabeth-line,dlr,national-rail,walking",
            "timeIs": "Departing",
        })
        plan = self._json(TFL_JOURNEY + path + "?" + query, timeout=FEEDS_DEADLINE)
        journeys = [j for j in plan.get("journeys") or [] if j.get("duration")]
        if not journeys:
            return None
        options = [self._journey_option(j) for j in journeys]
        best = min(options, key=lambda o: o["minutes"])
        # A cheaper route that is not much slower (often a bus instead of the tube).
        cheaper = [o for o in options
                   if o["pence"] is not None and best["pence"] is not None
                   and o["pence"] < best["pence"]
                   and o["minutes"] - best["minutes"] <= WORTH_PAYING_MIN]
        best["cheaper"] = min(cheaper, key=lambda o: (o["pence"], o["minutes"])) if cheaper else None
        return best

    @staticmethod
    def _journey_option(journey: dict) -> dict:
        """Minutes, fare and a readable route such as 'bus 14, then Northern line'."""
        names = {"tube": "{} line", "bus": "bus {}", "overground": "{} (Overground)",
                 "elizabeth-line": "Elizabeth line", "dlr": "DLR",
                 "national-rail": "train", "river-bus": "river bus {}"}
        steps, modes = [], []
        for leg in journey.get("legs") or []:
            mode = ((leg.get("mode") or {}).get("name") or "").lower()
            if not mode or mode == "walking":
                continue
            route = ((leg.get("routeOptions") or [{}])[0] or {}).get("name") or ""
            label = names.get(mode, mode + " {}").format(route).strip()
            if not steps or steps[-1] != label:
                steps.append(label)
            modes.append(mode)
        fare = (journey.get("fare") or {}).get("totalCost")
        if not modes:
            kind = "walk"
        elif all(m == "bus" for m in modes):
            kind = "bus"
        elif all(m == "tube" for m in modes):
            kind = "tube"
        else:
            kind = "public transport"
        return {
            "minutes": int(journey["duration"]),
            "pence": int(fare) if isinstance(fare, (int, float)) else None,
            "route": ", then ".join(steps) if steps else "walk",
            "kind": kind,
        }

    def _weather(self, a) -> dict:
        query = urlencode({
            "latitude": f"{a[0]:.4f}", "longitude": f"{a[1]:.4f}",
            "minutely_15": "precipitation",
            "hourly": "precipitation_probability",
            "current": "wind_speed_10m,wind_direction_10m",
            "timezone": "UTC", "forecast_days": 1,
        })
        raw = self._json(OPEN_METEO + query, timeout=5.0)
        m15 = raw.get("minutely_15") or {}
        hourly = raw.get("hourly") or {}
        current = raw.get("current") or {}
        return {
            "slots": [(utc(t), mm or 0.0) for t, mm in
                      zip(m15.get("time") or [], m15.get("precipitation") or [])],
            "hourly": {utc(t): p for t, p in
                       zip(hourly.get("time") or [],
                           hourly.get("precipitation_probability") or [])},
            "wind_kmh": current.get("wind_speed_10m"),
            "wind_from": current.get("wind_direction_10m"),
        }

    def _elevation(self, a, b) -> float | None:
        query = urlencode({"latitude": f"{a[0]:.4f},{b[0]:.4f}",
                           "longitude": f"{a[1]:.4f},{b[1]:.4f}"})
        heights = self._json(OPEN_METEO_ELEVATION + query, timeout=5.0).get("elevation")
        if not heights or len(heights) < 2:
            return None
        return float(heights[1]) - float(heights[0])

    # ── Choosing a bike ──────────────────────────────────────────────────────
    def _best_lime(self, feed, a, b, needed_km, ride_min, notes,
                   lime_prime=False) -> dict | None:
        if feed is None:
            notes.append("Lime doesn't publish live bike data for London, so Lime "
                         "bikes and their battery can't be checked here.")
            return None
        candidates, skipped = [], 0
        for bike in feed["bikes"]:
            if bike.get("is_disabled") or bike.get("is_reserved"):
                continue
            vtype = feed["types"].get(bike.get("vehicle_type_id")) or {}
            if vtype and vtype.get("form_factor") not in (None, "bicycle"):
                continue  # scooters
            try:
                point = (float(bike["lat"]), float(bike["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            walk = walk_minutes(a, point)
            if walk > MAX_WALK_MIN:
                continue
            range_m = bike.get("current_range_meters")
            if range_m is None:
                continue
            if range_m / 1000 < needed_km:
                skipped += 1
                continue
            candidates.append((walk, -range_m, point, range_m / 1000))
        if not candidates:
            if skipped:
                notes.append(f"{skipped} Lime e-bike(s) nearby had too little battery.")
            return None
        candidates.sort()
        walk, _, point, range_km = candidates[0]
        # Only count skipped bikes that were closer than the one picked.
        closer_skipped = 0
        for bike in feed["bikes"]:
            try:
                p = (float(bike["lat"]), float(bike["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            r = bike.get("current_range_meters")
            if r is not None and r / 1000 < needed_km and walk_minutes(a, p) < walk:
                closer_skipped += 1

        parking, walk_from = self._lime_parking(feed["stations"], b)
        extra = max(0, ride_min - LIMEPRIME_INCLUDED_MIN)
        prime_pence = LIMEPRIME_RIDE_PENCE + LIME_PER_MINUTE_PENCE * extra
        payg_pence = LIME_UNLOCK_PENCE + LIME_PER_MINUTE_PENCE * ride_min
        pence = prime_pence if lime_prime else payg_pence
        distance_m = round(km_between(a, point) * 1000 / 10) * 10
        return {
            "operator": "Lime",
            "pickup": (f"{walk} min walk, about {distance_m} m "
                       f"{compass(bearing(a, point))} of the start"),
            "walk_to": walk,
            "minutes": walk + ride_min + walk_from,
            "pence": pence,
            "range_km": range_km,
            "skipped": closer_skipped,
            "subscription_pence": None if lime_prime else prime_pence,
            "subscription_name": "LimePrime",
            "parking": parking,
        }

    def _lime_parking(self, stations, b) -> tuple[str, int]:
        best = None
        for s in stations:
            try:
                p = (float(s["lat"]), float(s["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            d = km_between(b, p)
            if best is None or d < best[0]:
                best = (d, s.get("name") or "a Lime parking bay")
        if best is None:
            return ("Lime's feed doesn't list parking bays here, so check the "
                    "Lime app for where you can end the ride.", 0)
        walk = max(1, round(best[0] * WALK_DETOUR / WALK_KMH * 60))
        return (f"Lime bay: {best[1]}, {walk} min walk from the destination", walk)

    def _best_santander(self, docks, a, b, ride_min, notes,
                        member=False) -> dict | None:
        if docks is None:
            notes.append("TfL's Santander Cycles feed did not answer.")
            return None
        pickup = dropoff = None
        for dock in docks:
            props = {p.get("key"): p.get("value")
                     for p in dock.get("additionalProperties") or []}
            if props.get("Installed") == "false" or props.get("Locked") == "true":
                continue
            try:
                point = (float(dock["lat"]), float(dock["lon"]))
                ebikes = int(props.get("NbEBikes") or 0)
                spaces = int(props.get("NbEmptyDocks") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            name = dock.get("commonName") or "a docking station"
            to_start, to_end = walk_minutes(a, point), walk_minutes(b, point)
            if ebikes > 0 and to_start <= MAX_WALK_MIN:
                if pickup is None or to_start < pickup[0]:
                    pickup = (to_start, name, ebikes)
            if spaces > 0 and to_end <= MAX_WALK_MIN:
                if dropoff is None or to_end < dropoff[0]:
                    dropoff = (to_end, name, spaces)
        if pickup is None or dropoff is None:
            if pickup is None:
                notes.append("No Santander e-bike within a 10 minute walk of the start.")
            else:
                notes.append("No empty Santander dock within a 10 minute walk "
                             "of the destination.")
            return None
        return {
            "operator": "Santander Cycles",
            "pickup": (f"{pickup[1]} dock, {pickup[0]} min walk "
                       f"({pickup[2]} e-bike{'s' if pickup[2] != 1 else ''} there now)"),
            "walk_to": pickup[0],
            "minutes": pickup[0] + ride_min + dropoff[0],
            "pence": SANTANDER_MEMBER_EBIKE_PENCE if member else SANTANDER_EBIKE_RIDE_PENCE,
            "range_km": None,
            "skipped": 0,
            "subscription_pence": None if member else SANTANDER_MEMBER_EBIKE_PENCE,
            "subscription_name": "a Santander Cycles membership",
            "parking": (f"{dropoff[1]} dock, {dropoff[0]} min walk from the "
                        f"destination ({dropoff[2]} empty dock"
                        f"{'s' if dropoff[2] != 1 else ''} now)"),
        }

    @staticmethod
    def _lime_advice(needed_km: float, ride_min: int, lime_prime: bool) -> str:
        """No live Lime data: still say how much range a Lime needs, and its cost."""
        payg = LIME_UNLOCK_PENCE + LIME_PER_MINUTE_PENCE * ride_min
        prime = (LIMEPRIME_RIDE_PENCE
                 + LIME_PER_MINUTE_PENCE * max(0, ride_min - LIMEPRIME_INCLUDED_MIN))
        cost = money(prime) if lime_prime else (
            f"{money(payg)} ({money(prime)} with LimePrime)")
        return (f"If you'd rather use Lime, pick a bike showing at least "
                f"{needed_km:.1f} km of range in the Lime app; the ride would cost "
                f"about {cost}.")

    @staticmethod
    def _subscription_tip(*options) -> str | None:
        """Show what a subscription the user didn't mention would save."""
        tips = []
        for o in options:
            if o and o.get("subscription_pence") is not None and o["subscription_pence"] < o["pence"]:
                tips.append(f"with {o['subscription_name']} the {o['operator']} ride would be "
                            f"{money(o['subscription_pence'])} instead of {money(o['pence'])}")
        if not tips:
            return None
        return ("If you have a subscription, tell me: " + "; ".join(tips) + ".")

    # ── Rain ─────────────────────────────────────────────────────────────────
    def _rain_on_ride(self, start: datetime, ride_min: int, weather: dict) -> dict:
        end = start + timedelta(minutes=ride_min)
        wet_min, first_wet = 0.0, None
        for slot_start, mm in weather["slots"]:
            slot_end = slot_start + timedelta(minutes=15)
            overlap = (min(slot_end, end) - max(slot_start, start)).total_seconds() / 60
            if overlap > 0 and mm >= WET_MM:
                wet_min += overlap
                first_wet = first_wet or max(slot_start, start)
        chance = weather["hourly"].get(start.replace(minute=0, second=0, microsecond=0))
        share = wet_min / ride_min if ride_min else 0.0

        # When does it dry up, if it is wet now?
        clears = None
        if share > 0:
            for slot_start, mm in weather["slots"]:
                if slot_start >= start and mm < WET_MM:
                    later = all(m < WET_MM for s, m in weather["slots"]
                                if slot_start <= s < slot_start + timedelta(minutes=ride_min))
                    if later:
                        clears = slot_start
                        break

        clock = lambda t: t.strftime("%H:%M")
        if share == 0:
            text, kind = "Dry the whole way.", "dry"
        elif share >= 0.9:
            text, kind = "Rain for the whole ride.", "wet"
        elif first_wet and first_wet > start:
            text, kind = (f"Dry until about {clock(first_wet)}, then rain near "
                          f"the end. Go now to beat most of it."), "late"
        else:
            text, kind = "Rain at the start of the ride.", "early"
        if share > 0 and chance is not None and chance < LIKELY_RAIN_PCT and kind != "wet":
            text, kind = "It might rain on the way, so bring a jacket.", "maybe"
        if share > 0 and clears and (clears - start) <= timedelta(minutes=30):
            text += f" It looks dry from {clock(clears)} if you can wait."
        if chance is not None and share > 0:
            text += f" ({chance}% chance of rain this hour.)"
        return {"text": text, "kind": kind, "share": share, "chance": chance}

    # ── The decision ─────────────────────────────────────────────────────────
    def _decide(self, bike, transit, rain, cap, budget_gbp, days_left):
        reasons: list[str] = []
        budget_note = None
        if bike is None:
            reasons.append("there's no e-bike nearby that can make the trip")
            return "public_transport", reasons, None
        verdict = "lime" if bike["operator"] == "Lime" else "santander"
        if transit is None:
            return verdict, reasons, None

        saved = transit["minutes"] - bike["minutes"]

        if budget_gbp is not None:
            budget_pence = round(float(budget_gbp) * 100)
            days = days_left if isinstance(days_left, int) and days_left > 0 else 1
            daily = budget_pence // days
            if bike["pence"] > budget_pence:
                budget_note = (f"The e-bike ({money(bike['pence'])}) is more than "
                               f"the {money(max(budget_pence, 0))} you have left.")
                reasons.append("it's over your budget")
                return "public_transport", reasons, budget_note
            if bike["pence"] > daily and days > 1:
                budget_note = (f"The e-bike ({money(bike['pence'])}) is more than "
                               f"your {money(daily)} a day for the next {days} days.")
                if saved < WORTH_PAYING_MIN:
                    reasons.append("it would eat into your budget for a small time saving")
                    return "public_transport", reasons, budget_note
            else:
                budget_note = (f"The e-bike ({money(bike['pence'])}) fits your "
                               f"{money(daily)} a day.")

        if rain and rain["kind"] == "wet" and (rain["chance"] or 100) >= LIKELY_RAIN_PCT:
            reasons.append("it's going to rain for the whole ride")
            return "public_transport", reasons, budget_note
        if cap and saved < WORTH_PAYING_MIN:
            reasons.append("you've hit your fare cap, so the tube and bus are free")
            return "public_transport", reasons, budget_note
        if saved >= -CLOSE_ENOUGH_MIN:
            return verdict, reasons, budget_note
        reasons.append(f"public transport is {-saved} min faster")
        return "public_transport", reasons, budget_note

    def _summary(self, verdict, bike, transit, rain, reasons) -> str:
        parts = []
        if verdict == "public_transport":
            why = f" because {reasons[0]}" if reasons else ""
            if transit:
                fare = (f", {money(transit['pence'])}" if transit.get("pence") is not None
                        else "")
                kind, route = transit.get("kind", "public transport"), transit.get("route", "")
                how = f"the {route}" if route.startswith(kind) else f"the {kind} ({route})"
                parts.append(f"Take {how}{why}: about {transit['minutes']} min door "
                             f"to door{fare}.")
                if transit.get("cheaper"):
                    c = transit["cheaper"]
                    parts.append(f"Cheaper: {c['route']}, {c['minutes']} min for "
                                 f"{money(c['pence'])}.")
            else:
                parts.append(f"Take public transport{why}.")
            if bike:
                parts.append(f"The best e-bike option would be {bike['operator']} at "
                             f"about {bike['minutes']} min, {money(bike['pence'])}.")
        else:
            if bike["operator"] == "Lime":
                lead = (f"Take the Lime e-bike that's a {bike['pickup']}. It has "
                        f"about {bike['range_km']:.0f} km of battery.")
                if bike["skipped"]:
                    lead += (f" Skip the {bike['skipped']} closer one"
                             f"{'s' if bike['skipped'] != 1 else ''} with too little battery.")
            else:
                lead = f"Take a Santander e-bike from the {bike['pickup']}."
            parts.append(lead)
            compare = f"About {bike['minutes']} min door to door, {money(bike['pence'])}"
            if transit:
                compare += (f", against {transit['minutes']} min by "
                            f"{transit.get('route', 'public transport')}")
                if transit.get("pence") is not None:
                    compare += f" ({money(transit['pence'])})"
            parts.append(compare + ".")
        if rain:
            parts.append(rain["text"])
        return " ".join(parts)
