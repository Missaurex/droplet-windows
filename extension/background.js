// Мост между вкладкой amoCRM и программой Droplet Звонки на этом компьютере (только 127.0.0.1).
const API = 'http://127.0.0.1:47811/';
chrome.runtime.onMessage.addListener((m, sender, reply) => {
  if (m.t === 'focus') {
    chrome.tabs.update(sender.tab.id, { active: true });
    chrome.windows.update(sender.tab.windowId, { focused: true });
    return;
  }
  fetch(API + m.t, { method: 'POST', headers: { 'X-Droplet': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(m.body) })
    .then(r => r.json()).then(reply, () => reply(null));
  return true; // ответ придёт позже
});
