'use strict';
/*
 * PC Remote — LAN control panel (single-page client).
 * No external dependencies and no inline handlers: all events are delegated.
 */
(function () {
  // ------------------------------------------------------------------ state
  const state = {
    csrf: '',
    route: 'overview',
    panel: null,
    telegram: 'unknown',
    theme: 'dark',
    viewToken: 0,
    timers: [],
    refresh: null,
    overview: null,
    files: { path: null, kind: 'drives', data: null, query: '', busy: false },
    chart: { metric: 'cpu', range: '5m', device: '' },
    settings: null,
    settingsTab: 'apps',
    processes: { sort: 'cpu', filter: '' },
    scheduler: null,
    speedtestPending: false,
  };

  const ROUTES = {
    overview: { title: 'Обзор', sub: 'Состояние компьютера' },
    files: { title: 'Файлы', sub: 'Диски и папки' },
    processes: { title: 'Процессы', sub: 'Запущенные программы' },
    apps: { title: 'Приложения', sub: 'Запуск и профили' },
    power: { title: 'Питание и медиа', sub: 'Управление компьютером' },
    network: { title: 'Сеть', sub: 'Интерфейсы и диагностика' },
    scheduler: { title: 'Планировщик', sub: 'Отложенные действия' },
    settings: { title: 'Настройки', sub: 'Параметры панели и бота' },
  };

  // ------------------------------------------------------------------ utils
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function isNum(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function num(value, digits) {
    if (!isNum(value)) return value == null ? '—' : String(value);
    const d = digits == null ? 0 : digits;
    return value.toFixed(d).replace('.', ',');
  }

  function fmtBytes(bytes, digits) {
    if (!isNum(bytes)) return '—';
    const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return num(value, index === 0 ? 0 : digits == null ? 1 : digits) + ' ' + units[index];
  }

  function fmtUptime(seconds) {
    if (!isNum(seconds) || seconds < 0) return '—';
    const total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const parts = [];
    if (days) parts.push(days + ' д');
    if (hours) parts.push(hours + ' ч');
    if (!days) parts.push(minutes + ' мин');
    return parts.join(' ') || 'меньше минуты';
  }

  function fmtTime(seconds) {
    if (!isNum(seconds)) return '—';
    const date = new Date(seconds * 1000);
    return date.toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function levelPercent(value) {
    return isNum(value) ? clamp(value, 0, 100) : 0;
  }

  function setHidden(node, hidden) {
    if (!node) return;
    if (hidden) node.setAttribute('hidden', '');
    else node.removeAttribute('hidden');
  }

  function onTimer(fn, ms) {
    const id = setInterval(fn, ms);
    state.timers.push(id);
    return id;
  }

  function clearTimers() {
    state.timers.forEach((id) => clearInterval(id));
    state.timers = [];
  }

  // ------------------------------------------------------------------ icons
  const ICONS = {
    overview:
      '<rect x="3" y="3" width="7.5" height="7.5" rx="1.8"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.8"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.8"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.8"/>',
    files:
      '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5Z"/><path d="M3 10h18"/>',
    processes:
      '<path d="M3 12h4l2.5-6 4 12 2.5-6h5"/>',
    apps:
      '<path d="M12 3 5 7v10l7 4 7-4V7Z"/><path d="M12 21V12"/><path d="m5 7 7 5 7-5"/>',
    power:
      '<path d="M12 3v9"/><path d="M7.2 6.3a8 8 0 1 0 9.6 0"/>',
    network:
      '<path d="M4.5 9.5a11 11 0 0 1 15 0"/><path d="M7.5 13a7 7 0 0 1 9 0"/><path d="M10.5 16.5a3 3 0 0 1 3 0"/><circle cx="12" cy="20" r="1"/>',
    scheduler:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3.2 2"/>',
    settings:
      '<path d="M4 7h10"/><circle cx="17" cy="7" r="2.4"/><path d="M20 12h-9"/><circle cx="8" cy="12" r="2.4"/><path d="M4 17h7"/><circle cx="14" cy="17" r="2.4"/>',
    x:
      '<path d="M6 6l12 12M18 6 6 18"/>',
    menu:
      '<path d="M4 7h16M4 12h16M4 17h16"/>',
    refresh:
      '<path d="M20 11a8 8 0 1 0-2.3 6.3"/><path d="M20 5v6h-6"/>',
    sun:
      '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
    moon:
      '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z"/>',
    collapse:
      '<path d="M14 6l-6 6 6 6"/><path d="M20 4v16"/>',
    logout:
      '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
    chevron:
      '<path d="M9 6l6 6-6 6"/>',
    up:
      '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
    search:
      '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    upload:
      '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    folder:
      '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5Z"/>',
    file:
      '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4"/>',
    download:
      '<path d="M12 4v12"/><path d="m7 11 5 5 5-5"/><path d="M4 20h16"/>',
    trash:
      '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v5M14 11v5"/>',
    dots:
      '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    plus:
      '<path d="M12 5v14M5 12h14"/>',
    play:
      '<path d="M7 4l13 8-13 8Z"/>',
    stop:
      '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    check:
      '<path d="m4 12 5 5L20 6"/>',
    alert:
      '<path d="M12 3 2.5 20h19Z"/><path d="M12 9v5"/><circle cx="12" cy="17.3" r=".9"/>',
    info:
      '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.8" r=".9"/>',
    clipboard:
      '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1H9Z"/>',
    monitor:
      '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    clock:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3.2 2"/>',
    garbage:
      '<path d="M5 7h14"/><path d="M10 7V4h4v3"/><path d="M7 7l1 13h8l1-13"/>',
    disk:
      '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    edit:
      '<path d="M4 20h4l10-10a2.8 2.8 0 1 0-4-4L4 16Z"/><path d="m13.5 6.5 4 4"/>',
    copy:
      '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    move:
      '<path d="M12 3v18M3 12h18"/><path d="m8 7 4-4 4 4M8 17l4 4 4-4M7 8l-4 4 4 4M17 8l4 4-4 4"/>',
    wifi:
      '<path d="M2.5 8.5a15 15 0 0 1 19 0"/><path d="M6 12a10 10 0 0 1 12 0"/><path d="M9.5 15.5a5 5 0 0 1 5 0"/><circle cx="12" cy="19.5" r="1"/>',
    shield:
      '<path d="M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6Z"/><path d="m9 12 2 2 4-4"/>',
    lock:
      '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    volume:
      '<path d="M4 9v6h4l5 4V5L8 9Z"/><path d="M16.5 9a4 4 0 0 1 0 6"/>',
    mute:
      '<path d="M4 9v6h4l5 4V5L8 9Z"/><path d="m16 9 5 6M21 9l-5 6"/>',
  };

  function icon(name, cls) {
    const body = ICONS[name] || ICONS.info;
    return '<svg class="' + (cls || '') + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
  }

  // -------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.data = data || {};
    }
  }

  function describeError(error) {
    if (error instanceof ApiError) return error.message;
    if (error && error.message) return error.message;
    return 'Неизвестная ошибка';
  }

  async function request(path, options) {
    const opts = options || {};
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({}, opts.headers || {});
    let body = opts.body;
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json; charset=utf-8';
      body = JSON.stringify(opts.json);
    }
    if (method !== 'GET' && method !== 'HEAD' && state.csrf) {
      headers['X-CSRF-Token'] = state.csrf;
    }

    let response;
    try {
      response = await fetch(path, {
        method: method,
        headers: headers,
        body: body,
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (error) {
      throw new ApiError('Нет связи с панелью', 0, {});
    }

    const type = response.headers.get('Content-Type') || '';
    let data = null;
    if (type.indexOf('application/json') >= 0) {
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }
    }

    if (!response.ok) {
      const message = (data && (data.error || data.message)) || 'Ошибка ' + response.status;
      const apiError = new ApiError(message, response.status, data || {});
      if (response.status === 401 && !opts.noAuth) handleLostSession();
      if (response.status === 429 && data && data.retry_after) {
        apiError.message = message + '. Повторите через ' + data.retry_after + ' с';
      }
      throw apiError;
    }
    return data;
  }

  async function api(path, options) {
    return request(path, options);
  }

  // ----------------------------------------------------------------- toasts
  function toast(message, kind, ms) {
    const host = $('#toasts');
    if (!host) return;
    const node = document.createElement('div');
    node.className = 'toast' + (kind ? ' ' + kind : '');
    node.innerHTML = '<span>' + esc(message) + '</span>';
    host.appendChild(node);
    setTimeout(function () {
      node.classList.add('out');
      setTimeout(function () { node.remove(); }, 220);
    }, ms || 3400);
  }

  // ----------------------------------------------------------------- modals
  let openModals = 0;

  function closeModals() {
    const root = $('#modal-root');
    if (root) root.innerHTML = '';
    openModals = 0;
  }

  function openModal(opts) {
    const root = $('#modal-root');
    const wrap = document.createElement('div');
    wrap.className = 'modal-wrap';
    wrap.innerHTML =
      '<div class="modal-backdrop" data-act="modal-close"></div>' +
      '<div class="modal' + (opts.wide ? ' wide' : '') + '" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><h3>' + esc(opts.title || '') + '</h3>' +
      '<button type="button" class="iconbtn sm" data-act="modal-close" aria-label="Закрыть">' + icon('x') + '</button></div>' +
      '<div class="modal-body">' + (opts.body || '') + '</div>' +
      (opts.footer ? '<div class="modal-foot">' + opts.footer + '</div>' : '') +
      '</div>';
    root.appendChild(wrap);
    openModals += 1;
    const focusTarget = wrap.querySelector('[data-autofocus]');
    if (focusTarget) setTimeout(function () { focusTarget.focus(); }, 30);
    return wrap;
  }

  function confirmModal(opts) {
    return new Promise(function (resolve) {
      const wrap = openModal({
        title: opts.title || 'Подтверждение',
        body: '<p class="note">' + esc(opts.text || '') + '</p>',
        footer:
          '<button type="button" class="btn ghost" data-confirm="no">' + esc(opts.cancelText || 'Отмена') + '</button>' +
          '<button type="button" class="btn ' + (opts.danger ? 'danger' : 'primary') + '" data-confirm="yes">' +
          esc(opts.okText || 'Подтвердить') + '</button>',
      });
      function done(value) {
        wrap.remove();
        openModals = Math.max(0, openModals - 1);
        resolve(value);
      }
      wrap.querySelector('[data-confirm="yes"]').addEventListener('click', function () { done(true); });
      wrap.querySelector('[data-confirm="no"]').addEventListener('click', function () { done(false); });
      wrap.querySelector('.modal-backdrop').addEventListener('click', function () { done(false); });
    });
  }

  function promptModal(opts) {
    return new Promise(function (resolve) {
      const isTextarea = !!opts.multiline;
      const input = isTextarea
        ? '<textarea class="textarea" data-prompt rows="' + (opts.rows || 5) + '" data-autofocus>' + esc(opts.value || '') + '</textarea>'
        : '<input class="input" data-prompt type="text" data-autofocus value="' + esc(opts.value || '') + '" placeholder="' + esc(opts.placeholder || '') + '">';
      const wrap = openModal({
        title: opts.title || 'Введите значение',
        body:
          (opts.label ? '<label class="field"><span>' + esc(opts.label) + '</span>' : '<label class="field">') +
          input + '</label>' +
          (opts.hint ? '<p class="note">' + esc(opts.hint) + '</p>' : ''),
        footer:
          '<button type="button" class="btn ghost" data-confirm="no">Отмена</button>' +
          '<button type="button" class="btn primary" data-confirm="yes">' + esc(opts.okText || 'Сохранить') + '</button>',
      });
      const field = wrap.querySelector('[data-prompt]');
      function done(value) {
        wrap.remove();
        openModals = Math.max(0, openModals - 1);
        resolve(value);
      }
      wrap.querySelector('[data-confirm="yes"]').addEventListener('click', function () { done(field.value); });
      wrap.querySelector('[data-confirm="no"]').addEventListener('click', function () { done(null); });
      wrap.querySelector('.modal-backdrop').addEventListener('click', function () { done(null); });
      if (field) {
        field.addEventListener('keydown', function (event) {
          if (event.key === 'Enter' && !isTextarea) {
            event.preventDefault();
            done(field.value);
          }
        });
      }
    });
  }

  document.addEventListener('click', function (event) {
    const closer = event.target.closest('[data-act="modal-close"]');
    if (closer) {
      const wrap = closer.closest('.modal-wrap');
      if (wrap) wrap.remove();
      openModals = Math.max(0, openModals - 1);
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeModals();
    }
  });

// --------------------------------------------------------------- registries
  const ACT = {};
  const FORM = {};
  const VIEWS = {};

  function action(name, fn) {
    ACT[name] = fn;
  }

  document.addEventListener('click', function (event) {
    const node = event.target.closest('[data-act]');
    if (!node) return;
    const name = node.getAttribute('data-act');
    if (name === 'modal-close' || !ACT[name]) return;
    event.preventDefault();
    try {
      ACT[name](node, event);
    } catch (error) {
      toast(describeError(error), 'error');
    }
  });

  document.addEventListener('submit', function (event) {
    const form = event.target.closest('form[data-form]');
    if (!form) return;
    const handler = FORM[form.getAttribute('data-form')];
    if (!handler) return;
    event.preventDefault();
    try {
      handler(form, event);
    } catch (error) {
      toast(describeError(error), 'error');
    }
  });

  // ----------------------------------------------------------------- theme
  const THEME_KEY = 'pcpanel-theme';

  function applyTheme() {
    let theme = 'dark';
    try {
      theme = localStorage.getItem(THEME_KEY) || 'dark';
    } catch (error) {
      theme = 'dark';
    }
    if (theme !== 'light' && theme !== 'dark') theme = 'dark';
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#eef1f7' : '#0f1115');
  }

  function toggleTheme() {
    state.theme = state.theme === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem(THEME_KEY, state.theme);
    } catch (error) {
      /* storage may be unavailable */
    }
    applyTheme();
    toast(state.theme === 'dark' ? 'Тёмная тема' : 'Светлая тема', 'ok', 1600);
  }

  // -------------------------------------------------------------- navigation
  const NAV_GROUPS = [
    { label: 'Панель', items: ['overview'] },
    { label: 'Система', items: ['files', 'processes', 'apps', 'power'] },
    { label: 'Сеть', items: ['network'] },
    { label: 'Автоматизация', items: ['scheduler'] },
    { label: 'Настройки', items: ['settings'] },
  ];
  const BOTTOM_ITEMS = ['overview', 'files', 'processes', 'network'];

  function brandMark() {
    return '<div class="brand-mark">' + icon('monitor') + '</div>';
  }

  function buildSidebar() {
    const sidebar = $('#sidebar');
    if (!sidebar || sidebar.getAttribute('data-built') === '1') return;
    sidebar.setAttribute('data-built', '1');
    const nav = NAV_GROUPS.map(function (group) {
      return (
        '<div class="nav-label">' + esc(group.label) + '</div>' +
        group.items.map(function (key) {
          return (
            '<button type="button" class="nav-item" data-nav="' + key + '" title="' + esc(ROUTES[key].title) + '">' +
            icon(key) + '<span class="label">' + esc(ROUTES[key].title) + '</span>' +
            (key === 'overview' ? '<span class="nav-count" id="nav-alerts" hidden></span>' : '') +
            '</button>'
          );
        }).join('')
      );
    }).join('');

    sidebar.innerHTML =
      '<div class="brand">' + brandMark() +
      '<div class="brand-text"><strong>PC Remote</strong><span id="brand-host">Локальная панель</span></div></div>' +
      '<nav class="nav" aria-label="Разделы">' + nav + '</nav>' +
      '<div class="sidebar-foot">' +
      '<button type="button" class="side-action" data-act="theme-toggle">' +
      icon('sun', 'icon-sun') + icon('moon', 'icon-moon') +
      '<span class="label">Сменить тему</span></button>' +
      '<button type="button" class="side-action" data-act="logout">' + icon('logout') +
      '<span class="label">Выйти</span></button>' +
      '<button type="button" class="collapse-btn" id="collapse-btn" data-act="nav-collapse" title="Свернуть меню">' +
      icon('collapse') + '<span class="label">Свернуть</span></button>' +
      '</div>';
  }

  function buildBottomNav() {
    const nav = $('#bottomnav');
    if (!nav || nav.getAttribute('data-built') === '1') return;
    nav.setAttribute('data-built', '1');
    nav.innerHTML =
      '<div class="bottomnav-inner">' +
      BOTTOM_ITEMS.map(function (key) {
        return (
          '<button type="button" class="bn-item" data-nav="' + key + '">' +
          icon(key) + '<span>' + esc(ROUTES[key].title) + '</span></button>'
        );
      }).join('') +
      '<button type="button" class="bn-item" data-act="sheet-open" id="bn-more">' +
      icon('dots') + '<span>Ещё</span></button>' +
      '</div>';
  }

  function buildSheet() {
    const body = $('#sheet-body');
    if (!body || body.getAttribute('data-built') === '1') return;
    body.setAttribute('data-built', '1');
    const extra = ['apps', 'power', 'scheduler', 'settings'].map(function (key) {
      return (
        '<button type="button" class="sheet-item" data-nav="' + key + '">' +
        icon(key) + '<span>' + esc(ROUTES[key].title) + '</span></button>'
      );
    }).join('');
    body.innerHTML =
      extra +
      '<div class="sheet-sep"></div>' +
      '<button type="button" class="sheet-item" data-act="theme-toggle">' + icon('moon') +
      '<span>Сменить тему</span></button>' +
      '<button type="button" class="sheet-item" data-act="logout">' + icon('logout') +
      '<span>Выйти</span></button>';
  }

  function openDrawer() {
    document.body.classList.add('drawer-open');
    setHidden($('#scrim'), false);
  }

  function closeDrawer() {
    document.body.classList.remove('drawer-open');
    setHidden($('#scrim'), true);
  }

  function openSheet() {
    setHidden($('#sheet'), false);
    document.body.classList.add('sheet-open');
  }

  function closeSheet() {
    setHidden($('#sheet'), true);
    document.body.classList.remove('sheet-open');
  }

  function toggleCollapse() {
    const collapsed = !document.body.classList.contains('nav-collapsed');
    document.body.classList.toggle('nav-collapsed', collapsed);
    try {
      localStorage.setItem('pcpanel-nav-collapsed', collapsed ? '1' : '0');
    } catch (error) {
      /* storage may be unavailable */
    }
  }

  function restoreCollapse() {
    let collapsed = false;
    try {
      collapsed = localStorage.getItem('pcpanel-nav-collapsed') === '1';
    } catch (error) {
      collapsed = false;
    }
    document.body.classList.toggle('nav-collapsed', collapsed);
  }

  function setActiveNav(route) {
    $$('[data-nav]').forEach(function (node) {
      node.classList.toggle('active', node.getAttribute('data-nav') === route);
    });
  }

  function updateBadges(payload) {
    if (payload && payload.telegram !== undefined) state.telegram = payload.telegram;
    if (payload && payload.panel) state.panel = payload.panel;
    const raw = String(state.telegram || 'unknown').toLowerCase();
    const badge = $('#tg-badge');
    if (badge) {
      let kind = 'subtle';
      let label = 'TG: ' + raw;
      if (raw.indexOf('online') >= 0 || raw.indexOf('connect') >= 0 || raw === 'ok') {
        kind = 'ok';
        label = 'Telegram';
      } else if (raw.indexOf('offline') >= 0 || raw.indexOf('error') >= 0 || raw === 'down') {
        kind = 'bad';
        label = 'Telegram';
      }
      badge.className = 'badge ' + kind;
      badge.textContent = label;
      badge.title = 'Состояние Telegram-бота: ' + raw;
    }
    if (payload && payload.unread_alerts !== undefined) {
      const counter = $('#nav-alerts');
      if (counter) {
        const value = Number(payload.unread_alerts) || 0;
        counter.textContent = value > 99 ? '99+' : String(value);
        setHidden(counter, value <= 0);
      }
    }
    const host = state.panel && state.panel.host;
    const brandHost = $('#brand-host');
    if (brandHost && host) brandHost.textContent = host;
  }

  // ------------------------------------------------------------------- auth
  function authCard(inner) {
    return (
      '<div class="auth-card">' +
      '<div class="auth-brand">' + brandMark() +
      '<div><strong>PC Remote</strong><span>Локальная панель управления</span></div></div>' +
      inner +
      '</div>'
    );
  }

  function showAuth(payload) {
    const view = $('#auth');
    if (!view) return;
    setHidden($('#boot'), true);
    setHidden($('#shell'), true);
    setHidden(view, false);
    clearTimers();

    if (payload && payload.panel) state.panel = payload.panel;

    if (payload && payload.needs_setup) {
      if (payload.setup_allowed) {
        view.innerHTML = authCard(
          '<h2>Первый запуск</h2>' +
          '<p>Задайте пароль для входа в панель. Его можно изменить позже в настройках.</p>' +
          '<form class="auth-form" data-form="setup">' +
          '<label class="field"><span>Пароль</span>' +
          '<input class="input" type="password" name="password" autocomplete="new-password" minlength="8" required data-autofocus></label>' +
          '<label class="field"><span>Повторите пароль</span>' +
          '<input class="input" type="password" name="password2" autocomplete="new-password" minlength="8" required></label>' +
          '<p class="auth-error note" data-error hidden></p>' +
          '<button class="btn primary" type="submit">Задать пароль</button>' +
          '</form>' +
          '<div class="auth-foot"><span class="auth-note">Пароль хранится только на этом компьютере.</span></div>'
        );
      } else {
        view.innerHTML = authCard(
          '<h2>Нужен первый пароль</h2>' +
          '<p>Панель ещё не настроена. Откройте её на самом компьютере по адресу ' +
          '<b>http://127.0.0.1:' + esc((state.panel && state.panel.port) || 8090) + '</b> и задайте пароль.</p>' +
          '<button class="btn primary" type="button" data-act="auth-retry">Проверить снова</button>'
        );
      }
      return;
    }

    view.innerHTML = authCard(
      '<h2>Вход в панель</h2>' +
      '<p>Введите пароль, чтобы управлять компьютером.</p>' +
      '<form class="auth-form" data-form="login">' +
      '<label class="field"><span>Пароль</span>' +
      '<input class="input" type="password" name="password" autocomplete="current-password" required data-autofocus></label>' +
      '<p class="auth-error note" data-error hidden></p>' +
      '<button class="btn primary" type="submit">Войти</button>' +
      '</form>' +
      '<div class="auth-foot"><span class="auth-note">Доступ только в локальной сети.</span>' +
      '<a href="#" data-act="auth-retry">Обновить</a></div>'
    );
  }

  function authError(message) {
    const box = $('#auth [data-error]');
    if (!box) {
      toast(message, 'error');
      return;
    }
    box.textContent = message;
    setHidden(box, false);
  }

  function enterApp() {
    window.scrollTo(0, 0);
    closeSheet();
    closeDrawer();
    clearTimers();
    buildSidebar();
    buildBottomNav();
    buildSheet();
    restoreCollapse();
    setHidden($('#boot'), true);
    setHidden($('#auth'), true);
    setHidden($('#shell'), false);
    setHidden($('#bottomnav'), false);
    renderRoute();
  }

  let lostSessionBusy = false;

  async function handleLostSession() {
    if (lostSessionBusy || !$('#shell') || $('#shell').hasAttribute('hidden')) return;
    lostSessionBusy = true;
    state.csrf = '';
    try {
      const payload = await request('/api/session', { noAuth: true });
      showAuth(payload);
    } catch (error) {
      toast('Сессия истекла. Обновите страницу.', 'warn');
    } finally {
      lostSessionBusy = false;
    }
  }

  async function refreshSession() {
    try {
      const payload = await request('/api/session', { noAuth: true });
      if (payload.authenticated) {
        state.csrf = payload.csrf;
        if (!($('#shell').hasAttribute('hidden'))) {
          updateBadges(payload);
          renderRoute();
        } else {
          enterApp();
        }
      } else {
        showAuth(payload);
      }
    } catch (error) {
      authError('Панель недоступна: ' + describeError(error));
    }
  }

  // ----------------------------------------------------------------- router
  function currentRoute() {
    const raw = (location.hash || '').replace(/^#\/?/, '').split('?')[0];
    return ROUTES[raw] ? raw : 'overview';
  }

  function navigate(route) {
    if (!ROUTES[route]) route = 'overview';
    window.scrollTo(0, 0);
    closeSheet();
    closeDrawer();
    const target = '#/' + route;
    if (location.hash === target) renderRoute();
    else location.hash = target;
  }

  function renderRoute() {
    const route = currentRoute();
    if (!ROUTES[route]) return;
    state.route = route;
    clearTimers();
    state.refresh = null;
    closeModals();
    const token = ++state.viewToken;
    setActiveNav(route);
    const meta = ROUTES[route];
    $('#page-title').textContent = meta.title;
    $('#page-sub').textContent = meta.sub;
    $('#view').innerHTML = '<div class="loading-box"><span class="spinner"></span><p>Загрузка…</p></div>';
    const render = VIEWS[route];
    if (!render) {
      $('#view').innerHTML = '<div class="empty">Раздел пока недоступен</div>';
      return;
    }
    Promise.resolve()
      .then(function () { return render(token); })
      .catch(function (error) {
        if (token !== state.viewToken) return;
        $('#view').innerHTML =
          '<div class="empty">' + esc(describeError(error)) +
          '<div style="margin-top:14px"><button class="btn" data-act="page-reload">Повторить</button></div></div>';
      });
  }

  function pageToken(token) {
    return token === state.viewToken;
  }

  function pageError(token, error) {
    if (!pageToken(token)) return;
    toast(describeError(error), 'error');
  }

  // ------------------------------------------------------------- global acts
  action('theme-toggle', function () { toggleTheme(); });
  action('logout', async function () {
    const confirmed = await confirmModal({
      title: 'Выйти из панели?', text: 'Потребуется снова ввести пароль.', okText: 'Выйти',
    });
    if (!confirmed) return;
    try {
      await api('/api/logout', { method: 'POST', json: {} });
    } catch (error) {
      /* cookie may already be gone */
    }
    state.csrf = '';
    closeModals();
    const payload = await request('/api/session', { noAuth: true });
    showAuth(payload);
  });
  action('nav-collapse', function () { toggleCollapse(); });
  action('sheet-open', function () { openSheet(); });
  action('auth-retry', function () { refreshSession(); });
  action('page-reload', function () { renderRoute(); });

  document.addEventListener('click', function (event) {
    const nav = event.target.closest('[data-nav]');
    if (nav) {
      event.preventDefault();
      navigate(nav.getAttribute('data-nav'));
      return;
    }
    if (event.target.closest('[data-sheet-close]')) {
      closeSheet();
      return;
    }
    if (event.target.closest('#scrim')) {
      closeDrawer();
    }
  });

  window.addEventListener('hashchange', function () {
    if ($('#shell').hasAttribute('hidden')) return;
    renderRoute();
  });

  window.addEventListener('resize', function () {
    if (window.innerWidth > 980) closeDrawer();
  });

  const menuButton = $('#menu-btn');
  if (menuButton) menuButton.addEventListener('click', function () { openDrawer(); });
  const refreshButton = $('#refresh-btn');
  if (refreshButton) {
    refreshButton.addEventListener('click', function () {
      if (state.refresh) {
        Promise.resolve()
          .then(state.refresh)
          .catch(function (error) { toast(describeError(error), 'error'); });
      } else {
        renderRoute();
      }
    });
  }
  const themeButton = $('#theme-btn');
  if (themeButton) themeButton.addEventListener('click', function () { toggleTheme(); });

  // ------------------------------------------------------------------- boot
  async function boot() {
    applyTheme();
    try {
      const payload = await request('/api/session', { noAuth: true });
      state.panel = payload.panel || null;
      state.telegram = payload.telegram || 'unknown';
      if (payload.authenticated && payload.csrf) {
        state.csrf = payload.csrf;
        enterApp();
      } else {
        showAuth(payload);
      }
    } catch (error) {
      setHidden($('#boot'), true);
      showAuth({ needs_setup: false });
      authError('Не удалось связаться с панелью: ' + describeError(error));
    }
  }

  FORM.login = async function (form) {
    const button = form.querySelector('button[type="submit"]');
    const password = form.querySelector('[name="password"]').value;
    if (!password) return;
    button.disabled = true;
    try {
      const payload = await api('/api/login', { method: 'POST', json: { password: password }, noAuth: true });
      state.csrf = payload.csrf;
      state.panel = payload.panel || state.panel;
      enterApp();
    } catch (error) {
      authError(describeError(error));
      button.disabled = false;
    }
  };

  FORM.setup = async function (form) {
    const password = form.querySelector('[name="password"]').value;
    const repeat = form.querySelector('[name="password2"]').value;
    if (password !== repeat) {
      authError('Пароли не совпадают');
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const payload = await api('/api/setup', { method: 'POST', json: { password: password }, noAuth: true });
      state.csrf = payload.csrf;
      state.panel = payload.panel || state.panel;
      enterApp();
    } catch (error) {
      authError(describeError(error));
      button.disabled = false;
    }
  };

// ------------------------------------------------------------ ui helpers
  const CHANGE = {};

  function onchange(name, fn) {
    CHANGE[name] = fn;
  }

  document.addEventListener('change', function (event) {
    const node = event.target.closest('[data-change]');
    if (!node) return;
    const handler = CHANGE[node.getAttribute('data-change')];
    if (!handler) return;
    try {
      handler(node, event);
    } catch (error) {
      toast(describeError(error), 'error');
    }
  });

  function guarded(fn) {
    return function (node, event) {
      try {
        const result = fn(node, event);
        if (result && typeof result.catch === 'function') {
          result.catch(function (error) { toast(describeError(error), 'error'); });
        }
      } catch (error) {
        toast(describeError(error), 'error');
      }
    };
  }

  function card(title, body, tools, cls) {
    return (
      '<section class="card' + (cls ? ' ' + cls : '') + '">' +
      '<div class="card-head"><h3>' + esc(title) + '</h3>' +
      (tools ? '<div class="head-tools">' + tools + '</div>' : '') +
      '</div>' + body + '</section>'
    );
  }

  function badge(label, kind, dot) {
    return '<span class="badge ' + (kind || 'subtle') + (dot === false ? ' no-dot' : '') + '">' + esc(label) + '</span>';
  }

  function emptyBox(iconName, title, text) {
    return (
      '<div class="empty"><div class="empty-icon">' + icon(iconName || 'info') + '</div>' +
      '<strong>' + esc(title) + '</strong>' +
      (text ? '<p>' + esc(text) + '</p>' : '') + '</div>'
    );
  }

  function gauge(value, cls, small) {
    const pct = levelPercent(value);
    return (
      '<div class="gauge ' + (cls || '') + (small ? ' sm' : '') + '" style="--p:' + pct.toFixed(1) + '">' +
      '<span class="gauge-value">' + (isNum(value) ? num(value, 0) : '—') +
      (isNum(value) ? '<small>%</small>' : '') + '</span></div>'
    );
  }

  function usageLevel(percent) {
    if (!isNum(percent)) return '';
    if (percent >= 90) return 'bad';
    if (percent >= 70) return 'warn';
    return 'green';
  }

  function bar(percent, cls) {
    return '<div class="bar ' + (cls || '') + '"><i style="width:' + levelPercent(percent).toFixed(1) + '%"></i></div>';
  }

  function measure(value, digits, unit) {
    if (!isNum(value)) return '—';
    return num(value, digits == null ? 0 : digits) + (unit || '');
  }

  function mbText(value) {
    if (!isNum(value)) return '—';
    return fmtBytes(value * 1024 * 1024, 1);
  }

  function frequencyText(value) {
    if (!isNum(value)) return '—';
    return value >= 1000 ? num(value / 1000, 2) + ' ГГц' : num(value, 0) + ' МГц';
  }

  function telegramBadge() {
    const raw = String(state.telegram || 'unknown').toLowerCase();
    if (raw.indexOf('online') >= 0 || raw.indexOf('connect') >= 0 || raw === 'ok') {
      return badge('Telegram на связи', 'ok');
    }
    if (raw.indexOf('offline') >= 0 || raw.indexOf('error') >= 0 || raw === 'down') {
      return badge('Telegram недоступен', 'bad');
    }
    return badge('Telegram: нет данных', 'subtle');
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (error) {
      /* fall back to execCommand below */
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (error) {
      ok = false;
    }
    area.remove();
    return ok;
  }

  // ---------------------------------------------------------------- overview
  const ALERT_CATEGORIES = {
    cpu: 'Процессор', gpu: 'Видеокарта', ram: 'Память', disk: 'Диск',
    recovery: 'Восстановление', agent: 'Агент',
  };
  const ALERT_LEVELS = { info: 'Инфо', warning: 'Внимание', critical: 'Критично' };
  const CHART_METRICS = [
    { key: 'cpu', label: 'CPU' },
    { key: 'ram', label: 'RAM' },
    { key: 'gpu', label: 'GPU' },
    { key: 'disk', label: 'Диск' },
  ];
  const CHART_RANGES = [
    { key: '5m', label: '5 мин' },
    { key: '1h', label: '1 час' },
    { key: '24h', label: '24 часа' },
  ];

  function chartControlsHtml() {
    const current = state.chart;
    const metrics = CHART_METRICS.map(function (item) {
      return '<button type="button" class="' + (current.metric === item.key ? 'active' : '') +
        '" data-act="chart-metric" data-metric="' + item.key + '">' + esc(item.label) + '</button>';
    }).join('');
    const ranges = CHART_RANGES.map(function (item) {
      return '<button type="button" class="' + (current.range === item.key ? 'active' : '') +
        '" data-act="chart-range" data-range="' + item.key + '">' + esc(item.label) + '</button>';
    }).join('');
    let devices = '';
    if (current.metric === 'disk') {
      const keys = Object.keys((state.overview && state.overview.disk) || {});
      if (!keys.length) keys.push(current.device || '');
      const options = keys.filter(Boolean).map(function (key) {
        return '<option value="' + esc(key) + '"' + (key === current.device ? ' selected' : '') + '>' + esc(key) + '</option>';
      }).join('');
      devices = '<select class="select" data-change="chart-device" aria-label="Диск">' + options + '</select>';
    }
    return (
      '<div class="seg" role="group" aria-label="Метрика">' + metrics + '</div>' +
      '<div class="seg" role="group" aria-label="Диапазон">' + ranges + '</div>' +
      devices +
      '<button type="button" class="iconbtn sm" data-act="chart-reload" title="Обновить график">' + icon('refresh') + '</button>'
    );
  }

  function chartCardHtml() {
    return (
      '<section class="card chart-card">' +
      '<div class="card-head"><h3>История нагрузки</h3>' +
      '<div class="head-tools chart-controls" id="chart-controls">' + chartControlsHtml() + '</div></div>' +
      '<div class="chart-box" id="chart-box"><span class="spinner"></span></div></section>'
    );
  }

  async function loadChart() {
    const box = $('#chart-box');
    if (!box) return;
    const token = state.viewToken;
    const current = state.chart;
    if (current.metric === 'disk' && !current.device) {
      box.innerHTML = emptyBox('disk', 'Нет дисков', 'Не удалось найти доступные диски.');
      return;
    }
    const seq = (current.seq || 0) + 1;
    current.seq = seq;
    box.innerHTML = '<span class="spinner"></span>';
    const params = new URLSearchParams({
      metric: current.metric, range: current.range, t: String(Date.now()),
    });
    if (current.metric === 'disk') params.set('device', current.device);
    try {
      const response = await fetch('/api/chart?' + params.toString(), {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (seq !== current.seq || !pageToken(token)) return;
      if (response.status === 204) {
        box.innerHTML = emptyBox('chart', 'Пока нет данных', 'График появится после нескольких измерений.');
        return;
      }
      if (!response.ok) throw new Error('Не удалось загрузить график');
      const blob = await response.blob();
      if (seq !== current.seq || !pageToken(token)) return;
      if (current.url) URL.revokeObjectURL(current.url);
      current.url = URL.createObjectURL(blob);
      box.innerHTML = '<img alt="График ' + esc(current.metric) + '" src="' + current.url + '">';
    } catch (error) {
      if (seq !== current.seq) return;
      box.innerHTML = emptyBox('chart', 'График недоступен', describeError(error));
    }
  }

  function systemCardHtml(payload) {
    const cpu = payload.cpu || {};
    const unread = Number(payload.unread_alerts) || 0;
    const cores = isNum(cpu.logical_cores) ? cpu.logical_cores : null;
    const physical = isNum(cpu.physical_cores) ? cpu.physical_cores : null;
    return (
      '<section class="card system-card">' +
      '<div class="card-head"><h3>Компьютер</h3><div class="head-tools">' +
      '<button type="button" class="iconbtn sm" data-act="ov-refresh" title="Обновить">' + icon('refresh') + '</button>' +
      '</div></div>' +
      '<div class="stack-sm">' +
      '<div class="host-name">' + esc(payload.hostname || 'Компьютер') + '</div>' +
      '<div class="chip-row">' +
      (payload.internet ? badge('Интернет есть', 'ok') : badge('Нет интернета', 'bad')) +
      telegramBadge() +
      badge('Работает ' + fmtUptime(payload.uptime), 'subtle', false) +
      '</div>' +
      '<div class="metric-lines">' +
      '<div class="metric-line"><span>Ядра</span><b>' +
      (physical && cores ? esc(physical) + ' / ' + esc(cores) + ' потоков' : cores ? esc(cores) + ' потоков' : '—') +
      '</b></div>' +
      '<div class="metric-line"><span>Частота CPU</span><b>' + esc(frequencyText(cpu.frequency)) + '</b></div>' +
      '<div class="metric-line"><span>Непрочитанные оповещения</span><b>' + unread + '</b></div>' +
      '<div class="metric-line"><span>Задачи планировщика</span><b>' + (Number(payload.scheduler_count) || 0) + '</b></div>' +
      '</div></div></section>'
    );
  }

  function cpuCardHtml(payload) {
    const cpu = payload.cpu || {};
    const util = isNum(cpu.utilization) ? cpu.utilization : null;
    return (
      '<section class="card stat-card">' +
      '<div class="stat-top"><div><div class="stat-title">Процессор</div>' +
      '<div class="stat-sub">' + (isNum(cpu.logical_cores) ? esc(cpu.logical_cores) + ' потоков' : 'Загрузка') + '</div></div>' +
      gauge(util, usageLevel(util)) + '</div>' +
      '<div><div class="stat-headline">' + (isNum(util) ? num(util, 0) : '—') + (isNum(util) ? '<small>%</small>' : '') + '</div>' +
      '<div class="stat-sub">' + esc(measure(cpu.temperature, 0, ' °C')) + ' · ' + esc(frequencyText(cpu.frequency)) + '</div></div>' +
      '</section>'
    );
  }

  function ramCardHtml(payload) {
    const ram = payload.ram || {};
    const util = isNum(ram.utilization_percent) ? ram.utilization_percent : null;
    return (
      '<section class="card stat-card">' +
      '<div class="stat-top"><div><div class="stat-title">Память</div>' +
      '<div class="stat-sub">' + esc(mbText(ram.total_mb)) + ' всего</div></div>' +
      gauge(util, usageLevel(util), true) + '</div>' +
      '<div><div class="stat-headline">' + (isNum(util) ? num(util, 0) : '—') + (isNum(util) ? '<small>%</small>' : '') + '</div>' +
      '<div class="stat-sub">' + esc(mbText(ram.used_mb)) + ' занято · ' + esc(mbText(ram.available_mb)) + ' свободно</div></div>' +
      bar(util, usageLevel(util)) + '</section>'
    );
  }

  function gpuCardHtml(payload) {
    const gpu = payload.gpu || {};
    const util = isNum(gpu.utilization) ? gpu.utilization : null;
    const vram = isNum(gpu.vram_used_mb) && isNum(gpu.vram_total_mb)
      ? mbText(gpu.vram_used_mb) + ' / ' + mbText(gpu.vram_total_mb) : '—';
    return (
      '<section class="card stat-card">' +
      '<div class="stat-top"><div><div class="stat-title">Видеокарта</div>' +
      '<div class="stat-sub">' + esc(measure(gpu.temperature, 0, ' °C')) + '</div></div>' +
      gauge(util, usageLevel(util), true) + '</div>' +
      '<div><div class="stat-headline">' + (isNum(util) ? num(util, 0) : '—') + (isNum(util) ? '<small>%</small>' : '') + '</div>' +
      '<div class="stat-sub">VRAM ' + esc(vram) + '</div></div>' +
      '<div class="metric-lines"><div class="metric-line"><span>Частота</span><b>' + esc(frequencyText(gpu.frequency)) + '</b></div>' +
      '<div class="metric-line"><span>Питание</span><b>' + esc(measure(gpu.power_watts, 0, ' Вт')) + '</b></div></div>' +
      '</section>'
    );
  }

  function disksCardHtml(payload) {
    const drives = payload.disk || {};
    const keys = Object.keys(drives);
    const body = keys.length
      ? '<div class="drive-list">' + keys.map(function (key) {
        const info = drives[key] || {};
        const pct = isNum(info.utilization_percent) ? info.utilization_percent : null;
        return (
          '<div class="drive-line"><div class="drive-meta"><b>' + esc(key) + '</b>' +
          '<span>' + esc(mbText(info.used_mb)) + ' / ' + esc(mbText(info.total_mb)) + '</span></div>' +
          bar(pct, usageLevel(pct)) +
          '<div class="drive-meta"><span>' + (isNum(pct) ? num(pct, 0) + '%' : '—') + '</span>' +
          '<span>свободно ' + esc(mbText(info.available_mb)) + '</span></div></div>'
        );
      }).join('') + '</div>'
      : emptyBox('disk', 'Диски не найдены', 'Не удалось получить список дисков.');
    return card('Диски', body);
  }
function alertItemHtml(alert) {
    const level = ['info', 'warning', 'critical'].indexOf(alert.level) >= 0 ? alert.level : 'info';
    const unread = !alert.acknowledged;
    const dotClass = level === 'critical' ? 'bad' : level === 'warning' ? 'warn' : '';
    return (
      '<div class="alert-item ' + level + (unread ? ' unread' : '') + '">' +
      '<span class="dot ' + dotClass + '"></span>' +
      '<div class="alert-body"><div class="alert-msg">' + esc(alert.message || '') + '</div>' +
      '<div class="alert-meta"><span>' + esc(alert.time || fmtTime(alert.timestamp)) + '</span>' +
      '<span>·</span><span>' + esc(ALERT_CATEGORIES[alert.category] || alert.category || '') + '</span>' +
      '<span>·</span><span>' + esc(ALERT_LEVELS[level]) + '</span></div></div>' +
      (unread
        ? '<button type="button" class="iconbtn sm" data-act="alert-ack" data-ts="' + esc(alert.timestamp) + '" title="Отметить прочитанным">' + icon('check') + '</button>'
        : '') +
      '</div>'
    );
  }

  function alertsCardHtml(payload) {
    const alerts = payload.alerts || [];
    const unread = Number(payload.unread_alerts) || 0;
    const tools = unread
      ? '<button type="button" class="btn ghost sm" data-act="alert-ack-all">Прочитать все</button>'
      : '';
    const body = alerts.length
      ? '<div class="alert-list">' + alerts.map(alertItemHtml).join('') + '</div>'
      : emptyBox('check', 'Всё спокойно', 'Новых оповещений нет.');
    return card('Оповещения' + (unread ? ' · ' + unread : ''), body, tools);
  }

  function launchesCardHtml(payload) {
    const launches = payload.launches || [];
    const body = launches.length
      ? '<div class="launch-list">' + launches.map(function (item) {
        return (
          '<div class="launch-item">' +
          '<span class="dot ' + (item.success ? 'ok' : 'bad') + '"></span>' +
          '<span class="launch-name" title="' + esc(item.error || item.name) + '">' + esc(item.name || '—') + '</span>' +
          '<span class="launch-time">' + esc(item.time || fmtTime(item.timestamp)) + '</span>' +
          '</div>'
        );
      }).join('') + '</div>'
      : emptyBox('apps', 'Запусков не было', 'Здесь появятся последние запуски приложений.');
    return card('Последние запуски', body);
  }

  function topProcessesCardHtml(payload) {
    const rows = payload.top_processes || [];
    const body = rows.length
      ? '<div class="table-wrap"><table class="table"><thead><tr>' +
        '<th>Процесс</th><th class="num">CPU</th><th class="num">Память</th></tr></thead><tbody>' +
        rows.slice(0, 6).map(function (proc) {
          return '<tr><td class="proc-name" title="' + esc(proc.name || '') + '">' + esc(proc.name || '—') + '</td>' +
            '<td class="num">' + esc(measure(proc.cpu, 1, '%')) + '</td>' +
            '<td class="num">' + esc(measure(proc.memory_mb, 0, ' МБ')) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : emptyBox('processes', 'Нет данных', 'Не удалось получить список процессов.');
    const tools = '<button type="button" class="btn ghost sm" data-nav="processes">Все процессы</button>';
    return card('Нагрузка процессов', body, tools);
  }

  function accessCardHtml(payload) {
    const panel = payload.panel || {};
    const urls = panel.urls || {};
    const rows = [];
    if (urls.local) {
      rows.push('<div class="url-row"><span class="url-label">Этот ПК</span><a href="' + esc(urls.local) + '" target="_blank" rel="noreferrer">' + esc(urls.local) + '</a>' +
        '<button type="button" class="iconbtn sm" data-act="copy-url" data-copy="' + esc(urls.local) + '" title="Скопировать">' + icon('clipboard') + '</button></div>');
    }
    if (urls.lan && urls.lan !== urls.local) {
      rows.push('<div class="url-row"><span class="url-label">Локальная сеть</span><a href="' + esc(urls.lan) + '" target="_blank" rel="noreferrer">' + esc(urls.lan) + '</a>' +
        '<button type="button" class="iconbtn sm" data-act="copy-url" data-copy="' + esc(urls.lan) + '" title="Скопировать">' + icon('clipboard') + '</button></div>');
    }
    const body = (rows.length ? '<div class="stack-sm">' + rows.join('') + '</div>' : '') +
      '<p class="stat-sub" style="margin-top:10px">Панель доступна только в локальной сети и требует пароль.</p>';
    return card('Доступ к панели', body);
  }

  function overviewStats(payload) {
    return (
      '<div class="grid cols-2">' + systemCardHtml(payload) + cpuCardHtml(payload) + '</div>' +
      '<div class="grid cols-3">' + ramCardHtml(payload) + gpuCardHtml(payload) + disksCardHtml(payload) + '</div>'
    );
  }

  function overviewLower(payload) {
    return (
      '<div class="grid cols-2">' + alertsCardHtml(payload) + launchesCardHtml(payload) + '</div>' +
      '<div class="grid cols-2">' + topProcessesCardHtml(payload) + accessCardHtml(payload) + '</div>'
    );
  }

  async function refreshOverview() {
    const token = state.viewToken;
    const payload = await api('/api/overview');
    if (!pageToken(token)) return;
    state.overview = payload;
    updateBadges(payload);
    const stats = $('#ov-stats');
    const lower = $('#ov-lower');
    if (!stats || !lower) return;
    stats.innerHTML = overviewStats(payload);
    lower.innerHTML = overviewLower(payload);
  }

  VIEWS.overview = async function (token) {
    const payload = await api('/api/overview');
    if (!pageToken(token)) return;
    state.overview = payload;
    updateBadges(payload);
    const drives = Object.keys(payload.disk || {});
    if (!state.chart.device || drives.indexOf(state.chart.device) < 0) {
      state.chart.device = drives[0] || '';
    }
    $('#view').innerHTML =
      '<div class="stack">' +
      '<div id="ov-stats" class="stack">' + overviewStats(payload) + '</div>' +
      chartCardHtml() +
      '<div id="ov-lower" class="stack">' + overviewLower(payload) + '</div>' +
      '</div>';
    loadChart();
    state.refresh = function () { return refreshOverview(); };
    onTimer(function () {
      refreshOverview().catch(function () { /* keep the panel alive on transient errors */ });
    }, 8000);
  };

  action('ov-refresh', guarded(async function () {
    await refreshOverview();
    toast('Обновлено', 'ok', 1500);
  }));

  action('chart-metric', function (node) {
    const metric = node.getAttribute('data-metric');
    if (!metric || metric === state.chart.metric) return;
    state.chart.metric = metric;
    if (metric === 'disk') {
      const drives = Object.keys((state.overview && state.overview.disk) || {});
      if (drives.indexOf(state.chart.device) < 0) state.chart.device = drives[0] || '';
    }
    const controls = $('#chart-controls');
    if (controls) controls.innerHTML = chartControlsHtml();
    loadChart();
  });

  action('chart-range', function (node) {
    const range = node.getAttribute('data-range');
    if (!range || range === state.chart.range) return;
    state.chart.range = range;
    const controls = $('#chart-controls');
    if (controls) controls.innerHTML = chartControlsHtml();
    loadChart();
  });

  onchange('chart-device', function (node) {
    state.chart.device = node.value;
    loadChart();
  });

  action('chart-reload', function () { loadChart(); });

  action('alert-ack', guarded(async function (node) {
    const timestamp = Number(node.getAttribute('data-ts'));
    if (!isNum(timestamp)) return;
    await api('/api/alerts/ack', { method: 'POST', json: { timestamp: timestamp } });
    await refreshOverview();
  }));

  action('alert-ack-all', guarded(async function () {
    await api('/api/alerts/ack', { method: 'POST', json: {} });
    await refreshOverview();
    toast('Все оповещения прочитаны', 'ok');
  }));

  action('copy-url', guarded(async function (node) {
    const text = node.getAttribute('data-copy') || '';
    const ok = await copyText(text);
    if (ok) toast('Скопировано', 'ok', 1500);
    else toast('Не удалось скопировать', 'error');
  }));
// ------------------------------------------------------------------- files
  const FILES = { path: null, parent: null, kind: 'drives', data: null };

  function filesShellHtml() {
    return (
      '<section class="card" style="overflow:hidden">' +
      '<div class="files-toolbar">' +
      '<button type="button" class="iconbtn sm" id="file-up" data-act="file-up" title="Наверх">' + icon('up') + '</button>' +
      '<div class="crumbs" id="files-crumbs"></div>' +
      '<button type="button" class="iconbtn sm" data-act="files-refresh" title="Обновить">' + icon('refresh') + '</button>' +
      '<form class="files-search" data-form="files-search">' +
      '<input class="input" id="files-search-input" name="q" placeholder="Поиск в папке" autocomplete="off">' +
      '<button type="submit" class="iconbtn sm" title="Найти">' + icon('search') + '</button>' +
      '</form>' +
      '<button type="button" class="btn sm" id="file-upload" data-act="files-upload">' + icon('upload') + 'Загрузить</button>' +
      '<button type="button" class="btn sm ghost" id="file-mkdir" data-act="file-mkdir">' + icon('plus') + 'Папка</button>' +
      '</div>' +
      '<div id="files-body"></div>' +
      '<div id="upload-slot"></div>' +
      '<input type="file" id="file-input" data-change="file-input" multiple hidden>' +
      '</section>'
    );
  }

  function crumbParts(path) {
    const out = [];
    if (!path) return out;
    let rest = String(path).replace(/\//g, '\\');
    let built;
    const drive = rest.match(/^([A-Za-z]:\\)/);
    if (drive) {
      built = drive[1];
      rest = rest.slice(drive[1].length);
    } else if (rest.slice(0, 2) === '\\\\') {
      const segments = rest.replace(/^\\\\/, '').split('\\');
      const server = segments.shift() || '';
      const share = segments.shift() || '';
      built = '\\\\' + server + '\\' + share;
      rest = segments.join('\\');
    } else {
      return [{ label: rest, path: rest }];
    }
    out.push({ label: built, path: built });
    rest.split('\\').filter(Boolean).forEach(function (segment) {
      built = built.charAt(built.length - 1) === '\\' ? built + segment : built + '\\' + segment;
      out.push({ label: segment, path: built });
    });
    return out;
  }

  function filesCrumbsHtml() {
    if (FILES.kind === 'drives') return '<span class="crumb current">Диски</span>';
    const parts = crumbParts(FILES.path);
    const nodes = ['<button type="button" class="crumb" data-act="file-drives">Диски</button>'];
    parts.forEach(function (part, index) {
      nodes.push('<span class="crumb-sep">›</span>');
      if (index === parts.length - 1) {
        nodes.push('<span class="crumb current">' + esc(part.label) + '</span>');
      } else {
        nodes.push('<button type="button" class="crumb" data-act="file-open" data-dir="1" data-path="' + esc(part.path) + '">' + esc(part.label) + '</button>');
      }
    });
    return nodes.join('');
  }

  function driveTileHtml(drive) {
    const total = Number(drive.total) || 0;
    const free = Number(drive.free) || 0;
    const percent = isNum(drive.percent) ? drive.percent : total > 0 ? ((total - free) / total) * 100 : 0;
    return (
      '<button type="button" class="drive-tile" data-act="file-open" data-dir="1" data-path="' + esc(drive.path) + '">' +
      '<div class="drive-head"><span class="drive-icon">' + icon('disk') + '</span>' +
      '<div><strong>' + esc(drive.name || drive.path) + '</strong>' +
      '<span class="drive-sub">' + esc(drive.fstype || '') + (total ? ' · ' + esc(fmtBytes(total)) : '') + '</span></div></div>' +
      '<div class="drive-meta"><span>Свободно ' + esc(fmtBytes(free)) + '</span><span>' + num(percent, 0) + '%</span></div>' +
      bar(percent, usageLevel(percent)) +
      '</button>'
    );
  }

  function fileRowHtml(entry, showPath) {
    const isDir = !!entry.is_dir;
    const label = '<span class="file-label">' + esc(entry.name) + '</span>' +
      (showPath ? '<span class="hit-path">' + esc(String(entry.path || '').replace(/[^\\/]+$/, '')) + '</span>' : '');
    return (
      '<div class="file-row' + (showPath ? ' search-hit' : '') + '">' +
      '<button type="button" class="file-name" data-act="file-open" data-dir="' + (isDir ? '1' : '0') + '" data-path="' + esc(entry.path) + '">' +
      '<span class="file-icon' + (isDir ? ' dir' : '') + '">' + icon(isDir ? 'folder' : 'file') + '</span>' +
      '<span style="min-width:0;display:block">' + label + '</span></button>' +
      '<span class="file-size">' + (isDir ? 'папка' : esc(fileSizeText(entry.size))) + '</span>' +
      '<button type="button" class="iconbtn sm row-menu" data-act="file-menu" data-dir="' + (isDir ? '1' : '0') +
      '" data-path="' + esc(entry.path) + '" data-name="' + esc(entry.name) + '" title="Действия">' + icon('dots') + '</button>' +
      '</div>'
    );
  }

  function fileSizeText(size) {
    return isNum(size) && size > 0 ? fmtBytes(size, 1) : '—';
  }

  function filesBodyHtml() {
    const data = FILES.data || {};
    if (FILES.kind === 'drives') {
      const drives = data.drives || [];
      if (!drives.length) return emptyBox('disk', 'Диски не найдены', 'Не удалось получить список разделов.');
      return '<div class="drive-grid">' + drives.map(driveTileHtml).join('') + '</div>';
    }
    if (FILES.kind === 'search') {
      const results = data.results || [];
      const head = '<div class="search-head">' +
        '<span>Найдено: <b>' + results.length + '</b>' + (data.truncated ? ' (список ограничен)' : '') + '</span>' +
        '<span class="file-size">Просмотрено папок: ' + (Number(data.scanned_dirs) || 0) + '</span>' +
        '<span class="spacer"></span>' +
        '<button type="button" class="btn ghost sm" data-act="file-search-close">Закрыть поиск</button></div>';
      const body = results.length
        ? '<div class="file-list">' + results.map(function (entry) { return fileRowHtml(entry, true); }).join('') + '</div>'
        : emptyBox('search', 'Ничего не найдено', 'Попробуйте другую часть имени.');
      return head + body;
    }
    const entries = data.entries || [];
    if (!entries.length) return emptyBox('folder', 'Папка пуста', 'Перетащите файлы или загрузите их кнопкой выше.');
    return '<div class="file-list">' + entries.map(function (entry) { return fileRowHtml(entry, false); }).join('') + '</div>';
  }

  function filesRender() {
    const crumbs = $('#files-crumbs');
    const body = $('#files-body');
    const up = $('#file-up');
    const upload = $('#file-upload');
    const mkdir = $('#file-mkdir');
    if (crumbs) crumbs.innerHTML = filesCrumbsHtml();
    if (body) body.innerHTML = filesBodyHtml();
    if (up) up.disabled = FILES.kind === 'drives';
    if (upload) upload.disabled = FILES.kind !== 'dir';
    if (mkdir) mkdir.disabled = FILES.kind !== 'dir';
    const slot = $('#upload-slot');
    if (slot) slot.innerHTML = '';
  }

  async function filesLoad(path) {
    const token = state.viewToken;
    const data = await api(path ? '/api/files?path=' + encodeURIComponent(path) : '/api/files');
    if (!pageToken(token)) return;
    FILES.kind = data.kind || 'dir';
    FILES.data = data;
    FILES.path = data.kind === 'dir' ? data.path : null;
    FILES.parent = data.kind === 'dir' ? data.parent : null;
    filesRender();
  }

  async function filesSearch(root, query) {
    const token = state.viewToken;
    const data = await api('/api/files/search?path=' + encodeURIComponent(root) + '&q=' + encodeURIComponent(query));
    if (!pageToken(token)) return;
    FILES.kind = 'search';
    FILES.data = data;
    FILES.path = data.root || root;
    filesRender();
  }

  function filesReload() {
    if (FILES.kind === 'search' && FILES.data) return filesSearch(FILES.data.root, FILES.data.query);
    return filesLoad(FILES.path);
  }

  function downloadFile(path) {
    const link = document.createElement('a');
    link.href = '/api/files/download?path=' + encodeURIComponent(path);
    link.setAttribute('download', '');
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function filesAction(payload) {
    const result = await api('/api/files/action', { method: 'POST', json: payload });
    toast((result && result.message) || 'Готово', 'ok', 2600);
    await filesReload();
  }

  function uploadFiles(fileList) {
    const slot = $('#upload-slot');
    if (!slot) return;
    const files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    if (!FILES.path || FILES.kind !== 'dir') {
      toast('Сначала откройте папку', 'warn');
      return;
    }
    const total = files.reduce(function (sum, file) { return sum + (file.size || 0); }, 0);
    if (total > 200 * 1024 * 1024) {
      toast('Слишком большой объём: максимум 200 МБ за раз', 'error', 5000);
      return;
    }
    slot.innerHTML =
      '<div class="upload-panel"><span class="spinner"></span>' +
      '<span id="upload-text">Загрузка…</span><div class="bar"><i id="upload-bar" style="width:0%"></i></div></div>';
    const form = new FormData();
    form.append('path', FILES.path);
    files.forEach(function (file) { form.append('file', file, file.name); });

    const xhr = new XMLHttpRequest();
    let finished = false;
    function fail(message) {
      if (finished) return;
      finished = true;
      slot.innerHTML = '<div class="upload-panel"><span class="dot bad"></span><span>' + esc(message) + '</span>' +
        '<span class="spacer"></span><button type="button" class="iconbtn sm" data-act="upload-dismiss">' + icon('x') + '</button></div>';
    }
    xhr.open('POST', '/api/files/upload');
    xhr.setRequestHeader('X-CSRF-Token', state.csrf || '');
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      const percent = Math.max(0, Math.min(100, (event.loaded / event.total) * 100));
      const barNode = $('#upload-bar');
      const textNode = $('#upload-text');
      if (barNode) barNode.style.width = percent.toFixed(1) + '%';
      if (textNode) textNode.textContent = 'Загрузка ' + num(percent, 0) + '%';
    };
    xhr.onerror = function () { fail('Нет связи с панелью'); };
    xhr.onabort = function () { fail('Загрузка отменена'); };
    xhr.onload = function () {
      if (finished) return;
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch (error) { payload = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        finished = true;
        const names = ((payload && payload.saved) || []).map(function (item) { return item.name; }).join(', ');
        toast('Загружено: ' + (names || 'файлы'), 'ok', 3600);
        filesReload().catch(function (error) { toast(describeError(error), 'error'); });
      } else if (xhr.status === 401) {
        finished = true;
        handleLostSession();
      } else {
        fail((payload && payload.error) || 'Ошибка загрузки (' + xhr.status + ')');
      }
    };
    xhr.send(form);
  }

  VIEWS.files = async function () {
    FILES.kind = 'drives';
    FILES.data = null;
    FILES.path = null;
    FILES.parent = null;
    $('#view').innerHTML = filesShellHtml();
    await filesLoad(null).catch(function (error) {
      const body = $('#files-body');
      if (body) body.innerHTML = emptyBox('alert', 'Не удалось открыть файлы', describeError(error));
    });
  };

  action('file-open', guarded(async function (node) {
    const path = node.getAttribute('data-path');
    if (!path) return;
    if (node.getAttribute('data-dir') === '0') {
      downloadFile(path);
      return;
    }
    await filesLoad(path);
  }));

  action('file-up', guarded(async function () {
    if (FILES.kind === 'drives') return;
    await filesLoad(FILES.parent || null);
  }));

  action('file-drives', guarded(async function () {
    await filesLoad(null);
  }));

  action('file-search-close', guarded(async function () {
    await filesLoad(FILES.path);
  }));

  action('files-refresh', guarded(async function () {
    await filesReload();
  }));

  action('files-upload', function () {
    if (FILES.kind !== 'dir') {
      toast('Сначала откройте папку', 'warn');
      return;
    }
    const input = $('#file-input');
    if (input) input.click();
  });

  action('upload-dismiss', function () {
    const slot = $('#upload-slot');
    if (slot) slot.innerHTML = '';
  });

  action('file-mkdir', guarded(async function () {
    if (FILES.kind !== 'dir' || !FILES.path) {
      toast('Сначала откройте папку', 'warn');
      return;
    }
    const name = await promptModal({ title: 'Новая папка', label: 'Имя папки', placeholder: 'Например, Отчёты' });
    if (name === null) return;
    const clean = String(name).trim();
    if (!clean) {
      toast('Введите имя папки', 'warn');
      return;
    }
    await filesAction({ action: 'mkdir', path: FILES.path, name: clean });
  }));

  action('file-menu', function (node) {
    const path = node.getAttribute('data-path') || '';
    const name = node.getAttribute('data-name') || path;
    const isDir = node.getAttribute('data-dir') === '1';
    const attrs = ' data-path="' + esc(path) + '" data-dir="' + (isDir ? '1' : '0') + '" data-name="' + esc(name) + '"';
    const tiles = [];
    if (!isDir) {
      tiles.push('<button type="button" class="action-tile" data-act="file-download-one"' + attrs + '>' + icon('download') + '<strong>Скачать</strong></button>');
    }
    tiles.push('<button type="button" class="action-tile" data-act="file-rename-one"' + attrs + '>' + icon('edit') + '<strong>Переименовать</strong></button>');
    tiles.push('<button type="button" class="action-tile" data-act="file-copy-one"' + attrs + '>' + icon('copy') + '<strong>Копировать</strong></button>');
    tiles.push('<button type="button" class="action-tile" data-act="file-move-one"' + attrs + '>' + icon('move') + '<strong>Переместить</strong></button>');
    tiles.push('<button type="button" class="action-tile danger" data-act="file-delete-one"' + attrs + '>' + icon('trash') + '<strong>Удалить</strong></button>');
    openModal({
      title: isDir ? 'Папка' : 'Файл',
      body: '<p class="note" style="margin-bottom:10px;overflow-wrap:anywhere">' + esc(path) + '</p>' +
        '<div class="action-grid">' + tiles.join('') + '</div>',
      footer: '<button type="button" class="btn ghost" data-act="modal-close">Закрыть</button>',
    });
  });

  action('file-download-one', function (node) {
    closeModals();
    downloadFile(node.getAttribute('data-path') || '');
  });

  action('file-rename-one', guarded(async function (node) {
    const path = node.getAttribute('data-path') || '';
    const oldName = node.getAttribute('data-name') || '';
    closeModals();
    const name = await promptModal({ title: 'Переименовать', label: 'Новое имя', value: oldName });
    if (name === null) return;
    const clean = String(name).trim();
    if (!clean || clean === oldName) return;
    await filesAction({ action: 'rename', path: path, name: clean });
  }));

  action('file-copy-one', guarded(async function (node) {
    const path = node.getAttribute('data-path') || '';
    closeModals();
    const dest = await promptModal({
      title: 'Копировать',
      label: 'Папка назначения',
      value: FILES.path || '',
      hint: 'Укажите полный путь к существующей папке.',
    });
    if (dest === null || !String(dest).trim()) return;
    await filesAction({ action: 'copy', path: path, dest: String(dest).trim() });
  }));

  action('file-move-one', guarded(async function (node) {
    const path = node.getAttribute('data-path') || '';
    closeModals();
    const dest = await promptModal({
      title: 'Переместить',
      label: 'Папка назначения',
      value: FILES.path || '',
      hint: 'Укажите полный путь к существующей папке.',
    });
    if (dest === null || !String(dest).trim()) return;
    await filesAction({ action: 'move', path: path, dest: String(dest).trim() });
  }));

  action('file-delete-one', guarded(async function (node) {
    const path = node.getAttribute('data-path') || '';
    const name = node.getAttribute('data-name') || path;
    const isDir = node.getAttribute('data-dir') === '1';
    closeModals();
    const confirmed = await confirmModal({
      title: 'Удалить ' + (isDir ? 'папку' : 'файл') + '?',
      text: name + (isDir ? ' будет удалена вместе с содержимым.' : ' будет удалён без корзины.'),
      okText: 'Удалить',
      danger: true,
    });
    if (!confirmed) return;
    await filesAction({ action: 'delete', path: path, confirm: true });
  }));

  FORM['files-search'] = async function (form) {
    const query = String((form.querySelector('[name="q"]') || {}).value || '').trim();
    if (!query) return;
    if (FILES.kind === 'drives' || !FILES.path) {
      toast('Сначала откройте диск или папку', 'warn');
      return;
    }
    await filesSearch(FILES.path, query);
  };

  onchange('file-input', function (node) {
    const files = Array.prototype.slice.call(node.files || []);
    node.value = '';
    uploadFiles(files);
  });
// --------------------------------------------------------------- processes
  const PROC = { list: [], sort: 'cpu', limit: 60 };

  const PROC_SORTS = [
    { key: 'cpu', label: 'CPU' },
    { key: 'mem', label: 'Память' },
    { key: 'name', label: 'Имя' },
  ];

  function procSortHtml() {
    return PROC_SORTS.map(function (item) {
      return '<button type="button" class="' + (PROC.sort === item.key ? 'active' : '') +
        '" data-act="proc-sort" data-sort="' + item.key + '">' + esc(item.label) + '</button>';
    }).join('');
  }

  function procLimitOptions() {
    return [30, 60, 120, 200].map(function (value) {
      return '<option value="' + value + '"' + (value === PROC.limit ? ' selected' : '') + '>' + value + '</option>';
    }).join('');
  }

  function processesShellHtml() {
    return (
      '<section class="card">' +
      '<div class="card-head"><h3>Процессы</h3><div class="head-tools">' +
      '<div class="seg" role="group" aria-label="Сортировка">' + procSortHtml() + '</div>' +
      '<select class="select" data-change="proc-limit" aria-label="Количество">' + procLimitOptions() + '</select>' +
      '<button type="button" class="iconbtn sm" data-act="proc-refresh" title="Обновить">' + icon('refresh') + '</button>' +
      '</div></div>' +
      '<div class="files-toolbar">' +
      '<input class="input" data-change="proc-filter" id="proc-filter" placeholder="Фильтр по имени, пользователю или PID" value="' + esc(state.processes.filter || '') + '">' +
      '<span class="spacer"></span><span class="stat-sub" id="proc-count"></span>' +
      '</div>' +
      '<div id="proc-table"></div>' +
      '</section>'
    );
  }

  function procRowHtml(proc) {
    const cpu = Number(proc.cpu) || 0;
    const name = String(proc.name || '—');
    const system = proc.critical
      ? '<span class="sys-badge">системный</span>'
      : '';
    const action = proc.terminable
      ? '<button type="button" class="iconbtn sm" data-act="proc-kill" data-pid="' + esc(proc.pid) + '" data-name="' + esc(name) + '" title="Завершить">' + icon('stop') + '</button>'
      : '<button type="button" class="iconbtn sm" disabled title="Защищённый процесс">' + icon('shield') + '</button>';
    return (
      '<tr>' +
      '<td><div class="proc-name"><span class="name" title="' + esc(name) + '">' + esc(name) + '</span>' + system + '</div>' +
      '<div class="proc-user">' + esc(proc.user || '—') + ' · PID ' + esc(proc.pid) + '</div></td>' +
      '<td class="num hide-xs">' + esc(proc.pid) + '</td>' +
      '<td class="num"><div class="cpu-cell">' + bar(cpu, usageLevel(cpu)) + '<span>' + num(cpu, 1) + '%</span></div></td>' +
      '<td class="num hide-xs">' + esc(measure(proc.memory_mb, 0, ' МБ')) + '</td>' +
      '<td class="actions">' + action + '</td>' +
      '</tr>'
    );
  }

  function procTableHtml() {
    const query = String(state.processes.filter || '').trim().toLowerCase();
    const list = PROC.list.filter(function (proc) {
      if (!query) return true;
      return String(proc.name || '').toLowerCase().indexOf(query) >= 0 ||
        String(proc.user || '').toLowerCase().indexOf(query) >= 0 ||
        String(proc.pid || '') === query;
    });
    if (!list.length) {
      return emptyBox('processes', 'Процессы не найдены', query ? 'Измените фильтр.' : 'Список пуст.');
    }
    return (
      '<div class="table-wrap"><table class="table"><thead><tr>' +
      '<th>Процесс</th><th class="num hide-xs">PID</th><th class="num">CPU</th>' +
      '<th class="num hide-xs">Память</th><th class="actions">Действие</th></tr></thead><tbody>' +
      list.map(procRowHtml).join('') + '</tbody></table></div>'
    );
  }

  function procRender() {
    const table = $('#proc-table');
    const count = $('#proc-count');
    if (table) table.innerHTML = procTableHtml();
    if (count) count.textContent = 'Показано ' + PROC.list.length + ' из ' + PROC.limit;
  }

  async function loadProcesses(silent) {
    const token = state.viewToken;
    const query = '/api/processes?sort=' + encodeURIComponent(PROC.sort) + '&limit=' + PROC.limit;
    if (!silent) {
      const table = $('#proc-table');
      if (table) table.innerHTML = '<div class="loading-box"><span class="spinner"></span><span>Читаю процессы…</span></div>';
    }
    const data = await api(query);
    if (!pageToken(token)) return;
    PROC.list = data.processes || [];
    procRender();
  }

  VIEWS.processes = async function () {
    state.processes.limit = PROC.limit;
    $('#view').innerHTML = processesShellHtml();
    await loadProcesses(false);
  };

  action('proc-sort', guarded(async function (node) {
    const sort = node.getAttribute('data-sort');
    if (!sort || sort === PROC.sort) return;
    PROC.sort = sort;
    const seg = $('.seg');
    if (seg) seg.innerHTML = procSortHtml();
    await loadProcesses(false);
  }));

  action('proc-refresh', guarded(async function () {
    await loadProcesses(false);
  }));

  onchange('proc-limit', guarded(async function (node) {
    PROC.limit = Math.max(10, Math.min(Number(node.value) || 60, 200));
    await loadProcesses(false);
  }));

  onchange('proc-filter', function (node) {
    state.processes.filter = node.value || '';
    procRender();
  });

  action('proc-kill', guarded(async function (node) {
    const pid = Number(node.getAttribute('data-pid'));
    const name = node.getAttribute('data-name') || 'процесс';
    if (!isNum(pid)) return;
    const confirmed = await confirmModal({
      title: 'Завершить процесс?',
      text: name + ' (PID ' + pid + ') будет остановлен. Несохранённые данные могут быть потеряны.',
      okText: 'Завершить',
      danger: true,
    });
    if (!confirmed) return;
    const result = await api('/api/processes/kill', { method: 'POST', json: { pid: pid, confirm: true } });
    toast((result && result.message) || 'Процесс завершён', 'ok');
    await loadProcesses(true);
  }));
// -------------------------------------------------------------------- apps
  const APPS = { apps: [], profiles: [], filter: '' };

  function appCardHtml(app) {
    const name = String(app.name || app.file || 'Приложение');
    const initial = name.trim().charAt(0).toUpperCase() || '?';
    const exists = app.exists !== false;
    return (
      '<article class="app-card' + (exists ? '' : ' missing') + '">' +
      '<div class="app-head"><div class="app-avatar">' + esc(initial) + '</div>' +
      '<div class="app-title"><strong title="' + esc(name) + '">' + esc(name) + '</strong>' +
      '<span>' + (exists ? 'Готово к запуску' : 'Файл не найден') + '</span></div></div>' +
      '<div class="app-path" title="' + esc(app.file || '') + '">' + esc(app.file || '—') + '</div>' +
      '<div class="app-foot">' +
      (exists ? badge('Найдено', 'ok') : badge('Нет файла', 'bad')) +
      '<button type="button" class="btn sm" data-act="app-launch" data-name="' + esc(app.name) + '"' +
      (exists ? '' : ' disabled') + '>' + icon('play') + '<span>Запустить</span></button>' +
      '</div></article>'
    );
  }

  function profileCardHtml(profile) {
    const key = String(profile.key || '');
    const description = String(profile.description || '');
    const apps = Array.isArray(profile.apps) ? profile.apps : [];
    const optional = Array.isArray(profile.optional) ? profile.optional : [];
    const chips = apps.map(function (name) {
      return '<span class="pill">' + esc(name) + '</span>';
    }).join('') + optional.map(function (name) {
      return '<span class="pill opt" title="Необязательно">' + esc(name) + '</span>';
    }).join('');
    return (
      '<article class="profile-card">' +
      '<div class="profile-head"><div><h4>' + esc(key) + '</h4>' +
      (description ? '<div class="profile-desc">' + esc(description) + '</div>' : '') + '</div>' +
      (profile.read_only ? badge('Обычный', 'subtle') : badge('Игровой', 'info')) + '</div>' +
      (chips ? '<div class="chip-list">' + chips + '</div>' : '<div class="profile-desc">Приложения не заданы.</div>') +
      '<div class="app-foot"><span class="profile-desc">' + apps.length + ' прил.</span>' +
      '<button type="button" class="btn sm" data-act="profile-launch" data-key="' + esc(key) + '"' +
      (apps.length ? '' : ' disabled') + '>' + icon('play') + '<span>Запустить профиль</span></button></div>' +
      '</article>'
    );
  }

  function appsGridHtml() {
    const list = APPS.apps.filter(function (app) {
      const query = APPS.filter.trim().toLowerCase();
      if (!query) return true;
      return String(app.name || '').toLowerCase().indexOf(query) >= 0 ||
        String(app.file || '').toLowerCase().indexOf(query) >= 0;
    });
    if (!list.length) {
      return emptyBox('apps', APPS.filter ? 'Ничего не найдено' : 'Приложения не настроены',
        APPS.filter ? 'Измените запрос.' : 'Добавьте приложения в настройках.');
    }
    return list.map(appCardHtml).join('');
  }

  function profilesGridHtml() {
    if (!APPS.profiles.length) {
      return emptyBox('apps', 'Профилей нет', 'Создайте профиль в настройках.');
    }
    return APPS.profiles.map(profileCardHtml).join('');
  }

  function appsShellHtml() {
    return (
      '<div class="stack">' +
      '<section class="card"><div class="card-head"><h3>Приложения</h3>' +
      '<div class="head-tools"><div class="files-search">' + icon('search') +
      '<input class="input" type="search" placeholder="Поиск приложения" data-change="apps-filter" value="' + esc(APPS.filter) + '"></div>' +
      '<button type="button" class="iconbtn sm" data-act="apps-refresh" title="Обновить">' + icon('refresh') + '</button></div></div>' +
      '<div class="app-grid" id="apps-grid"><div class="loading-box"><span class="spinner"></span><span>Читаю список…</span></div></div></section>' +
      '<section class="card"><div class="card-head"><h3>Профили запуска</h3>' +
      '<div class="head-tools"><button type="button" class="iconbtn sm" data-act="apps-refresh" title="Обновить">' + icon('refresh') + '</button></div></div>' +
      '<div class="profile-grid" id="profiles-grid"><div class="loading-box"><span class="spinner"></span><span>Читаю профили…</span></div></div></section>' +
      '</div>'
    );
  }

  async function loadApps(silent) {
    const token = state.viewToken;
    if (!silent) {
      const grid = $('#apps-grid');
      if (grid) grid.innerHTML = '<div class="loading-box"><span class="spinner"></span><span>Читаю список…</span></div>';
    }
    const results = await Promise.all([api('/api/apps'), api('/api/profiles')]);
    if (!pageToken(token)) return;
    APPS.apps = (results[0] && results[0].apps) || [];
    APPS.profiles = (results[1] && results[1].profiles) || [];
    const grid = $('#apps-grid');
    const profiles = $('#profiles-grid');
    if (grid) grid.innerHTML = appsGridHtml();
    if (profiles) profiles.innerHTML = profilesGridHtml();
  }

  VIEWS.apps = async function () {
    $('#view').innerHTML = appsShellHtml();
    await loadApps(false);
  };

  action('apps-refresh', guarded(async function () {
    await loadApps(false);
  }));

  onchange('apps-filter', function (node) {
    APPS.filter = node.value || '';
    const grid = $('#apps-grid');
    if (grid) grid.innerHTML = appsGridHtml();
  });

  action('app-launch', guarded(async function (node) {
    const name = node.getAttribute('data-name') || '';
    if (!name) return;
    node.disabled = true;
    try {
      const result = await api('/api/apps/launch', { method: 'POST', json: { name: name } });
      toast((result && result.message) || ('Запущено: ' + name), 'ok');
    } finally {
      node.disabled = false;
    }
  }));

  action('profile-launch', guarded(async function (node) {
    const key = node.getAttribute('data-key') || '';
    const profile = APPS.profiles.filter(function (item) { return item.key === key; })[0];
    if (!key) return;
    const confirmed = await confirmModal({
      title: 'Запустить профиль ' + key + '?',
      text: profile && profile.apps && profile.apps.length
        ? 'Будут запущены: ' + profile.apps.join(', ') + '.'
        : 'Профиль не содержит приложений.',
      okText: 'Запустить',
    });
    if (!confirmed) return;
    const result = await api('/api/profiles/launch', { method: 'POST', json: { key: key } });
    const started = (result && result.started) || [];
    const failed = (result && result.failed) || [];
    if (failed.length) {
      toast('Не удалось запустить: ' + failed.join(', '), 'warn', 6000);
    } else {
      toast('Запущено приложений: ' + started.length, 'ok');
    }
  }));

// ------------------------------------------------- power / media / tools
  const POWER = {
    status: null,
    actions: [],
    media: [],
    mediaAvailable: false,
    clipboard: null,
    clipboardAvailable: true,
    screenshotUrl: null,
  };

  const POWER_ICONS = {
    lock: 'lock',
    sleep: 'moon',
    hibernate: 'moon',
    display_off: 'monitor',
    cancel: 'x',
    shutdown: 'power',
    restart: 'refresh',
  };

  const MEDIA_ICONS = {
    vol_up: 'volume',
    vol_down: 'volume',
    mute: 'mute',
    play_pause: 'play',
    next: 'chevron',
    prev: 'chevron',
    stop: 'stop',
  };

  const MEDIA_LABELS = {
    vol_up: 'Громче',
    vol_down: 'Тише',
    mute: 'Без звука',
    play_pause: 'Пауза / играть',
    next: 'Следующий трек',
    prev: 'Предыдущий трек',
    stop: 'Стоп',
  };

  function powerActionsHtml() {
    if (!POWER.actions.length) {
      return emptyBox('power', 'Нет доступных действий', 'Управление питанием недоступно.');
    }
    return POWER.actions.map(function (item) {
      const cls = item.danger ? ' danger' : '';
      return (
        '<button type="button" class="action-tile' + cls + '" data-act="power-action" data-key="' + esc(item.key) + '">' +
        icon(POWER_ICONS[item.key] || 'power') +
        '<strong>' + esc(item.label) + '</strong>' +
        '</button>'
      );
    }).join('');
  }

  function mediaKeysHtml() {
    if (!POWER.mediaAvailable) {
      return emptyBox('power', 'Недоступно', 'Мультимедиа-клавиши работают только в Windows.');
    }
    if (!POWER.media.length) {
      return emptyBox('power', 'Нет команд', 'Список мультимедиа-команд пуст.');
    }
    return POWER.media.map(function (item) {
      return (
        '<button type="button" class="media-key" data-act="media-key" data-key="' + esc(item.key) + '">' +
        icon(MEDIA_ICONS[item.key] || 'play') +
        '<span>' + esc(MEDIA_LABELS[item.key] || item.label) + '</span>' +
        '</button>'
      );
    }).join('');
  }

  function clipboardHtml() {
    if (POWER.clipboardAvailable === false) {
      return '<div class="note warn">' + icon('alert') + '<span>Буфер обмена недоступен.</span></div>';
    }
    const text = POWER.clipboard && POWER.clipboard.text ? POWER.clipboard.text : '';
    return (
      '<div class="stack-sm">' +
      (POWER.clipboard && POWER.clipboard.truncated
        ? '<div class="note warn">' + icon('alert') + '<span>Текст обрезан до безопасной длины.</span></div>'
        : '') +
      '<textarea class="clipboard-box" id="clipboard-text" spellcheck="false" placeholder="Текст буфера обмена">' + esc(text) + '</textarea>' +
      '<div class="row-flex">' +
      '<button type="button" class="btn sm" data-act="clipboard-refresh">' + icon('refresh') + '<span>Прочитать</span></button>' +
      '<button type="button" class="btn sm" data-act="clipboard-save">' + icon('check') + '<span>Записать</span></button>' +
      '<button type="button" class="btn sm" data-act="clipboard-local">' + icon('copy') + '<span>Скопировать себе</span></button>' +
      '</div></div>'
    );
  }

  function screenshotHtml() {
    if (POWER.screenshotUrl) {
      return (
        '<div class="screenshot-box" id="screenshot-box">' +
        '<img id="screenshot-img" src="' + esc(POWER.screenshotUrl) + '" alt="Снимок экрана">' +
        '</div>'
      );
    }
    return (
      '<div class="screenshot-box" id="screenshot-box">' +
      emptyBox('monitor', 'Снимок не сделан', 'Нажмите «Сделать снимок», чтобы увидеть экран.') +
      '</div>'
    );
  }

  function powerShellHtml() {
    return (
      '<div class="stack">' +
      '<section class="card"><div class="card-head"><h3>Управление питанием</h3>' +
      '<div class="head-tools"><button type="button" class="iconbtn sm" data-act="power-refresh" title="Обновить">' + icon('refresh') + '</button></div></div>' +
      '<div class="action-grid" id="power-actions">' + powerActionsHtml() + '</div>' +
      '<div class="note">' + icon('info') + '<span>Выключение и перезагрузка выполняются с задержкой 30 секунд, показываются на экране ПК и могут быть отменены действием «Отменить таймер».</span></div></section>' +
      '<div class="grid cols-2">' +
      '<section class="card"><div class="card-head"><h3>Мультимедиа</h3></div>' +
      '<div class="media-grid" id="media-keys">' + mediaKeysHtml() + '</div></section>' +
      '<section class="card"><div class="card-head"><h3>Буфер обмена</h3></div>' +
      '<div id="clipboard-host">' + clipboardHtml() + '</div></section>' +
      '</div>' +
      '<section class="card"><div class="card-head"><h3>Снимок экрана</h3>' +
      '<div class="head-tools"><button type="button" class="btn sm primary" data-act="screenshot-take">' + icon('monitor') + '<span>Сделать снимок</span></button>' +
      (POWER.screenshotUrl ? '<button type="button" class="btn sm" data-act="screenshot-open">' + icon('download') + '<span>Открыть</span></button>' : '') +
      '</div></div>' +
      '<div id="screenshot-host">' + screenshotHtml() + '</div></section>' +
      '</div>'
    );
  }

  function renderPower() {
    const actions = $('#power-actions');
    if (actions) actions.innerHTML = powerActionsHtml();
    const media = $('#media-keys');
    if (media) media.innerHTML = mediaKeysHtml();
    const clip = $('#clipboard-host');
    if (clip) clip.innerHTML = clipboardHtml();
  }

  async function loadPower() {
    const token = state.viewToken;
    const results = await Promise.all([api('/api/power'), api('/api/media'), api('/api/clipboard')]);
    if (!pageToken(token)) return;
    POWER.status = results[0].status || null;
    POWER.actions = results[0].actions || [];
    POWER.media = results[1].keys || [];
    POWER.mediaAvailable = results[1].available !== false;
    POWER.clipboard = results[2] || null;
    POWER.clipboardAvailable = !results[2] || results[2].available !== false;
  }

  VIEWS.power = async function () {
    $('#view').innerHTML =
      '<div class="loading-box"><span class="spinner"></span><span>Читаю состояние…</span></div>';
    await loadPower();
    $('#view').innerHTML = powerShellHtml();
  };

  action('power-refresh', guarded(async function () {
    await loadPower();
    renderPower();
    toast('Состояние обновлено', 'ok');
  }));

  action('power-action', guarded(async function (node) {
    const key = node.getAttribute('data-key') || '';
    if (!key) return;
    const labels = { shutdown: 'выключить ПК', restart: 'перезагрузить ПК' };
    if (labels[key]) {
      const confirmed = await confirmModal({
        title: 'Точно ' + labels[key] + '?',
        text: 'Будет запущен таймер на 30 секунд. Отменить можно действием «Отменить таймер».',
        okText: key === 'shutdown' ? 'Выключить' : 'Перезагрузить',
        danger: true,
      });
      if (!confirmed) return;
    }
    const payload = { action: key };
    if (key === 'shutdown' || key === 'restart') payload.confirm = true;
    node.disabled = true;
    try {
      const result = await api('/api/power', { method: 'POST', json: payload });
      toast((result && result.message) || 'Готово', 'ok', 6000);
    } finally {
      node.disabled = false;
    }
  }));

  action('media-key', guarded(async function (node) {
    const key = node.getAttribute('data-key') || '';
    if (!key) return;
    await api('/api/media/key', { method: 'POST', json: { key: key } });
    toast(MEDIA_LABELS[key] || 'Команда отправлена', 'ok', 1800);
  }));

  action('clipboard-refresh', guarded(async function () {
    const result = await api('/api/clipboard');
    POWER.clipboard = result;
    POWER.clipboardAvailable = result.available !== false;
    const host = $('#clipboard-host');
    if (host) host.innerHTML = clipboardHtml();
  }));

  action('clipboard-save', guarded(async function () {
    const field = $('#clipboard-text');
    if (!field) return;
    const result = await api('/api/clipboard', { method: 'POST', json: { text: field.value } });
    toast((result && result.message) || 'Записано в буфер обмена', 'ok');
  }));

  action('clipboard-local', guarded(async function () {
    const field = $('#clipboard-text');
    if (!field) return;
    if (!field.value) {
      toast('Буфер обмена пуст', 'warn');
      return;
    }
    await copyText(field.value);
  }));

  function screenshotReset() {
    if (POWER.screenshotUrl) {
      URL.revokeObjectURL(POWER.screenshotUrl);
      POWER.screenshotUrl = null;
    }
  }

  action('screenshot-take', guarded(async function (node) {
    node.disabled = true;
    const host = $('#screenshot-host');
    if (host) host.innerHTML = '<div class="screenshot-box"><div class="loading-box"><span class="spinner"></span><span>Делаю снимок…</span></div></div>';
    try {
      const response = await fetch('/api/screenshot', { credentials: 'same-origin', cache: 'no-store' });
      if (response.status === 401) {
        handleLostSession();
        return;
      }
      if (!response.ok) {
        let message = 'Не удалось сделать снимок';
        try {
          const data = await response.json();
          message = data.error || data.message || message;
        } catch (error) { /* ignore */ }
        throw new ApiError(message, response.status, {});
      }
      const blob = await response.blob();
      screenshotReset();
      POWER.screenshotUrl = URL.createObjectURL(blob);
      if (host) host.innerHTML = screenshotHtml();
      toast('Снимок готов', 'ok');
    } catch (error) {
      if (host) host.innerHTML = screenshotHtml();
      throw error;
    } finally {
      node.disabled = false;
    }
  }));

  action('screenshot-open', function () {
    if (POWER.screenshotUrl) window.open(POWER.screenshotUrl, '_blank', 'noopener');
  });

// ------------------------------------------------------------- network
  const NET = {
    status: null,
    diag: null,
    speed: null,
    busy: {},
  };

  function diagTile(label, value, cls) {
    return (
      '<div class="diag-tile"><div class="diag-label">' + esc(label) + '</div>' +
      '<div class="diag-value ' + (cls || '') + '">' + esc(value == null || value === '' ? '—' : value) + '</div></div>'
    );
  }

  function ifaceCardHtml(item) {
    const flags = [];
    if (item.vpn) flags.push(badge('VPN', 'info'));
    flags.push(badge(item.is_up ? 'Активен' : 'Отключён', item.is_up ? 'ok' : 'subtle'));
    const ips = (item.ipv4 || []).join(', ') || 'нет IPv4';
    const meta = [];
    if (item.mac) meta.push(item.mac);
    if (item.speed_mbps) meta.push(num(item.speed_mbps, 0) + ' Мбит/с');
    return (
      '<div class="iface-card"><div class="iface-head">' +
      '<strong title="' + esc(item.name) + '">' + esc(item.name) + '</strong>' +
      '<div class="row-flex">' + flags.join('') + '</div></div>' +
      '<div class="iface-ip">' + esc(ips) + '</div>' +
      '<div class="iface-meta">' + esc(meta.join(' · ') || '—') + '</div></div>'
    );
  }

  function netInterfacesHtml() {
    const status = NET.status;
    if (!status) return emptyBox('network', 'Нет данных', 'Не удалось прочитать сетевые интерфейсы.');
    const list = status.interfaces || [];
    if (!list.length) return emptyBox('network', 'Интерфейсы не найдены', 'Система не вернула ни одного интерфейса.');
    return '<div class="iface-grid">' + list.map(ifaceCardHtml).join('') + '</div>';
  }

  function netSummaryHtml() {
    const status = NET.status || {};
    const active = status.active || [];
    const vpn = status.vpn_active || [];
    const wifi = status.wifi;
    const tiles = [
      diagTile('Хост', status.hostname || '—'),
      diagTile('Активных', String(active.length), active.length ? 'ok' : 'bad'),
      diagTile('VPN', vpn.length ? vpn[0].name : 'не подключён', vpn.length ? 'ok' : ''),
      diagTile('Wi-Fi', wifi ? wifi.ssid : 'нет', wifi ? 'ok' : ''),
      diagTile('Публичный IP', status.public_ip || 'недоступен'),
    ];
    return '<div class="diag-grid">' + tiles.join('') + '</div>';
  }

  function lookupsText(lookups) {
    if (!Array.isArray(lookups) || !lookups.length) return '—';
    return lookups
      .map(function (item) {
        if (!item) return '';
        if (item.ok) return item.host + ' → ' + item.address + ' (' + num(item.seconds, 2) + ' с)';
        return item.host + ' → ошибка' + (item.error ? ': ' + item.error : '');
      })
      .filter(Boolean)
      .join('\n');
  }

  function pingText(result) {
    if (!result) return 'Нет данных';
    const lines = ['Хост: ' + (result.host || '—')];
    if (result.ok) {
      lines.push('Потери: ' + (isNum(result.loss_percent) ? num(result.loss_percent, 0) + '%' : '—'));
      lines.push('Средний пинг: ' + (isNum(result.avg_ms) ? num(result.avg_ms, 1) + ' мс' : '—'));
      lines.push('Статус: доступен');
    } else {
      lines.push('Статус: недоступен');
      if (isNum(result.loss_percent)) lines.push('Потери: ' + num(result.loss_percent, 0) + '%');
      if (result.error) lines.push('Ошибка: ' + result.error);
    }
    return lines.join('\n');
  }

  function dnsText(result) {
    if (!result) return 'Нет данных';
    const lines = ['Имя: ' + (result.host || '—')];
    lines.push('Адрес: ' + (result.address || 'не найден'));
    lines.push('Время: ' + (isNum(result.seconds) ? num(result.seconds, 3) + ' с' : '—'));
    lines.push('Статус: ' + (result.ok ? 'успешно' : 'ошибка'));
    if (result.error) lines.push('Ошибка: ' + result.error);
    return lines.join('\n');
  }

  function traceText(host, hops) {
    if (!Array.isArray(hops) || !hops.length) return 'Маршрут не получен' + (host ? ' до ' + host : '');
    return hops
      .map(function (hop) {
        return (
          String(hop.hop == null ? '?' : hop.hop).padStart(2, ' ') + '. ' +
          (hop.address || '*') +
          (isNum(hop.ms) ? '  ' + num(hop.ms, 1) + ' мс' : '')
        );
      })
      .join('\n');
  }

  function netDiagHtml() {
    const diag = NET.diag;
    if (!diag) {
      return '<div class="loading-box"><span class="spinner"></span><span>Проверяю сеть…</span></div>';
    }
    const gatewayPing = diag.gateway_ping;
    const internetPing = diag.internet_ping;
    const tiles = [
      diagTile('Шлюз', diag.gateway || '—'),
      diagTile(
        'Пинг шлюза',
        gatewayPing && isNum(gatewayPing.avg_ms) ? num(gatewayPing.avg_ms, 1) + ' мс' : 'нет ответа',
        gatewayPing && gatewayPing.ok ? 'ok' : 'bad'
      ),
      diagTile(
        'Интернет',
        internetPing && isNum(internetPing.avg_ms) ? num(internetPing.avg_ms, 1) + ' мс' : 'нет ответа',
        internetPing && internetPing.ok ? 'ok' : 'bad'
      ),
      diagTile('DNS-серверы', (diag.dns_servers || []).join(', ') || '—'),
    ];
    return (
      '<div class="diag-grid">' + tiles.join('') + '</div>' +
      '<div class="result-box">' + esc(lookupsText(diag.lookups)) + '</div>'
    );
  }

  function speedHtml() {
    const speed = NET.speed;
    if (NET.busy.speed) {
      return '<div class="loading-box"><span class="spinner"></span><span>Измеряю скорость, это занимает до 20 секунд…</span></div>';
    }
    if (!speed) {
      return '<div class="note">' + icon('info') + '<span>Тест скачивает и отправляет небольшой объём данных через Cloudflare. Запускайте вручную.</span></div>';
    }
    const tiles = [
      '<div class="speed-tile"><b>' + (isNum(speed.download_mbps) ? num(speed.download_mbps, 2) : '—') + '</b><span>Скачивание, Мбит/с</span></div>',
      '<div class="speed-tile"><b>' + (isNum(speed.upload_mbps) ? num(speed.upload_mbps, 2) : '—') + '</b><span>Отдача, Мбит/с</span></div>',
      '<div class="speed-tile"><b>' + esc(speed.server || '—') + '</b><span>Сервер</span></div>',
    ];
    let html = '<div class="speed-grid">' + tiles.join('') + '</div>';
    if (speed.errors && speed.errors.length) {
      html += '<div class="note danger">' + icon('alert') + '<span>' + esc(speed.errors.join('; ')) + '</span></div>';
    }
    return html;
  }

  function netShellHtml() {
    return (
      '<div class="stack">' +
      card(
        'Сетевые интерфейсы',
        '<div id="net-summary">' + netSummaryHtml() + '</div><div class="divider"></div><div id="net-ifaces">' + netInterfacesHtml() + '</div>',
        '<button type="button" class="iconbtn sm" data-act="net-refresh" title="Обновить">' + icon('refresh') + '</button>'
      ) +
      card(
        'Диагностика',
        '<div id="net-diag">' + netDiagHtml() + '</div>',
        '<button type="button" class="btn sm" data-act="net-diag">' + icon('shield') + '<span>Проверить</span></button>'
      ) +
      '<section class="card"><div class="card-head"><h3>Инструменты</h3></div><div class="grid cols-3">' +
      '<div class="tool-block"><h4>Ping</h4><div class="input-group"><input class="input" id="ping-host" value="1.1.1.1" spellcheck="false" aria-label="Хост для ping"><button type="button" class="btn sm primary" data-act="net-ping">Пинг</button></div><div class="result-box" id="ping-result"></div></div>' +
      '<div class="tool-block"><h4>DNS</h4><div class="input-group"><input class="input" id="dns-host" value="example.com" spellcheck="false" aria-label="Имя для DNS"><button type="button" class="btn sm primary" data-act="net-dns">Проверить</button></div><div class="result-box" id="dns-result"></div></div>' +
      '<div class="tool-block"><h4>Traceroute</h4><div class="input-group"><input class="input" id="trace-host" value="1.1.1.1" spellcheck="false" aria-label="Хост для traceroute"><button type="button" class="btn sm primary" data-act="net-trace">Запустить</button></div><div class="result-box" id="trace-result"></div></div>' +
      '</div></section>' +
      card(
        'Скорость интернета',
        '<div id="speed-body">' + speedHtml() + '</div>',
        '<button type="button" class="btn sm primary" data-act="net-speedtest">' + icon('play') + '<span>Запустить тест</span></button>'
      ) +
      '</div>'
    );
  }

  function renderNet() {
    const summary = $('#net-summary');
    if (summary) summary.innerHTML = netSummaryHtml();
    const ifaces = $('#net-ifaces');
    if (ifaces) ifaces.innerHTML = netInterfacesHtml();
    const diag = $('#net-diag');
    if (diag) diag.innerHTML = netDiagHtml();
    const speed = $('#speed-body');
    if (speed) speed.innerHTML = speedHtml();
  }

  async function loadNetwork() {
    const token = state.viewToken;
    const status = await api('/api/network');
    if (!pageToken(token)) return;
    NET.status = status;
  }

  VIEWS.network = async function () {
    $('#view').innerHTML =
      '<div class="loading-box"><span class="spinner"></span><span>Читаю сеть…</span></div>';
    await loadNetwork();
    $('#view').innerHTML = netShellHtml();
  };

  action('net-refresh', guarded(async function (node) {
    node.disabled = true;
    try {
      await loadNetwork();
      renderNet();
      toast('Сеть обновлена', 'ok');
    } finally {
      node.disabled = false;
    }
  }));

  action('net-diag', guarded(async function (node) {
    node.disabled = true;
    NET.diag = null;
    const box = $('#net-diag');
    if (box) box.innerHTML = netDiagHtml();
    try {
      const payload = await api('/api/network/diagnostics', { method: 'POST', json: {} });
      NET.diag = payload.result || null;
    } finally {
      node.disabled = false;
      const host = $('#net-diag');
      if (host) host.innerHTML = netDiagHtml();
    }
  }));

  function toolHost(inputId, fallback) {
    const field = $(inputId);
    const value = field && field.value ? field.value.trim() : '';
    return value || fallback;
  }

  async function toolRequest(path, host, resultId, formatter, defaultHost) {
    const box = $(resultId);
    const target = host || defaultHost;
    if (box) box.textContent = 'Выполняю…';
    const payload = await api(path, { method: 'POST', json: { host: target } });
    if (box) box.textContent = formatter(target, payload);
  }

  action('net-ping', guarded(async function () {
    const host = toolHost('#ping-host', '1.1.1.1');
    await toolRequest('/api/network/ping', host, '#ping-result', function (target, payload) {
      return pingText(payload.result);
    });
  }));

  action('net-dns', guarded(async function () {
    const host = toolHost('#dns-host', 'example.com');
    await toolRequest('/api/network/dns', host, '#dns-result', function (target, payload) {
      return dnsText(payload.result);
    });
  }));

  action('net-trace', guarded(async function () {
    const host = toolHost('#trace-host', '1.1.1.1');
    await toolRequest('/api/network/traceroute', host, '#trace-result', function (target, payload) {
      return traceText(payload.host || target, payload.hops);
    });
  }));

  action('net-speedtest', guarded(async function (node) {
    if (NET.busy.speed) return;
    NET.busy.speed = true;
    NET.speed = null;
    node.disabled = true;
    const body = $('#speed-body');
    if (body) body.innerHTML = speedHtml();
    try {
      const payload = await api('/api/network/speedtest', { method: 'POST', json: {} });
      NET.speed = payload.result || null;
      toast('Тест скорости завершён', 'ok');
    } finally {
      NET.busy.speed = false;
      node.disabled = false;
      const host = $('#speed-body');
      if (host) host.innerHTML = speedHtml();
    }
  }));

// ----------------------------------------------------------- scheduler
  const SCHED_LABELS = {
    shutdown: 'Выключение',
    restart: 'Перезагрузка',
    sleep: 'Спящий режим',
    lock: 'Блокировка',
    profile: 'Запуск профиля',
  };

  const SCHED = { tasks: [], actions: [], profiles: [], available: true, message: '' };

  function countdownText(seconds) {
    if (!isNum(seconds)) return '—';
    const total = Math.max(0, Math.floor(seconds));
    if (total < 60) return total + ' с';
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + ' мин ' + (total % 60) + ' с';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' ч ' + (minutes % 60) + ' мин';
    return Math.floor(hours / 24) + ' д ' + (hours % 24) + ' ч';
  }

  function schedTaskRow(task) {
    const params = task.params || {};
    const actionLabel = SCHED_LABELS[task.action] || task.action || '—';
    let title = actionLabel;
    if (task.action === 'profile' && params.profile) title += ': ' + params.profile;
    const note = task.label ? '<div class="app-path">' + esc(task.label) + '</div>' : '';
    return (
      '<tr><td><strong>' + esc(title) + '</strong>' + note + '</td>' +
      '<td class="task-time">' + esc(task.run_at_text || '—') + '</td>' +
      '<td class="task-time"><span class="countdown" data-run-at="' + esc(task.run_at) + '">' +
      esc(countdownText(task.remaining)) + '</span></td>' +
      '<td class="actions"><button type="button" class="iconbtn sm" data-act="sched-cancel" data-id="' +
      esc(task.id) + '" title="Отменить">' + icon('x') + '</button></td></tr>'
    );
  }

  function schedTasksHtml() {
    if (!SCHED.available) {
      return '<div class="note warn">' + icon('alert') + '<span>' + esc(SCHED.message || 'Планировщик недоступен.') + '</span></div>';
    }
    if (!SCHED.tasks.length) {
      return emptyBox('scheduler', 'Нет задач', 'Запланируйте выключение, перезагрузку или запуск профиля.');
    }
    return (
      '<div class="table-wrap"><table class="table"><thead><tr>' +
      '<th>Действие</th><th>Время</th><th>Осталось</th><th class="actions"></th>' +
      '</tr></thead><tbody>' + SCHED.tasks.map(schedTaskRow).join('') + '</tbody></table></div>'
    );
  }

  function schedProfileOptions() {
    if (!SCHED.profiles.length) return '<option value="">Профили не найдены</option>';
    return '<option value="">Выберите профиль</option>' + SCHED.profiles
      .map(function (profile) {
        return '<option value="' + esc(profile.key) + '">' + esc(profile.key) +
          (profile.description ? ' — ' + esc(profile.description) : '') + '</option>';
      })
      .join('');
  }

  function schedActionOptions() {
    const actions = SCHED.actions.length ? SCHED.actions : Object.keys(SCHED_LABELS);
    return actions
      .map(function (name) {
        return '<option value="' + esc(name) + '">' + esc(SCHED_LABELS[name] || name) + '</option>';
      })
      .join('');
  }

  function schedulerShellHtml() {
    return (
      '<div class="stack">' +
      card(
        'Запланированные задачи',
        '<div id="sched-tasks">' + schedTasksHtml() + '</div>',
        '<button type="button" class="iconbtn sm" data-act="sched-refresh" title="Обновить">' + icon('refresh') + '</button>'
      ) +
      card(
        'Новая задача',
        '<form class="schedule-form" data-form="sched-add">' +
        '<div class="form-grid">' +
        '<label class="field"><span>Действие</span><select class="select" name="action" data-change="sched-action">' +
        schedActionOptions() + '</select></label>' +
        '<label class="field"><span>Через, минут</span><input class="input" type="number" name="minutes" min="1" step="1" value="30"></label>' +
        '<label class="field"><span>Или точное время</span><input class="input" type="datetime-local" name="run_at"></label>' +
        '<label class="field"><span>Подпись</span><input class="input" name="label" maxlength="60" placeholder="необязательно" autocomplete="off"></label>' +
        '</div>' +
        '<label class="field" id="sched-profile-wrap" hidden><span>Профиль</span><select class="select" name="profile">' +
        schedProfileOptions() + '</select></label>' +
        '<div class="form-actions"><button type="submit" class="btn primary">' + icon('plus') + '<span>Добавить</span></button>' +
        '<span class="note">Укажите задержку или точное время — не оба поля сразу.</span></div>' +
        '</form>'
      ) +
      '</div>'
    );
  }

  function schedTicker() {
    let finished = false;
    $$('.countdown[data-run-at]').forEach(function (node) {
      const runAt = parseFloat(node.getAttribute('data-run-at'));
      if (!Number.isFinite(runAt)) return;
      const left = Math.max(0, Math.round(runAt - Date.now() / 1000));
      node.textContent = countdownText(left);
      if (left <= 0 && node.getAttribute('data-expired') !== '1') {
        node.setAttribute('data-expired', '1');
        finished = true;
      }
    });
    if (finished) {
      loadScheduler()
        .then(function () {
          const box = $('#sched-tasks');
          if (box) box.innerHTML = schedTasksHtml();
        })
        .catch(function () { /* keep the stale list on error */ });
    }
  }

  async function loadScheduler(silent) {
    const token = state.viewToken;
    if (!silent) {
      const box = $('#sched-tasks');
      if (box) box.innerHTML = '<div class="loading-box"><span class="spinner"></span><span>Читаю задачи…</span></div>';
    }
    const payload = await api('/api/scheduler');
    if (!pageToken(token)) return;
    SCHED.available = payload.ok !== false;
    SCHED.message = payload.message || '';
    SCHED.tasks = payload.tasks || [];
    if (payload.actions && payload.actions.length) SCHED.actions = payload.actions;
  }

  VIEWS.scheduler = async function (token) {
    const results = await Promise.all([api('/api/scheduler'), api('/api/profiles')]);
    if (!pageToken(token)) return;
    SCHED.available = results[0].ok !== false;
    SCHED.message = results[0].message || '';
    SCHED.tasks = results[0].tasks || [];
    SCHED.actions = results[0].actions || [];
    SCHED.profiles = (results[1] && results[1].profiles) || [];
    $('#view').innerHTML = schedulerShellHtml();
    state.timers.push(setInterval(schedTicker, 1000));
  };

  action('sched-refresh', guarded(async function (node) {
    node.disabled = true;
    try {
      await loadScheduler(false);
      const box = $('#sched-tasks');
      if (box) box.innerHTML = schedTasksHtml();
    } finally {
      node.disabled = false;
    }
  }));

  action('sched-cancel', guarded(async function (node) {
    const id = node.getAttribute('data-id');
    if (!id) return;
    const confirmed = await confirmModal({
      title: 'Отменить задачу?',
      text: 'Задача будет удалена из планировщика.',
      okText: 'Отменить задачу',
      danger: true,
    });
    if (!confirmed) return;
    await api('/api/scheduler/cancel', { method: 'POST', json: { id: Number(id) } });
    await loadScheduler(true);
    const box = $('#sched-tasks');
    if (box) box.innerHTML = schedTasksHtml();
    toast('Задача отменена', 'ok');
  }));

  onchange('sched-action', function (node) {
    const wrap = $('#sched-profile-wrap');
    if (!wrap) return;
    const isProfile = node.value === 'profile';
    wrap.hidden = !isProfile;
    const select = wrap.querySelector('select[name="profile"]');
    if (select) select.required = isProfile;
  });

  FORM['sched-add'] = async function (form) {
    const actionField = form.querySelector('[name="action"]');
    const minutesField = form.querySelector('[name="minutes"]');
    const atField = form.querySelector('[name="run_at"]');
    const labelField = form.querySelector('[name="label"]');
    const action = actionField ? actionField.value : '';
    if (!action) throw new Error('Выберите действие');

    const payload = { action: action };
    const atRaw = atField && atField.value ? atField.value : '';
    const minutesRaw = minutesField && minutesField.value ? minutesField.value.trim() : '';
    if (atRaw) {
      const stamp = new Date(atRaw).getTime();
      if (!Number.isFinite(stamp)) throw new Error('Некорректное время');
      payload.run_at = Math.floor(stamp / 1000);
      if (payload.run_at <= Date.now() / 1000 + 1) throw new Error('Время уже прошло');
    } else if (minutesRaw) {
      const minutes = parseFloat(minutesRaw);
      if (!Number.isFinite(minutes) || minutes <= 0) throw new Error('Укажите задержку в минутах');
      payload.delay_seconds = Math.round(minutes * 60);
    } else {
      throw new Error('Укажите задержку или точное время');
    }

    if (action === 'profile') {
      const profileField = form.querySelector('[name="profile"]');
      const key = profileField ? profileField.value.trim() : '';
      if (!key) throw new Error('Выберите профиль');
      payload.profile = key;
    }
    const label = labelField ? labelField.value.trim() : '';
    if (label) payload.label = label;

    await api('/api/scheduler/add', { method: 'POST', json: payload });
    form.reset();
    const wrap = $('#sched-profile-wrap');
    if (wrap) wrap.hidden = true;
    await loadScheduler(true);
    const box = $('#sched-tasks');
    if (box) box.innerHTML = schedTasksHtml();
    toast('Задача добавлена', 'ok');
  };

// --------------------------------------------------------------- settings
  const SETTINGS_TABS = [
    { key: 'apps', label: 'Приложения' },
    { key: 'alerts', label: 'Мониторинг' },
    { key: 'notifications', label: 'Уведомления' },
    { key: 'profiles', label: 'Профили' },
  ];

  const ALERT_FIELDS = [
    { key: 'cpu_temperature', label: 'Температура CPU', unit: '°C', step: '1' },
    { key: 'gpu_temperature', label: 'Температура GPU', unit: '°C', step: '1' },
    { key: 'ram_utilization', label: 'Загрузка RAM', unit: '%', step: '1' },
    { key: 'disk_utilization', label: 'Заполнение диска', unit: '%', step: '1' },
  ];

  const NOTIFY_TOGGLES = [
    { key: 'notify_login', label: 'Вход в систему' },
    { key: 'notify_battery', label: 'Батарея' },
    { key: 'notify_apps', label: 'Запуск приложений' },
    { key: 'notify_crashes', label: 'Падения приложений' },
  ];

  function settingsSection(name) {
    const sections = state.settings || {};
    const value = sections[name];
    return value && typeof value === 'object' ? value : {};
  }

  function settingsNumber(value, digits) {
    if (!isNum(value)) return '';
    return String(digits == null ? value : Math.round(value * Math.pow(10, digits)) / Math.pow(10, digits));
  }

  function appEditRow(name, path) {
    return (
      '<div class="app-edit-row" data-app-row>' +
      '<input class="input" name="app_name" value="' + esc(name || '') + '" placeholder="Название" ' +
      'maxlength="40" autocomplete="off" spellcheck="false">' +
      '<input class="input app-path-input" name="app_path" value="' + esc(path || '') + '" ' +
      'placeholder="C:\\Путь\\к\\программе.exe" autocomplete="off" spellcheck="false">' +
      '<button type="button" class="iconbtn sm" data-act="settings-app-remove" title="Удалить" ' +
      'aria-label="Удалить">' + icon('trash') + '</button>' +
      '</div>'
    );
  }

  function appsSettingsCard() {
    const apps = settingsSection('applications');
    const names = Object.keys(apps).sort(function (a, b) {
      return a.localeCompare(b, 'ru');
    });
    const rows = names.map(function (name) { return appEditRow(name, apps[name]); }).join('');
    const body =
      '<form data-form="settings-save" data-section="applications">' +
      '<p class="note">' + icon('info') +
      '<span>Названия и пути видны только в панели и не публикуются в чате.</span></p>' +
      '<div class="form-section-title">Список приложений</div>' +
      '<div class="app-edit-list" id="app-edit-list">' + rows + '</div>' +
      '<div class="form-actions">' +
      '<button type="button" class="btn" data-act="settings-app-add">' + icon('plus') +
      '<span>Добавить</span></button>' +
      '<button type="submit" class="btn primary">Сохранить</button>' +
      '</div></form>';
    const tools = '<span class="pill">' + names.length + ' шт.</span>';
    return card('Приложения', body, tools, 'settings-card');
  }

  function alertsSettingsCard() {
    const alerts = settingsSection('alerts');
    const thresholds = alerts.thresholds || {};
    const cooldowns = alerts.cooldowns || {};
    const interval = isNum(alerts.interval_seconds) ? alerts.interval_seconds : 60;

    const thresholdFields = ALERT_FIELDS.map(function (field) {
      return (
        '<label class="field"><span>' + esc(field.label) + ', ' + esc(field.unit) + '</span>' +
        '<input class="input" type="number" min="0" step="' + esc(field.step) + '" ' +
        'name="threshold_' + esc(field.key) + '" value="' +
        esc(settingsNumber(thresholds[field.key])) + '"></label>'
      );
    }).join('');

    const cooldownFields = ALERT_FIELDS.map(function (field) {
      const value = cooldowns[field.key];
      const never = value == null || !Number.isFinite(value);
      const minutes = never ? '' : settingsNumber(value / 60, 1);
      return (
        '<div class="cooldown-item">' +
        '<div class="cooldown-head"><span>' + esc(field.label) + '</span>' +
        '<label class="check"><input type="checkbox" name="never_' + esc(field.key) + '"' +
        (never ? ' checked' : '') + ' data-change="settings-never"><span>Не повторять</span></label></div>' +
        '<label class="field"><span>Повтор, мин</span>' +
        '<input class="input" type="number" min="0" step="1" name="cooldown_' + esc(field.key) + '" value="' +
        esc(minutes) + '"' + (never ? ' disabled' : '') + '></label>' +
        '</div>'
      );
    }).join('');

    const body =
      '<form data-form="settings-save" data-section="alerts">' +
      '<div class="form-section-title">Пороги</div>' +
      '<div class="form-grid">' + thresholdFields +
      '<label class="field"><span>Интервал проверки, сек</span>' +
      '<input class="input" type="number" min="15" max="86400" step="1" name="interval_seconds" value="' +
      esc(settingsNumber(interval)) + '"></label>' +
      '</div>' +
      '<div class="form-section-title">Повтор уведомлений</div>' +
      '<div class="cooldown-grid">' + cooldownFields + '</div>' +
      '<div class="form-actions"><button type="submit" class="btn primary">Сохранить</button></div>' +
      '</form>';
    return card('Мониторинг', body, '', 'settings-card');
  }

  function notifyNumberField(name, label, value, low, high, step) {
    return (
      '<label class="field"><span>' + esc(label) + '</span>' +
      '<input class="input" type="number" min="' + low + '" max="' + high + '" step="' + (step || '1') +
      '" name="' + esc(name) + '" value="' + esc(settingsNumber(value)) + '"></label>'
    );
  }

  function notificationsSettingsCard() {
    const notify = settingsSection('notifications');
    const quiet = notify.quiet_hours || {};
    const warning = isNum(notify.battery_warning) ? notify.battery_warning : 20;
    const critical = isNum(notify.battery_critical) ? notify.battery_critical : 10;
    const appCooldown = isNum(notify.app_cooldown_minutes) ? notify.app_cooldown_minutes : 5;
    const crashCooldown = isNum(notify.crash_cooldown_minutes) ? notify.crash_cooldown_minutes : 10;

    const toggles = NOTIFY_TOGGLES.map(function (item) {
      const on = notify[item.key] !== false;
      return (
        '<label class="switch toggle-item"><span>' + esc(item.label) + '</span>' +
        '<input type="checkbox" name="' + esc(item.key) + '"' + (on ? ' checked' : '') + '>' +
        '<span class="track"></span></label>'
      );
    }).join('');

    const body =
      '<form data-form="settings-save" data-section="notifications">' +
      '<div class="form-section-title">Батарея</div>' +
      '<div class="form-grid">' +
      notifyNumberField('battery_warning', 'Предупреждение, %', warning, 5, 100) +
      notifyNumberField('battery_critical', 'Критично, %', critical, 1, 100) +
      notifyNumberField('app_cooldown_minutes', 'Повтор запуска, мин', appCooldown, 0, 1440) +
      notifyNumberField('crash_cooldown_minutes', 'Повтор падения, мин', crashCooldown, 0, 1440) +
      '</div>' +
      '<div class="form-section-title">События</div>' +
      '<div class="toggle-grid">' + toggles + '</div>' +
      '<div class="form-section-title">Тихие часы</div>' +
      '<div class="form-grid">' +
      '<label class="switch toggle-item"><span>Не беспокоить</span>' +
      '<input type="checkbox" name="quiet_enabled"' + (quiet.enabled ? ' checked' : '') + '>' +
      '<span class="track"></span></label>' +
      '<label class="field"><span>С</span><input class="input" type="time" name="quiet_start" value="' +
      esc(quiet.start || '23:00') + '"></label>' +
      '<label class="field"><span>До</span><input class="input" type="time" name="quiet_end" value="' +
      esc(quiet.end || '07:00') + '"></label>' +
      '</div>' +
      '<p class="note warn">' + icon('alert') +
      '<span>В тихие часы приходят только критичные уведомления о батарее.</span></p>' +
      '<div class="form-actions"><button type="submit" class="btn primary">Сохранить</button></div>' +
      '</form>';
    return card('Уведомления', body, '', 'settings-card');
  }

  function profilesSettingsCard() {
    const profiles = settingsSection('profiles');
    const text = JSON.stringify(profiles, null, 2);
    const body =
      '<form data-form="settings-save" data-section="profiles">' +
      '<p class="note">' + icon('info') +
      '<span>Имя профиля, описание, обязательные и необязательные приложения.</span></p>' +
      '<label class="field"><span>JSON</span>' +
      '<textarea class="textarea" name="profiles_json" rows="18" spellcheck="false">' +
      esc(text) + '</textarea></label>' +
      '<div class="form-actions"><button type="submit" class="btn primary">Сохранить</button></div>' +
      '</form>';
    return card('Профили', body, '', 'settings-card');
  }

function settingsTabsHtml() {
    const buttons = SETTINGS_TABS.map(function (tab) {
      return (
        '<button type="button" data-act="settings-tab" data-tab="' + tab.key + '"' +
        (state.settingsTab === tab.key ? ' class="active"' : '') + '>' + esc(tab.label) + '</button>'
      );
    }).join('');
    return '<div class="tabs settings-tabs">' + buttons + '</div>';
  }

  function settingsTabHtml() {
    if (state.settingsTab === 'alerts') return alertsSettingsCard();
    if (state.settingsTab === 'notifications') return notificationsSettingsCard();
    if (state.settingsTab === 'profiles') return profilesSettingsCard();
    return appsSettingsCard();
  }

  function renderSettingsTab() {
    const body = $('#settings-body');
    if (body) body.innerHTML = settingsTabHtml();
    $$('.settings-tabs button[data-tab]').forEach(function (node) {
      node.classList.toggle('active', node.getAttribute('data-tab') === state.settingsTab);
    });
  }

  function kvRow(label, value) {
    return '<div class="kv-row"><dt>' + esc(label) + '</dt><dd>' + value + '</dd></div>';
  }

  function urlCopyRow(url) {
    return (
      '<span class="row-flex"><code>' + esc(url) + '</code>' +
      '<button type="button" class="iconbtn sm" data-act="settings-copy" data-text="' + esc(url) +
      '" title="Копировать" aria-label="Копировать">' + icon('copy') + '</button></span>'
    );
  }

  function dataSettingsCard() {
    const body =
      '<p class="note warn">' + icon('alert') +
      '<span>Экспорт содержит локальные пути. Храните файл только на своём ПК.</span></p>' +
      '<div class="data-actions">' +
      '<button type="button" class="btn" data-act="settings-export">' + icon('download') +
      '<span>Экспорт</span></button>' +
      '<button type="button" class="btn" data-act="settings-import">' + icon('upload') +
      '<span>Импорт</span></button>' +
      '<button type="button" class="btn danger" data-act="settings-reset">' + icon('refresh') +
      '<span>Сбросить</span></button>' +
      '<input type="file" id="settings-import-file" accept="application/json,.json" hidden ' +
      'data-change="settings-import-file">' +
      '</div>';
    return card('Данные и резервные копии', body, '', 'settings-card');
  }

  function securitySettingsCard() {
    const body =
      '<form data-form="settings-password">' +
      '<div class="form-grid">' +
      '<label class="field"><span>Текущий пароль</span>' +
      '<input class="input" type="password" name="current" autocomplete="current-password" required></label>' +
      '<label class="field"><span>Новый пароль</span>' +
      '<input class="input" type="password" name="next" autocomplete="new-password" ' +
      'minlength="8" maxlength="200" required></label>' +
      '<label class="field"><span>Повторите новый</span>' +
      '<input class="input" type="password" name="repeat" autocomplete="new-password" ' +
      'minlength="8" maxlength="200" required></label>' +
      '</div>' +
      '<div class="form-actions"><button class="btn primary" type="submit">Изменить пароль</button></div>' +
      '</form>';
    return card('Безопасность', body, '', 'settings-card');
  }

  function panelSettingsCard() {
    const panel = state.panel || {};
    const urls = panel.urls || {};
    const rows = [];
    rows.push(kvRow('Адрес', esc(panel.host || '0.0.0.0') + ':' + esc(panel.port == null ? '' : panel.port)));
    if (urls.local) rows.push(kvRow('Локально', urlCopyRow(urls.local)));
    if (urls.lan) rows.push(kvRow('В сети', urlCopyRow(urls.lan)));
    rows.push(kvRow('Запущена', esc(isNum(panel.started_at) ? fmtTime(panel.started_at) : '—')));
    rows.push(kvRow('Telegram', telegramBadge()));
    const body =
      '<dl class="kv">' + rows.join('') + '</dl>' +
      '<p class="note">' + icon('shield') +
      '<span>Панель работает только в локальной сети. Не открывайте её порт в интернет.</span></p>';
    return card('Панель', body, '', 'settings-card');
  }

  function settingsShellHtml() {
    return (
      '<div class="settings-panel">' +
      settingsTabsHtml() +
      '<div id="settings-body">' + settingsTabHtml() + '</div>' +
      dataSettingsCard() +
      securitySettingsCard() +
      panelSettingsCard() +
      '</div>'
    );
  }

// ------------------------------------------------------ settings validation
  function readNumberField(form, name, label, low, high) {
    const input = form.querySelector('[name="' + name + '"]');
    if (!input) throw new Error('Поле «' + label + '» не найдено');
    const raw = String(input.value == null ? '' : input.value).replace(',', '.').trim();
    if (!raw) throw new Error('Заполните поле «' + label + '»');
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error('Некорректное значение: ' + label);
    if (low != null && value < low) throw new Error(label + ': минимум ' + low);
    if (high != null && value > high) throw new Error(label + ': максимум ' + high);
    return value;
  }

  function buildApplications(form) {
    const result = {};
    $$('[data-app-row]', form).forEach(function (row) {
      const nameInput = row.querySelector('[name="app_name"]');
      const pathInput = row.querySelector('[name="app_path"]');
      const name = nameInput ? nameInput.value.trim() : '';
      const path = pathInput ? pathInput.value.trim() : '';
      if (!name && !path) return;
      if (!name) throw new Error('Укажите название приложения');
      if (!path) throw new Error('Укажите путь для «' + name + '»');
      if (Object.prototype.hasOwnProperty.call(result, name)) {
        throw new Error('Название повторяется: ' + name);
      }
      result[name] = path;
    });
    if (!Object.keys(result).length) throw new Error('Добавьте хотя бы одно приложение');
    return result;
  }

  function buildAlerts(form) {
    const thresholds = {};
    ALERT_FIELDS.forEach(function (field) {
      thresholds[field.key] = readNumberField(
        form, 'threshold_' + field.key, field.label, 0, null
      );
    });
    const cooldowns = {};
    ALERT_FIELDS.forEach(function (field) {
      const never = form.querySelector('[name="never_' + field.key + '"]');
      if (never && never.checked) {
        cooldowns[field.key] = null;
        return;
      }
      const minutes = readNumberField(
        form, 'cooldown_' + field.key, 'Повтор: ' + field.label, 0, null
      );
      cooldowns[field.key] = Math.round(minutes * 60);
    });
    const interval = readNumberField(form, 'interval_seconds', 'Интервал проверки', 15, 86400);
    return {
      thresholds: thresholds,
      cooldowns: cooldowns,
      interval_seconds: Math.round(interval),
    };
  }

  function buildNotifications(form) {
    const warning = readNumberField(form, 'battery_warning', 'Предупреждение батареи', 5, 100);
    const critical = readNumberField(form, 'battery_critical', 'Критичный заряд', 1, 100);
    if (critical >= warning) {
      throw new Error('Критичный порог должен быть ниже предупреждающего');
    }
    const quietEnabled = form.querySelector('[name="quiet_enabled"]');
    const startInput = form.querySelector('[name="quiet_start"]');
    const endInput = form.querySelector('[name="quiet_end"]');
    const start = startInput ? startInput.value : '';
    const end = endInput ? endInput.value : '';
    if (quietEnabled && quietEnabled.checked) {
      if (!/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end)) {
        throw new Error('Укажите начало и конец тихих часов');
      }
    }
    const data = {
      battery_warning: Math.round(warning),
      battery_critical: Math.round(critical),
      app_cooldown_minutes: Math.round(
        readNumberField(form, 'app_cooldown_minutes', 'Повтор запуска', 0, 1440)
      ),
      crash_cooldown_minutes: Math.round(
        readNumberField(form, 'crash_cooldown_minutes', 'Повтор падения', 0, 1440)
      ),
      quiet_hours: {
        enabled: !!(quietEnabled && quietEnabled.checked),
        start: start || '23:00',
        end: end || '07:00',
      },
    };
    NOTIFY_TOGGLES.forEach(function (item) {
      const input = form.querySelector('[name="' + item.key + '"]');
      data[item.key] = !!(input && input.checked);
    });
    return data;
  }

  function buildProfiles(form) {
    const area = form.querySelector('[name="profiles_json"]');
    const raw = area ? area.value.trim() : '';
    if (!raw) throw new Error('Список профилей пуст');
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new Error('Некорректный JSON профилей');
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Ожидается объект профилей');
    }
    return parsed;
  }

  const SETTINGS_BUILDERS = {
    applications: buildApplications,
    alerts: buildAlerts,
    notifications: buildNotifications,
    profiles: buildProfiles,
  };

// ---------------------------------------------------------- settings actions
  action('settings-tab', function (node) {
    const key = node.getAttribute('data-tab');
    if (!key || key === state.settingsTab) return;
    state.settingsTab = key;
    renderSettingsTab();
  });

  action('settings-app-add', function () {
    const list = $('#app-edit-list');
    if (!list) return;
    list.insertAdjacentHTML('beforeend', appEditRow('', ''));
    const rows = $$('[data-app-row]', list);
    const last = rows[rows.length - 1];
    const input = last ? last.querySelector('[name="app_name"]') : null;
    if (input) input.focus();
  });

  action('settings-app-remove', function (node) {
    const row = node.closest('[data-app-row]');
    if (row) row.remove();
  });

  onchange('settings-never', function (node) {
    const item = node.closest('.cooldown-item');
    if (!item) return;
    const input = item.querySelector('input[name^="cooldown_"]');
    if (input) input.disabled = node.checked;
  });

  FORM['settings-save'] = async function (form) {
    const section = form.getAttribute('data-section');
    const builder = SETTINGS_BUILDERS[section];
    if (!builder) throw new Error('Неизвестный раздел настроек');
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    try {
      const values = builder(form);
      const sections = {};
      sections[section] = values;
      const result = await api('/api/settings', { method: 'POST', json: { sections: sections } });
      state.settings = (result && result.sections) || state.settings;
      renderSettingsTab();
      toast('Настройки сохранены', 'ok');
    } catch (error) {
      toast(describeError(error), 'error');
    } finally {
      if (button && button.isConnected) button.disabled = false;
    }
  };

  action('settings-export', guarded(async function () {
    const payload = await api('/api/settings/export');
    const text = JSON.stringify(payload, null, 2);
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'pc-remote-settings.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    toast('Файл настроек сохранён', 'ok');
  }));

  action('settings-import', function () {
    const input = $('#settings-import-file');
    if (input) input.click();
  });

  onchange('settings-import-file', guarded(async function (node) {
    const file = node.files && node.files[0];
    node.value = '';
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) throw new Error('Файл больше 2 МБ');
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch (error) {
      throw new Error('Файл не является корректным JSON');
    }
    const result = await api('/api/settings/import', { method: 'POST', json: { payload: parsed } });
    state.settings = (result && result.sections) || state.settings;
    renderSettingsTab();
    toast('Настройки импортированы', 'ok');
  }));

  action('settings-reset', guarded(async function (node) {
    node.disabled = true;
    try {
      const confirmed = await confirmModal({
        title: 'Сбросить настройки?',
        text: 'Приложения, профили, пороги и уведомления вернутся к значениям по умолчанию.',
        okText: 'Сбросить',
        danger: true,
      });
      if (!confirmed) return;
      await api('/api/settings/reset', { method: 'POST', json: {} });
      const payload = await api('/api/settings');
      state.settings = (payload && payload.sections) || {};
      renderSettingsTab();
      toast('Настройки сброшены', 'ok');
    } finally {
      if (node && node.isConnected) node.disabled = false;
    }
  }));

  action('settings-copy', function (node) {
    const text = node.getAttribute('data-text') || '';
    copyText(text).then(function (ok) {
      toast(ok ? 'Скопировано' : 'Не удалось скопировать', ok ? 'ok' : 'error');
    });
  });

  FORM['settings-password'] = async function (form) {
    const current = form.querySelector('[name="current"]');
    const next = form.querySelector('[name="next"]');
    const repeat = form.querySelector('[name="repeat"]');
    const oldValue = current ? current.value : '';
    const newValue = next ? next.value : '';
    if (!oldValue) throw new Error('Введите текущий пароль');
    if (newValue.length < 8) throw new Error('Новый пароль — не меньше 8 символов');
    if (newValue !== (repeat ? repeat.value : '')) throw new Error('Пароли не совпадают');
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    try {
      const result = await api('/api/password', {
        method: 'POST',
        json: { current: oldValue, new: newValue },
      });
      if (result && result.csrf) state.csrf = result.csrf;
      form.reset();
      toast('Пароль изменён. Другие сеансы завершены.', 'ok');
    } catch (error) {
      toast(describeError(error), 'error');
    } finally {
      if (button && button.isConnected) button.disabled = false;
    }
  };

  VIEWS.settings = async function (token) {
    const payload = await api('/api/settings');
    if (!pageToken(token)) return;
    state.settings = (payload && payload.sections) || {};
    if (!SETTINGS_TABS.some(function (tab) { return tab.key === state.settingsTab; })) {
      state.settingsTab = 'apps';
    }
    $('#view').innerHTML = settingsShellHtml();
  };

  boot();
})();