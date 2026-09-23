/* Native disclosure remains usable without JavaScript. Compact months on mobile. */
(function () {
    'use strict';
    var wide = window.matchMedia('(min-width: 768px)');
    var months = document.querySelectorAll('.streak-month');
    function layout() {
        var container = document.querySelector('.streak-months');
        if (!container) return;
        if (!wide.matches) {
            months.forEach(function (month) {
                if (month.dataset.preferred === 'true') container.appendChild(month);
            });
        }
        months.forEach(function (month) {
            if (wide.matches || month.dataset.preferred !== 'true') container.appendChild(month);
        });
        months.forEach(function (month) {
            month.open = wide.matches || month.dataset.preferred === 'true';
        });
    }
    layout();
    if (wide.addEventListener) {
        wide.addEventListener('change', layout);
    }
}());
