# VerifyHub — Deploy Guide: Hostinger (website + API) + Render (bot)

Short version first, details below.

## Big picture — kaise connected hai sab

```
Telegram user
   │  /start, buttons
   ▼
Bot (Render — Background Worker, polling) ──┐
   │  reads/writes submissions              │
   ▼                                        ▼
Render PostgreSQL  ◄── one shared database ──►  API (Render Web Service)
   (verifyhub-db)                              (FastAPI)
                                                    ▲ Mini App calls it
                                                    │ config.js → API URL
Mini App pages (Hostinger shared hosting, public_html)
   ▲ user opens Mini App from bot
   │ MINI_APP_URL = https://YOURDOMAIN/
```

- **Website (jo user dekhta hai) → Hostinger shared hosting.** Static
  files (`index.html`, `styles.css`, `app.js`, `config.js`) in `public_html`.
- **API → Render Web Service.** `config.js` me ek line se Mini App is API
  ko point karti hai. CORS pehle se khula hai.
- **Bot → Render Background Worker.** Polling mode, no public URL needed.
- **Database → Render PostgreSQL.** Both sides connect to this ONE database.
  That is how the bot's `/status` and the website show the same submission.
- (Agar kabhi VPS le lo to Plan A: API + website dono VPS par — § 3 dekho.
  Us case me `render.yaml` se `verifyhub-api` service delete kar dena.)
- **Secrets kabhi git/chat me mat daalo.** Token, DB password — sirf Render
  dashboard aur Hostinger `.env` file me.

## Honest note — Hostinger plan ka farak (important)

- **Hostinger VPS (KVM, Ubuntu) → yehi sahi rasta hai.** Isi par FastAPI
  24x7 chal sakta hai (uvicorn + systemd + Nginx). Guide neeche usi ka hai.
- **Hostinger shared hosting (Premium/Business, sirf website wala plan) →
  is par FastAPI NAHI chalega.** Shared hosting persistent Python server
  nahi chalata — sirf CGI/cron scripts. Agar tumhare paas sirf shared
  hosting hai, to **Plan B** use karo (§ 6): frontend files Hostinger
  `public_html` me (website phir bhi Hostinger par dikhega), aur API Render
  Web Service par. Dono plan is zip me ready hain — pehle apna plan check
  karo: hPanel me "VPS" dikhe to Plan A, sirf "Web Hosting" dikhe to Plan B.

## 0. Pehle ye 4 cheezein ready rakho

| Cheez | Kahan se |
|---|---|
| Domain (e.g. `verify.example.com`) | Hostinger — DNS me A record → VPS IP (Plan A) ya hosting IP (Plan B) |
| `BOT_TOKEN` | Telegram → @BotFather → `/newbot` (ya existing bot ka token). Chat me mat bhejo. |
| `REGISTRATION_URL` | Official registration site ka link |
| Apni Telegram ID | Telegram → @userinfobot ko message karo, jo number de wahi `ADMIN_TELEGRAM_IDS` |

GitHub account bhi chahiye (Render Blueprint repo se deploy karta hai).
Repo private rakhna — secrets waise bhi repo me nahi hain.

---

## 1. Render — PostgreSQL database (pehle ye, kyunki dono ko iska URL chahiye)

1. [render.com](https://render.com) → **New → PostgreSQL**.
2. Name: `verifyhub-db`. Region: apne users ke paas wala (Singapore achha
   hai India ke liye). Plan: Free (agar free na mile to Starter).
3. Create karo. Dashboard me do URLs milenge — dono ko note karo:
   - **Internal Database URL** — sirf Render ke andar kaam karta hai.
     Ye bot ko `render.yaml` khud de dega, tumhe type nahi karna.
   - **External Database URL** — ye Hostinger VPS ko chahiye (Step 3).
4. Tables banane ki zaroorat nahi — app start hote hi tables khud bana leta
   hai (`Base.metadata.create_all`). `migrations/001_init.sql` sirf
   reference ke liye hai.

## 2. Render — Bot worker (Blueprint)

1. Is project ko GitHub repo me push karo (zip ke saare files, `.env` ke
   bina — `.env` kabhi commit mat karo).
2. Render → **New → Blueprint** → repo connect karo. Render `render.yaml`
   padh ke teen cheezein bana dega: `verifyhub-db` (database),
   `verifyhub-bot` (worker) aur `verifyhub-api` (web service).
3. Jo values maange, bharo:
   - `BOT_TOKEN` — @BotFather wala token
   - `BOT_USERNAME` — `@` ke bina, e.g. `verifyhub_bot`
   - `MINI_APP_URL` — `https://YOURDOMAIN/` (apna Hostinger domain)
   - `REGISTRATION_URL` — official link
   - `ADMIN_TELEGRAM_IDS` — apni Telegram ID (API service ke liye)
   - `DATABASE_URL` — khud set ho jayega (database se linked hai)
4. **Deploy** dabao. **Logs** me check karo — polling start hone par
   python-telegram-bot ke start messages dikhenge. Error aaye to § 7 dekho.
5. **Plan note (paise):** Background Worker 24x7 chalane ke liye Render ka
   paid plan chahiye (Starter, ~$7/month). Free tier me always-on worker
   nahi milta — free service so jata hai aur bot jawab dena band kar dega.
   Current price deploy karte time Render dashboard par check kar lena.

**Warning — ek hi poller:** Ek bot token ko sirf EK process poll kar sakta
hai. Jab Render worker live ho, apne laptop par `python -m app.bot` mat
chalao — Telegram 409 conflict dega aur bot chup ho jayega.

## 3. Hostinger VPS — Website + API (Plan A)

1. Hostinger → VPS (KVM, **Ubuntu 24.04**) lo. `hPanel → DNS` me apne
   domain ka **A record → VPS ka IP** set karo. DNS propagate hone me
   kuch minute lag sakte hain.
2. Is zip ko VPS par upload karo (FileZilla / `scp`):
   ```bash
   scp VerifyHub-hostinger-render.zip root@VPS_IP:/root/
   ssh root@VPS_IP
   cd /root && unzip -o VerifyHub-hostinger-render.zip
   cd verification-platform
   ```
3. Script me apna domain bharo aur chalao:
   ```bash
   nano deploy/hostinger/deploy-hostinger.sh   # DOMAIN="yourdomain.com" likho
   bash deploy/hostinger/deploy-hostinger.sh
   ```
   Ye Python, Nginx, venv, dependencies, systemd service aur Nginx site
   sab set kar dega. Last me jo checklist print ho, wahi follow karo.
4. Secrets bharo (VPS par, editor me):
   ```bash
   nano /opt/verifyhub/.env
   ```
   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | Wahi token jo Render par diya (dono jagah SAME hona chahiye, warna 401 aayega) |
   | `REGISTRATION_URL` | Official link |
   | `ADMIN_TELEGRAM_IDS` | Apni Telegram ID (comma-separated, e.g. `123456789`) |
   | `MINI_APP_URL` | `https://YOURDOMAIN/` |
   | `DATABASE_URL` | Render PostgreSQL ka **EXTERNAL** URL (internal wala yahan NAHI chalega) |
5. Start + HTTPS:
   ```bash
   systemctl restart verifyhub-api
   systemctl status verifyhub-api        # active (running) hona chahiye
   curl http://127.0.0.1:8000/api/health  # {"ok":true}
   certbot --nginx -d YOURDOMAIN         # HTTPS — Telegram ko https chahiye
   ```
6. Browser me check karo: `https://YOURDOMAIN/` (Mini App dikhe) aur
   `https://YOURDOMAIN/api/health` (`{"ok":true}`).

## 4. BotFather — Menu Button (Mini App ka button)

Telegram me @BotFather:
1. `/setmenubutton` → apna bot chuno → **Web App** → URL:
   `https://YOURDOMAIN/`
2. (Optional) `/setdescription` aur `/about` me wahi lines likho jo bot ke
   `/start` me hain.
3. Bot ko `/start` bhejo → **Open Verification Panel** dabao → Mini App
   khulni chahiye. Agar blank screen aaye to § 7 dekho.

## 5. End-to-end test (live jaane se pehle)

1. `/start` → **Register Account** → official site khule.
2. Wahan register karo → UID copy karo.
3. **Open Verification Panel** → UID ek baar submit karo → verification
   windows dikhne chahiye (Received → Under review → Decision).
4. Apni Telegram ID se Mini App kholo → **Admin** tab dikhna chahiye.
   Kisi aur ID se kholo → Admin tab NAHI dikhna chahiye (server-gated hai).
5. Admin tab me submission ko **Verified** karo → user ko Telegram par
   notification aana chahiye, aur uski windows me Decision update hona
   chahiye.
6. Dobara UID submit karne ki koshish karo → 409 aana chahiye
   ("already under review") — one-time rule kaam kar raha hai.

## 6. Plan B — Hostinger SHARED hosting (tumhara plan — yehi follow karo)

Tumhare paas shared hosting hai, to yehi plan hai. Code pehle se Plan B
ke liye ready hai — kuch uncomment nahi karna, kuch code nahi badalna:

1. **Render Blueprint deploy karo (§ 1 + § 2).** `render.yaml` me API web
   service pehle se active hai, to ek hi Blueprint se teeno ban jayenge:
   database + bot worker + `verifyhub-api` web service. Deploy ke baad
   Render dashboard me API service ka URL note karo — kuch aisa hoga:
   `https://verifyhub-api.onrender.com`
2. **Frontend → Hostinger `public_html`.** `frontend/` ke files File
   Manager se `public_html/` me upload karo: `index.html`, `styles.css`,
   `app.js` **aur `config.js`** (chaaro zaroori hain). Website ab
   `https://YOURDOMAIN/` par dikhega.
3. **Ek line badlo — `config.js`.** Hostinger File Manager me `config.js`
   kholo aur ye line bharo (apna Render API URL, aakhir me `/` nahi):
   ```js
   window.VERIFYHUB_API_BASE = 'https://verifyhub-api.onrender.com';
   ```
   Bas — Mini App ke saare API calls ab Render ko jayenge. Website phir
   bhi Hostinger par hai; API alag domain par hai, aur CORS pehle se khula
   hai (`main.py` me `allow_origins ["*"]`), isliye cross-domain kaam karega.
4. `MINI_APP_URL` dono jagah (Render bot + API env) `https://YOURDOMAIN/`
   hi rahega. Baaki steps same (§ 4, § 5).
5. **Free tier note:** free Web Service ~15 min idle ke baad so jata hai.
   Pehla Mini App open slow hoga (~50s wake-up), phir normal chalega. Agar
   hamesha instant chahiye to API service ko bhi Starter plan par lo.
   Bot worker ke liye bhi paid plan chahiye (§ 2 note).

## 7. Problems? (troubleshooting)

| Problem | Likely wajah / fix |
|---|---|
| Mini App blank / nahi khulti | `MINI_APP_URL` http hai ya galat hai → `https://YOURDOMAIN/` hona chahiye, aur BotFather menu button wahi URL. Telegram ko https chahiye. |
| `/api/me` 401 | `BOT_TOKEN` Hostinger aur Render par SAME nahi hai. Dono jagah ek hi token daalo, API restart karo. |
| Bot jawab nahi deta | Render Logs dekho. 409 conflict = do jagah bot chal raha hai (local + Render) — local band karo. |
| API start nahi hoti | `journalctl -u verifyhub-api -f` dekho. Common: `.env` me `DATABASE_URL` galat (External URL use karo), ya `BOT_TOKEN` khali hai. |
| Hostinger se DB connect nahi ho raha | Render External URL use karo (Internal sirf Render ke andar chalta hai). Render Postgres dashboard me connection details check karo. |
| Admin tab nahi dikh raha | `ADMIN_TELEGRAM_IDS` Hostinger `.env` me apni sahi Telegram ID daalo, phir `systemctl restart verifyhub-api`. ID @userinfobot se lo. |
| Free Render par bot sota hai | Free tier me worker always-on nahi hota. 24x7 ke liye Starter plan lo (worker § 2 note). |

## 8. Kaunsa variable kahan (cheat sheet)

| Variable | Hostinger VPS `.env` | Render bot worker |
|---|---|---|
| `BOT_TOKEN` | ✅ same token | ✅ same token |
| `BOT_USERNAME` | ✅ | ✅ |
| `MINI_APP_URL` | ✅ `https://YOURDOMAIN/` | ✅ `https://YOURDOMAIN/` |
| `REGISTRATION_URL` | ✅ | ✅ |
| `DATABASE_URL` | ✅ External URL (hand-typed) | ✅ auto (fromDatabase) |
| `ADMIN_TELEGRAM_IDS` | ✅ | ❌ bot ko nahi chahiye |
| `PROJECT_NAME` | ✅ | — |

## 9. Kharcha (approx — deploy karte time site par check karo)

- Hostinger VPS KVM 1: ~$5–7/month (website + API, 24x7)
- Render Background Worker Starter: ~$7/month (bot, 24x7)
- Render PostgreSQL: Free plan (limits apply) ya Starter ~$6/month
- Domain + HTTPS (Certbot/Let's Encrypt): free

**Deploy karne ke baad mujhe bata do kaunsa step atak raha hai — exact
error + kahan (Render Logs / `systemctl status`) bhej dena, main wahi
fix kar dunga.**
