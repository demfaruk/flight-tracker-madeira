import requests
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# ── Loaded from GitHub Secrets ──────────────────────────────────────────────
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
GMAIL_USER   = os.environ["GMAIL_USER"]
GMAIL_PASS   = os.environ["GMAIL_PASS"]
TO_EMAIL     = os.environ["TO_EMAIL"]

# ── Settings ─────────────────────────────────────────────────────────────────
ORIGIN_QUERY      = "Amsterdam Schiphol"
DESTINATION_QUERY = "Funchal Madeira"
CURRENCY          = "EUR"
MARKET            = "NL"
LOCALE            = "en-GB"
DURATIONS         = [5, 6, 7]  # nights

HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY,
    "X-RapidAPI-Host": "skyscanner-flights-travel-api.p.rapidapi.com",
}

# ── Look up Skyscanner's internal airport ID ─────────────────────────────────
def get_sky_id(query):
    url = "https://skyscanner-flights-travel-api.p.rapidapi.com/flights/searchAirport"
    r = requests.get(url, headers=HEADERS, params={"query": query, "locale": LOCALE}, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("places", [])
    if not items:
        raise ValueError(f"No results returned for: {query}")
    chosen    = next((i for i in items if i.get("placeType") == "AIRPORT"), items[0])
    sky_id    = chosen["skyId"]
    entity_id = chosen["entityId"]
    print(f"  Found: {chosen.get('name', query)} → skyId={sky_id}, entityId={entity_id}")
    return sky_id, entity_id

# ── Fetch the price calendar for return trips ────────────────────────────────
def fetch_calendar(origin_sky, origin_entity, dest_sky, dest_entity,
                   depart_from, depart_to, return_from, return_to):
    url = "https://skyscanner-flights-travel-api.p.rapidapi.com/flights/getPriceCalendarReturn"
    params = {
        "originSkyId":         origin_sky,
        "destinationSkyId":    dest_sky,
        "originEntityId":      origin_entity,
        "destinationEntityId": dest_entity,
        "currency":            CURRENCY,
        "market":              MARKET,
        "locale":              LOCALE,
        "fromDate":            depart_from,
        "toDate":              depart_to,
        "returnFromDate":      return_from,
        "returnToDate":        return_to,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if not r.ok:
        print(f"  API error {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()

# ── Fetch detailed flight info (airline + times) for one specific date pair ──
def get_flight_details(origin_sky, origin_entity, dest_sky, dest_entity,
                       depart_date, return_date):
    url = "https://skyscanner-flights-travel-api.p.rapidapi.com/flights/searchFlights"
    params = {
        "originSkyId":         origin_sky,
        "destinationSkyId":    dest_sky,
        "originEntityId":      origin_entity,
        "destinationEntityId": dest_entity,
        "date":                depart_date,
        "returnDate":          return_date,
        "adults":              "1",
        "currency":            CURRENCY,
        "market":              MARKET,
        "locale":              LOCALE,
        "cabinClass":          "economy",
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if not r.ok:
        return None
    data = r.json()

    # Navigate to itineraries — try common response structures
    itineraries = (
        data.get("data", {}).get("itineraries") or
        data.get("itineraries") or []
    )

    def parse_time(dt_str):
        if not dt_str:
            return "–"
        try:
            return datetime.strptime(dt_str[:16], "%Y-%m-%dT%H:%M").strftime("%H:%M")
        except ValueError:
            return "–"

    def get_carrier(leg):
        raw = leg.get("carriers", [])
        # carriers can be a list directly, or a dict with "marketing"/"operating" keys
        if isinstance(raw, list):
            carriers = raw
        elif isinstance(raw, dict):
            carriers = raw.get("marketing") or raw.get("operating") or []
        else:
            carriers = []
        if not carriers:
            return "–"
        c = carriers[0]
        return c.get("name") or c.get("alternateId") or "–"

    for itin in itineraries:
        legs = itin.get("legs", [])
        if len(legs) < 2:
            continue
        outbound, inbound = legs[0], legs[1]
        # Direct flights only
        if outbound.get("stopCount", 1) != 0 or inbound.get("stopCount", 1) != 0:
            continue
        return {
            "out_airline": get_carrier(outbound),
            "out_depart":  parse_time(outbound.get("departure")),
            "out_arrive":  parse_time(outbound.get("arrival")),
            "in_airline":  get_carrier(inbound),
            "in_depart":   parse_time(inbound.get("departure")),
            "in_arrive":   parse_time(inbound.get("arrival")),
        }
    return None

# ── Find 10 cheapest trips for a given duration, enriched with flight details ─
def get_cheapest_trips(origin_sky, origin_entity, dest_sky, dest_entity, duration):
    today    = datetime.now()
    end_date = today + timedelta(days=275)  # ~9 months

    data = fetch_calendar(
        origin_sky, origin_entity, dest_sky, dest_entity,
        today.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        (today + timedelta(days=duration)).strftime("%Y-%m-%d"),
        (end_date + timedelta(days=duration)).strftime("%Y-%m-%d"),
    )

    outbound_list = data.get("outboundDates", [])
    inbound_list  = data.get("inboundDates",  [])

    if not outbound_list and not inbound_list:
        print(f"  [{duration}n] Could not parse response. Keys: {list(data.keys())}")
        return []

    # Build lookup: return_date → cheapest inbound price
    inbound_by_date = {}
    for entry in inbound_list:
        date, price = entry.get("date", ""), entry.get("price")
        if date and price is not None:
            p = float(price)
            if date not in inbound_by_date or p < inbound_by_date[date]:
                inbound_by_date[date] = p

    trips = []
    for entry in outbound_list:
        depart_date    = entry.get("date", "")
        outbound_price = entry.get("price")
        if not depart_date or outbound_price is None:
            continue
        try:
            dep_dt        = datetime.strptime(depart_date, "%Y-%m-%d")
            return_date   = (dep_dt + timedelta(days=duration)).strftime("%Y-%m-%d")
            inbound_price = inbound_by_date.get(return_date)
            if inbound_price is None:
                continue
            trips.append({
                "depart": depart_date,
                "return": return_date,
                "price":  float(outbound_price) + inbound_price,
            })
        except (ValueError, TypeError):
            continue

    top10 = sorted(trips, key=lambda x: x["price"])[:10]

    # Enrich each trip with airline + times from searchFlights
    print(f"  Fetching flight details for {len(top10)} trips...")
    for trip in top10:
        details = get_flight_details(
            origin_sky, origin_entity, dest_sky, dest_entity,
            trip["depart"], trip["return"]
        )
        if details:
            trip.update(details)
        else:
            # Fallback if no direct flight detail found
            trip.update({
                "out_airline": "–", "out_depart": "–", "out_arrive": "–",
                "in_airline":  "–", "in_depart":  "–", "in_arrive":  "–",
            })

    return top10

# ── Build HTML email ──────────────────────────────────────────────────────────
def build_email(results):
    today_str = datetime.now().strftime("%d %B %Y")
    colors    = {5: "#2980b9", 6: "#f39c12", 7: "#27ae60"}
    bg        = {5: "#eaf4fb", 6: "#fef9e7", 7: "#eafaf1"}

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:780px;margin:auto;padding:20px;">
    <h2 style="color:#1a1a2e;">AMS &harr; FNC Flight Tracker</h2>
    <p style="color:#666;">Daily report &mdash; {today_str} &nbsp;|&nbsp; Direct flights &nbsp;|&nbsp; Round trip &nbsp;|&nbsp; EUR &nbsp;|&nbsp; Local times</p>
    <hr/>"""

    for duration, trips in results.items():
        html += f"""<h3 style="background:{colors[duration]};color:white;padding:10px;border-radius:6px;margin-top:24px;">
            {duration}-Night Trips &mdash; Top 10 Cheapest</h3>"""

        if not trips:
            html += "<p>No results found for this duration.</p>"
            continue

        html += f"""<table style="width:100%;border-collapse:collapse;font-size:13px;">
        <tr style="background:{colors[duration]};color:white;">
            <th style="padding:8px;text-align:center;">#</th>
            <th style="padding:8px;text-align:left;">Fly out</th>
            <th style="padding:8px;text-align:left;">Fly back</th>
            <th style="padding:8px;text-align:right;">Price</th>
        </tr>"""

        for i, t in enumerate(trips, 1):
            dep_date = datetime.strptime(t["depart"], "%Y-%m-%d").strftime("%a %d %b")
            ret_date = datetime.strptime(t["return"], "%Y-%m-%d").strftime("%a %d %b")
            row_bg   = bg[duration] if i % 2 == 0 else "white"

            html += f"""<tr style="border-bottom:1px solid #ddd;background:{row_bg};">
                <td style="padding:8px;text-align:center;font-weight:bold;">{i}</td>
                <td style="padding:8px;">
                    <strong>{dep_date}</strong><br/>
                    <span style="color:#333;">{t['out_depart']} &rarr; {t['out_arrive']}</span><br/>
                    <span style="color:#888;font-size:11px;">{t['out_airline']}</span>
                </td>
                <td style="padding:8px;">
                    <strong>{ret_date}</strong><br/>
                    <span style="color:#333;">{t['in_depart']} &rarr; {t['in_arrive']}</span><br/>
                    <span style="color:#888;font-size:11px;">{t['in_airline']}</span>
                </td>
                <td style="padding:8px;text-align:right;vertical-align:middle;">
                    <strong style="font-size:15px;">&euro;{t['price']:.0f}</strong>
                </td>
            </tr>"""

        html += "</table>"

    html += """<br/><p style="color:#aaa;font-size:11px;">
        Prices from Skyscanner via RapidAPI. Times are local. Always verify before booking.</p>
    </body></html>"""
    return html

# ── Send email via Gmail ──────────────────────────────────────────────────────
def send_email(html):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"AMS-FNC Flights | {datetime.now().strftime('%d %b %Y')}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, TO_EMAIL, msg.as_string())
    print("Email sent.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Running — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    print("Looking up airport IDs...")
    origin_sky, origin_entity = get_sky_id(ORIGIN_QUERY)
    dest_sky,   dest_entity   = get_sky_id(DESTINATION_QUERY)

    results = {}
    for d in DURATIONS:
        print(f"Searching {d}-night trips...")
        results[d] = get_cheapest_trips(origin_sky, origin_entity, dest_sky, dest_entity, d)
        print(f"  → {len(results[d])} trips enriched")

    if all(len(v) == 0 for v in results.values()):
        print("No flights found. No email sent.")
        return

    send_email(build_email(results))

if __name__ == "__main__":
    main()
