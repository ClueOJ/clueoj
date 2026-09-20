/* Native details remain usable without JavaScript. Preview on hover/focus,
 * pin with click/Enter/Space, dismiss with Escape; explanations stay in flow. */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.exam-milestone-note').forEach(function (detail) {
        const summary = detail.querySelector('summary');
        let pinned = detail.open;
        let suppressed = false;
        function show(open) {
            detail.open = open;
            summary.setAttribute('aria-expanded', String(open));
        }
        show(pinned);
        detail.addEventListener('pointerenter', function (event) {
            if (event.pointerType === 'mouse' && !suppressed) show(true);
        });
        detail.addEventListener('pointerleave', function () {
            suppressed = false;
            if (!pinned && !detail.contains(document.activeElement)) show(false);
        });
        detail.addEventListener('focusin', function () {
            if (!suppressed) show(true);
        });
        detail.addEventListener('focusout', function (event) {
            if (!detail.contains(event.relatedTarget)) {
                suppressed = false;
                if (!pinned && !detail.matches(':hover')) show(false);
            }
        });
        summary.addEventListener('click', function (event) {
            event.preventDefault();
            pinned = !pinned;
            suppressed = !pinned;
            show(pinned);
        });
        detail.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                pinned = false;
                suppressed = true;
                show(false);
                summary.focus();
            }
        });
        detail.addEventListener('toggle', function () {
            summary.setAttribute('aria-expanded', String(detail.open));
        });
    });
    document.querySelectorAll('.exam-milestones-disclosure').forEach(function (detail) {
        const confirmation = detail.querySelector('.exam-milestone-confirm');
        const cancel = detail.querySelector('.exam-milestone-cancel');
        const message = detail.querySelector('.exam-milestone-confirm-message');
        if (confirmation && cancel && message) {
            cancel.hidden = false;
            cancel.addEventListener('click', function (event) {
                event.preventDefault();
                event.stopPropagation();
                confirmation.open = false;
                message.hidden = false;
                cancel.hidden = false;
                detail.open = false;
                detail.querySelector('summary').focus();
            });
            confirmation.addEventListener('toggle', function () {
                message.hidden = confirmation.open;
                cancel.hidden = confirmation.open;
            });
            detail.addEventListener('toggle', function () {
                if (!detail.open) confirmation.open = false;
            });
        }
        detail.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                detail.open = false;
                detail.querySelector('summary').focus();
            }
        });
    });
});
