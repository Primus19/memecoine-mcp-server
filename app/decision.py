from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

MODEL_VERSION = "3.1"
COMPONENT_LIMITS = {"regime":15,"liquidity":20,"momentum":13,"volume_quality":12,"tokenomics":10,"safety":15,"news":10,"social":5}

def canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()

def build_recommendation(candidate: dict[str, Any]) -> dict[str, Any]:
    components=candidate.get("component_scores") or {}; normalized={}
    for name,maximum in COMPONENT_LIMITS.items():
        value=float(components.get(name,-1))
        if not 0<=value<=maximum: raise ValueError(f"invalid {name} component score")
        normalized[name]=value
    now=datetime.now(timezone.utc); expiry=now+timedelta(minutes=min(60,max(1,int(candidate.get("expiry_minutes",60)))))
    payload={"ticket_id":str(candidate.get("ticket_id") or uuid.uuid4()),"model_version":MODEL_VERSION,"created_at":now.isoformat(),"expires_at":expiry.isoformat(),"product_id":str(candidate.get("product_id","")).upper(),"regime":candidate.get("regime"),"score":round(sum(normalized.values()),4),"component_scores":normalized,"news_score":normalized["news"],"change_1h_pct":float(candidate.get("change_1h_pct",0)),"change_24h_pct":float(candidate.get("change_24h_pct",0)),"market_cap_usd":float(candidate.get("market_cap_usd",0)),"volume_24h_usd":float(candidate.get("volume_24h_usd",0)),"turnover":float(candidate.get("turnover",-1)),"spread_bps":float(candidate.get("spread_bps",9999)),"slippage_bps":float(candidate.get("slippage_bps",9999)),"identity_verified":candidate.get("identity_verified") is True,"spot_available":candidate.get("spot_available") is True,"no_safety_veto":candidate.get("no_safety_veto") is True,"notional_usdc":float(candidate.get("notional_usdc",0)),"max_loss_usdc":float(candidate.get("max_loss_usdc",9999)),"limit_price":float(candidate.get("limit_price",0)),"stop_price":float(candidate.get("stop_price",0)),"target_price":float(candidate.get("target_price",0)),"thesis":str(candidate.get("thesis",""))[:4000],"invalidation":str(candidate.get("invalidation",""))[:2000],"evidence_urls":sorted({str(x) for x in candidate.get("evidence_urls",[]) if str(x).startswith("https://")})[:20],"source_timestamp":str(candidate.get("source_timestamp",""))}
    payload["recommendation_hash"]=canonical_hash(payload); return payload
