document.addEventListener('DOMContentLoaded', () => {
    if (window.lucide) {
        window.lucide.createIcons({
            attrs: {
                'stroke-width': 1.8
            }
        });
    }

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
