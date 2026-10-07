(() => {
  const note = document.querySelector('[data-sms-live-note]');
  if (!note) return;
  const normal = note.textContent;
  let timer, busy = false, stopped = false;
  const schedule = () => {
    clearTimeout(timer);
    if (!stopped && !document.hidden) timer = setTimeout(refresh, 30000);
  };
  const refresh = async () => {
    if (busy || stopped || document.hidden) return;
    busy = true;
    clearTimeout(timer);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const rows = [...document.querySelectorAll('tr[data-message-id]')];
      const query = new URLSearchParams();
      rows.forEach(row => query.append('id', row.dataset.messageId));
      const response = await fetch(`/api/v1/sms/statuses?${query}`, {
        cache: 'no-store', signal: controller.signal,
      });
      if (response.status === 401 || response.status === 403 || response.redirected) {
        stopped = true;
        throw new Error('session');
      }
      if (!response.ok) throw new Error('unavailable');
      const {data} = await response.json();
      const messages = new Map(data.messages.map(message => [String(message.id), message]));
      rows.forEach(row => {
        const message = messages.get(row.dataset.messageId);
        if (!message) return;
        const badge = row.querySelector('.sms-status');
        badge.textContent = message.status_label;
        badge.className = `sms-status is-${message.status}`;
        row.querySelector('[data-sms-segments]').textContent = message.segments || '—';
        row.querySelector('[data-sms-cost]').textContent = message.cost == null ? '—' : `${message.cost} ${message.currency || ''}`;
      });
      document.querySelectorAll('[data-sms-summary]').forEach(node => {
        node.textContent = data.summary[node.dataset.smsSummary];
      });
      note.textContent = normal;
    } catch (error) {
      note.textContent = stopped
        ? 'Сессия завершена. Обновите страницу и войдите снова.'
        : 'Автообновление временно недоступно. Повторим через 30 секунд.';
    } finally {
      clearTimeout(timeout);
      busy = false;
      schedule();
    }
  };
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) refresh();
  });
  document.addEventListener('sms:statuses-synced', refresh);
  schedule();
})();
