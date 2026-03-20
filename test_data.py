import requests
import json

url = "https://api.phemex.com/exchange/public/md/v2/kline/last"
params = {"symbol": "BTCUSDT", "resolution": 14400, "limit": 5}
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "application/json",
}
resp = requests.get(url, params=params, headers=headers)
print(f"Status: {resp.status_code}")
try:
    data = resp.json()
    print(f"Rows found: {len(data.get('data', {}).get('rows', []))}")
except:
    print(f"Response text: {resp.text[:200]}")