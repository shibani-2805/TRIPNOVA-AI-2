/* ==========================================================
   TripNova – script.js
   Talks to the Flask backend (app.py) using fetch().
   All text from the server is added with textContent / DOM
   methods (never innerHTML), so it cannot inject HTML.
   ========================================================== */

(() => {
  'use strict';

  /* ---------------------------------------------------------
     STATE
     --------------------------------------------------------- */
  const state = {
    config: null,        // destinations, categories, limits (from /api/config)
    user: null,          // logged-in user or null
    plan: null,          // the plan currently shown on the results page
    savedTripId: null,   // id if the shown plan is saved
    aiBrief: null,       // AI text for the saved trip (optional feature)
    homeFilter: 'All',
    pickerFilter: 'All',
    afterAuth: null      // function to run after a successful login
  };

  const VIEWS = ['home', 'planner', 'results', 'trips'];
  const CATEGORY_ICONS = { Outdoor: '🌿', Cultural: '🏛️', Adventure: '🧗', Relaxation: '🧘' };

  /* ---------------------------------------------------------
     SMALL HELPERS
     --------------------------------------------------------- */
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // el('div', {class: 'x'}, 'text', childNode) -> element
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);

    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(value));
      }
    }

    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }

    return node;
  }

  class ApiError extends Error {
    constructor(message, status, fields) {
      super(message);
      this.status = status;
      this.fields = fields || null;
    }
  }

  async function api(path, { method = 'GET', body } = {}) {
    const options = {
      method,
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'TripNova' }
    };

    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    let response;

    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new ApiError('Cannot reach the server. Check that the Flask app is running and try again.', 0);
    }

    let data = null;

    try {
      data = await response.json();
    } catch (error) {
      /* the reply was not JSON */
    }

    if (!response.ok) {
      throw new ApiError(
        (data && data.error) || `Request failed (${response.status}).`,
        response.status,
        data && data.fields
      );
    }

    return data;
  }

  function toast(message, type = 'info') {
    const box = $('#toasts');
    const node = el('div', { class: `toast ${type}`, role: 'status' }, message);
    box.append(node);
    setTimeout(() => node.classList.add('hide'), 4200);
    setTimeout(() => node.remove(), 4800);
  }

  function setLoading(button, on) {
    if (!button) return;
    button.classList.toggle('loading', on);
    button.disabled = on;
  }

  /* ---------------------------------------------------------
     FORMATTING
     --------------------------------------------------------- */
  function fmtMoney(amount, currency) {
    const digits = (currency === 'USD' || currency === 'EUR') ? 2 : 0;

    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency,
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      }).format(amount);
    } catch (error) {
      return `${currency} ${Number(amount).toFixed(digits)}`;
    }
  }

  function fmtDate(iso) {
    const date = new Date(`${iso}T00:00:00`);
    return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function isoDate(date) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function capitalise(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function rangeText(days) {
    if (!days.length) return '';
    const parts = [];
    let start = days[0];
    let previous = days[0];

    for (let i = 1; i <= days.length; i += 1) {
      const current = days[i];
      if (current !== previous + 1) {
        parts.push(start === previous ? `${start}` : `${start}–${previous}`);
        start = current;
      }
      previous = current;
    }

    return (days.length > 1 ? 'Days ' : 'Day ') + parts.join(', ');
  }

  function weatherIcon(condition) {
    const text = condition.toLowerCase();
    if (text.includes('thunder')) return '⛈️';
    if (text.includes('snow')) return '❄️';
    if (text.includes('rain') || text.includes('drizzle') || text.includes('shower')) return '🌧️';
    if (text.includes('fog')) return '🌫️';
    if (text.includes('overcast') || text.includes('cloud')) return '☁️';
    if (text.includes('mostly clear')) return '🌤️';
    if (text.includes('clear') || text.includes('warm')) return '☀️';
    return '🌤️';
  }

  function safeUrl(url) {
    return /^https?:\/\//i.test(url) ? url : '#';
  }

  function mapsLink(name, place) {
    return 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(`${name} ${place}`);
  }

  /* ---------------------------------------------------------
     DESTINATION PHOTOS (from Wikipedia; gradient if unavailable)
     --------------------------------------------------------- */
  const imageCache = new Map();

  function probeImage(url) {
    return new Promise((resolve) => {
      const image = new Image();
      image.onload = () => resolve(url);
      image.onerror = () => resolve(null);
      image.referrerPolicy = 'no-referrer';
      image.src = url;
    });
  }

  function wikiImage(title, width) {
    const key = `${title}|${width}`;

    if (imageCache.has(key)) return imageCache.get(key);

    try {
      const stored = sessionStorage.getItem(`tn_img_${key}`);
      if (stored) {
        const ready = Promise.resolve(stored);
        imageCache.set(key, ready);
        return ready;
      }
    } catch (error) {
      /* storage may be blocked */
    }

    const promise = fetch(`https://en.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(title)}`)
      .then((response) => (response.ok ? response.json() : null))
      .then(async (data) => {
        const thumb = data && data.thumbnail && data.thumbnail.source;
        if (!thumb) return null;
        const sized = thumb.replace(/\/\d+px-/, `/${width}px-`);
        let working = await probeImage(sized);
        if (!working && sized !== thumb) working = await probeImage(thumb);
        return working;
      })
      .catch(() => null)
      .then((url) => {
        if (url) {
          try { sessionStorage.setItem(`tn_img_${key}`, url); } catch (error) { /* ignore */ }
        }
        return url;
      });

    imageCache.set(key, promise);
    return promise;
  }

  function applyImage(node, title, width = 500) {
    wikiImage(title, width).then((url) => {
      if (!url) return;
      node.style.backgroundImage = `url("${url.replace(/"/g, '%22')}")`;
      node.classList.add('has-img');
    });
  }

  /* ---------------------------------------------------------
     ROUTER (#/home, #/planner, #/results, #/trips ...)
     --------------------------------------------------------- */
  function route() {
    let name = (location.hash || '#/home').replace(/^#\/?/, '').split('?')[0] || 'home';
    let scrollTarget = null;

    if (name === 'destinations' || name === 'about') {
      scrollTarget = name;
      name = 'home';
    }

    if (!VIEWS.includes(name)) name = 'home';

    if (name === 'results' && !state.plan) {
      location.hash = '#/planner';
      return;
    }

    VIEWS.forEach((view) => { $(`#view-${view}`).hidden = view !== name; });

    const activeRoute = scrollTarget || (name === 'results' ? 'planner' : name);
    $$('[data-route]').forEach((link) => link.classList.toggle('active', link.dataset.route === activeRoute));

    closeMenu();

    if (name === 'trips') renderTrips();

    if (scrollTarget) {
      setTimeout(() => {
        const target = document.getElementById(scrollTarget);
        if (target) target.scrollIntoView();
      }, 30);
    } else {
      window.scrollTo(0, 0);
    }
  }

  function goTo(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  function closeMenu() {
    $('#nav-menu').classList.remove('open');
    const toggle = $('#nav-toggle');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open menu');
  }

  function setupNav() {
    const toggle = $('#nav-toggle');

    toggle.addEventListener('click', () => {
      const open = $('#nav-menu').classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    });

    // Clicking the link for the page you are already on should still scroll.
    document.addEventListener('click', (event) => {
      const link = event.target.closest('a[href^="#/"]');
      if (link && link.getAttribute('href') === location.hash) route();
    });

    window.addEventListener('hashchange', route);
  }

  /* ---------------------------------------------------------
     ACCOUNT: nav buttons + login / register dialog
     --------------------------------------------------------- */
  function renderNavAuth() {
    const box = $('#nav-auth');

    if (state.user) {
      box.replaceChildren(
        el('span', { class: 'user-chip', text: `Hi, ${state.user.name}` }),
        el('button', { class: 'btn btn-outline btn-sm', type: 'button', onclick: logout }, 'Log out')
      );
    } else {
      box.replaceChildren(
        el('button', { class: 'btn btn-outline btn-sm', type: 'button', onclick: () => openAuth('login') }, 'Log in'),
        el('button', { class: 'btn btn-primary btn-sm', type: 'button', onclick: () => openAuth('register') }, 'Sign up')
      );
    }
  }

  function openAuth(tab = 'login') {
    switchTab(tab);
    const dialog = $('#auth-dialog');
    if (!dialog.open) dialog.showModal();
    closeMenu();
  }

  function switchTab(tab) {
    const isLogin = tab === 'login';
    $('#login-form').hidden = !isLogin;
    $('#register-form').hidden = isLogin;
    $('#tab-login').classList.toggle('active', isLogin);
    $('#tab-register').classList.toggle('active', !isLogin);
    $('#tab-login').setAttribute('aria-selected', String(isLogin));
    $('#tab-register').setAttribute('aria-selected', String(!isLogin));
    $('#auth-title').textContent = isLogin ? 'Welcome back' : 'Create your account';
  }

  function clearErrors(scope) {
    $$('.field-error', scope).forEach((node) => { node.textContent = ''; });
    $$('[aria-invalid]', scope).forEach((node) => node.removeAttribute('aria-invalid'));
    const banner = $('.form-error', scope);
    if (banner) { banner.hidden = true; banner.textContent = ''; }
  }

  function showFieldErrors(fields, scope) {
    let generic = null;

    for (const [key, message] of Object.entries(fields || {})) {
      const target = $(`#err-${key}`, scope);
      if (target) {
        target.textContent = message;
        const input = $(`#${key}`, scope);
        if (input) input.setAttribute('aria-invalid', 'true');
      } else {
        generic = message;
      }
    }

    return generic;
  }

  function showFormError(scope, message) {
    const banner = $('.form-error', scope);
    if (!banner) return;
    banner.textContent = message;
    banner.hidden = false;
  }

  function onAuthSuccess(user, message) {
    state.user = user;
    renderNavAuth();
    $('#auth-dialog').close();
    toast(message, 'success');

    const next = state.afterAuth;
    state.afterAuth = null;

    if (next) next();
    else if (location.hash === '#/trips') renderTrips();
    else if (location.hash === '#/results' && state.plan) renderResults(state.plan);
  }

  function setupAuthDialog() {
    const dialog = $('#auth-dialog');

    $('#auth-close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    // If the dialog is closed without logging in, forget the pending action.
    dialog.addEventListener('close', () => { state.afterAuth = null; });
    $$('.tab', dialog).forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

    $('#login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = $('button[type="submit"]', form);
      clearErrors(form);

      const email = $('#login-email').value.trim();
      const password = $('#login-password').value;

      if (!email || !password) {
        showFormError(form, 'Please enter your email and password.');
        return;
      }

      setLoading(button, true);
      try {
        const result = await api('/api/login', { method: 'POST', body: { email, password } });
        form.reset();
        onAuthSuccess(result.user, `Welcome back, ${result.user.name}!`);
      } catch (error) {
        showFormError(form, error.message);
      } finally {
        setLoading(button, false);
      }
    });

    $('#register-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = $('button[type="submit"]', form);
      clearErrors(form);

      const body = {
        name: $('#register-name').value.trim(),
        email: $('#register-email').value.trim(),
        password: $('#register-password').value
      };

      setLoading(button, true);
      try {
        const result = await api('/api/register', { method: 'POST', body });
        form.reset();
        onAuthSuccess(result.user, `Welcome to TripNova, ${result.user.name}!`);
      } catch (error) {
        const generic = showFieldErrors(error.fields, form);
        if (!error.fields || generic) showFormError(form, generic || error.message);
      } finally {
        setLoading(button, false);
      }
    });
  }

  async function logout() {
    try {
      await api('/api/logout', { method: 'POST', body: {} });
    } catch (error) {
      /* even if this fails, clear the screen state */
    }

    state.user = null;
    state.savedTripId = null;
    state.aiBrief = null;
    renderNavAuth();
    toast('You have been logged out.');

    if (location.hash === '#/trips') renderTrips();
    else if (location.hash === '#/results' && state.plan) renderResults(state.plan);
  }

  /* ---------------------------------------------------------
     HOME PAGE
     --------------------------------------------------------- */
  function destCard(destination) {
    const image = el('div', { class: 'dest-img' }, el('span', { class: 'dest-initial', text: destination.name.charAt(0) }));
    applyImage(image, destination.image_title, 500);

    return el('article', { class: 'dest-card' },
      image,
      el('div', { class: 'dest-body' },
        el('div', { class: 'dest-meta' },
          el('span', { class: `badge ${destination.type === 'Domestic' ? 'badge-dom' : 'badge-int'}`, text: destination.type }),
          el('span', { class: 'badge badge-cur', text: destination.currency })
        ),
        el('h3', { text: destination.name }),
        el('p', { class: 'muted', text: `${destination.state}, ${destination.country}` }),
        el('ul', { class: 'chip-list' }, destination.attractions.slice(0, 3).map((name) => el('li', { text: name }))),
        el('button', { class: 'btn btn-outline btn-sm', type: 'button', onclick: () => planWith(destination.name) }, 'Plan a trip here')
      )
    );
  }

  function renderHomeDestinations() {
    const list = state.config.destinations.filter(
      (d) => state.homeFilter === 'All' || d.type === state.homeFilter
    );
    $('#dest-grid').replaceChildren(...list.map(destCard));
  }

  function buildHome() {
    $('#stat-destinations').textContent = state.config.destinations.length;

    $$('[data-hero-image]').forEach((node) => applyImage(node, node.dataset.heroImage, 500));

    renderHomeDestinations();

    $('#home-filters').addEventListener('click', (event) => {
      const button = event.target.closest('[data-filter]');
      if (!button) return;
      state.homeFilter = button.dataset.filter;
      $$('#home-filters .chip-btn').forEach((b) => b.classList.toggle('active', b === button));
      renderHomeDestinations();
    });
  }

  function planWith(name) {
    $$('input[name="destinations"]').forEach((input) => { input.checked = input.value === name; });
    onDestinationChange();
    goTo('#/planner');
  }

  /* ---------------------------------------------------------
     TRIP PLANNER FORM
     --------------------------------------------------------- */
  function buildPlanner() {
    const { destinations, categories, currencies, limits } = state.config;

    // destinations
    $('#dest-options').replaceChildren(...destinations.map((destination) => {
      const id = `dest-${destination.name.replace(/\W+/g, '-')}`;
      const input = el('input', { type: 'checkbox', id, name: 'destinations', value: destination.name });
      input.addEventListener('change', onDestinationChange);

      const label = el('label', { class: 'pick', for: id, 'data-type': destination.type },
        input,
        el('span', { class: 'pick-box' },
          el('strong', { text: destination.name }),
          el('small', { text: `${destination.state} · ${destination.currency}` })
        )
      );

      return label;
    }));

    $('#picker-filters').addEventListener('click', (event) => {
      const button = event.target.closest('[data-filter]');
      if (!button) return;
      state.pickerFilter = button.dataset.filter;
      $$('#picker-filters .chip-btn').forEach((b) => b.classList.toggle('active', b === button));
      $$('#dest-options .pick').forEach((label) => {
        label.hidden = state.pickerFilter !== 'All' && label.dataset.type !== state.pickerFilter;
      });
    });

    // activity categories
    $('#category-options').replaceChildren(...categories.map((category) => {
      const id = `cat-${category.name}`;
      const input = el('input', { type: 'checkbox', id, name: 'categories', value: category.name });
      if (category.name === 'Outdoor' || category.name === 'Cultural') input.checked = true;
      input.addEventListener('change', updateActivityLimit);

      return el('label', { class: 'pill', for: id },
        input,
        el('span', null, `${CATEGORY_ICONS[category.name] || '•'} ${category.name}`)
      );
    }));

    // currencies
    $('#display_currency').replaceChildren(...currencies.map((currency) =>
      el('option', { value: currency.code, text: `${currency.code} – ${currency.name} (${currency.symbol})` })
    ));

    // dates and limits
    const today = new Date();
    const defaultStart = new Date(today.getTime() + 14 * 86400000);
    $('#start_date').min = isoDate(today);
    $('#start_date').value = isoDate(defaultStart);
    $('#days').max = limits.max_days;
    $('#travellers').max = limits.max_travellers;

    updateActivityLimit();
    onDestinationChange();

    $('#trip-form').addEventListener('submit', onGenerate);
  }

  function onDestinationChange() {
    const count = $$('input[name="destinations"]:checked').length;
    $('#dest-count').textContent = `${count} selected`;

    const days = $('#days');
    days.min = Math.max(1, count);

    $('#days-hint').textContent = count > 1
      ? `With ${count} destinations you need at least ${count} days.`
      : `Up to ${state.config.limits.max_days} days.`;
  }

  function updateActivityLimit() {
    const selected = $$('input[name="categories"]:checked').map((input) => input.value);
    const total = state.config.categories
      .filter((category) => selected.includes(category.name))
      .reduce((sum, category) => sum + category.count, 0);

    const input = $('#number_of_activities');
    const hint = $('#activities-hint');

    if (total > 0) {
      input.max = total;
      if (Number(input.value) > total) input.value = total;
      hint.textContent = `Your chosen preferences offer ${total} different activities. Three are scheduled each day (morning, afternoon, evening).`;
    } else {
      input.max = 20;
      hint.textContent = 'Choose at least one preference to see how many activities are available.';
    }
  }

  function readForm() {
    const form = $('#trip-form');
    const budget = $('input[name="budget_type"]:checked', form);

    return {
      destinations: $$('input[name="destinations"]:checked', form).map((input) => input.value),
      start_date: $('#start_date').value,
      days: $('#days').value,
      travellers: $('#travellers').value,
      budget_type: budget ? budget.value : '',
      categories: $$('input[name="categories"]:checked', form).map((input) => input.value),
      number_of_activities: $('#number_of_activities').value,
      display_currency: $('#display_currency').value
    };
  }

  function fillForm(request) {
    $$('input[name="destinations"]').forEach((input) => { input.checked = request.destinations.includes(input.value); });
    $$('input[name="categories"]').forEach((input) => { input.checked = request.categories.includes(input.value); });
    $$('input[name="budget_type"]').forEach((input) => { input.checked = input.value === request.budget_type; });
    $('#start_date').value = request.start_date;
    $('#days').value = request.days;
    $('#travellers').value = request.travellers;
    $('#number_of_activities').value = request.number_of_activities;
    $('#display_currency').value = request.display_currency;
    onDestinationChange();
    updateActivityLimit();
  }

  // Quick checks for instant feedback. The server repeats every check.
  function clientValidate(values) {
    const errors = {};
    const { limits } = state.config;

    if (!values.destinations.length) errors.destinations = 'Please select at least one destination.';
    if (!values.start_date) errors.start_date = 'Please choose a start date.';

    const days = Number(values.days);
    if (!Number.isInteger(days) || days < 1 || days > limits.max_days) {
      errors.days = `Trip length must be between 1 and ${limits.max_days} days.`;
    } else if (values.destinations.length > days) {
      errors.days = `You selected ${values.destinations.length} destinations, so you need at least ${values.destinations.length} days.`;
    }

    const travellers = Number(values.travellers);
    if (!Number.isInteger(travellers) || travellers < 1 || travellers > limits.max_travellers) {
      errors.travellers = `Travellers must be between 1 and ${limits.max_travellers}.`;
    }

    if (!values.budget_type) errors.budget_type = 'Please choose Budget, Mid-range or Luxury.';
    if (!values.categories.length) errors.categories = 'Please select at least one activity preference.';

    const activities = Number(values.number_of_activities);
    if (!Number.isInteger(activities) || activities < 1) {
      errors.number_of_activities = 'Please choose at least 1 activity.';
    }

    return errors;
  }

  function focusFirstError(scope) {
    const first = $$('.field-error', scope).find((node) => node.textContent);
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function generatePlan(values, button) {
    setLoading(button, true);
    try {
      return await api('/api/generate', { method: 'POST', body: values });
    } finally {
      setLoading(button, false);
    }
  }

  async function onGenerate(event) {
    event.preventDefault();

    const form = event.currentTarget;
    const button = $('#generate-btn');
    clearErrors(form);

    const values = readForm();
    const problems = clientValidate(values);

    if (Object.keys(problems).length) {
      showFieldErrors(problems, form);
      showFormError(form, 'Please fix the highlighted fields.');
      focusFirstError(form);
      return;
    }

    try {
      const plan = await generatePlan(values, button);
      showPlan(plan, null, null);
    } catch (error) {
      const generic = showFieldErrors(error.fields, form);
      showFormError(form, generic || error.message);
      focusFirstError(form);
    }
  }

  /* ---------------------------------------------------------
     RESULTS PAGE
     --------------------------------------------------------- */
  function showPlan(plan, savedId, aiBrief) {
    state.plan = plan;
    state.savedTripId = savedId;
    state.aiBrief = aiBrief;
    renderResults(plan);
    goTo('#/results');
  }

  function section(title, icon) {
    return el('div', { class: 'results-section' }, el('h2', null, `${icon} ${title}`));
  }

  function actionBar(plan) {
    const saved = Boolean(state.savedTripId);

    const bar = el('div', { class: 'results-bar' },
      el('button', {
        class: 'btn btn-primary', type: 'button', disabled: saved, onclick: (event) => onSave(event.currentTarget)
      }, saved ? '✓ Saved' : '💾 Save trip'),
      el('button', {
        class: 'btn btn-outline', type: 'button', onclick: (event) => onRegenerate(event.currentTarget)
      }, '🔄 New version'),
      el('button', {
        class: 'btn btn-outline', type: 'button', onclick: () => { fillForm(plan.request); goTo('#/planner'); }
      }, '✏️ Edit inputs'),
      el('button', { class: 'btn btn-outline', type: 'button', onclick: () => window.print() }, '🖨️ Print / PDF')
    );

    if (saved && state.config.features.ai_brief && !state.aiBrief) {
      bar.append(el('button', {
        class: 'btn btn-outline', type: 'button', onclick: (event) => onAiBrief(event.currentTarget)
      }, '✨ AI trip brief'));
    }

    return bar;
  }

  function summaryCards(plan) {
    const s = plan.summary;
    const r = plan.request;
    const cur = s.display_currency;

    const table = el('table', { class: 'currency-table' },
      el('thead', null, el('tr', null, el('th', { text: 'Currency' }), el('th', { text: 'Days' }), el('th', { text: 'Group total' }))),
      el('tbody', null, s.totals_by_currency.map((row) =>
        el('tr', null,
          el('td', { text: row.currency }),
          el('td', { text: row.days }),
          el('td', { text: fmtMoney(row.total, row.currency) })
        )
      ))
    );

    return el('div', { class: 'summary-grid' },
      el('div', { class: 'card stat-card primary' },
        el('h3', { text: 'Total estimated trip cost' }),
        el('div', { class: 'stat-big', text: fmtMoney(s.grand_total, cur) }),
        el('p', { class: 'stat-sub', text: `${fmtMoney(s.per_person, cur)} per person · ${fmtMoney(s.per_day, cur)} per day (group)` })
      ),
      el('div', { class: 'card stat-card' },
        el('h3', { text: 'Cost by currency' }),
        table,
        el('p', { class: 'stat-sub', text: s.rates_source === 'live' ? 'Converted with live exchange rates.' : 'Converted with approximate fixed rates.' })
      ),
      el('div', { class: 'card stat-card' },
        el('h3', { text: 'Trip at a glance' }),
        el('div', { class: 'stat-big', text: `${r.days} days` }),
        el('p', { class: 'stat-sub', text: `${r.destinations.length} destination${r.destinations.length > 1 ? 's' : ''} · ${r.travellers} traveller${r.travellers > 1 ? 's' : ''}` })
      )
    );
  }

  function destinationDetail(destination) {
    const photo = el('div', { class: 'photo' }, el('span', { class: 'dest-initial', text: destination.name.charAt(0) }));
    applyImage(photo, destination.image_title, 500);

    const hotel = destination.hotel;

    return el('article', { class: 'card dest-detail' },
      photo,
      el('div', { class: 'dest-detail-body' },
        el('div', { class: 'dest-meta' },
          el('span', { class: `badge ${destination.type === 'Domestic' ? 'badge-dom' : 'badge-int'}`, text: destination.type }),
          el('span', { class: 'badge badge-cur', text: destination.currency })
        ),
        el('h3', { text: destination.name }),
        el('p', { class: 'muted', text: `${destination.state}, ${destination.country} · ${rangeText(destination.days)}` }),
        el('h4', { class: 'mini-title', text: 'Top attractions' }),
        el('ul', { class: 'chip-list' }, destination.attractions.map((name) => el('li', { text: name }))),
        el('h4', { class: 'mini-title', text: 'Hotel recommendation' }),
        el('div', { class: 'hotel-box' },
          el('a', { href: safeUrl(hotel.website), target: '_blank', rel: 'noopener noreferrer' }, `${hotel.name} ↗`)
        )
      )
    );
  }

  function slotRow(label, content) {
    return el('div', { class: 'slot-row' }, el('span', { class: 'label', text: label }), content);
  }

  function mealRow(label, name, place) {
    return slotRow(label, el('a', { href: mapsLink(name, place), target: '_blank', rel: 'noopener noreferrer', title: 'Search on Google Maps' }, name));
  }

  function activityRow(slot, currency) {
    return slotRow('Activity', el('span', null,
      slot.activity,
      el('span', { class: 'cat-tag', text: slot.category }),
      el('br'),
      el('small', { class: 'muted', text: `≈ ${fmtMoney(slot.cost, currency)} per person` })
    ));
  }

  function dayCard(day) {
    const w = day.weather;
    const c = day.costs;

    const head = el('div', { class: 'day-head' },
      el('div', { class: 'day-title' },
        el('span', { class: 'day-num', text: `Day ${day.day}` }),
        el('div', null,
          el('h3', { text: day.destination }),
          el('small', { text: `${day.weekday}, ${fmtDate(day.date)}` })
        )
      ),
      el('div', { class: 'weather-chip', title: w.label },
        `${weatherIcon(w.condition)} ${w.condition}, ${w.low}–${w.high}°C`,
        el('small', { text: w.label })
      )
    );

    const body = el('div', { class: 'day-body' },
      el('div', { class: 'slot' },
        el('h4', { text: '🌅 Morning' }),
        mealRow('Breakfast', day.food.breakfast, day.destination),
        activityRow(day.morning, c.currency),
        slotRow('Attraction', day.morning.attraction)
      ),
      el('div', { class: 'slot' },
        el('h4', { text: '☀️ Afternoon' }),
        mealRow('Lunch', day.food.lunch, day.destination),
        activityRow(day.afternoon, c.currency),
        slotRow('Attraction', day.afternoon.attraction)
      ),
      el('div', { class: 'slot' },
        el('h4', { text: '🌙 Evening' }),
        activityRow(day.evening, c.currency),
        mealRow('Dinner', day.food.dinner, day.destination)
      )
    );

    const showConverted = c.currency !== c.display_currency;

    const foot = el('div', { class: 'day-foot' },
      el('div', null,
        el('span', { class: 'label', text: 'Stay' }),
        el('a', { href: safeUrl(day.hotel.website), target: '_blank', rel: 'noopener noreferrer' }, `${day.hotel.name} ↗`)
      ),
      el('div', null,
        el('span', { class: 'label', text: 'Estimated daily cost' }),
        el('div', { class: 'day-cost' },
          `${fmtMoney(c.per_person, c.currency)} `,
          el('small', { text: 'per person' })
        ),
        el('div', { class: 'day-cost' },
          `${fmtMoney(c.group_total, c.currency)} `,
          el('small', { text: 'whole group' }),
          showConverted ? el('small', { text: `  ≈ ${fmtMoney(c.group_total_display, c.display_currency)}` }) : null
        )
      )
    );

    return el('article', { class: 'card day-card' }, head, body, foot);
  }

  function renderAiArea() {
    const area = $('#ai-area');
    if (!area) return;

    if (!state.aiBrief) {
      area.replaceChildren();
      return;
    }

    area.replaceChildren(
      el('div', { class: 'ai-box' },
        el('h3', { text: '✨ AI trip brief' }),
        el('pre', { text: state.aiBrief }),
        el('p', { class: 'ai-note', text: 'Written by an AI model from your finished plan. It can make mistakes, so check details with official sources. It does not change your itinerary or costs.' })
      )
    );
  }

  function renderResults(plan) {
    const root = $('#results-content');
    const r = plan.request;

    $('#results-sub').textContent =
      `${r.destinations.join(' • ')}  ·  ${fmtDate(r.start_date)} – ${fmtDate(r.end_date)}`;

    const s = plan.summary;

    const notice = el('div', { class: 'notice', role: 'note' },
      el('strong', { text: 'Please read: these are estimates' }),
      el('ul', null,
        el('li', { text: `${s.forecast_days} of ${s.total_days} days use a live weather forecast; the rest show a seasonal estimate, which is not a forecast.` }),
        plan.notes.map((note) => el('li', { text: note }))
      )
    );

    const metaChips = el('div', { class: 'trip-meta' },
      el('span', { class: 'meta-chip', text: `🧳 ${r.travellers} traveller${r.travellers > 1 ? 's' : ''}` }),
      el('span', { class: 'meta-chip', text: `💼 ${capitalise(r.budget_type)}` }),
      el('span', { class: 'meta-chip', text: `🎯 ${r.categories.join(', ')}` }),
      el('span', { class: 'meta-chip', text: `📋 ${r.number_of_activities} activities in the pool` })
    );

    const tips = el('ul', { class: 'tips-list' }, plan.destinations.map((destination) =>
      el('li', null, el('strong', { text: destination.name }), destination.tip)
    ));

    root.replaceChildren(
      actionBar(plan),
      metaChips,
      summaryCards(plan),
      notice,
      el('div', { id: 'ai-area' }),
      section('Destinations & attractions', '📍'),
      el('div', { class: 'dest-detail-grid' }, plan.destinations.map(destinationDetail)),
      section('Day-wise itinerary', '🗓️'),
      el('div', { class: 'day-list' }, plan.itinerary.map(dayCard)),
      section('Travel tips', '💡'),
      tips
    );

    renderAiArea();
  }

  async function onRegenerate(button) {
    setLoading(button, true);
    try {
      const plan = await api('/api/generate', { method: 'POST', body: state.plan.request });
      showPlan(plan, null, null);
      toast('Here is a fresh version of your plan.', 'success');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      setLoading(button, false);
    }
  }

  async function onSave(button) {
    if (!state.user) {
      state.afterAuth = () => onSave(null);
      toast('Please log in or sign up to save your trip.');
      openAuth('login');
      return;
    }

    setLoading(button, true);
    try {
      const result = await api('/api/trips', {
        method: 'POST',
        body: { ...state.plan.request, seed: state.plan.seed }
      });
      state.savedTripId = result.id;
      renderResults(state.plan);
      toast('Trip saved. Find it under My Trips.', 'success');
    } catch (error) {
      toast(error.message, 'error');
      setLoading(button, false);
    }
  }

  async function onAiBrief(button) {
    setLoading(button, true);
    try {
      const result = await api(`/api/trips/${state.savedTripId}/ai-brief`, { method: 'POST', body: {} });
      state.aiBrief = result.ai_brief;
      renderResults(state.plan);
    } catch (error) {
      toast(error.message, 'error');
      setLoading(button, false);
    }
  }

  /* ---------------------------------------------------------
     MY TRIPS
     --------------------------------------------------------- */
  async function renderTrips() {
    const root = $('#trips-content');

    if (!state.user) {
      root.replaceChildren(
        el('div', { class: 'card empty' },
          el('div', { class: 'big', text: '🔒' }),
          el('h2', { text: 'Log in to see your trips' }),
          el('p', { class: 'muted', text: 'Create a free account to save plans and open them any time.' }),
          el('button', {
            class: 'btn btn-primary', type: 'button',
            onclick: () => { state.afterAuth = renderTrips; openAuth('login'); }
          }, 'Log in or sign up')
        )
      );
      return;
    }

    root.replaceChildren(el('p', { class: 'muted', text: 'Loading your trips…' }));

    try {
      const { trips } = await api('/api/trips');

      if (!trips.length) {
        root.replaceChildren(
          el('div', { class: 'card empty' },
            el('div', { class: 'big', text: '🧭' }),
            el('h2', { text: 'No saved trips yet' }),
            el('p', { class: 'muted', text: 'Generate a plan and press “Save trip”.' }),
            el('a', { class: 'btn btn-primary', href: '#/planner' }, 'Plan a trip')
          )
        );
        return;
      }

      root.replaceChildren(el('div', { class: 'trip-list' }, trips.map((trip) =>
        el('article', { class: 'card trip-item' },
          el('div', null,
            el('h3', { text: trip.title }),
            el('p', { text: `${trip.destinations.join(', ')} · ${fmtDate(trip.start_date)} · ${trip.days} days · ${trip.travellers} traveller${trip.travellers > 1 ? 's' : ''}` }),
            el('p', { text: `${capitalise(trip.budget_type)} · about ${fmtMoney(trip.total_amount, trip.display_currency)} · saved ${fmtDate(trip.created_at.slice(0, 10))}` })
          ),
          el('div', { class: 'trip-actions' },
            el('button', { class: 'btn btn-primary btn-sm', type: 'button', onclick: () => openTrip(trip.id) }, 'View'),
            el('button', { class: 'btn btn-danger btn-sm', type: 'button', onclick: () => deleteTrip(trip) }, 'Delete')
          )
        )
      )));
    } catch (error) {
      if (error.status === 401) {
        state.user = null;
        renderNavAuth();
        renderTrips();
        return;
      }
      root.replaceChildren(el('div', { class: 'card empty' }, el('h2', { text: 'Could not load trips' }), el('p', { class: 'muted', text: error.message })));
    }
  }

  async function openTrip(id) {
    try {
      const data = await api(`/api/trips/${id}`);
      showPlan(data.plan, data.id, data.ai_brief);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function deleteTrip(trip) {
    if (!window.confirm(`Delete “${trip.title}”? This cannot be undone.`)) return;

    try {
      await api(`/api/trips/${trip.id}`, { method: 'DELETE' });
      if (state.savedTripId === trip.id) state.savedTripId = null;
      toast('Trip deleted.', 'success');
      renderTrips();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  /* ---------------------------------------------------------
     START
     --------------------------------------------------------- */
  async function init() {
    setupNav();
    setupAuthDialog();

    try {
      const [config, me] = await Promise.all([api('/api/config'), api('/api/me')]);
      state.config = config;
      state.user = me.user;
    } catch (error) {
      $('#dest-grid').replaceChildren(
        el('p', { class: 'form-error', text: `Could not load TripNova data. ${error.message}` })
      );
    }

    renderNavAuth();

    if (state.config) {
      buildHome();
      buildPlanner();
    }

    route();
  }

  init();
})();
