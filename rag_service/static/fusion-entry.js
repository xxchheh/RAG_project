'use strict';
async function refreshFusionEntry() {
  const entry = document.getElementById('fusion-entry');
  try {
    const token = sessionStorage.getItem('rag-token');
    if (!token || sessionStorage.getItem('rag-access') !== 'admin') {
      entry.classList.add('hidden');
      return;
    }
    const response = await fetch('/api/v1/admin/knowledge-fusion/status', {
      headers: {
        Authorization: 'Bearer ' + token
      }
    });
    entry.classList.toggle('hidden', !response.ok || !(await response.json()).enabled);
  } catch (_) {
    entry.classList.add('hidden');
  }
}
refreshFusionEntry();
document.getElementById('connection-form').addEventListener('submit', () => setTimeout(refreshFusionEntry, 500));
