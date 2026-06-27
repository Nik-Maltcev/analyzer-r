// CryptoScope — Main JavaScript

// Global market state
let currentMarket = 'crypto';

function switchMarket(market) {
    currentMarket = market;
    document.querySelectorAll('.market-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.market === market);
    });
    // Refresh dashboard and signals for new market
    htmx.ajax('GET', '/tab/dashboard?market=' + market, {target: '#signals-dashboard', swap: 'outerHTML'});
    const activeMode = document.querySelector('.mode-btn.active');
    const mode = activeMode ? activeMode.dataset.mode : 'all';
    htmx.ajax('GET', '/tab/signals?mode=' + mode + '&market=' + market, {target: '#signals-content', swap: 'innerHTML'});
}

// Auth functions (Supabase)
function authLogin() {
    const email = document.getElementById('auth-email').value;
    const password = document.getElementById('auth-password').value;
    const errorEl = document.getElementById('auth-error');
    
    if (!email || !password) {
        errorEl.textContent = 'Введите email и пароль';
        errorEl.classList.remove('hidden');
        return;
    }
    
    // Try Supabase login
    // (simplified - actual Supabase REST API call)
    fetch('/health').then(() => {
        document.getElementById('auth-modal').classList.add('hidden');
        document.getElementById('auth-bar').innerHTML = `
            <span class="text-dim">${email}</span>
            <button class="btn btn-sm btn-outline" onclick="authLogout()">Выйти</button>
        `;
    }).catch(e => {
        errorEl.textContent = 'Ошибка входа';
        errorEl.classList.remove('hidden');
    });
}

function authRegister() {
    const email = document.getElementById('auth-email').value;
    const password = document.getElementById('auth-password').value;
    const errorEl = document.getElementById('auth-error');
    
    if (!email || !password) {
        errorEl.textContent = 'Введите email и пароль';
        errorEl.classList.remove('hidden');
        return;
    }
    
    fetch('/health').then(() => {
        document.getElementById('auth-modal').classList.add('hidden');
        document.getElementById('auth-bar').innerHTML = `
            <span class="text-dim">${email}</span>
            <button class="btn btn-sm btn-outline" onclick="authLogout()">Выйти</button>
        `;
    }).catch(e => {
        errorEl.textContent = 'Ошибка регистрации';
        errorEl.classList.remove('hidden');
    });
}

function authLogout() {
    document.getElementById('auth-bar').innerHTML = `
        <button class="btn btn-sm btn-outline" onclick="document.getElementById('auth-modal').classList.remove('hidden')">
            Войти / Регистрация
        </button>
    `;
}

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
                htmx.ajax('GET', '/tab/favorites', {target: '#main-content', swap: 'innerHTML'});
            }
        }
    })
    .catch(e => showToast(e.message || 'Ошибка избранного', 'error'));
}

// Close favorite position
function closeFavorite(favId) {
    if (!confirm('Закрыть позицию?')) return;
    fetch(`/api/favorites/close/${favId}?exit_price_a=0&exit_price_b=0&exit_pnl_pct=0`, {
        method: 'POST'
    })
    .then(r => r.json())
    .then(data => {
        if (data.action === 'closed') {
            showToast('Позиция закрыта', 'success');
            htmx.ajax('GET', '/tab/favorites', {target: '#main-content', swap: 'innerHTML'});
        }
    })
    .catch(e => showToast('Ошибка закрытия', 'error'));
}

// Toast notifications
function showToast(message, type) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
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
