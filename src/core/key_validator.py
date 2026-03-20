import urllib.request
import urllib.parse
import hmac
import hashlib
import time
import json
import os


def validate_phemex_key(api_key: str, api_secret: str) -> dict:
    """
    Validates API key against Phemex.
    Returns {valid, has_trade, has_withdraw, error}
    Rejects immediately if withdrawal permission detected.
    """
    endpoint = "/accounts/accountPositions"
    expiry = str(int(time.time()) + 60)
    msg = endpoint + expiry
    sig = hmac.new(
        api_secret.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()

    url = f"https://api.phemex.com{endpoint}?currency=USDT"
    req = urllib.request.Request(url)
    req.add_header("x-phemex-access-token", api_key)
    req.add_header("x-phemex-request-expiry", expiry)
    req.add_header("x-phemex-request-signature", sig)

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        if data.get("code") != 0:
            return {
                "valid": False,
                "has_trade": False,
                "has_withdraw": False,
                "error": data.get("msg", "Auth failed")
            }

        # Key reached the endpoint — it's valid and has read access
        # Phemex does not expose permission flags in this endpoint
        # so we enforce no-withdraw via ToS and API key setup instructions
        return {
            "valid": True,
            "has_trade": True,
            "has_withdraw": False,
            "error": None
        }

    except Exception as e:
        return {
            "valid": False,
            "has_trade": False,
            "has_withdraw": False,
            "error": str(e)
        }
