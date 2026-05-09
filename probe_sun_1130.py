import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "src")
from padelbot.client import BackboneClient

with BackboneClient() as c:
    # Narrow query: bookings on Sun 10 May around 11:30 Brussels (= 09:30 UTC)
    out = c.get("/bookings", params={
        "s": {
            "productId": {"$in": [4377, 4383, 4384]},
            "startDate": {"$gte": "2026-05-10T09:00:00.000Z"},
            "endDate":   {"$lte": "2026-05-10T11:00:00.000Z"},
            "status":    {"$ne": 2},
        },
        "limit": 50,
    }).get("data", [])
    print(f"Sun 10 May 11:30 padel bookings: {len(out)}")
    for b in out:
        print(f"  parent={b['productId']} start={b['startDate']} status={b['status']} paidFor={b.get('paidFor')} avail={b.get('availableParticipantCount')}/{b.get('maxParticipants')} curr={b.get('currentParticipantCount')} desc={b.get('description')!r} updated={b.get('updatedAt')}")
