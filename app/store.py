from __future__ import annotations
import hashlib,json,sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography.fernet import Fernet

def utcnow(): return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self,data_dir,encryption_key):
        root=Path(data_dir); root.mkdir(parents=True,exist_ok=True); self.cipher=Fernet(encryption_key.encode())
        self.db=sqlite3.connect(root/"coinbase_mcp.sqlite3",check_same_thread=False); self.db.row_factory=sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY,value BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,kind TEXT NOT NULL,ticket_id TEXT,payload TEXT NOT NULL,digest TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS recommendations (ticket_id TEXT PRIMARY KEY,recommendation_hash TEXT UNIQUE NOT NULL,model_version TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,product_id TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,order_id TEXT,net_return REAL,realized_pnl REAL,closed_at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS positions (ticket_id TEXT PRIMARY KEY,product_id TEXT NOT NULL,order_id TEXT,status TEXT NOT NULL,entry_notional REAL NOT NULL,entry_price REAL NOT NULL,opened_at TEXT NOT NULL,updated_at TEXT NOT NULL,closed_at TEXT,realized_pnl REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS capital_flows (seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,kind TEXT NOT NULL,amount REAL NOT NULL,balance_after REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS reviews (review_key TEXT PRIMARY KEY,at TEXT NOT NULL,trigger TEXT NOT NULL,payload TEXT NOT NULL)"); self.db.commit()

    def save_credentials(self,key_name,private_key):
        for n,v in (("key_name",key_name),("private_key",private_key)):
            self.db.execute("INSERT INTO secrets(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(n,self.cipher.encrypt(v.encode())))
        self.db.commit(); self.event("CREDENTIALS_REPLACED",{"key_fingerprint":hashlib.sha256(key_name.encode()).hexdigest()[:12]})
    def credentials(self):
        rows=dict(self.db.execute("SELECT name,value FROM secrets").fetchall())
        if "key_name" not in rows or "private_key" not in rows: raise RuntimeError("Coinbase credentials not configured")
        return self.cipher.decrypt(rows["key_name"]).decode(),self.cipher.decrypt(rows["private_key"]).decode()
    def setting(self,name,default=None):
        row=self.db.execute("SELECT value FROM settings WHERE name=?",(name,)).fetchone(); return str(row[0]) if row else default
    def set_setting(self,name,value):
        self.db.execute("INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(name,str(value))); self.db.commit()
    def initialize_baseline(self,total):
        old=self.setting("pilot_baseline_usdc")
        if old is not None:return float(old)
        if not 5<=total<=30: raise RuntimeError("Initial dedicated USDC balance must be between $5 and $30")
        today=datetime.now(timezone.utc).date()
        for n,v in (("pilot_baseline_usdc",total),("realized_pnl_usdc",0),("net_external_flows_usdc",0),("peak_equity_usdc",total),("daily_start_equity_usdc",total),("daily_start_date",today),("consecutive_losses",0)): self.set_setting(n,v)
        self.event("PILOT_BASELINE_INITIALIZED",{"usdc":total}); return total
    def permitted_capital(self):
        # Only the immutable pilot baseline and realized trading P&L compound.
        # Deposits are audited but never silently expand the trading mandate.
        return max(0,sum(float(self.setting(k,"0") or 0) for k in ("pilot_baseline_usdc","realized_pnl_usdc")))
    def reconciled_equity(self,open_trade_pnl=0.0):
        """Return risk equity from authorized capital plus fill-derived P&L.

        Coinbase account balances and fill history are separate API snapshots.
        Immediately after an exit fill, sale proceeds can be absent from the
        balance snapshot even though the fills already show no remaining asset.
        Risk controls must not treat that temporary settlement gap as a loss.
        """
        return self.permitted_capital()+float(open_trade_pnl)
    def add_realized_pnl(self,pnl):
        self.set_setting("realized_pnl_usdc",float(self.setting("realized_pnl_usdc","0") or 0)+pnl)
    def sync_external_flow(self,actual):
        if self.open_position(): return None
        grace_until=self.setting("settlement_grace_until")
        if grace_until:
            try:
                if datetime.now(timezone.utc)<datetime.fromisoformat(grace_until.replace("Z","+00:00")):
                    return {"status":"SETTLEMENT_GRACE","until":grace_until}
            except ValueError: pass
        expected=self.permitted_capital()+float(self.setting("net_external_flows_usdc","0") or 0)
        delta=actual-expected
        if abs(delta)<.01:return None
        kind="DEPOSIT" if delta>0 else "WITHDRAWAL"; self.set_setting("net_external_flows_usdc",float(self.setting("net_external_flows_usdc","0") or 0)+delta)
        self.db.execute("INSERT INTO capital_flows(at,kind,amount,balance_after) VALUES(?,?,?,?)",(utcnow(),kind,delta,actual)); self.db.commit(); self.event("CAPITAL_"+kind,{"amount_usdc":delta,"balance_after_usdc":actual}); return {"kind":kind,"amount_usdc":delta}
    def issue_recommendation(self,p):
        self.db.execute("INSERT INTO recommendations(ticket_id,recommendation_hash,model_version,created_at,expires_at,product_id,payload,status) VALUES(?,?,?,?,?,?,?,?)",(p["ticket_id"],p["recommendation_hash"],p["model_version"],p["created_at"],p["expires_at"],p["product_id"],json.dumps(p,sort_keys=True),"ISSUED")); self.db.commit(); self.event("RECOMMENDATION_ISSUED",{"hash":p["recommendation_hash"],"product_id":p["product_id"],"score":p["score"]},p["ticket_id"])
    def recommendation(self,ticket_id):
        row=self.db.execute("SELECT * FROM recommendations WHERE ticket_id=?",(ticket_id,)).fetchone()
        if not row:return None
        result=dict(row); result["payload"]=json.loads(result["payload"]); return result
    def recent_recommendations(self,limit=20):
        return [dict(r) for r in self.db.execute("SELECT ticket_id,recommendation_hash,model_version,created_at,expires_at,product_id,status,net_return,realized_pnl,closed_at FROM recommendations ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]
    def mark_recommendation(self,ticket_id,status,**values):
        allowed={"order_id","net_return","realized_pnl","closed_at"}; u={"status":status,**{k:v for k,v in values.items() if k in allowed}}
        self.db.execute("UPDATE recommendations SET "+",".join(f"{k}=?" for k in u)+" WHERE ticket_id=?",(*u.values(),ticket_id)); self.db.commit()
    def add_position(self,ticket,order_id):
        now=utcnow(); self.db.execute("INSERT INTO positions(ticket_id,product_id,order_id,status,entry_notional,entry_price,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(ticket["ticket_id"],ticket["product_id"],order_id,"SUBMITTED",ticket["notional_usdc"],ticket["limit_price"],now,now)); self.db.commit()
    def open_position(self):
        row=self.db.execute("SELECT * FROM positions WHERE status NOT IN ('CLOSED','CANCELLED','FAILED','EXPIRED') ORDER BY opened_at DESC LIMIT 1").fetchone(); return dict(row) if row else None
    def update_position(self,ticket_id,status,pnl=None):
        self.db.execute("UPDATE positions SET status=?,updated_at=?,closed_at=CASE WHEN ?='CLOSED' THEN ? ELSE closed_at END,realized_pnl=COALESCE(?,realized_pnl) WHERE ticket_id=?",(status,utcnow(),status,utcnow(),pnl,ticket_id)); self.db.commit()
    def record_closed_trade(self,ticket_id,pnl,return_pct):
        self.add_realized_pnl(pnl); losses=int(self.setting("consecutive_losses","0") or 0); losses=losses+1 if pnl<0 else 0; self.set_setting("consecutive_losses",losses)
        self.update_position(ticket_id,"CLOSED",pnl); self.mark_recommendation(ticket_id,"CLOSED",net_return=return_pct,realized_pnl=pnl,closed_at=utcnow()); self.set_setting("settlement_grace_until",(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()); self.event("POSITION_CLOSED",{"realized_pnl_usdc":pnl,"net_return_pct":return_pct,"consecutive_losses":losses},ticket_id)
        controls=self.update_equity_controls(self.reconciled_equity(),source="REALIZED_CAPITAL"); return {"pnl":pnl,"return_pct":return_pct,"controls":controls}
    def update_equity_controls(self,equity,source="RECONCILED_EQUITY"):
        today=str(datetime.now(timezone.utc).date())
        if self.setting("daily_start_date")!=today:self.set_setting("daily_start_date",today);self.set_setting("daily_start_equity_usdc",equity)
        peak=max(float(self.setting("peak_equity_usdc","0") or 0),equity);self.set_setting("peak_equity_usdc",peak); daily=float(self.setting("daily_start_equity_usdc",str(equity)) or equity)
        dd=0 if peak<=0 else (peak-equity)/peak; ddd=0 if daily<=0 else (daily-equity)/daily; losses=int(self.setting("consecutive_losses","0") or 0); reasons=[]
        if losses>=2:reasons.append("two consecutive losses")
        if ddd>=.15:reasons.append("daily drawdown reached 15%")
        if dd>=.25:reasons.append("peak drawdown reached 25%")
        if reasons and not self.paused():self.event("PAUSED",{"reason":"; ".join(reasons),"automatic":True})
        return {"equity_usdc":equity,"equity_source":source,"peak_equity_usdc":peak,"drawdown_pct":dd*100,"daily_drawdown_pct":ddd*100,"consecutive_losses":losses,"circuit_breakers":reasons}
    def model_review(self,trigger,key):
        old=self.db.execute("SELECT payload FROM reviews WHERE review_key=?",(key,)).fetchone()
        if old:return json.loads(old[0])
        rows=[dict(r) for r in self.db.execute("""SELECT r.product_id,r.payload,r.realized_pnl,
            p.entry_notional,p.entry_price FROM recommendations r LEFT JOIN positions p ON p.ticket_id=r.ticket_id
            WHERE r.status='CLOSED' AND r.realized_pnl IS NOT NULL""").fetchall()]
        pnls=[float(r["realized_pnl"]) for r in rows]; wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
        by_product={}; captures=[]; peak_opportunities=[]
        for row in rows:
            payload=json.loads(row["payload"]); product=str(row["product_id"]); pnl=float(row["realized_pnl"])
            bucket=by_product.setdefault(product,{"sample_size":0,"wins":0,"net_pnl_usdc":0.0})
            bucket["sample_size"]+=1;bucket["wins"]+=int(pnl>0);bucket["net_pnl_usdc"]=round(bucket["net_pnl_usdc"]+pnl,8)
            ticket_id=str(payload.get("ticket_id") or "");entry=float(row.get("entry_price") or 0);notional=float(row.get("entry_notional") or 0)
            high=float(self.setting("high_water:"+ticket_id,str(entry)) or entry) if ticket_id else entry
            peak=notional*(high/entry-1) if entry>0 else 0;peak_opportunities.append(peak)
            if peak>0:captures.append(pnl/peak)
        p={"model_version":"3.1","sample_size":len(pnls),"wins":len(wins),"losses":len(losses),"win_rate":len(wins)/len(pnls) if pnls else None,"net_pnl_usdc":sum(pnls),"net_expectancy_usdc":sum(pnls)/len(pnls) if pnls else None,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,"average_win_usdc":sum(wins)/len(wins) if wins else None,"average_loss_usdc":sum(losses)/len(losses) if losses else None,"average_peak_opportunity_usdc":sum(peak_opportunities)/len(peak_opportunities) if peak_opportunities else None,"average_profit_capture":sum(captures)/len(captures) if captures else None,"by_product":by_product,"status":"MODEL LOCKED - COLLECTING EVIDENCE" if len(pnls)<30 else "ELIGIBLE FOR PROSPECTIVE CHALLENGER REVIEW","parameters_changed":False,"promotion_rule":"At least 30 closed trades; positive expectancy and profit factor above 1.0; challenger must then outperform prospectively without materially worse drawdown","trigger":trigger}
        self.db.execute("INSERT INTO reviews(review_key,at,trigger,payload) VALUES(?,?,?,?)",(key,utcnow(),trigger,json.dumps(p,sort_keys=True)));self.db.commit();self.event("MODEL_REVIEW",p);return p
    def recent_reviews(self,limit=10):
        return [{**dict(r),"payload":json.loads(r["payload"])} for r in self.db.execute("SELECT review_key,at,trigger,payload FROM reviews ORDER BY at DESC LIMIT ?",(limit,)).fetchall()]
    def event(self,kind,payload,ticket_id=None):
        raw=json.dumps(payload,sort_keys=True,separators=(",",":"),default=str);self.db.execute("INSERT INTO events(at,kind,ticket_id,payload,digest) VALUES(?,?,?,?,?)",(utcnow(),kind,ticket_id,raw,hashlib.sha256(raw.encode()).hexdigest()));self.db.commit()
    def seen(self,ticket_id):return self.db.execute("SELECT 1 FROM events WHERE ticket_id=? LIMIT 1",(ticket_id,)).fetchone() is not None
    def paused(self):
        row=self.db.execute("SELECT kind FROM events WHERE kind IN ('PAUSED','RESUMED') ORDER BY seq DESC LIMIT 1").fetchone();return bool(row and row[0]=="PAUSED")
    def recent(self,limit=50,since_seq=0):
        return [{**dict(r),"payload":json.loads(r["payload"])} for r in self.db.execute("SELECT seq,at,kind,ticket_id,payload,digest FROM events WHERE seq>? ORDER BY seq DESC LIMIT ?",(since_seq,limit)).fetchall()]
