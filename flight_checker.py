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

# ── Step 1: Look up Skyscanner's internal airport ID (SkyId) ─────────────────
# Skyscanner uses its own IDs for airports, not standard IATA codes.
# We search by name and grab the first airport result.
def get_sky_id(query):
    url = "https://skyscanner-flights-travel-api.p.rapidapi.com/flights/searchAirport"
    r = requests.get(url, headers=HEADERS, params={"query": query, "locale": LOCALE}, timeout=15)
    r.raise_for_status()
    data = r.json()
    for item in data.get("data", []):
        if item.get("navigation", {}).get("entityType") == "AIRPORT":
            sky_id = item["navigation"]["relevantFlightParams"]["skyId"]
            entity_id = item["navigation"]["relevantFlightParams"]["entityId"]
            print(f"  Found: {item['presentation']['title']} → skyId={sky_id}, entityId={entity_id}")
            return sky_id, entity_id
    raise ValueError(f"No airport found for query: {query}")

# ── Step 2: Fetch the price calendar for return trips ────────────────────────
def fetch_calendar(origin_sky, origin_entity, dest_sky, dest_entity,
                   depart_from, depart_to, return_from, return_to):
    url = "https://skyscanner-flights-travel-api.p.rapidapi.com/flights/getPriceCalendarReturn"
    params = {
        "originSkyId":        origin_sky,
        "destinationSkyId":   dest_sky,
        "originEntityId":     origin_entity,
        "destinationEntityId": dest_entity,
        "currency":           CURRENCY,
        "market":             MARKET,
        "locale":             LOCALE,
        "fromDate":           depart_from,
        "toDate":             depart_to,
        "returnFromDate":     return_from,
        "returnToDate":       return_to,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if not r.ok:
        print(f"  API error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()

# ── Step 3: Extract cheapest 10 trips for a given duration ───────────────────
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

    # Navigate response — try common structures
    days = None
    for path in [
        lambda d: d["data"]["flights"]["days"],
        lambda d: d["data"]["days"],
        lambda d: d["flights"]["days"],
        lambda d: d["days"],
    ]:
        try:
            days = path(data)
            break
        except (KeyError, TypeError):
            continue

    if days is None:
        print(f"  [{duration}n] Could not parse response. Top-level keys: {list(data.keys())}")
        return []

    trips = []
    for entry in days:
        depart_date = entry.get("day", "")
        price       = entry.get("price")
        if not depart_date or price is None:
            continue
        try:
            dep_dt = datetime.strptime(depart_date, "%Y-%m-%d")
            trips.append({
                "depart": depart_date,
                "return": (dep_dt + timedelta(days=duration)).strftime("%Y-%m-%d"),
                "price":  float(price),
            })
        except (ValueError, TypeError):
            continue

    return sorted(trips, key=lambda x: x["price"])[:10]

# ── Build HTML email ──────────────────────────────────────────────────────────
def build_email(results):
    today_str = datetime.now().strftime("%d %B %Y")
    colors    = {5: "#2980b9", 6: "#f39c12", 7: "#27ae60"}
    bg        = {5: "#eaf4fb", 6: "#fef9e7", 7: "#eafaf1"}

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;padding:20px;">
    <h2 style="color:#1a1a2e;">AMS &harr; FNC Flight Tracker</h2>
    <p style="color:#666;">Daily report &mdash; {today_str} &nbsp;|&nbsp; Direct flights &nbsp;|&nbsp; Round trip &nbsp;|&nbsp; EUR</p>
    <hr/>"""

    for duration, trips in results.items():
        html += f"""<h3 style="background:{colors[duration]};color:white;padding:10px;border-radius:6px;margin-top:24px;">
            {duration}-Night Trips &mdash; Top 10 Cheapest</h3>"""

        if not trips:
            html += "<p>No results found for this duration.</p>"
            continue

        html += f"""<table style="width:100%;border-collapse:collapse;background:{bg[duration]};">
        <tr style="background:{colors[duration]};color:white;">
            <th style="padding:8px;text-align:left;">#</th>
            <th style="padding:8px;text-align:left;">Departure</th>
            <th style="padding:8px;text-align:left;">Return</th>
            <th style="padding:8px;text-align:right;">Price</th>
        </tr>"""

        for i, t in enumerate(trips, 1):
            dep = datetime.strptime(t["depart"], "%Y-%m-%d").strftime("%a, %d %b %Y")
            ret = datetime.strptime(t["return"], "%Y-%m-%d").strftime("%a, %d %b %Y")
            html += f"""<tr style="border-bottom:1px solid #ddd;">
                <td style="padding:8px;">{i}</td>
                <td style="padding:8px;">{dep}</td>
                <td style="padding:8px;">{ret}</td>
                <td style="padding:8px;text-align:right;"><strong>&euro;{t['price']:.0f}</strong></td>
            </tr>"""

        html += "</table>"

    html += """<br/><p style="color:#aaa;font-size:11px;">
        Prices from Skyscanner via RapidAPI. Always verify before booking.</p>
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
    origin_sky, origin_entity   = get_sky_id(ORIGIN_QUERY)
    dest_sky,   dest_entity     = get_sky_id(DESTINATION_QUERY)

    results = {}
    for d in DURATIONS:
        print(f"Searching {d}-night trips...")
        results[d] = get_cheapest_trips(origin_sky, origin_entity, dest_sky, dest_entity, d)
        print(f"  → {len(results[d])} results")

    if all(len(v) == 0 for v in results.values()):
        print("No flights found. No email sent.")
        return

    send_email(build_email(results))

if __name__ == "__main__":
    main()
