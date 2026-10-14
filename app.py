"""
เครื่องมือ SEO ส่วนตัว (คล้าย Ubersuggest) แบบหลายโปรเจ็กต์
 - 1 โปรเจ็กต์ = 1 ธุรกิจ: เว็บของเรา + คู่แข่งหลายเว็บ + ประเทศ/ภาษา
 - หาคีย์เวิร์ด (Autocomplete + volume/CPC) แล้วบันทึกเก็บไว้ต่อโปรเจ็กต์
 - เปรียบเทียบคู่แข่ง (on-page + keyword gap) และเก็บประวัติทุกครั้งที่กด
ข้อมูลเก็บในไฟล์ seo.db ข้างๆ app.py

รัน:  pip install -r requirements.txt
      export DATAFORSEO_LOGIN=...  DATAFORSEO_PASSWORD=...
      python app.py   ->  เปิด http://127.0.0.1:5000
      (ตั้ง APP_PASSWORD ถ้าอยากให้ถามรหัสผ่านก่อนเข้าเว็บ ส่วนขึ้น Railway ดู README.md)
"""
import csv, hmac, io, json, os, re, sqlite3, threading, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN")
DFS_PASS = os.getenv("DATAFORSEO_PASSWORD")
APP_USER = os.getenv("APP_USER") or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD")
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Bangkok")  # เซิร์ฟเวอร์ Railway เป็น UTC ตั้งเวลาไทยไว้ให้ตารางรันตรงชั่วโมง
UA = {"User-Agent": "Mozilla/5.0 (compatible; MySEOTool/1.0)"}
# บน Railway ให้เก็บ seo.db ไว้ใน Volume (Railway ตั้ง RAILWAY_VOLUME_MOUNT_PATH ให้อัตโนมัติ) ไม่งั้นข้อมูลหายตอน deploy
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("SEO_DB_PATH") or os.path.join(DATA_DIR, "seo.db")
os.makedirs(os.path.dirname(DB), exist_ok=True)
MAX_RIVALS = 5
MAX_TRACK = 100                                      # คีย์เวิร์ดที่ติดตามอันดับได้ต่อโปรเจ็กต์
RANK_DEPTH = int(os.getenv("SEO_RANK_DEPTH", "50"))  # ค้นหาอันดับลึกกี่อันดับ (ยิ่งลึกยิ่งแพง)

app = Flask(__name__)


# ---------- รหัสผ่านเข้าเว็บ ----------
@app.before_request
def require_login():
    if request.path == "/healthz":  # ให้ healthcheck ของ Railway ผ่านได้โดยไม่ต้องใช้รหัส
        return None
    if not APP_PASSWORD:
        if ON_RAILWAY:  # กันลืมตั้งรหัสแล้วเว็บเปิดโล่ง
            return Response("ยังไม่ได้ตั้ง APP_PASSWORD ใน Variables ของ Railway", 503,
                            mimetype="text/plain; charset=utf-8")
        return None  # รันในเครื่องตัวเอง ไม่ต้องใช้รหัส
    a = request.authorization
    if a and hmac.compare_digest((a.username or "").encode(), APP_USER.encode()) \
            and hmac.compare_digest((a.password or "").encode(), APP_PASSWORD.encode()):
        return None
    return Response("ต้องใส่รหัสผ่าน", 401, {"WWW-Authenticate": 'Basic realm="SEO", charset="UTF-8"'},
                    mimetype="text/plain; charset=utf-8")


# ---------- ฐานข้อมูล ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


with db() as _c:
    _c.executescript("""
    create table if not exists projects(id integer primary key autoincrement, name text, domain text, location int, lang text);
    create table if not exists competitors(id integer primary key autoincrement, project_id int references projects(id) on delete cascade, domain text);
    create table if not exists saved_keywords(project_id int references projects(id) on delete cascade, keyword text, volume int, cpc real, created text default current_timestamp, primary key(project_id, keyword));
    create table if not exists snapshots(id integer primary key autoincrement, project_id int references projects(id) on delete cascade, created text default current_timestamp, data text);
    create table if not exists audits(id integer primary key autoincrement, project_id int references projects(id) on delete cascade, created text default current_timestamp, data text);
    create table if not exists tracked_keywords(project_id int references projects(id) on delete cascade, keyword text, primary key(project_id, keyword));
    create table if not exists ranks(project_id int references projects(id) on delete cascade, keyword text, domain text, day text, pos int, primary key(project_id, keyword, domain, day));
    create table if not exists actions_done(project_id int references projects(id) on delete cascade, key text, primary key(project_id, key));
    create table if not exists toppages(project_id int references projects(id) on delete cascade, domain text, data text, created text, primary key(project_id, domain));
    create table if not exists schedules(project_id int primary key references projects(id) on delete cascade, audit_every int default 0, rank_every int default 0, hour int default 3, last_audit text, last_rank text, last_msg text);
    """)


def norm_url(u):
    u = u.strip()
    return u if u.startswith("http") else "https://" + u


def host(u):
    return urlparse(norm_url(u)).netloc.replace("www.", "").lower()


def get_project(pid):
    with db() as c:
        p = c.execute("select * from projects where id=?", (pid,)).fetchone()
        if not p:
            return None
        rivals = [r["domain"] for r in c.execute("select domain from competitors where project_id=? order by id", (pid,))]
    return {**dict(p), "competitors": rivals}


def save_project(pid, d):
    name, domain = (d.get("name") or "").strip(), host(d.get("domain") or "")
    if not name or not domain:
        raise ValueError("กรอกชื่อโปรเจ็กต์และเว็บของคุณ")
    rivals = []
    for line in re.split(r"[\n,]+", d.get("competitors") or ""):
        h = host(line) if line.strip() else ""
        if h and h != domain and h not in rivals:
            rivals.append(h)
    with db() as c:
        vals = (name, domain, int(d.get("location") or 2764), d.get("lang") or "th")
        if pid:
            c.execute("update projects set name=?,domain=?,location=?,lang=? where id=?", vals + (pid,))
            c.execute("delete from competitors where project_id=?", (pid,))
        else:
            pid = c.execute("insert into projects(name,domain,location,lang) values(?,?,?,?)", vals).lastrowid
        c.executemany("insert into competitors(project_id,domain) values(?,?)", [(pid, r) for r in rivals[:MAX_RIVALS]])
    return pid


@app.get("/api/projects")
def projects_list():
    with db() as c:
        ids = [r["id"] for r in c.execute("select id from projects order by name")]
    return jsonify([get_project(i) for i in ids])


@app.post("/api/projects")
@app.put("/api/projects/<int:pid>")
def projects_save(pid=None):
    try:
        return jsonify(id=save_project(pid, request.json))
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.delete("/api/projects/<int:pid>")
def projects_delete(pid):
    with db() as c:
        c.execute("delete from projects where id=?", (pid,))
    return jsonify(ok=True)


# ---------- DataForSEO ----------
def dfs_post(path, payload):
    if not (DFS_LOGIN and DFS_PASS):
        raise RuntimeError("ยังไม่ได้ตั้งค่า DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD")
    r = requests.post("https://api.dataforseo.com/v3/" + path, json=[payload],
                      auth=(DFS_LOGIN, DFS_PASS), timeout=60)
    if r.status_code == 401:
        raise RuntimeError("DataForSEO ปฏิเสธ login/password (401) ต้องใช้ API login และ API password จากหน้า API Access ไม่ใช่รหัสล็อกอินเว็บ")
    r.raise_for_status()
    task = r.json()["tasks"][0]
    if task["status_code"] != 20000:
        raise RuntimeError(task["status_message"])
    return task["result"]


def volumes(keywords, loc, lang):
    out = {}
    for i in range(0, len(keywords), 700):
        res = dfs_post("keywords_data/google_ads/search_volume/live", {
            "keywords": keywords[i:i + 700], "location_code": loc, "language_code": lang})
        for it in res or []:
            out[it["keyword"]] = {"volume": it.get("search_volume") or 0, "cpc": it.get("cpc") or 0,
                                  "competition": it.get("competition") or ""}
    return out


def gap(me, rival, loc, lang):
    res = dfs_post("dataforseo_labs/google/domain_intersection/live", {
        "target1": me, "target2": rival, "location_code": loc, "language_code": lang,
        "intersections": False, "limit": 300,
        "order_by": ["keyword_data.keyword_info.search_volume,desc"]})
    rows = []
    for it in (res[0].get("items") or []) if res else []:
        kd = it["keyword_data"]
        a, b = it.get("first_domain_serp_element"), it.get("second_domain_serp_element")
        mine, theirs = a and a.get("rank_absolute"), b and b.get("rank_absolute")
        rows.append({"keyword": kd["keyword"], "volume": kd["keyword_info"].get("search_volume") or 0,
                     "cpc": kd["keyword_info"].get("cpc") or 0, "me": mine, "rival": theirs,
                     "kind": "both" if mine and theirs else ("gap" if theirs else "mine")})
    return rows


@app.get("/api/dfs/status")
def dfs_status():
    if not (DFS_LOGIN and DFS_PASS):
        return jsonify(ok=False, message="ยังไม่ได้ตั้ง DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD " + (
            "(ตั้งในแท็บ Variables ของ Railway แล้วกด Deploy)" if ON_RAILWAY else "(ต้องตั้งใน terminal เดียวกับที่รัน app.py)"))
    try:
        r = requests.get("https://api.dataforseo.com/v3/appendix/user_data", auth=(DFS_LOGIN, DFS_PASS), timeout=15)
        if r.status_code == 401:
            return jsonify(ok=False, message="login/password ไม่ถูกต้อง ต้องใช้ API login และ API password จากหน้า API Access")
        r.raise_for_status()
        money = r.json()["tasks"][0]["result"][0].get("money") or {}
        return jsonify(ok=True, balance=money.get("balance"))
    except Exception as e:
        return jsonify(ok=False, message=f"เชื่อมต่อไม่ได้: {e}")


# ---------- Google Search Console (ฟรี ใช้ได้เฉพาะเว็บของเราเอง) ----------
# ใส่ไฟล์ JSON ของ service account ทั้งไฟล์ใน GSC_CREDENTIALS_JSON แล้วเพิ่มอีเมล service account เป็นผู้ใช้ใน Search Console
GSC_JSON = os.getenv("GSC_CREDENTIALS_JSON")
GSC_API = "https://www.googleapis.com/webmasters/v3/"
_gsc = {}


def gsc_session():
    if not GSC_JSON:
        raise RuntimeError("ยังไม่ได้ตั้ง GSC_CREDENTIALS_JSON")
    if "s" not in _gsc:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GSC_JSON), scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
        _gsc["s"], _gsc["email"] = AuthorizedSession(creds), creds.service_account_email
    return _gsc["s"]


def gsc_site(domain):
    """หา property ใน Search Console ที่ตรงกับโดเมน (รองรับทั้งแบบ Domain และ URL prefix)"""
    r = gsc_session().get(GSC_API + "sites", timeout=20)
    r.raise_for_status()
    sites = [s["siteUrl"] for s in r.json().get("siteEntry", []) if s.get("permissionLevel") != "siteUnverifiedUser"]
    for s in sites:
        if (s[len("sc-domain:"):] if s.startswith("sc-domain:") else host(s)) == domain:
            return s
    raise RuntimeError(f"Search Console ยังไม่ได้ให้สิทธิ์เว็บ {domain} กับ {_gsc.get('email')} "
                       "(เพิ่มอีเมลนี้เป็นผู้ใช้ใน Search Console > การตั้งค่า > ผู้ใช้และสิทธิ์)")


def gsc_query(site, body):
    r = gsc_session().post(GSC_API + "sites/" + requests.utils.quote(site, safe="") + "/searchAnalytics/query",
                           json=body, timeout=60)
    if r.status_code == 403:
        raise RuntimeError(f"Search Console ไม่อนุญาต ({_gsc.get('email')} ต้องเป็นผู้ใช้ของเว็บนี้)")
    r.raise_for_status()
    return r.json().get("rows") or []


def gsc_history(domain):
    """คลิกรายเดือนย้อนหลัง (ไม่นับเดือนปัจจุบันที่ยังไม่จบ) ในรูปแบบเดียวกับ overview()"""
    site, today = gsc_site(domain), datetime.now(TZ).date()
    start = today.replace(day=1, year=today.year - 1)  # วันที่ 1 ของเดือนเดียวกันปีก่อน = ครบ 12 เดือนเต็ม
    rows = gsc_query(site, {"startDate": start.isoformat(), "endDate": today.isoformat(), "dimensions": ["date"], "rowLimit": 25000})
    months = {}
    for r in rows:
        ym = r["keys"][0][:7]
        months[ym] = months.get(ym, 0) + r["clicks"]
    this = today.strftime("%Y-%m")
    return [{"ym": ym, "etv": round(v), "count": None} for ym, v in sorted(months.items()) if ym != this][-12:]


def gsc_pages(domain, limit=20):
    """หน้าที่ได้คลิกมากที่สุด 28 วันล่าสุด + จำนวนคีย์เวิร์ดที่พาคนเข้าแต่ละหน้า"""
    site, today = gsc_site(domain), datetime.now(TZ).date()
    rng = {"startDate": date.fromordinal(today.toordinal() - 28).isoformat(), "endDate": today.isoformat()}
    pages = gsc_query(site, {**rng, "dimensions": ["page"], "rowLimit": limit})
    kw = {}
    for r in gsc_query(site, {**rng, "dimensions": ["page", "query"], "rowLimit": 25000}):
        kw[r["keys"][0]] = kw.get(r["keys"][0], 0) + 1
    return [{"url": r["keys"][0], "traffic": round(r["clicks"]), "keywords": kw.get(r["keys"][0], 0),
             "impressions": round(r["impressions"]), "position": round(r["position"], 1), "src": "gsc"} for r in pages]


@app.get("/api/gsc/status")
def gsc_status():
    if not GSC_JSON:
        return jsonify(ok=False, message="ยังไม่ได้ตั้ง GSC_CREDENTIALS_JSON " + (
            "(ตั้งในแท็บ Variables ของ Railway แล้วกด Deploy)" if ON_RAILWAY else "(ต้องตั้งใน terminal เดียวกับที่รัน app.py)"))
    p = get_project(request.args.get("project_id"))
    try:
        gsc_session()
        if p:
            gsc_site(p["domain"])
        return jsonify(ok=True, email=_gsc.get("email"))
    except Exception as e:
        return jsonify(ok=False, message=str(e)[:300])


# ---------- คีย์เวิร์ด ----------
def suggest(args):
    q, lang = args
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search",
                         params={"client": "firefox", "q": q, "hl": lang}, headers=UA, timeout=10)
        return r.json()[1]
    except Exception:
        return []


def expand(seed, lang):
    letters = "กขคงจชซดตถทนบปผพฟมยรลวศสหอ" if lang == "th" else "abcdefghijklmnopqrstuvwxyz"
    queries = [(seed, lang)] + [(f"{seed} {s}", lang) for s in letters]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(suggest, queries))
    seen, out = set(), []
    for lst in results:
        for k in lst:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


@app.post("/api/keywords")
def api_keywords():
    p = get_project(request.json.get("project_id"))
    seed = (request.json.get("seed") or "").strip()
    if not p or not seed:
        return jsonify(error="เลือกโปรเจ็กต์และกรอกคำค้นก่อน"), 400
    kws = expand(seed, p["lang"])[:300]
    rows, note = [{"keyword": k, "volume": None, "cpc": None, "competition": ""} for k in kws], ""
    try:
        v = volumes(kws, p["location"], p["lang"])
        for r in rows:
            r.update(v.get(r["keyword"], {"volume": 0, "cpc": 0, "competition": ""}))
        rows.sort(key=lambda r: r["volume"] or 0, reverse=True)
    except Exception as e:
        note = f"แสดงเฉพาะไอเดียคีย์เวิร์ด ({e})"
    return jsonify(rows=rows, note=note)


@app.post("/api/keywords/save")
def kw_save():
    pid, rows = request.json.get("project_id"), request.json.get("rows") or []
    with db() as c:
        c.executemany("insert or replace into saved_keywords(project_id,keyword,volume,cpc) values(?,?,?,?)",
                      [(pid, r["keyword"], r.get("volume"), r.get("cpc")) for r in rows])
    return jsonify(saved=len(rows))


@app.get("/api/keywords/saved")
def kw_saved():
    with db() as c:
        rows = c.execute("select keyword,volume,cpc,created from saved_keywords where project_id=? order by volume desc",
                         (request.args.get("project_id"),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/keywords/delete")
def kw_delete():
    with db() as c:
        c.execute("delete from saved_keywords where project_id=? and keyword=?",
                  (request.json["project_id"], request.json["keyword"]))
    return jsonify(ok=True)


@app.get("/api/keywords/export.csv")
def kw_export():
    with db() as c:
        rows = c.execute("select keyword,volume,cpc,created from saved_keywords where project_id=? order by volume desc",
                         (request.args.get("project_id"),)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["keyword", "volume", "cpc", "saved_at"])
    w.writerows([tuple(r) for r in rows])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=keywords.csv"})


# ---------- เปรียบเทียบ ----------
def audit(url):
    t0 = time.time()
    r = requests.get(url, headers=UA, timeout=20)
    secs = time.time() - t0
    s = BeautifulSoup(r.text, "html.parser")
    h = urlparse(r.url).netloc
    meta = s.find("meta", attrs={"name": "description"})
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    links = [a.get("href", "") for a in s.find_all("a", href=True)]
    internal = [x for x in links if x.startswith("/") or h in x]
    imgs = s.find_all("img")
    title = s.title.string.strip() if s.title and s.title.string else ""
    desc = (meta.get("content") or "").strip() if meta else ""
    return {
        "Title": title, "ความยาว Title": len(title),
        "Meta description": desc, "ความยาว Meta": len(desc),
        "H1": len(s.find_all("h1")), "H2": len(s.find_all("h2")),
        "จำนวนคำในหน้า": len(re.findall(r"\S+", s.get_text(" "))),
        "รูปภาพ": len(imgs), "รูปที่ไม่มี alt": sum(1 for i in imgs if not i.get("alt")),
        "ลิงก์ภายใน": len(internal), "ลิงก์ภายนอก": len(links) - len(internal),
        "Schema (JSON-LD)": "มี" if s.find("script", type="application/ld+json") else "ไม่มี",
        "Open Graph": "มี" if s.find("meta", property="og:title") else "ไม่มี",
        "HTTPS": "ใช่" if r.url.startswith("https") else "ไม่",
        "เวลาตอบสนอง (วินาที)": round(secs, 2), "ขนาด HTML (KB)": round(len(r.content) / 1024),
    }


def audit_safe(d):
    try:
        return {"domain": d, "audit": audit(norm_url(d))}
    except Exception as e:
        return {"domain": d, "error": str(e)[:120]}


def overview(domain, loc, lang):
    """ทราฟฟิก organic รายเดือนย้อนหลัง 12 เดือน + backlinks ของเว็บหนึ่ง"""
    o = {"history": [], "traffic": None, "keywords": None, "backlinks": None, "error": None}
    try:
        res = dfs_post("dataforseo_labs/google/historical_rank_overview/live",
                       {"target": domain, "location_code": loc, "language_code": lang})
        hist = []
        for i in (res[0].get("items") or []) if res else []:
            org = (i.get("metrics") or {}).get("organic") or {}
            hist.append({"ym": f"{i['year']}-{int(i['month']):02d}", "etv": round(org.get("etv") or 0),
                         "count": org.get("count") or 0})
        hist.sort(key=lambda x: x["ym"])
        o["history"] = hist[-12:]
        if hist:
            o["traffic"], o["keywords"] = hist[-1]["etv"], hist[-1]["count"]
        else:
            o["error"] = "DataForSEO ไม่มีข้อมูลทราฟฟิกของเว็บนี้ (เว็บเล็กหรือใหม่มักยังไม่ถูกเก็บ)"
    except Exception as e:
        o["error"] = str(e)[:200]
    try:  # ต้องมีสิทธิ์ Backlinks API ถ้าไม่มีจะเป็น None
        o["backlinks"] = dfs_post("backlinks/summary/live", {"target": domain})[0].get("backlinks")
    except Exception:
        pass
    return o


@app.post("/api/compare")
def api_compare():
    p = get_project(request.json.get("project_id"))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    if not p["competitors"]:
        return jsonify(error="ยังไม่มีเว็บคู่แข่ง กด 'แก้ไข' เพื่อเพิ่มก่อน"), 400
    domains = [p["domain"]] + p["competitors"]
    with ThreadPoolExecutor(4) as ex:
        sites = list(ex.map(audit_safe, domains))
        ov = dict(zip(domains, ex.map(lambda d: overview(d, p["location"], p["lang"]), domains)))
    gaps, note = {}, ""
    for r in p["competitors"]:
        try:
            gaps[r] = gap(p["domain"], r, p["location"], p["lang"])
        except Exception as e:
            note = f"ข้าม keyword gap: {e}"
            break
    if GSC_JSON:  # เว็บของเราใช้คลิกจริงจาก Search Console แทนค่าประมาณ (ฟรี)
        mine = ov[p["domain"]]
        try:
            hist = gsc_history(p["domain"])
            if hist:
                mine.update(history=hist, traffic=hist[-1]["etv"], error=None, source="gsc")
        except Exception as e:
            mine["error"] = (mine["error"] + " / " if mine.get("error") else "") + f"Search Console: {str(e)[:200]}"
    data = {"sites": sites, "gaps": gaps, "gap_note": note, "overview": ov}
    with db() as c:
        sid = c.execute("insert into snapshots(project_id,data) values(?,?)", (p["id"], json.dumps(data))).lastrowid
    return jsonify(id=sid, data=data)


@app.get("/api/snapshots")
def snap_list():
    with db() as c:
        rows = c.execute("select id,created from snapshots where project_id=? order by id desc limit 50",
                         (request.args.get("project_id"),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/snapshots/<int:sid>")
def snap_get(sid):
    with db() as c:
        r = c.execute("select data from snapshots where id=?", (sid,)).fetchone()
    return jsonify(data=json.loads(r["data"])) if r else (jsonify(error="ไม่พบข้อมูล"), 404)


# ---------- Site Audit ----------
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|rar|mp4|mp3|css|js|ico|xml|docx?|xlsx?)(\?|$)", re.I)
ISSUES = [  # (id, ชื่อปัญหา, วิธีแก้)
    ("broken", "ลิงก์เสีย (ตอบกลับ 4xx/5xx)", "ลิงก์ในเว็บชี้ไปหน้าที่เปิดไม่ได้ แก้ลิงก์หรือทำ redirect ไปหน้าใหม่"),
    ("blocked", "หน้าถูกบล็อกไม่ให้ขึ้นใน Google (robots.txt / noindex)", "ถ้าไม่ได้ตั้งใจบล็อก ให้แก้ robots.txt หรือเอา noindex ออก"),
    ("no_title", "ไม่มี <title>", "ใส่ title ที่มีคีย์เวิร์ดหลักของหน้า"),
    ("dup_title", "<title> ซ้ำกับหน้าอื่น", "ตั้ง title ให้ไม่ซ้ำกันในแต่ละหน้า"),
    ("title_len", "<title> ยาวเกิน 65 หรือสั้นกว่า 10 ตัวอักษร", "ปรับให้อยู่ราว 30-65 ตัวอักษรเพื่อไม่ถูกตัดในผลค้นหา"),
    ("no_desc", "ไม่มี meta description", "เขียนคำอธิบายหน้า 70-160 ตัวอักษร ช่วยให้คนกดจากผลค้นหามากขึ้น"),
    ("no_h1", "ไม่มีหัวข้อ H1", "ใส่ H1 หนึ่งอันต่อหน้า บอกหัวข้อหลักของหน้านั้น"),
    ("multi_h1", "มี H1 มากกว่า 1 อัน", "เหลือ H1 เดียว ที่เหลือเปลี่ยนเป็น H2/H3"),
    ("img_alt", "มีรูปภาพที่ไม่มี alt", "ใส่ alt อธิบายรูป ช่วยทั้ง SEO และผู้ใช้ที่มองไม่เห็น"),
    ("thin", "เนื้อหาน้อยมาก (ต่ำกว่า ~600 ตัวอักษร)", "เพิ่มเนื้อหาที่เป็นประโยชน์ หรือรวมหน้าที่คล้ายกัน"),
    ("slow", "โหลดช้ากว่า 3 วินาที", "บีบอัดรูป ใช้ cache/CDN และลดสคริปต์ที่ไม่จำเป็น"),
]
WEIGHT = {"broken": 3, "blocked": 2, "no_title": 3, "dup_title": 2, "title_len": 1, "no_desc": 2,
          "no_h1": 2, "multi_h1": 1, "img_alt": 1, "thin": 1, "slow": 1}


def load_robots(base):
    rp = RobotFileParser()
    try:
        r = requests.get(base + "/robots.txt", headers=UA, timeout=10)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    rp.modified()  # ไม่เรียกจะ can_fetch ได้ False ทุกหน้า
    return rp


def fetch_page(args):
    url, rp = args
    if not rp.can_fetch("*", url):
        return {"url": url, "status": "blocked", "code": None, "links": []}
    t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=15)
    except Exception:
        return {"url": url, "status": "broken", "code": 0, "links": []}
    page = {"url": url, "code": r.status_code, "links": [], "secs": round(time.time() - t0, 2)}
    if r.status_code >= 400:
        page["status"] = "broken"
        return page
    page["status"] = "redirected" if r.history else "success"
    if "html" not in r.headers.get("content-type", ""):
        return page
    s = BeautifulSoup(r.text, "html.parser")
    meta = s.find("meta", attrs={"name": "description"})
    robots = s.find("meta", attrs={"name": "robots"})
    imgs = s.find_all("img")
    links = [urldefrag(urljoin(r.url, a["href"]))[0] for a in s.find_all("a", href=True)]
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    page.update(html=True,
                title=s.title.string.strip() if s.title and s.title.string else "",
                desc=bool(meta and (meta.get("content") or "").strip()),
                h1=len(s.find_all("h1")), noalt=sum(1 for i in imgs if not i.get("alt")),
                chars=len(re.sub(r"\s+", "", s.get_text())),
                noindex="noindex" in ((robots.get("content") or "").lower() if robots else ""),
                links=[l for l in links if urlparse(l).scheme in ("http", "https")])
    return page


def crawl(domain, limit):
    base = "https://" + domain
    rp = load_robots(base)
    seen, queue, pages = {base}, [base], []
    while queue and len(pages) < limit:
        n = min(8, limit - len(pages))
        batch, queue = queue[:n], queue[n:]
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(fetch_page, [(u, rp) for u in batch]))
        for p in res:
            pages.append(p)
            for l in p.pop("links"):
                l = l.rstrip("/") or l
                if l not in seen and host(l) == domain and not SKIP_EXT.search(l):
                    seen.add(l)
                    queue.append(l)
    return pages


def analyse(pages):
    titles = {}
    for p in pages:
        if p.get("html") and p.get("title"):
            titles[p["title"]] = titles.get(p["title"], 0) + 1
    found = {k: [] for k in WEIGHT}
    for p in pages:
        ids = []
        if p["status"] == "broken":
            ids.append("broken")
        if p["status"] == "blocked" or p.get("noindex"):
            ids.append("blocked")
        if p.get("html"):
            t = p["title"]
            if not t:
                ids.append("no_title")
            else:
                if titles[t] > 1:
                    ids.append("dup_title")
                if len(t) > 65 or len(t) < 10:
                    ids.append("title_len")
            if not p["desc"]:
                ids.append("no_desc")
            if p["h1"] == 0:
                ids.append("no_h1")
            if p["h1"] > 1:
                ids.append("multi_h1")
            if p["noalt"]:
                ids.append("img_alt")
            if p["chars"] < 600:
                ids.append("thin")
            if p["secs"] > 3:
                ids.append("slow")
        p["issues"] = ids
        for i in ids:
            found[i].append(p["url"])
    counts = {k: sum(1 for p in pages if p["status"] == k) for k in ("success", "redirected", "broken", "blocked")}
    checked = max(1, sum(1 for p in pages if p.get("html")))
    penalty = sum(WEIGHT[k] * len(v) for k, v in found.items()) / checked * 10
    issues = [{"id": k, "label": L, "tip": T, "count": len(found[k]), "urls": found[k][:200]}
              for k, L, T in ISSUES if found[k]]
    issues.sort(key=lambda x: WEIGHT[x["id"]] * x["count"], reverse=True)
    slim = [{"url": p["url"], "status": p["status"], "code": p["code"], "title": (p.get("title") or "")[:100],
             "issues": p["issues"]} for p in pages]
    return {"score": max(0, round(100 - penalty)), "counts": counts, "pages": slim, "issues": issues,
            "total_issues": sum(len(v) for v in found.values())}


def site_metrics(domain, loc, lang):
    m = {"traffic": None, "keywords": None, "backlinks": None}
    try:
        res = dfs_post("dataforseo_labs/google/domain_rank_overview/live",
                       {"target": domain, "location_code": loc, "language_code": lang})
        o = res[0]["items"][0]["metrics"].get("organic") or {}
        m["traffic"], m["keywords"] = round(o.get("etv") or 0), o.get("count") or 0
    except Exception:
        pass
    try:  # Backlinks API ของ DataForSEO อาจต้องสมัครแยก ถ้าไม่มีจะแสดง "-"
        m["backlinks"] = dfs_post("backlinks/summary/live", {"target": domain})[0].get("backlinks")
    except Exception:
        pass
    return m


@app.post("/api/audit/run")
def audit_run():
    p = get_project(request.json.get("project_id"))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    limit = max(10, min(150, int(request.json.get("limit") or 50)))
    data = analyse(crawl(p["domain"], limit))
    data["metrics"] = site_metrics(p["domain"], p["location"], p["lang"])
    with db() as c:
        sid = c.execute("insert into audits(project_id,data) values(?,?)", (p["id"], json.dumps(data))).lastrowid
        created = c.execute("select created from audits where id=?", (sid,)).fetchone()[0]
    return jsonify(data=data, created=created)


@app.get("/api/audit/latest")
def audit_latest():
    with db() as c:
        r = c.execute("select data,created from audits where project_id=? order by id desc limit 1",
                      (request.args.get("project_id"),)).fetchone()
    return jsonify(data=json.loads(r["data"]), created=r["created"]) if r else jsonify(data=None)


@app.get("/api/audit/export.csv")
def audit_export():
    with db() as c:
        r = c.execute("select data from audits where project_id=? order by id desc limit 1",
                      (request.args.get("project_id"),)).fetchone()
    labels = {k: l for k, l, _ in ISSUES}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["url", "status", "http_code", "title", "issues"])
    for p in (json.loads(r["data"])["pages"] if r else []):
        w.writerow([p["url"], p["status"], p["code"], p["title"], " | ".join(labels[i] for i in p["issues"])])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=site-audit.csv"})


# ---------- Rank Tracking + Dashboard ----------
def serp_positions(keyword, domains, loc, lang):
    """อันดับ organic ของแต่ละโดเมนใน Google สำหรับคีย์เวิร์ดนี้ (None = ไม่ติดใน RANK_DEPTH อันดับแรก)"""
    res = dfs_post("serp/google/organic/live/regular", {"keyword": keyword, "location_code": loc,
                                                        "language_code": lang, "depth": RANK_DEPTH})
    out = {d: None for d in domains}
    for it in (res[0].get("items") or []) if res else []:
        if it.get("type") != "organic":
            continue
        dom = (it.get("domain") or "").replace("www.", "").lower()
        for d in domains:
            if out[d] is None and (dom == d or dom.endswith("." + d)):
                out[d] = it.get("rank_group")
    return out


def summarize(rows):
    chk = [r for r in rows if r["n"]]
    pos = [r["pos"] for r in chk if r["pos"]]
    return {"tracked": len(rows), "ranked": len(pos), "top3": sum(1 for x in pos if x <= 3),
            "top10": sum(1 for x in pos if x <= 10), "avg": round(sum(pos) / len(pos), 1) if pos else None,
            "up": sum(1 for r in chk if r["n"] > 1 and r["pos"] and (r["prev"] is None or r["pos"] < r["prev"])),
            "down": sum(1 for r in chk if r["n"] > 1 and r["prev"] and (r["pos"] is None or r["pos"] > r["prev"]))}


def rank_data(p):
    pid = p["id"]
    with db() as c:
        kws = [r["keyword"] for r in c.execute("select keyword from tracked_keywords where project_id=? order by keyword", (pid,))]
        vols = {r["keyword"]: r["volume"] for r in c.execute("select keyword,volume from saved_keywords where project_id=?", (pid,))}
        recs = c.execute("select keyword,domain,day,pos from ranks where project_id=? order by day", (pid,)).fetchall()
    hist, by_day, days = {}, {}, set()
    for r in recs:
        hist.setdefault((r["keyword"], r["domain"]), []).append(r["pos"])
        by_day.setdefault((r["domain"], r["day"]), []).append(r["pos"])
        days.add(r["day"])
    days = sorted(days)
    rows = []
    for k in kws:
        h = hist.get((k, p["domain"]), [])
        rows.append({"keyword": k, "volume": vols.get(k), "n": len(h), "pos": h[-1] if h else None,
                     "prev": h[-2] if len(h) > 1 else None, "best": min((x for x in h if x), default=None),
                     "rivals": {d: (hist.get((k, d)) or [None])[-1] for d in p["competitors"]}})
    me, domains, miss = p["domain"], [p["domain"]] + p["competitors"], RANK_DEPTH + 1

    def stats(vals):  # avg = เฉพาะคำที่ติด, avg_all = นับคำที่ไม่ติดเป็นอันดับ depth+1 (เทียบเว็บที่ติดจำนวนคำไม่เท่ากันได้ยุติธรรมกว่า)
        pos = [v for v in vals if v]
        return {"tracked": len(vals), "ranked": len(pos), "top3": sum(1 for v in pos if v <= 3),
                "top10": sum(1 for v in pos if v <= 10), "avg": round(sum(pos) / len(pos), 1) if pos else None,
                "avg_all": round(sum(v or miss for v in vals) / len(vals), 1) if vals else None}

    def last(k, d):
        return (hist.get((k, d)) or [None])[-1]
    trend = {d: [dict(day=day, **stats(by_day.get((d, day), []))) for day in days] for d in domains}
    chk = [k for k in kws if hist.get((k, me))]
    compare = []
    for d in domains:
        st = dict(domain=d, **stats([last(k, d) for k in chk]))
        if d != me:
            both = [(last(k, me), last(k, d)) for k in chk if last(k, me) and last(k, d)]
            st["h2h"] = {"common": len(both),
                         "me_avg": round(sum(a for a, _ in both) / len(both), 1) if both else None,
                         "rival_avg": round(sum(b for _, b in both) / len(both), 1) if both else None,
                         "me_wins": sum(1 for a, b in both if a < b), "rival_wins": sum(1 for a, b in both if b < a)}
        compare.append(st)
    summ = summarize(rows)
    summ["day"] = days[-1] if days else None
    summ["avg_all"] = compare[0]["avg_all"]
    rv = [x["avg_all"] for x in compare[1:] if x["avg_all"] is not None and x["ranked"]]
    summ["rival_best"] = min(rv) if rv else None
    return {"rows": rows, "days": days, "trend": trend, "compare": compare, "summary": summ, "depth": RANK_DEPTH}


@app.get("/api/rank")
def rank_get():
    p = get_project(request.args.get("project_id", type=int))
    return jsonify(rank_data(p)) if p else (jsonify(error="ไม่พบโปรเจ็กต์"), 404)


@app.post("/api/rank/add")
def rank_add():
    pid = request.json.get("project_id")
    with db() as c:
        have = c.execute("select count(*) from tracked_keywords where project_id=?", (pid,)).fetchone()[0]
        room = MAX_TRACK - have
        if request.json.get("from_saved"):
            new = [r[0] for r in c.execute("select keyword from saved_keywords where project_id=? order by volume desc", (pid,))]
        else:
            new = [k.strip() for k in re.split(r"[\n,]+", request.json.get("keywords") or "") if k.strip()]
        added = 0
        for k in new:
            if added >= room:
                break
            added += c.execute("insert or ignore into tracked_keywords values(?,?)", (pid, k)).rowcount
    if room <= 0:
        return jsonify(error=f"ติดตามได้สูงสุด {MAX_TRACK} คีย์เวิร์ดต่อโปรเจ็กต์"), 400
    return jsonify(added=added)


@app.post("/api/rank/remove")
def rank_remove():
    pid, k = request.json["project_id"], request.json["keyword"]
    with db() as c:
        c.execute("delete from tracked_keywords where project_id=? and keyword=?", (pid, k))
        c.execute("delete from ranks where project_id=? and keyword=?", (pid, k))
    return jsonify(ok=True)


@app.post("/api/rank/run")
def rank_run():
    p = get_project(request.json.get("project_id"))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    with db() as c:
        kws = [r["keyword"] for r in c.execute("select keyword from tracked_keywords where project_id=?", (p["id"],))][:MAX_TRACK]
    if not kws:
        return jsonify(error="ยังไม่มีคีย์เวิร์ดที่ติดตาม"), 400
    domains, errors = [p["domain"]] + p["competitors"], []

    def one(k):
        try:
            return k, serp_positions(k, domains, p["location"], p["lang"])
        except Exception as e:
            errors.append(str(e))
            return k, None
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(one, kws))
    ok = [(k, r) for k, r in results if r is not None]
    if not ok:
        return jsonify(error=f"เช็กอันดับไม่สำเร็จ: {errors[0] if errors else 'ไม่ทราบสาเหตุ'}"), 400
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    with db() as c:
        c.executemany("insert or replace into ranks values(?,?,?,?,?)",
                      [(p["id"], k, d, day, pos) for k, r in ok for d, pos in r.items()])
    return jsonify(checked=len(ok), failed=len(kws) - len(ok))


@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        pids = [r["id"] for r in c.execute("select id from projects order by name")]
    out = []
    for pid in pids:
        p = get_project(pid)
        with db() as c:
            a = c.execute("select data,created from audits where project_id=? order by id desc limit 1", (pid,)).fetchone()
            sn = c.execute("select data from snapshots where project_id=? order by id desc limit 1", (pid,)).fetchone()
        ad = json.loads(a["data"]) if a else None
        traffic = json.loads(sn["data"]).get("overview", {}).get(p["domain"], {}).get("traffic") if sn else None
        out.append({"id": pid, "name": p["name"], "domain": p["domain"], "rivals": len(p["competitors"]),
                    "audit": {"score": ad["score"], "issues": ad["total_issues"], "created": a["created"],
                              "top": [{"label": i["label"], "count": i["count"]} for i in ad["issues"][:3]]} if ad else None,
                    "rank": rank_data(p)["summary"], "traffic": traffic})
    return jsonify(out)


# ---------- Content Ideas / Top Pages / Next Actions / ตั้งเวลา ----------
QWORDS = {"th": ["วิธี", "ทำไม", "อะไร", "ที่ไหน", "เมื่อไหร่", "อย่างไร", "ดีไหม", "ราคา", "รีวิว", "เปรียบเทียบ", "ยี่ห้อไหนดี"],
          "en": ["how", "why", "what", "where", "when", "best", "vs", "review", "price", "cheap"]}


def gap_ideas(pid, rivals, limit=20):
    """คีย์เวิร์ดที่คู่แข่งติดแต่เราไม่ติด จากผลเปรียบเทียบครั้งล่าสุด"""
    with db() as c:
        sn = c.execute("select data from snapshots where project_id=? order by id desc limit 1", (pid,)).fetchone()
    best = {}
    for rival, rows in (json.loads(sn["data"]).get("gaps", {}) if sn else {}).items():
        for r in rows if rival in rivals else []:
            if r["kind"] == "gap" and r["volume"] > best.get(r["keyword"], {}).get("volume", -1):
                best[r["keyword"]] = {"keyword": r["keyword"], "volume": r["volume"], "rival": rival, "pos": r["rival"]}
    return sorted(best.values(), key=lambda x: x["volume"], reverse=True)[:limit]


@app.get("/api/gapideas")
def gapideas():
    p = get_project(request.args.get("project_id", type=int))
    return jsonify(gap_ideas(p["id"], p["competitors"]) if p else [])


@app.post("/api/ideas")
def ideas():
    p = get_project(request.json.get("project_id"))
    seed = (request.json.get("seed") or "").strip()
    if not p or not seed:
        return jsonify(error="เลือกโปรเจ็กต์และกรอกหัวข้อก่อน"), 400
    words = QWORDS.get(p["lang"], QWORDS["en"])
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(suggest, [(f"{w} {seed}", p["lang"]) for w in words] + [(f"{seed} {w}", p["lang"]) for w in words]))
    questions = list(dict.fromkeys(k for lst in res for k in lst if k != seed))[:60]
    out = {"questions": questions, "paa": [], "related": [], "top": [], "note": ""}
    try:  # People Also Ask / Related searches ต้องใช้ SERP Advanced
        r = dfs_post("serp/google/organic/live/advanced", {"keyword": seed, "location_code": p["location"],
                                                           "language_code": p["lang"], "depth": 10})
        for it in (r[0].get("items") or []) if r else []:
            t = it.get("type")
            if t == "organic":
                out["top"].append({"pos": it.get("rank_group"), "title": it.get("title") or it.get("url"),
                                   "url": it.get("url"), "domain": it.get("domain")})
            elif t == "people_also_ask":
                out["paa"] += [x["title"] for x in it.get("items") or [] if x.get("title")]
            elif t == "related_searches":
                out["related"] += [x if isinstance(x, str) else x.get("title") for x in it.get("items") or []]
    except Exception as e:
        out["note"] = f"แสดงเฉพาะคำถามจาก Autocomplete ({e})"
    return jsonify(out)


def relevant_pages(domain, loc, lang):
    res = dfs_post("dataforseo_labs/google/relevant_pages/live", {
        "target": domain, "location_code": loc, "language_code": lang, "limit": 20,
        "order_by": ["metrics.organic.etv,desc"]})
    pages = []
    for i in (res[0].get("items") or []) if res else []:
        o = (i.get("metrics") or {}).get("organic") or {}
        pages.append({"url": i["page_address"], "traffic": round(o.get("etv") or 0), "keywords": o.get("count") or 0})
    return pages


@app.post("/api/toppages/run")
def toppages_run():
    p = get_project(request.json.get("project_id"))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    errs = {}

    def one(d):
        try:
            if d == p["domain"] and GSC_JSON:  # เว็บเราใช้ Search Console (ฟรี) ก่อน
                try:
                    return d, gsc_pages(d)
                except Exception as e:
                    if not (DFS_LOGIN and DFS_PASS):
                        raise RuntimeError(f"Search Console: {e}")
            if not (DFS_LOGIN and DFS_PASS):
                raise RuntimeError("ข้อมูลเว็บคู่แข่งต้องใช้ DataForSEO (Search Console ดูได้เฉพาะเว็บของเราเอง)"
                                   if d != p["domain"] else "ยังไม่ได้ตั้ง GSC_CREDENTIALS_JSON หรือ DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD")
            return d, relevant_pages(d, p["location"], p["lang"])
        except Exception as e:
            errs[d] = str(e)[:300]
            return d, None
    with ThreadPoolExecutor(4) as ex:
        res = [x for x in ex.map(one, [p["domain"]] + p["competitors"]) if x[1] is not None]
    if not res:
        return jsonify(error=f"ดึงข้อมูลไม่สำเร็จ: {errs.get(p['domain']) or next(iter(errs.values()), 'ไม่ทราบสาเหตุ')}", errors=errs), 400
    with db() as c:
        for d, pg in res:
            c.execute("insert or replace into toppages values(?,?,?,datetime('now'))", (p["id"], d, json.dumps(pg)))
    return jsonify(ok=len(res), errors=errs)


@app.get("/api/toppages")
def toppages_get():
    with db() as c:
        rows = c.execute("select domain,data,created from toppages where project_id=?", (request.args.get("project_id"),)).fetchall()
    return jsonify({r["domain"]: {"pages": json.loads(r["data"]), "created": r["created"]} for r in rows})


@app.get("/api/todo")
def todo_get():
    p = get_project(request.args.get("project_id", type=int))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    pid, items = p["id"], []

    def add(key, cat, pri, text, detail=""):
        items.append({"key": key, "cat": cat, "pri": pri, "text": text, "detail": detail})
    with db() as c:
        a = c.execute("select data from audits where project_id=? order by id desc limit 1", (pid,)).fetchone()
        done = {r["key"] for r in c.execute("select key from actions_done where project_id=?", (pid,))}
    if not p["competitors"]:
        add("setup:rivals", "ตั้งค่า", 2, "เพิ่มเว็บคู่แข่งในโปรเจ็กต์", "กด 'แก้ไข' ที่เมนูซ้าย เพื่อใช้ฟีเจอร์เปรียบเทียบและ keyword gap")
    if not a:
        add("setup:audit", "Site Audit", 1, "Crawl เว็บของคุณครั้งแรก", "ไปที่ Site Audit แล้วกด Crawl เว็บใหม่")
    else:
        for i in json.loads(a["data"])["issues"]:
            w = WEIGHT[i["id"]]
            add("audit:" + i["id"], "Site Audit", 1 if w >= 3 else 2 if w == 2 else 3, f"แก้: {i['label']} ({i['count']} หน้า)", i["tip"])
    rd = rank_data(p)
    if not rd["rows"]:
        add("setup:rank", "อันดับ", 2, "เพิ่มคีย์เวิร์ดที่ต้องการติดตามอันดับ", "ไปที่ติดตามอันดับ แล้วเพิ่มคีย์เวิร์ดหลักของธุรกิจ")
    day = rd["summary"]["day"]
    rows = sorted(rd["rows"], key=lambda r: r["volume"] or 0, reverse=True)
    for r in rows:
        if r["n"] > 1 and r["prev"] and (r["pos"] is None or r["pos"] > r["prev"]):
            lost = r["pos"] is None or r["pos"] - r["prev"] >= 3
            add(f"drop:{r['keyword']}:{day}", "อันดับ", 1 if lost else 2, f"อันดับตก: “{r['keyword']}”",
                f"จากอันดับ {r['prev']} เป็น {r['pos'] or 'ไม่ติด'} ตรวจเนื้อหาและอัปเดตหน้าที่เกี่ยวข้อง")
    for r in [r for r in rows if r["pos"] and 4 <= r["pos"] <= 15][:5]:
        add(f"push:{r['keyword']}", "อันดับ", 2, f"ดันขึ้นหน้าแรก: “{r['keyword']}”",
            f"ตอนนี้อันดับ {r['pos']} เพิ่มเนื้อหา ลิงก์ภายใน และปรับ title ของหน้านี้")
    for r in [r for r in rows if r["n"] and r["pos"] is None][:3]:
        add(f"new:{r['keyword']}", "คอนเทนต์", 2, f"สร้างหน้าสำหรับ “{r['keyword']}”", "ยังไม่ติดอันดับในผลค้นหา ลองเขียนบทความหรือหน้าสินค้าที่ตรงคำนี้")
    for g in gap_ideas(pid, p["competitors"], 8):
        add("gap:" + g["keyword"], "คอนเทนต์", 2, f"ทำคอนเทนต์เรื่อง “{g['keyword']}”", f"ค้นหา {g['volume']:,}/เดือน, {g['rival']} ติดอันดับ {g['pos']}")
    for i in items:
        i["done"] = i["key"] in done
    items.sort(key=lambda i: (i["done"], i["pri"]))
    return jsonify(items=items)


@app.post("/api/todo/toggle")
def todo_toggle():
    d = request.json
    with db() as c:
        if d.get("done"):
            c.execute("insert or ignore into actions_done values(?,?)", (d["project_id"], d["key"]))
        else:
            c.execute("delete from actions_done where project_id=? and key=?", (d["project_id"], d["key"]))
    return jsonify(ok=True)


@app.get("/api/schedule")
def sched_get():
    with db() as c:
        r = c.execute("select * from schedules where project_id=?", (request.args.get("project_id"),)).fetchone()
    return jsonify(dict(r) if r else {"audit_every": 0, "rank_every": 0, "hour": 3, "last_audit": None, "last_rank": None, "last_msg": None})


@app.post("/api/schedule")
def sched_save():
    d = request.json
    with db() as c:
        c.execute("insert into schedules(project_id,audit_every,rank_every,hour) values(?,?,?,?) on conflict(project_id) do update set "
                  "audit_every=excluded.audit_every, rank_every=excluded.rank_every, hour=excluded.hour",
                  (d["project_id"], int(d["audit_every"]), int(d["rank_every"]), int(d["hour"])))
    return jsonify(ok=True)


def due(last, every, hour):
    now = datetime.now(TZ)
    if not every or now.hour < hour:
        return False
    return not last or (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days >= every


def scheduler_loop():
    """ตรวจทุก 5 นาที ถึงรอบแล้วเรียก endpoint เดิมผ่าน test client (ทำงานตลอดที่แอปเปิดอยู่ ทั้ง python app.py และ gunicorn)"""
    time.sleep(10)
    while True:
        try:
            with db() as c:
                rows = c.execute("select * from schedules").fetchall()
            for r in rows:
                for kind, url in (("audit", "/api/audit/run"), ("rank", "/api/rank/run")):
                    if not due(r["last_" + kind], r[kind + "_every"], r["hour"]):
                        continue
                    resp = app.test_client().post(url, json={"project_id": r["project_id"], "limit": 50},
                                                  auth=(APP_USER, APP_PASSWORD) if APP_PASSWORD else None)
                    msg = "สำเร็จ" if resp.status_code == 200 else (resp.get_json(silent=True) or {}).get("error", "ผิดพลาด")
                    # บันทึกวันที่ไว้แม้ล้มเหลว เพื่อไม่ให้ยิง API ซ้ำทุก 5 นาที
                    with db() as c:
                        c.execute(f"update schedules set last_{kind}=?, last_msg=? where project_id=?",
                                  (datetime.now(TZ).strftime("%Y-%m-%d"), f"{datetime.now(TZ).strftime('%H:%M')} {kind}: {msg}", r["project_id"]))
        except Exception:
            pass
        time.sleep(300)


# ---------- หน้าเว็บ ----------
PAGE = r"""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SEO ส่วนตัว</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600&family=IBM+Plex+Sans+Thai+Looped:wght@700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1f2937;--mute:#6b7280;--line:#e5e7eb;--brand:#d9400a;--on:#fff;--peach:#ffefe9;--peachline:#ffc9b8;--win:#16a34a;--lose:#dc2626;--soft:#f3f4f6}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--panel:#1d2026;--ink:#eceef1;--mute:#9aa1ad;--line:#2b303a;--brand:#ff7a4d;--on:#14161a;--peach:#2c2019;--peachline:#5a3425;--win:#4ade80;--lose:#f87171;--soft:#252932}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "IBM Plex Sans Thai",system-ui,sans-serif}
.shell{display:flex;min-height:100vh}.content{flex:1;min-width:0}main{max-width:1000px;margin:auto;padding:22px 24px}
.side{width:236px;flex:none;background:var(--panel);border-right:1px solid var(--line);padding:18px 14px;position:sticky;top:0;height:100vh;overflow:auto}
.logo{font-weight:700;font-size:22px;color:var(--brand);margin:0 0 16px}
.pbox{background:var(--peach);border:1px solid var(--peachline);border-radius:12px;padding:10px}
.pbox label{display:block;font-weight:600;color:var(--brand);font-size:14px;margin-bottom:6px}
#proj{width:100%;font-weight:600}.pact{display:flex;gap:6px;margin-top:8px}.pact .sm{flex:1;padding:5px 6px;font-size:13px}
.info{color:var(--mute);font-size:12px;margin:8px 0 0;word-break:break-all}
nav{margin-top:18px;display:flex;flex-direction:column;gap:2px}
nav button{text-align:left;background:none;border:0;border-left:3px solid transparent;border-radius:8px;padding:10px 12px;color:var(--ink)}
nav button[aria-selected=true]{background:var(--peach);color:var(--brand);font-weight:600;border-left-color:var(--brand)}
@media(max-width:800px){.shell{flex-direction:column}.side{width:auto;height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}nav{flex-direction:row;flex-wrap:wrap}main{padding:16px}}
select,input,textarea{padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit}
button{font:inherit;cursor:pointer}.bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;align-items:center}.bar input{flex:1;min-width:200px}
.go{padding:10px 20px;border:0;border-radius:8px;background:var(--brand);color:var(--on);font-weight:600}
.sm{padding:6px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink)}a.sm{text-decoration:none;display:inline-block}
.danger{color:var(--lose)}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:3px solid var(--brand);outline-offset:2px}
.msg{color:var(--mute);margin:8px 0}.err{color:var(--lose)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--soft);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}.win{color:var(--win);font-weight:600}.lose{color:var(--lose);font-weight:600}
.tabs2{display:flex;gap:8px;margin:14px 0 10px;flex-wrap:wrap;align-items:center}
.tabs2 button{padding:6px 14px;border:1px solid var(--line);border-radius:20px;background:var(--panel);color:var(--ink)}
.tabs2 button[aria-pressed=true]{background:var(--brand);color:var(--on);border-color:var(--brand)}
h2{font-size:17px;margin:22px 0 8px}.long{max-width:300px;word-break:break-word;color:var(--mute)}
.top{display:flex;gap:10px;justify-content:flex-end;align-items:center;flex-wrap:wrap;margin-bottom:14px}.top .msg{margin:0;font-size:13px;text-align:right}
.hero{background:var(--peach);border:1px solid var(--peachline);border-radius:16px;padding:10px;display:grid;grid-template-columns:minmax(230px,1fr) 3fr;gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 22px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);text-align:center;align-items:center}.metrics>div+div{border-left:1px solid var(--line)}
.big{font-size:30px;font-weight:700;line-height:1.3}.lbl{font-weight:600;font-size:14px}.cap{color:var(--mute);font-size:13px;margin:0}
.grid2{display:grid;grid-template-columns:1fr 1.3fr;gap:16px;margin-top:16px}
.sec{border-top:1px solid var(--line);margin:16px -22px 0;padding:14px 22px 0}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}
.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:10px}
.lnk{background:none;border:0;color:var(--brand);font-weight:600;padding:0;white-space:nowrap}
@media(max-width:800px){.hero,.grid2{grid-template-columns:1fr}.metrics{grid-template-columns:1fr}.metrics>div+div{border-left:0;border-top:1px solid var(--line);padding-top:10px}}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);width:min(520px,92vw);padding:22px}
dialog::backdrop{background:rgba(0,0,0,.45)}dialog label{display:block;margin:12px 0 4px;font-weight:600}
dialog input,dialog select,dialog textarea{width:100%}.acts{display:flex;gap:8px;margin-top:18px}.acts .sp{flex:1}
.ttl{font-size:22px;font-weight:700;margin:0 0 12px}.hl{border-color:var(--brand);color:var(--brand)}
button:disabled{opacity:.45;cursor:default}.legend{display:flex;flex-wrap:wrap;gap:6px 20px;margin-top:10px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.grid2.eq{grid-template-columns:1fr 1fr}
@media(max-width:800px){.grid2.eq{grid-template-columns:1fr}}tr.cur td{background:var(--peach)}
.rep{margin-top:16px}.bar label{display:flex;gap:8px;align-items:center}.todo{cursor:pointer}
@media print{:root{--bg:#fff;--panel:#fff;--ink:#111;--mute:#555;--line:#ccc;--brand:#c2380a;--peach:#fff3ee;--peachline:#f3c3b2;--soft:#f1f1f1;--win:#15803d;--lose:#b91c1c}
body{background:#fff}.side,.top{display:none!important}.shell{display:block}main{max-width:none;padding:0}.card,tr{break-inside:avoid}.rep{break-inside:avoid-page}}
</style></head><body>
<div class="shell"><aside class="side"><div class="logo">SEO ส่วนตัว</div>
<div class="pbox"><label for="proj">โปรเจ็กต์</label><select id="proj"></select>
<div class="pact"><button class="sm" id="pnew">+ ใหม่</button><button class="sm" id="pedit">แก้ไข</button></div></div>
<p class="info" id="info"></p><p class="info" id="dfs"></p><p class="info" id="gsc"></p>
<nav><button aria-selected="true" data-t="dash">ภาพรวม</button><button aria-selected="false" data-t="todo">สิ่งที่ควรทำ</button><button aria-selected="false" data-t="audit">Site Audit</button><button aria-selected="false" data-t="rank">ติดตามอันดับ</button><button aria-selected="false" data-t="kw">หาคีย์เวิร์ด</button>
<button aria-selected="false" data-t="saved">คีย์เวิร์ดที่บันทึก</button><button aria-selected="false" data-t="cmp">เปรียบเทียบคู่แข่ง</button><button aria-selected="false" data-t="ideas">ไอเดียคอนเทนต์</button><button aria-selected="false" data-t="pages">หน้ายอดนิยม</button><button aria-selected="false" data-t="report">รายงาน</button></nav></aside>
<div class="content"><main>
<section id="dash"><div id="dout"></div><div id="sched"></div></section>
<section id="todo" hidden><div id="tout"></div></section>
<section id="rank" hidden><div class="top"><span class="msg" id="rdate"></span><button class="go" id="rrun">เช็กอันดับตอนนี้</button></div>
<div class="bar"><input id="radd" placeholder="เพิ่มคีย์เวิร์ดที่ต้องการติดตาม (คั่นด้วยจุลภาคหรือขึ้นบรรทัดใหม่)"><button class="sm" id="raddb">เพิ่ม</button><button class="sm" id="rsaved">เพิ่มจากที่บันทึกไว้</button><span class="msg" id="rmsg"></span></div>
<div id="rout"></div></section>
<section id="audit" hidden><div class="top"><span class="msg" id="acrawl"></span>
<select id="alimit" aria-label="จำนวนหน้าที่ crawl"><option value="25">25 หน้า</option><option value="50" selected>50 หน้า</option><option value="100">100 หน้า</option></select>
<button class="go" id="arun">Crawl เว็บใหม่</button><a class="sm" id="aexp" href="#">ส่งออก CSV</a></div><div id="aout"></div></section>
<section id="kw" hidden><div class="bar"><input id="seed" placeholder="คำค้นหลัก เช่น รองเท้าวิ่ง"><button class="go" id="kgo">หาคีย์เวิร์ด</button></div><div id="kout"></div></section>
<section id="saved" hidden><div id="sout"></div></section>
<section id="ideas" hidden><div class="bar"><input id="idseed" placeholder="หัวข้อหรือคีย์เวิร์ด เช่น รองเท้าวิ่ง"><button class="go" id="idgo">หาไอเดีย</button></div><div id="idout"></div>
<h2 class="ttl" style="margin-top:28px">หัวข้อที่คู่แข่งติด แต่เรายังไม่ติด</h2><div id="gapout"></div></section>
<section id="pages" hidden><div class="top"><span class="msg" id="pdate"></span><select id="pdom" aria-label="เว็บ"></select><button class="go" id="prun">ดึงข้อมูลใหม่</button></div><div id="pout"></div></section>
<section id="report" hidden><div class="top"><span class="msg">กดแล้วเลือก "บันทึกเป็น PDF" ในหน้าต่างพิมพ์ของเบราว์เซอร์</span><button class="go" id="repbtn">บันทึกเป็น PDF / พิมพ์</button></div><div id="repout"></div></section>
<section id="cmp" hidden><div class="bar"><button class="go" id="cgo">เปรียบเทียบกับคู่แข่งทั้งหมด</button><select id="hist" aria-label="ประวัติ"></select></div><div id="cover"></div><div id="cout"></div></section>
</main></div></div>
<dialog id="idlg"><h2 id="it" style="margin-top:0"></h2><p class="cap" id="ih"></p><div class="wrap" style="max-height:50vh;overflow:auto;margin-top:12px"><table id="iu"></table></div>
<div class="acts"><button class="sm" id="iclose">ปิด</button></div></dialog>
<dialog id="dlg"><h2 id="dtitle" style="margin-top:0"></h2>
<label for="dn">ชื่อโปรเจ็กต์ / ธุรกิจ</label><input id="dn">
<label for="dd">เว็บของคุณ</label><input id="dd" placeholder="myshop.com">
<label for="dc">ประเทศและภาษาที่ต้องการวิเคราะห์</label><select id="dc"></select>
<label for="dr">เว็บคู่แข่ง (บรรทัดละ 1 เว็บ สูงสุด 5)</label><textarea id="dr" rows="5" placeholder="rival1.com&#10;rival2.com"></textarea>
<p class="err" id="derr"></p>
<div class="acts"><button class="go" id="dsave">บันทึก</button><button class="sm" id="dcancel">ยกเลิก</button><span class="sp"></span><button class="sm danger" id="ddel">ลบโปรเจ็กต์</button></div></dialog>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=v=>v==null?"-":Number(v).toLocaleString("th-TH");
const C=[["ไทย (ภาษาไทย)",2764,"th"],["สหรัฐฯ (อังกฤษ)",2840,"en"],["สหราชอาณาจักร (อังกฤษ)",2826,"en"],["สิงคโปร์ (อังกฤษ)",2702,"en"],["ออสเตรเลีย (อังกฤษ)",2036,"en"]];
$("#dc").innerHTML=C.map((c,i)=>`<option value="${i}">${c[0]}</option>`).join("");
async function api(u,m="GET",b){const r=await fetch(u,{method:m,headers:{"Content-Type":"application/json"},body:b?JSON.stringify(b):undefined});const d=await r.json();if(!r.ok)throw new Error(d.error||"เกิดข้อผิดพลาด");return d}
let P=[],cur=null,KW=[],SNAP=null;const proj=()=>P.find(p=>p.id==cur);
const noProj=()=>`<p class="msg">สร้างหรือเลือกโปรเจ็กต์ก่อน (ปุ่ม + โปรเจ็กต์ใหม่ ด้านบน)</p>`;

async function load(sel){
 P=await api("/api/projects");cur=sel||(P.find(p=>p.id==localStorage.cur)||P[0]||{}).id||null;localStorage.cur=cur||"";
 $("#proj").innerHTML=P.map(p=>`<option value="${p.id}" ${p.id==cur?"selected":""}>${esc(p.name)}</option>`).join("")||"<option>ยังไม่มีโปรเจ็กต์</option>";
 $("#pedit").disabled=!cur;const p=proj();
 $("#info").textContent=p?`${p.domain}  |  ${C.find(c=>c[1]==p.location&&c[2]==p.lang)?.[0]||p.location}  |  คู่แข่ง ${p.competitors.length} เว็บ`:"สร้างโปรเจ็กต์แรกเพื่อเริ่มใช้งาน";
 $("#kout").innerHTML=p?"":noProj();$("#cout").innerHTML=p?"":noProj();KW=[];SNAP=null;$("#cover").innerHTML="";
 PERR={};refreshDfs();refreshSaved();refreshHist(true);refreshAudit();refreshRank();refreshDash();refreshSched();refreshPages();refreshTodo();refreshGapIdeas();if(!$('#report').hidden)buildReport()}
$("#proj").onchange=e=>load(+e.target.value);
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{document.querySelectorAll("nav button").forEach(x=>x.setAttribute("aria-selected",x===b));
 ["dash","todo","audit","rank","kw","ideas","saved","cmp","pages","report"].forEach(id=>$("#"+id).hidden=id!==b.dataset.t);({dash:()=>{refreshDash();refreshSched()},todo:refreshTodo,pages:refreshPages,report:buildReport,ideas:refreshGapIdeas}[b.dataset.t]||(()=>{}))()});

// ---- dialog โปรเจ็กต์ ----
let editing=null;
function openDlg(p){editing=p;$("#dtitle").textContent=p?"แก้ไขโปรเจ็กต์":"โปรเจ็กต์ใหม่";$("#dn").value=p?.name||"";$("#dd").value=p?.domain||"";
 $("#dr").value=(p?.competitors||[]).join("\n");$("#dc").value=Math.max(0,C.findIndex(c=>p&&c[1]==p.location&&c[2]==p.lang));
 $("#ddel").style.display=p?"":"none";$("#derr").textContent="";$("#dlg").showModal()}
$("#pnew").onclick=()=>openDlg(null);$("#pedit").onclick=()=>openDlg(proj());$("#dcancel").onclick=()=>$("#dlg").close();
$("#dsave").onclick=async()=>{const c=C[$("#dc").value];
 try{const d=await api(editing?`/api/projects/${editing.id}`:"/api/projects",editing?"PUT":"POST",{name:$("#dn").value,domain:$("#dd").value,location:c[1],lang:c[2],competitors:$("#dr").value});
 $("#dlg").close();load(d.id)}catch(e){$("#derr").textContent=e.message}};
$("#ddel").onclick=async()=>{if(confirm(`ลบโปรเจ็กต์ "${editing.name}" พร้อมคีย์เวิร์ดและประวัติทั้งหมด?`)){await api(`/api/projects/${editing.id}`,"DELETE");$("#dlg").close();localStorage.cur="";load()}};

// ---- คีย์เวิร์ด ----
$("#seed").onkeydown=e=>e.key==="Enter"&&$("#kgo").click();
$("#kgo").onclick=async()=>{const o=$("#kout");if(!cur)return o.innerHTML=noProj();o.innerHTML='<p class="msg">กำลังดึงคีย์เวิร์ด…</p>';
 try{const d=await api("/api/keywords","POST",{seed:$("#seed").value,project_id:cur});KW=d.rows;
 o.innerHTML=(d.note?`<p class="msg">${esc(d.note)}</p>`:"")+`<div class="bar"><span class="msg">พบ ${KW.length} คีย์เวิร์ด</span><button class="sm" id="ksel">บันทึกที่ติ๊ก</button><button class="sm" id="kall">บันทึกทั้งหมด</button><span class="msg" id="kmsg"></span></div><div class="wrap"><table><tr><th></th><th>คีย์เวิร์ด</th><th class="n">ค้นหา/เดือน</th><th class="n">CPC ($)</th><th>การแข่งขัน</th></tr>`+
 KW.map((r,i)=>`<tr><td><input type="checkbox" data-i="${i}" aria-label="เลือก"></td><td>${esc(r.keyword)}</td><td class="n">${num(r.volume)}</td><td class="n">${r.cpc==null?"-":r.cpc.toFixed(2)}</td><td>${esc(r.competition)}</td></tr>`).join("")+"</table></div>";
 const save=async rows=>{if(!rows.length)return $("#kmsg").textContent="ยังไม่ได้ติ๊กคีย์เวิร์ด";await api("/api/keywords/save","POST",{project_id:cur,rows});$("#kmsg").textContent=`บันทึกแล้ว ${rows.length} คำ`;refreshSaved()};
 $("#ksel").onclick=()=>save([...o.querySelectorAll("input[data-i]:checked")].map(x=>KW[x.dataset.i]));$("#kall").onclick=()=>save(KW)}
 catch(e){o.innerHTML=`<p class="err">${esc(e.message)}</p>`}};
async function refreshSaved(){const o=$("#sout");if(!cur)return o.innerHTML=noProj();const rows=await api("/api/keywords/saved?project_id="+cur);
 o.innerHTML=rows.length?`<div class="bar"><span class="msg">บันทึกไว้ ${rows.length} คำ</span><a class="sm" style="text-decoration:none;color:inherit" href="/api/keywords/export.csv?project_id=${cur}">ดาวน์โหลด CSV</a></div><div class="wrap"><table><tr><th>คีย์เวิร์ด</th><th class="n">ค้นหา/เดือน</th><th class="n">CPC ($)</th><th>บันทึกเมื่อ</th><th></th></tr>`+
 rows.map(r=>`<tr><td>${esc(r.keyword)}</td><td class="n">${num(r.volume)}</td><td class="n">${r.cpc==null?"-":r.cpc.toFixed(2)}</td><td>${esc(r.created)}</td><td><button class="sm" data-del="${esc(r.keyword)}">ลบ</button></td></tr>`).join("")+"</table></div>":'<p class="msg">ยังไม่มีคีย์เวิร์ดที่บันทึก ไปแท็บ "หาคีย์เวิร์ด" แล้วกดบันทึก</p>'}
$("#sout").onclick=async e=>{const k=e.target.dataset.del;if(k){await api("/api/keywords/delete","POST",{project_id:cur,keyword:k});refreshSaved()}};

// ---- เปรียบเทียบ ----
const lowBetter=["รูปที่ไม่มี alt","เวลาตอบสนอง (วินาที)","ขนาด HTML (KB)"],hiBetter=["จำนวนคำในหน้า","ลิงก์ภายใน","รูปภาพ","H2"];
async function refreshHist(auto){const h=$("#hist");if(!cur){h.innerHTML="";return}const rows=await api("/api/snapshots?project_id="+cur);
 h.innerHTML='<option value="">ประวัติการเปรียบเทียบ ('+rows.length+')</option>'+rows.map(r=>`<option value="${r.id}">${esc(r.created)}</option>`).join("");
 if(auto&&rows.length){h.value=rows[0].id;showSnap((await api("/api/snapshots/"+rows[0].id)).data)}}
$("#hist").onchange=async e=>{if(e.target.value)showSnap((await api("/api/snapshots/"+e.target.value)).data)};
$("#cgo").onclick=async()=>{const o=$("#cout");if(!cur)return o.innerHTML=noProj();o.innerHTML='<p class="msg">กำลังเช็กทุกเว็บและดึง keyword gap…</p>';
 try{const d=await api("/api/compare","POST",{project_id:cur});await refreshHist();$("#hist").value=d.id;showSnap(d.data)}catch(e){o.innerHTML=`<p class="err">${esc(e.message)}</p>`}};
function showDetail(d){SNAP=d;const S=d.sites,ok=S.find(s=>s.audit);let h="";
 if(ok){h+=`<h2>เทียบ On-page</h2><div class="wrap"><table><tr><th>รายการ</th>`+S.map((s,i)=>`<th>${i?"":"(เรา) "}${esc(s.domain)}</th>`).join("")+"</tr>";
 for(const k in ok.audit){const vals=S.map(s=>s.audit?s.audit[k]:null),nums=vals.filter(v=>typeof v==="number"),hi=hiBetter.includes(k),lo=lowBetter.includes(k);
  const best=(hi||lo)&&nums.length&&new Set(nums).size>1?(hi?Math.max(...nums):Math.min(...nums)):null;
  h+=`<tr><td>${esc(k)}</td>`+S.map((s,i)=>{const v=vals[i],long=typeof v==="string"&&v.length>30;return `<td class="${v===best?"win":""} ${long?"long":""}">${s.error?"เปิดไม่ได้":esc(v)}</td>`}).join("")+"</tr>"}
 h+="</table></div><p class='msg'>สีเขียว = ค่าที่ดีที่สุดในเชิง SEO</p>"}
 const rivals=Object.keys(d.gaps);
 if(rivals.length)h+=`<h2>Keyword gap</h2><div class="tabs2"><select id="grival" aria-label="คู่แข่ง">${rivals.map(r=>`<option>${esc(r)}</option>`).join("")}</select><span id="gbtns"></span></div><div class="wrap"><table id="gtab"></table></div>`;
 else if(d.gap_note)h+=`<p class="msg">${esc(d.gap_note)}</p>`;
 $("#cout").innerHTML=h;if(rivals.length){$("#grival").onchange=()=>drawGap("gap");drawGap("gap")}}
function drawGap(kind){const all=SNAP.gaps[$("#grival").value]||[],c=k=>all.filter(x=>x.kind===k).length;
 $("#gbtns").innerHTML=[["gap",`คู่แข่งติด เราไม่ติด (${c("gap")})`],["both",`ติดทั้งคู่ (${c("both")})`],["mine",`เราติดอย่างเดียว (${c("mine")})`],["all","ทั้งหมด"]].map(([k,t])=>`<button data-k="${k}" aria-pressed="${k===kind}">${t}</button>`).join("");
 $("#gbtns").onclick=e=>e.target.dataset.k&&drawGap(e.target.dataset.k);
 const rows=all.filter(r=>kind==="all"||r.kind===kind);
 $("#gtab").innerHTML="<tr><th>คีย์เวิร์ด</th><th class='n'>ค้นหา/เดือน</th><th class='n'>CPC ($)</th><th class='n'>อันดับเรา</th><th class='n'>อันดับคู่แข่ง</th></tr>"+
 (rows.map(r=>`<tr><td>${esc(r.keyword)}</td><td class="n">${num(r.volume)}</td><td class="n">${r.cpc.toFixed(2)}</td><td class="n ${r.me&&r.rival&&r.me<r.rival?"win":""}">${r.me||"-"}</td><td class="n ${r.me&&r.rival&&r.rival<r.me?"lose":""}">${r.rival||"-"}</td></tr>`).join("")||"<tr><td colspan=5>ไม่มีข้อมูล</td></tr>")}
// ---- Site Audit ----
const ST=[["success","สำเร็จ","#22c55e"],["redirected","ถูก Redirect","#86efac"],["broken","ลิงก์เสีย","#f59e0b"],["blocked","ถูกบล็อก","#f43f5e"]];
let AUD=null;
function gauge(s){const c=s>=80?"#22c55e":s>=50?"#f59e0b":"#ef4444",t=s>=80?"สูง":s>=50?"ปานกลาง":"ต่ำ",d="M10 60A50 50 0 0 1 110 60";
 return `<div><svg viewBox="0 0 120 68" width="120" role="img" aria-label="คะแนน ${s} จาก 100"><path d="${d}" fill="none" style="stroke:var(--line)" stroke-width="12" stroke-linecap="round"/><path d="${d}" fill="none" stroke="${c}" stroke-width="12" stroke-linecap="round" pathLength="100" stroke-dasharray="${s} 100"/></svg><div class="cap" style="text-align:center;margin-top:-14px">${t}</div></div>`}
async function refreshAudit(){const o=$("#aout");$("#aexp").href=cur?"/api/audit/export.csv?project_id="+cur:"#";if(!cur){AUD=null;$("#acrawl").textContent="";return o.innerHTML=noProj()}
 const d=await api("/api/audit/latest?project_id="+cur);AUD=d.data;drawAudit(d.created)}
function drawAudit(created){const o=$("#aout"),d=AUD;$("#acrawl").textContent=d?"Crawl ล่าสุด: "+created:"";
 if(!d)return o.innerHTML='<p class="msg">ยังไม่เคยตรวจเว็บนี้ กดปุ่ม "Crawl เว็บใหม่" เพื่อเริ่ม</p>';
 const m=d.metrics||{},f=v=>v==null?"-":num(v),total=d.pages.length,bl=d.counts.blocked;
 o.innerHTML=`<div class="hero"><div class="card"><div class="lbl">คะแนน On-page SEO</div><div style="display:flex;align-items:center;gap:14px"><span class="big">${d.score}</span>${gauge(d.score)}</div></div>
 <div class="card metrics"><div><div class="lbl">Organic traffic / เดือน</div><div class="big">${f(m.traffic)}</div></div><div><div class="lbl">Organic keywords</div><div class="big">${f(m.keywords)}</div></div><div><div class="lbl">Backlinks</div><div class="big">${f(m.backlinks)}</div></div></div></div>
 <div class="grid2"><div class="card"><div class="lbl">หน้าที่ crawl</div><div class="big">${total}</div><p class="cap">crawl สำเร็จ ${total-bl} หน้า และพบถูกบล็อก ${bl} หน้า</p>
 <div class="sec"><div class="lbl">สถานะหน้า</div>${ST.map(([k,t,c])=>`<div class="row"><span><i class="dot" style="background:${c}"></i>${t}</span><b>${d.counts[k]||0}</b></div>`).join("")}</div></div>
 <div class="card"><div class="lbl">ปัญหา SEO ที่พบ</div><div class="big">${d.total_issues}</div>
 <div class="sec"><div class="lbl">ปัญหาหลัก</div>${d.issues.length?d.issues.map((x,i)=>`<div class="row"><span><b style="color:var(--brand)">${x.count} หน้า</b> ${esc(x.label)}</span><button class="lnk" data-iss="${i}">ดูรายละเอียด &rarr;</button></div>`).join(""):'<p class="msg">ไม่พบปัญหา</p>'}</div></div></div>`}
$("#arun").onclick=async()=>{if(!cur)return;$("#aout").innerHTML='<p class="msg">กำลัง crawl เว็บ อาจใช้เวลาประมาณ 1-2 นาที อย่าปิดหน้านี้…</p>';
 try{const d=await api("/api/audit/run","POST",{project_id:cur,limit:+$("#alimit").value});AUD=d.data;drawAudit(d.created)}catch(e){$("#aout").innerHTML=`<p class="err">${esc(e.message)}</p>`}};
$("#aout").onclick=e=>{const i=e.target.dataset.iss;if(i==null||!AUD)return;const x=AUD.issues[i];
 $("#it").textContent=`${x.label} (${x.count} หน้า)`;$("#ih").textContent="วิธีแก้: "+x.tip;
 $("#iu").innerHTML=x.urls.map(u=>`<tr><td><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></td></tr>`).join("");$("#idlg").showModal()};
$("#iclose").onclick=()=>$("#idlg").close();
// ---- ภาพรวมคู่แข่ง: กราฟทราฟฟิก + ตารางคู่แข่งที่ติดตาม ----
const COL=["var(--brand)","#f59e0b","#10b981","#3b82f6","#8b5cf6","#ec4899"],MON=["ม.ค.","ก.พ.","มี.ค.","เม.ย.","พ.ค.","มิ.ย.","ก.ค.","ส.ค.","ก.ย.","ต.ค.","พ.ย.","ธ.ค."];
function chart(ov,doms){
 const ser=doms.map(x=>(ov[x]||{}).history||[]),yms=[...new Set(ser.flat().map(x=>x.ym))].sort().slice(-12);
 if(!yms.length){const m={};doms.forEach(x=>{const e=ov[x]&&ov[x].error;if(e)(m[e]=m[e]||[]).push(x)});const er=Object.entries(m).map(([e,ds])=>`<li>${esc(e)} <span class="cap">(${ds.map(esc).join(", ")})</span></li>`).join("");
  return '<p class="msg">ยังไม่มีข้อมูลทราฟฟิกย้อนหลัง</p>'+(er?`<ul class="msg">${er}</ul>`:'<p class="msg">กด "เปรียบเทียบกับคู่แข่งทั้งหมด" อีกครั้งเพื่อให้ระบบบันทึกสาเหตุ และดูสถานะการเชื่อมต่อ DataForSEO ที่เมนูซ้ายล่าง</p>')}
 const vals=ser.map(s=>yms.map(m=>(s.find(x=>x.ym===m)||{}).etv||0)),mx=Math.max(4,...vals.flat());
 const r=mx/4,pw=10**Math.floor(Math.log10(r)),fr=r/pw,step=(fr<=1?1:fr<=2?2:fr<=5?5:10)*pw,top=step*Math.ceil(mx/step);
 const W=900,H=300,L=52,R=16,T=12,B=34,x=i=>L+(W-L-R)*(yms.length>1?i/(yms.length-1):.5),y=v=>T+(H-T-B)*(1-v/top);
 let g="";for(let t=0;t<=top+1e-9;t+=step)g+=`<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" style="stroke:var(--line)" stroke-dasharray="4 4"/><text x="${L-8}" y="${y(t)+4}" text-anchor="end" font-size="12" style="fill:var(--mute)">${num(t)}</text>`;
 yms.forEach((m,i)=>g+=`<text x="${x(i)}" y="${H-10}" text-anchor="middle" font-size="12" style="fill:var(--mute)">${MON[+m.slice(5)-1]} ${m.slice(2,4)}</text>`);
 const paths=vals.map((v,k)=>{let d=`M${x(0)},${y(v[0])}`;for(let i=1;i<v.length;i++){const xm=(x(i-1)+x(i))/2;d+=`C${xm},${y(v[i-1])} ${xm},${y(v[i])} ${x(i)},${y(v[i])}`}
  return `<path d="${d}" fill="none" style="stroke:${COL[k%6]}" stroke-width="2.5" stroke-linecap="round"/>`}).reverse().join("");
 return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="กราฟทราฟฟิก Organic รายเดือนของเว็บคุณเทียบกับคู่แข่ง">${g}${paths}</svg>
 <div class="legend">${doms.map((d,k)=>`<span><i style="display:inline-block;width:18px;height:3px;background:${COL[k%6]};vertical-align:middle;margin-right:6px"></i>${esc(d)}${k?"":" (เรา)"}${(ov[d]||{}).source==="gsc"?` <span class="cap">คลิกจริงจาก Search Console</span>`:""}</span>`).join("")}</div>`}
function drawOverview(d){const p=proj(),ov=d.overview||{},co=p.competitors.filter(c=>d.gaps[c]||ov[c]||d.sites.some(s=>s.domain===c)),doms=[p.domain,...co];
 const cn=(C.find(c=>c[1]==p.location&&c[2]==p.lang)||[p.location])[0];
 const st=r=>{const g=d.gaps[r];return g?{both:g.filter(x=>x.kind==="both").length,gap:g.filter(x=>x.kind==="gap").length}:null};
 $("#cover").innerHTML=`<div class="card" style="margin-bottom:20px"><h2 class="ttl">ทราฟฟิกจาก Organic</h2>${chart(ov,doms)}</div>
 <h2 class="ttl" style="margin-bottom:4px">คู่แข่งที่ติดตาม <span class="cap" style="font-weight:400;font-size:14px">แสดง ${co.length} เว็บ ใน ${esc(cn)}</span></h2>
 <div class="bar" style="margin:8px 0"><span class="cap" id="selc">เลือก 0 จาก ${co.length}</span><button class="sm" id="cexp">ส่งออก CSV</button><button class="sm danger" id="crem" disabled>ลบที่เลือก</button><button class="sm" id="cadd">+ เพิ่มคู่แข่ง</button></div>
 <div class="wrap"><table><tr><th></th><th>เว็บคู่แข่ง</th><th class="n">คีย์เวิร์ดที่ติดทั้งคู่</th><th class="n">Keyword gap</th><th class="n">ทราฟฟิกโดยประมาณ</th><th class="n">Backlinks</th></tr>`+
 co.map(r=>{const s=st(r),o=ov[r]||{};return `<tr><td><input type="checkbox" data-r="${esc(r)}" aria-label="เลือก ${esc(r)}"></td><td>${esc(r)}</td>
 <td class="n">${s?s.both:"-"} <button class="sm" data-v="both" data-d="${esc(r)}" ${s&&s.both?"":"disabled"}>ดูทั้งหมด</button></td>
 <td class="n">${s?s.gap:"-"} <button class="sm hl" data-v="gap" data-d="${esc(r)}" ${s&&s.gap?"":"disabled"}>ดูทั้งหมด</button></td><td class="n">${num(o.traffic)}</td><td class="n">${num(o.backlinks)}</td></tr>`}).join("")+
 `</table></div><p class="cap" style="margin:8px 0 0">จำนวนคีย์เวิร์ดนับจาก 300 คำที่มีคนค้นหามากที่สุดของแต่ละคู่</p>`}
function showSnap(d){const p=proj(),keep=x=>p&&(x===p.domain||p.competitors.includes(x)),pick=o=>Object.fromEntries(Object.entries(o||{}).filter(([k])=>keep(k)));
 d={...d,sites:d.sites.filter(s=>keep(s.domain)),gaps:pick(d.gaps),overview:pick(d.overview)};SNAP=d;drawOverview(d);showDetail(d)}
$("#cover").onchange=e=>{if(e.target.dataset.r!==undefined){const n=document.querySelectorAll("#cover input[data-r]:checked").length,t=document.querySelectorAll("#cover input[data-r]").length;$("#selc").textContent=`เลือก ${n} จาก ${t}`;$("#crem").disabled=!n}};
$("#cover").onclick=async e=>{const t=e.target,p=proj();
 if(t.dataset.v){const r=t.dataset.d,gap=t.dataset.v==="gap",rows=(SNAP.gaps[r]||[]).filter(x=>x.kind===t.dataset.v);
  $("#it").textContent=`${gap?"Keyword gap":"คีย์เวิร์ดที่ติดทั้งคู่"}: ${r}`;$("#ih").textContent=gap?"คู่แข่งติดอันดับแต่เว็บคุณยังไม่ติด เหมาะเอาไปทำคอนเทนต์":"ทั้งสองเว็บติดอันดับ เทียบอันดับกันได้";
  $("#iu").innerHTML="<tr><th>คีย์เวิร์ด</th><th class='n'>ค้นหา/เดือน</th><th class='n'>อันดับเรา</th><th class='n'>อันดับคู่แข่ง</th></tr>"+rows.map(x=>`<tr><td>${esc(x.keyword)}</td><td class="n">${num(x.volume)}</td><td class="n">${x.me||"-"}</td><td class="n">${x.rival||"-"}</td></tr>`).join("");$("#idlg").showModal()}
 else if(t.id==="cadd")openDlg(p);
 else if(t.id==="cexp"){const q=v=>`"${String(v??"").replace(/"/g,'""')}"`,ov=SNAP.overview||{};
  const rows=[["domain","common_keywords","keyword_gap","est_traffic","backlinks"],...p.competitors.filter(r=>SNAP.gaps[r]||ov[r]).map(r=>{const g=SNAP.gaps[r]||[];return [r,g.filter(x=>x.kind==="both").length,g.filter(x=>x.kind==="gap").length,(ov[r]||{}).traffic,(ov[r]||{}).backlinks]})];
  const a=document.createElement("a");a.href=URL.createObjectURL(new Blob(["\ufeff"+rows.map(r=>r.map(q).join(",")).join("\n")],{type:"text/csv"}));a.download="competitors.csv";a.click()}
 else if(t.id==="crem"){const del=[...document.querySelectorAll("#cover input[data-r]:checked")].map(x=>x.dataset.r);
  if(del.length&&confirm(`ลบคู่แข่ง ${del.length} เว็บออกจากโปรเจ็กต์?`)){await api("/api/projects/"+p.id,"PUT",{name:p.name,domain:p.domain,location:p.location,lang:p.lang,competitors:p.competitors.filter(c=>!del.includes(c)).join("\n")});await load(p.id)}}};
// ---- ติดตามอันดับ ----
const fday=d=>`${+d.slice(8)} ${MON[+d.slice(5,7)-1]}`;
function lc(days,series,inv){if(days.length<2)return '<p class="msg">ต้องเช็กอันดับอย่างน้อย 2 ครั้งจึงจะเห็นแนวโน้ม</p>';
 const mx=Math.max(4,...series.flatMap(s=>s.vals)),r=mx/4,pw=10**Math.floor(Math.log10(r)),fr=r/pw,step=Math.max(1,(fr<=1?1:fr<=2?2:fr<=5?5:10)*pw),top=step*Math.ceil(mx/step);
 const W=900,H=260,L=46,R=16,T=12,B=34,x=i=>L+(W-L-R)*i/(days.length-1),y=v=>inv?T+(H-T-B)*(v-1)/(top-1):T+(H-T-B)*(1-v/top),ev=Math.ceil(days.length/10),ticks=inv?[1,...Array.from({length:Math.floor(top/step)},(_,i)=>(i+1)*step)]:Array.from({length:Math.floor(top/step)+1},(_,i)=>i*step);
 let g="";for(const t of ticks)g+=`<line x1="${L}" x2="${W-R}" y1="${y(t)}" y2="${y(t)}" style="stroke:var(--line)" stroke-dasharray="4 4"/><text x="${L-8}" y="${y(t)+4}" text-anchor="end" font-size="12" style="fill:var(--mute)">${t}</text>`;
 days.forEach((m,i)=>{if(i%ev===0)g+=`<text x="${x(i)}" y="${H-10}" text-anchor="middle" font-size="12" style="fill:var(--mute)">${fday(m)}</text>`});
 const paths=series.map((s,k)=>{let d=`M${x(0)},${y(s.vals[0])}`;for(let i=1;i<s.vals.length;i++){const xm=(x(i-1)+x(i))/2;d+=`C${xm},${y(s.vals[i-1])} ${xm},${y(s.vals[i])} ${x(i)},${y(s.vals[i])}`}
  return `<path d="${d}" fill="none" style="stroke:${COL[k%6]}" stroke-width="2.5" stroke-linecap="round"/>`}).reverse().join("");
 return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${inv?'กราฟอันดับเฉลี่ยตามเวลา ตำแหน่งยิ่งสูงยิ่งดี':'กราฟจำนวนคีย์เวิร์ดที่ติดอันดับ Top 10 ตามเวลา'}">${g}${paths}</svg><div class="legend">${series.map((s,k)=>`<span><i style="display:inline-block;width:18px;height:3px;background:${COL[k%6]};vertical-align:middle;margin-right:6px"></i>${esc(s.name)}${k?"":" (เรา)"}</span>`).join("")}</div>`}
let RK=null;
function chg(r){if(r.n<2)return"-";if(r.pos==null)return r.prev!=null?'<span class="lose">หลุดอันดับ</span>':"-";if(r.prev==null)return'<span class="win">ติดอันดับใหม่</span>';
 const d=r.prev-r.pos;return d>0?`<span class="win">▲ ${d}</span>`:d<0?`<span class="lose">▼ ${-d}</span>`:"-"}
async function refreshRank(){const o=$("#rout");if(!cur){RK=null;$("#rdate").textContent="";return o.innerHTML=noProj()}RK=await api("/api/rank?project_id="+cur);drawRank()}
function drawRank(){const d=RK,o=$("#rout"),s=d.summary,riv=proj().competitors;$("#rdate").textContent=s.day?"เช็กล่าสุด: "+s.day:"ยังไม่เคยเช็กอันดับ";
 if(!d.rows.length)return o.innerHTML='<p class="msg">ยังไม่มีคีย์เวิร์ดที่ติดตาม เพิ่มด้านบน หรือกด "เพิ่มจากที่บันทึกไว้"</p>';
 const k=[["คีย์เวิร์ดที่ติดตาม",s.tracked],["ติด Top 3",s.top3],["ติด Top 10",s.top10],["อันดับเฉลี่ย",s.avg??"-"],["อันดับขึ้น / ตก",`${s.up} / ${s.down}`]];
 o.innerHTML=`<div class="kpis">${k.map(([t,v])=>`<div class="card"><div class="lbl">${t}</div><div class="big">${v}</div></div>`).join("")}</div>
 ${cmpRank(d)}<div class="card" style="margin:16px 0"><h2 class="ttl">จำนวนคีย์เวิร์ดที่ติด Top 10</h2>${lc(d.days,Object.entries(d.trend).map(([n,v])=>({name:n,vals:v.map(x=>x.top10)})))}</div>
 <div class="wrap"><table><tr><th>คีย์เวิร์ด</th><th class="n">ค้นหา/เดือน</th><th class="n">อันดับเรา</th><th class="n">เปลี่ยนแปลง</th><th class="n">ดีที่สุด</th>${riv.map(r=>`<th class="n">${esc(r)}</th>`).join("")}<th></th></tr>`+
 d.rows.map(r=>`<tr><td>${esc(r.keyword)}</td><td class="n">${num(r.volume)}</td><td class="n">${r.n?(r.pos??"ไม่ติด"):"-"}</td><td class="n">${chg(r)}</td><td class="n">${r.best??"-"}</td>${riv.map(c=>`<td class="n">${r.n?(r.rivals[c]??"ไม่ติด"):"-"}</td>`).join("")}<td><button class="sm" data-rm="${esc(r.keyword)}">ลบ</button></td></tr>`).join("")+
 `</table></div><p class="cap" style="margin:8px 0 0">"ไม่ติด" = ไม่พบใน ${d.depth} อันดับแรกของ Google</p>`}
const addKw=async b=>{if(!cur)return;try{const r=await api("/api/rank/add","POST",{project_id:cur,...b});$("#radd").value="";$("#rmsg").textContent=`เพิ่มแล้ว ${r.added} คำ`;refreshRank()}catch(e){$("#rmsg").textContent=e.message}};
$("#raddb").onclick=()=>addKw({keywords:$("#radd").value});$("#rsaved").onclick=()=>addKw({from_saved:true});
$("#rout").onclick=async e=>{const k=e.target.dataset.rm;if(k){await api("/api/rank/remove","POST",{project_id:cur,keyword:k});refreshRank()}};
$("#rrun").onclick=async()=>{if(!cur||!RK||!RK.rows.length)return $("#rmsg").textContent="เพิ่มคีย์เวิร์ดก่อน";
 if(!confirm(`เช็กอันดับ ${RK.rows.length} คีย์เวิร์ด จะเรียก SERP API ${RK.rows.length} ครั้ง (คิดค่าใช้จ่ายตามจำนวนคำ) ดำเนินการต่อ?`))return;
 $("#rout").innerHTML='<p class="msg">กำลังเช็กอันดับจาก Google…</p>';try{await api("/api/rank/run","POST",{project_id:cur});refreshRank()}catch(e){$("#rout").innerHTML=`<p class="err">${esc(e.message)}</p>`}};

// ---- ภาพรวม ----
async function refreshDash(){const o=$("#dout");if(!cur){o.innerHTML=noProj();return}
 const all=await api("/api/dashboard"),m=all.find(x=>x.id==cur);if(!m){o.innerHTML=noProj();return}
 const rk=await api("/api/rank?project_id="+cur),a=m.audit,s=m.rank,mv=rk.rows.filter(r=>r.n>1&&r.pos&&r.prev).map(r=>({...r,d:r.prev-r.pos}));
 const list=(arr,up)=>arr.length?arr.slice(0,5).map(r=>`<div class="row"><span>${esc(r.keyword)}</span><b class="${up?"win":"lose"}">${up?"▲":"▼"} ${Math.abs(r.d)}</b></div>`).join(""):'<p class="msg">ยังไม่มีข้อมูล</p>';
 const k=[["คะแนน Site Audit",a?a.score:"-",a?"ตรวจ "+a.created.slice(0,10):"ยังไม่เคยตรวจ"],["ปัญหา SEO",a?a.issues:"-","จาก Site Audit"],["คีย์เวิร์ดติด Top 10",`${s.top10} / ${s.tracked}`,s.day?"เช็ก "+s.day:"ยังไม่เคยเช็ก"],["อันดับเฉลี่ย",s.avg??"-",s.rival_best!=null?`รวมคำที่ไม่ติด: เรา ${s.avg_all} / คู่แข่งดีสุด ${s.rival_best}`:"เฉพาะคำที่ติดอันดับ"],["ทราฟฟิกโดยประมาณ",m.traffic==null?"-":num(m.traffic),"ต่อเดือน"]];
 o.innerHTML=`<h2 class="ttl">ภาพรวม ${esc(m.name)}</h2><div class="kpis">${k.map(([t,v,c])=>`<div class="card"><div class="lbl">${t}</div><div class="big">${v}</div><p class="cap">${c}</p></div>`).join("")}</div>
 <div class="grid2 eq"><div class="card"><div class="lbl">คีย์เวิร์ดที่อันดับขึ้น</div>${list(mv.filter(r=>r.d>0).sort((x,y)=>y.d-x.d),true)}</div><div class="card"><div class="lbl">คีย์เวิร์ดที่อันดับตก</div>${list(mv.filter(r=>r.d<0).sort((x,y)=>x.d-y.d),false)}</div></div>
 <div class="card" style="margin-top:16px"><div class="lbl">ปัญหา SEO ที่ควรแก้ก่อน</div>${a&&a.top.length?a.top.map(x=>`<div class="row"><span><b style="color:var(--brand)">${x.count} หน้า</b> ${esc(x.label)}</span><button class="lnk" data-go="audit">ไปที่ Site Audit &rarr;</button></div>`).join(""):'<p class="msg">ยังไม่มีข้อมูล ไปที่ Site Audit แล้วกด Crawl เว็บใหม่</p>'}</div>
 <h2 class="ttl" style="margin-top:28px">ทุกโปรเจ็กต์</h2><div class="wrap"><table><tr><th>โปรเจ็กต์</th><th class="n">Site Audit</th><th class="n">ปัญหา</th><th class="n">Top 10 / ติดตาม</th><th class="n">อันดับเฉลี่ย</th><th class="n">ทราฟฟิก</th><th>เช็กอันดับล่าสุด</th><th></th></tr>`+
 all.map(p=>`<tr class="${p.id==cur?"cur":""}"><td>${esc(p.name)}<div class="cap">${esc(p.domain)}</div></td><td class="n">${p.audit?p.audit.score:"-"}</td><td class="n">${p.audit?p.audit.issues:"-"}</td><td class="n">${p.rank.top10} / ${p.rank.tracked}</td><td class="n">${p.rank.avg??"-"}</td><td class="n">${num(p.traffic)}</td><td>${p.rank.day||"-"}</td><td>${p.id==cur?"":`<button class="sm" data-open="${p.id}">เปิด</button>`}</td></tr>`).join("")+"</table></div>"}
$("#dout").onclick=e=>{const t=e.target.dataset;if(t.go)document.querySelector(`nav button[data-t="${t.go}"]`).click();else if(t.open)load(+t.open)};
// ---- ไอเดียคอนเทนต์ ----
const rows=(a,f)=>a&&a.length?a.map(x=>`<div class="row">${f(x)}</div>`).join(""):'<p class="msg">ไม่พบข้อมูล</p>',tx=x=>`<span>${esc(x)}</span>`;
$("#idseed").onkeydown=e=>e.key==="Enter"&&$("#idgo").click();
$("#idgo").onclick=async()=>{const o=$("#idout");if(!cur)return o.innerHTML=noProj();o.innerHTML='<p class="msg">กำลังหาไอเดีย…</p>';
 try{const d=await api("/api/ideas","POST",{project_id:cur,seed:$("#idseed").value});
 o.innerHTML=(d.note?`<p class="msg">${esc(d.note)}</p>`:"")+`<div class="grid2 eq"><div class="card"><div class="lbl">อันดับต้นๆ บน Google</div>${rows(d.top,x=>`<span><b>${x.pos}</b> <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a><div class="cap">${esc(x.domain)}</div></span>`)}</div>
 <div class="card"><div class="lbl">คำถามที่คนค้นหา (People Also Ask)</div>${rows(d.paa,tx)}</div></div>
 <div class="grid2 eq" style="margin-top:16px"><div class="card"><div class="lbl">คำค้นที่เกี่ยวข้อง</div>${rows(d.related,tx)}</div><div class="card"><div class="lbl">คำถามและหัวข้อจาก Autocomplete</div>${rows(d.questions,tx)}</div></div>`}
 catch(e){o.innerHTML=`<p class="err">${esc(e.message)}</p>`}};
async function refreshGapIdeas(){const o=$("#gapout");if(!cur){o.innerHTML="";return}const r=await api("/api/gapideas?project_id="+cur);
 o.innerHTML=r.length?`<div class="wrap"><table><tr><th>คีย์เวิร์ด</th><th class="n">ค้นหา/เดือน</th><th>คู่แข่งที่ติด</th><th class="n">อันดับคู่แข่ง</th></tr>`+r.map(x=>`<tr><td>${esc(x.keyword)}</td><td class="n">${num(x.volume)}</td><td>${esc(x.rival)}</td><td class="n">${x.pos}</td></tr>`).join("")+"</table></div>":'<p class="msg">ยังไม่มีข้อมูล ไปที่ "เปรียบเทียบคู่แข่ง" แล้วกดเปรียบเทียบก่อน</p>'}

// ---- หน้ายอดนิยม ----
let TP={};
async function refreshPages(){if(!cur){$("#pout").innerHTML=noProj();return}TP=await api("/api/toppages?project_id="+cur);const p=proj();
 $("#pdom").innerHTML=[p.domain,...p.competitors].map((d,i)=>`<option value="${esc(d)}">${i?"":"(เรา) "}${esc(d)}</option>`).join("");drawPages()}
let PERR={};
function drawPages(){const d=$("#pdom").value,t=TP[d],o=$("#pout"),gsc=t&&t.pages.length&&t.pages[0].src==="gsc",lnk=x=>`<td style="word-break:break-all"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a></td>`;
 $("#pdate").textContent=t?"ดึงข้อมูลเมื่อ "+t.created+(gsc?" (Search Console, 28 วันล่าสุด)":""):"";
 const err=PERR[d]?`<p class="err">${esc(PERR[d])}</p>`:"";
 o.innerHTML=err+(t?(t.pages.length?(gsc?`<div class="wrap"><table><tr><th>หน้า</th><th class="n">คลิก (28 วัน)</th><th class="n">การแสดงผล</th><th class="n">อันดับเฉลี่ย</th><th class="n">คีย์เวิร์ดที่พาคนเข้า</th></tr>`+t.pages.map(x=>`<tr>${lnk(x)}<td class="n">${num(x.traffic)}</td><td class="n">${num(x.impressions)}</td><td class="n">${x.position}</td><td class="n">${num(x.keywords)}</td></tr>`).join("")+"</table></div><p class='cap'>ข้อมูลจริงจาก Google Search Console</p>"
 :`<div class="wrap"><table><tr><th>หน้า</th><th class="n">ทราฟฟิกโดยประมาณ/เดือน</th><th class="n">คีย์เวิร์ดที่ติดอันดับ</th></tr>`+t.pages.map(x=>`<tr>${lnk(x)}<td class="n">${num(x.traffic)}</td><td class="n">${num(x.keywords)}</td></tr>`).join("")+"</table></div>"):'<p class="msg">ไม่พบข้อมูลของเว็บนี้</p>'):(err?"":'<p class="msg">ยังไม่เคยดึงข้อมูล กด "ดึงข้อมูลใหม่"</p>'))}
$("#pdom").onchange=drawPages;
$("#prun").onclick=async()=>{if(!cur)return;if(!confirm(`ดึงหน้ายอดนิยมของ ${proj().competitors.length+1} เว็บ (เว็บเราใช้ Search Console ฟรี ถ้าตั้งไว้ ส่วนคู่แข่งคิดค่า DataForSEO ต่อเว็บ) ดำเนินการต่อ?`))return;
 $("#pout").innerHTML='<p class="msg">กำลังดึงข้อมูล…</p>';
 const r=await fetch("/api/toppages/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project_id:cur})}),d=await r.json().catch(()=>({}));
 PERR=d.errors||{};if(!r.ok&&!d.errors){$("#pout").innerHTML=`<p class="err">${esc(d.error||"เกิดข้อผิดพลาด")}</p>`;return}refreshPages()};

// ---- สิ่งที่ควรทำ ----
async function refreshTodo(){const o=$("#tout");if(!cur)return o.innerHTML=noProj();const d=await api("/api/todo?project_id="+cur),n=d.items.filter(x=>x.done).length,pc=["","#ef4444","#f59e0b","#22c55e"],pl=["","สำคัญมาก","ควรทำ","ถ้ามีเวลา"];
 o.innerHTML=`<p class="msg">ทำแล้ว ${n} จาก ${d.items.length} รายการ สร้างจากผล Site Audit, อันดับ และ keyword gap ล่าสุด</p>`+(d.items.length?`<div class="card">`+d.items.map(x=>`<label class="row todo" style="${x.done?"opacity:.55":""}"><span style="display:flex;gap:12px;align-items:flex-start"><input type="checkbox" data-k="${esc(x.key)}" ${x.done?"checked":""}><span><b style="${x.done?"text-decoration:line-through":""}">${esc(x.text)}</b><div class="cap">${esc(x.detail)}</div></span></span><span class="cap" style="white-space:nowrap"><i class="dot" style="background:${pc[x.pri]}"></i>${pl[x.pri]}, ${esc(x.cat)}</span></label>`).join("")+"</div>":'<p class="msg">ไม่มีรายการ</p>')}
$("#tout").onchange=async e=>{const k=e.target.dataset.k;if(k){await api("/api/todo/toggle","POST",{project_id:cur,key:k,done:e.target.checked});refreshTodo()}};

// ---- ตั้งเวลาอัตโนมัติ ----
const EVERY=[[0,"ปิด"],[1,"ทุกวัน"],[3,"ทุก 3 วัน"],[7,"ทุกสัปดาห์"],[14,"ทุก 2 สัปดาห์"],[30,"ทุกเดือน"]];
async function refreshSched(){const o=$("#sched");if(!cur){o.innerHTML="";return}const s=await api("/api/schedule?project_id="+cur),sel=(id,v)=>`<select id="${id}">${EVERY.map(([n,t])=>`<option value="${n}" ${n==v?"selected":""}>${t}</option>`).join("")}</select>`;
 o.innerHTML=`<h2 class="ttl" style="margin-top:28px">ตั้งเวลาอัตโนมัติ</h2><div class="card"><div class="bar"><label>Crawl เว็บ ${sel("sa",s.audit_every)}</label><label>เช็กอันดับ ${sel("sr",s.rank_every)}</label>
 <label>เริ่มหลังเวลา <select id="sh">${[...Array(24).keys()].map(h=>`<option value="${h}" ${h==s.hour?"selected":""}>${String(h).padStart(2,"0")}:00</option>`).join("")}</select></label><button class="sm" id="ssave">บันทึก</button><span class="msg" id="smsg"></span></div>
 <p class="cap">ล่าสุด: crawl ${s.last_audit||"-"}, เช็กอันดับ ${s.last_rank||"-"}${s.last_msg?", "+esc(s.last_msg):""}</p><p class="cap">ทำงานเฉพาะตอนที่เปิด app.py ค้างไว้ และมีค่าใช้จ่าย API ทุกรอบ</p></div>`}
$("#sched").onclick=async e=>{if(e.target.id==="ssave"){await api("/api/schedule","POST",{project_id:cur,audit_every:+$("#sa").value,rank_every:+$("#sr").value,hour:+$("#sh").value});$("#smsg").textContent="บันทึกแล้ว"}};

// ---- รายงาน (พิมพ์เป็น PDF) ----
$("#repbtn").onclick=()=>window.print();
async function buildReport(){const o=$("#repout");if(!cur)return o.innerHTML=noProj();o.innerHTML='<p class="msg">กำลังสร้างรายงาน…</p>';
 const p=proj(),[all,rk,au,hs,td]=await Promise.all([api("/api/dashboard"),api("/api/rank?project_id="+cur),api("/api/audit/latest?project_id="+cur),api("/api/snapshots?project_id="+cur),api("/api/todo?project_id="+cur)]);
 const m=all.find(x=>x.id==cur),a=au.data,s=rk.summary,sn=hs.length?(await api("/api/snapshots/"+hs[0].id)).data:null,keep=x=>x===p.domain||p.competitors.includes(x);
 const ov=sn?Object.fromEntries(Object.entries(sn.overview||{}).filter(([k])=>keep(k))):{},rv=p.competitors.filter(c=>ov[c]||(sn&&sn.gaps&&sn.gaps[c]));
 const sec=(t,b)=>`<div class="card rep"><h2 class="ttl" style="font-size:19px">${t}</h2>${b}</div>`,kp=(t,v)=>`<div class="card"><div class="lbl">${t}</div><div class="big">${v}</div></div>`;
 const cnt=(r,k)=>sn&&sn.gaps&&sn.gaps[r]?sn.gaps[r].filter(x=>x.kind===k).length:"-";
 o.innerHTML=`<h1 class="ttl" style="font-size:28px;margin-bottom:4px">รายงาน SEO ${esc(p.name)}</h1><p class="cap" style="margin-bottom:16px">${esc(p.domain)} | สร้างเมื่อ ${new Date().toLocaleDateString("th-TH",{dateStyle:"long"})}</p>
 <div class="kpis">${kp("คะแนน Site Audit",a?a.score:"-")}${kp("ปัญหา SEO",a?a.total_issues:"-")}${kp("คีย์เวิร์ดติด Top 10",`${s.top10} / ${s.tracked}`)}${kp("ทราฟฟิกโดยประมาณ",m.traffic==null?"-":num(m.traffic))}</div>`+
 sec("ทราฟฟิกจาก Organic เทียบคู่แข่ง",sn?chart(ov,[p.domain,...rv]):'<p class="msg">ยังไม่มีข้อมูล ไปที่เปรียบเทียบคู่แข่งก่อน</p>')+
 (rv.length?sec("คู่แข่ง",`<table><tr><th>เว็บ</th><th class="n">ติดทั้งคู่</th><th class="n">Keyword gap</th><th class="n">ทราฟฟิก</th><th class="n">Backlinks</th></tr>`+rv.map(r=>`<tr><td>${esc(r)}</td><td class="n">${cnt(r,"both")}</td><td class="n">${cnt(r,"gap")}</td><td class="n">${num((ov[r]||{}).traffic)}</td><td class="n">${num((ov[r]||{}).backlinks)}</td></tr>`).join("")+"</table>"):"")+
 sec("สถานะเว็บไซต์ (Site Audit)",a?`<p>crawl ${a.pages.length} หน้า สำเร็จ ${a.counts.success}, Redirect ${a.counts.redirected}, ลิงก์เสีย ${a.counts.broken}, ถูกบล็อก ${a.counts.blocked}</p>`+a.issues.slice(0,8).map(x=>`<div class="row"><span>${esc(x.label)}</span><b>${x.count} หน้า</b></div>`).join(""):'<p class="msg">ยังไม่เคย crawl</p>')+
 sec("อันดับคีย์เวิร์ด",rk.rows.length?`<p>ติด Top 3: ${s.top3}, Top 10: ${s.top10}, อันดับเฉลี่ย ${s.avg??"-"}, อันดับขึ้น ${s.up}, ตก ${s.down} (เช็กล่าสุด ${s.day||"-"})</p>`+lc(rk.days,Object.entries(rk.trend).map(([n,v])=>({name:n,vals:v.map(x=>x.top10)})))+`<table><tr><th>คีย์เวิร์ด</th><th class="n">ค้นหา/เดือน</th><th class="n">อันดับ</th><th class="n">เปลี่ยนแปลง</th></tr>`+[...rk.rows].sort((x,y)=>(y.volume||0)-(x.volume||0)).slice(0,15).map(r=>`<tr><td>${esc(r.keyword)}</td><td class="n">${num(r.volume)}</td><td class="n">${r.n?(r.pos??"ไม่ติด"):"-"}</td><td class="n">${chg(r)}</td></tr>`).join("")+"</table>":'<p class="msg">ยังไม่มีคีย์เวิร์ดที่ติดตาม</p>')+
 cmpRank(rk).replace('class="card" style="margin:16px 0"','class="card rep"')+sec("สิ่งที่ควรทำต่อ",td.items.filter(x=>!x.done).slice(0,10).map(x=>`<div class="row"><span><b>${esc(x.text)}</b><div class="cap">${esc(x.detail)}</div></span></div>`).join("")||'<p class="msg">ไม่มีรายการค้าง</p>')+
 '<p class="cap" style="margin-top:16px">รายงานนี้สร้างจากข้อมูลล่าสุดที่เก็บไว้ในเครื่องมือ ตัวเลขทราฟฟิกเป็นค่าประมาณ</p>'}
// ---- เปรียบเทียบอันดับเฉลี่ยกับคู่แข่ง ----
function cmpRank(d){const c=d.compare||[];if(!c.length||!c[0].tracked||!d.days.length)return"";const best=Math.min(...c.map(x=>x.avg_all??1e9)),hh=c.slice(1).filter(x=>x.h2h&&x.h2h.common);
 return `<div class="card" style="margin:16px 0"><h2 class="ttl">เปรียบเทียบอันดับเฉลี่ยกับคู่แข่ง</h2><div class="wrap"><table><tr><th>เว็บ</th><th class="n">อันดับเฉลี่ย (เฉพาะคำที่ติด)</th><th class="n">อันดับเฉลี่ย (รวมคำที่ไม่ติด)</th><th class="n">ติดอันดับ</th><th class="n">Top 3</th><th class="n">Top 10</th></tr>`+
 c.map((x,i)=>`<tr class="${i?"":"cur"}"><td>${esc(x.domain)}${i?"":" (เรา)"}</td><td class="n">${x.avg??"-"}</td><td class="n ${x.avg_all===best?"win":""}">${x.avg_all??"-"}</td><td class="n">${x.ranked} / ${x.tracked}</td><td class="n">${x.top3}</td><td class="n">${x.top10}</td></tr>`).join("")+
 `</table></div><p class="cap" style="margin:8px 0 12px">ตัวเลขน้อยกว่า = อยู่สูงกว่า ดีกว่า คอลัมน์ "รวมคำที่ไม่ติด" นับคำที่ไม่ติดเป็นอันดับ ${d.depth+1} เพื่อเทียบเว็บที่ติดจำนวนคำไม่เท่ากันได้ยุติธรรมกว่า</p>`+
 (hh.length?`<div class="lbl">เทียบเฉพาะคำที่ทั้งสองเว็บติดอันดับ</div><div class="wrap"><table><tr><th>คู่แข่ง</th><th class="n">จำนวนคำ</th><th class="n">อันดับเฉลี่ยเรา</th><th class="n">อันดับเฉลี่ยคู่แข่ง</th><th class="n">เราสูงกว่า / ต่ำกว่า</th></tr>`+hh.map(x=>{const h=x.h2h;return `<tr><td>${esc(x.domain)}</td><td class="n">${h.common}</td><td class="n ${h.me_avg<h.rival_avg?"win":""}">${h.me_avg}</td><td class="n ${h.rival_avg<h.me_avg?"lose":""}">${h.rival_avg}</td><td class="n">${h.me_wins} / ${h.rival_wins}</td></tr>`}).join("")+"</table></div>":"")+
 `<div class="lbl" style="margin-top:16px">อันดับเฉลี่ยตามเวลา (รวมคำที่ไม่ติด ตำแหน่งยิ่งอยู่สูงยิ่งดี)</div>${lc(d.days,Object.entries(d.trend).map(([n,v])=>({name:n,vals:v.map(x=>x.avg_all)})),true)}</div>`}
async function refreshDfs(){try{const d=await api("/api/dfs/status");$("#dfs").innerHTML=d.ok?`DataForSEO: เชื่อมต่อแล้ว${d.balance!=null?` (เครดิตคงเหลือ $${Number(d.balance).toFixed(2)})`:""}`:`<span class="err">DataForSEO: ${esc(d.message)}</span>`}catch(e){}
 try{const g=await api("/api/gsc/status"+(cur?"?project_id="+cur:""));$("#gsc").innerHTML=g.ok?`Search Console: เชื่อมต่อแล้ว (ฟรี)`:`<span class="cap">Search Console: ${esc(g.message)}</span>`}catch(e){}}
load();
</script></body></html>"""


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


# เริ่มตอน import เพื่อให้ทำงานใต้ gunicorn ด้วย (railway.json ใช้ 1 worker จึงมี scheduler ตัวเดียว)
if os.getenv("SEO_SCHEDULER", "1") != "0":
    threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=False)
