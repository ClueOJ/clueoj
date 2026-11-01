class InvocationCellSelection {
    constructor() {
        this.selected = new Set();
        this.anchor = null;
    }

    key(row, column) {
        return `${row}:${column}`;
    }

    has(row, column) {
        return this.selected.has(this.key(row, column));
    }

    toggle(row, column, extend = false) {
        if (extend && this.anchor) {
            const [anchorRow, anchorColumn] = this.anchor;
            for (let r = Math.min(row, anchorRow); r <= Math.max(row, anchorRow); r++) {
                for (let c = Math.min(column, anchorColumn); c <= Math.max(column, anchorColumn); c++) {
                    this.selected.add(this.key(r, c));
                }
            }
        } else {
            const key = this.key(row, column);
            if (this.selected.has(key)) this.selected.delete(key);
            else this.selected.add(key);
            this.anchor = [row, column];
        }
    }

    clear() {
        this.selected.clear();
        this.anchor = null;
    }
}

(function () {
    if (typeof document === 'undefined') return;

    function initialize() {
        const table = document.querySelector('.invocation-sources-table');
        if (!table) return;
        const selection = new InvocationCellSelection();
        const cells = [...table.querySelectorAll('[data-tag-row][data-tag-col]')];
        const value = document.getElementById('invocation-bulk-status');
        const apply = document.getElementById('invocation-bulk-apply');
        const clear = document.getElementById('invocation-bulk-clear');
        const count = document.getElementById('invocation-selected-count');
        const settings = document.getElementById('invocation-settings');
        const save = document.getElementById('invocation-save-settings');
        const run = document.getElementById('invocation-run-button');
        const remove = document.getElementById('invocation-delete-button');
        const selectedForm = document.getElementById('invocation-run');
        const sourceChecks = [...table.querySelectorAll('input[form="invocation-run"][name="source"]')];
        const selectAll = document.getElementById('invocation-select-all');
        const state = document.getElementById('invocation-settings-state');
        const fields = [...table.querySelectorAll('select[form="invocation-settings"]')];
        const original = new Map(fields.map(field => [field, field.value]));
        const stale = Boolean(table.querySelector('.invocation-source-stale'));

        function refreshDirtyState() {
            const dirty = stale || fields.some(field => field.value !== original.get(field));
            const selected = sourceChecks.filter(input => input.checked).length;
            save.disabled = !dirty;
            run.disabled = dirty || selected === 0;
            remove.disabled = dirty || selected === 0;
            selectAll.checked = selected === sourceChecks.length;
            selectAll.indeterminate = selected > 0 && selected < sourceChecks.length;
            state.textContent = dirty ? state.dataset.dirtyMessage : '';
        }

        function refreshSelection() {
            for (const cell of cells) {
                const selected = selection.has(Number(cell.dataset.tagRow), Number(cell.dataset.tagCol));
                cell.classList.toggle('is-selected', selected);
                cell.querySelector('.invocation-cell-check').checked = selected;
            }
            count.textContent = String(selection.selected.size);
            clear.disabled = selection.selected.size === 0;
            apply.disabled = selection.selected.size === 0 || value.value === '__choose';
        }

        for (const cell of cells) cell.querySelector('.invocation-cell-check').hidden = false;
        document.getElementById('invocation-bulk-tools').hidden = false;

        table.addEventListener('click', event => {
            const cell = event.target.closest('[data-tag-row][data-tag-col]');
            if (!cell || event.target.closest('select, option')) return;
            selection.toggle(Number(cell.dataset.tagRow), Number(cell.dataset.tagCol), event.shiftKey);
            refreshSelection();
        });

        table.addEventListener('change', event => {
            if (event.target.matches('select[form="invocation-settings"], input[form="invocation-run"][name="source"]')) {
                refreshDirtyState();
            }
        });
        value.addEventListener('change', refreshSelection);
        clear.addEventListener('click', () => {
            selection.clear();
            refreshSelection();
        });
        apply.addEventListener('click', () => {
            if (apply.disabled) return;
            for (const cell of cells) {
                if (selection.has(Number(cell.dataset.tagRow), Number(cell.dataset.tagCol))) {
                    cell.querySelector('select').value = value.value;
                }
            }
            refreshDirtyState();
        });
        table.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                selection.clear();
                refreshSelection();
            }
        });
        settings.addEventListener('submit', () => { save.disabled = true; });
        selectedForm.addEventListener('submit', event => {
            // common.js disables submit buttons before form serialization.
            // Keep the chosen action in a hidden input so it is still submitted.
            document.getElementById('invocation-selected-action').value =
                event.submitter?.value === 'delete_sources' ? 'delete_sources' : 'run';
        });
        selectAll.addEventListener('change', event => {
            sourceChecks.forEach(input => {
                input.checked = event.target.checked;
            });
            refreshDirtyState();
        });
        refreshDirtyState();
        refreshSelection();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
})();
