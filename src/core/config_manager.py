import os
import json
from dotenv import load_dotenv
from supabase import create_client
from src.core.config_schema import BotConfig

load_dotenv()
_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

def save_config(user_id: str, config: BotConfig) -> dict:
    data = {
        "user_id": user_id,
        "mode": config.mode,
        "config": config.to_dict(),
        "is_active": False
    }
    result = _client.table("bot_configs").upsert(data).execute()
    return result.data

def load_config(user_id: str) -> BotConfig:
    result = _client.table("bot_configs")\
        .select("*")\
        .eq("user_id", user_id)\
        .order("updated_at", desc=True)\
        .limit(1)\
        .execute()
    if not result.data:
        return BotConfig()
    return BotConfig.from_dict(result.data[0]["config"])