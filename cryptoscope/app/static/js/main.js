// CryptoScope — Main JavaScript

// Global market state
let currentMarket = window.CRYPTOSCOPE_INITIAL_MARKET || 'crypto';
window.currentMarket = currentMarket;

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

function switchMarket(market) {
    currentMarket = market;
    window.currentMarket = market;
    document.querySelectorAll('.market-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.market === market);
    });
    // Refresh dashboard and signals for new market
    htmx.ajax('GET', '/tab/dashboard?market=' + market, {target: '#signals-dashboard', swap: 'outerHTML'});
    const activeMode = document.querySelector('.mode-btn.active');
    const mode = activeMode ? activeMode.dataset.mode : 'all';
    htmx.ajax('GET', '/tab/signals?mode=' + mode + '&market=' + market, {target: '#signals-content', swap: 'innerHTML'});
}
window.switchMarket = switchMarket;

function filterPolymarket(category, button) {
    document.querySelectorAll('.poly-filter-btn').forEach(item => {
        item.classList.toggle('active', item === button);
    });
    document.querySelectorAll('[data-poly-category]').forEach(row => {
        row.hidden = category !== 'all' && row.dataset.polyCategory !== category;
    });
}
window.filterPolymarket = filterPolymarket;

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

function setAuthMessage(message, type = '') {
    const messageEl = document.getElementById('auth-message');
    if (!messageEl) return;
    messageEl.textContent = translateUi(message);
    messageEl.className = `auth-message ${type}`.trim();
}

function renderAuthBar(email = null, authAvailable = true) {
    const bar = document.getElementById('auth-bar');
    if (!bar) return;
    bar.replaceChildren();

    if (email) {
        const emailEl = document.createElement('span');
        emailEl.className = 'auth-email';
        emailEl.textContent = email;
        const logoutButton = document.createElement('button');
        logoutButton.className = 'btn btn-sm btn-outline';
        logoutButton.type = 'button';
        logoutButton.textContent = translateUi('Выйти');
        logoutButton.addEventListener('click', authLogout);
        bar.append(emailEl, logoutButton);
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
            data.auth_available !== false
        );
    } catch (_) {
        renderAuthBar();
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
            body: JSON.stringify({email})
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
    if (event.key === 'Escape' && !document.getElementById('auth-modal')?.classList.contains('hidden')) {
        closeAuthModal();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    refreshAuthStatus();
    const url = new URL(window.location.href);
    const authResult = url.searchParams.get('auth');
    if (authResult === 'success') {
        showToast('Вход выполнен', 'success');
    } else if (authResult === 'invalid') {
        openAuthModal();
        setAuthMessage('Ссылка недействительна или уже использована', 'error');
    }
    if (authResult) {
        url.searchParams.delete('auth');
        window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
    }
});

// Toggle favorite
function toggleFavorite(pairId, tickerA, tickerB, signal, signalType, zAtEntry, priceA, priceB, halflife, corr) {
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
    appendNumber('z_at_entry', zAtEntry);
    appendNumber('price_a_entry', priceA);
    appendNumber('price_b_entry', priceB);
    appendNumber('halflife', halflife);
    appendNumber('corr', corr);

    fetch(`/api/favorites/toggle?${params.toString()}`, {
        method: 'POST'
    })
    .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (r.status === 401) openAuthModal();
        if (!r.ok) throw new Error(data.detail || 'Не удалось обновить избранное');
        return data;
    })
    .then(data => {
        if (data.action === 'added') {
            btns.forEach(b => { b.classList.add('favorited'); b.textContent = '★'; });
            showToast('Добавлено в избранное', 'success');
        } else {
            btns.forEach(b => { b.classList.remove('favorited'); b.textContent = '☆'; });
            showToast('Удалено из избранного', '');

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
    .catch(e => showToast(e.message || 'Ошибка избранного', 'error'));
}

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
function showToast(message, type) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
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
    document.getElementById('onboarding-modal').classList.add('hidden');
}

// Show onboarding on first visit
document.addEventListener('DOMContentLoaded', () => {
    const seen = localStorage.getItem('cryptoscope_onboarded');
    if (!seen) {
        document.getElementById('onboarding-modal').classList.remove('hidden');
    }
    
    // Will set this when onboarding is closed
    updateOnboardButtons();
});

// ... rest of closeOnboarding to save state
const origClose = closeOnboarding;
closeOnboarding = function() {
    localStorage.setItem('cryptoscope_onboarded', 'true');
    origClose();
};

// Wake lock
if ('wakeLock' in navigator) {
    try {
        navigator.wakeLock.request('screen');
    } catch(e) {}
}

// HTMX extensions
document.body.addEventListener('htmx:afterSwap', function(evt) {
    // Re-initialize any dynamic content
});

document.body.addEventListener('htmx:responseError', function(evt) {
    showToast('Ошибка загрузки данных', 'error');
});

// Swap tickers for spread chart
function swapTickers() {
    const a = document.getElementById('spread-a');
    const b = document.getElementById('spread-b');
    const tmp = a.value;
    a.value = b.value;
    b.value = tmp;
    a.dispatchEvent(new Event('change'));
}

// Real calculator — fetch P&L from API
function updateCalculator(cardId, tickerA, tickerB, signalType, zNow, halflife) {
    const capital = document.getElementById('calc-capital')?.value || 1000;
    const leverage = document.getElementById('calc-leverage')?.value || 3;
    const takerFee = document.getElementById('calc-taker')?.value || 0.02;
    const fundingRate = document.getElementById('calc-funding')?.value || 0.01;
    
    const zMove = Math.abs(zNow || 2);
    const holdDays = halflife ? Math.min(halflife, 30) : 5;
    
    fetch(`/api/signals/pnl?capital=${capital}&leverage=${leverage}&taker_fee=${takerFee}&funding_rate=${fundingRate}&hold_days=${holdDays}&z_move=${zMove}`)
        .then(r => r.json())
        .then(data => {
            const block = document.getElementById(`calc-${cardId}`);
            if (!block) return;
            
            const pnlClass = data.net_pnl >= 0 ? 'positive' : 'negative';
            const pnlSign = data.net_pnl >= 0 ? '+' : '';
            
            block.innerHTML = `
                <div class="calc-row">
                    <div class="calc-field">
                        <label>Позиция</label>
                        <div class="calc-value">$${data.position_size.toLocaleString()}</div>
                    </div>
                    <div class="calc-field">
                        <label>Комиссия</label>
                        <div class="calc-value text-dim">$${data.commissions.toFixed(2)}</div>
                    </div>
                    <div class="calc-field">
                        <label>Фандинг</label>
                        <div class="calc-value text-dim">$${data.funding_cost.toFixed(2)}</div>
                    </div>
                    <div class="calc-field">
                        <label>P&L netto</label>
                        <div class="calc-value ${pnlClass}">${pnlSign}$${data.net_pnl.toFixed(2)}</div>
                    </div>
                </div>
            `;
        })
        .catch(() => {});
}

// Initialize calculators for all signal cards
function initCalculators() {
    document.querySelectorAll('.signal-card').forEach(card => {
        const pairId = card.id.replace('card-', '');
        const ta = card.dataset.tickerA;
        const tb = card.dataset.tickerB;
        const st = card.dataset.signalType;
        const z = parseFloat(card.dataset.zNow);
        const hl = parseInt(card.dataset.halflife);
        if (pairId && ta) {
            updateCalculator(pairId, ta, tb, st, z, hl);
        }
    });
}

function ensureInitialSignalsLoaded() {
    const content = document.getElementById('signals-content');
    if (!content || content.querySelector('.fav-btn')) return;
    if (!content.querySelector('.loading-container')) return;
    if (content.dataset.fallbackLoading === '1') return;
    if (typeof htmx === 'undefined') return;

    content.dataset.fallbackLoading = '1';
    const market = currentMarket || 'crypto';
    htmx.ajax('GET', `/tab/signals?mode=all&market=${encodeURIComponent(market)}`, {
        target: '#signals-content',
        swap: 'innerHTML'
    });
}

// Leverage slider display + calc settings handlers
document.addEventListener('DOMContentLoaded', () => {
    const levSlider = document.getElementById('calc-leverage');
    if (levSlider) {
        levSlider.addEventListener('input', function() {
            document.getElementById('leverage-value').textContent = this.value + 'x';
            initCalculators();
        });
    }
    
    // Recalculate on capital/fee/funding change
    ['calc-capital', 'calc-taker', 'calc-funding'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('change', initCalculators);
            el.addEventListener('input', initCalculators);
        }
    });
    
    // Init calculators after HTMX loads signals
    document.body.addEventListener('htmx:afterSwap', function(evt) {
        if (evt.detail.target.id === 'signals-content') {
            setTimeout(initCalculators, 100);
        }
    });

    setTimeout(ensureInitialSignalsLoaded, 500);
});

// Ticker logos — load crypto icons from CDN
function loadTickerLogos() {
    document.querySelectorAll('.ticker-logo[data-ticker]').forEach(el => {
        const ticker = el.dataset.ticker;
        const base = ticker.split('/')[0].split('.')[0].toLowerCase();
        // Try cryptocurrency-icon CDN
        const url = `https://cdnjs.cloudflare.com/ajax/libs/cryptocurrency-icons/0.18.1/svg/color/${base}.svg`;
        fetch(url, { method: 'HEAD' })
            .then(r => {
                if (r.ok) {
                    el.style.backgroundImage = `url(${url})`;
                    el.classList.add('has-logo');
                } else {
                    el.textContent = base.slice(0, 2).toUpperCase();
                }
            })
            .catch(() => {
                el.textContent = base.slice(0, 2).toUpperCase();
            });
    });
}

document.body.addEventListener('htmx:afterSwap', function() {
    loadTickerLogos();
});
