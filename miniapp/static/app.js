// Noam Coach Mini App client.
// All business calculations happen on the server; this file only renders state
// and forwards user actions to the API. Never trust client-side values for
// goals, targets or plans.

async function apiCall(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'object' ? detail.message : detail;
    throw new Error(message || 'הפעולה נכשלה');
  }
  return data;
}

function planCard(plan, index) {
  const rationale = (plan.rationale || []).slice(0, 3).join(' · ');
  const tradeoffs = (plan.tradeoffs || []).slice(0, 2).join(' · ');
  const score = Math.round((plan.fit_score ?? plan.score ?? 0) * 100);
  return `<div class="candidate"><b>${index}. ${plan.title}</b> · התאמה ${score}%<br>` +
    `<small>✓ ${rationale}<br>△ ${tradeoffs}</small><br>` +
    `<button class="action-btn" onclick="activatePlan(${plan.id})">בחר כתוכנית ראשית</button></div>`;
}

async function loadDashboard() {
  const box = document.getElementById('planStatus');
  try {
    const data = await apiCall('/mini/api/dashboard');
    const plans = data.active_plans || {};
    const action = data.coaching?.next_action;
    box.innerHTML = action ?
      `<div class="candidate"><b>הפעולה הבאה: ${action.title}</b><br><small>${action.reason}</small></div>` : '';
    box.innerHTML += `תזונה: <b>${plans.nutrition?.title || 'טרם נבחרה'}</b><br>` +
      `אימונים: <b>${plans.workout?.title || 'טרם נבחרה'}</b><br>` +
      `שבוע מאוחד: <b>${plans.unified?.title || 'טרם נבנה'}</b>`;
  } catch (err) { box.innerHTML = `<span class="error">${err.message}</span>`; }
}

async function generatePlans(type) {
  const box = document.getElementById('planCandidates');
  box.textContent = 'בונה ובודק שלוש חלופות…';
  try {
    const data = await apiCall(`/mini/api/plans/${type}/generate`, { method: 'POST' });
    box.innerHTML = (data.candidates || []).map((plan, i) => planCard(plan, i + 1)).join('');
  } catch (err) { box.innerHTML = `<span class="error">${err.message}</span>`; }
}

async function activatePlan(id) {
  const box = document.getElementById('planCandidates');
  try {
    const data = await apiCall(`/mini/api/plans/${id}/activate`, { method: 'POST' });
    box.innerHTML = `<b>${data.active.title}</b> נבחרה כתוכנית הראשית ✅`;
    await loadDashboard();
  } catch (err) { box.innerHTML = `<span class="error">${err.message}</span>`; }
}

async function buildUnified() {
  const box = document.getElementById('planCandidates');
  box.textContent = 'מחבר את התזונה והאימונים לשבוע אחד…';
  try {
    await apiCall('/mini/api/plans/unified/build', { method: 'POST' });
    box.textContent = 'התוכנית השבועית נבנתה ✅';
    await loadDashboard();
  } catch (err) { box.innerHTML = `<span class="error">${err.message}</span>`; }
}

function factValue(snapshot, key) {
  return snapshot?.facts?.[key]?.value ?? null;
}

async function loadProfile() {
  const statusBox = document.getElementById('profileStatus');
  try {
    const data = await apiCall('/mini/api/profile');
    const snapshot = data.snapshot || {};
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
  } catch (err) { statusBox.textContent = err.message; statusBox.className = 'upload-status error'; }
}

let savingProfile = false;
async function saveProfile() {
  const statusBox = document.getElementById('profileStatus');
  if (savingProfile) return; // guard against double-click duplicate writes
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
  statusBox.textContent = 'שומר…';
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
    await loadDashboard();
  } catch (err) { statusBox.textContent = err.message; statusBox.className = 'upload-status error'; }
  finally { savingProfile = false; }
}

loadDashboard();
loadProfile();

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
  if (!f || uploading) return; // guard against double submit
  uploading = true;
  submitBtn.disabled = true;
  status.textContent = '📥 מעלה ומעבד… זה עשוי לקחת דקה.';
  status.className = 'upload-status';
  const body = new FormData();
  body.append('file', f);
  try {
    const resp = await fetch('/mini/upload', { method: 'POST', body });
    const data = await resp.json();
    if (resp.ok) {
      status.textContent = data.message;
      status.className = 'upload-status success';
      setTimeout(() => location.reload(), 2000);
    } else {
      status.textContent = data.detail || 'שגיאה בייבוא.';
      status.className = 'upload-status error';
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
