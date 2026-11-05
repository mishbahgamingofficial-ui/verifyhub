/* VerifyHub Mini App — vanilla JS, no build step.
   Security notes:
   - Identity comes ONLY from Telegram.WebApp.initData, sent as a header.
   - Admin UI is rendered only after /api/me says is_admin (server-derived).
     The server re-checks admin on every admin endpoint regardless.
   - All server/user text is escaped before insertion (esc()).
   - Preview mode (no Telegram) never calls authenticated endpoints as a
     fake user; it shows the preview banner and disables submission. */
(function () {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const initData = tg ? tg.initData : '';
  const isPreview = !initData;
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (iso) => iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—';

  const LABELS = {
    not_submitted: 'Not submitted', pending_review: 'Pending review', under_review: 'Under review',
    verified: 'Verified', rejected: 'Rejected', info_required: 'Additional information required',
  };

  let config = null, me = null, uidPattern = null, pendingAdminAction = null;
  // Three-step flow: register -> deposit proof -> send UID.
  // The deposit screenshot is compressed client-side and sent with the
  // UID submit; nothing is uploaded before the user taps Continue.
  let flowStep = 'register', depositDataUrl = null, isResubmit = false;

  function toast(msg, type) {
    const el = $('#toast'); el.textContent = msg;
    el.className = 'toast' + (type ? ' ' + type : '');
    el.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { el.hidden = true; }, 4200);
  }
  // API base: '' = same domain (Plan A, Hostinger VPS). Plan B (shared
  // hosting) sets window.VERIFYHUB_API_BASE in config.js to the Render
  // API URL, e.g. 'https://verifyhub-api.onrender.com'.
  const API_BASE = (typeof window !== 'undefined' && window.VERIFYHUB_API_BASE ? String(window.VERIFYHUB_API_BASE) : '').replace(/\/$/, '');
  // Free-tier cold start: the API sleeps after ~15 min idle and the first
  // request takes ~50s to wake it. Give every call a long timeout and one
  // retry on timeout/network failure, and translate 500/503 into plain
  // words instead of "Request failed (500)".
  const API_TIMEOUT_MS = 70000;
  function friendlyError(status, detail) {
    if (status === 503 || status === 500) return 'Server is waking up or busy — please wait a moment and try again.';
    return detail || ('Request failed (' + status + ')');
  }
  async function api(path, opts, _retried) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
    try {
      const res = await fetch(API_BASE + path, Object.assign({ headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData }, signal: controller.signal }, opts || {}));
      let data = null; try { data = await res.json(); } catch (e) { /* empty */ }
      if (!res.ok) throw new Error(friendlyError(res.status, data && data.detail));
      return data;
    } catch (err) {
      if (!_retried && (err.name === 'AbortError' || err instanceof TypeError)) {
        return api(path, opts, true);
      }
      if (err instanceof TypeError) throw new Error('Network error — please check your connection and try again.');
      throw err;
    } finally { clearTimeout(timer); }
  }
  function badge(status) { return '<span class="badge b-' + esc(status) + '">' + esc(LABELS[status] || status) + '</span>'; }
  // Verification windows: Window 01 Received, 02 Under review, 03 Decision.
  // One-time rule lives here too: the form is gone once a submission exists;
  // only info_required re-opens it (see setPhase / resubmit button).
  function statusBlock(s) {
    if (!s) return '<div class="empty"><b>No submission yet.</b>Send your UID once.</div>';
    const st = s.status;
    const w1 = 'done', w1Note = 'Received. Queued for review · ' + esc(fmt(s.submitted_at)) + '.';
    const w2State = (st === 'pending_review') ? 'active' : (['under_review', 'verified', 'rejected', 'info_required'].includes(st) ? 'done' : 'next');
    const w2Note = st === 'pending_review' ? 'In the queue.'
      : (st === 'under_review' ? 'An administrator is checking it.'
      : (['verified', 'rejected', 'info_required'].includes(st) ? 'Checked by an administrator.' : 'Waiting.'));
    const w3State = (['verified', 'rejected', 'info_required'].includes(st)) ? 'done' : (st === 'under_review' ? 'active' : 'next');
    const w3Note = (['verified', 'rejected', 'info_required'].includes(st))
      ? esc(LABELS[st] || st) + (s.review_message ? ' — ' + esc(s.review_message) : '') + ' · ' + esc(fmt(s.updated_at)) + '.'
      : 'Decision appears here.';
    function win(n, title, state, note) {
      const dot = state === 'next' ? 'hollow' : 'filled';
      return '<div class="window ' + state + '"><div class="window-bar"><span>Window ' + n + ' — ' + title + '</span><span class="dot ' + dot + '"></span></div>'
        + '<div class="window-body"><b>' + esc(title === 'Decision' ? (LABELS[st] || st) : title) + '</b><span>' + note + '</span></div></div>';
    }
    return win('01', 'Received', w1, w1Note)
      + win('02', 'Under review', w2State, w2Note)
      + win('03', 'Decision', w3State, w3Note)
      + '<div style="margin-top:14px">' + badge(st) + '</div>'
      + '<dl class="meta"><div><dt>UID</dt><dd class="mono">' + esc(s.uid_masked) + '</dd></div>'
      + '<div><dt>Submitted</dt><dd>' + esc(fmt(s.submitted_at)) + '</dd></div>'
      + '<div><dt>Last updated</dt><dd>' + esc(fmt(s.updated_at)) + '</dd></div>'
      + '<div><dt>Deposit proof</dt><dd>' + (s.has_deposit_screenshot ? 'Received' : '—') + '</dd></div></dl>'
      + (s.review_message ? '<p class="review-message"><strong>Review message:</strong><br>' + esc(s.review_message) + '</p>' : '');
  }
  // Step switch inside Phase 1: register -> deposit -> uid.
  function showStep(name) {
    flowStep = name;
    ['register', 'deposit', 'uid'].forEach((s) => { const el = $('#step-' + s); if (el) el.hidden = s !== name; });
    const labels = { register: 'Step 1 of 3 — Register', deposit: 'Step 2 of 3 — Deposit proof', uid: 'Step 3 of 3 — Send UID' };
    const counts = { register: '01 / 03', deposit: '02 / 03', uid: '03 / 03' };
    const fills = { register: '33%', deposit: '66%', uid: '100%' };
    const label = $('#phase-label'), count = $('#phase-count'), fill = $('#progress-fill');
    if (label) label.textContent = labels[name];
    if (count) count.textContent = counts[name];
    if (fill) fill.style.width = fills[name];
    syncMainButton();
    window.scrollTo({ top: 0, behavior: 'auto' });
  }
  // Phase switch: Phase 1 = three steps, Phase 2 = verification windows.
  // The form locks for pending_review / under_review / verified / rejected.
  // Only info_required shows the one corrected resubmission.
  function setPhase(submission) {
    const has = !!submission;
    const send = $('#phase-send'), verify = $('#phase-verify');
    if (send) send.hidden = has;
    if (verify) verify.hidden = !has;
    const label = $('#phase-label'), count = $('#phase-count'), fill = $('#progress-fill');
    if (has) {
      if (label) label.textContent = 'Verification';
      if (count) count.textContent = '03 / 03';
      if (fill) fill.style.width = '100%';
    } else {
      showStep('register');
    }
    const allowResubmit = has && submission.status === 'info_required';
    const rb = $('#resubmit-btn'), rh = $('#resubmit-hint');
    if (rb) rb.hidden = !allowResubmit;
    if (rh) rh.hidden = !allowResubmit;
    if (tg && tg.MainButton && has) tg.MainButton.hide();
  }
  function showTab(name) {
    document.querySelectorAll('[role="tab"]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
    if (tg && tg.BackButton) { if (name !== 'home') tg.BackButton.show(); else tg.BackButton.hide(); }
    if (name === 'admin') loadAdmin();
  }
  document.querySelectorAll('[role="tab"]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  const resubmitBtn = $('#resubmit-btn');
  // A corrected UID goes straight to step 3: the deposit proof from the
  // first submit is already on file, and the server carries it over.
  if (resubmitBtn) resubmitBtn.addEventListener('click', () => { isResubmit = true; setPhase(null); showStep('uid'); });

  async function loadStatus() {
    if (isPreview) { setPhase(null); $('#sample-windows').hidden = false; $('#status-body').innerHTML = '<p>Open this panel from Telegram to see your status.</p>'; $('#current-body').innerHTML = '<p>Open this panel from Telegram to see your status.</p>'; return; }
    try {
      const latest = await api('/api/me/latest');
      setPhase(latest.submission || null);
      const html = latest.submission ? statusBlock(latest.submission) : '<div class="empty"><b>Not submitted yet.</b>Send your UID once above.</div>';
      $('#verify-windows').innerHTML = html;
      $('#current-body').innerHTML = latest.submission ? badge(latest.submission.status) + '<p style="margin-top:10px">UID <span class="mono">' + esc(latest.submission.uid_masked) + '</span> · submitted ' + esc(fmt(latest.submission.submitted_at)) + '</p>' : html;
      const rows = await api('/api/me/submissions');
      $('#activity-list').innerHTML = rows.length ? rows.slice(0, 5).map((r) =>
        '<li><span><strong>' + esc(r.uid_masked) + '</strong><small>' + esc(fmt(r.submitted_at)) + (r.review_message ? ' · ' + esc(r.review_message) : '') + '</small></span>' + badge(r.status) + '</li>').join('')
        : '<li><span>No activity yet.</span></li>';
    } catch (e) { $('#verify-windows').innerHTML = '<p>' + esc(e.message) + '</p>'; $('#current-body').innerHTML = '<p>' + esc(e.message) + '</p>'; }
  }

  // Submit form
  const form = $('#submit-form');
  function validateUid() {
    const v = $('#uid').value.trim(); let msg = '';
    if (!v) msg = 'Enter your UID.';
    else if (uidPattern && !(new RegExp(uidPattern).test(v))) msg = 'UID format is invalid. ' + (config ? config.uid_pattern_description : '');
    $('#uid').classList.toggle('invalid', !!msg); $('#uid-err').textContent = msg; return !msg;
  }
  $('#uid').addEventListener('input', validateUid);
  function syncMainButton() {
    if (!tg || !tg.MainButton) return;
    // The Telegram button only offers the UID submit, on step 3.
    const ok = flowStep === 'uid' && validateUid() && $('#confirm').checked && !isPreview;
    if (ok) { tg.MainButton.setText('Submit UID for review'); tg.MainButton.show(); tg.MainButton.enable(); }
    else { tg.MainButton.hide(); }
  }
  form.addEventListener('input', syncMainButton);
  if (tg && tg.MainButton) tg.MainButton.onClick(() => form.requestSubmit());
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (isPreview) { toast('Open this panel from Telegram to submit.'); return; }
    const okUid = validateUid();
    if (!$('#confirm').checked) { $('#confirm-err').textContent = 'Confirm the UID is correct.'; return; }
    $('#confirm-err').textContent = '';
    if (!okUid) return;
    // Fresh submissions need the deposit proof from step 2; a corrected
    // resubmission reuses the one already on file (isResubmit).
    if (!depositDataUrl && !isResubmit) { toast('Upload your deposit screenshot first.'); showStep('deposit'); return; }
    const btn = $('#submit-btn'); btn.disabled = true; btn.classList.add('is-loading'); btn.textContent = 'Submitting…';
    try {
      await api('/api/submissions', { method: 'POST', body: JSON.stringify({ registration_uid: $('#uid').value.trim(), reference: $('#reference').value.trim() || null, confirm_correct: true, deposit_screenshot: depositDataUrl }) });
      // One-time send: the form locks and the verification windows open.
      toast('UID sent — windows open.', 'success'); isResubmit = false; loadStatus(); showTab('home');
    } catch (err) { toast(err.message, 'error'); } finally { btn.disabled = false; btn.classList.remove('is-loading'); btn.textContent = 'Send UID once'; }
  });

  // Step navigation + deposit screenshot (step 2).
  $('#id-created-btn').addEventListener('click', () => showStep('deposit'));
  $('#deposit-continue-btn').addEventListener('click', () => showStep('uid'));
  $('#deposit-back-btn').addEventListener('click', () => showStep('register'));
  $('#uid-back-btn').addEventListener('click', () => showStep('deposit'));
  // Downscale the screenshot to a small JPEG data URL before it is ever
  // sent, so one upload cannot bloat the database.
  function compressImage(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(url);
        const maxSide = 1200;
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        let out = canvas.toDataURL('image/jpeg', 0.8);
        if (out.length > 3000000) out = canvas.toDataURL('image/jpeg', 0.6);
        if (out.length > 3000000) { reject(new Error('too large')); return; }
        resolve(out);
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('bad image')); };
      img.src = url;
    });
  }
  const depositFile = $('#deposit-file');
  $('#deposit-pick-btn').addEventListener('click', () => depositFile.click());
  depositFile.addEventListener('change', async () => {
    const file = depositFile.files && depositFile.files[0];
    $('#deposit-err').textContent = '';
    if (!file) return;
    if (!file.type || file.type.indexOf('image/') !== 0) { $('#deposit-err').textContent = 'Choose an image file.'; return; }
    if (file.size > 8 * 1024 * 1024) { $('#deposit-err').textContent = 'Screenshot is too large (max 8 MB).'; return; }
    try {
      depositDataUrl = await compressImage(file);
      const prev = $('#deposit-preview'); prev.src = depositDataUrl; prev.hidden = false;
      $('#deposit-ok').hidden = false;
      $('#deposit-continue-btn').disabled = false;
      $('#deposit-pick-btn').textContent = 'Change screenshot';
    } catch (err) { $('#deposit-err').textContent = 'Could not read that image. Try another.'; }
  });

  // Admin
  async function loadAdminStats() {
    try {
      const s = await api('/api/admin/stats');
      const order = [['total', 'Total'], ['pending_review', 'Pending'], ['under_review', 'Under review'], ['verified', 'Verified'], ['rejected', 'Rejected'], ['info_required', 'Info required']];
      $('#admin-stats').innerHTML = order.map(([k, label]) => '<div class="stat"><b>' + esc(k === 'total' ? s.total : (s.by_status[k] || 0)) + '</b><span>' + esc(label) + '</span></div>').join('');
    } catch (e) { $('#admin-stats').innerHTML = '<p>' + esc(e.message) + '</p>'; }
  }
  let searchTimer = null;
  async function loadAdmin() {
    if (!me || !me.is_admin) return;
    loadAdminStats();
    const q = new URLSearchParams({ search: $('#admin-search').value.trim(), status: $('#admin-status').value, sort: $('#admin-sort').value });
    try {
      const rows = await api('/api/admin/submissions?' + q.toString());
      $('#admin-empty').hidden = rows.length > 0;
      $('#admin-list').innerHTML = rows.length ? rows.map((r) =>
        '<li class="admin-item" data-id="' + esc(r.id) + '" tabindex="0" role="button"><span><strong class="mono">' + esc(r.registration_uid) + '</strong><small>#' + esc(r.id) + ' · Telegram ' + esc(r.user_telegram_id || '—') + (r.user_username ? ' · @' + esc(r.user_username) : '') + ' · ' + esc(fmt(r.submitted_at)) + '</small></span>' + badge(r.status) + '</li>').join('') : '';
      document.querySelectorAll('.admin-item').forEach((el) => {
        const open = () => openDetail(el.dataset.id);
        el.addEventListener('click', open); el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } });
      });
    } catch (e) { $('#admin-list').innerHTML = '<li><span>' + esc(e.message) + '</span></li>'; }
  }
  ['admin-search'].forEach((id) => $(('#' + id)).addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadAdmin, 300); }));
  ['admin-status', 'admin-sort'].forEach((id) => $(('#' + id)).addEventListener('change', loadAdmin));

  async function openDetail(id) {
    try {
      const d = await api('/api/admin/submissions/' + id);
      const s = d.submission;
      let html = '<h3>Submission #' + esc(s.id) + '</h3>' + badge(s.status)
        + '<dl class="meta"><div><dt>UID</dt><dd class="mono">' + esc(s.registration_uid) + '</dd></div>'
        + '<div><dt>Telegram ID</dt><dd>' + esc(s.user_telegram_id || '—') + '</dd></div>'
        + '<div><dt>User</dt><dd>' + esc(s.user_first_name || '—') + (s.user_username ? ' (@' + esc(s.user_username) + ')' : '') + '</dd></div>'
        + '<div><dt>Submitted</dt><dd>' + esc(fmt(s.submitted_at)) + '</dd></div></dl>'
        + (s.review_message ? '<p class="review-message"><strong>Review message:</strong><br>' + esc(s.review_message) + '</p>' : '')
        + '<h4 style="margin:18px 0 8px">History</h4>'
        + (d.history.length ? '<ul class="list">' + d.history.map((h) => '<li><span><strong>' + esc(h.previous_status) + ' → ' + esc(h.new_status) + '</strong><small>Admin ' + esc(h.admin_telegram_id) + ' · ' + esc(fmt(h.created_at)) + (h.reason ? ' · ' + esc(h.reason) : '') + '</small></span></li>').join('') + '</ul>' : '<p>No history yet.</p>')
        + '<label for="new-status">New status</label><select id="new-status"><option value="under_review">Under review</option><option value="verified">Verified</option><option value="rejected">Rejected</option><option value="info_required">Additional information required</option><option value="pending_review">Back to pending review</option></select>'
        + '<label for="admin-message">Message to user</label><textarea id="admin-message" rows="3" placeholder="Required for Rejected / Info needed"></textarea>'
        + '<label for="admin-note">Internal note</label><textarea id="admin-note" rows="2" placeholder="Admin-only"></textarea>'
        + '<div class="row"><button class="btn" id="save-status-btn" type="button">Save status change</button></div>';
      $('#detail-body').innerHTML = html;
      $('#detail-modal').classList.add('show');
      $('#save-status-btn').addEventListener('click', () => {
        const to = $('#new-status').value, msg = $('#admin-message').value.trim();
        if ((to === 'rejected' || to === 'info_required') && !msg) { toast('A review message is required.'); return; }
        pendingAdminAction = { id: s.id, to_status: to, message: msg || null, internal_note: $('#admin-note').value.trim() || null };
        $('#confirm-text').textContent = 'Change submission #' + s.id + ' from ' + s.status + ' to ' + to + '? The user will be notified only after the change is saved.';
        $('#confirm-modal').classList.add('show');
      });
      if (tg && tg.BackButton) tg.BackButton.show();
    } catch (e) { toast(e.message); }
  }
  function closeDetail() { $('#detail-modal').classList.remove('show'); }
  $('#detail-close').addEventListener('click', closeDetail);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if ($('#confirm-modal').classList.contains('show')) { $('#confirm-modal').classList.remove('show'); pendingAdminAction = null; }
    else if ($('#detail-modal').classList.contains('show')) closeDetail();
  });
  $('#detail-modal').addEventListener('click', (e) => { if (e.target.id === 'detail-modal') closeDetail(); });
  $('#confirm-no').addEventListener('click', () => { $('#confirm-modal').classList.remove('show'); pendingAdminAction = null; });
  $('#confirm-yes').addEventListener('click', async () => {
    if (!pendingAdminAction) return;
    const a = pendingAdminAction; $('#confirm-modal').classList.remove('show'); pendingAdminAction = null;
    try {
      await api('/api/admin/submissions/' + a.id + '/status', { method: 'POST', body: JSON.stringify(a) });
      toast('Updated. User notified.', 'success'); closeDetail(); loadAdmin(); loadStatus();
    } catch (e) { toast(e.message, 'error'); }
  });
  $('#register-link').addEventListener('click', (e) => { if (!config || !config.registration_url) { e.preventDefault(); toast('Registration link not set yet.'); } });
  $('#copy-link-btn').addEventListener('click', async () => {
    if (!config || !config.registration_url) { toast('Registration link not set yet.'); return; }
    try { await navigator.clipboard.writeText(config.registration_url); toast('Link copied.'); } catch (e) { toast('Copy failed. Long-press the link.'); }
  });

  // Live ping on every panel status bar (21–29ms jitter, like the reference).
  function tickPing() {
    const ms = 21 + Math.floor(Math.random() * 9);
    document.querySelectorAll('.ping-ms').forEach((el) => { el.textContent = ms + 'MS'; });
  }
  setInterval(tickPing, 2000); tickPing();

  // Init
  async function init() {
    if (tg) {
      try { tg.ready(); tg.expand(); tg.setHeaderColor('#150303'); tg.setBackgroundColor('#150303'); } catch (e) { /* older clients */ }
      tg.onEvent('backButtonClicked', () => { if ($('#detail-modal').classList.contains('show')) closeDetail(); else showTab('home'); });
    }
    if (isPreview) $('#preview-banner').classList.add('show');
    try {
      config = await api('/api/config');
      // /api/config is public, so call it without auth in preview too — api() sends an empty header, which is fine.
      $('#brand-name').textContent = config.project_name || 'VerifyHub';
      $('#uid-hint').textContent = config.uid_pattern_description || '';
      uidPattern = config.uid_pattern;
      if (config.registration_url) { $('#register-link').href = config.registration_url; }
      else { $('#register-link').href = '#'; $('#register-text').textContent = 'Registration link not set yet. Ask the administrator.'; }
    } catch (e) { /* config endpoint must not block the rest */ }
    // Note: /api/config above used api() with an empty initData header; if the
    // server ever requires auth there, the catch keeps the UI usable.
    if (isPreview) {
      $('#who-pill').textContent = 'Preview';
      $('#submit-btn').disabled = true;
      setPhase(null);
      const sw = $('#sample-windows'); if (sw) sw.hidden = false;
      $('#current-body').innerHTML = '<p>Open this panel from Telegram to see your status.</p>';
      $('#activity-list').innerHTML = '<li><span>Open this panel from Telegram to see your activity.</span></li>';
      return;
    }
    try {
      me = await api('/api/me');
      $('#who-pill').textContent = (me.first_name ? me.first_name : 'User') + (me.is_admin ? ' · Admin' : '');
      if (me.is_admin) $('#admin-tab').hidden = false;
      loadStatus();
    } catch (e) {
      $('#who-pill').textContent = 'Not signed in';
      $('#current-body').innerHTML = '<p>' + esc(e.message) + ' Reopen the panel from the bot.</p>';
      toast(e.message);
    }
  }
  // The public config call in init() goes through api(), which always sends
  // the header — but the server's /api/config does not read it, so preview works.
  init();
})();
