/* Общие помощники Mini App: тема Telegram, API, элементы iOS, навигация. */
(function () {
  const tg = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData !== undefined) ? window.Telegram.WebApp : null;
  const at = v => { try { return !!tg && tg.isVersionAtLeast(v); } catch (e) { return false; } };

  // ── Тема: единая тёмная дизайн-система BobrGO Cloud; Telegram красим в цвет фона ──
  const BG = '#17191E';
  function applyTheme() {
    const root = document.documentElement;
    root.dataset.scheme = 'dark';
    root.classList.add('premium');
    if (!tg) return;
    try { if (at('6.1')) { tg.setHeaderColor(BG); tg.setBackgroundColor(BG); } } catch (e) {}
    try { if (at('7.10')) tg.setBottomBarColor(BG); } catch (e) {}
  }

  const UI = {
    tg,
    init() {
      try { tg && tg.ready(); tg && tg.expand(); } catch (e) {}
      // запрет масштабирования щипком и двойным тапом (iOS игнорирует user-scalable=no)
      ['gesturestart', 'gesturechange', 'gestureend'].forEach(t => document.addEventListener(t, e => e.preventDefault(), { passive: false }));
      document.addEventListener('touchmove', e => { if (e.touches && e.touches.length > 1) e.preventDefault(); }, { passive: false });
      // двойной тап отключён через CSS touch-action: pan-y — без перехвата касаний, чтобы не терять быстрые нажатия
      applyTheme();
      try { tg && tg.onEvent('themeChanged', applyTheme); } catch (e) {}
      try { if (at('7.7')) tg.disableVerticalSwipes(); } catch (e) {}
      // на телефонах — во весь экран; отступы под кнопки Telegram задают CSS-переменные --tg-*-safe-area-inset-*
      try {
        if (tg && at('8.0') && ['ios', 'android'].includes(tg.platform) && !tg.isFullscreen) tg.requestFullscreen();
        if (tg) tg.onEvent('fullscreenChanged', () => document.documentElement.classList.toggle('tg-fullscreen', !!tg.isFullscreen));
        if (tg && tg.isFullscreen) document.documentElement.classList.add('tg-fullscreen');
      } catch (e) {}
    },
    initData() { return tg ? tg.initData : ''; },

    api(base) {
      return async function (path, method = 'GET', body) {
        let r;
        try {
          r = await fetch(base + path, {
            method, headers: { 'Content-Type': 'application/json', 'X-Init-Data': UI.initData() },
            body: body === undefined ? undefined : JSON.stringify(body),
          });
        } catch (e) { throw new Error('Нет соединения с сервером. Проверьте интернет и попробуйте снова.'); }
        let j = null; try { j = await r.json(); } catch (e) {}
        if (!r.ok) throw new Error(UI.errText(j, r.status));
        return j;
      };
    },
    errText(j, status) {
      const d = j && (j.detail !== undefined ? j.detail : j.error);
      if (typeof d === 'string' && d) return d;
      if (Array.isArray(d)) return d.map(x => (x && (x.msg || x.message)) || String(x)).join('; ');
      if (d && typeof d === 'object') return d.msg || d.message || JSON.stringify(d);
      if (status === 403) return 'Нет доступа. Откройте приложение из Telegram.';
      if (status >= 500) return 'Сервер не смог выполнить запрос. Попробуйте ещё раз через минуту.';
      return 'Ошибка ' + status;
    },

    // ── элементы ──
    h(tag, props, ...kids) {
      const n = document.createElement(tag);
      for (const [k, v] of Object.entries(props || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'style') n.style.cssText = v;
        else if (k === 'html') n.innerHTML = v;
        else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
        else if (k in n && typeof v !== 'string') n[k] = v;
        else n.setAttribute(k, v === true ? '' : v);
      }
      for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) n.append(k.nodeType ? k : String(k));
      return n;
    },
    icon(name, cls) { const s = document.createElement('span'); s.style.display = 'contents'; s.innerHTML = ICONS[name] || ''; const svg = s.firstElementChild; if (svg && cls) svg.setAttribute('class', cls); return svg || s; },
    rub(v, frac) { return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: frac === undefined ? (Number.isInteger(+v) ? 0 : 2) : frac }).format(+v || 0) + ' ₽'; },
    date(iso) { if (!iso) return '—'; const d = new Date(iso); return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) + ', ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }); },
    flag(code) { const c = String(code || '').toUpperCase(); return /^[A-Z]{2}$/.test(c) ? String.fromCodePoint(...[...c].map(ch => 0x1F1E6 + ch.charCodeAt(0) - 65)) : '🌍'; },
    plural(n, one, few, many) { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b > 1 && b < 5 ? few : b === 1 ? one : many; },

    /* Плавное изменение числа (цены, баланса): from → to за ms, формат fmt. */
    tween(el, from, to, fmt, ms = 260) {
      cancelAnimationFrame(el._tw);
      if (from === to || from == null || matchMedia('(prefers-reduced-motion: reduce)').matches) { el.textContent = fmt(to); return; }
      const t0 = performance.now();
      const stepFn = t => {
        const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);
        el.textContent = fmt(k < 1 ? Math.round(from + (to - from) * e) : to);
        if (k < 1) el._tw = requestAnimationFrame(stepFn);
      };
      el._tw = requestAnimationFrame(stepFn);
    },
    bump(el) { el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump'); },
    /* Секция: подпись + карточка; footer — пояснение под карточкой. */
    section(label, body, footer, cls) {
      return UI.h('section', { class: 'section' + (cls ? ' ' + cls : '') }, label ? UI.h('div', { class: 'group-h' }, label) : null,
        body, footer ? UI.h('div', { class: 'group-f' }, footer) : null);
    },
    /* Приглушённые цвета иконок: старые iOS-цвета → палитра дизайн-системы. */
    TILE: { '#34C759': '#34D399', '#0A84FF': '#438CFF', '#007AFF': '#438CFF', '#5856D6': '#8B8CFF', '#FF9500': '#FBBF5B', '#FF3B30': '#FF6B6B',
      '#FF2D55': '#FF7A9A', '#AF52DE': '#C084FC', '#30B0C7': '#4CC9E0', '#34AADC': '#5BB8F0', '#8E8E93': '#A3A8B4' },

    // ── строки списков ──
    group(header, rows, footer) {
      return UI.h('section', { class: 'group' }, header ? UI.h('div', { class: 'group-h' }, header) : null,
        UI.h('div', { class: 'list' }, rows), footer ? UI.h('div', { class: 'group-f' }, footer) : null);
    },
    row({ title, sub, detail, icon, tile, flag, onClick, chevron, cls, right, label }) {
      const tap = !!onClick;
      const lead = icon ? UI.h('span', { class: 'icon-tile', style: '--tile:' + (UI.TILE[tile] || tile || 'var(--accent)') }, UI.icon(icon))
        : flag ? UI.h('span', { class: 'flag-tile', 'aria-hidden': 'true' }, flag) : null;
      const n = UI.h(tap ? 'button' : 'div', {
        class: 'row' + (tap ? ' tap' : '') + (lead ? ' has-icon' : '') + (cls ? ' ' + cls : ''), onclick: onClick, type: tap ? 'button' : undefined, 'aria-label': label,
      }, lead,
        UI.h('div', { class: 'grow' }, UI.h('div', { class: 't' }, title), sub ? UI.h('div', { class: 's' }, sub) : null),
        detail !== undefined && detail !== null ? UI.h('span', { class: 'd num' }, detail) : null,
        right || null,
        (chevron !== undefined ? chevron : tap && !/accent|destructive/.test(cls || '')) ? UI.icon('chevron', 'chev') : null);
      return n;
    },
    toggle(checked, onChange, label) {
      const i = UI.h('input', { type: 'checkbox', role: 'switch', 'aria-label': label });
      i.checked = !!checked;
      i.addEventListener('change', () => { UI.haptic('select'); onChange(i.checked, i); });
      return UI.h('label', { class: 'switch' }, i, UI.h('span'));
    },
    segmented(options, value, onChange, cls) {
      const wrap = UI.h('div', { class: 'segmented' + (cls ? ' ' + cls : ''), role: 'group' });
      options.forEach(o => {
        const b = UI.h('button', { type: 'button', 'aria-pressed': String(o.value === value) }, o.label);
        b.onclick = () => { if (o.value === value) return; value = o.value; [...wrap.children].forEach((c, i) => c.setAttribute('aria-pressed', String(options[i].value === value))); UI.haptic('select'); onChange(o.value); };
        wrap.append(b);
      });
      return wrap;
    },

    chips(options, value, onChange) {
      const wrap = UI.h('div', { class: 'chips', role: 'group' });
      options.forEach(o => {
        const b = UI.h('button', { type: 'button', 'aria-pressed': String(o.value === value) }, o.flag ? UI.h('span', { class: 'f', 'aria-hidden': 'true' }, o.flag) : null, o.label);
        b.onclick = () => { if (o.value === value) return; value = o.value; UI.haptic('select'); onChange(o.value); };
        wrap.append(b);
      });
      requestAnimationFrame(() => { const on = wrap.querySelector('[aria-pressed="true"]'); if (on && on.offsetLeft + on.offsetWidth > wrap.clientWidth) wrap.scrollLeft = on.offsetLeft - 16; });
      return wrap;
    },

    /* Аватар: фото из Telegram, иначе инициалы на цветном круге. src — функция, возвращающая Promise<url|null>. */
    avatar(user, size, src) {
      const name = (user && (user.first_name || user.username)) || '?';
      const letters = name.replace(/^@/, '').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
      const colors = ['#FF6B6B', '#F7A23B', '#34C759', '#30B0C7', '#007AFF', '#5856D6', '#AF52DE', '#FF2D55'];
      const id = Number(user && (user.id || user.tg_id)) || 0;
      const n = UI.h('span', { class: 'avatar', 'aria-hidden': 'true', style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.4)}px;--ava-bg:${colors[id % colors.length]}` }, letters);
      if (src) Promise.resolve(src()).then(url => { if (!url) return; const img = new Image(); img.alt = ''; img.onload = () => n.replaceChildren(img); img.src = url; }).catch(() => {});
      return n;
    },
    async blobUrl(path) {
      const r = await fetch(path, { headers: { 'X-Init-Data': UI.initData() } });
      if (!r.ok) return null;
      return URL.createObjectURL(await r.blob());
    },

    // ── обратная связь ──
    haptic(kind) {
      try {
        if (!tg || !at('6.1')) return;
        if (kind === 'select') tg.HapticFeedback.selectionChanged();
        else if (kind === 'success' || kind === 'error' || kind === 'warning') tg.HapticFeedback.notificationOccurred(kind);
        else tg.HapticFeedback.impactOccurred(kind || 'light');
      } catch (e) {}
    },
    toast(text, isErr) {
      let t = document.querySelector('.toast');
      if (!t) { t = UI.h('div', { class: 'toast', role: 'status', 'aria-live': 'polite' }); document.body.append(t); }
      t.className = 'toast' + (isErr ? ' err' : '');
      t.replaceChildren(UI.icon(isErr ? 'alert' : 'checkCircle'), UI.h('span', {}, text));
      requestAnimationFrame(() => t.classList.add('show'));
      clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
      UI.haptic(isErr ? 'error' : 'success');
    },
    alert(msg) { return new Promise(r => { try { if (at('6.2')) return tg.showAlert(msg, () => r()); } catch (e) {} window.alert(msg); r(); }); },
    /* Диалог действия. buttons: [{id, text, type:'default'|'destructive'}]; кнопка «Отмена» добавляется сама. */
    ask({ title, message, buttons }) {
      return new Promise(res => {
        try {
          if (at('6.2')) {
            return tg.showPopup({ title: title || '', message, buttons: [...buttons.map(b => ({ id: b.id, type: b.type || 'default', text: b.text })), { type: 'cancel' }] }, id => res(id || null));
          }
        } catch (e) {}
        res(window.confirm((title ? title + '\n\n' : '') + message) ? buttons[0].id : null);
      });
    },
    async run(fn, okText) {
      try { const r = await fn(); if (okText) UI.toast(okText); return r; }
      catch (e) { UI.haptic('error'); await UI.alert(e.message || String(e)); return undefined; }
    },
    busy(btn, fn) { return async (ev) => { if (btn.disabled) return; btn.disabled = true; try { await fn(ev); } finally { btn.disabled = false; } }; },
    copy(text, what) {
      const done = () => UI.toast((what || 'Текст') + ' скопирован');
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => fallback());
      else fallback();
      function fallback() { const ta = UI.h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} ta.remove(); }
    },
    openLink(url) { try { if (tg) return tg.openLink(url); } catch (e) {} window.open(url, '_blank'); },

    /* Переписка: messages [{author, text, created}], mine — какие авторы «свои» (справа). */
    chat(messages, mine, labels) {
      const box = UI.h('div', { class: 'chat' });
      let day = '';
      messages.forEach(m => {
        const d = new Date(m.created), dd = d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
        if (dd !== day) { day = dd; box.append(UI.h('div', { class: 'day-sep' }, dd)); }
        const t = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        if (m.author === 'system') return box.append(UI.h('div', { class: 'msg sys' }, UI.h('div', { class: 'bubble' }, m.text + ' · ' + t)));
        if (m.author === 'note') return box.append(UI.h('div', { class: 'msg note' }, UI.h('div', { class: 'bubble' }, UI.h('div', { class: 'who' }, (labels && labels.note) || 'Заметка'), m.text), UI.h('time', {}, t)));
        const cls = mine.includes(m.author) ? 'mine' : 'theirs';
        box.append(UI.h('div', { class: 'msg ' + cls }, UI.h('div', { class: 'bubble' }, m.text), UI.h('time', {}, (labels && labels[m.author] ? labels[m.author] + ' · ' : '') + t + (m.source === 'bot' ? ' · из чата' : ''))));
      });
      return box;
    },
    /* Поле ввода внизу экрана. onSend(text, extra) → Promise; extra — состояние доп. кнопки (например, «заметка»). */
    composer(placeholder, onSend, modeLabel) {
      const ta = UI.h('textarea', { rows: 1, placeholder, 'aria-label': placeholder, maxlength: 4000 });
      const send = UI.h('button', { class: 'send', type: 'button', 'aria-label': 'Отправить', disabled: true }, UI.icon('send'));
      let mode = false;
      const modeBtn = modeLabel ? UI.h('button', { class: 'mode', type: 'button', 'aria-pressed': 'false', onclick: () => { mode = !mode; modeBtn.classList.toggle('on', mode); modeBtn.setAttribute('aria-pressed', String(mode)); ta.placeholder = mode ? 'Заметка для себя' : placeholder; UI.haptic('select'); } }, modeLabel) : null;
      const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; send.disabled = !ta.value.trim(); };
      ta.addEventListener('input', fit);
      send.onclick = UI.busy(send, async () => { const text = ta.value.trim(); if (!text) return; const ok = await onSend(text, mode); if (ok !== false) { ta.value = ''; fit(); } });
      return UI.h('div', { class: 'composer' }, modeBtn, ta, send);
    },

    // ── нижний лист ──
    sheet(title, content) {
      const scrim = UI.h('div', { class: 'scrim' });
      const close = () => { sh.classList.remove('show'); scrim.classList.remove('show'); setTimeout(() => { sh.remove(); scrim.remove(); }, 350); UI.nav._sheetClose = null; UI.nav.syncBack(); };
      const sh = UI.h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
        UI.h('div', { class: 'grabber' }),
        UI.h('div', { class: 'sheet-h' }, UI.h('b', {}, title), UI.h('button', { class: 'btn plain', style: 'width:auto;min-height:0;padding:4px 0', onclick: close }, 'Готово')),
        typeof content === 'function' ? content(close) : content);
      scrim.onclick = close;
      document.body.append(scrim, sh);
      requestAnimationFrame(() => { scrim.classList.add('show'); sh.classList.add('show'); });
      UI.nav._sheetClose = close; UI.nav.syncBack();
      return close;
    },

    // ── навигация со стеком и системной кнопкой «Назад» ──
    nav: {
      stack: [], root: null, _sheetClose: null, _onRoot: null,
      mount(rootEl, renderRoot) { this.root = rootEl; this._onRoot = renderRoot; this.stack = []; this.render(false);
        try { if (tg && at('6.1')) tg.BackButton.onClick(() => this.back()); } catch (e) {} },
      push(render) { this.stack.push(render); this.render(false); window.scrollTo(0, 0); UI.haptic('light'); },
      back() { if (this._sheetClose) return this._sheetClose(); if (!this.stack.length) return; this.stack.pop(); this.render(true); },
      reset() { this.stack = []; this.render(false); },
      refresh() { this.render(null); },
      render(isPop) {
        const fn = this.stack.length ? this.stack[this.stack.length - 1] : this._onRoot;
        const node = fn();
        const v = UI.h('div', { class: isPop === null ? '' : 'view' + (isPop ? ' pop' : '') }, node);
        this.root.replaceChildren(v);
        document.body.classList.toggle('has-composer', !!v.querySelector('.composer'));
        this.syncBack();
        document.dispatchEvent(new CustomEvent('navchange', { detail: { depth: this.stack.length } }));
      },
      syncBack() { try { if (tg && at('6.1')) (this.stack.length || this._sheetClose) ? tg.BackButton.show() : tg.BackButton.hide(); } catch (e) {} },
      backLink(text) { // для браузера вне Telegram, где нет системной кнопки
        if (tg && at('6.1')) return document.createTextNode('');  // в Telegram — системная кнопка «Назад»
        return UI.h('div', { class: 'nav' }, UI.h('button', { class: 'back', type: 'button', onclick: () => UI.nav.back() }, UI.icon('back'), text || 'Назад'));
      },
    },
  };

  const S = (d, extra = '') => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${d}</svg>`;
  const ICONS = {
    chevron: '<svg viewBox="0 0 8 13" aria-hidden="true"><path d="M1.5 1.5 6.5 6.5l-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    back: '<svg viewBox="0 0 12 20" width="12" height="20" aria-hidden="true"><path d="M10 2 2 10l8 8" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    server: S('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
    plus: S('<path d="M12 5v14M5 12h14"/>'),
    plusCircle: S('<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>'),
    wallet: S('<path d="M3 7a2 2 0 0 1 2-2h12v4"/><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M16.5 13.5h.01"/>'),
    gear: S('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    copy: S('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>'),
    eye: S('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
    eyeOff: S('<path d="M17.9 17.9A10.4 10.4 0 0 1 12 19c-6.4 0-10-7-10-7a18 18 0 0 1 4.1-5M9.9 5.2A9.6 9.6 0 0 1 12 5c6.4 0 10 7 10 7a18 18 0 0 1-2.2 3.2M3 3l18 18"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>'),
    power: S('<path d="M12 3v8"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>'),
    restart: S('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
    stop: S('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
    terminal: S('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>'),
    globe: S('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
    key: S('<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.2-8.2M17 6l2 2M15 8l2 2"/>'),
    disc: S('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>'),
    trash: S('<path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>'),
    renew: S('<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/>'),
    receipt: S('<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>'),
    chat: S('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>'),
    checkCircle: S('<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>'),
    check: S('<path d="m5 12.5 4.5 4.5L19 7.5"/>', 'stroke-width="2.4"'),
    alert: S('<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>'),
    users: S('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 20a6.5 6.5 0 0 0-3-5.5"/>'),
    card: S('<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h3"/>'),
    tag: S('<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><path d="M7.5 7.5h.01"/>'),
    store: S('<path d="M4 9h16l-1-5H5zM4 9v11h16V9"/><path d="M9 20v-6h6v6"/>'),
    text: S('<path d="M5 6h14M5 12h14M5 18h9"/>'),
    person: S('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
    doc: S('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>'),
    shield: S('<path d="M12 3 4 6v6c0 5 3.4 8.4 8 9 4.6-.6 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>'),
    gift: S('<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M19 12v9H5v-9M7.5 8a2.5 2.5 0 1 1 0-5C10 3 12 8 12 8s2-5 4.5-5a2.5 2.5 0 1 1 0 5"/>'),
    clock: S('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    send: S('<path d="M12 19V5M5 12l7-7 7 7"/>', 'stroke-width="2.4"'),
    lifebuoy: S('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/><path d="m5.6 5.6 3.9 3.9M14.5 14.5l3.9 3.9M18.4 5.6l-3.9 3.9M9.5 14.5l-3.9 3.9"/>'),
    cpu: S('<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>'),
    ram: S('<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7v10M11 7v10M15 7v10M6 17v3M18 17v3"/>'),
    drive: S('<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 15h.01M11 15h6M3 11h18"/>'),
    pin: S('<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>'),
    bolt: S('<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'),
    arrowDown: S('<path d="M12 5v14M6 13l6 6 6-6"/>'),
    sparkle: S('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z"/>'),
    share: S('<path d="M21 4 3 11l7 2.5L12.5 21z"/><path d="m21 4-11 9.5"/>'),
    link: S('<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4L12 5.6"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3.2 3.2a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/>'),
    info: S('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>'),
    bell: S('<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>'),
    megaphone: S('<path d="M3 10.5v3a1 1 0 0 0 1 1h2.5L12 18V6L6.5 9.5H4a1 1 0 0 0-1 1z"/><path d="M15.5 9a3.5 3.5 0 0 1 0 6M18.5 6a7.5 7.5 0 0 1 0 12"/>'),
    stack: S('<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>'),
  };

  window.UI = UI;
})();
