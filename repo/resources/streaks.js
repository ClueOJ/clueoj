/* Progressive enhancement: the annual calendar remains usable without JS. */
(() => {
    const page = document.querySelector('.streak-page');
    if (!page) return;
    const switcher = page.querySelector('.streak-view-switch');
    const picker = page.querySelector('.streak-month-picker');
    const select = page.querySelector('#streak-month-select');
    const months = [...page.querySelectorAll('[data-month]')];
    const buttons = [...page.querySelectorAll('[data-streak-view]')];
    const params = new URLSearchParams(window.location.search);
    const requested = params.get('view');
    const month = Number(params.get('month'));
    if (month >= 1 && month <= 12 && Number.isInteger(month)) select.value = String(month);
    function updateMonth() {
        months.forEach(panel => { panel.hidden = panel.dataset.month !== select.value; });
    }
    function setView(view) {
        page.dataset.view = view;
        buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.streakView === view)));
        picker.hidden = view !== 'month';
        updateMonth();
    }
    buttons.forEach(button => button.addEventListener('click', () => setView(button.dataset.streakView)));
    select.addEventListener('change', updateMonth);
    // Keep the chosen presentation through day, year, and history navigation.
    page.addEventListener('click', event => {
        const link = event.target.closest('a[href^="?year="]');
        if (!link) return;
        const url = new URL(link.href);
        url.searchParams.set('view', page.dataset.view);
        url.searchParams.set('month', url.searchParams.has('day') ? String(Number(url.searchParams.get('day').slice(5, 7))) : select.value);
        link.href = url.toString();
    });
    setView(['month', 'year'].includes(requested) ? requested : (window.matchMedia('(max-width: 600px)').matches ? 'month' : 'year'));
    switcher.hidden = false;
})();
