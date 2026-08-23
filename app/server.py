from __future__ import annotations
import hashlib,hmac,html,os,uuid
from datetime import datetime,timezone
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.disk import DiskStore
from starlette.requests import Request
from starlette.responses import HTMLResponse,JSONResponse
from .decision import build_recommendation,canonical_hash
from .exchange import Exchange
from .risk import TicketRejected,validate_ticket
from .store import Store

BASE_URL=os.environ["PUBLIC_BASE_URL"].rstrip("/");SETUP_TOKEN=os.environ["SETUP_TOKEN"];REST_API_TOKEN=os.getenv("REST_API_TOKEN","")
LIVE_ARMED=os.getenv("LIVE_TRADING","false").lower()=="true" and os.getenv("LIVE_CONFIRMATION","")=="I_ACCEPT_THE_25_USDC_LIVE_RISK"
ALLOCATION_FRACTION=min(.95,max(.20,float(os.getenv("MAX_CAPITAL_ALLOCATION_PCT","95"))/100))
store=Store(os.getenv("DATA_DIR","/app/data"),os.environ["CREDENTIAL_ENCRYPTION_KEY"])
auth=GitHubProvider(client_id=os.environ["GITHUB_CLIENT_ID"],client_secret=os.environ["GITHUB_CLIENT_SECRET"],base_url=BASE_URL,client_storage=DiskStore(directory=os.path.join(os.getenv("DATA_DIR","/app/data"),"oauth")))
mcp=FastMCP("Primus Unified Coinbase Pilot",auth=auth,instructions="Model 3.1 recommendations must be issued and frozen here before reporting or execution. Execution accepts only the exact ticket ID and hash. Reconcile before every decision. Live writes are high impact.")

def exchange():return Exchange(*store.credentials())
def order_id(response):
    success=response.get("success_response") or {};return success.get("order_id") or response.get("order_id")
def summarize_fills(fills):
    buys=[f for f in fills if f["side"]=="BUY"];sells=[f for f in fills if f["side"]=="SELL"]
    buy_qty=sum(f["size"] for f in buys);sell_qty=sum(f["size"] for f in sells);fees=sum(f["commission"] for f in fills)
    buy_cost=sum(f["size"]*f["price"] for f in buys)+sum(f["commission"] for f in buys);sell_value=sum(f["size"]*f["price"] for f in sells)-sum(f["commission"] for f in sells)
    return {"buy_qty":buy_qty,"sell_qty":sell_qty,"net_qty":max(0,buy_qty-sell_qty),"buy_cost_usdc":buy_cost,"sell_value_usdc":sell_value,"fees_usdc":fees,"fill_count":len(fills)}

def run_preflight():
    pf=exchange().preflight();baseline=store.initialize_baseline(pf["usdc_total"]);flow=store.sync_external_flow(pf["usdc_total"])
    result={**pf,"pilot_baseline_usdc":baseline,"permitted_capital_usdc":store.permitted_capital(),"allocation_fraction":ALLOCATION_FRACTION,"capital_flow":flow};store.event("PREFLIGHT_OK",result);return result

def reconcile():
    position=store.open_position(); ex=exchange();pf=ex.preflight()
    if not position:
        flow=store.sync_external_flow(pf["usdc_total"]);controls=store.update_equity_controls(store.permitted_capital());return {"open_position":None,"usdc_total":pf["usdc_total"],"capital_flow":flow,"controls":controls}
    fills=ex.fills(position["product_id"],position["opened_at"]);summary=summarize_fills(fills);product=ex.product(position["product_id"]);mark_value=summary["net_qty"]*product["price"]
    equity=pf["usdc_total"]+mark_value;controls=store.update_equity_controls(equity)
    if summary["buy_qty"]>0 and summary["net_qty"]<=max(1e-12,float(product.get("base_min_size") or 0)/10) and summary["sell_qty"]>0:
        pnl=summary["sell_value_usdc"]-summary["buy_cost_usdc"];ret=100*pnl/summary["buy_cost_usdc"] if summary["buy_cost_usdc"] else 0
        closed=store.record_closed_trade(position["ticket_id"],pnl,ret);review=store.model_review("trade_close","trade:"+position["ticket_id"]);store.update_equity_controls(pf["usdc_total"])
        return {"open_position":None,"closed_trade":closed,"fill_summary":summary,"model_review":review,"controls":controls}
    status="FILLED" if summary["buy_qty"]>0 else "SUBMITTED";store.update_position(position["ticket_id"],status)
    unrealized=summary["sell_value_usdc"]+mark_value-summary["buy_cost_usdc"]
    return {"open_position":{**position,"status":status,"mark_price":product["price"],"mark_value_usdc":mark_value,"net_unrealized_pnl_usdc":unrealized,"fills":summary},"usdc_total":pf["usdc_total"],"controls":controls}

def issue(candidate):
    if store.paused():raise RuntimeError("live pilot is paused")
    state=reconcile()
    if state.get("open_position"):raise RuntimeError("one-position limit reached")
    pf=run_preflight();recommendation=build_recommendation(candidate);product=exchange().product(recommendation["product_id"])
    validate_ticket(recommendation,available_usdc=pf["usdc_available"],permitted_capital=store.permitted_capital(),open_positions=0,product=product,allocation_fraction=ALLOCATION_FRACTION)
    recommendation["verified_product"]={k:product.get(k) for k in ("product_id","product_type","price","volume_24h","base_min_size","quote_min_size","trading_disabled","view_only")}
    recommendation["recommendation_hash"]=canonical_hash({k:v for k,v in recommendation.items() if k!="recommendation_hash"})
    store.issue_recommendation(recommendation);return recommendation

def execute(ticket_id,recommendation_hash):
    if store.paused():raise RuntimeError("live pilot is paused")
    record=store.recommendation(ticket_id)
    if not record:raise ValueError("unknown ticket_id")
    if not hmac.compare_digest(record["recommendation_hash"],recommendation_hash):raise ValueError("recommendation hash mismatch")
    if record["status"]!="ISSUED":raise RuntimeError("ticket is not executable")
    ticket=record["payload"]
    if datetime.fromisoformat(ticket["expires_at"])<=datetime.now(timezone.utc):store.mark_recommendation(ticket_id,"EXPIRED");raise RuntimeError("ticket expired")
    state=reconcile()
    if state.get("open_position"):raise RuntimeError("one-position limit reached")
    ex=exchange();pf=run_preflight();product=ex.product(ticket["product_id"])
    validate_ticket(ticket,available_usdc=pf["usdc_available"],permitted_capital=store.permitted_capital(),open_positions=0,product=product,allocation_fraction=ALLOCATION_FRACTION)
    preview=ex.preview_buy(ticket);store.event("ORDER_PREVIEWED",{"preview_sha256":hashlib.sha256(str(preview).encode()).hexdigest()},ticket_id)
    if not LIVE_ARMED:return {"status":"DRY_RUN_ONLY","ticket_id":ticket_id,"recommendation_hash":recommendation_hash,"message":"No real order submitted"}
    response=ex.submit_buy(ticket);oid=order_id(response)
    if not oid:raise RuntimeError("Coinbase did not return an order id")
    store.add_position(ticket,oid);store.mark_recommendation(ticket_id,"SUBMITTED",order_id=oid);store.event("ORDER_SUBMITTED",{"order_id":oid,"product_id":ticket["product_id"],"notional_usdc":ticket["notional_usdc"],"recommendation_hash":recommendation_hash},ticket_id)
    return {"status":"ORDER_SUBMITTED","ticket_id":ticket_id,"order_id":oid}

def hourly_snapshot(since_seq=0):
    state=reconcile() if store.setting("pilot_baseline_usdc") else {"open_position":store.open_position()};bucket=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    review=store.model_review("hourly_report","hourly:"+bucket)
    return {"timestamp":datetime.now(timezone.utc).isoformat(),"mode":"LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED","paused":store.paused(),"baseline_usdc":float(store.setting("pilot_baseline_usdc","0") or 0),"net_external_flows_usdc":float(store.setting("net_external_flows_usdc","0") or 0),"realized_pnl_usdc":float(store.setting("realized_pnl_usdc","0") or 0),"permitted_capital_usdc":store.permitted_capital(),"portfolio":state,"recommendations":store.recent_recommendations(),"model_review":review,"recent_reviews":store.recent_reviews(),"notification_events":store.recent(since_seq=since_seq)}

def pause(reason):store.event("PAUSED",{"reason":reason,"automatic":False});return {"paused":True,"reason":reason}
def resume(acknowledgement):
    if acknowledgement!="I_REVIEWED_THE_LOSSES_AND_ACCEPT_RESUMING":raise ValueError("resume acknowledgement mismatch")
    store.event("RESUMED",{"acknowledgement":acknowledgement});return {"paused":False}
def flatten(reason,confirmation):
    if confirmation!="CANCEL_AND_SELL_NOW":raise ValueError("flatten confirmation mismatch")
    pause(reason);position=store.open_position()
    if not position:return {"paused":True,"flattened":False,"reason":"no open position"}
    ex=exchange();cancel=ex.cancel_order(position["order_id"]) if position.get("order_id") else {};size=ex.base_balance(position["product_id"]);sale={}
    if size>0:sale=ex.market_sell(position["product_id"],size,"flatten-"+str(uuid.uuid4()))
    store.event("EMERGENCY_FLATTEN_SUBMITTED",{"reason":reason,"base_size":size,"cancel_response":str(cancel),"sell_response":str(sale)},position["ticket_id"]);return {"paused":True,"flattened":bool(size>0),"base_size":size}

def rest_authorized(request):
    supplied=request.headers.get("authorization","");supplied=supplied[7:] if supplied.lower().startswith("bearer ") else supplied
    return bool(REST_API_TOKEN) and hmac.compare_digest(supplied,REST_API_TOKEN)
def unauthorized():return JSONResponse({"error":"unauthorized"},status_code=401,headers={"WWW-Authenticate":"Bearer"})

@mcp.custom_route("/health",methods=["GET"])
async def health(_):return JSONResponse({"ok":True,"mode":"LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED"})
@mcp.custom_route("/setup",methods=["GET","POST"])
async def setup(request):
    if not hmac.compare_digest(request.query_params.get("token",""),SETUP_TOKEN):return HTMLResponse("Not found",status_code=404)
    message=""
    if request.method=="POST":
        form=await request.form();key,secret=str(form.get("key_name","")).strip(),str(form.get("private_key","")).strip()
        if not key or "PRIVATE KEY" not in secret:message="Invalid key fields. Nothing saved."
        else:store.save_credentials(key,secret);message="Credentials encrypted and saved. Rotate SETUP_TOKEN now."
    return HTMLResponse(f'''<!doctype html><meta name="viewport" content="width=device-width"><title>Coinbase pilot setup</title><main style="max-width:620px;margin:30px auto;font:16px system-ui"><h1>Unified Coinbase pilot</h1><p>{html.escape(message)}</p><form method="post"><input name="key_name" placeholder="API key name" required style="width:100%;padding:12px"><textarea name="private_key" required style="width:100%;height:220px;margin-top:12px"></textarea><button style="padding:12px;margin-top:12px">Encrypt and save</button></form></main>''')

@mcp.tool(annotations={"readOnlyHint":True,"openWorldHint":True})
def preflight_coinbase()->dict:return run_preflight()
@mcp.tool(annotations={"readOnlyHint":True,"openWorldHint":True})
def list_eligible_spot_products()->dict:
    products=exchange().eligible_products();return {"count":len(products),"products":products}
@mcp.tool(annotations={"openWorldHint":True,"idempotentHint":False})
def issue_model_3_1_recommendation(candidate:dict)->dict:
    """Score, validate and freeze the exact recommendation that must appear in the report before execution."""
    return issue(candidate)
@mcp.tool(annotations={"readOnlyHint":True,"openWorldHint":True})
def pilot_status(since_event_seq:int=0)->dict:return hourly_snapshot(since_event_seq)
@mcp.tool(annotations={"destructiveHint":True,"openWorldHint":True,"idempotentHint":True})
def execute_issued_ticket(ticket_id:str,recommendation_hash:str)->dict:
    """Execute only an unmodified, unexpired server-issued recommendation. This can spend real money."""
    return execute(ticket_id,recommendation_hash)
@mcp.tool(annotations={"destructiveHint":True,"idempotentHint":True})
def emergency_pause(reason:str)->dict:return pause(reason)
@mcp.tool(annotations={"destructiveHint":True,"openWorldHint":True,"idempotentHint":True})
def emergency_flatten(reason:str,confirmation:str)->dict:
    """Cancel the tracked entry and market-sell the tracked asset. This can realize a loss."""
    return flatten(reason,confirmation)
@mcp.tool(annotations={"destructiveHint":True,"idempotentHint":True})
def resume_trading(acknowledgement:str)->dict:return resume(acknowledgement)

@mcp.custom_route("/api/pilot-status",methods=["GET"])
async def rest_status(request):
    if not rest_authorized(request):return unauthorized()
    try:return JSONResponse(hourly_snapshot(int(request.query_params.get("since_seq","0"))))
    except Exception as exc:return JSONResponse({"error":str(exc)},status_code=500)

if __name__=="__main__":mcp.run(transport="http",host="0.0.0.0",port=int(os.getenv("PORT","8080")))
