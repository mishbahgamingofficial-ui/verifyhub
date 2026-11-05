/* VerifyHub — Mini App API address.
   DEFAULT (empty): the Mini App calls the API on the SAME domain
   (Plan A — Hostinger VPS, where one process serves pages + API).
   Leave this file exactly as it is for Plan A.

   PLAN B (Hostinger SHARED hosting): the API runs on Render, on a
   different domain than the website. Then put the Render API URL here,
   with NO trailing slash, e.g.:
       window.VERIFYHUB_API_BASE = 'https://verifyhub-api.onrender.com';
   The website stays on Hostinger; only the API calls go to Render.
   CORS is already open on the API (allow_origins ["*"] in main.py),
   and auth is the Telegram initData header, not cookies — so this
   cross-domain setup works. */
window.VERIFYHUB_API_BASE = '';
