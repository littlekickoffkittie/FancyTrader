import os
import sys
from dotenv import load_dotenv

# Ensure PYTHONPATH includes the project root
sys.path.append(os.path.expanduser("~/fancybot"))

load_dotenv(os.path.expanduser("~/fancybot/.env"))

def test_crypto():
    from src.core.crypto import encrypt, decrypt
    secret = "test_api_key_123"
    enc = encrypt(secret)
    dec = decrypt(enc)
    assert dec == secret, f"Crypto mismatch: {dec} != {secret}"
    print("Crypto: OK")

def test_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("Supabase env vars missing")
        return False
    
    client = create_client(url, key)
    # Just try a simple select to verify connection
    try:
        # Assuming there is a users table or similar
        result = client.table("users").select("count", count="exact").limit(1).execute()
        print("Supabase connection: OK")
    except Exception as e:
        print(f"Supabase connection failed: {e}")
        return False
    return True

if __name__ == "__main__":
    try:
        test_crypto()
        if test_supabase():
            print("GATE 0 COMPLETE")
        else:
            print("GATE 0 FAILED (Supabase)")
            sys.exit(1)
    except Exception as e:
        print(f"GATE 0 FAILED: {e}")
        sys.exit(1)
