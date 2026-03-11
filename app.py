"""
Nifty 50 Live Open Interest Dashboard v2
Run: py app.py  →  Open: http://localhost:5000
"""

import json, time, math, random
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, render_template, Response, request

app = Flask(__name__)

# ─── NSE Session (robust browser-like) ───────────────────────────────────────

_session      = None
_session_time = 0

def make_nse_session():
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":  "document",
            "Sec-Fetch-Mode":  "navigate",
            "Sec-Fetch-Site":  "none",
            "Sec-Fetch-User":  "?1",
        })
        # Step 1 – get main page cookies
        r = s.get("https://www.nseindia.com/", timeout=12)
        print(f"  [NSE] homepage: {r.status_code}  cookies: {list(s.cookies.keys())}")
        time.sleep(1.5)

        # Step 2 – visit option-chain page (sets more cookies)
        s.headers.update({
            "Referer":        "https://www.nseindia.com/",
            "Sec-Fetch-Site": "same-origin",
        })
        r2 = s.get("https://www.nseindia.com/option-chain", timeout=12)
        print(f"  [NSE] option-chain page: {r2.status_code}  cookies: {list(s.cookies.keys())}")
        time.sleep(1.0)

        # Step 3 – update headers for API calls
        s.headers.update({
            "Accept":   "application/json, text/plain, */*",
            "Referer":  "https://www.nseindia.com/option-chain",
            "X-Requested-With": "XMLHttpRequest",
        })
        return s
    except Exception as e:
        print(f"  [NSE] session error: {e}")
        return None

def get_session(force=False):
    global _session, _session_time
    if force or _session is None or (time.time() - _session_time) > 240:
        print("[NSE] Creating new session...")
        _session      = make_nse_session()
        _session_time = time.time()
    return _session

def fetch_nse_option_chain():
    for attempt in range(2):
        try:
            s = get_session(force=(attempt > 0))
            if s is None:
                return None
            url  = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
            resp = s.get(url, timeout=12)
            print(f"  [NSE] OI API: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                spot = data.get("records", {}).get("underlyingValue", 0)
                if spot > 0:
                    print(f"  [NSE] Spot={spot} ✓")
                    return data
                else:
                    print("  [NSE] spot=0, retrying...")
            elif resp.status_code == 401:
                print("  [NSE] 401 – session expired, refreshing...")
                _session = None
        except Exception as e:
            print(f"  [NSE] attempt {attempt+1} error: {e}")
    return None

# ─── Greeks (Black-Scholes) ───────────────────────────────────────────────────

def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x):
    return math.exp(-0.5*x*x) / math.sqrt(2*math.pi)

def compute_greeks(S, K, T, r, sigma, opt_type="call"):
    try:
        if T<=0 or sigma<=0 or S<=0 or K<=0:
            return {"delta":0,"gamma":0,"theta":0,"vega":0}
        d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
        d2 = d1 - sigma*math.sqrt(T)
        delta = norm_cdf(d1) if opt_type=="call" else norm_cdf(d1)-1
        gamma = norm_pdf(d1)/(S*sigma*math.sqrt(T))
        theta = (-(S*norm_pdf(d1)*sigma)/(2*math.sqrt(T))
                 - r*K*math.exp(-r*T)*(norm_cdf(d2) if opt_type=="call" else norm_cdf(-d2)))/365
        vega  = S*norm_pdf(d1)*math.sqrt(T)/100
        return {"delta":round(delta,4),"gamma":round(gamma,6),"theta":round(theta,2),"vega":round(vega,2)}
    except:
        return {"delta":0,"gamma":0,"theta":0,"vega":0}

def days_to_expiry(expiry_str):
    for fmt in ["%d-%b-%Y","%d %b %Y"]:
        try:
            return max((datetime.strptime(expiry_str,fmt).date()-date.today()).days,0)/365.0
        except: pass
    return 7/365.0

# ─── OI Signals ───────────────────────────────────────────────────────────────

def get_oi_signal(call_chg, put_chg):
    if   call_chg>0 and put_chg<0: return {"label":"Long Buildup","color":"bullish"}
    elif put_chg>0  and call_chg<0: return {"label":"Short Buildup","color":"bearish"}
    elif call_chg<0 and put_chg>0: return {"label":"Short Cover","color":"bullish"}
    elif call_chg>0 and put_chg>0: return {"label":"OI Adding","color":"neutral"}
    elif call_chg<0 and put_chg<0: return {"label":"OI Unwinding","color":"neutral"}
    return {"label":"—","color":"neutral"}

def find_sr(records, spot):
    below = [r for r in records if r["strike"]<=spot]
    above = [r for r in records if r["strike"]>=spot]
    return {
        "supports":    [{"strike":r["strike"],"oi":r["put_oi"]}  for r in sorted(below,key=lambda x:x["put_oi"],  reverse=True)[:3]],
        "resistances": [{"strike":r["strike"],"oi":r["call_oi"]} for r in sorted(above,key=lambda x:x["call_oi"], reverse=True)[:3]],
    }

def compute_max_pain(records):
    if not records: return 0
    best,minp = records[0]["strike"],float("inf")
    for c in records:
        s = c["strike"]
        p = sum(r["call_oi"]*max(0,r["strike"]-s)+r["put_oi"]*max(0,s-r["strike"]) for r in records)
        if p<minp: minp=p; best=s
    return best

# ─── Simulated fallback ───────────────────────────────────────────────────────

def get_expiry_list():
    d,out=[],[]
    cur=date.today()
    for _ in range(5):
        da=3-cur.weekday()
        if da<=0: da+=7
        cur=cur+timedelta(days=da)
        out.append(cur.strftime("%d-%b-%Y"))
    return out

def generate_simulated_data(base_spot=24200, expiry=None):
    expiries = get_expiry_list()
    expiry   = expiry or expiries[0]
    spot     = base_spot + random.uniform(-30,30)
    atm      = round(spot/50)*50
    strikes  = [atm+i*50 for i in range(-10,11)]
    T=days_to_expiry(expiry); r=0.065

    records,tco,tpo=[],0,0
    for strike in strikes:
        dist=abs(strike-spot)/spot
        bo=max(100000,int(5000000*(1-dist*8)))
        co=int(bo*random.uniform(0.7,1.3)); po=int(bo*random.uniform(0.7,1.3))
        if strike<spot:   co=int(co*0.6);po=int(po*1.3)
        elif strike>spot: co=int(co*1.3);po=int(po*0.6)
        cc=random.uniform(-15,20); pc=random.uniform(-15,20)
        iv_b=0.12+dist*0.5
        civ=round((iv_b+random.uniform(-0.01,0.01))*100,2)
        piv=round((iv_b+random.uniform(-0.01,0.01))*100,2)
        cg=compute_greeks(spot,strike,T,r,civ/100,"call")
        pg=compute_greeks(spot,strike,T,r,piv/100,"put")
        sig=get_oi_signal(int(co*cc/100),int(po*pc/100))
        records.append({
            "strike":strike,
            "moneyness":"ATM" if strike==atm else ("ITM" if strike<spot else "OTM"),
            "call_oi":co,"call_oi_chg":int(co*cc/100),"call_iv":civ,
            "call_volume":int(co*random.uniform(0.05,0.3)),
            "call_ltp":round(max(0.5,(spot-strike+300)*random.uniform(0.9,1.1)),2) if strike<spot else round(random.uniform(5,300),2),
            "call_delta":cg["delta"],"call_gamma":cg["gamma"],"call_theta":cg["theta"],"call_vega":cg["vega"],
            "put_oi":po,"put_oi_chg":int(po*pc/100),"put_iv":piv,
            "put_volume":int(po*random.uniform(0.05,0.3)),
            "put_ltp":round(max(0.5,(strike-spot+300)*random.uniform(0.9,1.1)),2) if strike>spot else round(random.uniform(5,300),2),
            "put_delta":pg["delta"],"put_gamma":pg["gamma"],"put_theta":pg["theta"],"put_vega":pg["vega"],
            "signal":sig,
        })
        tco+=co; tpo+=po

    pcr=round(tpo/tco,3) if tco else 0
    return {
        "spot":round(spot,2),"atm":atm,
        "timestamp":datetime.now().strftime("%d %b %Y  %H:%M:%S"),
        "expiry":expiry,"expiry_list":expiries,
        "pcr":pcr,"max_pain":compute_max_pain(records),
        "total_call_oi":tco,"total_put_oi":tpo,
        "support_resistance":find_sr(records,spot),
        "records":records,"source":"simulated",
    }

# ─── Parse live NSE data ──────────────────────────────────────────────────────

def parse_nse_data(raw, expiry_filter=None):
    try:
        all_rows     = raw.get("records",{}).get("data",[])
        spot         = raw.get("records",{}).get("underlyingValue",0)
        expiry_dates = raw.get("records",{}).get("expiryDates",[])
        expiry = expiry_filter or (expiry_dates[0] if expiry_dates else "N/A")
        T=days_to_expiry(expiry); r=0.065
        atm=round(spot/50)*50
        records,tco,tpo=[],0,0

        for row in all_rows:
            if expiry_filter and row.get("expiryDate")!=expiry_filter: continue
            strike=row.get("strikePrice",0)
            ce=row.get("CE",{}); pe=row.get("PE",{})
            co=ce.get("openInterest",0); po=pe.get("openInterest",0)
            civ=ce.get("impliedVolatility",0); piv=pe.get("impliedVolatility",0)
            cc=ce.get("changeinOpenInterest",0); pc=pe.get("changeinOpenInterest",0)
            tco+=co; tpo+=po
            cg=compute_greeks(spot,strike,T,r,civ/100 if civ else 0.15,"call")
            pg=compute_greeks(spot,strike,T,r,piv/100 if piv else 0.15,"put")
            records.append({
                "strike":strike,
                "moneyness":"ATM" if strike==atm else ("ITM" if strike<spot else "OTM"),
                "call_oi":co,"call_oi_chg":cc,"call_iv":round(civ,2),
                "call_ltp":ce.get("lastPrice",0),"call_volume":ce.get("totalTradedVolume",0),
                "call_delta":cg["delta"],"call_gamma":cg["gamma"],"call_theta":cg["theta"],"call_vega":cg["vega"],
                "put_oi":po,"put_oi_chg":pc,"put_iv":round(piv,2),
                "put_ltp":pe.get("lastPrice",0),"put_volume":pe.get("totalTradedVolume",0),
                "put_delta":pg["delta"],"put_gamma":pg["gamma"],"put_theta":pg["theta"],"put_vega":pg["vega"],
                "signal":get_oi_signal(cc,pc),
            })

        pcr=round(tpo/tco,3) if tco else 0
        return {
            "spot":round(spot,2),"atm":atm,
            "timestamp":datetime.now().strftime("%d %b %Y  %H:%M:%S"),
            "expiry":expiry,"expiry_list":expiry_dates[:5],
            "pcr":pcr,"max_pain":compute_max_pain(records),
            "total_call_oi":tco,"total_put_oi":tpo,
            "support_resistance":find_sr(records,spot),
            "records":records,"source":"live",
        }
    except Exception as e:
        print(f"Parse error: {e}"); return None

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/oi")
def api_oi():
    expiry=request.args.get("expiry",None)
    raw=fetch_nse_option_chain()
    if raw:
        data=parse_nse_data(raw,expiry)
        if data: return jsonify({"ok":True,"data":data})
    return jsonify({"ok":True,"data":generate_simulated_data(expiry=expiry)})

@app.route("/api/stream")
def stream():
    expiry=request.args.get("expiry",None)
    def gen():
        while True:
            raw=fetch_nse_option_chain()
            data=parse_nse_data(raw,expiry) if raw else None
            if not data: data=generate_simulated_data(expiry=expiry)
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(30)
    return Response(gen(),mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__=="__main__":
    print("\n"+"="*55)
    print("  🚀  Nifty 50 OI Dashboard v2")
    print("  📡  Open:  http://localhost:5000")
    print("  ℹ️   Auto-refresh every 30 seconds")
    print("="*55+"\n")
    app.run(debug=False,host="0.0.0.0",port=5000,threaded=True)
