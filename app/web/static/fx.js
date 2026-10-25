/* Оформление мини-приложения: фоны и сезонные эффекты (снег, Новый год, Хэллоуин, листопад, сердечки, весна, конфетти).
   Два слоя частиц: дальний (крупные, размытые, боке) — за карточками, ближний (мелкие, со свечением) — поверх.
   Клики проходят насквозь; ~30 кадров/с; пауза в фоне; выключение при «уменьшить движение». */
(function () {
  const BACKGROUNDS = {
    none: null,
    aurora: { light: 'radial-gradient(120% 70% at 0% 0%, #C9E7FF 0%, transparent 60%), radial-gradient(100% 60% at 100% 10%, #E5D4FF 0%, transparent 55%), radial-gradient(120% 80% at 50% 100%, #CFF5E7 0%, transparent 60%), #F2F2F7',
              dark: 'radial-gradient(120% 70% at 0% 0%, #0E3B5C 0%, transparent 60%), radial-gradient(100% 60% at 100% 10%, #3A1F5C 0%, transparent 55%), radial-gradient(120% 80% at 50% 100%, #0F4034 0%, transparent 60%), #000' },
    night: { light: 'linear-gradient(180deg, #DDE3F5 0%, #F2F2F7 60%)',
             dark: 'radial-gradient(1px 1px at 20% 15%, #fff8 50%, transparent 51%), radial-gradient(1px 1px at 70% 8%, #fff6 50%, transparent 51%), radial-gradient(1.5px 1.5px at 40% 30%, #fff5 50%, transparent 51%), radial-gradient(1px 1px at 85% 40%, #fff7 50%, transparent 51%), radial-gradient(1px 1px at 10% 55%, #fff5 50%, transparent 51%), linear-gradient(180deg, #0B1026 0%, #000 70%)' },
    ocean: { light: 'linear-gradient(180deg, #D6EEFF 0%, #EAF6FF 45%, #F2F2F7 100%)', dark: 'linear-gradient(180deg, #06263D 0%, #031421 50%, #000 100%)' },
    sunset: { light: 'linear-gradient(180deg, #FFE1CC 0%, #FFE9F0 40%, #F2F2F7 100%)', dark: 'linear-gradient(180deg, #3D1622 0%, #1F0E1A 45%, #000 100%)' },
    forest: { light: 'linear-gradient(180deg, #DDF2E3 0%, #EEF7F0 45%, #F2F2F7 100%)', dark: 'linear-gradient(180deg, #0D2A1B 0%, #08170F 50%, #000 100%)' },
  };
  const BG_NAMES = { none: 'Без фона', aurora: 'Северное сияние', night: 'Ночь', ocean: 'Океан', sunset: 'Закат', forest: 'Лес', image: 'Своя картинка' };
  const EFFECT_NAMES = { none: 'Без эффектов', auto: 'По календарю', snow: 'Снег', newyear: 'Новый год', halloween: 'Хэллоуин', leaves: 'Листопад', hearts: 'Сердечки', flowers: 'Весна', confetti: 'Конфетти' };

  /* Автовыбор по дате (месяц 1–12, день). */
  function seasonal(d) {
    const m = d.getMonth() + 1, day = d.getDate(), md = m * 100 + day;
    if (md >= 1215 || md <= 114) return 'newyear';
    if (md >= 1024 && md <= 1101) return 'halloween';
    if (md >= 210 && md <= 214) return 'hearts';
    if (md >= 305 && md <= 308) return 'flowers';
    if (m === 12 || m === 1 || m === 2) return 'snow';
    if (md >= 915 && md <= 1110) return 'leaves';
    return 'none';
  }

  /* ───────── Движок эффектов: два слоя (за контентом и поверх), спрайты со свечением, боке, 3D-переворот ───────── */
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  const isDark = () => { const r = document.documentElement; return r.dataset.scheme ? r.dataset.scheme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches; };
  const rnd = (a, b) => a + Math.random() * (b - a);
  const pick = a => a[(Math.random() * a.length) | 0];

  /* Рисует фигуру один раз в отдельный холст (со свечением/размытием), дальше — только drawImage: быстро даже на слабых телефонах. */
  function sprite(size, draw, o = {}) {
    const pad = Math.ceil((o.glow || 0) * 1.6 + (o.blur || 0) * 2.2 + 2);
    const S = Math.ceil(size + pad * 2);
    const c = document.createElement('canvas'); c.width = c.height = Math.ceil(S * DPR);
    const g = c.getContext('2d'); g.scale(DPR, DPR); g.translate(S / 2, S / 2);
    if (o.blur) { // размытие через тень (работает везде, в отличие от ctx.filter): рисуем за кадром, тень — в кадре
      g.shadowColor = o.color || '#fff'; g.shadowBlur = o.blur; g.shadowOffsetX = 4000; g.translate(-4000, 0);
    } else if (o.glow) { g.shadowColor = o.glowColor || o.color || '#fff'; g.shadowBlur = o.glow; }
    draw(g, size / 2);
    return { c, S };
  }

  const shapes = {
    flake(g, r, col) {
      g.strokeStyle = col; g.lineCap = 'round'; g.lineWidth = Math.max(1.2, r * 0.11);
      for (let i = 0; i < 6; i++) {
        g.save(); g.rotate(i * Math.PI / 3);
        g.beginPath(); g.moveTo(0, 0); g.lineTo(0, -r);
        for (const t of [0.45, 0.72]) { const b = r * (t === 0.45 ? 0.3 : 0.22); g.moveTo(0, -r * t); g.lineTo(-b * 0.75, -r * t - b * 0.75); g.moveTo(0, -r * t); g.lineTo(b * 0.75, -r * t - b * 0.75); }
        g.stroke(); g.restore();
      }
      g.beginPath(); g.arc(0, 0, r * 0.12, 0, 6.283); g.fillStyle = col; g.fill();
    },
    dot(g, r, col) { g.beginPath(); g.arc(0, 0, r, 0, 6.283); g.fillStyle = col; g.fill(); },
    bokeh(g, r, col) {
      const gr = g.createRadialGradient(0, 0, 0, 0, 0, r);
      gr.addColorStop(0, col); gr.addColorStop(0.55, col.replace(/[\d.]+\)$/, m => (parseFloat(m) * 0.55).toFixed(2) + ')')); gr.addColorStop(1, col.replace(/[\d.]+\)$/, '0)'));
      g.fillStyle = gr; g.beginPath(); g.arc(0, 0, r, 0, 6.283); g.fill();
    },
    heart(g, r, base, light, deep) {
      g.beginPath(); g.moveTo(0, r * 0.92);
      g.bezierCurveTo(-r * 1.35, r * 0.05, -r * 0.72, -r * 1.0, 0, -r * 0.38);
      g.bezierCurveTo(r * 0.72, -r * 1.0, r * 1.35, r * 0.05, 0, r * 0.92); g.closePath();
      const gr = g.createRadialGradient(-r * 0.35, -r * 0.45, r * 0.05, 0, 0, r * 1.25);
      gr.addColorStop(0, light); gr.addColorStop(0.45, base); gr.addColorStop(1, deep);
      g.fillStyle = gr; g.fill();
      g.shadowBlur = 0; g.globalAlpha = 0.45; g.fillStyle = '#fff';
      g.beginPath(); g.ellipse(-r * 0.42, -r * 0.42, r * 0.2, r * 0.11, -0.7, 0, 6.283); g.fill(); g.globalAlpha = 1;
    },
    petal(g, r) {
      g.beginPath(); g.moveTo(0, r);
      g.bezierCurveTo(-r * 0.95, r * 0.3, -r * 0.55, -r * 0.85, -r * 0.12, -r * 0.95);
      g.lineTo(0, -r * 0.7); g.lineTo(r * 0.12, -r * 0.95);
      g.bezierCurveTo(r * 0.55, -r * 0.85, r * 0.95, r * 0.3, 0, r); g.closePath();
      const gr = g.createLinearGradient(0, r, 0, -r); gr.addColorStop(0, '#FF7FA6'); gr.addColorStop(0.6, '#FFC2D4'); gr.addColorStop(1, '#FFE6EE');
      g.fillStyle = gr; g.fill();
    },
    blossom(g, r) {
      for (let i = 0; i < 5; i++) { g.save(); g.rotate(i * 1.2566); g.translate(0, -r * 0.48); shapes.petal(g, r * 0.52); g.restore(); }
      g.shadowBlur = 0;
      g.beginPath(); g.arc(0, 0, r * 0.16, 0, 6.283); g.fillStyle = '#FF5C8A'; g.fill();
      for (let i = 0; i < 7; i++) { const a = i * 0.9; g.beginPath(); g.arc(Math.cos(a) * r * 0.24, Math.sin(a) * r * 0.24, r * 0.045, 0, 6.283); g.fillStyle = '#FFD45C'; g.fill(); }
    },
    ribbon(g, r, col) {
      const w = r * 1.9, h = r * 0.7;
      const gr = g.createLinearGradient(0, -h / 2, 0, h / 2); gr.addColorStop(0, '#ffffffaa'); gr.addColorStop(0.35, col); gr.addColorStop(1, col);
      g.fillStyle = gr; g.beginPath();
      if (g.roundRect) g.roundRect(-w / 2, -h / 2, w, h, h * 0.35); else g.rect(-w / 2, -h / 2, w, h);
      g.fill();
    },
    curl(g, r, col) {
      g.strokeStyle = col; g.lineWidth = r * 0.42; g.lineCap = 'round';
      g.beginPath(); g.moveTo(-r, 0); g.bezierCurveTo(-r * 0.5, -r, r * 0.1, r, r * 0.3, -r * 0.1); g.bezierCurveTo(r * 0.5, -r * 0.9, r, -r * 0.2, r, r * 0.2); g.stroke();
    },
    ornament(g, r, col) {
      g.fillStyle = '#C9A44A'; g.fillRect(-r * 0.22, -r * 1.18, r * 0.44, r * 0.26);
      const gr = g.createRadialGradient(-r * 0.35, -r * 0.4, r * 0.08, 0, 0, r);
      gr.addColorStop(0, '#ffffffd0'); gr.addColorStop(0.25, col); gr.addColorStop(1, '#00000080');
      g.beginPath(); g.arc(0, 0, r, 0, 6.283); g.fillStyle = gr; g.fill();
      g.shadowBlur = 0; g.strokeStyle = '#FFE08A90'; g.lineWidth = r * 0.07;
      g.beginPath(); g.ellipse(0, 0, r * 0.98, r * 0.28, 0, 0, 6.283); g.stroke();
    },
    star(g, r) {
      g.beginPath();
      for (let i = 0; i < 10; i++) { const a = -Math.PI / 2 + i * Math.PI / 5, rr = i % 2 ? r * 0.45 : r; g.lineTo(Math.cos(a) * rr, Math.sin(a) * rr); }
      g.closePath();
      const gr = g.createLinearGradient(-r, -r, r, r); gr.addColorStop(0, '#FFF3C4'); gr.addColorStop(0.5, '#F5C04A'); gr.addColorStop(1, '#B47A12');
      g.fillStyle = gr; g.fill();
    },
    sparkle(g, r, col) {
      g.fillStyle = col; g.beginPath(); g.moveTo(0, -r); g.quadraticCurveTo(0, 0, r, 0); g.quadraticCurveTo(0, 0, 0, r); g.quadraticCurveTo(0, 0, -r, 0); g.quadraticCurveTo(0, 0, 0, -r); g.fill();
    },
    emoji(g, r, ch) { g.font = `${r * 1.8}px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif`; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(ch, 0, r * 0.08); },
  };

  /* Наборы спрайтов разных размеров/размытий: [спрайт, ...]; выбираются частицами случайно. */
  function bank(n, mk) { return Array.from({ length: n }, (_, i) => mk(i)); }

  const TINT = {
    snow: ['radial-gradient(90% 50% at 50% 0%, rgba(80,140,255,.22), transparent 70%)', 'radial-gradient(90% 50% at 50% 0%, rgba(120,170,255,.18), transparent 70%)'],
    newyear: ['radial-gradient(80% 45% at 50% 0%, rgba(255,190,90,.20), transparent 70%), radial-gradient(70% 40% at 50% 100%, rgba(255,120,60,.12), transparent 70%)', 'radial-gradient(80% 45% at 50% 0%, rgba(255,200,120,.22), transparent 70%)'],
    halloween: ['radial-gradient(90% 45% at 50% 100%, rgba(255,110,0,.30), transparent 70%), radial-gradient(60% 35% at 100% 0%, rgba(140,60,255,.16), transparent 70%)', 'radial-gradient(90% 45% at 50% 100%, rgba(255,140,40,.22), transparent 70%)'],
    leaves: ['radial-gradient(90% 50% at 50% 100%, rgba(255,140,30,.24), transparent 70%)', 'radial-gradient(90% 50% at 50% 100%, rgba(255,170,60,.2), transparent 70%)'],
    hearts: ['radial-gradient(80% 50% at 50% 30%, rgba(255,40,90,.22), transparent 70%)', 'radial-gradient(80% 50% at 50% 30%, rgba(255,90,130,.16), transparent 70%)'],
    flowers: ['radial-gradient(80% 50% at 50% 0%, rgba(255,120,170,.22), transparent 70%)', 'radial-gradient(80% 50% at 50% 0%, rgba(255,150,190,.2), transparent 70%)'],
    confetti: ['radial-gradient(70% 40% at 0% 0%, rgba(120,80,255,.20), transparent 70%), radial-gradient(70% 40% at 100% 100%, rgba(255,60,120,.16), transparent 70%)', 'radial-gradient(70% 40% at 0% 0%, rgba(140,110,255,.16), transparent 70%)'],
  };
  const CONF = ['#FF375F', '#FF9F0A', '#FFD60A', '#30D158', '#0A84FF', '#BF5AF2', '#64D2FF'];

  /* Описание эффектов: для каждого слоя — сколько частиц и как их создавать. */
  function recipe(kind) {
    const dark = isDark();
    const snowCol = dark ? '#FFFFFF' : '#8FB3DA', snowGlow = dark ? 'rgba(160,200,255,.9)' : 'rgba(90,140,200,.6)';
    const R = {};
    if (kind === 'snow' || kind === 'newyear') {
      const far = bank(4, i => sprite(26 + i * 6, (g, r) => shapes.flake(g, r, snowCol), { blur: 3 + i * 1.5, color: snowCol }));
      const near = bank(4, i => sprite(10 + i * 4, (g, r) => shapes.flake(g, r, snowCol), { glow: 6, glowColor: snowGlow }));
      const dust = bank(3, i => sprite(4 + i * 2, (g, r) => shapes.dot(g, r, snowCol), { glow: 4, glowColor: snowGlow }));
      R.back = [{ n: 10, sp: far, a: [0.25, 0.5], vy: [0.25, 0.55], spin: 0.006 }, { n: 22, sp: dust, a: [0.3, 0.7], vy: [0.3, 0.8] }];
      R.front = [{ n: 16, sp: near, a: [0.55, 0.9], vy: [0.5, 1.1], spin: 0.012 }];
      if (kind === 'newyear') {
        const gold = bank(4, i => sprite(30 + i * 14, (g, r) => shapes.bokeh(g, r, i % 2 ? 'rgba(255,196,90,.55)' : 'rgba(255,120,70,.45)')));
        const spark = bank(2, i => sprite(10 + i * 6, (g, r) => shapes.sparkle(g, r, '#FFE7A3'), { glow: 8, glowColor: 'rgba(255,210,120,1)' }));
        R.back.unshift({ n: 12, sp: gold, a: [0.35, 0.8], vy: [-0.08, 0.12], vx: [-0.12, 0.12], twinkle: true });
        R.front.push({ n: 6, sp: spark, a: [0.4, 1], vy: [0.05, 0.2], twinkle: true });
        R.hang = [['#E0352B', 0.12, 110], ['#F5C04A', 0.86, 80], ['#2E7DFF', 0.68, 150]].map(([col, x, len]) => ({ sp: sprite(34, (g, r) => shapes.ornament(g, r, col), { glow: 10, glowColor: 'rgba(255,200,120,.45)' }), x, len }));
        R.hang.push({ sp: sprite(30, (g, r) => shapes.star(g, r), { glow: 14, glowColor: 'rgba(255,200,80,.9)' }), x: 0.34, len: 60 });
      }
    } else if (kind === 'halloween') {
      const pumpkins = bank(3, i => sprite(30 + i * 16, (g, r) => shapes.emoji(g, r, '🎃'), { glow: 18, glowColor: 'rgba(255,120,0,.95)' }));
      const embers = bank(3, i => sprite(4 + i * 2, (g, r) => shapes.dot(g, r, '#FFB347'), { glow: 8, glowColor: 'rgba(255,110,0,1)' }));
      const glow = bank(3, i => sprite(40 + i * 20, (g, r) => shapes.bokeh(g, r, 'rgba(255,120,20,.45)')));
      const bats = [sprite(22, (g, r) => shapes.emoji(g, r, '🦇')), sprite(30, (g, r) => shapes.emoji(g, r, '🦇'))];
      R.back = [{ n: 9, sp: glow, a: [0.3, 0.7], vy: [-0.15, -0.04], vx: [-0.1, 0.1], twinkle: true }, { n: 5, sp: pumpkins, a: [0.55, 0.85], vy: [-0.22, -0.08], spin: 0.004, rise: true }];
      R.front = [{ n: 18, sp: embers, a: [0.5, 1], vy: [-0.9, -0.35], vx: [-0.25, 0.25], rise: true, twinkle: true }, { n: 2, sp: bats, a: [0.45, 0.7], bat: true }];
    } else if (kind === 'leaves') {
      const big = bank(4, i => sprite(34 + i * 8, (g, r) => shapes.emoji(g, r, i % 2 ? '🍁' : '🍂'), { glow: 10, glowColor: 'rgba(255,140,20,.7)' }));
      const small = bank(4, i => sprite(20 + i * 5, (g, r) => shapes.emoji(g, r, i % 2 ? '🍁' : '🍂'), { glow: 6, glowColor: 'rgba(255,150,40,.6)' }));
      const glow = bank(3, i => sprite(36 + i * 18, (g, r) => shapes.bokeh(g, r, 'rgba(255,150,40,.4)')));
      R.back = [{ n: 8, sp: glow, a: [0.3, 0.6], vy: [0.05, 0.2], twinkle: true }, { n: 9, sp: big, a: [0.6, 0.9], vy: [0.35, 0.7], spin: 0.02, flip: true }];
      R.front = [{ n: 12, sp: small, a: [0.65, 0.95], vy: [0.6, 1.2], spin: 0.04, flip: true }];
    } else if (kind === 'hearts') {
      const far = bank(3, i => sprite(44 + i * 16, (g, r) => shapes.heart(g, r, '#FF2D55', '#FF9AB0', '#8A0024'), { blur: 3 + i * 2, color: 'rgba(255,45,85,1)' }));
      const near = bank(4, i => sprite(18 + i * 7, (g, r) => shapes.heart(g, r, '#FF2D55', '#FFB3C4', '#A3002B'), { glow: 12, glowColor: 'rgba(255,40,90,.9)' }));
      const glow = bank(3, i => sprite(36 + i * 16, (g, r) => shapes.bokeh(g, r, 'rgba(255,60,110,.45)')));
      R.back = [{ n: 10, sp: glow, a: [0.3, 0.7], vy: [-0.12, -0.03], twinkle: true }, { n: 7, sp: far, a: [0.35, 0.6], vy: [-0.35, -0.12], rise: true, spin: 0.004 }];
      R.front = [{ n: 9, sp: near, a: [0.5, 0.85], vy: [-0.7, -0.3], rise: true, spin: 0.008, pulse: true }];
    } else if (kind === 'flowers') {
      const blossoms = bank(3, i => sprite(36 + i * 12, (g, r) => shapes.blossom(g, r), { glow: 10, glowColor: 'rgba(255,140,180,.7)' }));
      const farPetals = bank(3, i => sprite(18 + i * 6, (g, r) => shapes.petal(g, r), { blur: 3 + i, color: 'rgba(255,170,200,1)' }));
      const petals = bank(4, i => sprite(14 + i * 5, (g, r) => shapes.petal(g, r), { glow: 5, glowColor: 'rgba(255,150,190,.8)' }));
      const glow = bank(3, i => sprite(36 + i * 16, (g, r) => shapes.bokeh(g, r, 'rgba(255,150,190,.4)')));
      R.back = [{ n: 8, sp: glow, a: [0.3, 0.6], vy: [0.03, 0.15], twinkle: true }, { n: 10, sp: farPetals, a: [0.35, 0.6], vy: [0.3, 0.6], spin: 0.02, flip: true }, { n: 4, sp: blossoms, a: [0.7, 0.95], vy: [0.18, 0.35], spin: 0.006 }];
      R.front = [{ n: 16, sp: petals, a: [0.6, 0.95], vy: [0.5, 1.0], spin: 0.04, flip: true }];
    } else if (kind === 'confetti') {
      const near = CONF.map(col => sprite(18, (g, r) => shapes.ribbon(g, r, col), { glow: 5, glowColor: col }));
      const curls = CONF.map(col => sprite(20, (g, r) => shapes.curl(g, r, col), { glow: 5, glowColor: col }));
      const far = CONF.map(col => sprite(28, (g, r) => shapes.ribbon(g, r, col), { blur: 4, color: col }));
      const glow = CONF.slice(0, 5).map(col => sprite(46, (g, r) => shapes.bokeh(g, r, hexA(col, 0.35))));
      R.back = [{ n: 8, sp: glow, a: [0.3, 0.6], vy: [0.03, 0.12], twinkle: true }, { n: 12, sp: far, a: [0.35, 0.6], vy: [0.6, 1.0], spin: 0.05, flip: true }];
      R.front = [{ n: 22, sp: near, a: [0.75, 1], vy: [1.0, 1.9], spin: 0.08, flip: true }, { n: 8, sp: curls, a: [0.7, 1], vy: [0.9, 1.6], spin: 0.05 }];
    }
    R.tint = TINT[kind] ? TINT[kind][dark ? 0 : 1] : '';
    return R;
  }
  function hexA(hex, a) { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; }

  let level = 1, layers = null, kind = 'none', raf = 0, last = 0, garland = null, tint = null, t0 = 0, R = null;

  function makeCanvas(cls) {
    const c = document.createElement('canvas'); c.className = cls; c.setAttribute('aria-hidden', 'true');
    document.body.append(c);
    return { c, g: c.getContext('2d'), parts: [] };
  }
  function sizeAll() {
    if (!layers) return;
    for (const L of Object.values(layers)) {
      L.c.width = innerWidth * DPR; L.c.height = innerHeight * DPR;
      L.c.style.width = innerWidth + 'px'; L.c.style.height = innerHeight + 'px';
      L.g.setTransform(DPR, 0, 0, DPR, 0, 0);
    }
  }
  const scaleN = n => Math.max(n < 1 ? 0 : 1, Math.round(n * Math.min(1.6, Math.max(0.6, innerWidth * innerHeight / (390 * 844)))));

  function spawn(spec, initial) {
    const w = innerWidth, h = innerHeight;
    const sp = pick(spec.sp);
    const p = { spec, sp, a: rnd(...spec.a), x: rnd(-20, w + 20), y: 0, vx: spec.vx ? rnd(...spec.vx) : rnd(-0.15, 0.15), vy: spec.vy ? rnd(...spec.vy) : 0.5,
      rot: rnd(0, 6.283), vr: spec.spin ? rnd(-spec.spin, spec.spin) : 0, fl: rnd(0, 6.283), vfl: spec.flip ? rnd(0.03, 0.08) : 0,
      sw: rnd(0, 6.283), swA: rnd(0.2, 0.7), ph: rnd(0, 6.283) };
    if (spec.bat) { p.x = Math.random() < 0.5 ? -40 : w + 40; p.y = rnd(h * 0.08, h * 0.5); p.vx = (p.x < 0 ? 1 : -1) * rnd(0.9, 1.5); p.vy = 0; p.wait = initial ? rnd(0, 200) : rnd(150, 500); return p; }
    if (initial) p.y = rnd(-20, h + 20);
    else p.y = p.vy < 0 ? h + sp.S : -sp.S;
    return p;
  }

  function step(L, dt, t) {
    const g = L.g, w = innerWidth, h = innerHeight;
    g.clearRect(0, 0, w, h);
    for (let i = 0; i < L.parts.length; i++) {
      const p = L.parts[i], s = p.spec;
      if (s.bat) {
        if (p.wait > 0) { p.wait -= dt; continue; }
        p.x += p.vx * dt; p.y += Math.sin((t / 260) + p.ph) * 0.9 * dt;
        if (p.x < -60 || p.x > w + 60) { L.parts[i] = spawn(s, false); continue; }
        const flap = 0.75 + Math.abs(Math.sin(t / 90 + p.ph)) * 0.35;
        g.save(); g.globalAlpha = p.a; g.translate(p.x, p.y); g.scale(p.vx < 0 ? -1 : 1, flap); g.drawImage(p.sp.c, -p.sp.S / 2, -p.sp.S / 2, p.sp.S, p.sp.S); g.restore();
        continue;
      }
      p.sw += 0.018 * dt; p.rot += p.vr * dt; p.fl += p.vfl * dt;
      p.x += (p.vx + Math.sin(p.sw) * p.swA * (s.rise ? 0.35 : 0.5)) * dt;
      p.y += p.vy * dt;
      const m = p.sp.S;
      if (p.y > h + m || p.y < -m * 1.5 || p.x < -m * 1.5 || p.x > w + m * 1.5) { L.parts[i] = spawn(s, false); continue; }
      let a = p.a;
      if (s.twinkle) a *= 0.6 + 0.4 * Math.sin(t / 700 + p.ph);
      g.save(); g.globalAlpha = Math.max(0, Math.min(1, a)); g.translate(p.x, p.y); g.rotate(p.rot);
      let sx = 1, sy = 1;
      if (s.flip) { sx = Math.cos(p.fl); sy = 0.85 + 0.15 * Math.sin(p.fl * 0.7); }
      if (s.pulse) { const k = 1 + 0.06 * Math.sin(t / 260 + p.ph); sx *= k; sy *= k; }
      g.scale(Math.abs(sx) < 0.08 ? 0.08 * Math.sign(sx || 1) : sx, sy);
      g.drawImage(p.sp.c, -m / 2, -m / 2, m, m);
      g.restore();
    }
  }

  function drawHang(g, t) {
    if (!R || !R.hang) return;
    const w = innerWidth, top = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tg-content-safe-area-inset-top')) || 0;
    for (const o of R.hang) {
      const ang = Math.sin(t / 1100 + o.x * 9) * 0.12, x0 = o.x * w, y0 = -4;
      const x = x0 + Math.sin(ang) * o.len, y = y0 + Math.cos(ang) * o.len + top * 0.2;
      g.save(); g.globalAlpha = 0.9; g.strokeStyle = 'rgba(255,220,150,.55)'; g.lineWidth = 1; g.beginPath(); g.moveTo(x0, y0); g.lineTo(x, y - o.sp.S * 0.32); g.stroke();
      g.translate(x, y); g.rotate(ang * 0.6); g.drawImage(o.sp.c, -o.sp.S / 2, -o.sp.S / 2, o.sp.S, o.sp.S); g.restore();
    }
  }

  function frame(t) {
    raf = requestAnimationFrame(frame);
    if (t - last < 30) return; // ~30 кадров/с — бережём батарею
    const dt = Math.min(3, (t - last) / 16.7); last = t;
    step(layers.back, dt, t); drawHang(layers.back.g, t);
    step(layers.front, dt, t);
  }

  function stop() {
    cancelAnimationFrame(raf); raf = 0;
    if (layers) { layers.back.c.remove(); layers.front.c.remove(); layers = null; }
    if (garland) { garland.remove(); garland = null; }
    if (tint) { tint.remove(); tint = null; }
    R = null; document.documentElement.classList.remove('fx-on');
  }

  function makeGarland() {
    const g = document.createElement('div');
    g.className = 'fx-garland'; g.setAttribute('aria-hidden', 'true');
    const n = Math.ceil(innerWidth / 26);
    const colors = ['#FF3B30', '#FFCC00', '#34C759', '#0A84FF', '#FF2D55'];
    for (let i = 0; i < n; i++) { const b = document.createElement('i'); b.style.background = colors[i % colors.length]; b.style.color = colors[i % colors.length]; b.style.animationDelay = (i % 5) * 0.3 + 's'; g.append(b); }
    document.body.append(g);
    return g;
  }

  const FX = {
    BACKGROUNDS, BG_NAMES, EFFECT_NAMES, seasonal,
    /* Применить фон: name из BACKGROUNDS или 'image' с url. */
    background(name, imageUrl) {
      const root = document.documentElement;
      let layer = document.getElementById('fx-bg');
      if (!name || name === 'none' || (name === 'image' && !imageUrl)) { if (layer) layer.remove(); root.classList.remove('has-bg'); return; }
      if (!layer) { layer = document.createElement('div'); layer.id = 'fx-bg'; layer.setAttribute('aria-hidden', 'true'); document.body.prepend(layer); }
      const dark = root.dataset.scheme ? root.dataset.scheme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      if (name === 'image') layer.style.background = `linear-gradient(${dark ? 'rgba(0,0,0,.45),rgba(0,0,0,.65)' : 'rgba(242,242,247,.35),rgba(242,242,247,.75)'}), center / cover no-repeat url("${imageUrl}")`;
      else layer.style.background = (BACKGROUNDS[name] || {})[dark ? 'dark' : 'light'] || '';
      root.classList.add('has-bg');
    },
    /* Запустить эффект; 'auto' — по календарю. */
    effect(name) {
      const resolved = name === 'auto' ? seasonal(new Date()) : (name || 'none');
      stop(); kind = resolved;
      if (resolved === 'none' || matchMedia('(prefers-reduced-motion: reduce)').matches) return resolved;
      R = recipe(resolved); document.documentElement.classList.add('fx-on');
      if (R.tint) { tint = document.createElement('div'); tint.className = 'fx-tint'; tint.setAttribute('aria-hidden', 'true'); tint.style.background = R.tint; document.body.append(tint); }
      layers = { back: makeCanvas('fx-canvas fx-back'), front: makeCanvas('fx-canvas fx-front') };
      sizeAll();
      for (const [name, L] of Object.entries(layers)) for (const spec of R[name] || []) for (let k = 0; k < scaleN(spec.n * level); k++) L.parts.push(spawn(spec, true));
      if (resolved === 'newyear') garland = makeGarland();
      last = performance.now(); t0 = last; raf = requestAnimationFrame(frame);
      return resolved;
    },
    stop,
    /* Интенсивность: 'low' | 'normal' | 'high' — меняет количество частиц; эффект перезапускается. */
    intensity(l) {
      const v = { low: 0.5, normal: 1, high: 1.5 }[l] || 1;
      if (v === level) return;
      level = v;
      if (layers) FX.effect(kind);
    },
    /* Приглушить эффекты (экраны оплаты и создания сервера). */
    calm(on) { document.documentElement.classList.toggle('fx-calm', !!on); },
  };
  addEventListener('resize', () => sizeAll());
  document.addEventListener('visibilitychange', () => { if (!layers) return; if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else if (!raf) { last = performance.now(); raf = requestAnimationFrame(frame); } });
  window.FX = FX;
})();
