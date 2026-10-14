# SEO ส่วนตัว

เครื่องมือหาคีย์เวิร์ดและเทียบเว็บกับคู่แข่ง (Flask + SQLite)

## รันในเครื่อง

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/flask --app app.py run --port 5001
```

เปิด http://localhost:5001 (รันในเครื่องไม่ต้องใส่รหัสผ่าน เว้นแต่ตั้ง `APP_PASSWORD`)

## ขึ้น Railway

1. เอาโฟลเดอร์นี้ขึ้น GitHub เป็น repo แบบ **private** (`.gitignore` กัน `.venv` และ `seo.db` ไว้แล้ว)
2. ที่ railway.com → **New Project** → **Deploy from GitHub repo** → เลือก repo นี้
3. **เพิ่ม Volume** (สำคัญ ไม่งั้นข้อมูลหายทุกครั้งที่ deploy):
   คลิกขวาที่ service → **Attach Volume** → Mount path ใส่ `/data`
   แอปจะเก็บ `seo.db` ใน volume ให้เอง
4. ไปแท็บ **Variables** ของ service แล้วเพิ่ม:

   | ตัวแปร | ค่า | จำเป็นไหม |
   |---|---|---|
   | `APP_PASSWORD` | รหัสผ่านเข้าเว็บ (ตั้งให้ยาวและเดายาก) | **จำเป็น** ไม่ตั้ง เว็บจะขึ้น 503 |
   | `APP_USER` | ชื่อผู้ใช้เข้าเว็บ | ไม่จำเป็น (ค่าเริ่มต้น `admin`) |
   | `DATAFORSEO_LOGIN` | อีเมล API ของ DataForSEO | ไม่จำเป็น |
   | `DATAFORSEO_PASSWORD` | API password ของ DataForSEO | ไม่จำเป็น |
   | `GSC_CREDENTIALS_JSON` | เนื้อหาไฟล์ JSON ของ service account ทั้งไฟล์ (ดูหัวข้อ Google Search Console ด้านล่าง) | ไม่จำเป็น (ฟรี ใช้แทน DataForSEO ได้สำหรับเว็บของเราเอง) |
   | `APP_TZ` | เขตเวลาของตารางรันอัตโนมัติ | ไม่จำเป็น (ค่าเริ่มต้น `Asia/Bangkok`) |
   | `SEO_RANK_DEPTH` | ค้นหาอันดับลึกกี่อันดับ (ยิ่งลึกยิ่งเปลือง DataForSEO) | ไม่จำเป็น (ค่าเริ่มต้น `50`) |

5. **Settings → Networking → Generate Domain** จะได้ลิงก์ `xxx.up.railway.app`
6. เปิดลิงก์ เบราว์เซอร์จะถามชื่อผู้ใช้/รหัสผ่าน ให้ใส่ตามที่ตั้งในข้อ 4

## Google Search Console (ฟรี)

ใช้ข้อมูลจริงของเว็บเราเองแทน DataForSEO ในกราฟ "ทราฟฟิกจาก Organic" และหน้า "หน้ายอดนิยม"
(ข้อมูลเว็บคู่แข่งยังต้องใช้ DataForSEO เพราะ Search Console ดูได้เฉพาะเว็บที่เราเป็นเจ้าของ)

1. เว็บต้องอยู่ใน Search Console แล้ว (search.google.com/search-console)
2. ไปที่ console.cloud.google.com → สร้างโปรเจ็กต์ใหม่ (ชื่ออะไรก็ได้)
3. เมนู **APIs & Services → Library** → ค้นหา **Google Search Console API** → **Enable**
4. เมนู **IAM & Admin → Service Accounts** → **Create service account** → ตั้งชื่อ → กด Done (ไม่ต้องให้ role)
5. คลิก service account ที่สร้าง → แท็บ **Keys** → **Add key → Create new key → JSON** → จะได้ไฟล์ .json
6. คัดลอกอีเมลของ service account (ลงท้าย `iam.gserviceaccount.com`)
7. ใน Search Console → **การตั้งค่า → ผู้ใช้และสิทธิ์ → เพิ่มผู้ใช้** → ใส่อีเมลจากข้อ 6 → สิทธิ์ **จำกัด (Restricted)**
8. ใน Railway → Variables → เพิ่ม `GSC_CREDENTIALS_JSON` แล้ววางเนื้อหาไฟล์ .json ทั้งไฟล์ → **Deploy**
9. เปิดเว็บ แถบซ้ายควรขึ้น "Search Console: เชื่อมต่อแล้ว (ฟรี)" แล้วกด "ดึงข้อมูลใหม่" / "เปรียบเทียบ" อีกครั้ง

ไฟล์ .json เป็นกุญแจเข้าถึงข้อมูล อย่า commit ขึ้น GitHub หรือส่งให้ใคร

### หมายเหตุ
- ค่าตั้ง start command อยู่ใน `railway.json` (ใช้ gunicorn, 1 worker เพื่อให้ SQLite ปลอดภัย, timeout 10 นาทีเพราะการเทียบคู่แข่งใช้เวลานาน)
- ตารางรันอัตโนมัติ (audit / เช็กอันดับ) ทำงานใน thread ของแอปเอง จึงต้องใช้ 1 worker ห้ามเพิ่ม `--workers` ไม่งั้นจะรันซ้ำ
- อย่าใส่รหัสใดๆ ลงในโค้ดหรือ commit ขึ้น GitHub ให้ใส่ใน Variables ของ Railway เท่านั้น
