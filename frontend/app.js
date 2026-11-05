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
  // Telegram haptics — best-effort, guarded: plain browsers have no
  // tg.HapticFeedback and must never crash on these calls.
  function haptic(kind) {
    try {
      const hf = tg && tg.HapticFeedback;
      if (!hf) return;
      if (kind === 'success' || kind === 'error' || kind === 'warning') hf.notificationOccurred(kind);
      else hf.impactOccurred(kind || 'light');
    } catch (e) { /* haptics are best-effort */ }
  }
  // Set a countdown's text, popping the digits (scale 1.15 -> 1) when it changes.
  function setCountdownText(el, text) {
    if (!el || el.textContent === text) return;
    el.textContent = text;
    el.classList.remove('pop');
    void el.offsetWidth; // restart the animation
    el.classList.add('pop');
  }
  function fmtCountdown(totalSeconds) {
    const s = Math.max(0, Math.ceil(totalSeconds));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  // One-shot gold/red particle burst centred on el. Creates ~14 dots that
  // fly outward and fade; the host is removed after the animation.
  function burst(el, count) {
    if (!el || !document.body) return;
    const r = el.getBoundingClientRect();
    const host = document.createElement('span');
    host.className = 'burst';
    host.setAttribute('aria-hidden', 'true');
    host.style.left = (r.left + r.width / 2) + 'px';
    host.style.top = (r.top + r.height / 2) + 'px';
    const n = count || 14;
    for (let i = 0; i < n; i++) {
      const p = document.createElement('i');
      const ang = (Math.PI * 2 * i) / n + Math.random() * 0.5;
      const dist = 46 + Math.random() * 54;
      p.style.setProperty('--tx', (Math.cos(ang) * dist).toFixed(1) + 'px');
      p.style.setProperty('--ty', (Math.sin(ang) * dist).toFixed(1) + 'px');
      p.style.setProperty('--s', (3 + Math.random() * 4).toFixed(1) + 'px');
      host.appendChild(p);
    }
    document.body.appendChild(host);
    setTimeout(() => host.remove(), 950);
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
      if (!res.ok) { const err = new Error(friendlyError(res.status, data && data.detail)); err.status = res.status; throw err; }
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
    haptic('light');
    document.querySelectorAll('[role="tab"]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
    if (tg && tg.BackButton) { if (name !== 'home') tg.BackButton.show(); else tg.BackButton.hide(); }
    if (name === 'admin') loadAdmin();
    if (name === 'prediction') loadPrediction();
    else stopPredTimers();
  }
  document.querySelectorAll('[role="tab"]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  const resubmitBtn = $('#resubmit-btn');
  // A corrected UID goes straight to step 3: the deposit proof from the
  // first submit is already on file, and the server carries it over.
  if (resubmitBtn) resubmitBtn.addEventListener('click', () => { isResubmit = true; setPhase(null); showStep('uid'); });

  // Celebration + dock: fired once per load, only when the latest
  // submission is verified (never on every refresh).
  let verifiedBurstDone = false;
  async function loadStatus() {
    if (isPreview) { setPhase(null); $('#sample-windows').hidden = false; $('#status-body').innerHTML = '<p>Open this panel from Telegram to see your status.</p>'; $('#current-body').innerHTML = '<p>Open this panel from Telegram to see your status.</p>'; return; }
    try {
      const latest = await api('/api/me/latest');
      setPhase(latest.submission || null);
      const html = latest.submission ? statusBlock(latest.submission) : '<div class="empty"><b>Not submitted yet.</b>Send your UID once above.</div>';
      $('#verify-windows').innerHTML = html;
      $('#current-body').innerHTML = latest.submission ? badge(latest.submission.status) + '<p style="margin-top:10px">UID <span class="mono">' + esc(latest.submission.uid_masked) + '</span> · submitted ' + esc(fmt(latest.submission.submitted_at)) + '</p>' : html;
      const dockStatus = $('#dock-status');
      if (dockStatus) dockStatus.innerHTML = badge(latest.submission ? latest.submission.status : 'not_submitted');
      if (latest.submission && latest.submission.status === 'verified' && !verifiedBurstDone) {
        verifiedBurstDone = true;
        setTimeout(() => {
          burst($('#current-card'), 14);
          const stamp = document.querySelector('#current-card .stamp');
          if (stamp) stamp.classList.add('pop');
        }, 250);
      }
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
    haptic('medium');
    try {
      await api('/api/submissions', { method: 'POST', body: JSON.stringify({ registration_uid: $('#uid').value.trim(), reference: $('#reference').value.trim() || null, confirm_correct: true, deposit_screenshot: depositDataUrl }) });
      // One-time send: the form locks and the verification windows open.
      toast('UID sent — windows open.', 'success'); haptic('success'); isResubmit = false; loadStatus(); showTab('home');
    } catch (err) { toast(err.message, 'error'); } finally { btn.disabled = false; btn.classList.remove('is-loading'); btn.textContent = 'Send UID once'; }
  });

  // Step navigation + deposit screenshot (step 2).
  $('#id-created-btn').addEventListener('click', () => { haptic('medium'); showStep('deposit'); });
  $('#deposit-continue-btn').addEventListener('click', () => { haptic('medium'); showStep('uid'); });
  $('#deposit-back-btn').addEventListener('click', () => { haptic('light'); showStep('register'); });
  $('#uid-back-btn').addEventListener('click', () => { haptic('light'); showStep('deposit'); });
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
  $('#deposit-pick-btn').addEventListener('click', () => { haptic('light'); depositFile.click(); });
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
      haptic('success');
    } catch (err) { $('#deposit-err').textContent = 'Could not read that image. Try another.'; }
  });

  // Prediction (verified users only — the server gates /api/prediction;
  // the tab is always visible, but the content is locked for anyone else).
  // Signal only, never a guarantee: the page says so, and the signal's
  // confidence is never "high".
  let predTimer = '1m', predRefreshId = null, predCountdownId = null;
  let predRemainingMs = 0, predFetchedAt = 0, predLoading = false, lastPredPeriod = null;
  // predHasData: good data is currently on screen. Background refreshes
  // must never blank it (that was the flicker: every 10s the open
  // prediction flashed to a loading spinner, and any transient failure
  // flipped it to the error screen).
  let predHasData = false;
  function stopPredTimers() {
    if (predRefreshId) { clearInterval(predRefreshId); predRefreshId = null; }
    if (predCountdownId) { clearInterval(predCountdownId); predCountdownId = null; }
  }
  // Re-arm the 10s background refresh (clears the old one first, so
  // intervals can never stack). It keeps running after a failure too,
  // so the page heals itself instead of dying on the error screen.
  function armPredRefresh() {
    // Never poll while the prediction view is closed: returning to the
    // tab reloads it fresh (showTab calls loadPrediction).
    if (!$('#view-prediction').classList.contains('active')) return;
    if (predRefreshId) clearInterval(predRefreshId);
    predRefreshId = setInterval(() => loadPrediction({ background: true }), 10000);
  }
  function showPredState(name) {
    ['pred-locked', 'pred-open', 'pred-error', 'pred-loading'].forEach((id) => {
      const el = $('#' + id); if (el) el.hidden = id !== name;
    });
  }
  // WinGo ball colour from the number alone: 0 = red+violet, 5 = green+violet.
  function ballClass(n) {
    if (n === 0) return 'vr';
    if (n === 5) return 'vg';
    return n % 2 === 0 ? 'red' : 'green';
  }
  function tickPredCountdown() {
    const left = Math.max(0, predRemainingMs - (Date.now() - predFetchedAt));
    const s = Math.ceil(left / 1000);
    setCountdownText($('#pred-countdown'), String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'));
    if (left <= 0) {
      // Period rolled over: fetch the new signal in the background.
      // Never blank the view for this — the last signal stays on screen
      // until the new one arrives.
      clearInterval(predCountdownId); predCountdownId = null;
      loadPrediction({ background: true }).then((started) => {
        // A fetch was already running: retry the rollover shortly, but
        // only while the prediction view is still open.
        if (!started && !predCountdownId && $('#view-prediction').classList.contains('active')) {
          predRemainingMs = 5000; predFetchedAt = Date.now();
          predCountdownId = setInterval(tickPredCountdown, 1000);
        }
      });
    }
  }
  function renderPrediction(d) {
    $('#pred-period').textContent = d.period;
    $('#pred-size').textContent = d.signal.size === 'big' ? 'BIG' : 'SMALL';
    const ball = $('#pred-ball');
    ball.className = 'pred-ball ball ' + ballClass(d.signal.number);
    ball.innerHTML = '<span>' + esc(d.signal.number) + '</span>';
    // One-shot burst behind the ball when a NEW signal period arrives.
    if (d.period !== lastPredPeriod) { lastPredPeriod = d.period; burst(document.querySelector('.pred-signal'), 10); }
    $('#pred-reason').textContent = d.signal.reason + ' · Confidence: ' + d.signal.confidence;
    $('#pred-history').innerHTML = '<span class="pred-history-label">LAST 10</span>' + (d.results || []).map((r) =>
      '<span class="mini-ball ' + ballClass(r.number) + '" title="' + esc(r.period) + '"><span>' + esc(r.number) + '</span></span>').join('');
    const predErr = $('#pred-err');
    predErr.textContent = '';
    predErr.className = 'err';
    // Countdown from the SERVER clock (endTime - serviceTime), not the
    // client clock, so a wrong phone time cannot skew it.
    predRemainingMs = new Date(d.next_period_at).getTime() - new Date(d.server_time).getTime();
    predFetchedAt = Date.now();
    tickPredCountdown();
    if (predCountdownId) clearInterval(predCountdownId);
    predCountdownId = setInterval(tickPredCountdown, 1000);
  }
  // Accuracy tracker: honest record of past signals vs real results.
  // Fetched with the prediction view; a failure here never breaks the
  // signal above. Counts are shown exactly as the server returns them.
  let accLastGood = null;
  function renderAccuracy(d) {
    const s = d.summary || {};
    // Dots always render, oldest left, newest right (records arrive
    // newest first). Grey = still pending, so the record visibly builds.
    $('#acc-dots').innerHTML = (d.records || []).slice().reverse().map((r) =>
      '<span class="acc-dot ' + (r.correct === true ? 'right' : r.correct === false ? 'wrong' : 'pending') + '" title="#' + esc(String(r.period).slice(-5)) + '"></span>').join('');
    if (s.settled === 0) {
      $('#acc-summary').textContent = '';
      $('#acc-rows').innerHTML = '<p class="hint" style="margin:0">No settled signals yet — the record builds as periods finish.</p>';
    } else {
      $('#acc-summary').textContent = s.correct + ' RIGHT · ' + s.wrong + ' WRONG · ' + s.accuracy_pct + '%';
      // Last 5, newest first: #0814 · BIG → 7 BIG ✓ (✗ when wrong, … while pending).
      $('#acc-rows').innerHTML = (d.records || []).slice(0, 5).map((r) => {
        const short = '#' + String(r.period).slice(-5);
        const verdict = r.correct === true
          ? esc(r.actual_number) + ' ' + esc(r.actual_size) + ' ✓'
          : r.correct === false
            ? esc(r.actual_number) + ' ' + esc(r.actual_size) + ' ✗'
            : '…';
        const cls = r.correct === true ? 'ok' : r.correct === false ? 'bad' : '';
        return '<div class="acc-row"><span>' + esc(short) + ' · ' + esc(r.signal) + '</span><span class="' + cls + '">→ ' + verdict + '</span></div>';
      }).join('');
    }
    $('#acc-err').textContent = '';
    $('#pred-accuracy').hidden = false;
    accLastGood = d;
  }
  async function loadAccuracy(timer) {
    try {
      const d = await api('/api/prediction/accuracy?timer=' + encodeURIComponent(timer));
      if (timer !== predTimer) return; // timer switched mid-flight; the new load covers it
      renderAccuracy(d);
    } catch (e) {
      if (e.status === 403) { $('#pred-accuracy').hidden = true; return; }
      // 503 or anything else: keep the last good record, say so once.
      $('#acc-err').textContent = 'Record temporarily unavailable.';
      if (accLastGood) $('#pred-accuracy').hidden = false;
    }
  }
  // Foreground = first load / manual refresh / timer switch: may show
  // the loading state, but ONLY when nothing good is on screen yet.
  // Background = 10s interval + countdown rollover: never blank the
  // view and never show the error screen — a failed background refresh
  // keeps the last good signal, history and countdown exactly as they
  // are, notes it in place, and keeps retrying. Returns true when a
  // fetch was started, false when one was already running.
  async function loadPrediction(opts = {}) {
    const background = !!opts.background;
    if (predLoading) return false;
    if (isPreview) {
      $('#pred-locked-status').textContent = 'Open this panel from Telegram — the prediction page unlocks after verification.';
      showPredState('pred-locked');
      return false;
    }
    const timerAtLoad = predTimer;
    const errEl = $('#pred-err');
    if (!predHasData) {
      // Nothing to show yet: a foreground load may show the spinner.
      // A background load with no data (e.g. auto-retry from the error
      // screen) keeps the current state and just fetches.
      if (!background) showPredState('pred-loading');
    } else if (!$('#pred-open').hidden) {
      // Good data on screen: keep it, note the refresh in place.
      errEl.textContent = 'Updating…';
      errEl.className = 'hint';
    }
    predLoading = true;
    try {
      const d = await api('/api/prediction?timer=' + encodeURIComponent(predTimer));
      // The timer was switched while this fetch was in flight: don't
      // flash the old timer's data — the finally block reloads for the
      // new timer, and the old data stays on screen until it arrives.
      if (predTimer !== timerAtLoad) return true;
      renderPrediction(d);
      showPredState('pred-open');
      predHasData = true;
      loadAccuracy(timerAtLoad);
      armPredRefresh();
      return true;
    } catch (e) {
      if (e.status === 403) {
        // Locked: show the user's real status, fetched live.
        let statusText = 'Not submitted';
        try { const latest = await api('/api/me/latest'); statusText = LABELS[latest.status] || latest.status; } catch (e2) { /* keep default */ }
        $('#pred-locked-status').textContent = 'Your status: ' + statusText + ' — the prediction page unlocks when an administrator marks you Verified.';
        showPredState('pred-locked');
        predHasData = false;
        stopPredTimers();
        return true;
      }
      if (predHasData) {
        // Transient failure on a background refresh: keep the last good
        // signal, history and countdown exactly as they are.
        errEl.textContent = 'Update failed — showing the last signal. Retrying…';
        errEl.className = 'err';
        armPredRefresh();
      } else {
        $('#pred-error-text').textContent = e.status === 503
          ? 'Results temporarily unavailable. Please try again in a moment.'
          : (e.message || 'Could not load the prediction.');
        showPredState('pred-error');
        armPredRefresh(); // heal itself: the next tick recovers the page
      }
      return true;
    } finally {
      predLoading = false;
      // The timer was switched while a load was in flight: reload with the new timer.
      if (predTimer !== timerAtLoad) loadPrediction();
    }
  }
  document.querySelectorAll('.pred-timer').forEach((b) => b.addEventListener('click', () => {
    if (predTimer === b.dataset.timer) return;
    haptic('light');
    predTimer = b.dataset.timer;
    document.querySelectorAll('.pred-timer').forEach((x) => x.classList.toggle('active', x === b));
    loadPrediction();
  }));
  $('#pred-refresh-btn').addEventListener('click', () => { haptic('medium'); loadPrediction(); });
  $('#pred-error-refresh').addEventListener('click', () => { haptic('medium'); loadPrediction(); });
  $('#pred-back-btn').addEventListener('click', () => showTab('home'));

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
  $('#register-link').addEventListener('click', (e) => { haptic('medium'); if (!config || !config.registration_url) { e.preventDefault(); toast('Registration link not set yet.'); } });
  $('#copy-link-btn').addEventListener('click', async () => {
    haptic('light');
    if (!config || !config.registration_url) { toast('Registration link not set yet.'); return; }
    try { await navigator.clipboard.writeText(config.registration_url); toast('Link copied.'); } catch (e) { toast('Copy failed. Long-press the link.'); }
  });

  // Live ping on every panel status bar (21–29ms jitter, like the reference).
  function tickPing() {
    const ms = 21 + Math.floor(Math.random() * 9);
    document.querySelectorAll('.ping-ms').forEach((el) => { el.textContent = ms + 'MS'; });
  }
  setInterval(tickPing, 2000); tickPing();

  // Live ticker: real WinGo 1-min results from the public /api/live
  // endpoint — never hardcoded, never invented. Polls every 5s; the
  // countdown ticks every second from the server-provided ends_in_seconds.
  let tickerRemaining = 0, tickerFetchedAt = 0, tickerTimerId = null, tickerTickId = null, tickerInFlight = false;
  function tickTicker() {
    const left = tickerRemaining - (Date.now() - tickerFetchedAt) / 1000;
    setCountdownText($('#ticker-countdown'), fmtCountdown(left));
    if (left <= 0) fetchLive();
  }
  async function fetchLive() {
    if (tickerInFlight) return;
    tickerInFlight = true;
    try {
      const controller = new AbortController();
      const to = setTimeout(() => controller.abort(), 8000);
      let res;
      try { res = await fetch(API_BASE + '/api/live', { signal: controller.signal }); } finally { clearTimeout(to); }
      if (!res.ok) throw new Error('live failed');
      renderTicker(await res.json());
    } catch (e) {
      // Keep the last good data, show that we are reconnecting.
      const live = $('#ticker-live');
      if (live) { live.textContent = 'RECONNECTING'; live.classList.add('reconnecting'); }
    } finally { tickerInFlight = false; }
  }
  function renderTicker(d) {
    const live = $('#ticker-live');
    if (live) { live.textContent = 'LIVE'; live.classList.remove('reconnecting'); }
    const periodEl = $('#ticker-period');
    if (periodEl) periodEl.textContent = '#' + d.period;
    const last = (d.last && d.last[0]) || null;
    if (last) {
      const ball = $('#ticker-ball');
      if (ball) { ball.className = 'mini-ball ' + ballClass(last.number); ball.innerHTML = '<span>' + esc(last.number) + '</span>'; }
      const lastEl = $('#ticker-last');
      if (lastEl) lastEl.textContent = last.number + ' ' + String(last.colour).replace(',', '+').toUpperCase();
    }
    const dock = $('#dock-period');
    if (dock) dock.textContent = 'WinGo 1M · #' + String(d.period).slice(-6);
    tickerRemaining = d.ends_in_seconds || 0;
    tickerFetchedAt = Date.now();
    setCountdownText($('#ticker-countdown'), fmtCountdown(tickerRemaining));
  }
  function startTicker() {
    fetchLive();
    if (tickerTimerId) clearInterval(tickerTimerId);
    tickerTimerId = setInterval(fetchLive, 5000);
    if (tickerTickId) clearInterval(tickerTickId);
    tickerTickId = setInterval(tickTicker, 1000);
  }

  // Init
  async function init() {
    if (tg) {
      try { tg.ready(); tg.expand(); tg.setHeaderColor('#150303'); tg.setBackgroundColor('#150303'); } catch (e) { /* older clients */ }
      tg.onEvent('backButtonClicked', () => { if ($('#detail-modal').classList.contains('show')) closeDetail(); else showTab('home'); });
    }
    // The ticker is public data — it runs for everyone, preview included.
    startTicker();
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
    // Deep link from the bot's /predict command: open straight on the
    // Prediction tab. Guarded — plain browsers have no start_param.
    const startParam = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : '';
    if (startParam === 'prediction') showTab('prediction');
  }
  // The public config call in init() goes through api(), which always sends
  // the header — but the server's /api/config does not read it, so preview works.
  init();
})();
