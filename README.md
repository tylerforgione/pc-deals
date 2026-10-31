# PC Deals · Montréal

A daily tracker of good-value PC hardware deals in Canada, with a focus on Montréal.
Live site: https://tylerforgione.github.io/pc-deals/

- `data/deals.json` is the database. A scheduled search adds each day's finds through `scripts/merge.py`,
  which scores them, tracks status (NEW, SEEN, PRICE DROP, NOT SEEN, GONE) and keeps price history.
  The commit history of that file is the full history of every listing.
- `data/deals.csv` is the same list (active listings only) as a spreadsheet.
- `index.html` is a static page that reads the JSON: search, filter by category, price and "fits my gear", and sort.

Scoring: mini PCs get a homelab points score (CPU tier, RAM, SSD, SO-DIMM slots, adapter) per dollar, with a $250 soft cap.
Everything else needs to be at least ~17% below the typical Canadian used/refurb price to be listed.
