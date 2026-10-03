"""Download the UCI Default of Credit Card Clients dataset (Taiwan, 30,000 rows).

Source: https://archive.ics.uci.edu/dataset/50/default+of+credit+card+clients
The file is an .xls. Saved raw to data/ for reproducibility.
"""
import urllib.request
from pathlib import Path

URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/00350/default%20of%20credit%20card%20clients.xls"
DEST = Path(__file__).resolve().parent.parent / "data" / "default_of_credit_card_clients.xls"

def main():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        print(f"already present: {DEST}")
        return
    print(f"downloading from {URL} ...")
    urllib.request.urlretrieve(URL, DEST)
    print(f"saved {DEST} ({DEST.stat().st_size / 1e6:.1f} MB)")

if __name__ == "__main__":
    main()
