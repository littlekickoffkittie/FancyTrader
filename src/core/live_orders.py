import urllib.request
import urllib.parse
import hmac
import hashlib
import time
import json
import uuid


def _sign_request(api_secret: str, path: str, query: str, body: str, expiry: str) -> str:
    msg = path + query + expiry + body
    return hmac.new(
        api_secret.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()


def _request(method: str, path: str, api_key: str, api_secret: str,
             params: dict = None, body: dict = None) -> dict:
    expiry = str(int(time.time()) + 60)
    query = urllib.parse.urlencode(params) if params else ""
    body_str = json.dumps(body) if body else ""
    sig = _sign_request(api_secret, path, query, body_str, expiry)

    url = f"https://api.phemex.com{path}"
    if query:
        url += f"?{query}"

    req = urllib.request.Request(url, method=method)
    req.add_header("x-phemex-access-token", api_key)
    req.add_header("x-phemex-request-expiry", expiry)
    req.add_header("x-phemex-request-signature", sig)
    req.add_header("Content-Type", "application/json")

    data = body_str.encode() if body_str else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def place_market_order(api_key: str, api_secret: str,
                       symbol: str, side: str,
                       size_usdt: float, leverage: int) -> dict:
    """
    Places a market order on Phemex USDT-M perps.
    side: 'Buy' or 'Sell'
    Returns order response dict.
    """
    # Set leverage first
    _request("PUT", "/g-orders/leverage", api_key, api_secret,
             body={"symbol": symbol, "leverageRr": str(leverage)})

    order_id = str(uuid.uuid4()).replace("-", "")[:20]
    body = {
        "symbol": symbol,
        "clOrdID": order_id,
        "side": side,
        "orderQtyRq": str(round(size_usdt, 2)),
        "ordType": "Market",
        "posSide": "Long" if side == "Buy" else "Short",
        "reduceOnly": False,
        "timeInForce": "ImmediateOrCancel"
    }
    return _request("POST", "/g-orders/create", api_key, api_secret, body=body)


def close_market_order(api_key: str, api_secret: str,
                       symbol: str, side: str, size_usdt: float) -> dict:
    """Close position with market order. side is opposite of entry."""
    order_id = str(uuid.uuid4()).replace("-", "")[:20]
    body = {
        "symbol": symbol,
        "clOrdID": order_id,
        "side": side,
        "orderQtyRq": str(round(size_usdt, 2)),
        "ordType": "Market",
        "reduceOnly": True,
        "timeInForce": "ImmediateOrCancel"
    }
    return _request("POST", "/g-orders/create", api_key, api_secret, body=body)
