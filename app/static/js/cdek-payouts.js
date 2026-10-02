(() => {
  'use strict';
  const root = document.getElementById('cdek-payouts');
  if (!root) return;
  const form = root.querySelector('#period-form');
  form.addEventListener('submit', event => {
    const start = form.elements.namedItem('from');
    const end = form.elements.namedItem('to');
    end.setCustomValidity(start.value > end.value ? 'Конец периода не может быть раньше начала.' : '');
    if (!form.reportValidity()) event.preventDefault();
  });
  form.addEventListener('input', () => form.elements.namedItem('to').setCustomValidity(''));
  root.querySelectorAll('[data-days]').forEach(button => button.addEventListener('click', () => {
    const end = root.dataset.today;
    const day = new Date(end + 'T12:00:00Z');
    if (button.dataset.days === 'month') day.setUTCDate(1);
    else day.setUTCDate(day.getUTCDate() - Number(button.dataset.days) + 1);
    form.elements.namedItem('from').value = day.toISOString().slice(0, 10);
    form.elements.namedItem('to').value = end;
    form.requestSubmit();
  }));
  const button = root.querySelector('#refresh');
  const message = root.querySelector('#sync-message');
  let timer = null;
  let failures = 0;
  function say(text) { message.textContent = text; message.hidden = false; }
  async function update(method) {
    button.disabled = true;
    try {
      const response = await fetch(root.dataset.sync, { method, credentials: 'same-origin',
        headers: { 'X-CSRF-Token': root.dataset.csrf, 'Accept': 'application/json' } });
      const state = await response.json();
      if (!response.ok) throw new Error(state.message || 'Не удалось обновить данные.');
      failures = 0;
      if (state.outcome === 'running') {
        say('Сверяем накладные и реестры. Пока показан предыдущий полный снимок.');
        timer = setTimeout(() => update('GET'), 4000);
      } else if (state.outcome === 'success') {
        window.location.reload();
      } else {
        say(state.message || 'Проверка ещё не запускалась.');
        button.disabled = false;
      }
    } catch (error) {
      say(error.message || 'Нет связи с сервером. Сохранённые суммы не изменены.');
      button.disabled = false;
      // No unbounded polling on failed requests or an expired login.
      failures += 1;
    }
  }
  button.addEventListener('click', () => { clearTimeout(timer); update('POST'); });
  if (root.dataset.running === '1') update('GET');
  window.addEventListener('pagehide', () => clearTimeout(timer));
})();
