from __future__ import annotations
import hashlib,hmac,html,os,time,uuid
from dataclasses import asdict
from datetime import datetime,timezone
from importlib.metadata import version as package_version
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.disk import DiskStore
from starlette.requests import Request
from starlette.responses import HTMLResponse,JSONResponse
from .auth_config import OAUTH_CALLBACK_PATH, load_or_create_signing_key, oauth_callback_url, validate_public_base_url
from .decision import build_recommendation,canonical_hash
from .enrichment import enrich_with_coinbase
from .exchange import CoinbaseOrderRejected,Exchange
from .lifecycle import profit_protection_challenger,supervision_levels
from .policy import OpportunityPolicy
from .risk import TicketRejected,validate_ticket
from .store import Store

BASE_URL=validate_public_base_url(os.environ["PUBLIC_BASE_URL"]);SETUP_TOKEN=os.environ["SETUP_TOKEN"];REST_API_TOKEN=os.getenv("REST_API_TOKEN","")
LIVE_ARMED=os.getenv("LIVE_TRADING","false").lower()=="true" and os.getenv("LIVE_CONFIRMATION","")=="I_ACCEPT_THE_25_USDC_LIVE_RISK"
PREAUTHORIZED_AUTO_EXECUTION=os.getenv("PREAUTHORIZED_AUTO_EXECUTION","false").lower()=="true"
ALLOCATION_FRACTION=min(.95,max(.20,float(os.getenv("MAX_CAPITAL_ALLOCATION_PCT","95"))/100))
ENTRY_TIMEOUT_SECONDS=min(90,max(5,int(os.getenv("ENTRY_TIMEOUT_SECONDS","45"))))
MAX_ENTRY_DRIFT_BPS=min(35,max(1,float(os.getenv("MAX_ENTRY_DRIFT_BPS","35"))))
DATA_DIR=os.getenv("DATA_DIR","/app/data")
store=Store(DATA_DIR,os.environ["CREDENTIAL_ENCRYPTION_KEY"])
OAUTH_STORAGE_DIR=os.path.join(DATA_DIR,"oauth")
JWT_SIGNING_KEY,JWT_SIGNING_KEY_SOURCE=load_or_create_signing_key(DATA_DIR,os.getenv("JWT_SIGNING_KEY"))
auth=GitHubProvider(client_id=os.environ["GITHUB_CLIENT_ID"],client_secret=os.environ["GITHUB_CLIENT_SECRET"],base_url=BASE_URL,redirect_path=OAUTH_CALLBACK_PATH,jwt_signing_key=JWT_SIGNING_KEY,client_storage=DiskStore(directory=OAUTH_STORAGE_DIR))
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
        flow=store.sync_external_flow(pf["usdc_total"]);controls=store.update_equity_controls(store.reconciled_equity(),source="PERMITTED_CAPITAL");return {"open_position":None,"usdc_total":pf["usdc_total"],"capital_flow":flow,"controls":controls}
    fills=ex.fills(position["product_id"],position["opened_at"]);summary=summarize_fills(fills);product=ex.product(position["product_id"]);mark_value=summary["net_qty"]*product["price"]
    order=ex.get_order(position["order_id"]) if position.get("order_id") else {};order_status=str(order.get("status","")).upper()
    if summary["buy_qty"]<=0 and order_status in {"CANCELLED","FAILED","EXPIRED"}:
        store.update_position(position["ticket_id"],order_status);store.mark_recommendation(position["ticket_id"],order_status)
        store.event("ENTRY_"+order_status,{"order_id":position.get("order_id")},position["ticket_id"])
        controls=store.update_equity_controls(store.reconciled_equity(),source="PERMITTED_CAPITAL")
        return {"open_position":None,"entry_status":order_status,"usdc_total":pf["usdc_total"],"controls":controls}
    if summary["buy_qty"]>0 and summary["net_qty"]<=max(1e-12,float(product.get("base_min_size") or 0)/10) and summary["sell_qty"]>0:
        pnl=summary["sell_value_usdc"]-summary["buy_cost_usdc"];ret=100*pnl/summary["buy_cost_usdc"] if summary["buy_cost_usdc"] else 0
        closed=store.record_closed_trade(position["ticket_id"],pnl,ret);review=store.model_review("trade_close","trade:"+position["ticket_id"]);controls=closed["controls"]
        reconciliation={"risk_equity_usdc":store.reconciled_equity(),"account_snapshot_equity_usdc":pf["usdc_total"],"settlement_gap_usdc":store.reconciled_equity()-pf["usdc_total"],"source":"REALIZED_CAPITAL"}
        return {"open_position":None,"closed_trade":closed,"fill_summary":summary,"model_review":review,"controls":controls,"equity_reconciliation":reconciliation}
    status=position["status"] if position["status"]=="EXIT_SUBMITTED" else ("FILLED" if summary["buy_qty"]>0 else "SUBMITTED")
    if position["status"]=="SUBMITTED" and status=="FILLED":store.event("ENTRY_FILLED",{"order_id":position.get("order_id"),"fill_summary":summary},position["ticket_id"])
    store.update_position(position["ticket_id"],status)
    unrealized=summary["sell_value_usdc"]+mark_value-summary["buy_cost_usdc"]
    store.update_position_excursions(position["ticket_id"],unrealized)
    equity=store.reconciled_equity(unrealized);controls=store.update_equity_controls(equity,source="FILLS_AND_MARK")
    reconciliation={"risk_equity_usdc":equity,"account_snapshot_equity_usdc":pf["usdc_total"]+mark_value,"settlement_gap_usdc":equity-(pf["usdc_total"]+mark_value),"source":"FILLS_AND_MARK"}
    return {"open_position":{**position,"status":status,"mark_price":product["price"],"mark_value_usdc":mark_value,"net_unrealized_pnl_usdc":unrealized,"fills":summary},"usdc_total":pf["usdc_total"],"controls":controls,"equity_reconciliation":reconciliation}

def supervise(regime=""):
    state=reconcile();position=state.get("open_position")
    if not position:return {"status":"IDLE","state":state}
    if position.get("status")=="EXIT_SUBMITTED":
        exit_id=store.setting("exit_order:"+position["ticket_id"])
        if exit_id:
            exit_status=str(exchange().get_order(exit_id).get("status","")).upper()
            if exit_status not in {"CANCELLED","FAILED","EXPIRED"}:return {"status":"AWAITING_EXIT_FILL","exit_order_id":exit_id,"exit_order_status":exit_status,"state":state}
        store.update_position(position["ticket_id"],"FILLED")
    record=store.recommendation(position["ticket_id"]);ticket=record["payload"]
    mark=float(position["mark_price"]);fills=position.get("fills") or {};entry=float(fills.get("buy_cost_usdc") or 0)/float(fills.get("buy_qty") or 1) if float(fills.get("buy_qty") or 0)>0 else float(position["entry_price"]);ticket_id=position["ticket_id"]
    high_key="high_water:"+ticket_id;levels=supervision_levels(ticket,entry=entry,mark=mark,high_water=float(store.setting(high_key,str(entry)) or entry),regime=str(regime));high=levels["high_water_price"];trail_active=levels["trail_active"];trail_stop=levels["effective_stop_price"];store.set_setting(high_key,high)
    challenger=profit_protection_challenger(ticket,entry=entry,mark=mark,high_water=high)
    target_1=float(ticket.get("target_1_price") or 0);milestone_key="target_1_seen:"+ticket_id
    if target_1 and mark>=target_1 and store.setting(milestone_key)!="1":
        store.set_setting(milestone_key,"1");store.event("TARGET_1_REACHED",{"mark_price":mark,"target_1_price":target_1},ticket_id)
    reason=levels["exit_reason"]
    audit={"entry_price":entry,"entry_score":ticket.get("score"),"component_scores":ticket.get("component_scores"),"entry_spread_bps":ticket.get("spread_bps"),"entry_slippage_bps":ticket.get("slippage_bps"),"stop_price":ticket.get("stop_price"),"target_1_price":ticket.get("target_1_price"),"target_price":ticket.get("target_price"),"recommendation_hash":record.get("recommendation_hash")}
    if not reason:return {"status":"MONITORING","mark_price":mark,"high_water_price":high,"trail_active":trail_active,"effective_stop_price":trail_stop,"entry_audit":audit,"profit_protection_challenger":challenger,"state":state}
    ex=exchange();cancel=ex.cancel_open_sell_orders(position["product_id"]);time.sleep(1);size=ex.available_base_balance(position["product_id"])
    if size<=0:return {"status":"EXIT_WAITING_FOR_CANCEL","reason":reason,"cancel":cancel,"state":reconcile()}
    sale=ex.market_sell(position["product_id"],size,"managed-exit-"+str(uuid.uuid4()));exit_id=order_id(sale)
    if not exit_id:raise RuntimeError("Coinbase did not return an exit order id")
    store.set_setting("exit_order:"+ticket_id,exit_id);store.update_position(ticket_id,"EXIT_SUBMITTED");store.event("MANAGED_EXIT_SUBMITTED",{"reason":reason,"base_size":size,"effective_stop_price":trail_stop,"profit_protection_challenger":challenger,"cancel":str(cancel),"sale":str(sale),"exit_order_id":exit_id},ticket_id)
    return {"status":"EXIT_SUBMITTED","reason":reason,"base_size":size,"exit_order_id":exit_id,"profit_protection_challenger":challenger,"state":reconcile()}

def issue(candidate):
    if store.paused():raise RuntimeError("live pilot is paused")
    state=reconcile()
    if state.get("open_position"):raise RuntimeError("one-position limit reached")
    pf=run_preflight();ex=exchange();draft=build_recommendation(candidate);product=ex.product(draft["product_id"])
    quote=ex.execution_quote(draft["product_id"],draft["notional_usdc"])
    enriched=enrich_with_coinbase(candidate,product=product,quote=quote)
    recommendation=build_recommendation(enriched)
    recommendation["coinbase_evidence"]=enriched["coinbase_evidence"]
    recommendation["coinbase_checked_at"]=enriched["coinbase_checked_at"]
    validate_ticket(recommendation,available_usdc=pf["usdc_available"],permitted_capital=store.permitted_capital(),open_positions=0,product=product,allocation_fraction=ALLOCATION_FRACTION)
    recommendation["verified_product"]={k:product.get(k) for k in ("product_id","product_type","price","volume_24h","base_min_size","quote_min_size","trading_disabled","view_only")}
    recommendation["recommendation_hash"]=canonical_hash({k:v for k,v in recommendation.items() if k!="recommendation_hash"})
    store.issue_recommendation(recommendation);return recommendation

def validate_live_execution(ticket,ex,product):
    quote=ex.execution_quote(ticket["product_id"],ticket["notional_usdc"])
    reference=float(ticket.get("reference_price") or ticket["limit_price"])
    drift=abs(quote["best_ask"]-reference)/reference*10_000 if reference else 9999
    errors=[]
    if quote["spread_bps"]>50:errors.append("live spread above 50 bps")
    if quote["slippage_bps"]>50:errors.append("live slippage above 50 bps")
    if drift>MAX_ENTRY_DRIFT_BPS:errors.append(f"entry drift above {MAX_ENTRY_DRIFT_BPS:g} bps")
    if quote["best_ask"]>float(ticket["limit_price"]):errors.append("best ask exceeds entry ceiling")
    if errors:raise TicketRejected("; ".join(errors))
    result={**quote,"entry_drift_bps":drift,"checked_at":datetime.now(timezone.utc).isoformat()}
    store.event("LIVE_EXECUTION_REVALIDATED",result,ticket["ticket_id"]);return result

def wait_for_entry(ex,ticket,order_id):
    deadline=time.monotonic()+ENTRY_TIMEOUT_SECONDS; latest={}
    while time.monotonic()<deadline:
        latest=ex.get_order(order_id);status=str(latest.get("status","")).upper()
        if status in {"FILLED","CANCELLED","FAILED","EXPIRED"}:break
        time.sleep(1)
    status=str(latest.get("status","")).upper()
    if status not in {"FILLED","CANCELLED","FAILED","EXPIRED"}:
        cancel=ex.cancel_order(order_id);store.event("ENTRY_TIMEOUT_CANCEL_REQUESTED",{"order_id":order_id,"timeout_seconds":ENTRY_TIMEOUT_SECONDS,"response":str(cancel)},ticket["ticket_id"])
    return reconcile()

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
    live_quote=validate_live_execution(ticket,ex,product)
    preview=ex.preview_buy(ticket,product);store.event("ORDER_PREVIEWED",{"preview_sha256":hashlib.sha256(str(preview).encode()).hexdigest()},ticket_id)
    if not LIVE_ARMED:return {"status":"DRY_RUN_ONLY","ticket_id":ticket_id,"recommendation_hash":recommendation_hash,"live_quote":live_quote,"message":"No real order submitted"}
    try:
        response=ex.submit_buy(ticket,product);oid=order_id(response)
        if not oid:raise CoinbaseOrderRejected("UNKNOWN: Coinbase did not return an order id")
    except CoinbaseOrderRejected as exc:
        store.mark_recommendation(ticket_id,"SUBMISSION_REJECTED",rejection_reason=str(exc)[:500])
        store.event("ORDER_SUBMISSION_REJECTED",{"error":str(exc),"broker_response":exc.response},ticket_id)
        raise
    store.add_position(ticket,oid);store.mark_recommendation(ticket_id,"SUBMITTED",order_id=oid);store.event("ORDER_SUBMITTED",{"order_id":oid,"product_id":ticket["product_id"],"notional_usdc":ticket["notional_usdc"],"recommendation_hash":recommendation_hash},ticket_id)
    final_state=wait_for_entry(ex,ticket,oid)
    return {"status":"ORDER_SUBMITTED","ticket_id":ticket_id,"order_id":oid,"live_quote":live_quote,"reconciliation":final_state}

def auto_process(candidate):
    """Atomic pre-authorized path: freeze, validate and execute the same signal."""
    recommendation=issue(candidate)
    result=execute(recommendation["ticket_id"],recommendation["recommendation_hash"])
    return {"recommendation":recommendation,"execution":result}

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
async def health(_):
    return JSONResponse({"ok":True,"mode":"LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED","schema_version":"3.3","preauthorized_auto_execution":PREAUTHORIZED_AUTO_EXECUTION,"expected_tool_count":9,"opportunity_policy":{"version":"1.0",**asdict(OpportunityPolicy.from_env())},"oauth":{"provider":"github","base_url":BASE_URL,"callback_url":oauth_callback_url(BASE_URL),"persistent_client_storage":os.path.abspath(OAUTH_STORAGE_DIR).startswith("/app/data/"),"jwt_signing_key_configured":True,"jwt_signing_key_source":JWT_SIGNING_KEY_SOURCE,"fastmcp_version":package_version("fastmcp")}})
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
@mcp.tool(annotations={"destructiveHint":True,"openWorldHint":True,"idempotentHint":False})
def issue_model_3_1_recommendation(candidate:dict)->dict:
    """Score and freeze a recommendation. When preauthorized mode is enabled, immediately run the same validated fast execution path; this may spend real money if live trading is armed."""
    return auto_process(candidate) if PREAUTHORIZED_AUTO_EXECUTION else issue(candidate)
@mcp.tool(annotations={"readOnlyHint":True,"openWorldHint":True})
def pilot_status(since_event_seq:int=0)->dict:return hourly_snapshot(since_event_seq)
@mcp.tool(annotations={"destructiveHint":True,"openWorldHint":True,"idempotentHint":True})
def execute_issued_ticket(ticket_id:str,recommendation_hash:str)->dict:
    """Execute only an unmodified, unexpired server-issued recommendation. This can spend real money."""
    return execute(ticket_id,recommendation_hash)
@mcp.tool(annotations={"destructiveHint":True,"openWorldHint":True,"idempotentHint":False})
def process_preauthorized_candidate(candidate:dict)->dict:
    """Freeze and immediately process one qualifying Model 3.1 candidate. May spend up to the pre-authorized pilot capital when live trading is armed."""
    return auto_process(candidate)
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

@mcp.custom_route("/api/eligible-products",methods=["GET"])
async def rest_eligible_products(request):
    if not rest_authorized(request):return unauthorized()
    try:
        products=exchange().eligible_products();return JSONResponse({"count":len(products),"products":products})
    except Exception as exc:return JSONResponse({"error":str(exc)},status_code=500)

@mcp.custom_route("/api/auto-candidate",methods=["POST"])
async def rest_auto_candidate(request):
    if not rest_authorized(request):return unauthorized()
    try:return JSONResponse(auto_process(await request.json()))
    except (TicketRejected,CoinbaseOrderRejected,ValueError,TypeError,KeyError) as exc:return JSONResponse({"error":"ticket_rejected","detail":str(exc)},status_code=422)
    except Exception as exc:return JSONResponse({"error":str(exc)},status_code=500)

@mcp.custom_route("/api/position-supervision",methods=["POST"])
async def rest_position_supervision(request):
    if not rest_authorized(request):return unauthorized()
    try:
        payload=await request.json();return JSONResponse(supervise(str(payload.get("regime", ""))))
    except Exception as exc:
        store.event("POSITION_SUPERVISION_ERROR",{"error":type(exc).__name__,"detail":str(exc)[:500]})
        return JSONResponse({"error":str(exc)},status_code=500)

if __name__=="__main__":mcp.run(transport="http",host="0.0.0.0",port=int(os.getenv("PORT","8080")))
