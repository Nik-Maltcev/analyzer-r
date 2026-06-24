// CryptoScope — Main JavaScript

// Global market state
let currentMarket = 'crypto';

function switchMarket(market) {
    currentMarket = market;
    document.querySelectorAll('.market-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.market === market);
    });
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
    const btn = document.querySelector(`.fav-btn[data-pair="${pairId}"]`);
    
    fetch(`/api/favorites/toggle?pair=${encodeURIComponent(pairId)}&ticker_a=${tickerA}&ticker_b=${tickerB}&signal=${encodeURIComponent(signal)}&signal_type=${signalType}&z_at_entry=${zAtEntry}&price_a_entry=${priceA}&price_b_entry=${priceB}&halflife=${halflife || ''}&corr=${corr || 0}`, {
        method: 'POST'
    })
    .then(r => r.json())
    .then(data => {
        if (data.action === 'added') {
            btn.classList.add('favorited');
            btn.textContent = '★';
            showToast('Добавлено в избранное', 'success');
        } else {
            btn.classList.remove('favorited');
            btn.textContent = '☆';
            showToast('Удалено из избранного', '');
        }
    });
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
