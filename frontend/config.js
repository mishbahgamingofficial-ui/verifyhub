/* VerifyHub — Mini App API address.
   DEFAULT (empty): the Mini App calls the API on the SAME domain
   (Plan A — Hostinger VPS, where one process serves pages + API).
   Leave this file exactly as it is for Plan A.

   PLAN B (Hostinger SHARED hosting): the API runs on Render, on a
   different domain than the website. Then put the Render API URL here,
   with NO trailing slash. THIS COPY is the live Plan B deployment. */
window.VERIFYHUB_API_BASE = 'https://verifyhub-api-uyo8.onrender.com';
