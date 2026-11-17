#!/usr/bin/env python3
"""Offline generator for Departly's bundled multi-operator transit index.

Produces, per operator, a STOP coordinate index + ROUTE→stop-sequence list so the
on-device multi-modal journey planner can do fast, offline spatial snapping + transfer
search without thousands of runtime network calls. Run once (resumable via a raw-response
cache); ship the JSON under Departly/Resources/.

Usage:  python3 build_transit_index.py [nlb|ctb|gmb|kmb|all]

Output (Departly/Resources/transit_index/):
  <op>_stops.json   = [{"id","nameEn","nameTc","lat","lng"}]
  <op>_routes.json  = [{"route","dir","service","stops":[stopId,...]}]
Polite: capped concurrency, retries w/ backoff, on-disk cache of every raw GET.
"""
import json, os, sys, time, ssl, hashlib, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "..", "Departly", "Resources", "transit_index")
CACHE = os.path.join(ROOT, ".transit_cache")
os.makedirs(OUT, exist_ok=True); os.makedirs(CACHE, exist_ok=True)
_ctx = ssl.create_default_context(); _ctx.check_hostname=False; _ctx.verify_mode=ssl.CERT_NONE
WORKERS = int(os.environ.get("IDX_WORKERS", "8"))
# etagmb.gov.hk rate-limits hard. That was previously a COMMENT telling a human to pass
# IDX_WORKERS=3 by hand — and the first CI run, at the default 8, duly got throttled into writing
# 790 of 4,743 GMB stops (83% lost). A rule that only works when someone remembers it is not a rule,
# so GMB now clamps its own concurrency regardless of caller.
GMB_WORKERS = int(os.environ.get("IDX_GMB_WORKERS", "3"))

# Cache entries older than this are re-fetched. WITHOUT a TTL the cache was permanent, and it showed:
# all 11,531 cached responses were dated 2026-06-19, so the "nightly regeneration" had been replaying
# six-week-old data for every KMB/CTB/GMB/NLB/MTR-Bus stop and route. A route added or re-sequenced
# since June simply never appeared — which defeats the entire point of an OTA data channel.
# 7 days keeps the politeness the cache was built for (a full cold run is ~11.5k requests) while
# guaranteeing the index can never drift more than a week from the operators' own data.
CACHE_TTL_DAYS = float(os.environ.get("IDX_CACHE_TTL_DAYS", "7"))


def _ttl_for(digest):
    """Per-URL TTL, JITTERED deterministically over [0.5x, 1.5x] of CACHE_TTL_DAYS.

    A flat TTL looks right and behaves badly: every entry is written in the same run, so every entry
    also expires in the same run — one night in seven does a full ~11.5k-request cold fetch while the
    other six do almost nothing. That is slow for us and a thundering herd for free government APIs.
    Jittering by a hash of the URL spreads expiry evenly, so each night refreshes roughly a seventh of
    the index and no single run is ever cold. Deterministic (hash-derived, not random) so a given URL
    keeps a stable schedule instead of jittering afresh on every check.
    """
    frac = int(digest[:8], 16) / 0xFFFFFFFF          # 0.0 … 1.0, stable per URL
    return CACHE_TTL_DAYS * (0.5 + frac) * 86400


# Fetch failures for the operator currently being built. This is what lets save() distinguish an
# AUTHORITATIVE rebuild (fetched everything; removals are real) from a DEGRADED one (an upstream
# outage or IP-ban left holes, so a missing row means "we don't know", not "it's gone").
FETCH_FAILURES = 0


def get(url, tries=4):
    global FETCH_FAILURES
    digest = hashlib.sha1(url.encode()).hexdigest()
    key = os.path.join(CACHE, digest+".json")
    if os.path.exists(key):
        fresh = (time.time() - os.path.getmtime(key)) < _ttl_for(digest)
        if fresh:
            try: return json.load(open(key))
            except Exception: pass
    last=None
    for i in range(tries):
        try:
            req=urllib.request.Request(url, headers={"User-Agent":"Departly-index/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=_ctx) as r:
                d=json.loads(r.read().decode())
            json.dump(d, open(key,"w")); return d
        except Exception as e:
            last=e; time.sleep(1.5*(i+1))
    FETCH_FAILURES += 1
    print("  ! fail", url[:90], last); return None

def pmap(fn, items, workers=None):
    out=[]
    with ThreadPoolExecutor(max_workers=workers or WORKERS) as ex:
        futs={ex.submit(fn,it):it for it in items}
        for n,f in enumerate(as_completed(futs),1):
            r=f.result()
            if r is not None: out.append(r)
            if n%50==0: print(f"  ..{n}/{len(items)}")
    return out

def _dedupe_consecutive(seq):
    """Collapse a stop listed twice in a row (a source-data artifact → a 0-length hop)."""
    out=[]
    for s in seq:
        if not out or out[-1]!=s: out.append(s)
    return out

def validate(op, stops, routes):
    """LOG-ONLY sanity check for genuinely-broken routes — does NOT drop anything, and deliberately does
    NOT flag by distance/span. Many legitimate routes are long with few stops: NT 'residents' express
    green-minibuses (e.g. GMB 610S Tin Shui Wai/Tuen Mun <-> TST, 5 stops over ~27 km, confirmed against
    data.etagmb.gov.hk route_id 2001161 'Normal Route') run express on the highway. Dropping by span would
    delete ~99 real GMB routes. The app's distance-based hop-time floor (raptorFloorMetersPerMin in
    JourneyEngine) already times those long hops realistically, so the data needs no span filtering —
    only true-corruption signals (missing coords / too few stops) are worth a human's eyes."""
    sidset={s["id"]:s for s in stops}
    warn=0
    for r in routes:
        ids=r.get("stops",[])
        named=[s for s in ids if s in sidset and sidset[s].get("lat") and sidset[s].get("lng")]
        rid=f"{r.get('route')}/{r.get('dir')}"
        if len(named)<2:
            print(f"  ! VALIDATE {op} {rid}: <2 stops with coordinates ({len(named)}) — unplannable"); warn+=1
        zc=sum(1 for s in ids if s in sidset and not (sidset[s].get("lat") and sidset[s].get("lng")))
        if zc:
            print(f"  ! VALIDATE {op} {rid}: {zc} stop(s) missing coordinates"); warn+=1
    if warn: print(f"  ({op}: {warn} route(s) flagged for MANUAL REVIEW — none dropped)")

# A rebuild may not drop more than this share of either table before it is refused outright.
# CLAUDE.md §5 documents three layers of defence, and this generator had NONE — it wrote whatever
# came back. The publisher's 10% guard caught the 83% GMB collapse, but that is layer 2 doing
# layer 1's job: by then a corrupt file is already sitting in Departly/Resources, where the next
# app BUILD would happily bundle it even though the publish was refused.
# 30% is deliberately looser than the publisher's 10%, so this only ever fires on the unambiguous
# case and genuine network churn is left for the publisher's dual-baseline check to judge.
MAX_DROP_PCT = float(os.environ.get("IDX_MAX_DROP_PCT", "30"))


def _existing_count(name):
    try:
        with open(os.path.join(OUT, name)) as fh:
            return len(json.load(fh))
    except Exception:
        return None


def _merge_with_previous(op, stops, routes):
    """Fill gaps left by failed fetches with the previous good rows, and say what was carried.

    THE POINT: a stop that returned HTTP 403 is unknown, not deleted. Replacing the file wholesale
    turns a transient upstream ban into permanent data loss — which is exactly what happened when
    etagmb.gov.hk 403'd the CI runner and GMB came back 4,743 -> 4,041 stops. Floors caught the
    damage, but not losing the data is better than detecting that we lost it.

    Only reached when FETCH_FAILURES > 0. A clean rebuild still replaces outright, so genuine
    removals propagate normally and the index cannot silently accumulate dead rows for ever.
    """
    prev_stops = {}
    prev_routes = {}
    try:
        with open(os.path.join(OUT, f"{op}_stops.json")) as fh:
            prev_stops = {s["id"]: s for s in json.load(fh)}
        with open(os.path.join(OUT, f"{op}_routes.json")) as fh:
            prev_routes = {(r["route"], r["dir"], r.get("service", "")): r for r in json.load(fh)}
    except Exception:
        return stops, routes, 0, 0

    have = {s["id"] for s in stops}
    # A row fetched WITHOUT coordinates is also a gap: it is present but unusable, and the previous
    # copy (which had real coordinates) is strictly better than a 0,0 placeholder.
    broken = {s["id"] for s in stops if not (s.get("lat") and s.get("lng"))}
    merged_stops = [s for s in stops if s["id"] not in broken]
    carried_s = 0
    for sid, prev in prev_stops.items():
        if sid not in have or sid in broken:
            if prev.get("lat") and prev.get("lng"):
                merged_stops.append(prev); carried_s += 1

    keys = {(r["route"], r["dir"], r.get("service", "")) for r in routes}
    merged_routes = list(routes)
    carried_r = 0
    for k, prev in prev_routes.items():
        if k not in keys:
            merged_routes.append(prev); carried_r += 1
    return merged_stops, merged_routes, carried_s, carried_r


def save(op, stops, routes):
    for r in routes: r["stops"]=_dedupe_consecutive(r.get("stops",[]))

    if FETCH_FAILURES:
        stops, routes, cs, cr = _merge_with_previous(op, stops, routes)
        print(f"  ~ {op}: {FETCH_FAILURES} fetch failure(s) — carried {cs} stop(s) and {cr} route(s) "
              f"forward from the previous good index rather than dropping them")

    validate(op, stops, routes)

    # Refuse to overwrite a good table with a gutted one. Both files are checked, and BOTH are kept
    # if either fails: a route list without its stops is as useless as the reverse, and writing one
    # of the pair would leave the on-disk index internally inconsistent.
    for name, new in ((f"{op}_stops.json", len(stops)), (f"{op}_routes.json", len(routes))):
        old = _existing_count(name)
        if old and new < old * (1 - MAX_DROP_PCT / 100.0):
            lost = 100 * (1 - new / old)
            raise SystemExit(
                f"ABORT {op}: {name} {old} -> {new} rows ({lost:.1f}% LOST, limit {MAX_DROP_PCT:.0f}%).\n"
                f"  Almost always an upstream outage or rate-limit, not a real network change — "
                f"keeping the previous good files for {op}.\n"
                f"  If the shrink is GENUINE, re-run with IDX_MAX_DROP_PCT=100.")

    def w(name, obj):
        p=os.path.join(OUT,name); tmp=p+".tmp"
        json.dump(obj, open(tmp,"w"), ensure_ascii=False); os.replace(tmp,p)  # atomic
    w(f"{op}_stops.json", stops); w(f"{op}_routes.json", routes)
    print(f"== {op}: {len(stops)} stops, {len(routes)} routes -> {op}_stops.json / {op}_routes.json")

# ---- NLB (coords inline in route-stops) ----
def build_nlb():
    base="https://rt.data.gov.hk/v2/transport/nlb/"
    rl=get(base+"route.php?action=list") or {}
    routes_in=rl.get("routes",[])
    stops={}; routes=[]
    def one(r):
        rid=r.get("routeId")
        d=get(base+f"stop.php?action=list&routeId={rid}") or {}
        seq=[]
        for s in d.get("stops",[]):
            sid=str(s.get("stopId"))
            try: lat=float(s.get("latitude")); lng=float(s.get("longitude"))
            except (TypeError,ValueError): lat=lng=0.0
            stops[sid]={"id":sid,"nameEn":s.get("stopName_e",""),"nameTc":s.get("stopName_c",""),"lat":lat,"lng":lng}
            seq.append(sid)
        # routeName "A > B"; one entry per direction already
        return {"route":r.get("routeNo",""),"dir":str(rid),"service":"","stops":seq}
    routes=pmap(one, routes_in)
    save("nlb", list(stops.values()), routes)

# ---- CTB (route-stop -> per-stop coord) ----
def build_ctb():
    base="https://rt.data.gov.hk/v2/transport/citybus/"
    rl=get(base+"route/CTB") or {}
    routes_in=rl.get("data",[])
    seqs=[]; need=set()
    def rs(args):
        route,d=args
        x=get(base+f"route-stop/CTB/{route}/{d}") or {}
        ids=[e["stop"] for e in x.get("data",[])]
        return (route,d,ids)
    print(f"  CTB route-stops ({len(routes_in)} routes x2)…")
    for route,d,ids in pmap(rs, [(r["route"],dr) for r in routes_in for dr in ("inbound","outbound")]):
        if ids: seqs.append({"route":route,"dir":d,"service":"","stops":ids}); need.update(ids)
    print(f"  CTB unique stops to fetch: {len(need)}")
    stops={}
    def st(sid):
        x=get(base+f"stop/{sid}") or {}; dd=x.get("data",{})
        try: lat=float(dd.get("lat")); lng=float(dd.get("long"))
        except (TypeError,ValueError): lat=lng=0.0
        return {"id":sid,"nameEn":dd.get("name_en",""),"nameTc":dd.get("name_tc",""),"lat":lat,"lng":lng}
    for s in pmap(st, sorted(need)): stops[s["id"]]=s
    save("ctb", list(stops.values()), seqs)

# ---- GMB (route -> route-stop per seq -> per-stop coord) ----
def build_gmb():
    base="https://data.etagmb.gov.hk/"
    rl=get(base+"route") or {}
    regions=rl.get("data",{}).get("routes",{})
    # resolve each (region,code) -> route_id(s)
    pairs=[(reg,code) for reg,codes in regions.items() for code in codes]
    print(f"  GMB resolving {len(pairs)} (region,code)…")
    def resolve(rc):
        reg,code=rc
        x=get(base+f"route/{reg}/{code}") or {}
        return [(reg,code,r.get("route_id"),r.get("directions",[])) for r in x.get("data",[])]
    resolved=[item for sub in pmap(resolve,pairs,workers=GMB_WORKERS) for item in sub]
    seqs=[]; names={}     # GMB stop NAMES live in the route-stop response (not the stop/coord one)
    def rs(item):
        reg,code,rid,dirs=item; out=[]; nm={}
        for d in dirs:
            seq=d.get("route_seq")
            x=get(base+f"route-stop/{rid}/{seq}") or {}
            ids=[]
            for s in x.get("data",{}).get("route_stops",[]):
                sid=str(s.get("stop_id")); ids.append(sid)
                nm[sid]=(s.get("name_en") or "", s.get("name_tc") or "")
            if ids: out.append({"route":code,"dir":f"{rid}-{seq}","service":reg,"stops":ids})
        return {"seqs":out,"names":nm}
    print(f"  GMB route-stops ({len(resolved)} routes)…")
    for r in pmap(rs,resolved,workers=GMB_WORKERS):
        seqs.extend(r["seqs"]); names.update(r["names"])
    need=sorted(names.keys())
    print(f"  GMB unique stops to fetch: {len(need)}")
    stops={}
    def st(sid):
        x=get(base+f"stop/{sid}") or {}; dd=x.get("data",{})
        c=dd.get("coordinates",{}).get("wgs84",{})
        try: lat=float(c.get("latitude")); lng=float(c.get("longitude"))
        except (TypeError,ValueError): lat=lng=0.0
        en,tc=names.get(sid,("",""))
        return {"id":sid,"nameEn":en,"nameTc":tc,"lat":lat,"lng":lng}
    for s in pmap(st, need, workers=GMB_WORKERS): stops[s["id"]]=s
    save("gmb", list(stops.values()), seqs)

# ---- MTR Bus (MTR-operated feeder buses; CSVs already carry coords) ----
def build_mtrbus():
    import csv as _csv, io
    def csv_rows(name):
        req = urllib.request.Request("https://opendata.mtr.com.hk/data/" + name,
                                     headers={"User-Agent": "Departly-index/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=_ctx) as r:
            text = r.read().decode("utf-8-sig")
        return list(_csv.reader(io.StringIO(text)))
    srows = csv_rows("mtr_bus_stops.csv")           # ROUTE_ID,DIR,SEQNO,STATION_ID,LAT,LNG,CHI,ENG,REFERENCE_ID
    # One linear variant = (REFERENCE_ID, DIRECTION) — REFERENCE_ID splits a route's sub-variants
    # (K66, K66-1, K66-2…), each its own SEQNO. (csv.reader handles names with embedded commas.)
    stops = {}; seqs = {}; routeOf = {}
    for row in srows[1:]:
        if len(row) < 8: continue
        rid, d, seqno, sid = row[0], row[1], row[2], row[3]
        try: lat = float(row[4]); lng = float(row[5])
        except (ValueError, IndexError): lat = lng = 0.0
        tc, en = row[6].strip(), row[7].strip()
        if not sid: continue
        ref = (row[8].strip() if len(row) > 8 and row[8].strip() else rid)
        key = (ref, d)
        if sid not in stops or (en and not stops[sid]["nameEn"]):   # keep a named version over a blank one
            stops[sid] = {"id": sid, "nameEn": en, "nameTc": tc, "lat": lat, "lng": lng}
        routeOf[key] = rid
        seqs.setdefault(key, []).append((float(seqno or 0), sid))
    routes = [{"route": routeOf[k], "dir": f"{k[0]}|{k[1]}", "service": "", "stops": [s for _, s in sorted(v)]}
              for k, v in seqs.items()]
    save("mtrbus", list(stops.values()), routes)

# ---- KMB (global stop list + route-stop sequences) ----
def build_kmb():
    base="https://data.etabus.gov.hk/v1/transport/kmb/"
    sl=get(base+"stop") or {}
    stops=[{"id":s["stop"],"nameEn":s.get("name_en",""),"nameTc":s.get("name_tc",""),
            "lat":float(s.get("lat") or 0),"lng":float(s.get("long") or 0)} for s in sl.get("data",[])]
    rl=get(base+"route") or {}
    routes_in=rl.get("data",[])
    def rs(r):
        bound = "outbound" if r.get("bound")=="O" else "inbound"   # KMB API wants full word, not O/I
        x=get(base+f"route-stop/{r['route']}/{bound}/{r['service_type']}") or {}
        ids=[e["stop"] for e in x.get("data",[])]
        return {"route":r["route"],"dir":r["bound"],"service":r["service_type"],"stops":ids} if ids else None
    print(f"  KMB route-stops ({len(routes_in)} routes)…")
    routes=[r for r in pmap(rs,routes_in) if r]
    save("kmb", stops, routes)

if __name__=="__main__":
    which=(sys.argv[1] if len(sys.argv)>1 else "all").lower()
    jobs={"nlb":build_nlb,"ctb":build_ctb,"gmb":build_gmb,"kmb":build_kmb,"mtrbus":build_mtrbus}
    targets=list(jobs.keys()) if which=="all" else [which]
    # Per-operator isolation. The floor in save() raises, and letting that propagate would abort the
    # whole run — so one rate-limited operator (GMB, reliably) would stop KMB and MTR Bus refreshing
    # at all, turning a partial outage into a total one. Each operator is contained; the failures are
    # counted; the exit code reports them so regenerate_and_publish.sh can mark the night DEGRADED.
    failed=[]
    for t in targets:
        print(f"### building {t} ###")
        FETCH_FAILURES = 0          # per-operator: one operator's outage must not taint the next
        try:
            jobs[t]()
        except SystemExit as e:
            print(f"  ! {t} REFUSED: {e}"); failed.append(t)
        except Exception as e:
            print(f"  ! {t} FAILED: {e.__class__.__name__}: {e}"); failed.append(t)
    if failed:
        print(f"DONE with {len(failed)} operator(s) kept at last-good: {', '.join(failed)}")
        sys.exit(1)
    print("DONE")
