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

5. **Settings → Networking → Generate Domain** จะได้ลิงก์ `xxx.up.railway.app`
6. เปิดลิงก์ เบราว์เซอร์จะถามชื่อผู้ใช้/รหัสผ่าน ให้ใส่ตามที่ตั้งในข้อ 4

### หมายเหตุ
- ค่าตั้ง start command อยู่ใน `railway.json` (ใช้ gunicorn, 1 worker เพื่อให้ SQLite ปลอดภัย, timeout 10 นาทีเพราะการเทียบคู่แข่งใช้เวลานาน)
- อย่าใส่รหัสใดๆ ลงในโค้ดหรือ commit ขึ้น GitHub ให้ใส่ใน Variables ของ Railway เท่านั้น
