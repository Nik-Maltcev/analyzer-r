// MEANX — Main JavaScript

// Global market state
let currentMarket = window.CRYPTOSCOPE_INITIAL_MARKET || 'crypto';
window.currentMarket = currentMarket;
let activeSignalsRequest = null;

const uiTranslations = window.CRYPTOSCOPE_TRANSLATIONS || {};
const uiTranslationKeys = Object.keys(uiTranslations).sort((a, b) => b.length - a.length);

function translateUi(value) {
    let translated = String(value ?? '');
    uiTranslationKeys.forEach(source => {
        translated = translated.split(source).join(uiTranslations[source]);
    });
    return translated;
}
window.translateUi = translateUi;

const viewModeKey = 'meanx_view_mode';

function getViewMode() {
    try {
        const saved = localStorage.getItem(viewModeKey);
        return saved === 'pro' ? 'pro' : 'beginner';
    } catch (_) {
        return 'beginner';
    }
}

function applyViewMode(mode = getViewMode()) {
    const normalized = mode === 'pro' ? 'pro' : 'beginner';
    document.documentElement.dataset.viewMode = normalized;
    document.querySelectorAll('.view-mode-btn').forEach(button => {
        button.classList.toggle('active', button.dataset.viewMode === normalized);
    });
}

function setViewMode(mode) {
    const normalized = mode === 'pro' ? 'pro' : 'beginner';
    try {
        localStorage.setItem(viewModeKey, normalized);
    } catch (_) {}
    applyViewMode(normalized);
}

window.setViewMode = setViewMode;
applyViewMode();

function selectedMarket() {
    return document.querySelector('.market-btn.active')?.dataset.market
        || window.currentMarket
        || currentMarket
        || 'crypto';
}

function signalsWorkspacePath(button) {
    const scanner = button?.dataset.scanner;
    return scanner
        ? `/tab/scanner/${encodeURIComponent(scanner)}`
        : '/tab/signals';
}

function loadSignalsWorkspace(button, market = selectedMarket()) {
    if (!button || typeof htmx === 'undefined') return;
    const requestedMarket = String(market || 'crypto');
    button.dataset.requestMarket = requestedMarket;
    document.querySelectorAll('.mode-btn').forEach(item => {
        item.classList.toggle('active', item === button);
    });
    const filters = document.getElementById('mode-filters');
    if (filters) filters.hidden = Boolean(button.dataset.scanner);
    htmx.ajax('GET', signalsWorkspacePath(button), {
        source: button,
        target: '#signals-content',
        swap: 'innerHTML',
        values: button.dataset.scanner
            ? {market: requestedMarket}
            : {
                market: requestedMarket,
                mode: button.dataset.mode || 'all'
            }
    });
}

function switchMarket(market) {
    currentMarket = market;
    window.currentMarket = market;
    document.querySelectorAll('.market-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.market === market);
    });
    const activeMode = document.querySelector('.mode-btn.active');
    loadSignalsWorkspace(activeMode, market);
}
window.switchMarket = switchMarket;

function selectSignalsWorkspace(button) {
    loadSignalsWorkspace(button, selectedMarket());
}
window.selectSignalsWorkspace = selectSignalsWorkspace;

async function changeLocale(locale) {
    const response = await fetch(`/api/locale?lang=${encodeURIComponent(locale)}`, {
        method: 'POST',
        credentials: 'same-origin'
    });
    if (response.ok) window.location.reload();
}
window.changeLocale = changeLocale;

const favoritePnlDefaults = {
    capital: 1000,
    leverage: 1,
    taker_fee: 0.02,
    funding_rate: 0.01
};

function favoritePnlNumber(value, fallback, min, max) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
}

function getFavoritePnlSettings() {
    let stored = {};
    try {
        stored = JSON.parse(localStorage.getItem('cryptoscope_favorite_pnl') || '{}');
    } catch (_) {}
    return {
        capital: favoritePnlNumber(stored.capital, favoritePnlDefaults.capital, 10, 100000000),
        leverage: favoritePnlNumber(stored.leverage, favoritePnlDefaults.leverage, 1, 20),
        taker_fee: favoritePnlNumber(stored.taker_fee, favoritePnlDefaults.taker_fee, 0, 1),
        funding_rate: favoritePnlNumber(stored.funding_rate, favoritePnlDefaults.funding_rate, 0, 1)
    };
}

function storeFavoritePnlSettings(form) {
    if (!form) return;
    const values = Object.fromEntries(new FormData(form).entries());
    const settings = {
        capital: favoritePnlNumber(values.capital, favoritePnlDefaults.capital, 10, 100000000),
        leverage: favoritePnlNumber(values.leverage, favoritePnlDefaults.leverage, 1, 20),
        taker_fee: favoritePnlNumber(values.taker_fee, favoritePnlDefaults.taker_fee, 0, 1),
        funding_rate: favoritePnlNumber(values.funding_rate, favoritePnlDefaults.funding_rate, 0, 1)
    };
    localStorage.setItem('cryptoscope_favorite_pnl', JSON.stringify(settings));
}

function favoritePnlUrl(path, extra = {}) {
    const form = document.getElementById('favorites-pnl-settings');
    if (form) storeFavoritePnlSettings(form);
    const params = new URLSearchParams({
        ...getFavoritePnlSettings(),
        ...extra
    });
    return `${path}?${params.toString()}`;
}

window.getFavoritePnlSettings = getFavoritePnlSettings;
window.storeFavoritePnlSettings = storeFavoritePnlSettings;

const appPendingRequests = new Set();
const appBlockingRequests = new Set();
let appProgressHideTimer = null;
let appOverlayShowTimer = null;
let appOverlayLongWaitTimer = null;

function appLoadingMessage(source, target) {
    const customMessage = source?.dataset?.loadingMessage;
    if (customMessage) return customMessage;

    const targetId = target?.id || '';
    if (targetId === 'signals-content') return 'Обновляем сигналы';
    if (source?.matches?.('.crypto-refresh-button')) {
        return 'Обновляем криптоданные';
    }
    if (source?.matches?.('[hx-post]')) return 'Сохраняем изменения';

    if (targetId === 'main-content') {
        const tab = source?.dataset?.tab;
        if (tab === 'crypto') return 'Открываем раздел «Крипта»';
        if (tab === 'alpha') return 'Определяем режим крипторынка';
        if (tab === 'crypto-v2') return 'Считаем независимую стратегию Crypto V2';
        if (tab === 'reversal') return 'Открываем исследование 5-минутных разворотов';
        if (tab === 'favorites') return 'Открываем портфель';
        if (tab === 'data') return 'Открываем данные';
        return 'Открываем раздел';
    }

    return 'Загружаем данные';
}

function shouldBlockAppRequest(source, target) {
    if (source?.dataset?.nonblockingRequest === 'true') return false;
    const targetId = target?.id || '';
    if (
        targetId === 'main-content'
        || targetId === 'signals-content'
    ) {
        return true;
    }
    return Boolean(source?.matches?.(
        '.nav-tab, .market-btn, .mode-btn, [hx-post], .crypto-refresh-button'
    ));
}

function hideAppLoadingOverlay() {
    window.clearTimeout(appOverlayShowTimer);
    window.clearTimeout(appOverlayLongWaitTimer);
    appOverlayShowTimer = null;
    appOverlayLongWaitTimer = null;

    const overlay = document.getElementById('app-loading-overlay');
    if (!overlay) return;
    overlay.classList.remove('is-visible');
    overlay.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('app-is-loading');
}

function queueAppLoadingOverlay(source, target) {
    const overlay = document.getElementById('app-loading-overlay');
    const title = document.getElementById('app-loading-title');
    const detail = document.getElementById('app-loading-detail');
    if (!overlay || !title || !detail) return;

    title.textContent = appLoadingMessage(source, target);
    detail.textContent = 'Пожалуйста, подождите';
    window.clearTimeout(appOverlayShowTimer);
    window.clearTimeout(appOverlayLongWaitTimer);

    appOverlayShowTimer = window.setTimeout(() => {
        if (!appBlockingRequests.size) return;
        overlay.classList.add('is-visible');
        overlay.setAttribute('aria-hidden', 'false');
        document.body.classList.add('app-is-loading');
        appOverlayLongWaitTimer = window.setTimeout(() => {
            if (appBlockingRequests.size) {
                detail.textContent = 'Расчёт занимает немного больше времени';
            }
        }, 8000);
    }, 180);
}

function showAppRequestProgress(xhr, target, source = null, forceBlocking = false) {
    const progress = document.getElementById('app-request-progress');
    if (!progress) return;

    if (xhr) appPendingRequests.add(xhr);
    if (target) {
        target.setAttribute('aria-busy', 'true');
        target.dataset.meanxBusy = 'true';
    }

    window.clearTimeout(appProgressHideTimer);
    progress.classList.remove('is-completing');
    progress.classList.add('is-loading');

    if (forceBlocking || shouldBlockAppRequest(source, target)) {
        if (xhr) appBlockingRequests.add(xhr);
        queueAppLoadingOverlay(source, target);
    }
}

function finishAppRequestProgress(xhr) {
    if (xhr) appPendingRequests.delete(xhr);
    if (xhr) appBlockingRequests.delete(xhr);

    if (!appBlockingRequests.size) {
        hideAppLoadingOverlay();
    }
    if (appPendingRequests.size) return;

    const progress = document.getElementById('app-request-progress');
    document.querySelectorAll('[data-meanx-busy="true"]').forEach(target => {
        target.removeAttribute('aria-busy');
        delete target.dataset.meanxBusy;
    });
    if (!progress) return;

    progress.classList.add('is-completing');
    appProgressHideTimer = window.setTimeout(() => {
        progress.classList.remove('is-loading', 'is-completing');
    }, 180);
}

document.body.addEventListener('htmx:beforeRequest', event => {
    showAppRequestProgress(
        event.detail.xhr,
        event.detail.target,
        event.detail.elt
    );
});

document.body.addEventListener('htmx:afterRequest', event => {
    finishAppRequestProgress(event.detail.xhr);
});

document.body.addEventListener('htmx:sendError', event => {
    finishAppRequestProgress(event.detail.xhr);
});

document.body.addEventListener('htmx:timeout', event => {
    finishAppRequestProgress(event.detail.xhr);
});

document.body.addEventListener('htmx:abort', event => {
    finishAppRequestProgress(event.detail.xhr);
});

document.addEventListener('click', event => {
    // Ignore modified/middle clicks: the browser opens a new tab and this
    // page never unloads, which would leave the blocking overlay stuck on.
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const tab = event.target.closest('.nav-tab');
    if (!tab || tab.hasAttribute('hx-get')) return;

    const destination = new URL(tab.href, window.location.href);
    if (destination.origin === window.location.origin) {
        const pageNavigationRequest = {type: 'page-navigation'};
        appPendingRequests.add(pageNavigationRequest);
        appBlockingRequests.add(pageNavigationRequest);
        showAppRequestProgress(
            null,
            document.getElementById('main-content'),
            tab,
            true
        );
    }
});

document.body.addEventListener('htmx:configRequest', event => {
    if (event.detail.target?.id === 'signals-content') {
        const activeMode = document.querySelector('.mode-btn.active');
        event.detail.parameters.market = event.detail.elt?.dataset.requestMarket
            || selectedMarket();
        if (!activeMode?.dataset.scanner) {
            event.detail.parameters.mode = activeMode?.dataset.mode || 'all';
        }
    }

    if (event.detail.elt?.dataset?.tab !== 'favorites') return;
    const settings = getFavoritePnlSettings();
    Object.entries(settings).forEach(([key, value]) => {
        if (event.detail.parameters[key] === undefined) {
            event.detail.parameters[key] = value;
        }
    });
});

document.body.addEventListener('htmx:beforeRequest', event => {
    if (event.detail.target?.id !== 'signals-content') return;
    const nextRequest = event.detail.xhr;
    if (
        activeSignalsRequest
        && activeSignalsRequest !== nextRequest
        && activeSignalsRequest.readyState !== 4
    ) {
        activeSignalsRequest.abort();
    }
    activeSignalsRequest = nextRequest;
});

document.body.addEventListener('htmx:beforeSwap', event => {
    if (event.detail.target?.id !== 'signals-content') return;
    const responseUrl = event.detail.xhr?.responseURL;
    if (!responseUrl) return;

    const requestedMarkets = new URL(responseUrl, window.location.origin)
        .searchParams
        .getAll('market');
    const requestedMarket = requestedMarkets.at(-1);
    if (requestedMarket && requestedMarket !== selectedMarket()) {
        event.detail.shouldSwap = false;
    }
});

document.body.addEventListener('htmx:afterRequest', event => {
    if (event.detail.xhr === activeSignalsRequest) {
        activeSignalsRequest = null;
    }
});

document.body.addEventListener('htmx:afterSwap', () => applyViewMode());

// Passwordless authentication
function openAuthModal() {
    const modal = document.getElementById('auth-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    document.body.classList.add('modal-open');
    requestAnimationFrame(() => document.getElementById('auth-email')?.focus());
}

function closeAuthModal() {
    document.getElementById('auth-modal')?.classList.add('hidden');
    document.body.classList.remove('modal-open');
}

window.openAuthModal = openAuthModal;
window.closeAuthModal = closeAuthModal;

let paypalSdkPromise = null;

function setPayPalCheckoutStatus(message, type = '') {
    const status = document.getElementById('paypal-checkout-status');
    if (!status) return;
    status.textContent = translateUi(message);
    status.className = `auth-message ${type}`.trim();
}

function loadPayPalSdk() {
    if (window.paypal?.Buttons) return Promise.resolve(window.paypal);
    if (paypalSdkPromise) return paypalSdkPromise;

    const config = window.MEANX_PAYPAL_CONFIG || {};
    if (!config.clientId) {
        return Promise.reject(new Error('PayPal не настроен'));
    }
    const params = new URLSearchParams({
        'client-id': config.clientId,
        currency: config.currency || 'USD',
        components: 'buttons',
        intent: 'capture'
    });
    paypalSdkPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.id = 'meanx-paypal-sdk';
        script.src = `https://www.paypal.com/sdk/js?${params.toString()}`;
        script.async = true;
        script.addEventListener('load', () => resolve(window.paypal), {once: true});
        script.addEventListener(
            'error',
            () => reject(new Error('PayPal временно недоступен. Попробуйте позже.')),
            {once: true}
        );
        document.head.appendChild(script);
    });
    return paypalSdkPromise;
}

function closePayPalCheckout() {
    document.getElementById('paypal-checkout-modal')?.classList.add('hidden');
    if (document.getElementById('auth-modal')?.classList.contains('hidden')) {
        document.body.classList.remove('modal-open');
    }
}

async function openPayPalCheckout(plan) {
    if (!['month', 'year'].includes(plan)) return;
    const modal = document.getElementById('paypal-checkout-modal');
    const container = document.getElementById('paypal-checkout-buttons');
    const planLabel = document.getElementById('paypal-checkout-plan');
    const amountLabel = document.getElementById('paypal-checkout-amount');
    const config = window.MEANX_PAYPAL_CONFIG || {};
    if (!modal || !container) return;

    planLabel.textContent = translateUi(
        plan === 'month' ? 'Месячный тариф' : 'Годовой тариф'
    );
    const displayAmount = (
        plan === 'month' ? config.monthDisplay : config.yearDisplay
    );
    const checkoutAmount = (
        plan === 'month' ? config.monthAmount : config.yearAmount
    );
    amountLabel.textContent = config.displayCurrencyAtCheckout
        ? displayAmount
        : `${displayAmount} · PayPal ${config.currency || 'USD'} ${checkoutAmount}`;
    container.replaceChildren();
    container.classList.add('is-loading');
    setPayPalCheckoutStatus('', 'hidden');
    modal.classList.remove('hidden');
    document.body.classList.add('modal-open');

    try {
        const paypal = await loadPayPalSdk();
        const buttons = paypal.Buttons({
            style: {
                layout: 'vertical',
                shape: 'rect',
                label: 'paypal',
                height: 42
            },
            async createOrder() {
                const response = await fetch('/api/payments/paypal/orders', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({plan})
                });
                const data = await response.json().catch(() => ({}));
                if (response.status === 401) {
                    closePayPalCheckout();
                    openAuthModal();
                }
                if (!response.ok || !data.id) {
                    throw new Error(data.detail || 'Не удалось создать платёж PayPal');
                }
                return data.id;
            },
            async onApprove(data, actions) {
                setPayPalCheckoutStatus('Подтверждаем оплату...', '');
                const response = await fetch(
                    `/api/payments/paypal/orders/${encodeURIComponent(data.orderID)}/capture`,
                    {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'}
                    }
                );
                const result = await response.json().catch(() => ({}));
                if (
                    !response.ok
                    && String(result.detail || '').includes('INSTRUMENT_DECLINED')
                ) {
                    setPayPalCheckoutStatus(
                        'Способ оплаты отклонён. Выберите другой.',
                        'error'
                    );
                    return actions.restart();
                }
                if (!response.ok || result.status !== 'COMPLETED') {
                    throw new Error(
                        result.detail || 'Не удалось подтвердить платёж PayPal'
                    );
                }
                setPayPalCheckoutStatus(
                    'Оплата подтверждена. Доступ активирован.',
                    'success'
                );
                window.location.href = (
                    `/payment/success?paypal_order_id=${
                        encodeURIComponent(result.order_id)
                    }`
                );
            },
            onCancel() {
                setPayPalCheckoutStatus('Оплата отменена', '');
            },
            onError(error) {
                setPayPalCheckoutStatus(
                    error?.message || 'PayPal временно недоступен. Попробуйте позже.',
                    'error'
                );
            }
        });
        if (!buttons.isEligible()) {
            throw new Error('PayPal недоступен для этого устройства');
        }
        await buttons.render(container);
    } catch (error) {
        setPayPalCheckoutStatus(
            error.message || 'PayPal временно недоступен. Попробуйте позже.',
            'error'
        );
    } finally {
        container.classList.remove('is-loading');
    }
}

window.openPayPalCheckout = openPayPalCheckout;
window.closePayPalCheckout = closePayPalCheckout;

function setAuthMessage(message, type = '') {
    const messageEl = document.getElementById('auth-message');
    if (!messageEl) return;
    messageEl.textContent = translateUi(message);
    messageEl.className = `auth-message ${type}`.trim();
}

function renderAuthBar(email = null, authAvailable = true, access = null) {
    const bar = document.getElementById('auth-bar');
    if (!bar) return;
    bar.replaceChildren();

    if (email) {
        const emailEl = document.createElement('span');
        emailEl.className = 'auth-email';
        emailEl.textContent = email;
        let accessEl = null;
        if (access && access.status !== 'unrestricted') {
            accessEl = document.createElement('span');
            accessEl.className = `auth-access auth-access-${access.status}`;
            if (access.status === 'trial') {
                accessEl.textContent = `Пробный · ${access.remaining_days} дн.`;
            } else if (access.status === 'subscription') {
                accessEl.textContent = `Доступ · ${access.remaining_days} дн.`;
            } else if (access.status === 'admin') {
                accessEl.textContent = 'Администратор';
            } else {
                accessEl.textContent = 'Доступ завершён';
            }
        }
        const logoutButton = document.createElement('button');
        logoutButton.className = 'btn btn-sm btn-outline';
        logoutButton.type = 'button';
        logoutButton.textContent = translateUi('Выйти');
        logoutButton.addEventListener('click', authLogout);
        bar.append(emailEl);
        if (accessEl) bar.append(accessEl);
        bar.append(logoutButton);
        return;
    }

    if (!authAvailable) {
        const localMode = document.createElement('span');
        localMode.className = 'auth-email';
        localMode.textContent = translateUi('Локальный режим');
        bar.append(localMode);
        return;
    }

    const loginButton = document.createElement('button');
    loginButton.className = 'btn btn-sm btn-outline';
    loginButton.type = 'button';
    loginButton.textContent = translateUi('Войти');
    loginButton.addEventListener('click', openAuthModal);
    bar.append(loginButton);
}

async function refreshAuthStatus() {
    try {
        const response = await fetch('/api/auth/me', {credentials: 'same-origin'});
        const data = await response.json();
        renderAuthBar(
            data.authenticated ? data.email : null,
            data.auth_available !== false,
            data.access
        );
        window.MEANX_ACCESS_STATE = data.access || window.MEANX_ACCESS_STATE;
        return data;
    } catch (_) {
        renderAuthBar();
        return null;
    }
}

async function requestMagicLink(event) {
    event?.preventDefault();
    const emailInput = document.getElementById('auth-email');
    const submitButton = document.getElementById('auth-submit');
    const email = emailInput?.value.trim();
    if (!email || !emailInput.checkValidity()) {
        emailInput?.reportValidity();
        return;
    }

    submitButton.disabled = true;
    submitButton.textContent = translateUi('Отправляем...');
    setAuthMessage('', 'hidden');
    try {
        const response = await fetch('/api/auth/magic-link', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                email,
                next_path: `${window.location.pathname}${window.location.search}`
            })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Не удалось отправить письмо');
        }
        setAuthMessage(data.message || 'Ссылка отправлена на почту', 'success');
        submitButton.textContent = translateUi('Отправить ещё раз');
    } catch (error) {
        setAuthMessage(error.message || 'Не удалось отправить письмо', 'error');
        submitButton.textContent = translateUi('Получить ссылку');
    } finally {
        submitButton.disabled = false;
    }
}

async function authLogout() {
    try {
        await fetch('/api/auth/logout', {
            method: 'POST',
            credentials: 'same-origin'
        });
    } finally {
        window.location.href = '/';
    }
}

document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!document.getElementById('auth-modal')?.classList.contains('hidden')) {
        closeAuthModal();
    } else if (!document.getElementById('paypal-checkout-modal')?.classList.contains('hidden')) {
        closePayPalCheckout();
    } else if (!document.getElementById('onboarding-modal')?.classList.contains('hidden')) {
        closeOnboarding();
    }
});

document.addEventListener('DOMContentLoaded', async () => {
    applyViewMode();
    const authState = await refreshAuthStatus();
    const url = new URL(window.location.href);
    const authResult = url.searchParams.get('auth');
    const checkoutPlan = url.searchParams.get('checkout');
    const paymentResult = url.searchParams.get('payment');
    const authenticated = authState?.authenticated === true;
    const authAvailable = authState?.auth_available !== false;
    const accessStatus = authState?.access?.status
        || window.MEANX_ACCESS_STATE?.status;
    if (authResult === 'success') {
        showToast('Вход выполнен', 'success');
    } else if (authResult === 'invalid') {
        openAuthModal();
        setAuthMessage('Ссылка недействительна или уже использована', 'error');
    }
    const checkoutPlans = ['month', 'year'];
    if (checkoutPlans.includes(checkoutPlan)) {
        if (!authenticated) {
            openAuthModal();
            setAuthMessage(
                authAvailable
                    ? 'Для оплаты сначала войдите по ссылке из письма'
                    : 'Вход временно недоступен. Попробуйте немного позже.',
                authAvailable ? '' : 'error'
            );
        } else {
            if (window.MEANX_PRODUCT_VARIANT === 'global') {
                window.location.replace(
                    `/api/payments/payanyway/checkout?plan=${encodeURIComponent(checkoutPlan)}`
                );
                return;
            }
            url.searchParams.delete('checkout');
            window.history.replaceState(
                {},
                '',
                `${url.pathname}${url.search}${url.hash}`
            );
            await openPayPalCheckout(checkoutPlan);
        }
    } else if (accessStatus === 'unauthenticated') {
        openAuthModal();
    }
    if (paymentResult === 'failed') {
        showToast('Оплата не завершена', 'error');
    }
    if (paymentResult) {
        url.searchParams.delete('payment');
    }
    if (authResult || paymentResult) {
        url.searchParams.delete('auth');
        window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
    }
});

// Toggle favorite
function toggleFavorite(pairId, tickerA, tickerB, signal, signalType, zAtEntry, priceA, priceB, halflife, corr, options = {}) {
    const btns = document.querySelectorAll(`.fav-btn[data-pair="${pairId}"]`);
    const params = new URLSearchParams();
    const appendNumber = (name, value) => {
        const raw = value === null || value === undefined ? '' : String(value).trim();
        if (!raw || raw === 'None' || raw === 'NaN' || raw === 'nan' || raw === '—') return;
        const num = Number(raw.replace(',', '.'));
        if (Number.isFinite(num)) params.set(name, String(num));
    };

    params.set('pair', pairId);
    params.set('ticker_a', tickerA || '');
    params.set('ticker_b', tickerB || '');
    params.set('signal', signal || '');
    params.set('signal_type', signalType || 'wait');
    params.set('market', currentMarket || 'crypto');
    params.set('position_kind', options.positionKind || 'pair');
    params.set('source', options.source || 'signal');
    appendNumber('z_at_entry', zAtEntry);
    appendNumber('price_a_entry', priceA);
    appendNumber('price_b_entry', priceB);
    appendNumber('halflife', halflife);
    appendNumber('corr', corr);
    const execution = getFavoritePnlSettings();
    appendNumber('capital', execution.capital);
    appendNumber('leverage', execution.leverage);
    appendNumber('taker_fee', execution.taker_fee);
    appendNumber('funding_rate', execution.funding_rate);

    fetch(`/api/favorites/toggle?${params.toString()}`, {
        method: 'POST'
    })
    .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (r.status === 401) openAuthModal();
        if (!r.ok) throw new Error(data.detail || 'Не удалось обновить портфель');
        return data;
    })
    .then(data => {
        if (data.action === 'added') {
            btns.forEach(b => { b.classList.add('favorited'); b.textContent = '★'; });
            showToast('Добавлено в портфель', 'success');
        } else {
            btns.forEach(b => { b.classList.remove('favorited'); b.textContent = '☆'; });
            showToast('Удалено из портфеля', '');

            // If on favorites tab, remove the position card immediately
            const card = document.getElementById('position-' + pairId);
            if (card) {
                card.style.transition = 'opacity 0.3s';
                card.style.opacity = '0';
                setTimeout(() => card.remove(), 300);
            }

            // Refresh favorites tab if it's currently visible
            const activeTab = document.querySelector('#active-positions');
            if (activeTab) {
                htmx.ajax('GET', favoritePnlUrl('/tab/favorites'), {target: '#main-content', swap: 'innerHTML'});
            }
        }
    })
    .catch(e => showToast(e.message || 'Ошибка портфеля', 'error'));
}

function toggleScannerFavorite(ticker, scanner, recommendationClass, recommendation) {
    const signalType = recommendationClass === 'short' ? 'short_a' : 'long_a';
    toggleFavorite(
        ticker,
        ticker,
        '',
        recommendation,
        signalType,
        '',
        0,
        0,
        '',
        '',
        {
            positionKind: 'single',
            source: `scanner_${scanner}`
        }
    );
}
window.toggleScannerFavorite = toggleScannerFavorite;

async function refreshRuFavorites(button) {
    if (!button || button.disabled) return;
    button.disabled = true;
    button.classList.add('is-loading');

    try {
        const response = await fetch('/api/favorites/refresh-ru', {
            method: 'POST',
            credentials: 'same-origin'
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) openAuthModal();
        if (!response.ok) {
            throw new Error(data.detail || 'Не удалось обновить котировки MOEX');
        }

        showToast(
            data.cached
                ? 'Котировки MOEX уже актуальны'
                : `Обновлено инструментов: ${data.updated}`,
            'success'
        );
        await htmx.ajax('GET', favoritePnlUrl('/tab/favorites'), {
            target: '#main-content',
            swap: 'innerHTML'
        });
    } catch (error) {
        showToast(error.message || 'Ошибка обновления MOEX', 'error');
    } finally {
        if (button.isConnected) {
            button.disabled = false;
            button.classList.remove('is-loading');
        }
    }
}

async function refreshCryptoFavorites(button) {
    if (!button || button.disabled) return;
    button.disabled = true;
    button.classList.add('is-loading');

    try {
        const response = await fetch('/api/favorites/refresh-crypto', {
            method: 'POST',
            credentials: 'same-origin'
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) openAuthModal();
        if (!response.ok) {
            throw new Error(data.detail || 'Не удалось обновить котировки MEXC');
        }

        showToast(
            data.cached
                ? 'Котировки MEXC уже актуальны'
                : `Обновлено crypto-инструментов: ${data.updated}`,
            'success'
        );
        await htmx.ajax('GET', favoritePnlUrl('/tab/favorites'), {
            target: '#main-content',
            swap: 'innerHTML'
        });
    } catch (error) {
        showToast(error.message || 'Ошибка обновления MEXC', 'error');
    } finally {
        if (button.isConnected) {
            button.disabled = false;
            button.classList.remove('is-loading');
        }
    }
}

// Close favorite position
function closeFavorite(favId) {
    if (!confirm(translateUi('Закрыть позицию?'))) return;
    const closeUrl = favoritePnlUrl(
        `/api/favorites/close/${favId}`,
        {use_net: true}
    );
    fetch(closeUrl, {
        method: 'POST'
    })
    .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (r.status === 401) openAuthModal();
        if (!r.ok) throw new Error(data.detail || 'Ошибка закрытия');
        return data;
    })
    .then(data => {
        if (data.action === 'closed') {
            showToast('Позиция закрыта', 'success');
            htmx.ajax('GET', favoritePnlUrl('/tab/favorites'), {target: '#main-content', swap: 'innerHTML'});
        }
    })
    .catch(e => showToast(e.message || 'Ошибка закрытия', 'error'));
}

// Toast notifications
function showToast(message, type = '') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`.trim();
    toast.setAttribute('role', 'status');
    toast.textContent = translateUi(message);
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}

// Onboarding
let onboardStep = 1;

function onboardNext() {
    if (onboardStep < 3) {
        document.getElementById('os-' + onboardStep).classList.remove('active');
        document.querySelector(`.dot[data-dot="${onboardStep}"]`).classList.remove('active');
        onboardStep++;
        document.getElementById('os-' + onboardStep).classList.add('active');
        document.querySelector(`.dot[data-dot="${onboardStep}"]`).classList.add('active');
    }
    updateOnboardButtons();
}

function onboardPrev() {
    if (onboardStep > 1) {
        document.getElementById('os-' + onboardStep).classList.remove('active');
        document.querySelector(`.dot[data-dot="${onboardStep}"]`).classList.remove('active');
        onboardStep--;
        document.getElementById('os-' + onboardStep).classList.add('active');
        document.querySelector(`.dot[data-dot="${onboardStep}"]`).classList.add('active');
    }
    updateOnboardButtons();
}

function updateOnboardButtons() {
    document.getElementById('onboard-prev').style.display = onboardStep > 1 ? '' : 'none';
    document.getElementById('onboard-next-btn').style.display = onboardStep < 3 ? '' : 'none';
    document.getElementById('onboard-finish-btn').style.display = onboardStep === 3 ? '' : 'none';
}

function closeOnboarding() {
    document.getElementById('onboarding-modal')?.classList.add('hidden');
    document.body.classList.remove('modal-open');
    try {
        localStorage.setItem('cryptoscope_onboarded', 'true');
    } catch (_) {}
}

// Show onboarding on first visit — unless the auth modal takes priority
// (both opening at once would stack two modals on top of each other).
document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('onboarding-modal');
    if (!modal) return;
    const seen = localStorage.getItem('cryptoscope_onboarded');
    const authPending = window.MEANX_ACCESS_STATE?.status === 'unauthenticated';
    if (!seen && !authPending) {
        modal.classList.remove('hidden');
        document.body.classList.add('modal-open');
    }
    updateOnboardButtons();
});

// Wake lock is requested once in base.html — do not duplicate it here.

document.body.addEventListener('htmx:responseError', function(evt) {
    const detail = evt.detail || {};
    const request = detail.requestConfig || {};
    const status = detail.xhr ? detail.xhr.status : 0;
    const method = String(request.verb || '').toUpperCase();
    const source = detail.elt;
    const retryable = method === 'GET' && [502, 503, 504].includes(status);

    if (retryable && source && typeof htmx !== 'undefined') {
        const attempt = Number(source.dataset.meanxRetryAttempt || 0);
        const path = request.path || source.getAttribute('hx-get');
        if (attempt < 2 && path) {
            source.dataset.meanxRetryAttempt = String(attempt + 1);
            const target = detail.target || request.target;
            const swap = request.swap || source.getAttribute('hx-swap') || 'innerHTML';
            window.setTimeout(() => {
                htmx.ajax('GET', path, {source, target, swap});
            }, 700 * (attempt + 1));
            return;
        }
    }
    showToast('Ошибка загрузки данных', 'error');
});

document.body.addEventListener('htmx:afterRequest', function(evt) {
    if (evt.detail && evt.detail.successful && evt.detail.elt) {
        delete evt.detail.elt.dataset.meanxRetryAttempt;
    }
});

function ensureInitialSignalsLoaded() {
    const content = document.getElementById('signals-content');
    if (!content || content.querySelector('.fav-btn')) return;
    if (!content.querySelector('.loading-container')) return;
    if (content.dataset.fallbackLoading === '1') return;
    if (typeof htmx === 'undefined') return;

    content.dataset.fallbackLoading = '1';
    const market = selectedMarket();
    content.dataset.requestMarket = market;
    htmx.ajax('GET', '/tab/signals', {
        source: content,
        target: '#signals-content',
        swap: 'innerHTML',
        values: {mode: 'all', market}
    });
}

// Leverage slider display + signal filter value labels.
// Delegated to document so it also works inside htmx-swapped content
// (e.g. the calculator copy on the Data tab).
document.addEventListener('input', event => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.id === 'calc-leverage') {
        const label = document.getElementById('leverage-value');
        if (label) label.textContent = target.value + 'x';
    } else if (target.id === 'filter-corr') {
        const label = document.getElementById('corr-value');
        if (label) label.textContent = target.value;
    } else if (target.id === 'filter-days') {
        const label = document.getElementById('days-value');
        if (label) label.textContent = target.value;
    }
});

document.addEventListener('DOMContentLoaded', () => {
    setTimeout(ensureInitialSignalsLoaded, 500);
});

// Ticker logos — load crypto icons from CDN.
// Results are cached per ticker so repeated htmx swaps do not re-fire
// a HEAD request for every logo every time.
const tickerLogoCache = new Map();

function applyTickerLogo(el, base, url) {
    if (url) {
        el.style.backgroundImage = `url(${url})`;
        el.classList.add('has-logo');
    } else {
        el.textContent = base.slice(0, 2).toUpperCase();
    }
}

function loadTickerLogos() {
    document.querySelectorAll('.ticker-logo[data-ticker]').forEach(el => {
        if (el.dataset.logoResolved === '1') return;
        const ticker = el.dataset.ticker;
        const base = ticker.split('/')[0].split('.')[0].toLowerCase();
        el.dataset.logoResolved = '1';
        if (tickerLogoCache.has(base)) {
            applyTickerLogo(el, base, tickerLogoCache.get(base));
            return;
        }
        const url = `https://cdnjs.cloudflare.com/ajax/libs/cryptocurrency-icons/0.18.1/svg/color/${base}.svg`;
        fetch(url, { method: 'HEAD' })
            .then(r => {
                const resolved = r.ok ? url : null;
                tickerLogoCache.set(base, resolved);
                applyTickerLogo(el, base, resolved);
            })
            .catch(() => {
                tickerLogoCache.set(base, null);
                applyTickerLogo(el, base, null);
            });
    });
}

document.body.addEventListener('htmx:afterSwap', function() {
    loadTickerLogos();
});
