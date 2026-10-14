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
import csv, hmac, io, json, os, re, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

DFS_LOGIN = os.getenv("DATAFORSEO_LOGIN")
DFS_PASS = os.getenv("DATAFORSEO_PASSWORD")
APP_USER = os.getenv("APP_USER") or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD")
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT"))
UA = {"User-Agent": "Mozilla/5.0 (compatible; MySEOTool/1.0)"}
# บน Railway ให้เก็บ seo.db ไว้ใน Volume (Railway ตั้ง RAILWAY_VOLUME_MOUNT_PATH ให้อัตโนมัติ) ไม่งั้นข้อมูลหายตอน deploy
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("SEO_DB_PATH") or os.path.join(DATA_DIR, "seo.db")
os.makedirs(os.path.dirname(DB), exist_ok=True)
MAX_RIVALS = 5

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


@app.post("/api/compare")
def api_compare():
    p = get_project(request.json.get("project_id"))
    if not p:
        return jsonify(error="ไม่พบโปรเจ็กต์"), 404
    if not p["competitors"]:
        return jsonify(error="ยังไม่มีเว็บคู่แข่ง กด 'แก้ไข' เพื่อเพิ่มก่อน"), 400
    with ThreadPoolExecutor(4) as ex:
        sites = list(ex.map(audit_safe, [p["domain"]] + p["competitors"]))
    gaps, note = {}, ""
    for r in p["competitors"]:
        try:
            gaps[r] = gap(p["domain"], r, p["location"], p["lang"])
        except Exception as e:
            note = f"ข้าม keyword gap: {e}"
            break
    data = {"sites": sites, "gaps": gaps, "gap_note": note}
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


# ---------- หน้าเว็บ ----------
PAGE = r"""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SEO ส่วนตัว</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600&family=IBM+Plex+Sans+Thai+Looped:wght@700&display=swap" rel="stylesheet">
<style>
:root{--bg:#eef1ee;--panel:#fff;--ink:#1c2b2a;--mute:#5d6d6b;--line:#d5dcd8;--brand:#0f5c57;--win:#1b7f4b;--lose:#b3412b;--soft:#e2efe9}
@media(prefers-color-scheme:dark){:root{--bg:#121917;--panel:#1a2321;--ink:#e6efec;--mute:#9bb0ab;--line:#2c3b38;--brand:#5fc7bb;--win:#5fd08f;--lose:#f08a72;--soft:#20332f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "IBM Plex Sans Thai",system-ui,sans-serif}
header{padding:24px 20px 0;max-width:1080px;margin:auto}
h1{font:700 26px "IBM Plex Sans Thai Looped","IBM Plex Sans Thai",sans-serif;margin:0 0 12px;color:var(--brand)}
.pbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:4px}
select,input,textarea{padding:9px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit}
#proj{min-width:220px;font-weight:600}.info{color:var(--mute);margin:0 0 14px}
nav{display:flex;gap:4px;border-bottom:2px solid var(--line)}
nav button{background:none;border:0;padding:10px 18px;font:600 15px inherit;font-family:inherit;color:var(--mute);cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px}
nav button[aria-selected=true]{color:var(--brand);border-color:var(--brand)}
main{max-width:1080px;margin:auto;padding:20px}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;align-items:center}.bar input{flex:1;min-width:200px}
button{font:inherit;cursor:pointer}
.go{padding:10px 22px;border:0;border-radius:6px;background:var(--brand);color:var(--bg);font-weight:600}
.sm{padding:6px 14px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.danger{color:var(--lose)}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid var(--brand);outline-offset:2px}
.msg{color:var(--mute);margin:8px 0}.err{color:var(--lose)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--soft);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}.win{color:var(--win);font-weight:600}.lose{color:var(--lose);font-weight:600}
.tabs2{display:flex;gap:8px;margin:14px 0 10px;flex-wrap:wrap;align-items:center}
.tabs2 button{padding:6px 14px;border:1px solid var(--line);border-radius:20px;background:var(--panel);color:var(--ink)}
.tabs2 button[aria-pressed=true]{background:var(--brand);color:var(--bg);border-color:var(--brand)}
h2{font-size:17px;margin:22px 0 8px}.long{max-width:300px;word-break:break-word;color:var(--mute)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);width:min(480px,92vw);padding:22px}
dialog::backdrop{background:rgba(0,0,0,.45)}dialog label{display:block;margin:12px 0 4px;font-weight:600}
dialog input,dialog select,dialog textarea{width:100%}.acts{display:flex;gap:8px;margin-top:18px}.acts .sp{flex:1}
</style></head><body>
<header><h1>SEO ส่วนตัว</h1>
<div class="pbar"><select id="proj" aria-label="โปรเจ็กต์"></select>
<button class="sm" id="pnew">+ โปรเจ็กต์ใหม่</button><button class="sm" id="pedit">แก้ไข</button></div>
<p class="info" id="info"></p>
<nav role="tablist"><button role="tab" aria-selected="true" data-t="kw">หาคีย์เวิร์ด</button>
<button role="tab" aria-selected="false" data-t="saved">คีย์เวิร์ดที่บันทึก</button>
<button role="tab" aria-selected="false" data-t="cmp">เปรียบเทียบคู่แข่ง</button></nav></header>
<main>
<section id="kw"><div class="bar"><input id="seed" placeholder="คำค้นหลัก เช่น รองเท้าวิ่ง"><button class="go" id="kgo">หาคีย์เวิร์ด</button></div><div id="kout"></div></section>
<section id="saved" hidden><div id="sout"></div></section>
<section id="cmp" hidden><div class="bar"><button class="go" id="cgo">เปรียบเทียบกับคู่แข่งทั้งหมด</button><select id="hist" aria-label="ประวัติ"></select></div><div id="cout"></div></section>
</main>
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
 $("#kout").innerHTML=p?"":noProj();$("#cout").innerHTML=p?"":noProj();KW=[];SNAP=null;
 refreshSaved();refreshHist(true)}
$("#proj").onchange=e=>load(+e.target.value);
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{document.querySelectorAll("nav button").forEach(x=>x.setAttribute("aria-selected",x===b));
 ["kw","saved","cmp"].forEach(id=>$("#"+id).hidden=id!==b.dataset.t)});

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
function showSnap(d){SNAP=d;const S=d.sites,ok=S.find(s=>s.audit);let h="";
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
load();
</script></body></html>"""


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(debug=False)
