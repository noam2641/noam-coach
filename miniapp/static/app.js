// Noam Coach Mini App client.
// Business calculations happen on the server; this file renders state and
// forwards user actions to the API.

// ---------------------------------------------------------------------------
// Observability O8 — semantic view/action telemetry.
// API success is NOT proof the user saw anything: the client explicitly
// reports ui.view.rendered AFTER the semantic DOM region was updated, and
// ui.action.activated when a control is used. Events are batched (bounded),
// authenticated by the same session cookie, and every telemetry failure is
// swallowed — reporting must never break the Mini App itself.
// ---------------------------------------------------------------------------

function obsId(prefix) {
  return prefix + '_' + Math.random().toString(16).slice(2, 10) + Date.now().toString(16).slice(-6);
}

const obs = {
  queue: [],
  flushTimer: null,
  clientInteractionId: null,   // the client action currently in progress
  viewRenders: {},             // view -> latest render_id actually shown
};

function obsRecord(event, payload) {
  try {
    obs.queue.push({ event, client_ts: Date.now(), ...payload });
    if (obs.queue.length >= 10) { obsFlush(); return; }
    if (!obs.flushTimer) obs.flushTimer = setTimeout(obsFlush, 900);
  } catch (e) { /* telemetry must never break the app */ }
}

function obsFlush() {
  try {
    if (obs.flushTimer) { clearTimeout(obs.flushTimer); obs.flushTimer = null; }
    if (!obs.queue.length) return;
    const batch = obs.queue.splice(0, 20);
    fetch('/mini/api/obs/events', {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }),
    }).catch(() => {});
  } catch (e) { /* never break the app */ }
}

function obsViewRendered(view, trigger, state) {
  try {
    const renderId = obsId('rn');
    obs.viewRenders[view] = renderId;
    obsRecord('ui.view.rendered', {
      view,
      render_id: renderId,
      trigger: trigger || 'unknown',
      caused_by_client_interaction_id: obs.clientInteractionId,
      state: state || null,
    });
  } catch (e) { /* never break the app */ }
}

function obsAction(action, sourceView, payload) {
  try {
    obs.clientInteractionId = obsId('ci');
    obsRecord('ui.action.activated', {
      action,
      source_view: sourceView,
      source_render_id: obs.viewRenders[sourceView] || null,
      client_interaction_id: obs.clientInteractionId,
      payload: payload || null,
    });
    return obs.clientInteractionId;
  } catch (e) { return null; }
}

window.addEventListener('pagehide', obsFlush);

async function apiCall(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  // Server-side correlation: the API trace joins the client action.
  if (obs.clientInteractionId) headers['X-Obs-Client-Interaction'] = obs.clientInteractionId;
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'object' ? detail.message : detail;
    throw new Error(message || 'הפעולה נכשלה');
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function errorHtml(err) {
  return `<span class="error">${escapeHtml(err.message)}</span>`;
}

function planCard(plan, index) {
  const rationale = (plan.rationale || []).slice(0, 3).map(escapeHtml).join(' · ');
  const tradeoffs = (plan.tradeoffs || []).slice(0, 2).map(escapeHtml).join(' · ');
  const score = Math.round((plan.fit_score ?? plan.score ?? 0) * 100);
  return `<div class="candidate"><b>${index}. ${escapeHtml(plan.title)}</b> · התאמה ${score}%<br>` +
    `<small>✓ ${rationale}<br>▵ ${tradeoffs}</small><br>` +
    `<button class="action-btn activate-plan-btn" type="button" data-plan-id="${Number(plan.id)}">בחר כתוכנית ראשית</button></div>`;
}

async function loadDashboard(trigger = 'initial_load') {
  const box = document.getElementById('planStatus');
  try {
    const data = await apiCall('/mini/api/dashboard');
    const plans = data.active_plans || {};
    const action = data.coaching?.next_action;
    box.innerHTML = action ?
      `<div class="candidate"><b>הפעולה הבאה: ${escapeHtml(action.title)}</b><br><small>${escapeHtml(action.reason)}</small></div>` : '';
    box.innerHTML += `תזונה: <b>${escapeHtml(plans.nutrition?.title || 'טרם נבחרה')}</b><br>` +
      `אימונים: <b>${escapeHtml(plans.workout?.title || 'טרם נבחרה')}</b><br>` +
      `שבוע מאוחד: <b>${escapeHtml(plans.unified?.title || 'טרם נבנה')}</b>`;
    obsViewRendered('dashboard', trigger, {
      has_next_action: Boolean(action),
      nutrition_plan: plans.nutrition?.title || null,
      workout_plan: plans.workout?.title || null,
      unified_plan: plans.unified?.title || null,
    });
    renderOperations(data.operations || {}, trigger);
  } catch (err) { box.innerHTML = errorHtml(err); }
}

function renderOperations(operations, trigger = 'unknown') {
  const box = document.getElementById('operationsBox');
  if (!box) return;
  const pain = operations.active_pain || [];
  const session = operations.active_session;
  const load = operations.current_load_decision;
  const painText = pain.length
    ? pain.map(item => `${escapeHtml(item.label || item.region)} (${escapeHtml(item.severity ?? 'לא צוין')})`).join(', ')
    : 'אין כאב פעיל';
  const sessionText = session
    ? `${escapeHtml(session.name || session.code || 'אימון')} · תרגיל ${Number(session.exercise_index) + 1} · סט ${escapeHtml(session.set_number)}`
    : 'אין אימון פעיל';
  const loadText = load
    ? `${escapeHtml(load.decision)} · ${escapeHtml(load.recommended_weight)} ק״ג × ${escapeHtml(load.recommended_reps)}`
    : 'אין החלטת עומס פעילה';
  box.innerHTML =
    `<div class="ops-item"><b>כאב פעיל</b><span>${painText}</span></div>` +
    `<div class="ops-item"><b>אימון פעיל</b><span>${sessionText}</span></div>` +
    `<div class="ops-item"><b>החלטת עומס</b><span>${loadText}</span></div>`;
  obsViewRendered('operations', trigger, {
    active_pain_count: pain.length,
    has_active_session: Boolean(session),
    has_load_decision: Boolean(load),
  });
}

async function loadNextMeal(trigger = 'initial_load') {
  const box = document.getElementById('nextMealBox');
  if (!box) return;
  box.textContent = 'טוען המלצה...';
  try {
    const data = await apiCall('/mini/api/next-meal');
    renderNextMeal(data, trigger);
  } catch (err) { box.innerHTML = errorHtml(err); }
}

function renderNextMeal(data, trigger = 'unknown') {
  const box = document.getElementById('nextMealBox');
  const text = String(data.text || '').replaceAll('\n', '<br>');
  const actionRows = (data.actions || [])
    .map(row => `<div class="action-row">${row.map(action =>
      `<button class="action-btn secondary next-meal-status-btn" type="button" data-status="${escapeHtml(action.status)}">${escapeHtml(action.label)}</button>`
    ).join('')}</div>`)
    .join('');
  box.innerHTML = text + actionRows;
  obsViewRendered('next_meal', trigger, {
    text_chars: String(data.text || '').length,
    action_count: (data.actions || []).flat().length,
  });
}

async function setNextMealWorkoutStatus(status) {
  const box = document.getElementById('nextMealBox');
  obsAction('next_meal_workout_status', 'next_meal', { status });
  try {
    box.textContent = 'מעדכן המלצה...';
    const data = await apiCall('/mini/api/next-meal/workout-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    renderNextMeal(data, 'post_mutation_refresh');
  } catch (err) { box.innerHTML = errorHtml(err); }
}

function mealCard(meal) {
  const calories = Math.round(Number(meal.calories) || 0);
  const protein = Math.round(Number(meal.protein) || 0);
  const when = meal.eaten_at ? new Date(meal.eaten_at).toLocaleTimeString('he-IL', { hour: '2-digit', minute: '2-digit' }) : '';
  return `<div class="meal-row"><b>${escapeHtml(meal.name || 'ארוחה')}</b>` +
    `<small>${escapeHtml(when)} · כ-${calories} קל׳ · כ-${protein} גרם חלבון</small></div>`;
}

async function loadTodayMeals(trigger = 'initial_load') {
  const box = document.getElementById('todayMealsBox');
  if (!box) return;
  box.textContent = 'טוען ארוחות...';
  try {
    const data = await apiCall('/mini/api/meals/today');
    const meals = data.meals || [];
    const quality = data.quality || {};
    const summary = `<div class="muted">אמינות דיווח: ${escapeHtml(quality.confidence_label || 'לא ידוע')}</div>`;
    box.innerHTML = meals.length
      ? summary + meals.map(mealCard).join('')
      : summary + '<div class="muted">עוד לא נרשמו ארוחות היום.</div>';
    obsViewRendered('today_meals', trigger, {
      meal_count: meals.length,
      confidence_label: quality.confidence_label || null,
    });
  } catch (err) { box.innerHTML = errorHtml(err); }
}

async function generatePlans(type) {
  const box = document.getElementById('planCandidates');
  obsAction('generate_plans', 'plan_candidates', { plan_type: type });
  box.textContent = 'בונה ובודק שלוש חלופות...';
  try {
    const data = await apiCall(`/mini/api/plans/${type}/generate`, { method: 'POST' });
    box.innerHTML = (data.candidates || []).map((plan, i) => planCard(plan, i + 1)).join('');
    obsViewRendered('plan_candidates', 'user_action', {
      plan_type: type,
      candidate_count: (data.candidates || []).length,
    });
  } catch (err) { box.innerHTML = errorHtml(err); }
}

async function activatePlan(id) {
  const box = document.getElementById('planCandidates');
  obsAction('activate_plan', 'plan_candidates', { plan_id: id });
  try {
    const data = await apiCall(`/mini/api/plans/${id}/activate`, { method: 'POST' });
    box.innerHTML = `<b>${escapeHtml(data.active.title)}</b> נבחרה כתוכנית הראשית ✅`;
    obsViewRendered('plan_candidates', 'post_mutation_refresh', {
      activated_plan_id: id,
      activated_title: data.active?.title || null,
    });
    await loadDashboard('post_mutation_refresh');
  } catch (err) { box.innerHTML = errorHtml(err); }
}

async function buildUnified() {
  const box = document.getElementById('planCandidates');
  obsAction('build_unified_plan', 'plan_candidates', null);
  box.textContent = 'מחבר את התזונה והאימונים לשבוע אחד...';
  try {
    await apiCall('/mini/api/plans/unified/build', { method: 'POST' });
    box.textContent = 'התוכנית השבועית נבנתה ✅';
    obsViewRendered('plan_candidates', 'post_mutation_refresh', { unified_built: true });
    await loadDashboard('post_mutation_refresh');
  } catch (err) { box.innerHTML = errorHtml(err); }
}

function factValue(snapshot, key) {
  return snapshot?.facts?.[key]?.value ?? null;
}

function renderAvailability(availability) {
  const box = document.getElementById('availabilitySummary');
  if (!availability) {
    box.textContent = '';
    return;
  }
  const dayNames = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת'];
  const days = (availability.preferred_days || [])
    .map(day => dayNames[Number(day)] || String(day))
    .join(', ');
  const time = availability.preferred_time ? `, שעה ${availability.preferred_time}` : '';
  box.textContent = `לפי מה ששמור: עד ${availability.max_days_per_week} אימונים בשבוע, כ-${availability.session_minutes} דקות${time}. ימים: ${days || 'לא צוין'}.`;
}

async function loadProfile(trigger = 'initial_load') {
  const statusBox = document.getElementById('profileStatus');
  try {
    const data = await apiCall('/mini/api/profile');
    const snapshot = data.snapshot || {};
    renderAvailability(data.availability);
    const work = factValue(snapshot, 'work_schedule') || {};
    const mealBreak = factValue(snapshot, 'meal_break_info') || {};
    document.getElementById('workStart').value = work.start || '';
    document.getElementById('workEnd').value = work.end || '';
    document.getElementById('commuteMinutes').value = factValue(snapshot, 'commute_minutes') || '';
    document.getElementById('mealBreakTime').value = mealBreak.time || mealBreak.break_time || '';
    document.getElementById('sessionMinutes').value = factValue(snapshot, 'session_minutes') || '';
    document.getElementById('trainingLocation').value = factValue(snapshot, 'training_location') || '';
    document.getElementById('equipment').value = factValue(snapshot, 'equipment') || '';
    document.getElementById('cookingCapacity').value = factValue(snapshot, 'cooking_capacity') || '';
    document.getElementById('mealStructure').value = factValue(snapshot, 'meal_structure_preference') || '';
    document.getElementById('dietRestrictions').value = factValue(snapshot, 'diet_restrictions') || '';
    document.getElementById('allergies').value = factValue(snapshot, 'allergies') || '';
    const slots = factValue(snapshot, 'weekly_availability') || [];
    document.querySelectorAll('.day-row').forEach(row => {
      const slot = slots.find(x => Number(x.weekday) === Number(row.dataset.day));
      row.querySelector('.available').checked = Boolean(slot?.available ?? slot);
      row.querySelector('.start').value = slot?.start || slot?.time || '';
      row.querySelector('.minutes').value = slot?.minutes || '';
    });
    statusBox.textContent = '';
    obsViewRendered('profile', trigger, {
      has_availability: Boolean(data.availability),
      availability_slot_count: slots.length,
    });
  } catch (err) { statusBox.textContent = err.message; statusBox.className = 'upload-status error'; }
}

let savingProfile = false;
async function saveProfile() {
  const statusBox = document.getElementById('profileStatus');
  if (savingProfile) return;
  savingProfile = true;
  const slots = [...document.querySelectorAll('.day-row')].map(row => ({
    weekday: Number(row.dataset.day),
    available: row.querySelector('.available').checked,
    start: row.querySelector('.start').value || null,
    minutes: Number(row.querySelector('.minutes').value) || null,
  })).filter(slot => slot.available);
  const payload = {
    work_start: document.getElementById('workStart').value || null,
    work_end: document.getElementById('workEnd').value || null,
    commute_minutes: Number(document.getElementById('commuteMinutes').value) || null,
    meal_break_time: document.getElementById('mealBreakTime').value || null,
    session_minutes: Number(document.getElementById('sessionMinutes').value) || null,
    training_location: document.getElementById('trainingLocation').value || null,
    equipment: document.getElementById('equipment').value || null,
    cooking_capacity: document.getElementById('cookingCapacity').value || null,
    meal_structure_preference: document.getElementById('mealStructure').value || null,
    diet_restrictions: document.getElementById('dietRestrictions').value || null,
    allergies: document.getElementById('allergies').value || null,
    weekly_availability: slots,
  };
  obsAction('save_profile', 'profile', {
    field_count: Object.values(payload).filter(v => v !== null).length,
    availability_slot_count: slots.length,
  });
  statusBox.textContent = 'שומר...';
  statusBox.className = 'upload-status';
  try {
    const result = await apiCall('/mini/api/profile', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    statusBox.textContent = result.review_required
      ? 'נשמר ✅ בגלל השינוי מומלץ ליצור מחדש את הצעות התוכנית.'
      : 'הפרופיל נשמר ✅';
    statusBox.className = 'upload-status success';
    obsViewRendered('profile', 'post_mutation_refresh', {
      saved: true,
      review_required: Boolean(result.review_required),
    });
    await loadDashboard('post_mutation_refresh');
  } catch (err) { statusBox.textContent = err.message; statusBox.className = 'upload-status error'; }
  finally { savingProfile = false; }
}

loadDashboard('initial_load');
loadProfile('initial_load');
loadNextMeal('initial_load');
loadTodayMeals('initial_load');

// FIX 44 (partial): the Mini App was a static snapshot -- Telegram meal
// logs, goal/allergy/pain changes, plan activation, workout completion,
// and HealthKit imports were all invisible in an already-open Mini App
// until the user manually tapped a refresh button. Refresh the read-only
// panels whenever the page regains focus/visibility (the tab was
// switched away and back, or the Mini App window was minimized and
// reopened), so a user who left Telegram open and came back sees current
// state without a manual action. Debounced so rapid focus/blur toggling
// (e.g. quickly switching between Telegram and the Mini App) does not
// spam the API. Does NOT touch mutation endpoints or add a revision/
// optimistic-concurrency protocol -- that is a larger project tracked as
// remaining FIX 44 work; this closes the specific, low-risk read-refresh
// gap.
let lastFocusRefresh = 0;
const FOCUS_REFRESH_MIN_INTERVAL_MS = 5000;

function refreshOnRegainedFocus() {
  const now = Date.now();
  if (now - lastFocusRefresh < FOCUS_REFRESH_MIN_INTERVAL_MS) return;
  lastFocusRefresh = now;
  loadDashboard('focus_refresh');
  loadNextMeal('focus_refresh');
  loadTodayMeals('focus_refresh');
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshOnRegainedFocus();
});
window.addEventListener('focus', refreshOnRegainedFocus);

document.getElementById('saveProfileBtn').addEventListener('click', saveProfile);
document.getElementById('refreshNextMealBtn').addEventListener('click', () => {
  obsAction('refresh_next_meal', 'next_meal', null);
  loadNextMeal('user_action');
});
document.getElementById('refreshMealsBtn').addEventListener('click', () => {
  obsAction('refresh_today_meals', 'today_meals', null);
  loadTodayMeals('user_action');
});
document.getElementById('nextMealBox').addEventListener('click', event => {
  const buttonEl = event.target.closest('.next-meal-status-btn');
  if (!buttonEl) return;
  setNextMealWorkoutStatus(buttonEl.dataset.status);
});
document.getElementById('generateNutritionBtn').addEventListener('click', () => generatePlans('nutrition'));
document.getElementById('generateWorkoutBtn').addEventListener('click', () => generatePlans('workout'));
document.getElementById('buildUnifiedBtn').addEventListener('click', buildUnified);
document.getElementById('planCandidates').addEventListener('click', event => {
  const buttonEl = event.target.closest('.activate-plan-btn');
  if (!buttonEl) return;
  activatePlan(Number(buttonEl.dataset.planId));
});

const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const form = document.getElementById('uploadForm');
const submitBtn = document.getElementById('submitBtn');
const status = document.getElementById('uploadStatus');
const fileNameEl = document.getElementById('fileName');

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    onFileSelected();
  }
});
fileInput.addEventListener('change', onFileSelected);

function onFileSelected() {
  const f = fileInput.files[0];
  if (!f) return;
  const name = f.name.toLowerCase();
  const suffix = name.endsWith('.zip') ? 'zip' : name.endsWith('.xml') ? 'xml' : 'other';
  obsAction('health_file_selected', 'health_upload', { suffix, byte_size: f.size });
  if (!name.endsWith('.zip') && !name.endsWith('.xml')) {
    status.textContent = 'צריך קובץ ZIP או XML בלבד.';
    status.className = 'upload-status error';
    submitBtn.disabled = true;
    return;
  }
  fileNameEl.textContent = f.name + ' (' + (f.size / 1048576).toFixed(1) + ' MB)';
  status.textContent = '';
  status.className = 'upload-status';
  submitBtn.disabled = false;
}

let uploading = false;
form.addEventListener('submit', async e => {
  e.preventDefault();
  const f = fileInput.files[0];
  if (!f || uploading) return;
  uploading = true;
  submitBtn.disabled = true;
  const uploadInteraction = obsAction('health_import_submitted', 'health_upload', { byte_size: f.size });
  status.textContent = 'מעלה ומעבד... זה עשוי לקחת דקה.';
  status.className = 'upload-status';
  const body = new FormData();
  body.append('file', f);
  try {
    const headers = uploadInteraction ? { 'X-Obs-Client-Interaction': uploadInteraction } : {};
    const resp = await fetch('/mini/upload', { method: 'POST', body, headers });
    const data = await resp.json();
    if (resp.ok) {
      status.textContent = data.message;
      status.className = 'upload-status success';
      obsViewRendered('health_upload', 'post_mutation_refresh', { ok: true });
      obsFlush();
      setTimeout(() => location.reload(), 2000);
    } else {
      status.textContent = data.detail || 'שגיאה בייבוא.';
      status.className = 'upload-status error';
      obsViewRendered('health_upload', 'post_mutation_refresh', { ok: false });
      submitBtn.disabled = false;
    }
  } catch (err) {
    status.textContent = 'שגיאת רשת: ' + err.message;
    status.className = 'upload-status error';
    submitBtn.disabled = false;
  } finally {
    uploading = false;
  }
});
