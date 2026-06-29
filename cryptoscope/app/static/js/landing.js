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
                status.textContent = 'PayPal временно недоступен. Попробуйте позже.';
                status.classList.remove('hidden');
            }
        });
    };

    renderPayPalButton();
    document.getElementById('paypal-sdk')?.addEventListener('load', renderPayPalButton);
});
