/* Stonks Run - shared helpers for the Screen and History pages.
   Formatting, data loading, the watchlist, and small hand-built SVG charts
   (no chart library: keeps the pages fast and the look consistent). */
(function (global) {
  'use strict';

  const SR = {};
  const ET_TZ = 'America/New_York';
  SR.ET_TZ = ET_TZ;

  /* ---------------- formatting ---------------- */
  SR.esc = function (str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };
  SR.fmtPct = function (v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const d = digits ?? 2;
    const r = Number(Number(v).toFixed(d));   // so -0.004 prints as +0.00%, not -0.00%
    return (r >= 0 ? '+' : '') + (r === 0 ? 0 : r).toFixed(d) + '%';
  };
  SR.fmtNum = function (v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toLocaleString('en-US', { maximumFractionDigits: digits ?? 2 });
  };
  SR.fmtMoney = function (v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  SR.fmtVol = function (v) {
    if (v === null || v === undefined) return '—';
    if (v >= 1e12) return (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return String(Math.round(v));
  };
  SR.fmtCap = function (v) { return v == null ? '—' : '$' + SR.fmtVol(v); };
  SR.chgClass = function (v) { return v == null ? '' : (v >= 0 ? 'chg-pos' : 'chg-neg'); };

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const WDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  /* ISO 'YYYY-MM-DD' -> 'Oct 30' (calendar date, no timezone shifting) */
  SR.fmtDay = function (iso, withYear) {
    if (!iso) return '—';
    const [y, m, d] = iso.split('-').map(Number);
    return MONTHS[m - 1] + ' ' + d + (withYear ? ', ' + y : '');
  };
  SR.fmtDayLong = function (iso) {
    if (!iso) return '—';
    const [y, m, d] = iso.split('-').map(Number);
    const wd = WDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
    return wd + ', ' + MONTHS[m - 1] + ' ' + d + ', ' + y;
  };
  SR.fmtTimeET = function (isoTs) {
    const d = new Date(isoTs);
    if (isNaN(d)) return '';
    return d.toLocaleTimeString('en-US', { timeZone: ET_TZ, hour: 'numeric', minute: '2-digit' }) + ' ET';
  };

  /* ---------------- data ---------------- */
  SR.fetchJSON = async function (url) {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(url + ' ' + res.status);
    return res.json();
  };
  const memo = {};
  SR.loadOnce = function (url) {
    if (!memo[url]) memo[url] = SR.fetchJSON(url).catch(e => { delete memo[url]; throw e; });
    return memo[url];
  };
  SR.loadCompanies = () => SR.loadOnce('data/companies.json').then(d => d.c || {}).catch(() => ({}));
  SR.loadHistory = () => SR.loadOnce('data/history.json');
  SR.parseUniverse = function (u) {
    const F = u.fields;
    u.list = u.rows.map(row => {
      const o = {};
      F.forEach((k, i) => { o[k] = row[i]; });
      o.sector = o.s != null ? u.sectors[o.s] : '';
      o.industry = o.i != null ? u.industries[o.i] : '';
      return o;
    });
    u.byTicker = {};
    u.list.forEach(o => { u.byTicker[o.t] = o; });
    return u;
  };
  SR.loadUniverse = () => SR.loadOnce('data/universe.json').then(SR.parseUniverse);

  /* Minervini criteria in the scanner's fixed order (bit i = criterion i). */
  SR.CRITERIA = [
    'Price above rising 150-day & 200-day average',
    '150-day average above 200-day average',
    '200-day average trending up vs. 1 month ago',
    '50-day average above both 150-day & 200-day',
    'Price above 50-day average',
    'At least 30% above 52-week low',
    'Within 25% of 52-week high',
    'RS Rating >= 70',
  ];

  /* ---------------- watchlist (this browser only) ---------------- */
  const WL_KEY = 'sr.watchlist.v1';
  SR.watchlist = {
    get() {
      try { const v = JSON.parse(localStorage.getItem(WL_KEY) || '[]'); return Array.isArray(v) ? v : []; }
      catch (e) { return []; }
    },
    has(t) { return this.get().includes(t); },
    toggle(t) {
      const list = this.get();
      const i = list.indexOf(t);
      if (i >= 0) list.splice(i, 1); else list.unshift(t);
      try { localStorage.setItem(WL_KEY, JSON.stringify(list.slice(0, 50))); } catch (e) { /* storage unavailable */ }
      document.dispatchEvent(new CustomEvent('watchlist-change'));
      return i < 0;
    },
  };

  /* ---------------- chart tooltip ----------------
     One floating element shared by every chart. Built with textContent:
     ticker names and labels come from data files, never trusted as HTML. */
  let tipEl = null;
  function tip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'viz-tip';
      tipEl.setAttribute('role', 'status');
      tipEl.hidden = true;
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }
  /* rows: [{label, value, key?: cssColor, cls?}] ; title: string */
  SR.showTip = function (title, rows, clientX, clientY) {
    const el = tip();
    el.textContent = '';
    if (title) {
      const h = document.createElement('div');
      h.className = 'vt-title';
      h.textContent = title;
      el.appendChild(h);
    }
    rows.forEach(r => {
      const row = document.createElement('div');
      row.className = 'vt-row';
      if (r.key) {
        const k = document.createElement('span');
        k.className = 'vt-key';
        k.style.background = r.key;
        row.appendChild(k);
      }
      const v = document.createElement('span');
      v.className = 'vt-val' + (r.cls ? ' ' + r.cls : '');
      v.textContent = r.value;
      const l = document.createElement('span');
      l.className = 'vt-label';
      l.textContent = r.label;
      row.appendChild(v);
      row.appendChild(l);
      el.appendChild(row);
    });
    el.hidden = false;
    const pad = 14;
    const w = el.offsetWidth, h = el.offsetHeight;
    let x = clientX + pad, y = clientY - h - pad;
    if (x + w > window.innerWidth - 6) x = clientX - w - pad;
    if (x < 6) x = 6;
    if (y < 6) y = clientY + pad;
    el.style.left = x + 'px';
    el.style.top = y + 'px';
  };
  SR.hideTip = function () { if (tipEl) tipEl.hidden = true; };
  window.addEventListener('scroll', () => SR.hideTip(), { passive: true });

  /* Resolve 'var(--x)' to a concrete color: SVG presentation attributes do
     not accept CSS variables in every browser. */
  SR.color = function (c) {
    const m = /^var\((--[\w-]+)\)$/.exec(c || '');
    if (!m) return c;
    const v = getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim();
    return v || c;
  };

  /* ---------------- SVG helpers ---------------- */
  const NS = 'http://www.w3.org/2000/svg';
  function svgEl(name, attrs, parent) {
    const el = document.createElementNS(NS, name);
    for (const k in attrs) if (attrs[k] != null) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  }
  function svgText(parent, x, y, text, attrs) {
    const t = svgEl('text', Object.assign({ x, y }, attrs || {}), parent);
    t.textContent = text;
    return t;
  }
  SR.niceTicks = function (min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const step0 = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / mag;
    const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v / step) * step);
    return ticks;
  };
  /* Re-render on width change (charts are drawn at real pixel width so text never scales). */
  function responsive(el, draw) {
    let lastW = 0;
    const run = () => {
      const w = el.clientWidth;
      if (w && Math.abs(w - lastW) > 2) { lastW = w; draw(w); }
    };
    run();
    if (global.ResizeObserver) new ResizeObserver(run).observe(el);
    else window.addEventListener('resize', run);
  }

  /* ---------------- line chart with crosshair ----------------
     opts: { x: [labels], series: [{name, values, color, width}], height,
             yFmt(v), xFmt(label), tipTitle(i), tipExtra(i) -> rows,
             strip: [{color}] per x (optional status strip), ariaLabel } */
  SR.lineChart = function (el, opts) {
    responsive(el, (W) => {
      el.textContent = '';
      const H = opts.height || 220;
      const stripH = opts.strip ? 8 : 0;
      const M = { l: opts.padLeft || 46, r: 14, t: 12, b: 26 + (stripH ? stripH + 6 : 0) };
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      const n = opts.x.length;
      if (n < 2 || iw < 50) return;
      opts.series.forEach(s => { s.color = SR.color(s.color); });
      let lo = Infinity, hi = -Infinity;
      opts.series.forEach(s => s.values.forEach(v => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
      if (opts.yMin != null) lo = Math.min(lo, opts.yMin);
      if (!isFinite(lo)) return;
      const padv = (hi - lo) * 0.08 || 1;
      const ticks = SR.niceTicks(opts.yMin != null ? lo : lo - padv, hi + padv, 4);
      const y0 = ticks[0], y1 = ticks[ticks.length - 1];
      const X = i => M.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
      const Y = v => M.t + ih - ((v - y0) / (y1 - y0)) * ih;
      const yFmt = opts.yFmt || (v => SR.fmtNum(v, 0));
      const xFmt = opts.xFmt || (s => s);

      const svg = svgEl('svg', { width: W, height: H, class: 'viz-svg', role: 'img', tabindex: 0,
        'aria-label': opts.ariaLabel || 'Line chart' }, el);
      const grid = svgEl('g', { class: 'viz-grid' }, svg);
      ticks.forEach(t => {
        svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(t), y2: Y(t) }, grid);
        svgText(grid, M.l - 8, Y(t) + 4, yFmt(t), { class: 'viz-axis', 'text-anchor': 'end' });
      });
      const nLabels = Math.max(2, Math.min(6, Math.floor(iw / 90)));
      for (let k = 0; k < nLabels; k++) {
        const i = Math.round((k / (nLabels - 1)) * (n - 1));
        svgText(svg, X(i), H - 8, xFmt(opts.x[i]), { class: 'viz-axis',
          'text-anchor': k === 0 ? 'start' : k === nLabels - 1 ? 'end' : 'middle' });
      }
      if (opts.strip) {
        // one cell per x position, centred on its point, clipped to the plot
        const sy = M.t + ih + 6, cw = iw / (n - 1);
        opts.strip.forEach((s, i) => {
          if (!s || !s.color) return;
          const x0 = Math.max(M.l, X(i) - cw / 2), x1 = Math.min(M.l + iw, X(i) + cw / 2);
          svgEl('rect', { x: x0, y: sy, width: Math.max(1, x1 - x0 - (cw > 5 ? 1 : 0)), height: stripH, fill: SR.color(s.color) }, svg);
        });
      }
      opts.series.forEach(s => {
        if (s.area) {
          let d = '', started = false, firstX = 0, lastX = 0;
          s.values.forEach((v, i) => {
            if (v == null) return;
            if (!started) { firstX = X(i); d += 'M' + X(i) + ',' + Y(v); started = true; }
            else d += 'L' + X(i) + ',' + Y(v);
            lastX = X(i);
          });
          if (started) svgEl('path', { d: d + 'L' + lastX + ',' + (M.t + ih) + 'L' + firstX + ',' + (M.t + ih) + 'Z',
            fill: s.color, opacity: 0.1 }, svg);
        }
        let d = '';
        let pen = false;
        s.values.forEach((v, i) => {
          if (v == null) { pen = false; return; }
          d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1);
          pen = true;
        });
        svgEl('path', { d, fill: 'none', stroke: s.color, 'stroke-width': s.width || 2,
          'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      });
      // end dot on the first series
      const s0 = opts.series[0];
      for (let i = n - 1; i >= 0; i--) {
        if (s0.values[i] != null) {
          svgEl('circle', { cx: X(i), cy: Y(s0.values[i]), r: 4, fill: s0.color, class: 'viz-ring' }, svg);
          break;
        }
      }
      // crosshair
      const cross = svgEl('line', { y1: M.t, y2: M.t + ih, class: 'viz-cross', visibility: 'hidden' }, svg);
      const dots = opts.series.map(s => svgEl('circle', { r: 4, fill: s.color, class: 'viz-ring', visibility: 'hidden' }, svg));
      let cur = -1;
      const show = (i, cx, cy) => {
        cur = i;
        cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i));
        cross.setAttribute('visibility', 'visible');
        opts.series.forEach((s, k) => {
          const v = s.values[i];
          if (v == null) { dots[k].setAttribute('visibility', 'hidden'); return; }
          dots[k].setAttribute('cx', X(i)); dots[k].setAttribute('cy', Y(v));
          dots[k].setAttribute('visibility', 'visible');
        });
        const rows = opts.series.map(s => ({ label: s.name, value: s.values[i] == null ? '—' : (s.tipFmt || yFmt)(s.values[i]), key: s.color }));
        const extra = opts.tipExtra ? opts.tipExtra(i) : [];
        const r = svg.getBoundingClientRect();
        SR.showTip(opts.tipTitle ? opts.tipTitle(i) : xFmt(opts.x[i]), rows.concat(extra),
          cx != null ? cx : r.left + X(i), cy != null ? cy : r.top + M.t + 10);
      };
      const hide = () => {
        cross.setAttribute('visibility', 'hidden');
        dots.forEach(d => d.setAttribute('visibility', 'hidden'));
        SR.hideTip();
      };
      const hit = svgEl('rect', { x: M.l - 6, y: M.t, width: iw + 12, height: ih, fill: 'transparent' }, svg);
      hit.addEventListener('pointermove', e => {
        const r = svg.getBoundingClientRect();
        const i = Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left - M.l) / iw) * (n - 1))));
        show(i, e.clientX, e.clientY);
      });
      hit.addEventListener('pointerleave', hide);
      svg.addEventListener('keydown', e => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        const i = cur < 0 ? n - 1 : Math.max(0, Math.min(n - 1, cur + (e.key === 'ArrowRight' ? 1 : -1)));
        show(i);
      });
      svg.addEventListener('blur', hide);
    });
  };

  /* ---------------- grouped column chart ----------------
     opts: { cats: [labels], series: [{name, values, color}], height, yFmt, valueFmt, tipRows(ci) } */
  SR.columnChart = function (el, opts) {
    responsive(el, (W) => {
      el.textContent = '';
      const H = opts.height || 230;
      const M = { l: 46, r: 10, t: 22, b: 28 };
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      let lo = 0, hi = 0;
      opts.series.forEach(s => s.values.forEach(v => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
      if (lo === 0 && hi === 0) hi = 1;
      const ticks = SR.niceTicks(lo, hi, 4);
      const y0 = ticks[0], y1 = ticks[ticks.length - 1];
      const Y = v => M.t + ih - ((v - y0) / (y1 - y0)) * ih;
      const yFmt = opts.yFmt || (v => v + '%');
      const valueFmt = opts.valueFmt || (v => SR.fmtPct(v, 1));
      const svg = svgEl('svg', { width: W, height: H, class: 'viz-svg', role: 'img', 'aria-label': opts.ariaLabel || 'Column chart' }, el);
      const grid = svgEl('g', { class: 'viz-grid' }, svg);
      ticks.forEach(t => {
        svgEl('line', { x1: M.l, x2: W - M.r, y1: Y(t), y2: Y(t), class: t === 0 ? 'viz-zero' : null }, grid);
        svgText(grid, M.l - 8, Y(t) + 4, yFmt(t), { class: 'viz-axis', 'text-anchor': 'end' });
      });
      opts.series.forEach(s => { s.color = SR.color(s.color); });
      const nc = opts.cats.length, ns = opts.series.length;
      const band = iw / nc;
      const bw = Math.min(24, (band * 0.6 - (ns - 1) * 2) / ns);
      const groupW = ns * bw + (ns - 1) * 2;
      const useShort = opts.shortCats && band < 96;
      opts.cats.forEach((c, ci) => {
        const gx = M.l + ci * band + (band - groupW) / 2;
        svgText(svg, M.l + ci * band + band / 2, H - 8, useShort ? opts.shortCats[ci] : c, { class: 'viz-axis', 'text-anchor': 'middle' });
        const g = svgEl('g', { class: 'viz-col-group', tabindex: 0, role: 'img',
          'aria-label': c + ': ' + opts.series.map(s => s.name + ' ' + (s.values[ci] == null ? 'n/a' : valueFmt(s.values[ci]))).join(', ') }, svg);
        svgEl('rect', { x: M.l + ci * band, y: M.t, width: band, height: ih, fill: 'transparent' }, g);
        opts.series.forEach((s, si) => {
          const v = s.values[ci];
          if (v == null) return;
          const x = gx + si * (bw + 2);
          const yv = Y(v), yz = Y(0);
          const top = Math.min(yv, yz), h = Math.max(1, Math.abs(yz - yv));
          const r = Math.min(4, h, bw / 2);
          // rounded data-end, square at the baseline
          let d;
          if (v >= 0) d = `M${x},${top + h}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${top + h}Z`;
          else d = `M${x},${top}V${top + h - r}Q${x},${top + h} ${x + r},${top + h}H${x + bw - r}Q${x + bw},${top + h} ${x + bw},${top + h - r}V${top}Z`;
          svgEl('path', { d, fill: s.color, class: 'viz-bar' }, g);
        });
        // Direct-label the lead series only (the table and tooltip carry the
        // rest), placed clear of every bar in the group so labels never collide.
        const lead = opts.series[0].values[ci];
        if (lead != null) {
          const vals = opts.series.map(s => s.values[ci]).filter(v => v != null);
          const hiV = Math.max(0, ...vals), loV = Math.min(0, ...vals);
          const ly = lead >= 0 ? Y(hiV) - 7 : Y(loV) + 14;
          svgText(g, gx + bw / 2, ly, valueFmt(lead), { class: 'viz-val viz-val-lead', 'text-anchor': 'middle' });
        }
        const showT = (cx, cy) => SR.showTip(opts.tipTitle ? opts.tipTitle(ci) : c,
          opts.tipRows ? opts.tipRows(ci) : opts.series.map(s => ({ label: s.name, value: s.values[ci] == null ? '—' : valueFmt(s.values[ci]), key: s.color })), cx, cy);
        g.addEventListener('pointermove', e => showT(e.clientX, e.clientY));
        g.addEventListener('pointerleave', SR.hideTip);
        g.addEventListener('focus', () => { const r = g.getBoundingClientRect(); showT(r.left + r.width / 2, r.top); });
        g.addEventListener('blur', SR.hideTip);
      });
    });
  };

  /* ---------------- heatmap (sequential, one hue) ----------------
     opts: { rows: [labels], cols: [labels], value(ri, ci), max, colFmt, tip(ri, ci) -> {title, rows} } */
  SR.HEAT = ['#0f2a2e', '#124a4b', '#176e6b', '#1d948e', '#26b8af', '#3fe0d2'];
  SR.heatmap = function (el, opts) {
    responsive(el, (W) => {
      el.textContent = '';
      const labelW = Math.min(150, Math.max(96, W * 0.2));
      const M = { l: labelW, r: 6, t: 4, b: 24 };
      const nc = opts.cols.length, nr = opts.rows.length;
      const cw = Math.max(3, (W - M.l - M.r) / nc);
      const ch = 20;
      const H = M.t + nr * ch + M.b;
      const svg = svgEl('svg', { width: W, height: H, class: 'viz-svg', role: 'img', 'aria-label': opts.ariaLabel || 'Heatmap' }, el);
      const max = opts.max || 1;
      opts.rows.forEach((rl, ri) => {
        svgText(svg, M.l - 8, M.t + ri * ch + ch / 2 + 4, rl, { class: 'viz-axis viz-rowlabel', 'text-anchor': 'end' });
        for (let ci = 0; ci < nc; ci++) {
          const v = opts.value(ri, ci);
          const x = M.l + ci * cw, y = M.t + ri * ch;
          let fill = 'rgba(255,255,255,0.025)';
          if (v > 0) fill = SR.HEAT[Math.min(SR.HEAT.length - 1, Math.ceil((v / max) * SR.HEAT.length) - 1)];
          const r = svgEl('rect', { x: x + 1, y: y + 1, width: Math.max(1, cw - 2), height: ch - 2, rx: 2, fill, class: 'viz-cell' }, svg);
          r.addEventListener('pointermove', e => { const t = opts.tip(ri, ci); SR.showTip(t.title, t.rows, e.clientX, e.clientY); });
          r.addEventListener('pointerleave', SR.hideTip);
          if (opts.onClick) { r.style.cursor = 'pointer'; r.addEventListener('click', () => opts.onClick(ri, ci)); }
        }
      });
      const nLabels = Math.max(2, Math.min(7, Math.floor((W - M.l) / 80)));
      for (let k = 0; k < nLabels; k++) {
        const ci = Math.round((k / (nLabels - 1)) * (nc - 1));
        svgText(svg, M.l + ci * cw + cw / 2, H - 6, (opts.colFmt || (s => s))(opts.cols[ci]),
          { class: 'viz-axis', 'text-anchor': k === 0 ? 'start' : k === nLabels - 1 ? 'end' : 'middle' });
      }
    });
  };

  /* ---------------- tiny sparkline (stat tiles; not interactive) ---------------- */
  SR.sparkline = function (values, opts) {
    opts = opts || {};
    const W = opts.width || 120, H = opts.height || 28, pad = 4;
    const vals = values.filter(v => v != null);
    if (vals.length < 2) return '';
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const X = i => pad + (i / (values.length - 1)) * (W - pad * 2);
    const Y = v => H - pad - ((v - lo) / ((hi - lo) || 1)) * (H - pad * 2);
    let d = '', pen = false, lastI = -1;
    values.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ',' + Y(v).toFixed(1);
      pen = true; lastI = i;
    });
    return '<svg class="spark" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true">' +
      '<path d="' + d + '" fill="none" stroke="var(--text-faint)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + X(lastI).toFixed(1) + '" cy="' + Y(values[lastI]).toFixed(1) + '" r="3" fill="var(--teal)"/></svg>';
  };

  /* ---------------- history-derived helpers (used by both pages) ---------------- */
  /* Per-ticker record across every session in history.json. */
  SR.tickerRecords = function (history) {
    const days = history.days || [];
    const recs = {};
    days.forEach((d, di) => {
      (d.picks || []).forEach(p => {
        let r = recs[p.t];
        if (!r) r = recs[p.t] = { t: p.t, appearances: [], first: d.date, firstIdx: di, bestRank: 99, streak: 0 };
        r.appearances.push({ date: d.date, di, sec: p.sec, r: p.r, p: p.p, c: p.c, rs: p.rs, tn: p.tn, sc: p.sc, q: p.q, f: p.f || null, spy: d.spy || null });
        r.last = d.date; r.lastIdx = di;
        r.bestRank = Math.min(r.bestRank, p.r);
      });
    });
    const lastIdx = days.length - 1;
    Object.values(recs).forEach(r => {
      r.sessions = r.appearances.length;
      r.onList = r.lastIdx === lastIdx;
      let s = 0;
      for (let i = r.appearances.length - 1; i >= 0; i--) {
        if (r.appearances[i].di === lastIdx - s) s++; else break;
      }
      r.streak = r.onList ? s : 0;
      const first = r.appearances[0];
      r.sinceFirst = first.f && first.f.td != null ? first.f.td : null;
      r.spySinceFirst = first.spy && first.spy.td != null ? first.spy.td : null;
    });
    return recs;
  };

  global.SR = SR;
})(window);
