// Во вкладке amoCRM: раз в 2 секунды говорим программе, какая карточка открыта, и выполняем её просьбы
// (найти клиента, открыть карточку, вписать примечание) — запросами самой amoCRM с входом МОПа.
async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET', credentials: 'include', body: body || undefined,
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
  });
  return { code: r.status, text: await r.text() };
}

function ask(t, body) {
  return new Promise(ok => { try { chrome.runtime.sendMessage({ t, body }, ok); } catch (e) { ok(null); } });
}

async function tick() {
  const jobs = await ask('poll', { url: location.href, visible: document.visibilityState === 'visible' });
  for (const j of jobs || []) {
    let res;
    try {
      if (j.type === 'open') {
        await ask('done', { id: j.id, code: 200, text: '' });
        chrome.runtime.sendMessage({ t: 'focus' });
        location.assign(j.path);
        return;
      }
      res = await api(j.path, j.body);
    } catch (e) { res = { code: 0, text: String(e) }; }
    await ask('done', { id: j.id, ...res });
  }
}
setInterval(tick, 2000);
tick();
