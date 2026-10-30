/* Shared authenticated fetch wrapper, login/registration views and session UI. */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null };

  const getCookie = (name) => document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1) || '';

  function messageFromDetail(detail) {
    if (!detail) return '请求失败';
    if (typeof detail === 'string') return detail;
    if (detail.message) return detail.message;
    if (Array.isArray(detail)) {
      return detail.map((item) => {
        const path = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : '';
        return `${path ? `${path}：` : ''}${item.msg || '参数不合法'}`;
      }).join('；');
    }
    return '请求失败';
  }

  async function apiRequest(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const token = getCookie('business_csrf');
      if (token) headers.set('X-CSRF-Token', token);
    }
    let response;
    try {
      response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
    } catch {
      throw new Error('网络连接失败，请检查服务是否运行');
    }
    if (response.status === 401 && !options.allowUnauthorized) {
      handleUnauthorized();
      const error = new Error('登录已过期，请重新登录');
      error.status = 401;
      throw error;
    }
    if (!response.ok) {
      let body = null;
      try { body = await response.json(); } catch {}
      const error = new Error(messageFromDetail(body?.detail));
      error.status = response.status;
      error.code = body?.detail?.code;
      error.detail = body?.detail;
      throw error;
    }
    if (response.status === 204) return null;
    const type = response.headers.get('content-type') || '';
    return type.includes('application/json') ? response.json() : response.text();
  }

  const NAV_PARENT = {
    'order-detail': 'orders',
    'device-detail': 'devices',
    'ticket-detail': 'tickets',
  };

  function showView(name) {
    document.querySelectorAll('.view').forEach((node) => {
      node.classList.toggle('active-view', node.id === `${name}-view`);
    });
    const navName = NAV_PARENT[name] || name;
    document.querySelectorAll('.nav-item').forEach((node) => {
      node.classList.toggle('active', node.dataset.view === navName);
    });
    window.scrollTo(0, 0);
  }

  function setMessage(id, text, failed = false) {
    const node = $(id);
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('error', Boolean(failed));
  }

  function clearAuthMessages() {
    setMessage('login-message', '');
    setMessage('register-message', '');
  }

  function clearBusinessDom() {
    ['business-stats', 'order-list', 'order-detail', 'device-list', 'device-detail',
      'ticket-list', 'ticket-detail', 'customer-list',
      'tool-list', 'tool-result'].forEach((id) => {
      const node = $(id);
      if (node) node.replaceChildren();
    });
  }

  function applyUser() {
    document.body.classList.toggle('not-authenticated', !state.user);
    document.body.dataset.authenticated = state.user ? 'true' : 'false';
    const avatar = $('identity-avatar');
    if (!state.user) {
      $('identity-name').textContent = '未登录';
      $('identity-role').textContent = '';
      $('logout-button').hidden = true;
      if (avatar) avatar.textContent = '·';
      return;
    }
    $('identity-name').textContent = state.user.display_name;
    $('identity-role').textContent = `${state.user.role === 'admin' ? '管理员' : '客户'} · ${state.user.username}`;
    $('logout-button').hidden = false;
    if (avatar) {
      avatar.textContent = String(state.user.display_name || state.user.username).trim().slice(0, 1).toUpperCase();
    }
    const admin = isAdmin();
    document.querySelectorAll('[data-admin-only]').forEach((node) => { node.hidden = !admin; });
    document.querySelectorAll('[data-customer-only]').forEach((node) => { node.hidden = admin; });
  }

  function showLogin() {
    state.user = null;
    applyUser();
    clearAuthMessages();
    showView('login');
    if (location.hash === '#/register') location.hash = '#/login';
  }

  function showRegister() {
    state.user = null;
    applyUser();
    clearAuthMessages();
    showView('register');
    if (location.hash !== '#/register') location.hash = '#/register';
    const field = $('register-username');
    if (field) field.focus();
  }

  function handleUnauthorized() {
    const wasAuthenticated = Boolean(state.user);
    state.user = null;
    clearBusinessDom();
    document.dispatchEvent(new CustomEvent('auth:unauthorized'));
    applyUser();
    clearAuthMessages();
    showView('login');
    if (location.hash === '#/register') location.hash = '#/login';
    if (wasAuthenticated) setMessage('login-message', '登录已过期，请重新登录。', true);
  }

  function isAdmin() {
    return state.user?.role === 'admin';
  }

  async function loadSession() {
    try {
      state.user = await apiRequest('/api/v1/auth/me', { allowUnauthorized: true });
    } catch {
      showLogin();
      return null;
    }
    applyUser();
    showView('business');
    if (location.hash.startsWith('#/')) {
      history.replaceState(null, '', location.pathname + location.search);
    }
    document.dispatchEvent(new CustomEvent('business:ready', { detail: state.user }));
    return state.user;
  }

  async function submitAuth(url, payload, form, messageId, busyText) {
    clearAuthMessages();
    const button = form.querySelector('button[type="submit"]');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = busyText;
    try {
      await apiRequest(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        allowUnauthorized: true,
      });
      form.reset();
      await loadSession();
    } catch (error) {
      setMessage(messageId, error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('login-form').addEventListener('submit', (event) => {
      event.preventDefault();
      submitAuth(
        '/api/v1/auth/login',
        { username: $('login-username').value, password: $('login-password').value },
        event.currentTarget,
        'login-message',
        '正在登录…',
      );
    });

    $('register-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const password = $('register-password').value;
      const confirmation = $('register-password-confirm').value;
      if (password !== confirmation) {
        setMessage('register-message', '两次输入的密码不一致。', true);
        $('register-password-confirm').focus();
        return;
      }
      submitAuth(
        '/api/v1/auth/register',
        {
          username: $('register-username').value,
          display_name: $('register-display-name').value,
          password,
        },
        event.currentTarget,
        'register-message',
        '正在注册…',
      );
    });

    document.querySelectorAll('[data-auth-target]').forEach((button) => {
      button.addEventListener('click', () => {
        if (button.dataset.authTarget === 'register') {
          showRegister();
        } else if (location.hash !== '#/login') {
          location.hash = '#/login';
        } else {
          showLogin();
          $('login-username').focus();
        }
      });
    });

    window.addEventListener('hashchange', () => {
      if (state.user) return;
      if (location.hash === '#/register') showRegister();
      else if (location.hash === '#/login') showLogin();
    });

    document.querySelectorAll('[data-toggle-password]').forEach((button) => {
      button.addEventListener('click', () => {
        const input = $(button.dataset.togglePassword);
        if (!input) return;
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        button.textContent = show ? '隐藏' : '显示';
        button.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
      });
    });

    $('logout-button').addEventListener('click', async () => {
      try {
        await apiRequest('/api/v1/auth/logout', { method: 'POST' });
      } catch (error) {
        if (error.status !== 401) setMessage('login-message', error.message, true);
      }
      state.user = null;
      clearBusinessDom();
      document.dispatchEvent(new CustomEvent('auth:logged-out'));
      applyUser();
      clearAuthMessages();
      showView('login');
      if (location.hash === '#/register') location.hash = '#/login';
      setMessage('login-message', '已退出登录。');
    });

    if (location.hash === '#/register') showRegister();
    else showLogin();
    loadSession();
  });

  window.apiRequest = apiRequest;
  window.BusinessAuth = {
    apiRequest,
    get user() { return state.user; },
    isAdmin,
    loadSession,
    showView,
    showLogin,
    showRegister,
    handleUnauthorized,
  };
})();