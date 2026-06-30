document.addEventListener('DOMContentLoaded', () => {
    if (window.lucide) {
        window.lucide.createIcons({
            attrs: {
                'stroke-width': 1.8
            }
        });
    }

    const renderPayPalButton = () => {
        const container = document.getElementById('paypal-container-DNWAM39RY9XML');
        const status = document.getElementById('paypal-status');
        if (!container || container.dataset.rendered === 'true') return;
        if (!window.paypal?.HostedButtons) return;

        container.dataset.rendered = 'true';
        window.paypal.HostedButtons({
            hostedButtonId: 'DNWAM39RY9XML'
        })
        .render('#paypal-container-DNWAM39RY9XML')
        .catch(() => {
            container.dataset.rendered = 'false';
            if (status) {
                const message = 'PayPal временно недоступен. Попробуйте позже.';
                status.textContent = window.translateUi
                    ? window.translateUi(message)
                    : message;
                status.classList.remove('hidden');
            }
        });
    };

    renderPayPalButton();
    document.getElementById('paypal-sdk')?.addEventListener('load', renderPayPalButton);
});

async function changeLocale(locale) {
    const response = await fetch(`/api/locale?lang=${encodeURIComponent(locale)}`, {
        method: 'POST',
        credentials: 'same-origin'
    });
    if (response.ok) window.location.reload();
}

window.changeLocale = changeLocale;

const uiTranslations = window.CRYPTOSCOPE_TRANSLATIONS || {};
const uiTranslationKeys = Object.keys(uiTranslations).sort((a, b) => b.length - a.length);
window.translateUi = window.translateUi || function translateUi(value) {
    let translated = String(value ?? '');
    uiTranslationKeys.forEach(source => {
        translated = translated.split(source).join(uiTranslations[source]);
    });
    return translated;
};
