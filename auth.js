(() => {
  'use strict';

  const HASHES = Object.freeze({
    day: '8473d01df08bbb2e40cd7f0ad5e7c9ba002e3674eb16cbbe261c3c46991d6b9c',
    night: 'dfc1d541e6dbbc1f24d98dde8da2f19bd6fc57565ff43ff04a012a12958966ca',
  });
  const SESSION_KEY = 'yafco_access_v2';
  const FAILURE_KEY = 'yafco_access_failures_v2';

  function shanghaiParts() {
    const parts = new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).formatToParts(new Date());
    return Object.fromEntries(parts.map(part => [part.type, part.value]));
  }

  function windowState() {
    const p = shanghaiParts();
    const hour = Number(p.hour) % 24;
    const date = `${p.year}-${p.month}-${p.day}`;
    if (hour < 8) return { mode: 'maintenance', slot: `${date}:maintenance`, title: '服务器维护中', message: '北京时间 08:00 恢复访问。维护窗不加载行情与分析数据。' };
    if (hour < 9) return { mode: 'free', slot: `${date}:free`, title: '免费开放时段', message: '北京时间 08:00–08:59，无需密码。' };
    if (hour < 16) return { mode: 'day', slot: `${date}:day`, title: '日间访问验证', message: '北京时间 09:00–15:59，输入日间密码。' };
    return { mode: 'night', slot: `${date}:night`, title: '夜间访问验证', message: '北京时间 16:00–23:59，输入夜间密码。' };
  }

  async function digest(value) {
    const bytes = new TextEncoder().encode(value);
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }

  async function serverAuthorize(password) {
    try {
      const response = await fetch('/__auth', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
      if (response.status === 404 || response.status === 405) return true;
      return response.ok;
    } catch (_) { return true; }
  }

  function failures() {
    try { return JSON.parse(localStorage.getItem(FAILURE_KEY) || '{}'); } catch (_) { return {}; }
  }

  function isUnlocked(current) {
    if (current.mode === 'free') return true;
    if (current.mode === 'maintenance') return false;
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || '{}').slot === current.slot; } catch (_) { return false; }
  }

  function showGate(current, message) {
    const gate = document.getElementById('accessGate');
    const form = document.getElementById('accessForm');
    document.getElementById('accessTitle').textContent = current.title;
    document.getElementById('accessMessage').textContent = message || current.message;
    form.classList.toggle('hidden', current.mode === 'maintenance' || current.mode === 'free');
    gate.classList.remove('unlocked');
    document.documentElement.classList.add('access-locked');
  }

  function unlock(current) {
    const gate = document.getElementById('accessGate');
    gate.classList.add('unlocked');
    document.documentElement.classList.remove('access-locked');
    window.dispatchEvent(new CustomEvent('yafco:authorized', { detail: current }));
  }

  function enforce() {
    const current = windowState();
    const previous = window.__yafcoWindow;
    window.__yafcoWindow = current;
    if (previous && previous.slot !== current.slot) sessionStorage.removeItem(SESSION_KEY);
    if (isUnlocked(current)) unlock(current); else showGate(current);
    return current;
  }

  document.addEventListener('DOMContentLoaded', () => {
    let current = enforce();
    const form = document.getElementById('accessForm');
    const input = document.getElementById('accessPassword');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      current = windowState();
      if (current.mode === 'maintenance') return showGate(current);
      const record = failures();
      const now = Date.now();
      if (record.lockUntil && now < record.lockUntil) {
        return showGate(current, `尝试过多，请 ${Math.ceil((record.lockUntil - now) / 1000)} 秒后再试。`);
      }
      const password = input.value;
      const ok = current.mode === 'free' || await digest(password) === HASHES[current.mode];
      input.value = '';
      if (ok && await serverAuthorize(password)) {
        sessionStorage.setItem(SESSION_KEY, JSON.stringify({ slot: current.slot, at: now }));
        localStorage.removeItem(FAILURE_KEY);
        unlock(current);
      } else {
        const count = (record.count || 0) + 1;
        const lockUntil = count >= 5 ? now + Math.min(300000, 30000 * (count - 4)) : 0;
        localStorage.setItem(FAILURE_KEY, JSON.stringify({ count, lockUntil }));
        showGate(current, lockUntil ? '密码错误次数过多，已暂时锁定。' : `密码不正确，还可尝试 ${5 - count} 次。`);
      }
    });
    setInterval(enforce, 30000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) enforce(); });
  });

  window.YAFCOAccess = { current: windowState, allowed: () => isUnlocked(windowState()) };
})();
